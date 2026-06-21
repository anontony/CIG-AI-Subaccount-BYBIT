import asyncio
import json
import hashlib
import os
import time
import re
from decimal import Decimal
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ai_engine import DecisionEngine, compact_kline_summary
from indicator_engine import calculate_indicators
from skill_runtime import build_skill_context
from skill_sync import background_update_once, check_and_update_skill, read_status
from rsa_keys import generate_rsa_key_pair, public_key_from_private
from bybit_client import BybitAPIError, BybitClient
from risk_guard import RiskConfig, RiskError, RiskGuard
from manual_parser import parse_direct_command, is_clear_trade_execution_command
from control_parser import parse_control_command, redact_command_for_log
from session_auth import COOKIE_NAME, create_session_token, read_session_token
from state import RuntimeManager, UserRuntimeState
from storage import StoreError, UserStore, mask_secret
from trade_tracker import OPENING_ACTIONS, CLOSING_ACTIONS, pnl_snapshot, side_from_action
from strategy_parser import parse_strategy_prompt, summarize_strategy_directives

load_dotenv()

app = FastAPI(title="CIG AI Subaccount", version="44.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
store = UserStore()
runtimes = RuntimeManager(log_store=store)


class AuthIn(BaseModel):
    username: str
    password: str


class PromptIn(BaseModel):
    prompt: str


class SettingsIn(BaseModel):
    bybit_api_key: Optional[str] = None
    bybit_api_secret: Optional[str] = None
    bybit_api_private_key: Optional[str] = None
    bybit_auth_type: Optional[str] = None
    bybit_env: Optional[str] = None
    recv_window: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_model: Optional[str] = None
    allowed_symbols: Optional[str] = None
    default_category: Optional[str] = None
    dry_run: Optional[bool] = None
    max_leverage: Optional[int] = None
    max_margin_per_trade_usdt: Optional[str] = None
    max_notional_usdt: Optional[str] = None
    max_daily_trades: Optional[int] = None
    require_tp_sl: Optional[bool] = None
    default_take_profit_pct: Optional[str] = None
    default_stop_loss_pct: Optional[str] = None
    min_seconds_between_trades: Optional[int] = None
    loop_interval_seconds: Optional[int] = None
    ai_cost_saver: Optional[bool] = None


class CommandIn(BaseModel):
    command: str



@app.on_event("startup")
async def startup_skill_update() -> None:
    # Auto-update Bybit Skill in the background. The app never waits for it.
    asyncio.create_task(background_update_once())


def current_user(request: Request) -> Dict[str, Any]:
    token = request.cookies.get(COOKIE_NAME, "")
    user_id = read_session_token(token) if token else None
    if not user_id:
        raise HTTPException(status_code=401, detail="Chưa đăng nhập.")
    user = store.get_user(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Session không hợp lệ.")
    return user


def get_workspace(user_id: int, redact: bool = False) -> Dict[str, Any]:
    try:
        return store.get_workspace(user_id, redact=redact)
    except StoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def make_client(settings: Dict[str, Any]) -> BybitClient:
    return BybitClient(
        api_key=str(settings.get("bybit_api_key") or ""),
        api_secret=str(settings.get("bybit_api_secret") or ""),
        api_private_key=str(settings.get("bybit_api_private_key") or ""),
        auth_type=str(settings.get("bybit_auth_type") or "auto"),
        env=str(settings.get("bybit_env") or "testnet"),
        recv_window=str(settings.get("recv_window") or "5000"),
    )


def make_engine(settings: Dict[str, Any]) -> DecisionEngine:
    return DecisionEngine(
        api_key=str(settings.get("openai_api_key") or ""),
        model=str(settings.get("openai_model") or "gpt-4o-mini"),
    )


def make_risk_guard(settings: Dict[str, Any], runtime: UserRuntimeState) -> RiskGuard:
    guard = RiskGuard(RiskConfig.from_settings(settings))
    guard.last_trade_ts = runtime.last_trade_ts
    return guard


def allowed_symbols_from_guard(guard: RiskGuard) -> list[str]:
    return sorted(guard.config.allowed_symbols)


def json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def workspace_preflight(ws: Dict[str, Any], runtime: UserRuntimeState) -> Dict[str, Any]:
    settings = ws["settings"]
    client = make_client(settings)
    engine = make_engine(settings)
    guard = make_risk_guard(settings, runtime)

    errors: list[str] = []
    warnings: list[str] = []

    if not client.is_configured:
        if client.selected_auth_type == "rsa":
            errors.append("Thiếu Bybit API Key hoặc RSA private key. Hãy tạo RSA public key riêng cho user này rồi dán API Key Bybit trả về.")
        else:
            errors.append("Thiếu Bybit API Key/Secret. Vào Cài đặt API & Rủi ro để nhập key sub-account.")
    if not allowed_symbols_from_guard(guard):
        errors.append("Allowed Symbols đang rỗng.")
    if not ws.get("prompt"):
        warnings.append("Chưa có prompt strategy. Save Prompt trước khi Start hoặc Run Once.")
    if not engine.enabled:
        warnings.append("Thiếu OpenAI API Key. Strategy prompt không thể tự phân tích; Direct Command chỉ dùng parser đơn giản.")
    notes = [
        "Save Prompt chỉ lưu/ghi đè prompt, không tự vào lệnh.",
        "Muốn chạy prompt: bấm Start để chạy loop hoặc Run Prompt Once để chạy một vòng ngay.",
        "Max Leverage là trần risk guard. Prompt yêu cầu leverage cao hơn sẽ bị chặn.",
        "Bật mô phỏng = chỉ dry-run. Tắt mô phỏng = gửi order thật lên Bybit nếu API có quyền Trade.",
    ]
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "notes": notes,
        "bybit_key": client.masked_key(),
        "bybit_signing": client.signing_label(),
        "openai_enabled": engine.enabled,
        "risk": guard.public_config(),
    }


def categories_for_guard(guard: RiskGuard) -> list[str]:
    if guard.config.default_category == "auto":
        return ["linear", "spot"]
    return [guard.config.default_category]


def snapshot_key(category: str, symbol: str) -> str:
    return f"{category}:{symbol.upper()}"


def _short_number(value: Any) -> str:
    if value in (None, ""):
        return "-"
    try:
        num = Decimal(str(value))
        if num == 0:
            return "0"
        if abs(num) >= Decimal("100"):
            return str(num.quantize(Decimal("0.01"))).rstrip("0").rstrip(".")
        if abs(num) >= Decimal("1"):
            return str(num.quantize(Decimal("0.0001"))).rstrip("0").rstrip(".")
        return str(num.quantize(Decimal("0.00000001"))).rstrip("0").rstrip(".")
    except Exception:
        return str(value)


def action_label(action: str) -> str:
    labels = {
        "SPOT_BUY": "Spot mua",
        "SPOT_SELL": "Spot bán",
        "SPOT_SELL_ALL": "Spot bán hết",
        "OPEN_LONG": "Futures Long",
        "OPEN_SHORT": "Futures Short",
        "CLOSE_LONG": "Đóng Long",
        "CLOSE_SHORT": "Đóng Short",
        "CLOSE_ALL": "Đóng tất cả",
        "WAIT": "Đứng ngoài",
    }
    return labels.get((action or "").upper(), action or "-")


def summarize_trade_result(result: Dict[str, Any]) -> str:
    n = result.get("normalized") or {}
    execution = result.get("execution") or {}
    action = str(n.get("action") or "").upper()
    if action == "WAIT":
        wait_reason = str(n.get("reason") or execution.get("reason") or "").strip()
        if wait_reason.lower() in {"", "no reason provided", "none", "null", "n/a", "na"}:
            wait_reason = "KHÔNG VÀO LỆNH: chưa có tín hiệu đủ rõ hoặc chưa đủ dữ liệu để tính TP/SL cụ thể."
        return "AI quyết định ĐỨNG NGOÀI · " + wait_reason[:220]
    symbol = n.get("symbol") or "-"
    category = str(n.get("category") or "-").upper()
    status = str(execution.get("status") or "-")
    mode = "Mô phỏng" if status == "dry_run" else ("Đã gửi Bybit" if status == "live_sent" else status)
    size = ""
    if action.startswith("SPOT"):
        if n.get("order_usdt"):
            size = f" · vốn {n.get('order_usdt')} USDT"
        elif n.get("qty"):
            size = f" · qty {n.get('qty')}"
        lev = " · spot không dùng đòn bẩy"
    else:
        if n.get("margin_usdt"):
            size = f" · margin {n.get('margin_usdt')} USDT"
        lev = f" · đòn bẩy {n.get('leverage', 1)}x" if n.get("leverage") else ""
    entry = result.get("market_price") or n.get("entry_price") or ""
    entry_txt = f" · giá tham chiếu {_short_number(entry)}" if entry else ""
    tp = f" · TP {_short_number(n.get('take_profit'))}" if n.get("take_profit") else ""
    sl = f" · SL {_short_number(n.get('stop_loss'))}" if n.get("stop_loss") else ""
    reason = str(n.get("reason") or "")[:180]
    reason_txt = f" · lý do: {reason}" if reason else ""
    return f"{action_label(action)} {symbol} ({category}) · {mode}{size}{lev}{entry_txt}{tp}{sl}{reason_txt}"


def summarize_skill_result(result: Dict[str, Any]) -> str:
    status_map = {"updated": "Đã cập nhật", "current": "Đang là bản mới nhất", "refreshed": "Đã làm mới", "error": "Có lỗi"}
    text = status_map.get(str(result.get("status") or ""), str(result.get("status") or "Không rõ"))
    if result.get("from") and result.get("to"):
        text += f" · {result.get('from')} → {result.get('to')}"
    files = result.get("files") or []
    if isinstance(files, list) and files:
        text += f" · {len(files)} file"
    if result.get("error"):
        text += f" · {result.get('error')}"
    return text


def summarize_preflight(report: Dict[str, Any]) -> str:
    parts = ["OK" if report.get("ok") else "Cần kiểm tra"]
    if report.get("errors"):
        parts.append("Lỗi: " + "; ".join(map(str, report.get("errors", [])))[:300])
    if report.get("warnings"):
        parts.append("Cảnh báo: " + "; ".join(map(str, report.get("warnings", [])))[:300])
    if not report.get("errors") and not report.get("warnings"):
        parts.append("Workspace sẵn sàng")
    return " · ".join(parts)




def is_balance_query(command: str) -> bool:
    text = (command or "").lower()
    keywords = [
        "kiểm tra số dư", "kiem tra so du", "xem số dư", "xem so du",
        "số dư", "so du", "balance", "wallet", "tài sản", "tai san",
        "equity", "available", "còn bao nhiêu", "con bao nhieu",
    ]
    trade_words = ["mua", "buy", "bán", "ban", "sell", "long", "short", "đóng", "dong", "close", "tp", "sl"]
    return any(k in text for k in keywords) and not any(w in text for w in trade_words)


def wallet_summary_text(wallet: Dict[str, Any]) -> str:
    try:
        account = (wallet.get("result", {}).get("list", []) or [{}])[0]
        equity = account.get("totalEquity") or "-"
        available = account.get("totalAvailableBalance") or account.get("totalWalletBalance") or "-"
        coins = []
        for coin in account.get("coin", []) or []:
            name = str(coin.get("coin") or "").upper()
            balance = coin.get("walletBalance") or coin.get("equity") or "0"
            usd = coin.get("usdValue") or ""
            try:
                if Decimal(str(balance)) <= 0 and Decimal(str(usd or "0")) <= 0:
                    continue
            except Exception:
                pass
            if usd not in ("", None):
                coins.append(f"{name} {balance} (~{usd} USDT)")
            else:
                coins.append(f"{name} {balance}")
        coin_text = "; ".join(coins[:8]) if coins else "Không thấy coin có số dư."
        return f"Tổng equity ~{equity} USDT; available ~{available} USDT; {coin_text}."
    except Exception:
        return "Đã lấy được dữ liệu ví nhưng không đọc được định dạng trả về từ Bybit."


def wallet_summary_payload(wallet: Dict[str, Any]) -> Dict[str, Any]:
    account = (wallet.get("result", {}).get("list", []) or [{}])[0]
    total_equity = str(account.get("totalEquity") or "0")
    available = str(account.get("totalAvailableBalance") or account.get("totalWalletBalance") or "0")
    coins = []
    for coin in account.get("coin", []) or []:
        name = str(coin.get("coin") or "").upper()
        balance = str(coin.get("walletBalance") or coin.get("equity") or "0")
        usd = str(coin.get("usdValue") or "0")
        try:
            if Decimal(balance) <= 0 and Decimal(usd) <= 0:
                continue
        except Exception:
            pass
        coins.append({"coin": name, "balance": balance, "usd_value": usd})
    def sort_key(item: Dict[str, Any]) -> Decimal:
        try:
            return Decimal(str(item.get("usd_value") or "0"))
        except Exception:
            return Decimal("0")
    coins = sorted(coins, key=sort_key, reverse=True)
    spotlight = {item["coin"]: item for item in coins[:8]}
    for want in ["USDT", "BTC", "ETH"]:
        if want not in spotlight:
            for item in coins:
                if item.get("coin") == want:
                    spotlight[want] = item
                    break
    return {
        "total_equity": total_equity,
        "available_balance": available,
        "coins": coins[:8],
        "spotlight": spotlight,
    }


def is_wait_action(action: dict) -> bool:
    return str((action or {}).get("action", "WAIT")).upper().strip() in {"WAIT", "HOLD", "NO_TRADE"}


def direct_wait_error(raw_decision: dict) -> str:
    reason = str((raw_decision or {}).get("reason") or "Lệnh trực tiếp chưa đủ thông tin để thực thi.").strip()
    return "Lệnh trực tiếp chưa thể thực thi: " + reason[:260]


def should_skip_bot_control_for_command(command: str) -> bool:
    """Direct execution must not be hijacked by bot-control parsing.

    Examples that must go to execution: "đóng hết lệnh future btc",
    "long BTC 10u x20", "mua spot BTC 20u".
    """
    return is_clear_trade_execution_command(command)


def _direct_command_has_explicit_leverage(command: str) -> bool:
    text = (command or "").lower()
    return bool(re.search(r"(đòn|don|đòn bẩy|don bay|leverage|lev)\s*[:=]?\s*\d+|\bx\s*\d+\b|\b\d+\s*x\b", text))


def apply_direct_futures_defaults(command: str, raw_decision: Dict[str, Any], guard: RiskGuard) -> Dict[str, Any]:
    decision = dict(raw_decision or {})
    action = str(decision.get("action") or "").upper().strip()
    if action not in {"OPEN_LONG", "OPEN_SHORT"}:
        return decision
    category = str(decision.get("category") or guard.config.default_category or "linear").lower().strip()
    if category in {"", "auto", "spot"}:
        category = "linear"
    if category not in {"linear", "inverse"}:
        return decision
    decision["category"] = category
    if not _direct_command_has_explicit_leverage(command):
        try:
            current_lev = int(decision.get("leverage") or 0)
        except Exception:
            current_lev = 0
        if current_lev <= 1 and guard.config.max_leverage > 1:
            decision["leverage"] = guard.config.max_leverage
            reason = str(decision.get("reason") or "").strip()
            add = f"Không thấy đòn bẩy trong lệnh trực tiếp; dùng max leverage cấu hình {guard.config.max_leverage}x."
            decision["reason"] = (reason + " " + add).strip() if reason else add
    return decision




def merge_direct_parser_with_ai(command: str, ai_decision: Dict[str, Any], guard: RiskGuard) -> Dict[str, Any]:
    """Preserve explicit user parameters even when the AI misses them.

    Direct Command is execution-oriented. The AI may correctly infer the action
    but miss Vietnamese shorthand such as "bitcoin", "bẩy x10", "10u".
    This deterministic parser does not replace AI reasoning; it only fills or
    overrides explicit fields found in the user command before Risk Guard runs.
    """
    decision = dict(ai_decision or {})
    parsed = parse_direct_command(command, allowed_symbols_from_guard(guard), guard.config.default_category)
    if is_wait_action(parsed):
        return apply_direct_futures_defaults(command, decision, guard)

    ai_action = str(decision.get("action") or "").upper().strip()
    parser_action = str(parsed.get("action") or "").upper().strip()
    if not ai_action or ai_action in {"WAIT", "HOLD", "NO_TRADE"}:
        return apply_direct_futures_defaults(command, parsed, guard)

    compatible = (
        ai_action == parser_action
        or {ai_action, parser_action} <= {"OPEN_LONG", "OPEN_SHORT"}
        or {ai_action, parser_action} <= {"SPOT_BUY", "SPOT_SELL", "SPOT_SELL_ALL"}
    )
    if not compatible:
        return apply_direct_futures_defaults(command, decision, guard)

    for key in [
        "symbol", "category", "leverage", "margin_usdt", "order_usdt", "qty",
        "take_profit", "stop_loss", "take_profit_pct", "stop_loss_pct",
    ]:
        val = parsed.get(key)
        if val not in (None, ""):
            decision[key] = val

    reason = str(decision.get("reason") or "").strip()
    decision["reason"] = (reason + " Đã đối chiếu parser nội bộ để giữ đúng thông tin user nhập.").strip()
    return apply_direct_futures_defaults(command, decision, guard)


def _prompt_schedule_key(prompt: str, meta: Dict[str, Any]) -> str:
    base = json.dumps({
        "prompt": prompt.strip(),
        "symbols": meta.get("symbols"),
        "market": meta.get("market"),
        "interval": meta.get("interval_seconds"),
        "spot_order_usdt": meta.get("spot_order_usdt"),
        "futures_margin_usdt": meta.get("futures_margin_usdt"),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def _scheduled_prompt_wait_seconds(settings: Dict[str, Any], prompt: str, meta: Dict[str, Any]) -> int:
    """Return remaining seconds before this scheduled prompt may execute again.

    This is separate from the AI decision. AI can understand the prompt, but
    timing/cadence must be enforced by deterministic bot state so a prompt like
    `mua bitcoin spot 10 usdt/1h` cannot buy every loop tick.
    """
    interval = int(meta.get("interval_seconds") or 0)
    if interval <= 0:
        return 0
    key = _prompt_schedule_key(prompt, meta)
    last_key = str(settings.get("last_prompt_trade_key") or "")
    try:
        last_ts = float(settings.get("last_prompt_trade_ts") or 0)
    except Exception:
        last_ts = 0.0
    if last_key != key or last_ts <= 0:
        return 0
    elapsed = time.time() - last_ts
    return max(0, int(interval - elapsed))


def _mark_scheduled_prompt_executed(user_id: int, prompt: str, meta: Dict[str, Any]) -> None:
    if int(meta.get("interval_seconds") or 0) <= 0:
        return
    store.update_settings(user_id, {
        "last_prompt_trade_key": _prompt_schedule_key(prompt, meta),
        "last_prompt_trade_ts": str(int(time.time())),
    })



def _apply_prompt_tp_sl_constraints(raw_decision: Dict[str, Any], prompt_meta: Dict[str, Any]) -> Dict[str, Any]:
    """Attach non-negotiable prompt constraints to the AI/fallback decision.

    If a strategy prompt uses ATR/R-multiple/structure exits, TP/SL must be
    explicit prices from AI. The risk guard will block the order instead of
    applying dashboard default TP/SL percentages.
    """
    decision = dict(raw_decision or {})
    action = str(decision.get("action") or "WAIT").upper().strip()
    if action in OPENING_ACTIONS and prompt_meta.get("requires_explicit_tp_sl"):
        decision["require_explicit_tp_sl"] = True
        decision["no_default_tp_sl"] = True
        if not decision.get("reason"):
            decision["reason"] = "Prompt yêu cầu TP/SL cụ thể theo ATR/RR/structure."
    return decision



def _is_exact_rsi_candle_prompt(meta: Dict[str, Any]) -> bool:
    return bool(meta.get("exact_rsi_candle_strategy") and str(meta.get("primary_timeframe") or "").lower() in {"5m", "m5"})


def _is_prompt_only_mode(meta: Dict[str, Any] | None) -> bool:
    return bool((meta or {}).get("prompt_only_mode") or (meta or {}).get("strict_prompt_only"))


def _requested_timeframes_from_meta(meta: Dict[str, Any] | None) -> list[str]:
    meta = meta or {}
    allowed = [str(x).lower() for x in (meta.get("allowed_timeframes") or []) if str(x).strip()]
    if allowed:
        return allowed
    primary = str(meta.get("primary_timeframe") or "").lower().strip()
    if primary:
        return [primary]
    tf = str(meta.get("timeframe") or "").upper().strip()
    mapping = {"5M":"5m", "M5":"5m", "15M":"15m", "M15":"15m", "30M":"30m", "1H":"1h", "H1":"1h", "4H":"4h", "H4":"4h", "1D":"1d", "D1":"1d"}
    return [mapping[x] for x in tf.split("/") if x in mapping]


def _allowed_indicator_names(meta: Dict[str, Any] | None) -> set[str]:
    meta = meta or {}
    vals = meta.get("allowed_indicators") or meta.get("indicators") or []
    return {str(v).upper().strip() for v in vals if str(v).strip()}


def _first_symbol_snapshot(ai_snapshot: Dict[str, Any], key: str = "linear:BTCUSDT") -> Dict[str, Any]:
    symbols = ai_snapshot.get("symbols") if isinstance(ai_snapshot, dict) else {}
    if isinstance(symbols, dict):
        snap = symbols.get(key)
        if isinstance(snap, dict):
            return snap
        if symbols:
            first = next(iter(symbols.values()))
            return first if isinstance(first, dict) else {}
    return {}


def _last_candle_colors(pack: Dict[str, Any], count: int = 2) -> list[str]:
    candles = pack.get("recent_candles") if isinstance(pack, dict) else []
    if not isinstance(candles, list):
        return []
    colors = []
    for c in candles[:count]:
        if isinstance(c, dict):
            colors.append(str(c.get("color") or "").lower())
    return colors


def _has_open_position_same_side(snap: Dict[str, Any], action: str) -> bool:
    positions = snap.get("positions") if isinstance(snap, dict) else []
    if not isinstance(positions, list):
        return False
    want = "Buy" if action == "OPEN_LONG" else "Sell" if action == "OPEN_SHORT" else ""
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        side = str(pos.get("side") or "").strip()
        try:
            size = Decimal(str(pos.get("size") or "0"))
        except Exception:
            size = Decimal("0")
        if size > 0 and side.lower() == want.lower():
            return True
    return False


def _evaluate_exact_rsi_candle_prompt(prompt_meta: Dict[str, Any], ai_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic evaluator for prompts that explicitly define RSI 5m + candle rules.

    This mode intentionally ignores unmentioned H1/H4/D1/EMA/MACD rules. It prevents
    the LLM from self-authoring a different strategy such as multi-timeframe EMA short.
    """
    snap = _first_symbol_snapshot(ai_snapshot, "linear:BTCUSDT")
    symbol = snap.get("symbol") or "BTCUSDT"
    tfs = snap.get("timeframes") if isinstance(snap.get("timeframes"), dict) else {}
    tf5 = tfs.get("5m") if isinstance(tfs, dict) else {}
    if not isinstance(tf5, dict) or tf5.get("error"):
        return {
            "action": "WAIT", "category": "linear", "symbol": symbol, "confidence": 0,
            "reason": f"KHÔNG VÀO LỆNH: 1) thiếu dữ liệu 5m hợp lệ nên không kiểm tra được RSI 5m; 2) chiến lược này chỉ cho phép RSI 5m + 2 nến xác nhận, không dùng H1/H4/D1 để tự mở lệnh.",
            "_deterministic_exact_prompt": True,
        }
    ind = tf5.get("indicators") if isinstance(tf5.get("indicators"), dict) else {}
    rsi5 = None
    try:
        rsi5 = float(ind.get("rsi14")) if ind.get("rsi14") is not None else None
    except Exception:
        rsi5 = None
    colors = _last_candle_colors(tf5, int(prompt_meta.get("candle_confirm_count") or 2))
    if rsi5 is None:
        return {
            "action": "WAIT", "category": "linear", "symbol": symbol, "confidence": 0,
            "reason": "KHÔNG VÀO LỆNH: 1) snapshot có 5m nhưng thiếu RSI14 5m; 2) không được dùng chỉ báo khác thay thế vì prompt chỉ định RSI 5m.",
            "_deterministic_exact_prompt": True,
        }

    long_threshold = prompt_meta.get("rsi_long_below")
    short_threshold = prompt_meta.get("rsi_short_above")
    n = int(prompt_meta.get("candle_confirm_count") or 2)
    long_ok = long_threshold is not None and rsi5 < float(long_threshold) and len(colors) >= n and all(c == "green" for c in colors[:n])
    short_ok = short_threshold is not None and rsi5 > float(short_threshold) and len(colors) >= n and all(c == "red" for c in colors[:n])
    if long_ok and _has_open_position_same_side(snap, "OPEN_LONG"):
        return {
            "action": "WAIT", "category": "linear", "symbol": symbol, "confidence": 0,
            "reason": f"KHÔNG VÀO LỆNH: 1) RSI 5m={rsi5:.2f} và {n} nến xanh đạt điều kiện Long; 2) đã có vị thế Long/Buy cùng chiều nên không mở trùng lệnh.",
            "_deterministic_exact_prompt": True,
        }
    if short_ok and _has_open_position_same_side(snap, "OPEN_SHORT"):
        return {
            "action": "WAIT", "category": "linear", "symbol": symbol, "confidence": 0,
            "reason": f"KHÔNG VÀO LỆNH: 1) RSI 5m={rsi5:.2f} và {n} nến đỏ đạt điều kiện Short; 2) đã có vị thế Short/Sell cùng chiều nên không mở trùng lệnh.",
            "_deterministic_exact_prompt": True,
        }

    base = {
        "category": "linear",
        "symbol": symbol,
        "leverage": int(prompt_meta.get("leverage") or 20),
        "entry_type": "market",
        "margin_usdt": prompt_meta.get("futures_margin_usdt") or 10,
        "risk_usdt": 1,
        "take_profit_pct": prompt_meta.get("take_profit_pct"),
        "stop_loss_pct": prompt_meta.get("stop_loss_pct"),
        "_deterministic_exact_prompt": True,
    }
    if long_ok:
        return {
            **base,
            "action": "OPEN_LONG",
            "confidence": 70,
            "reason": f"Đúng rule gốc: RSI 5m={rsi5:.2f} < {float(long_threshold):g} và {n} nến gần nhất đều xanh ({colors[:n]}). Dùng đúng prompt RSI 5m, không dùng H1/H4/D1.",
        }
    if short_ok:
        return {
            **base,
            "action": "OPEN_SHORT",
            "confidence": 70,
            "reason": f"Đúng rule gốc: RSI 5m={rsi5:.2f} > {float(short_threshold):g} và {n} nến gần nhất đều đỏ ({colors[:n]}). Dùng đúng prompt RSI 5m, không dùng H1/H4/D1.",
        }

    needed_long = f"RSI 5m < {long_threshold:g} + {n} nến xanh" if long_threshold is not None else "không có rule Long"
    needed_short = f"RSI 5m > {short_threshold:g} + {n} nến đỏ" if short_threshold is not None else "không có rule Short"
    return {
        "action": "WAIT", "category": "linear", "symbol": symbol, "confidence": 0,
        "reason": f"KHÔNG VÀO LỆNH: 1) rule Long chưa đạt ({needed_long}); 2) rule Short chưa đạt ({needed_short}). Hiện RSI 5m={rsi5:.2f}, {n} nến gần nhất={colors[:n]}. Bot không dùng EMA/MACD/H1/H4/D1 vì prompt gốc chỉ yêu cầu RSI 5m + nến 5m.",
        "_deterministic_exact_prompt": True,
    }

def _is_simple_dca_or_direct_prompt(prompt: str, meta: Dict[str, Any]) -> bool:
    """Return True only for deterministic execution prompts safe to run without AI.

    Complex strategy prompts that mention indicators, ATR, R/R, structure,
    or require explicit TP/SL must NOT be converted from WAIT into an order.
    """
    lower = (prompt or "").lower()
    if meta.get("requires_explicit_tp_sl"):
        return False
    if meta.get("rsi_rules") or meta.get("indicators"):
        # Indicator strategies must be evaluated by AI/strategy engine, not fallback.
        return False
    complex_terms = [
        "atr", "1.5r", "2r", "r:r", "risk/reward", "risk reward",
        "ema", "macd", "rsi", "volume", "bollinger", "structure",
        "cấu trúc", "cau truc", "hỗ trợ", "ho tro", "kháng cự", "khang cu",
        "pullback", "xu hướng", "xu huong", "trend", "setup",
        "điều kiện", "dieu kien", "chỉ báo", "chi bao",
    ]
    if any(term in lower for term in complex_terms):
        return False
    # Only very short prompts should be deterministic fallback: e.g.
    # "mua btc spot 10u/1h" or "long btc x10 vốn 8u".
    if len(prompt or "") > 260:
        return False
    return True


def _action_from_prompt_directives(prompt: str, meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Deterministic fallback for simple recurring/DCA/execution prompts only.

    The fallback must never turn an AI WAIT into a trade for complex strategy
    prompts such as ATR/RR/EMA/RSI systems. For those, WAIT means WAIT.
    """
    if not _is_simple_dca_or_direct_prompt(prompt, meta):
        return None

    lower = (prompt or "").lower()
    is_buy = any(k in lower for k in ["mua", "buy", "gom", "dca", "tích lũy", "tich luy"])
    is_short = any(k in lower for k in ["short", "sell short"])
    symbols = meta.get("symbols") or []
    symbol = symbols[0] if symbols else ""
    if not symbol:
        return None
    market = meta.get("market") or ""
    if market == "spot" and is_buy and meta.get("spot_order_usdt"):
        out: Dict[str, Any] = {
            "action": "SPOT_BUY",
            "symbol": symbol,
            "category": "spot",
            "order_usdt": str(meta.get("spot_order_usdt")),
            "reason": "Prompt DCA/recurring spot buy parsed deterministically; cadence is enforced by scheduler.",
        }
        if meta.get("take_profit_pct"):
            out["take_profit_pct"] = meta.get("take_profit_pct")
        if meta.get("stop_loss_pct"):
            out["stop_loss_pct"] = meta.get("stop_loss_pct")
        return out
    if market == "linear" and meta.get("futures_margin_usdt") and meta.get("leverage"):
        if is_short:
            act = "OPEN_SHORT"
        elif is_buy or "long" in lower:
            act = "OPEN_LONG"
        else:
            return None
        out = {
            "action": act,
            "symbol": symbol,
            "category": "linear",
            "margin_usdt": str(meta.get("futures_margin_usdt")),
            "leverage": int(meta.get("leverage") or 1),
            "reason": "Simple recurring futures instruction parsed deterministically; cadence is enforced by scheduler.",
        }
        if meta.get("take_profit_pct"):
            out["take_profit_pct"] = meta.get("take_profit_pct")
        if meta.get("stop_loss_pct"):
            out["stop_loss_pct"] = meta.get("stop_loss_pct")
        return out
    return None


async def record_trade_tracking(user_id: int, runtime: UserRuntimeState, result: Dict[str, Any]) -> None:
    normalized = result.get("normalized") or {}
    action = str(normalized.get("action") or "").upper()
    if action in OPENING_ACTIONS:
        execution = result.get("execution") or {}
        trade = {
            "source": execution.get("status") or "dry_run",
            "action": action,
            "symbol": normalized.get("symbol"),
            "category": normalized.get("category"),
            "side": side_from_action(action),
            "entry_price": result.get("market_price") or normalized.get("entry_price") or "",
            "current_price": result.get("market_price") or "",
            "qty": normalized.get("qty") or normalized.get("approx_base_qty") or "",
            "order_usdt": normalized.get("order_usdt") or "",
            "margin_usdt": normalized.get("margin_usdt") or "",
            "leverage": "1" if str(normalized.get("category") or "").lower() == "spot" else str(normalized.get("leverage") or "1"),
            "take_profit": normalized.get("take_profit") or "",
            "stop_loss": normalized.get("stop_loss") or "",
            "reason": normalized.get("reason") or "",
            "normalized": normalized,
            "execution": execution,
        }
        trade_id = store.add_tracked_trade(user_id, trade)
        await runtime.log("INFO", f"Đã thêm lệnh #{trade_id} vào bảng theo dõi: {action_label(action)} {trade['symbol']}.")
    elif action in CLOSING_ACTIONS:
        closed = store.close_tracked_trades_for_action(user_id, normalized, current_price=str(result.get("market_price") or ""))
        if closed:
            await runtime.log("INFO", f"Đã đánh dấu đóng {closed} lệnh đang theo dõi cho {normalized.get('symbol')}.")



def _structure_from_klines(raw_klines: list[Any], *, lookback: int = 80) -> Dict[str, Any]:
    """Return simple recent structure levels from Bybit kline rows.

    Bybit returns newest-first rows. We only need conservative levels for the AI
    snapshot: recent swing high/low plus rough support/resistance. These are not
    execution guarantees; Risk Guard still validates orders.
    """
    rows = []
    for item in raw_klines or []:
        try:
            rows.append({
                "ts": int(item[0]),
                "high": Decimal(str(item[2])),
                "low": Decimal(str(item[3])),
                "close": Decimal(str(item[4])),
            })
        except Exception:
            continue
    rows.sort(key=lambda x: x["ts"])
    if not rows:
        return {"status": "no_data"}
    recent = rows[-lookback:] if len(rows) > lookback else rows
    highs = [r["high"] for r in recent]
    lows = [r["low"] for r in recent]
    closes = [r["close"] for r in recent]
    last_close = closes[-1]
    support = min(lows)
    resistance = max(highs)
    return {
        "status": "ok",
        "lookback": len(recent),
        "last_close": float(last_close),
        "recent_support": float(support),
        "recent_resistance": float(resistance),
        "swing_low": float(support),
        "swing_high": float(resistance),
        "distance_to_support_pct": float(((last_close - support) / last_close * Decimal("100"))) if last_close else None,
        "distance_to_resistance_pct": float(((resistance - last_close) / last_close * Decimal("100"))) if last_close else None,
    }


async def _timeframe_pack(client: BybitClient, symbol: str, category: str, interval: str, limit: int = 220) -> Dict[str, Any]:
    kline = await client.get_klines(symbol, category, interval=interval, limit=limit)
    raw_klines = kline.get("result", {}).get("list", [])
    return {
        "klines": compact_kline_summary(raw_klines),
        "indicators": calculate_indicators(raw_klines),
        "structure": _structure_from_klines(raw_klines),
    }


async def build_market_snapshot(client: BybitClient, symbol: str, category: str, prompt_meta: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ticker = await client.get_ticker(symbol, category)

    # V47: fetch only the prompt-requested timeframe in prompt-only mode.
    # This prevents an RSI 5m prompt from receiving D1/H4/H1/EMA/MACD context
    # that could authorize a different strategy.
    all_timeframe_specs = {
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "4h": "240",
        "1d": "D",
    }
    if _is_prompt_only_mode(prompt_meta):
        wanted = _requested_timeframes_from_meta(prompt_meta) or ["15m"]
        timeframe_specs = {k: v for k, v in all_timeframe_specs.items() if k in wanted}
    else:
        timeframe_specs = {k: all_timeframe_specs[k] for k in ["5m", "15m", "1h", "4h", "1d"]}
    timeframes: Dict[str, Any] = {}
    for label, interval in timeframe_specs.items():
        try:
            timeframes[label] = await _timeframe_pack(client, symbol, category, interval)
        except Exception as exc:
            timeframes[label] = {"error": f"Không lấy được dữ liệu {label}: {exc}"}

    wallet = await client.get_wallet_balance()
    positions = {"result": {"list": []}}
    if category in {"linear", "inverse"}:
        positions = await client.get_positions(symbol, category)

    # Keep legacy fields for older UI / Risk Guard code while adding new multi-TF fields.
    indicators_5m = (timeframes.get("5m") or {}).get("indicators", {})
    klines_5m = (timeframes.get("5m") or {}).get("klines", {})
    indicators_15m = (timeframes.get("15m") or {}).get("indicators", {})
    klines_15m = (timeframes.get("15m") or {}).get("klines", {})
    return {
        "symbol": symbol,
        "category": category,
        "ticker": ticker,
        "timeframes": timeframes,
        "klines_5m": klines_5m,
        "indicators_5m": indicators_5m,
        "klines_15m": klines_15m,
        "indicators_15m": indicators_15m,
        "indicators_1h": (timeframes.get("1h") or {}).get("indicators", {}),
        "indicators_4h": (timeframes.get("4h") or {}).get("indicators", {}),
        "indicators_1d": (timeframes.get("1d") or {}).get("indicators", {}),
        "structure_1h": (timeframes.get("1h") or {}).get("structure", {}),
        "structure_4h": (timeframes.get("4h") or {}).get("structure", {}),
        "structure_1d": (timeframes.get("1d") or {}).get("structure", {}),
        "positions": positions.get("result", {}).get("list", []),
        "wallet": wallet.get("result", {}),
    }


async def build_snapshots_for_guard(client: BybitClient, guard: RiskGuard, prompt_meta: Dict[str, Any] | None = None) -> Dict[str, Any]:
    snapshots: Dict[str, Any] = {}
    for category in categories_for_guard(guard):
        for symbol in allowed_symbols_from_guard(guard):
            try:
                snapshots[snapshot_key(category, symbol)] = await build_market_snapshot(client, symbol, category)
            except Exception as exc:
                # Do not kill the entire loop if one market type is unsupported for a symbol.
                snapshots[snapshot_key(category, symbol)] = {
                    "symbol": symbol,
                    "category": category,
                    "error": f"Không lấy được snapshot: {exc}",
                }
    return snapshots




def _short_json(obj: Any, limit: int = 1400) -> str:
    """Compact JSON for live logs without crashing on Decimal/unknown objects."""
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:
        text = str(obj)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _debug_signal_view(decision: Dict[str, Any]) -> Dict[str, Any]:
    """Small parsed-signal view for AI debug logs."""
    if not isinstance(decision, dict):
        return {"raw_type": type(decision).__name__}
    keys = [
        "action", "category", "symbol", "leverage", "margin_usdt", "risk_usdt",
        "take_profit", "stop_loss", "take_profit_pct", "stop_loss_pct", "confidence", "reason",
    ]
    return {k: decision.get(k) for k in keys if k in decision}




def _flag_enabled(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "debug"}


def _ai_debug_enabled(settings: Dict[str, Any] | None = None) -> bool:
    """Raw AI debug logs are useful during debugging but too noisy for daily use."""
    if settings and "ai_debug_logs" in settings:
        return _flag_enabled(settings.get("ai_debug_logs"), False)
    return _flag_enabled(os.getenv("AI_DEBUG_LOGS"), False)


def _fmt_num(value: Any, digits: int = 2) -> str:
    if value in (None, ""):
        return "-"
    try:
        q = Decimal("1") if digits <= 0 else Decimal("1").scaleb(-digits)
        return str(Decimal(str(value)).quantize(q)).rstrip("0").rstrip(".")
    except Exception:
        return str(value)


def _tf_brief(pack: Dict[str, Any] | None) -> str:
    pack = pack or {}
    ind = pack.get("indicators") or {}
    struct = pack.get("structure") or {}
    if ind.get("status") and ind.get("status") != "ok":
        return f"lỗi {ind.get('status')}"
    fields = []
    if ind.get("rsi14") is not None:
        fields.append(f"RSI {_fmt_num(ind.get('rsi14'), 1)}")
    if ind.get("trend") is not None:
        fields.append(f"trend {ind.get('trend')}")
    if isinstance(ind.get("macd"), dict):
        fields.append(f"MACD {((ind.get('macd') or {}).get('bias') or '-')}")
    ema_parts = []
    if ind.get("ema20") is not None:
        ema_parts.append("20=" + _fmt_num(ind.get("ema20"), 0))
    if ind.get("ema50") is not None:
        ema_parts.append("50=" + _fmt_num(ind.get("ema50"), 0))
    if ind.get("ema200") is not None:
        ema_parts.append("200=" + _fmt_num(ind.get("ema200"), 0))
    if ema_parts:
        fields.append("EMA " + "/".join(ema_parts))
    if ind.get("atr14") is not None:
        fields.append(f"ATR {_fmt_num(ind.get('atr14'), 1)}")
    if ind.get("volume_status") is not None:
        fields.append(f"Volume {ind.get('volume_status')}")
    candles = pack.get("recent_candles") or []
    if isinstance(candles, list) and candles:
        colors = [str(c.get("color") or "?") for c in candles[:3] if isinstance(c, dict)]
        if colors:
            fields.append("nến " + ",".join(colors))
    if struct:
        sr_parts = []
        if struct.get("recent_support") is not None:
            sr_parts.append("S=" + _fmt_num(struct.get("recent_support"),0))
        if struct.get("recent_resistance") is not None:
            sr_parts.append("R=" + _fmt_num(struct.get("recent_resistance"),0))
        if sr_parts:
            fields.append("/".join(sr_parts))
    return " · ".join(fields) if fields else "không có chỉ báo được phép theo prompt"

def _market_snapshot_brief(ai_snapshot: Dict[str, Any]) -> str:
    symbols = ai_snapshot.get("symbols") if isinstance(ai_snapshot, dict) else None
    if not isinstance(symbols, dict) or not symbols:
        return "Dữ liệu thị trường: trống hoặc không hợp lệ."
    preferred_key = None
    for key in symbols:
        if str(key).lower() == "linear:btcusdt":
            preferred_key = key
            break
    preferred_key = preferred_key or next(iter(symbols.keys()))
    snap = symbols.get(preferred_key) or {}
    sym = snap.get("symbol") or preferred_key
    cat = str(snap.get("category") or "-").upper()
    price = _fmt_num(snap.get("price"), 2)
    tfs = snap.get("timeframes") or {}
    parts = [f"Market {cat} {sym} giá {price}"]
    # Show the low timeframe first when it exists so custom 5m scalping prompts
    # are visible in Live Log instead of being buried under D1/H4/H1.
    order = ("5m", "15m", "1h", "4h", "1d") if "5m" in tfs else ("1d", "4h", "1h", "15m")
    for label in order:
        if label in tfs:
            parts.append(f"{label.upper()}: {_tf_brief(tfs.get(label))}")
    return " · ".join(parts)


def _raw_ai_brief(decision: Dict[str, Any]) -> str:
    if not isinstance(decision, dict):
        return f"AI trả dữ liệu không hợp lệ: {type(decision).__name__}"
    action = str(decision.get("action") or "WAIT").upper()
    symbol = decision.get("symbol") or "BTCUSDT"
    reason = str(decision.get("reason") or "").strip()
    if action in {"WAIT", "HOLD", "NO_TRADE"}:
        return f"AI: WAIT {symbol} · {reason[:260] if reason else 'chưa có setup đủ rõ.'}"
    pieces = [f"AI: {action} {symbol}"]
    if decision.get("leverage"):
        pieces.append(f"lev {decision.get('leverage')}x")
    if decision.get("margin_usdt"):
        pieces.append(f"margin {decision.get('margin_usdt')} USDT")
    if decision.get("risk_usdt"):
        pieces.append(f"risk {decision.get('risk_usdt')} USDT")
    if decision.get("stop_loss"):
        pieces.append(f"SL {_short_number(decision.get('stop_loss'))}")
    if decision.get("take_profit"):
        pieces.append(f"TP {_short_number(decision.get('take_profit'))}")
    if decision.get("confidence") is not None:
        pieces.append(f"conf {decision.get('confidence')}")
    if reason:
        pieces.append("lý do: " + reason[:220])
    return " · ".join(pieces)


async def _log_ai_result(runtime: UserRuntimeState, settings: Dict[str, Any], ai_snapshot: Dict[str, Any], raw_decision: Dict[str, Any]) -> None:
    """Clean live log by default; raw payloads only when AI_DEBUG_LOGS=true."""
    await runtime.log("INFO", _market_snapshot_brief(ai_snapshot))
    await runtime.log("INFO", _raw_ai_brief(raw_decision))
    if _ai_debug_enabled(settings):
        await runtime.log("DEBUG", "AI DEBUG · MARKET SNAPSHOT SENT TO AI: " + _short_json(ai_snapshot, 2200))
        debug_payload = raw_decision.get("_debug") if isinstance(raw_decision, dict) else None
        if isinstance(debug_payload, dict):
            await runtime.log("DEBUG", "AI DEBUG · RAW AI RESPONSE: " + str(debug_payload.get("raw_ai_response") or "")[:2200])
        else:
            await runtime.log("DEBUG", "AI DEBUG · RAW AI RESPONSE: <không có raw response>")
        await runtime.log("DEBUG", "AI DEBUG · PARSED SIGNAL: " + _short_json(_debug_signal_view(raw_decision), 1600))

def _filter_indicators_for_prompt(indicators: Dict[str, Any], meta: Dict[str, Any] | None) -> Dict[str, Any]:
    indicators = indicators or {}
    meta = meta or {}
    if not _is_prompt_only_mode(meta):
        return indicators
    allowed = _allowed_indicator_names(meta)
    # Always keep status/last_close so the decision can reference current price/validity.
    out: Dict[str, Any] = {}
    for k in ["status", "last_close"]:
        if k in indicators:
            out[k] = indicators.get(k)
    if "RSI" in allowed and "rsi14" in indicators:
        out["rsi14"] = indicators.get("rsi14")
    if "EMA" in allowed:
        for k in ["trend", "ema20", "ema50", "ema200"]:
            if k in indicators:
                out[k] = indicators.get(k)
    if "MACD" in allowed and "macd" in indicators:
        out["macd"] = indicators.get("macd")
    if "ATR" in allowed and "atr14" in indicators:
        out["atr14"] = indicators.get("atr14")
    if "VOLUME" in allowed or "VOL" in allowed:
        for k in ["volume_ma20", "volume_status"]:
            if k in indicators:
                out[k] = indicators.get(k)
    if "SMA" in allowed:
        # No SMA fields currently computed except MA-style volume; keep none by default.
        pass
    return out


def _allow_structure_for_prompt(meta: Dict[str, Any] | None) -> bool:
    if not _is_prompt_only_mode(meta):
        return True
    meta = meta or {}
    text_bits = " ".join(str(x).lower() for x in [meta.get("tp_sl_mode"), *(meta.get("rsi_rules") or []), *(meta.get("indicators") or [])])
    return any(k in text_bits for k in ["support", "resistance", "swing", "structure", "ho tro", "khang cu", "cau truc"])


def _compact_snapshot_for_ai(snapshots: Dict[str, Any], prompt_meta: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Trim Bybit snapshots before sending them to the LLM.

    V47 invariant: in Prompt-Only Mode, send ONLY the timeframe/indicator the
    saved prompt explicitly requested. Do not send D1/H4/H1 or EMA/MACD if the
    prompt only asked for RSI 5m.
    """
    compact: Dict[str, Any] = {}
    meta = prompt_meta or {}
    wanted_symbols = set(meta.get("symbols") or [])
    wanted_market = str(meta.get("market") or "").lower().strip()
    prompt_only = _is_prompt_only_mode(meta)
    requested_tfs = _requested_timeframes_from_meta(meta)
    for key, snap in (snapshots or {}).items():
        if wanted_symbols or wanted_market:
            try:
                cat, sym = key.split(":", 1)
            except ValueError:
                cat, sym = "", ""
            if wanted_symbols and sym not in wanted_symbols:
                continue
            if wanted_market and wanted_market != cat:
                continue
        if not isinstance(snap, dict):
            continue
        if snap.get("error"):
            compact[key] = {"error": snap.get("error")}
            continue
        ticker = snap.get("ticker") or {}
        positions = snap.get("positions") or []
        compact_positions = []
        for pos in positions[:2] if isinstance(positions, list) else []:
            if isinstance(pos, dict):
                compact_positions.append({
                    "side": pos.get("side"),
                    "size": pos.get("size"),
                    "avgPrice": pos.get("avgPrice"),
                    "unrealisedPnl": pos.get("unrealisedPnl"),
                })

        def tf(label: str) -> Dict[str, Any]:
            pack = ((snap.get("timeframes") or {}).get(label) or {}) if isinstance(snap.get("timeframes"), dict) else {}
            if pack.get("error"):
                return {"error": pack.get("error")}
            kl = pack.get("klines") or {}
            out: Dict[str, Any] = {
                "indicators": _filter_indicators_for_prompt(pack.get("indicators") or {}, meta),
                "recent_candles": kl.get("recent_candles") or [],
            }
            if _allow_structure_for_prompt(meta):
                out["structure"] = pack.get("structure") or {}
            return out

        if prompt_only:
            labels = requested_tfs or ([str(meta.get("primary_timeframe") or "").lower()] if meta.get("primary_timeframe") else ["15m"])
            tf_map = {label: tf(label) for label in labels if label}
        else:
            tf_map = {
                "5m": tf("5m"),
                "15m": tf("15m"),
                "1h": tf("1h"),
                "4h": tf("4h"),
                "1d": tf("1d"),
            }

        row = {
            "symbol": snap.get("symbol"),
            "category": snap.get("category"),
            "price": ticker.get("lastPrice") or ticker.get("markPrice"),
            "bid1Price": ticker.get("bid1Price"),
            "ask1Price": ticker.get("ask1Price"),
            "timeframes": tf_map,
            "positions": compact_positions,
        }
        if not prompt_only:
            row.update({
                "price24hPcnt": ticker.get("price24hPcnt"),
                "volume24h": ticker.get("volume24h"),
                "indicators_5m": snap.get("indicators_5m") or {},
                "indicators_15m": snap.get("indicators_15m") or {},
            })
        else:
            row["prompt_only_mode"] = True
            row["allowed_timeframes"] = requested_tfs
            row["allowed_indicators"] = list(_allowed_indicator_names(meta))
        compact[key] = row
    return compact

async def get_instrument_filters(client: BybitClient, symbol: str, category: str) -> tuple[Decimal, Decimal, Decimal | None, Decimal]:
    instrument = await client.get_instrument(symbol, category)
    lot = instrument.get("lotSizeFilter", {}) or {}
    price_filter = instrument.get("priceFilter", {}) or {}

    # Futures instruments usually expose qtyStep. Spot instruments commonly expose
    # Spot uses basePrecision/minOrderQty; futures uses qtyStep/minOrderQty.
    if str(category).lower() == "spot":
        qty_step_raw = (
            lot.get("qtyStep")
            or lot.get("basePrecision")
            or lot.get("basePrecision")
            or "0.00000001"
        )
        min_qty_raw = lot.get("minOrderQty") or qty_step_raw
    else:
        qty_step_raw = lot.get("qtyStep") or "0.001"
        min_qty_raw = lot.get("minOrderQty") or "0.001"

    qty_step = Decimal(str(qty_step_raw))
    min_qty = Decimal(str(min_qty_raw))
    raw_min_amt = lot.get("minOrderAmt") or lot.get("minNotionalValue")
    min_order_amt = Decimal(str(raw_min_amt)) if raw_min_amt not in (None, "") else None
    tick_size = Decimal(str(price_filter.get("tickSize") or "0.01"))
    return qty_step, min_qty, min_order_amt, tick_size


async def enforce_tracked_tp_sl(user_id: int, runtime: UserRuntimeState, settings: Dict[str, Any]) -> int:
    """Actively enforce TP/SL for tracked live trades.

    The dashboard tracker can detect tp_hit/sl_hit from price, but detection alone
    does not close an exchange position. This function turns a tracker hit into an
    actual close_position request when live trading is enabled; in dry-run it only
    marks the tracked row closed. It is intentionally conservative: one close
    request closes all open tracked rows for the same symbol/side because Bybit
    futures positions are normally netted by symbol and side.
    """
    client = make_client(settings)
    guard = make_risk_guard(settings, runtime)
    if not client.is_configured:
        return 0
    rows = store.list_tracked_trades(user_id, limit=100)
    closed_total = 0
    handled: set[tuple[str, str, str]] = set()
    for row in rows:
        if str(row.get("status") or "").lower() != "open":
            continue
        category = str(row.get("category") or "").lower()
        if category not in {"linear", "inverse"}:
            # Spot TP/SL is tracked visually only unless a separate spot OCO flow is added.
            continue
        symbol = str(row.get("symbol") or "").upper().strip()
        side = str(row.get("side") or "").lower().strip()
        if not symbol or side not in {"long", "short"}:
            continue
        key = (category, symbol, side)
        if key in handled:
            continue
        current_price = ""
        try:
            ticker = await client.get_ticker(symbol, category)
            current_price = str(ticker.get("lastPrice") or ticker.get("markPrice") or "")
        except Exception as exc:
            await runtime.log("WARN", f"Không kiểm tra được TP/SL cho {symbol}: {exc}")
            continue
        snap = pnl_snapshot(row, current_price)
        state = str(snap.get("tp_sl_state") or "tracking")
        if state not in {"tp_hit", "sl_hit"}:
            continue
        handled.add(key)
        close_action = "CLOSE_LONG" if side == "long" else "CLOSE_SHORT"
        reason = "TP" if state == "tp_hit" else "SL"
        if guard.config.dry_run:
            closed = store.close_tracked_trades_for_action(
                user_id,
                {"action": close_action, "symbol": symbol, "category": category},
                current_price=current_price,
            )
            closed_total += closed
            await runtime.log("INFO", f"{reason} đã chạm cho {symbol} {side}; DRY_RUN nên chỉ đóng theo dõi {closed} lệnh.")
            continue
        try:
            await client.close_position(symbol=symbol, target=side, category=category)
            closed = store.close_tracked_trades_for_action(
                user_id,
                {"action": close_action, "symbol": symbol, "category": category},
                current_price=current_price,
            )
            closed_total += closed
            await runtime.log("WARN", f"{reason} đã chạm cho {symbol} {side}; bot đã gửi lệnh đóng vị thế live và đóng theo dõi {closed} lệnh.")
        except BybitAPIError as exc:
            # If Bybit says there is no position, the exchange TP/SL may have closed it already.
            msg = str(exc)
            if "Không có position" in msg or "position" in msg.lower() and "no" in msg.lower():
                closed = store.close_tracked_trades_for_action(
                    user_id,
                    {"action": close_action, "symbol": symbol, "category": category},
                    current_price=current_price,
                )
                closed_total += closed
                await runtime.log("WARN", f"{reason} đã chạm cho {symbol} {side}; Bybit không còn position, đã đóng theo dõi {closed} lệnh.")
            else:
                await runtime.log("ERROR", f"{reason} đã chạm nhưng đóng vị thế thất bại: {exc}")
    return closed_total


async def wait_with_tp_sl_monitor(
    user_id: int,
    runtime: UserRuntimeState,
    settings: Dict[str, Any],
    stop_event: asyncio.Event,
    total_seconds: int,
    check_seconds: int = 10,
) -> None:
    """Sleep in short chunks while enforcing tracked TP/SL.

    Strategy loops may run every 30 minutes, but TP/SL must be checked much more
    often. This keeps the strategy schedule intact while still closing live
    Futures positions when the tracker detects TP/SL hits.
    """
    remaining = max(0, int(total_seconds or 0))
    check_seconds = max(3, int(check_seconds or 10))
    while remaining > 0 and not stop_event.is_set():
        try:
            await enforce_tracked_tp_sl(user_id, runtime, settings)
        except Exception as exc:
            await runtime.log("WARN", f"TP/SL monitor lỗi nhưng bot vẫn chạy: {type(exc).__name__}: {exc}")
        chunk = min(check_seconds, remaining)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=chunk)
            return
        except asyncio.TimeoutError:
            remaining -= chunk
            continue


async def execute_action(client: BybitClient, guard: RiskGuard, normalized: Dict[str, Any], *, qty_step: Decimal | None = None, min_qty: Decimal | None = None) -> Dict[str, Any]:
    action = normalized["action"]
    if action == "WAIT":
        return {"status": "wait", "reason": normalized.get("reason")}

    if guard.config.dry_run:
        return {"status": "dry_run", "would_execute": normalized}

    # Live execution is intentionally direct and minimal: market orders only.
    # API keys should be sub-account keys with Read + Trade only, no Withdraw permission.
    if action == "OPEN_LONG":
        data = await client.open_position(
            symbol=normalized["symbol"], side="long", qty=normalized["qty"],
            leverage=int(normalized["leverage"]), category=normalized["category"],
            take_profit=normalized.get("take_profit"), stop_loss=normalized.get("stop_loss"),
        )
        return {"status": "live_sent", "bybit": data}
    if action == "OPEN_SHORT":
        data = await client.open_position(
            symbol=normalized["symbol"], side="short", qty=normalized["qty"],
            leverage=int(normalized["leverage"]), category=normalized["category"],
            take_profit=normalized.get("take_profit"), stop_loss=normalized.get("stop_loss"),
        )
        return {"status": "live_sent", "bybit": data}
    if action in {"CLOSE_LONG", "CLOSE_SHORT", "CLOSE_ALL"}:
        target = "all" if action == "CLOSE_ALL" else ("long" if action == "CLOSE_LONG" else "short")
        data = await client.close_position(symbol=normalized["symbol"], target=target, category=normalized["category"])
        return {"status": "live_sent", "bybit": data}
    if action == "SPOT_BUY":
        data = await client.spot_market_buy(
            symbol=normalized["symbol"], quote_usdt=normalized["order_usdt"],
            take_profit=normalized.get("take_profit"), stop_loss=normalized.get("stop_loss"),
        )
        return {"status": "live_sent", "bybit": data}
    if action == "SPOT_SELL":
        data = await client.spot_market_sell(symbol=normalized["symbol"], qty=normalized["qty"])
        return {"status": "live_sent", "bybit": data}
    if action == "SPOT_SELL_ALL":
        if qty_step is None or min_qty is None:
            raise RiskError("Thiếu qty_step/min_qty để bán hết Spot.")
        data = await client.spot_market_sell_all(symbol=normalized["symbol"], qty_step=qty_step, min_qty=min_qty)
        return {"status": "live_sent", "bybit": data}
    raise RiskError(f"Action chưa hỗ trợ execution: {action}")


async def analyze_and_execute(
    *,
    user_id: Optional[int] = None,
    runtime: UserRuntimeState,
    settings: Dict[str, Any],
    raw_decision: Dict[str, Any],
    snapshots: Dict[str, Any],
) -> Dict[str, Any]:
    client = make_client(settings)
    guard = make_risk_guard(settings, runtime)
    allowed_symbols = allowed_symbols_from_guard(guard)
    if not allowed_symbols:
        raise RiskError("allowed_symbols đang rỗng.")

    if str(raw_decision.get("action", "WAIT")).upper().strip() in {"WAIT", "HOLD", "NO_TRADE"}:
        normalized = guard.normalize_action(raw_decision, market_price=Decimal("1"), qty_step=Decimal("0.001"), min_qty=Decimal("0.001"), daily_trade_count=runtime.daily_trade_count)
        result = await execute_action(client, guard, normalized)
        return {"normalized": normalized, "execution": result, "risk": guard.public_config()}

    decision_symbol = str(raw_decision.get("symbol") or allowed_symbols[0]).upper().strip()
    decision_category = guard.resolve_category(raw_decision)
    action_name = str(raw_decision.get("action", "WAIT")).upper().strip()
    if decision_symbol not in allowed_symbols and action_name not in {"WAIT", "HOLD", "NO_TRADE"}:
        raise RiskError(f"AI chọn symbol ngoài allowed list: {decision_symbol}")

    key = snapshot_key(decision_category, decision_symbol)
    price_source = snapshots.get(key)
    if not price_source or price_source.get("error"):
        price_source = await build_market_snapshot(client, decision_symbol, decision_category)
    last_price = Decimal(str(price_source["ticker"].get("lastPrice") or price_source["ticker"].get("markPrice") or "0"))
    qty_step, min_qty, min_order_amt, tick_size = await get_instrument_filters(client, decision_symbol, decision_category)

    normalized = guard.normalize_action(
        raw_decision,
        market_price=last_price,
        qty_step=qty_step,
        min_qty=min_qty,
        min_order_amt=min_order_amt,
        price_tick=tick_size,
        daily_trade_count=runtime.daily_trade_count,
    )
    result = await execute_action(client, guard, normalized, qty_step=qty_step, min_qty=min_qty)

    if normalized["action"] != "WAIT":
        guard.mark_trade_sent()
        runtime.last_trade_ts = guard.last_trade_ts
        runtime.daily_trade_count += 1
        runtime.last_action_at = runtime.now_iso()

    payload = {"normalized": normalized, "execution": result, "risk": guard.public_config(), "market_price": str(last_price)}
    if user_id is not None:
        await record_trade_tracking(user_id, runtime, payload)
    return payload


async def bot_loop_safe(user_id: int, stop_event: asyncio.Event) -> None:
    runtime = runtimes.get(user_id)
    prompt_hash: Optional[int] = None

    try:
        await runtime.log("INFO", "Bot bắt đầu chạy.")

        while not stop_event.is_set():
            runtime.reset_daily_counter_if_needed()
            ws = get_workspace(user_id, redact=False)
            settings = ws["settings"]
            prompt = (ws.get("prompt") or "").strip()
            client = make_client(settings)
            engine = make_engine(settings)
            guard = make_risk_guard(settings, runtime)
            interval = max(5, int(settings.get("loop_interval_seconds") or 30))
            allowed_symbols = allowed_symbols_from_guard(guard)

            if not client.is_configured:
                await runtime.log("ERROR", "Thiếu Bybit API Key/Secret trong Workspace Settings. Bot dừng.")
                return
            if not allowed_symbols:
                await runtime.log("ERROR", "allowed_symbols đang rỗng. Bot dừng.")
                return
            if not engine.enabled:
                await runtime.log("WARN", "OPENAI_API_KEY chưa cấu hình. Bot chỉ log WAIT, không tự giao dịch.")
            if not prompt:
                await runtime.log("WARN", "Chưa có prompt. Hãy nhập prompt rồi bấm Save Prompt.")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    continue
                continue

            new_hash = hash(prompt)
            if new_hash != prompt_hash:
                prompt_hash = new_hash
                await runtime.log("INFO", "Đã tải prompt mới.")
                await runtime.log("INFO", f"Bybit: {client.env} · {client.signing_label()} · dry_run={guard.config.dry_run} · key={client.masked_key()}")

            prompt_meta = parse_strategy_prompt(prompt, allowed_symbols_from_guard(guard))
            wait_left = _scheduled_prompt_wait_seconds(settings, prompt, prompt_meta)
            if wait_left > 0:
                # Do not call AI or Bybit while a recurring/DCA prompt is still in its wait window.
                # This prevents `10 USDT/1h` from being executed every loop tick.
                await wait_with_tp_sl_monitor(user_id, runtime, settings, stop_event, min(wait_left, interval))
                continue

            await runtime.log("INFO", "Đang kiểm tra Bybit...")
            try:
                conn = await asyncio.wait_for(client.test_connection(), timeout=25)
            except asyncio.TimeoutError:
                await runtime.log("ERROR", "Bybit connection timeout sau 25 giây. Bot dừng để tránh treo im lặng.")
                return
            except BybitAPIError as exc:
                await runtime.log("ERROR", f"Bybit connection failed: {exc}")
                return
            except Exception as exc:
                await runtime.log("ERROR", f"Bybit connection crashed: {type(exc).__name__}: {exc}")
                return
            await runtime.log("INFO", f"Bybit OK · env={conn['env']} · lệch giờ={conn['clock_drift_seconds']}s")

            await enforce_tracked_tp_sl(user_id, runtime, settings)

            snapshots = await build_snapshots_for_guard(client, guard, prompt_meta)
            fallback = _action_from_prompt_directives(prompt, prompt_meta)
            use_cost_saver = bool(settings.get("ai_cost_saver", True))
            has_indicator_condition = bool(prompt_meta.get("rsi_rules") or prompt_meta.get("indicators"))
            ai_snapshot = {"symbols": _compact_snapshot_for_ai(snapshots, prompt_meta)}
            if _is_exact_rsi_candle_prompt(prompt_meta):
                raw_decision = _evaluate_exact_rsi_candle_prompt(prompt_meta, ai_snapshot)
                await _log_ai_result(runtime, settings, ai_snapshot, raw_decision)
                await runtime.log("INFO", "Đã xử lý bằng rule engine đúng prompt gốc: RSI 5m + nến 5m. Không gọi AI để tránh tự biên chiến lược khác.")
                await runtime.log("INFO", "AI đã phân tích xong.")
            elif use_cost_saver and fallback and not has_indicator_condition:
                raw_decision = fallback
                await runtime.log("INFO", "Tiết kiệm token: prompt DCA/lệnh rõ ràng được parser xử lý, không gọi AI vòng này.")
            else:
                raw_decision = await engine.decide(
                    prompt=prompt,
                    snapshot=ai_snapshot,
                    risk_config=guard.public_config(),
                    skill_context=("" if _is_prompt_only_mode(prompt_meta) else build_skill_context(mode="strategy_loop", command_or_prompt=prompt)),
                    prompt_directives=prompt_meta,
                )
                await _log_ai_result(runtime, settings, ai_snapshot, raw_decision)
                if is_wait_action(raw_decision) and fallback:
                    raw_decision = fallback
                    await runtime.log("INFO", "Parser an toàn: AI trả WAIT nhưng prompt là lệnh đơn giản/DCA rõ ràng, dùng tín hiệu parser.")
                await runtime.log("INFO", "AI đã phân tích xong.")

            raw_decision = _apply_prompt_tp_sl_constraints(raw_decision, prompt_meta)
            if prompt_meta.get("requires_explicit_tp_sl") and str(raw_decision.get("action") or "").upper() in OPENING_ACTIONS:
                await runtime.log("INFO", "Prompt yêu cầu TP/SL cụ thể theo ATR/RR/structure; bot sẽ không dùng TP/SL mặc định.")

            try:
                result = await analyze_and_execute(user_id=user_id, runtime=runtime, settings=settings, raw_decision=raw_decision, snapshots=snapshots)
                if str((result.get("normalized") or {}).get("action") or "").upper() != "WAIT":
                    _mark_scheduled_prompt_executed(user_id, prompt, prompt_meta)
                await runtime.log("INFO", summarize_trade_result(result))
            except (RiskError, BybitAPIError) as exc:
                await runtime.log("WARN", f"Action blocked/failed: {exc}")

            await wait_with_tp_sl_monitor(user_id, runtime, settings, stop_event, interval)
            continue
    except asyncio.CancelledError:
        await runtime.log("INFO", "Tác vụ bot đã bị huỷ.")
    except Exception as exc:
        await runtime.log("ERROR", f"Bot crashed: {type(exc).__name__}: {exc}")
    finally:
        runtime.running = False
        runtime.stop_event = None
        runtime.active_task = None
        await runtime.log("INFO", "Bot đã dừng.")


async def run_saved_prompt_once(user_id: int) -> Dict[str, Any]:
    runtime = runtimes.get(user_id)
    runtime.reset_daily_counter_if_needed()
    ws = get_workspace(user_id, redact=False)
    settings = ws["settings"]
    prompt = (ws.get("prompt") or "").strip()
    client = make_client(settings)
    engine = make_engine(settings)
    guard = make_risk_guard(settings, runtime)

    await runtime.log("INFO", "Chạy thử prompt một lần.")

    if not prompt:
        msg = "Chưa có prompt. Hãy Save Prompt trước khi Run Once."
        await runtime.log("ERROR", msg)
        raise HTTPException(status_code=400, detail=msg)
    if not client.is_configured:
        msg = "Thiếu Bybit API Key/Secret trong API & Risk Settings."
        await runtime.log("ERROR", msg)
        raise HTTPException(status_code=400, detail=msg)
    if not engine.enabled:
        msg = "Thiếu OpenAI API Key. Prompt strategy cần OpenAI để phân tích thị trường và ra quyết định."
        await runtime.log("ERROR", msg)
        raise HTTPException(status_code=400, detail=msg)
    prompt_meta = parse_strategy_prompt(prompt, allowed_symbols_from_guard(guard))
    wait_left = _scheduled_prompt_wait_seconds(settings, prompt, prompt_meta)
    if wait_left > 0:
        msg = f"Prompt này có lịch {prompt_meta.get('interval_label') or prompt_meta.get('interval_seconds')} nên chưa tới giờ chạy lại. Còn khoảng {wait_left}s."
        await runtime.log("INFO", msg)
        return {"ok": True, "skipped": True, "summary": msg, "prompt_meta": prompt_meta}

    await runtime.log("INFO", "Đang kiểm tra Bybit trước khi chạy thử...")
    try:
        conn = await asyncio.wait_for(client.test_connection(), timeout=25)
    except asyncio.TimeoutError:
        msg = "Bybit connection timeout sau 25 giây."
        await runtime.log("ERROR", msg)
        raise HTTPException(status_code=400, detail=msg)
    except BybitAPIError as exc:
        msg = f"Bybit connection failed: {exc}"
        await runtime.log("ERROR", msg)
        raise HTTPException(status_code=400, detail=msg)
    await runtime.log("INFO", f"Bybit OK · env={conn['env']} · lệch giờ={conn['clock_drift_seconds']}s")
    await enforce_tracked_tp_sl(user_id, runtime, settings)
    snapshots = await build_snapshots_for_guard(client, guard, prompt_meta)
    fallback = _action_from_prompt_directives(prompt, prompt_meta)
    use_cost_saver = bool(settings.get("ai_cost_saver", True))
    has_indicator_condition = bool(prompt_meta.get("rsi_rules") or prompt_meta.get("indicators"))
    ai_snapshot = {"symbols": _compact_snapshot_for_ai(snapshots, prompt_meta)}
    if _is_exact_rsi_candle_prompt(prompt_meta):
        raw_decision = _evaluate_exact_rsi_candle_prompt(prompt_meta, ai_snapshot)
        await _log_ai_result(runtime, settings, ai_snapshot, raw_decision)
        await runtime.log("INFO", "Đã xử lý bằng rule engine đúng prompt gốc: RSI 5m + nến 5m. Không gọi AI để tránh tự biên chiến lược khác.")
        await runtime.log("INFO", "AI đã phân tích xong.")
    elif use_cost_saver and fallback and not has_indicator_condition:
        raw_decision = fallback
        await runtime.log("INFO", "Tiết kiệm token: prompt rõ ràng được parser xử lý, không gọi AI lần này.")
    else:
        raw_decision = await engine.decide(
            prompt=prompt,
            snapshot=ai_snapshot,
            risk_config=guard.public_config(),
            skill_context=("" if _is_prompt_only_mode(prompt_meta) else build_skill_context(mode="strategy_loop", command_or_prompt=prompt)),
            prompt_directives=prompt_meta,
        )
        await _log_ai_result(runtime, settings, ai_snapshot, raw_decision)
        if is_wait_action(raw_decision) and fallback:
            raw_decision = fallback
            await runtime.log("INFO", "Parser an toàn: AI trả WAIT nhưng prompt là lệnh đơn giản/DCA rõ ràng, dùng tín hiệu parser.")
        await runtime.log("INFO", "AI đã phân tích xong.")
    raw_decision = _apply_prompt_tp_sl_constraints(raw_decision, prompt_meta)
    if prompt_meta.get("requires_explicit_tp_sl") and str(raw_decision.get("action") or "").upper() in OPENING_ACTIONS:
        await runtime.log("INFO", "Prompt yêu cầu TP/SL cụ thể theo ATR/RR/structure; bot sẽ không dùng TP/SL mặc định.")
    try:
        result = await analyze_and_execute(user_id=user_id, runtime=runtime, settings=settings, raw_decision=raw_decision, snapshots=snapshots)
    except (RiskError, BybitAPIError) as exc:
        msg = f"Lưu và chạy một lần bị chặn/thất bại: {exc}"
        await runtime.log("WARN", msg)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        msg = f"Lưu và chạy một lần lỗi hệ thống: {type(exc).__name__}: {exc}"
        await runtime.log("ERROR", msg)
        raise HTTPException(status_code=500, detail=msg)
    if str((result.get("normalized") or {}).get("action") or "").upper() != "WAIT":
        _mark_scheduled_prompt_executed(user_id, prompt, prompt_meta)
    await runtime.log("INFO", summarize_trade_result(result))
    return json_safe({"ok": True, "ai_decision": raw_decision, "result": result, "summary": summarize_trade_result(result), "prompt_meta": prompt_meta})


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.post("/api/register")
async def register(payload: AuthIn, response: Response) -> Dict[str, Any]:
    try:
        user = store.create_user(payload.username, payload.password)
    except StoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    response.set_cookie(COOKIE_NAME, create_session_token(user["id"]), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7)
    return {"ok": True, "user": user}


@app.post("/api/login")
async def login(payload: AuthIn, response: Response) -> Dict[str, Any]:
    try:
        user = store.authenticate(payload.username, payload.password)
    except StoreError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    response.set_cookie(COOKIE_NAME, create_session_token(user["id"]), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7)
    return {"ok": True, "user": user}


@app.post("/api/logout")
async def logout(response: Response) -> Dict[str, Any]:
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@app.get("/api/me")
async def me(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return {"ok": True, "user": user}


@app.get("/api/status")
async def status(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    runtime = runtimes.get(user["id"])
    ws = get_workspace(user["id"], redact=True)
    settings = ws["settings"]
    real_ws = get_workspace(user["id"], redact=False)
    guard = make_risk_guard(real_ws["settings"], runtime)
    return {
        "running": runtime.running,
        "started_at": runtime.started_at,
        "last_action_at": runtime.last_action_at,
        "daily_trade_count": runtime.daily_trade_count,
        "bybit_env": settings.get("bybit_env"),
        "bybit_key": settings.get("bybit_api_key_masked", "not-set"),
        "bybit_signing": make_client(real_ws["settings"]).signing_label(),
        "rsa_key": settings.get("bybit_api_private_key_masked", "not-set"),
        "openai_key": settings.get("openai_api_key_masked", "not-set"),
        "openai_model": settings.get("openai_model", "gpt-5.5"),
        "risk": guard.public_config(),
        "skill": read_status(),
        "prompt_exists": bool(ws.get("prompt")),
        "workspace_updated_at": ws.get("updated_at"),
        "loop_interval_seconds": settings.get("loop_interval_seconds"),
    }


@app.get("/api/preflight")
async def preflight(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    runtime = runtimes.get(user["id"])
    ws = get_workspace(user["id"], redact=False)
    report = workspace_preflight(ws, runtime)
    level = "INFO" if report["ok"] else "WARN"
    await runtime.log(level, "Preflight: " + summarize_preflight(report))
    return report


@app.get("/api/account/summary")
async def account_summary(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    ws = get_workspace(user["id"], redact=False)
    client = make_client(ws["settings"])
    if not client.is_configured:
        return {"ok": False, "configured": False, "summary": None}
    wallet = await client.get_wallet_balance()
    return {"ok": True, "configured": True, "summary": wallet_summary_payload(wallet)}


@app.get("/api/settings")
async def get_settings(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    ws = get_workspace(user["id"], redact=True)
    return {"ok": True, "settings": ws["settings"], "prompt_exists": bool(ws.get("prompt"))}


@app.post("/api/settings")
async def save_settings(payload: SettingsIn, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    incoming = {k: v for k, v in payload.model_dump().items() if v is not None}
    # Không cho paste RSA private key từ UI thường. Private key chỉ được sinh bởi endpoint generate.
    if "bybit_api_private_key" in incoming:
        incoming.pop("bybit_api_private_key", None)
    try:
        saved = store.update_settings(user["id"], incoming)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    runtime = runtimes.get(user["id"])
    await runtime.log("INFO", "Đã lưu cấu hình workspace. API key được lưu riêng theo từng user. Ô key/secret để trống sẽ giữ nguyên giá trị cũ.")
    if runtime.running:
        await runtime.log("WARN", "Bot đang chạy. Nếu vừa đổi API key/cấu hình rủi ro, nên Dừng rồi Bắt đầu lại để kiểm soát rõ.")
    return {"ok": True, "settings": saved}


@app.post("/api/prompt")
async def save_prompt(payload: PromptIn, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    prompt = payload.prompt.strip()
    if len(prompt) < 20:
        raise HTTPException(status_code=400, detail="Prompt quá ngắn. Hãy mô tả rõ chiến lược, coin, rủi ro, điều kiện vào/ra lệnh.")
    ws = get_workspace(user["id"], redact=False)
    meta = parse_strategy_prompt(prompt, ws["settings"].get("allowed_symbols", "").split(","))
    store.save_prompt(user["id"], prompt)
    runtime = runtimes.get(user["id"])
    auto_updates: Dict[str, Any] = {}
    if meta.get("interval_seconds"):
        seconds = int(meta["interval_seconds"])
        auto_updates["loop_interval_seconds"] = seconds
    if auto_updates:
        store.update_settings(user["id"], auto_updates)
    await runtime.log("INFO", "Đã lưu prompt. Prompt cũ đã bị xoá/ghi đè trong workspace này.")
    await runtime.log("INFO", "Prompt parser ghi nhận: " + summarize_strategy_directives(meta))
    if auto_updates.get("loop_interval_seconds"):
        await runtime.log("INFO", f"Đã tự đồng bộ chu kỳ bot theo prompt: {auto_updates['loop_interval_seconds']} giây. Cooldown chống spam đã tắt; chỉ giữ lịch riêng của prompt.")
    return {
        "ok": True,
        "message": "Prompt đã được lưu và ghi đè prompt cũ trong workspace này.",
        "meta": meta,
        "meta_summary": summarize_strategy_directives(meta),
        "auto_updates": auto_updates,
    }


@app.post("/api/prompt/run-once")
async def run_prompt_once(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return await run_saved_prompt_once(user["id"])


@app.get("/api/prompt")
async def get_prompt(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    ws = get_workspace(user["id"], redact=False)
    meta = parse_strategy_prompt(ws.get("prompt", ""), ws["settings"].get("allowed_symbols", "").split(","))
    return {"prompt": ws.get("prompt", ""), "meta": meta, "meta_summary": summarize_strategy_directives(meta)}


@app.post("/api/start")
async def start_bot(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    runtime = runtimes.get(user["id"])
    if runtime.running:
        return {"ok": True, "message": "Bot đang chạy rồi."}
    runtime.stop_event = asyncio.Event()
    runtime.running = True
    runtime.started_at = runtime.now_iso()
    runtime.active_task = asyncio.create_task(bot_loop_safe(user["id"], runtime.stop_event))
    await runtime.log("INFO", "Đã bấm nút Bắt đầu.")
    return {"ok": True, "message": "Bot đã bắt đầu chạy trong workspace hiện tại."}


@app.post("/api/stop")
async def stop_bot(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    runtime = runtimes.get(user["id"])
    if not runtime.running or not runtime.stop_event:
        return {"ok": True, "message": "Bot hiện không chạy."}
    runtime.stop_event.set()
    await runtime.log("INFO", "Đã bấm nút Dừng.")
    return {"ok": True, "message": "Đã gửi tín hiệu dừng bot."}


@app.post("/api/test-connection")
async def test_connection(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    runtime = runtimes.get(user["id"])
    ws = get_workspace(user["id"], redact=False)
    settings = ws["settings"]
    client = make_client(settings)
    try:
        data = await client.test_connection()
        safe = {"env": data["env"], "key": mask_secret(str(settings.get("bybit_api_key") or "")), "signing": data.get("signing"), "clock_drift_seconds": data["clock_drift_seconds"]}
        await runtime.log("INFO", f"Kiểm tra kết nối thủ công thành công | môi trường={safe['env']} | sign={safe['signing']} | key={safe['key']}")
        return {"ok": True, "data": safe}
    except Exception as exc:
        await runtime.log("ERROR", f"Kiểm tra kết nối thủ công thất bại: {exc}")
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/command")
async def direct_command(payload: CommandIn, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    command = payload.command.strip()
    if len(command) < 3:
        raise HTTPException(status_code=400, detail="Lệnh quá ngắn.")

    runtime = runtimes.get(user["id"])
    runtime.reset_daily_counter_if_needed()
    ws = get_workspace(user["id"], redact=False)
    settings = ws["settings"]
    current_prompt = ws.get("prompt") or ""

    lower_command = command.lower()
    if any(k in lower_command for k in ["tạo rsa", "tao rsa", "tạo public key", "tao public key", "tạo key bybit ai", "tao key bybit ai"]):
        pair = generate_rsa_key_pair()
        saved = store.update_settings(user["id"], {"bybit_api_private_key": pair["private_key"], "bybit_auth_type": "rsa"})
        await runtime.log("INFO", "Lệnh trực tiếp đã tạo RSA Public Key riêng cho user này. Copy public key trong mục Cài đặt API & Rủi ro rồi dán vào Bybit.")
        return {"ok": True, "mode": "bot_control", "result": {"type": "RSA_GENERATED", "message": "Đã tạo RSA Public Key riêng cho user này.", "public_key": pair["public_key"], "settings_changed": {"bybit_auth_type": saved.get("bybit_auth_type")}}}

    # 1) Ưu tiên xử lý lệnh điều khiển bot/workspace trước lệnh giao dịch,
    # NHƯNG không được để control parser bắt nhầm lệnh execution trực tiếp.
    # Ví dụ: "đóng hết lệnh future btc" phải đi vào direct execution, không phải allowed_symbols/control.
    skip_control_for_trade = should_skip_bot_control_for_command(command)
    control = {"matched": False}
    if not skip_control_for_trade:
        control = parse_control_command(command, settings, current_prompt)
    if control.get("matched"):
        safe_command = redact_command_for_log(command)
        await runtime.log("INFO", f"Nhận lệnh điều chỉnh bot: {safe_command}")

        result: Dict[str, Any] = {
            "type": "BOT_CONTROL",
            "message": control.get("message"),
            "warnings": control.get("warnings", []),
            "settings_changed": {},
            "prompt_changed": False,
        }

        changed_settings = control.get("settings") or {}
        if changed_settings:
            try:
                saved = store.update_settings(user["id"], changed_settings)
            except Exception as exc:
                await runtime.log("ERROR", f"Không lưu được cấu hình từ lệnh trực tiếp: {exc}")
                raise HTTPException(status_code=400, detail=str(exc))
            result["settings_changed"] = {k: saved.get(k) for k in changed_settings.keys() if not k.endswith("secret") and "api_key" not in k}

        if control.get("prompt") is not None:
            prompt_text = str(control.get("prompt") or "").strip()
            if len(prompt_text) < 20:
                msg = "Prompt mới quá ngắn. Hãy mô tả rõ chiến lược, coin, điều kiện vào/ra lệnh và quản trị rủi ro."
                await runtime.log("ERROR", msg)
                raise HTTPException(status_code=400, detail=msg)
            store.save_prompt(user["id"], prompt_text)
            prompt_meta = parse_strategy_prompt(prompt_text, str((settings.get("allowed_symbols") or "")).split(","))
            if prompt_meta.get("interval_seconds"):
                seconds = int(prompt_meta["interval_seconds"])
                updates = {"loop_interval_seconds": seconds}
                saved = store.update_settings(user["id"], updates)
                result["settings_changed"]["loop_interval_seconds"] = saved.get("loop_interval_seconds")
            result["prompt_changed"] = True
            result["prompt_meta_summary"] = summarize_strategy_directives(prompt_meta)

        for warn in control.get("warnings", []):
            await runtime.log("WARN", str(warn))
        await runtime.log("INFO", "Đã áp dụng lệnh điều chỉnh bot: " + str(result.get("message") or "cấu hình đã được cập nhật"))
        if runtime.running:
            await runtime.log("WARN", "Bot đang chạy. Nếu vừa đổi API/risk/prompt quan trọng, nên Stop rồi Start lại để kiểm soát rõ.")
        return {"ok": True, "mode": "bot_control", "result": result}

    # 2) Nếu không phải lệnh điều chỉnh bot, xử lý như lệnh giao dịch.
    client = make_client(settings)
    engine = make_engine(settings)
    guard = make_risk_guard(settings, runtime)

    if not client.is_configured:
        msg = "Thiếu Bybit API Key/Secret trong phần Cài đặt API & Rủi ro."
        await runtime.log("ERROR", msg)
        raise HTTPException(status_code=400, detail=msg)

    if is_balance_query(command):
        await runtime.log("INFO", f"Nhận lệnh kiểm tra số dư: {redact_command_for_log(command)}")
        try:
            wallet = await client.get_wallet_balance()
            summary = wallet_summary_text(wallet)
            await runtime.log("INFO", "Số dư tài khoản: " + summary)
            return {"ok": True, "mode": "account_query", "result": {"message": summary}}
        except Exception as exc:
            await runtime.log("ERROR", f"Kiểm tra số dư thất bại: {exc}")
            raise HTTPException(status_code=400, detail=str(exc))

    await runtime.log("INFO", f"Nhận lệnh giao dịch trực tiếp: {redact_command_for_log(command)}")
    try:
        snapshots = await build_snapshots_for_guard(client, guard)
        parsed_first = parse_direct_command(command, allowed_symbols_from_guard(guard), guard.config.default_category)
        clear_direct = should_skip_bot_control_for_command(command)
        use_cost_saver = bool(settings.get("ai_cost_saver", True))
        if clear_direct and not is_wait_action(parsed_first):
            raw_decision = parsed_first
            await runtime.log("INFO", "Direct Command: đã hiểu bằng parser nội bộ, không gọi AI để tránh tự biên chiến lược khác.")
        elif use_cost_saver and not is_wait_action(parsed_first):
            raw_decision = parsed_first
            await runtime.log("INFO", "Tiết kiệm token: lệnh trực tiếp rõ ràng được parser xử lý, không gọi AI.")
        elif engine.enabled:
            raw_decision = await engine.command_to_action(
                command=command,
                snapshot={"symbols": _compact_snapshot_for_ai(snapshots)},
                risk_config=guard.public_config(),
                skill_context=build_skill_context(mode="manual_direct_command", command_or_prompt=command),
            )
            await runtime.log("INFO", "AI đã hiểu lệnh trực tiếp và chuyển thành tín hiệu giao dịch.")
            if is_wait_action(raw_decision):
                if not is_wait_action(parsed_first):
                    raw_decision = parsed_first
                    await runtime.log("WARN", "AI trả về đứng ngoài cho lệnh trực tiếp. Đã dùng bộ đọc lệnh nội bộ để tiếp tục thực thi.")
                else:
                    msg = direct_wait_error(parsed_first if parsed_first else raw_decision)
                    await runtime.log("WARN", msg)
                    raise RiskError(msg)
        else:
            raw_decision = parsed_first
            await runtime.log("WARN", "Chưa nhập OpenAI key. Đã dùng bộ đọc lệnh đơn giản để xử lý lệnh trực tiếp.")
            if is_wait_action(raw_decision):
                msg = direct_wait_error(raw_decision)
                await runtime.log("WARN", msg)
                raise RiskError(msg)
            await runtime.log("INFO", "Bộ đọc lệnh đơn giản đã chuyển câu lệnh thành tín hiệu giao dịch.")
        raw_decision = merge_direct_parser_with_ai(command, raw_decision, guard)
        result = await analyze_and_execute(user_id=user["id"], runtime=runtime, settings=settings, raw_decision=raw_decision, snapshots=snapshots)
        await runtime.log("INFO", summarize_trade_result(result))
        return json_safe({"ok": True, "mode": "trade_command", "ai_parse": raw_decision, "result": result, "summary": summarize_trade_result(result)})
    except (RiskError, BybitAPIError) as exc:
        await runtime.log("WARN", f"Lệnh trực tiếp bị chặn/thất bại: {exc}")
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        await runtime.log("ERROR", f"Lệnh trực tiếp lỗi hệ thống: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))



@app.get("/api/bybit-rsa/status")
async def bybit_rsa_status(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    ws = get_workspace(user["id"], redact=False)
    settings = ws["settings"]
    private_key = str(settings.get("bybit_api_private_key") or "")
    public_key = ""
    if private_key:
        try:
            public_key = public_key_from_private(private_key)
        except Exception:
            public_key = ""
    return {
        "ok": True,
        "has_private_key": bool(private_key),
        "public_key": public_key,
        "auth_type": settings.get("bybit_auth_type", "auto"),
        "bybit_key_masked": mask_secret(str(settings.get("bybit_api_key") or "")),
    }


@app.post("/api/bybit-rsa/generate")
async def bybit_rsa_generate(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    runtime = runtimes.get(user["id"])
    pair = generate_rsa_key_pair()
    saved = store.update_settings(user["id"], {
        "bybit_api_private_key": pair["private_key"],
        "bybit_auth_type": "rsa",
    })
    await runtime.log("INFO", "Đã tạo RSA key pair riêng cho user này. Chỉ public key được hiển thị để dán vào Bybit; private key được mã hoá trong workspace.")
    return {"ok": True, "public_key": pair["public_key"], "settings": saved}


@app.post("/api/bybit-rsa/clear")
async def bybit_rsa_clear(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    runtime = runtimes.get(user["id"])
    ws = get_workspace(user["id"], redact=False)
    settings = {**ws["settings"], "bybit_api_private_key": "", "bybit_auth_type": "hmac"}
    # update_settings giữ nguyên secret khi chuỗi rỗng, nên ghi trực tiếp qua DEFAULT key bằng cách dùng API riêng.
    store.update_settings(user["id"], {"bybit_auth_type": "hmac"})
    # Clear field directly through encrypted settings by using private helper-safe DB path.
    import time as _time, json as _json
    now = int(_time.time())
    encoded = store._encode_settings(settings)  # internal but controlled; no user input is exposed
    with store._connect() as conn:
        conn.execute("UPDATE workspace_settings SET settings_json=?, updated_at=? WHERE user_id=?", (_json.dumps(encoded, ensure_ascii=False), now, user["id"]))
    await runtime.log("WARN", "Đã xoá RSA private key của user này và chuyển ký API về HMAC.")
    return {"ok": True, "settings": store.get_workspace(user["id"], redact=True)["settings"]}


@app.get("/api/skill/status")
async def skill_status(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return read_status()


@app.post("/api/skill/update")
async def skill_update(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    runtime = runtimes.get(user["id"])
    await runtime.log("INFO", "Đang kiểm tra và cập nhật Bybit Skill cho CIG AI Subaccount...")
    result = await check_and_update_skill(force=True)
    level = "INFO" if result.get("status") in {"updated", "refreshed", "current"} else "WARN"
    await runtime.log(level, "Kết quả cập nhật Bybit Skill: " + summarize_skill_result(result))
    return {"ok": result.get("status") != "error", "result": result, "status": read_status()}

@app.post("/api/simulation/toggle")
async def toggle_simulation(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    runtime = runtimes.get(user["id"])
    ws = get_workspace(user["id"], redact=False)
    current = bool(ws["settings"].get("dry_run", True))
    saved = store.update_settings(user["id"], {"dry_run": not current})
    mode = "MÔ PHỎNG / DRY_RUN" if saved.get("dry_run") else "LỆNH THẬT / LIVE ORDERS"
    level = "WARN" if not saved.get("dry_run") else "INFO"
    await runtime.log(level, f"Đã chuyển chế độ thực thi sang: {mode}.")
    if runtime.running:
        await runtime.log("WARN", "Bot đang chạy. Nên Stop rồi Start lại sau khi đổi chế độ mô phỏng/lệnh thật.")
    return {"ok": True, "dry_run": saved.get("dry_run"), "settings": saved}


@app.get("/api/trades")
async def tracked_trades(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    ws = get_workspace(user["id"], redact=False)
    settings = ws["settings"]
    client = make_client(settings)
    rows = store.list_tracked_trades(user["id"], limit=80)
    items = []
    for row in rows:
        current_price = row.get("current_price") or row.get("entry_price") or ""
        if client.is_configured and row.get("status") == "open":
            try:
                ticker = await client.get_ticker(str(row.get("symbol") or ""), str(row.get("category") or "spot"))
                current_price = str(ticker.get("lastPrice") or ticker.get("markPrice") or current_price)
            except Exception:
                pass
        snap = pnl_snapshot(row, current_price)
        item = dict(row)
        item["current_price"] = snap.get("current_price") or current_price
        item["pnl"] = snap
        item["leverage_display"] = "Không dùng" if str(row.get("category") or "").lower() == "spot" else (str(row.get("leverage") or "1") + "x")
        item["action_label"] = action_label(str(row.get("action") or ""))
        items.append(item)
    return {"ok": True, "trades": items}


@app.post("/api/trades/{trade_id}/close")
async def close_tracked_trade(trade_id: int, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    runtime = runtimes.get(user["id"])
    store.update_tracked_trade_status(user["id"], trade_id, "closed")
    await runtime.log("INFO", f"Đã đóng thủ công lệnh theo dõi #{trade_id}.")
    return {"ok": True}


@app.post("/api/trades/clear-closed")
async def clear_closed_tracked_trades(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    runtime = runtimes.get(user["id"])
    store.clear_closed_trades(user["id"])
    await runtime.log("INFO", "Đã xoá các lệnh theo dõi đã đóng của workspace hiện tại.")
    return {"ok": True}

@app.get("/api/logs")
async def logs(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return {"logs": runtimes.get(user["id"]).get_logs()}


@app.post("/api/logs/clear")
async def clear_logs(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    store.clear_logs(user["id"])
    runtime = runtimes.get(user["id"])
    runtime.log_history.clear()
    await runtime.log("INFO", "Đã xoá nhật ký live của workspace hiện tại.")
    return {"ok": True}


@app.get("/api/logs/stream")
async def stream_logs(
    user: Dict[str, Any] = Depends(current_user),
    history: bool = Query(False),
) -> StreamingResponse:
    runtime = runtimes.get(user["id"])

    async def event_generator():
        if history:
            for item in runtime.get_logs()[-60:]:
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        while True:
            item = await runtime.log_queue.get()
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
