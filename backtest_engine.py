from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import re
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ai_engine import compact_kline_summary
from bybit_client import BybitClient
from indicator_engine import calculate_indicators


INTERVAL_TO_MS = {
    "1": 60_000,
    "3": 180_000,
    "5": 300_000,
    "15": 900_000,
    "30": 1_800_000,
    "60": 3_600_000,
    "120": 7_200_000,
    "240": 14_400_000,
    "360": 21_600_000,
    "720": 43_200_000,
    "D": 86_400_000,
}

INTERVAL_LABELS = {
    "1": "1m",
    "3": "3m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1h",
    "120": "2h",
    "240": "4h",
    "360": "6h",
    "720": "12h",
    "D": "1d",
}

OPEN_ACTIONS = {"OPEN_LONG", "OPEN_SHORT", "LONG", "SHORT", "BUY", "SELL"}
WAIT_ACTIONS = {"WAIT", "HOLD", "NO_TRADE", ""}

DecisionFn = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


@dataclass
class BacktestConfig:
    symbol: str = "BTCUSDT"
    category: str = "linear"
    interval: str = "5"
    start_time: str = ""
    end_time: str = ""
    strategy_prompt: str = ""
    initial_capital: float = 50.0
    order_margin_usdt: float = 10.0
    leverage: int = 10
    fee_rate: float = 0.0006
    slippage_rate: float = 0.0002
    max_trades_per_day: int = 5
    max_losing_streak_per_day: int = 2
    default_take_profit_pct: float = 10.0
    default_stop_loss_pct: float = 5.0
    confidence_threshold: float = 0.58
    entry_cooldown_candles: int = 0
    lookback_candles: int = 220
    max_ai_candles: int = 500
    decision_mode: str = "ai_once"  # ai_once | rule | ai_each


@dataclass
class Position:
    side: str
    entry_time_ms: int
    entry_price: float
    qty: float
    margin: float
    leverage: int
    take_profit: float
    stop_loss: float
    entry_reason: str = ""
    ai_action: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BacktestTrade:
    symbol: str
    category: str
    side: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    qty: float
    margin: float
    leverage: int
    gross_pnl: float
    fee: float
    net_pnl: float
    net_pnl_pct_on_margin: float
    net_pnl_pct_on_capital: float
    result: str
    entry_reason: str
    exit_reason: str


@dataclass
class BacktestReport:
    config: Dict[str, Any]
    metrics: Dict[str, Any]
    trades: List[Dict[str, Any]]
    logs: List[str]


def parse_time_ms(value: str) -> int:
    """Parse backtest time input as UTC.

    Supported user/UI formats:
    - 2026-05-21T10:37:00Z
    - 2026-05-21T10:37
    - 2026-05-21 10:37:00 UTC
    - 21/05/2026 10:37 SA / CH from Vietnamese browser display

    If timezone is omitted, UTC is assumed. This keeps Railway/server timezone
    from shifting the historical test range.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Thiếu thời gian backtest.")

    text = raw.replace("UTC", "").strip()

    # Vietnamese locale display: dd/mm/yyyy hh:mm SA|CH.
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(SA|CH|AM|PM)?$", text, flags=re.IGNORECASE)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour, minute = int(m.group(4)), int(m.group(5))
        second = int(m.group(6) or 0)
        marker = (m.group(7) or "").upper()
        if marker in {"CH", "PM"} and hour < 12:
            hour += 12
        if marker in {"SA", "AM"} and hour == 12:
            hour = 0
        dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    # Common ISO-like forms.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Định dạng thời gian không hợp lệ: {raw}. Hãy dùng dạng 2026-05-21T10:37:00Z") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _norm_interval(interval: str) -> str:
    raw = str(interval or "5").strip()
    if raw.lower() in {"d", "1d", "d1"}:
        return "D"
    aliases = {"m1": "1", "1m": "1", "m3": "3", "3m": "3", "m5": "5", "5m": "5", "m15": "15", "15m": "15", "m30": "30", "30m": "30", "h1": "60", "1h": "60", "h4": "240", "4h": "240"}
    return aliases.get(raw.lower(), raw)


def _action_side(action: str) -> Optional[str]:
    action = str(action or "").upper().strip()
    if action in {"OPEN_LONG", "LONG", "BUY"}:
        return "LONG"
    if action in {"OPEN_SHORT", "SHORT", "SELL"}:
        return "SHORT"
    return None


def _candle_color(row: List[Any]) -> str:
    try:
        o = Decimal(str(row[1])); c = Decimal(str(row[4]))
        return "green" if c > o else "red" if c < o else "doji"
    except Exception:
        return "doji"


class BacktestRsiWatch:
    """In-memory version of the V54 RSI 5m watch rule for backtests.

    The live bot stores watch state on disk per user/symbol. Backtest must not
    reuse that state because each run needs a clean historical simulation.
    """

    def __init__(self, prompt_meta: Dict[str, Any], symbol: str) -> None:
        self.meta = prompt_meta or {}
        self.symbol = symbol.upper()
        self.state = {"mode": "NONE", "last_processed_candle_time": 0, "green_count": 0, "red_count": 0, "trigger_rsi": None, "last_entry_time": 0}


    def _mean_reversion_decide(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        pack = _first_timeframe_pack(snapshot)
        klines = pack.get("klines") or {}
        source_rows = klines.get("recent_klines") or klines.get("recent_candles") or pack.get("recent_candles") or []
        rows: List[Dict[str, Any]] = []
        for c in source_rows:
            if not isinstance(c, dict):
                continue
            rows.append({
                "t": int(c.get("t") or 0),
                "o": _as_float(c.get("o"), None),
                "h": _as_float(c.get("h"), None),
                "l": _as_float(c.get("l"), None),
                "c": _as_float(c.get("c"), None),
                "v": _as_float(c.get("v"), None),
                "color": str(c.get("color") or "doji").lower(),
            })
        rows = [r for r in rows if r["t"] and r["o"] is not None and r["h"] is not None and r["l"] is not None and r["c"] is not None]
        rows.sort(key=lambda x: x["t"])
        mr = self.plan.get("mean_reversion") if isinstance(self.plan.get("mean_reversion"), dict) else {}
        bb_period = int(_as_float(mr.get("bb_period"), 20) or 20)
        bb_std = float(_as_float(mr.get("bb_stddev"), 2) or 2)
        rsi_period = int(_as_float(mr.get("rsi_period"), 14) or 14)
        adx_period = int(_as_float(mr.get("adx_period"), 14) or 14)
        vol_period = int(_as_float(mr.get("volume_ma_period"), 20) or 20)
        need = max(bb_period + 3, rsi_period + 3, adx_period * 2 + 3, vol_period + 3)
        if len(rows) < need:
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "MeanRev: chưa đủ nến để tính BB/RSI/ADX/VWAP."}
        latest_t = rows[-1]["t"]
        if not self._cooldown_ok(latest_t):
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "MeanRev cooldown: chưa đủ số nến nghỉ sau lệnh gần nhất."}
        closes = [float(r["c"] or 0) for r in rows]
        vols = [float(r.get("v") or 0) for r in rows]
        prev_closes = closes[:-1]
        cur_close = closes[-1]
        prev_close = closes[-2]
        cur = rows[-1]; prev = rows[-2]
        cur_mid = _sma_float(closes, bb_period); cur_std = _std_float(closes, bb_period)
        prev_mid = _sma_float(prev_closes, bb_period); prev_std = _std_float(prev_closes, bb_period)
        rsi_now = _rsi_float(closes, rsi_period)
        adx_now = _adx_float(rows, adx_period)
        vwap_now = _vwap_float(rows, 96)
        vol_ma = _sma_float(vols, vol_period)
        if None in {cur_mid, cur_std, prev_mid, prev_std, rsi_now, adx_now, vwap_now, vol_ma}:
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "MeanRev: indicator chưa đủ dữ liệu."}
        cur_upper = float(cur_mid + bb_std * cur_std); cur_lower = float(cur_mid - bb_std * cur_std)
        prev_upper = float(prev_mid + bb_std * prev_std); prev_lower = float(prev_mid - bb_std * prev_std)
        adx_max = float(_as_float(mr.get("adx_max"), 22) or 22)
        vol_min_ratio = float(_as_float(mr.get("volume_min_ratio"), 0.8) or 0.8)
        min_vwap_dist = float(_as_float(mr.get("min_distance_to_vwap_pct"), 0.35) or 0.35)
        long_rsi_max = float(_as_float(mr.get("long_rsi_max"), 30) or 30)
        short_rsi_min = float(_as_float(mr.get("short_rsi_min"), 70) or 70)
        band_walk_n = int(_as_float(mr.get("block_band_walk_candles"), 3) or 3)
        if adx_now >= adx_max:
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": f"MeanRev WAIT: ADX {adx_now:.2f} >= {adx_max:g}, thị trường dễ trend."}
        if float(cur.get("v") or 0) < float(vol_ma) * vol_min_ratio:
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "MeanRev WAIT: volume thấp."}
        dist_vwap = abs(cur_close - float(vwap_now)) / cur_close * 100 if cur_close else 0
        if dist_vwap < min_vwap_dist:
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": f"MeanRev WAIT: cách VWAP {dist_vwap:.2f}% < {min_vwap_dist:g}%."}
        # Rough band-walk block using current band approximation.
        recent = closes[-band_walk_n:]
        if len(recent) >= band_walk_n and all(x < cur_lower for x in recent):
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "MeanRev WAIT: giá walk dưới Lower Band."}
        if len(recent) >= band_walk_n and all(x > cur_upper for x in recent):
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "MeanRev WAIT: giá walk trên Upper Band."}
        # LONG: previous outside lower band, current re-enters, green candle, RSI oversold.
        if prev_close < prev_lower and cur_close > cur_lower and str(cur.get("color")) == "green" and cur_close > prev_close and rsi_now <= long_rsi_max:
            return self._base("OPEN_LONG", f"MeanRev LONG: prev close dưới LowerBB, current re-enter BB, RSI={rsi_now:.2f}, ADX={adx_now:.2f}, VWAP dist={dist_vwap:.2f}%.")
        # SHORT: previous outside upper band, current re-enters, red candle, RSI overbought.
        if prev_close > prev_upper and cur_close < cur_upper and str(cur.get("color")) == "red" and cur_close < prev_close and rsi_now >= short_rsi_min:
            return self._base("OPEN_SHORT", f"MeanRev SHORT: prev close trên UpperBB, current re-enter BB, RSI={rsi_now:.2f}, ADX={adx_now:.2f}, VWAP dist={dist_vwap:.2f}%.")
        return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": f"MeanRev WAIT: chưa đủ BB re-entry. RSI={rsi_now:.2f}, ADX={adx_now:.2f}."}

    def decide(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        tfs = snapshot.get("timeframes") or {}
        tf5 = tfs.get("5m") or {}
        ind = tf5.get("indicators") or {}
        rsi5 = ind.get("rsi14")
        if rsi5 is None:
            return {"action": "WAIT", "symbol": self.symbol, "category": "linear", "confidence": 0, "reason": "Backtest RSI Watch: thiếu RSI 5m."}
        try:
            rsi5 = float(rsi5)
        except Exception:
            return {"action": "WAIT", "symbol": self.symbol, "category": "linear", "confidence": 0, "reason": "Backtest RSI Watch: RSI 5m không hợp lệ."}

        rows = []
        for c in ((tf5.get("klines") or {}).get("recent_candles") or tf5.get("recent_candles") or []):
            if not isinstance(c, dict):
                continue
            try:
                t = int(c.get("t") or 0)
            except Exception:
                continue
            color = str(c.get("color") or "doji").lower()
            rows.append({"t": t, "color": color})
        rows.sort(key=lambda x: x["t"])
        if not rows:
            return {"action": "WAIT", "symbol": self.symbol, "category": "linear", "confidence": 0, "reason": "Backtest RSI Watch: thiếu nến 5m để đếm màu."}

        latest = rows[-1]
        latest_t = latest["t"]
        mode = str(self.state.get("mode") or "NONE").upper()
        n = int(self.meta.get("candle_confirm_count") or 2)
        long_th = self.meta.get("rsi_long_below")
        short_th = self.meta.get("rsi_short_above")
        base = {
            "category": "linear",
            "symbol": self.symbol,
            "leverage": int(self.meta.get("leverage") or 20),
            "margin_usdt": self.meta.get("futures_margin_usdt") or 10,
            "take_profit_pct": self.meta.get("take_profit_pct"),
            "stop_loss_pct": self.meta.get("stop_loss_pct"),
            "tp_sl_mode": self.meta.get("tp_sl_mode") or "pnl_percent",
            "confidence": 100,
            "_backtest_rule_engine": True,
        }

        if mode not in {"LONG_WATCH", "SHORT_WATCH"}:
            if long_th is not None and rsi5 < float(long_th):
                self.state.update({"mode": "LONG_WATCH", "last_processed_candle_time": latest_t, "green_count": 0, "red_count": 0, "trigger_rsi": rsi5})
                return {"action": "WAIT", "symbol": self.symbol, "category": "linear", "confidence": 0, "reason": f"Backtest LONG_WATCH bật: RSI5={rsi5:.2f} < {float(long_th):g}."}
            if short_th is not None and rsi5 > float(short_th):
                self.state.update({"mode": "SHORT_WATCH", "last_processed_candle_time": latest_t, "green_count": 0, "red_count": 0, "trigger_rsi": rsi5})
                return {"action": "WAIT", "symbol": self.symbol, "category": "linear", "confidence": 0, "reason": f"Backtest SHORT_WATCH bật: RSI5={rsi5:.2f} > {float(short_th):g}."}
            return {"action": "WAIT", "symbol": self.symbol, "category": "linear", "confidence": 0, "reason": f"Backtest RSI Watch: RSI5={rsi5:.2f}, chưa chạm trigger."}

        last_processed = int(self.state.get("last_processed_candle_time") or 0)
        new_rows = [r for r in rows if int(r.get("t") or 0) > last_processed]
        if not new_rows:
            return {"action": "WAIT", "symbol": self.symbol, "category": "linear", "confidence": 0, "reason": f"Backtest {mode}: chưa có nến xác nhận mới."}

        processed = []
        for row in new_rows:
            color = str(row.get("color") or "doji").lower()
            processed.append(color)
            self.state["last_processed_candle_time"] = int(row.get("t") or 0)
            if mode == "LONG_WATCH":
                if color == "green":
                    self.state["green_count"] = int(self.state.get("green_count") or 0) + 1
                    if int(self.state["green_count"]) >= n:
                        trig = self.state.get("trigger_rsi")
                        self.state.update({"mode": "NONE", "green_count": 0, "red_count": 0})
                        return {**base, "action": "OPEN_LONG", "reason": f"Backtest rule: RSI5 trigger {float(trig or 0):.2f}, sau đó đủ {n} nến xanh: {','.join(processed)}."}
                else:
                    self.state.update({"mode": "NONE", "green_count": 0, "red_count": 0})
                    return {"action": "WAIT", "symbol": self.symbol, "category": "linear", "confidence": 0, "reason": f"Backtest hủy LONG_WATCH vì nến {color}."}
            elif mode == "SHORT_WATCH":
                if color == "red":
                    self.state["red_count"] = int(self.state.get("red_count") or 0) + 1
                    if int(self.state["red_count"]) >= n:
                        trig = self.state.get("trigger_rsi")
                        self.state.update({"mode": "NONE", "green_count": 0, "red_count": 0})
                        return {**base, "action": "OPEN_SHORT", "reason": f"Backtest rule: RSI5 trigger {float(trig or 0):.2f}, sau đó đủ {n} nến đỏ: {','.join(processed)}."}
                else:
                    self.state.update({"mode": "NONE", "green_count": 0, "red_count": 0})
                    return {"action": "WAIT", "symbol": self.symbol, "category": "linear", "confidence": 0, "reason": f"Backtest hủy SHORT_WATCH vì nến {color}."}

        return {"action": "WAIT", "symbol": self.symbol, "category": "linear", "confidence": 0, "reason": f"Backtest {mode}: đang đếm nến xác nhận."}




def _first_timeframe_pack(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    tfs = snapshot.get("timeframes") or {}
    if isinstance(tfs, dict) and tfs:
        for _, pack in tfs.items():
            if isinstance(pack, dict):
                return pack
    return {}


def _nested(data: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return cur if cur is not None else default


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default




def _sma_float(values: List[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def _std_float(values: List[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    chunk = values[-period:]
    mean = sum(chunk) / period
    return (sum((x - mean) ** 2 for x in chunk) / period) ** 0.5


def _rsi_float(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) <= period:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _adx_float(rows: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
    if len(rows) < period * 2 + 1:
        return None
    trs: List[float] = []
    plus_dm: List[float] = []
    minus_dm: List[float] = []
    for i in range(1, len(rows)):
        high = float(rows[i].get('h') or 0)
        low = float(rows[i].get('l') or 0)
        prev_high = float(rows[i - 1].get('h') or 0)
        prev_low = float(rows[i - 1].get('l') or 0)
        prev_close = float(rows[i - 1].get('c') or 0)
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        up_move = high - prev_high
        down_move = prev_low - low
        pdm = up_move if up_move > down_move and up_move > 0 else 0.0
        mdm = down_move if down_move > up_move and down_move > 0 else 0.0
        trs.append(tr); plus_dm.append(pdm); minus_dm.append(mdm)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    p = sum(plus_dm[:period]) / period
    m = sum(minus_dm[:period]) / period
    dxs: List[float] = []
    for i in range(period, len(trs)):
        atr = ((atr * (period - 1)) + trs[i]) / period
        p = ((p * (period - 1)) + plus_dm[i]) / period
        m = ((m * (period - 1)) + minus_dm[i]) / period
        if atr <= 0:
            continue
        pdi = 100 * (p / atr)
        mdi = 100 * (m / atr)
        denom = pdi + mdi
        if denom > 0:
            dxs.append(100 * abs(pdi - mdi) / denom)
    if len(dxs) < period:
        return None
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = ((adx * (period - 1)) + dx) / period
    return adx


def _vwap_float(rows: List[Dict[str, Any]], period: int = 96) -> Optional[float]:
    chunk = rows[-period:] if len(rows) > period else rows
    pv = 0.0
    vol = 0.0
    for r in chunk:
        h = float(r.get('h') or 0); l = float(r.get('l') or 0); c = float(r.get('c') or 0); v = float(r.get('v') or 0)
        typical = (h + l + c) / 3.0
        pv += typical * v
        vol += v
    return pv / vol if vol > 0 else None

class BacktestPlanEvaluator:
    """Deterministic evaluator for the cost-saving backtest modes.

    The AI can be called once to compile a natural-language strategy into a
    compact plan. This evaluator then walks through historical candles without
    calling AI on every candle. It intentionally supports the common scalping
    rules used in this project: RSI trigger, candle confirmation, EMA filter,
    TP/SL percent on margin and fixed risk limits.
    """

    def __init__(self, plan: Dict[str, Any], prompt_meta: Dict[str, Any], cfg: BacktestConfig) -> None:
        self.plan = plan or {}
        self.meta = prompt_meta or {}
        self.cfg = cfg
        self.symbol = cfg.symbol.upper()
        self.state = {"mode": "NONE", "last_processed_candle_time": 0, "green_count": 0, "red_count": 0, "trigger_rsi": None, "last_entry_time": 0}

    def _rule_value(self, side: str, name: str, fallback: Any = None) -> Any:
        side_key = "long" if side == "LONG" else "short"
        for path in (
            (side_key, name),
            (f"{side_key}_entry", name),
            ("rules", side_key, name),
            ("entry_rules", side_key, name),
        ):
            v = _nested(self.plan, *path)
            if v is not None:
                return v
        return fallback

    def _base(self, action: str, reason: str) -> Dict[str, Any]:
        risk = self.plan.get("risk") if isinstance(self.plan.get("risk"), dict) else {}
        now_t = int(self.state.get("last_processed_candle_time") or 0)
        if now_t:
            self.state["last_entry_time"] = now_t
        return {
            "action": action,
            "category": self.cfg.category,
            "symbol": self.symbol,
            "leverage": int(_as_float(risk.get("leverage"), self.cfg.leverage) or self.cfg.leverage),
            "margin_usdt": _as_float(risk.get("margin_usdt"), self.cfg.order_margin_usdt) or self.cfg.order_margin_usdt,
            "take_profit_pct": _as_float(risk.get("take_profit_pct"), self.cfg.default_take_profit_pct),
            "stop_loss_pct": _as_float(risk.get("stop_loss_pct"), self.cfg.default_stop_loss_pct),
            "tp_sl_mode": risk.get("tp_sl_mode") or self.meta.get("tp_sl_mode") or "pnl_percent",
            "confidence": _as_float(self.plan.get("confidence"), 80) or 80,
            "reason": reason,
            "_backtest_plan_engine": True,
        }

    def _ema_filter_ok(self, side: str, indicators: Dict[str, Any], price: float) -> bool:
        raw = self._rule_value(side, "price_vs_ema") or self._rule_value(side, "ema_filter") or ""
        raw = str(raw or "").lower().strip()
        if not raw or raw in {"none", "off", "false", "no"}:
            return True
        ema_num = 50
        m = re.search(r"(20|50|200)", raw)
        if m:
            ema_num = int(m.group(1))
        ema_value = _as_float(indicators.get(f"ema{ema_num}"), None)
        if not ema_value or not price:
            return False
        if side == "LONG":
            if "below" in raw or "duoi" in raw:
                return price < ema_value
            return price > ema_value
        if "above" in raw or "tren" in raw:
            return price > ema_value
        return price < ema_value

    def _cooldown_ok(self, latest_t: int) -> bool:
        risk = self.plan.get("risk") if isinstance(self.plan.get("risk"), dict) else {}
        cooldown = int(_as_float(risk.get("cooldown_candles"), getattr(self.cfg, "entry_cooldown_candles", 0)) or 0)
        if cooldown <= 0:
            return True
        last_entry = int(self.state.get("last_entry_time") or 0)
        if not last_entry:
            return True
        step = INTERVAL_TO_MS.get(_norm_interval(self.cfg.interval), 300_000)
        passed = max(0, (latest_t - last_entry) // step)
        return passed >= cooldown

    def _advanced_filters_ok(self, side: str, indicators: Dict[str, Any], price: float) -> tuple[bool, str]:
        """Extra deterministic filters for high-winrate AI-once backtests."""
        side_plan = self.plan.get("long" if side == "LONG" else "short") if isinstance(self.plan.get("long" if side == "LONG" else "short"), dict) else {}
        risk = self.plan.get("risk") if isinstance(self.plan.get("risk"), dict) else {}
        trend = str(indicators.get("trend") or "").lower()
        ema20 = _as_float(indicators.get("ema20"), None)
        ema50 = _as_float(indicators.get("ema50"), None)
        ema200 = _as_float(indicators.get("ema200"), None)
        atr14 = _as_float(indicators.get("atr14"), None)
        volume_status = str(indicators.get("volume_status") or "").lower()

        trend_filter = str(side_plan.get("trend_filter") or self.plan.get("trend_filter") or "").lower()
        if trend_filter in {"with_trend", "trend_follow", "follow_trend", "ema_trend", "strict"}:
            if side == "LONG" and trend == "downtrend":
                return False, "trend filter chặn LONG: indicator trend đang downtrend"
            if side == "SHORT" and trend == "uptrend":
                return False, "trend filter chặn SHORT: indicator trend đang uptrend"

        ema_alignment = str(side_plan.get("ema_alignment") or "").lower()
        if ema_alignment in {"ema20_gt_ema50", "20>50", "bullish"} and not (ema20 and ema50 and ema20 > ema50):
            return False, "EMA alignment chặn LONG: EMA20 chưa > EMA50"
        if ema_alignment in {"ema20_lt_ema50", "20<50", "bearish"} and not (ema20 and ema50 and ema20 < ema50):
            return False, "EMA alignment chặn SHORT: EMA20 chưa < EMA50"

        max_dist = _as_float(side_plan.get("max_distance_ema50_pct"), _as_float(risk.get("max_distance_ema50_pct"), None))
        if max_dist is not None and ema50 and price:
            dist = abs(price - ema50) / price * 100
            if dist > max_dist:
                return False, f"EMA distance filter chặn: cách EMA50 {dist:.2f}% > {max_dist:g}%"

        min_atr = _as_float(risk.get("min_atr_pct"), None)
        max_atr = _as_float(risk.get("max_atr_pct"), None)
        if atr14 and price:
            atr_pct = atr14 / price * 100
            if min_atr is not None and atr_pct < min_atr:
                return False, f"ATR filter chặn: ATR {atr_pct:.3f}% < {min_atr:g}%"
            if max_atr is not None and atr_pct > max_atr:
                return False, f"ATR filter chặn: ATR {atr_pct:.3f}% > {max_atr:g}%"

        require_volume = bool(side_plan.get("require_volume_not_low") or risk.get("require_volume_not_low"))
        if require_volume and volume_status == "below_average":
            return False, "Volume filter chặn: volume dưới trung bình"

        if bool(side_plan.get("avoid_against_ema200") or risk.get("avoid_against_ema200")) and ema200 and price:
            if side == "LONG" and price < ema200:
                return False, "EMA200 filter chặn LONG: giá dưới EMA200"
            if side == "SHORT" and price > ema200:
                return False, "EMA200 filter chặn SHORT: giá trên EMA200"

        return True, "ok"


    def _mean_reversion_decide(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        pack = _first_timeframe_pack(snapshot)
        klines = pack.get("klines") or {}
        source_rows = klines.get("recent_klines") or klines.get("recent_candles") or pack.get("recent_candles") or []
        rows: List[Dict[str, Any]] = []
        for c in source_rows:
            if not isinstance(c, dict):
                continue
            rows.append({
                "t": int(c.get("t") or 0),
                "o": _as_float(c.get("o"), None),
                "h": _as_float(c.get("h"), None),
                "l": _as_float(c.get("l"), None),
                "c": _as_float(c.get("c"), None),
                "v": _as_float(c.get("v"), None),
                "color": str(c.get("color") or "doji").lower(),
            })
        rows = [r for r in rows if r["t"] and r["o"] is not None and r["h"] is not None and r["l"] is not None and r["c"] is not None]
        rows.sort(key=lambda x: x["t"])
        mr = self.plan.get("mean_reversion") if isinstance(self.plan.get("mean_reversion"), dict) else {}
        bb_period = int(_as_float(mr.get("bb_period"), 20) or 20)
        bb_std = float(_as_float(mr.get("bb_stddev"), 2) or 2)
        rsi_period = int(_as_float(mr.get("rsi_period"), 14) or 14)
        adx_period = int(_as_float(mr.get("adx_period"), 14) or 14)
        vol_period = int(_as_float(mr.get("volume_ma_period"), 20) or 20)
        need = max(bb_period + 3, rsi_period + 3, adx_period * 2 + 3, vol_period + 3)
        if len(rows) < need:
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "MeanRev: chưa đủ nến để tính BB/RSI/ADX/VWAP."}
        latest_t = rows[-1]["t"]
        if not self._cooldown_ok(latest_t):
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "MeanRev cooldown: chưa đủ số nến nghỉ sau lệnh gần nhất."}
        closes = [float(r["c"] or 0) for r in rows]
        vols = [float(r.get("v") or 0) for r in rows]
        prev_closes = closes[:-1]
        cur_close = closes[-1]
        prev_close = closes[-2]
        cur = rows[-1]; prev = rows[-2]
        cur_mid = _sma_float(closes, bb_period); cur_std = _std_float(closes, bb_period)
        prev_mid = _sma_float(prev_closes, bb_period); prev_std = _std_float(prev_closes, bb_period)
        rsi_now = _rsi_float(closes, rsi_period)
        adx_now = _adx_float(rows, adx_period)
        vwap_now = _vwap_float(rows, 96)
        vol_ma = _sma_float(vols, vol_period)
        if None in {cur_mid, cur_std, prev_mid, prev_std, rsi_now, adx_now, vwap_now, vol_ma}:
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "MeanRev: indicator chưa đủ dữ liệu."}
        cur_upper = float(cur_mid + bb_std * cur_std); cur_lower = float(cur_mid - bb_std * cur_std)
        prev_upper = float(prev_mid + bb_std * prev_std); prev_lower = float(prev_mid - bb_std * prev_std)
        adx_max = float(_as_float(mr.get("adx_max"), 22) or 22)
        vol_min_ratio = float(_as_float(mr.get("volume_min_ratio"), 0.8) or 0.8)
        min_vwap_dist = float(_as_float(mr.get("min_distance_to_vwap_pct"), 0.35) or 0.35)
        long_rsi_max = float(_as_float(mr.get("long_rsi_max"), 30) or 30)
        short_rsi_min = float(_as_float(mr.get("short_rsi_min"), 70) or 70)
        band_walk_n = int(_as_float(mr.get("block_band_walk_candles"), 3) or 3)
        if adx_now >= adx_max:
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": f"MeanRev WAIT: ADX {adx_now:.2f} >= {adx_max:g}, thị trường dễ trend."}
        if float(cur.get("v") or 0) < float(vol_ma) * vol_min_ratio:
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "MeanRev WAIT: volume thấp."}
        dist_vwap = abs(cur_close - float(vwap_now)) / cur_close * 100 if cur_close else 0
        if dist_vwap < min_vwap_dist:
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": f"MeanRev WAIT: cách VWAP {dist_vwap:.2f}% < {min_vwap_dist:g}%."}
        # Rough band-walk block using current band approximation.
        recent = closes[-band_walk_n:]
        if len(recent) >= band_walk_n and all(x < cur_lower for x in recent):
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "MeanRev WAIT: giá walk dưới Lower Band."}
        if len(recent) >= band_walk_n and all(x > cur_upper for x in recent):
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "MeanRev WAIT: giá walk trên Upper Band."}
        # LONG: previous outside lower band, current re-enters, green candle, RSI oversold.
        if prev_close < prev_lower and cur_close > cur_lower and str(cur.get("color")) == "green" and cur_close > prev_close and rsi_now <= long_rsi_max:
            return self._base("OPEN_LONG", f"MeanRev LONG: prev close dưới LowerBB, current re-enter BB, RSI={rsi_now:.2f}, ADX={adx_now:.2f}, VWAP dist={dist_vwap:.2f}%.")
        # SHORT: previous outside upper band, current re-enters, red candle, RSI overbought.
        if prev_close > prev_upper and cur_close < cur_upper and str(cur.get("color")) == "red" and cur_close < prev_close and rsi_now >= short_rsi_min:
            return self._base("OPEN_SHORT", f"MeanRev SHORT: prev close trên UpperBB, current re-enter BB, RSI={rsi_now:.2f}, ADX={adx_now:.2f}, VWAP dist={dist_vwap:.2f}%.")
        return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": f"MeanRev WAIT: chưa đủ BB re-entry. RSI={rsi_now:.2f}, ADX={adx_now:.2f}."}

    def decide(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        if _plan_is_generic_condition_strategy(self.plan):
            return _generic_condition_decide(self, snapshot)
        if str(self.plan.get("strategy_type") or "").lower() == "bollinger_vwap_mean_reversion":
            return self._mean_reversion_decide(snapshot)
        pack = _first_timeframe_pack(snapshot)
        ind = pack.get("indicators") or {}
        klines = pack.get("klines") or {}
        rows = []
        # Prefer recent_klines because it contains OHLCV; fall back to compact recent_candles.
        source_rows = klines.get("recent_klines") or klines.get("recent_candles") or pack.get("recent_candles") or []
        for c in source_rows:
            if not isinstance(c, dict):
                continue
            try:
                rows.append({
                    "t": int(c.get("t") or 0),
                    "color": str(c.get("color") or "doji").lower(),
                    "o": _as_float(c.get("o"), None),
                    "h": _as_float(c.get("h"), None),
                    "l": _as_float(c.get("l"), None),
                    "c": _as_float(c.get("c"), None),
                    "v": _as_float(c.get("v"), None),
                })
            except Exception:
                continue
        rows.sort(key=lambda x: x["t"])
        if not rows:
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "Backtest plan: thiếu nến để xét rule."}

        rsi_now = _as_float(ind.get("rsi14"), None)
        price = _as_float(ind.get("last_close"), _as_float((snapshot.get("ticker") or {}).get("lastPrice"), 0)) or 0
        if rsi_now is None:
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "Backtest plan: thiếu RSI14."}

        long_th = _as_float(self._rule_value("LONG", "rsi_below", self.meta.get("rsi_long_below")), None)
        short_th = _as_float(self._rule_value("SHORT", "rsi_above", self.meta.get("rsi_short_above")), None)
        confirm_n = int(_as_float(self.plan.get("confirm_candles"), self.meta.get("candle_confirm_count") or 2) or 2)
        confirm_n = max(1, min(confirm_n, 10))

        latest_t = rows[-1]["t"]
        mode = str(self.state.get("mode") or "NONE").upper()

        if mode not in {"LONG_WATCH", "SHORT_WATCH"} and not self._cooldown_ok(latest_t):
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "Plan cooldown: chưa đủ số nến nghỉ sau lệnh gần nhất."}

        if mode not in {"LONG_WATCH", "SHORT_WATCH"}:
            if long_th is not None and rsi_now < long_th:
                self.state.update({"mode": "LONG_WATCH", "last_processed_candle_time": latest_t, "green_count": 0, "red_count": 0, "trigger_rsi": rsi_now})
                return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": f"Plan LONG_WATCH bật: RSI={rsi_now:.2f} < {long_th:g}."}
            if short_th is not None and rsi_now > short_th:
                self.state.update({"mode": "SHORT_WATCH", "last_processed_candle_time": latest_t, "green_count": 0, "red_count": 0, "trigger_rsi": rsi_now})
                return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": f"Plan SHORT_WATCH bật: RSI={rsi_now:.2f} > {short_th:g}."}
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": f"Plan: RSI={rsi_now:.2f}, chưa chạm trigger."}

        last_processed = int(self.state.get("last_processed_candle_time") or 0)
        new_rows = [r for r in rows if int(r.get("t") or 0) > last_processed]
        if not new_rows:
            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": f"Plan {mode}: chưa có nến xác nhận mới."}

        seen = []
        for row in new_rows:
            color = row["color"]
            seen.append(color)
            self.state["last_processed_candle_time"] = int(row["t"])
            if mode == "LONG_WATCH":
                if color == "green":
                    self.state["green_count"] = int(self.state.get("green_count") or 0) + 1
                    if int(self.state["green_count"]) >= confirm_n:
                        reclaim = _as_float(self._rule_value("LONG", "rsi_reclaim", None), None)
                        if reclaim is not None and rsi_now < reclaim:
                            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": f"Plan chờ LONG: RSI chưa reclaim {reclaim:g}, hiện {rsi_now:.2f}."}
                        if not self._ema_filter_ok("LONG", ind, price):
                            self.state.update({"mode": "NONE", "green_count": 0, "red_count": 0})
                            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "Plan chặn LONG: không đạt EMA filter."}
                        ok, why = self._advanced_filters_ok("LONG", ind, price)
                        if not ok:
                            self.state.update({"mode": "NONE", "green_count": 0, "red_count": 0})
                            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "Plan chặn LONG: " + why}
                        trig = self.state.get("trigger_rsi")
                        self.state.update({"mode": "NONE", "green_count": 0, "red_count": 0})
                        return self._base("OPEN_LONG", f"Plan LONG: RSI trigger {float(trig or 0):.2f}, đủ {confirm_n} nến xanh: {','.join(seen)}.")
                else:
                    self.state.update({"mode": "NONE", "green_count": 0, "red_count": 0})
                    return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": f"Plan hủy LONG_WATCH vì nến {color}."}
            if mode == "SHORT_WATCH":
                if color == "red":
                    self.state["red_count"] = int(self.state.get("red_count") or 0) + 1
                    if int(self.state["red_count"]) >= confirm_n:
                        reject = _as_float(self._rule_value("SHORT", "rsi_reject", None), None)
                        if reject is not None and rsi_now > reject:
                            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": f"Plan chờ SHORT: RSI chưa reject xuống {reject:g}, hiện {rsi_now:.2f}."}
                        if not self._ema_filter_ok("SHORT", ind, price):
                            self.state.update({"mode": "NONE", "green_count": 0, "red_count": 0})
                            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "Plan chặn SHORT: không đạt EMA filter."}
                        ok, why = self._advanced_filters_ok("SHORT", ind, price)
                        if not ok:
                            self.state.update({"mode": "NONE", "green_count": 0, "red_count": 0})
                            return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": "Plan chặn SHORT: " + why}
                        trig = self.state.get("trigger_rsi")
                        self.state.update({"mode": "NONE", "green_count": 0, "red_count": 0})
                        return self._base("OPEN_SHORT", f"Plan SHORT: RSI trigger {float(trig or 0):.2f}, đủ {confirm_n} nến đỏ: {','.join(seen)}.")
                else:
                    self.state.update({"mode": "NONE", "green_count": 0, "red_count": 0})
                    return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": f"Plan hủy SHORT_WATCH vì nến {color}."}

        return {"action": "WAIT", "symbol": self.symbol, "category": self.cfg.category, "confidence": 0, "reason": f"Plan {mode}: đang đếm nến xác nhận."}

class BacktestEngine:
    def __init__(
        self,
        *,
        client: BybitClient,
        decide_fn: DecisionFn,
        prompt_meta: Optional[Dict[str, Any]] = None,
        backtest_plan: Optional[Dict[str, Any]] = None,
        plan_ai_calls: int = 0,
    ) -> None:
        self.client = client
        self.decide_fn = decide_fn
        self.prompt_meta = prompt_meta or {}
        self.backtest_plan = backtest_plan or {}
        self.plan_ai_calls = int(plan_ai_calls or 0)

    async def fetch_klines(self, cfg: BacktestConfig) -> List[List[Any]]:
        interval = _norm_interval(cfg.interval)
        if interval not in INTERVAL_TO_MS:
            raise ValueError(f"Khung thời gian không hỗ trợ: {cfg.interval}")
        start_ms = parse_time_ms(cfg.start_time)
        end_ms = parse_time_ms(cfg.end_time)
        if end_ms <= start_ms:
            raise ValueError("End time phải lớn hơn Start time.")
        step_ms = INTERVAL_TO_MS[interval]
        max_span = step_ms * 999
        cursor = start_ms
        rows_by_ts: Dict[int, List[Any]] = {}
        while cursor <= end_ms:
            chunk_end = min(cursor + max_span, end_ms)
            payload = await self.client.public_get("/v5/market/kline", {
                "category": cfg.category,
                "symbol": cfg.symbol,
                "interval": interval,
                "start": str(cursor),
                "end": str(chunk_end),
                "limit": "1000",
            })
            rows = payload.get("result", {}).get("list", []) or []
            for row in rows:
                try:
                    ts = int(row[0])
                    if start_ms <= ts <= end_ms:
                        rows_by_ts[ts] = row
                except Exception:
                    continue
            cursor = chunk_end + step_ms
        return [rows_by_ts[k] for k in sorted(rows_by_ts.keys())]

    def _structure_from_window(self, rows_oldest: List[List[Any]], lookback: int = 80) -> Dict[str, Any]:
        rows = rows_oldest[-lookback:] if len(rows_oldest) > lookback else rows_oldest
        if not rows:
            return {"status": "no_data"}
        highs = [_float(r[2]) for r in rows]
        lows = [_float(r[3]) for r in rows]
        closes = [_float(r[4]) for r in rows]
        close = closes[-1] if closes else 0
        support = min(lows) if lows else 0
        resistance = max(highs) if highs else 0
        return {
            "status": "ok",
            "lookback": len(rows),
            "last_close": close,
            "recent_support": support,
            "recent_resistance": resistance,
            "swing_low": support,
            "swing_high": resistance,
            "distance_to_support_pct": ((close - support) / close * 100) if close else None,
            "distance_to_resistance_pct": ((resistance - close) / close * 100) if close else None,
        }

    def _snapshot_at(self, cfg: BacktestConfig, window_oldest: List[List[Any]]) -> Dict[str, Any]:
        current = window_oldest[-1]
        interval = _norm_interval(cfg.interval)
        label = INTERVAL_LABELS.get(interval, f"{interval}m")
        raw_newest = list(reversed(window_oldest[-cfg.lookback_candles:]))
        pack = {
            "klines": compact_kline_summary(raw_newest),
            "indicators": calculate_indicators(raw_newest),
            "structure": self._structure_from_window(window_oldest),
        }
        # V60: keep a full OHLCV window for deterministic backtest evaluators.
        # compact_kline_summary only keeps ~20 rows for AI context, which is not
        # enough for user-defined indicators such as EMA200, ADX, Bollinger, VWAP.
        full_rows = []
        for r in raw_newest:
            try:
                color = _candle_color(r)
                full_rows.append({"t": int(r[0]), "o": str(r[1]), "h": str(r[2]), "l": str(r[3]), "c": str(r[4]), "v": str(r[5]), "color": color})
            except Exception:
                continue
        pack["klines"]["recent_klines_full"] = full_rows
        close = str(current[4])
        return {
            "symbol": cfg.symbol,
            "category": cfg.category,
            "ticker": {
                "lastPrice": close,
                "markPrice": close,
                "bid1Price": close,
                "ask1Price": close,
                "volume24h": "",
                "price24hPcnt": "",
            },
            "timeframes": {label: pack},
            "klines_5m": pack["klines"] if label == "5m" else {},
            "indicators_5m": pack["indicators"] if label == "5m" else {},
            "klines_15m": pack["klines"] if label == "15m" else {},
            "indicators_15m": pack["indicators"] if label == "15m" else {},
            "positions": [],
            "wallet": {},
        }

    async def run(self, cfg: BacktestConfig) -> BacktestReport:
        cfg.symbol = cfg.symbol.upper().strip() or "BTCUSDT"
        cfg.category = (cfg.category or "linear").lower().strip()
        cfg.interval = _norm_interval(cfg.interval)
        cfg.leverage = max(1, int(cfg.leverage or 1))
        cfg.lookback_candles = max(60, min(int(cfg.lookback_candles or 220), 500))
        cfg.max_ai_candles = max(20, min(int(cfg.max_ai_candles or 500), 1500))
        cfg.decision_mode = str(getattr(cfg, "decision_mode", "ai_once") or "ai_once").lower().strip()
        if cfg.decision_mode not in {"ai_once", "rule", "ai_each"}:
            cfg.decision_mode = "ai_once"

        start_ms = parse_time_ms(cfg.start_time)
        end_ms = parse_time_ms(cfg.end_time)
        cfg.start_time = iso_ms(start_ms)
        cfg.end_time = iso_ms(end_ms)

        logs: List[str] = []
        rows = await self.fetch_klines(cfg)
        if len(rows) < 80:
            raise RuntimeError(f"Không đủ dữ liệu nến để backtest. Chỉ có {len(rows)} nến.")

        raw_candle_count = len(rows)
        full_first_candle_ms = int(rows[0][0])
        full_last_candle_ms = int(rows[-1][0])

        # Critical fix V57:
        # max_ai_candles is only a cost guard for AI-each-candle mode.
        # In ai_once/rule mode, the bot must process the full requested range
        # because there is no AI call per candle. The previous V56 truncated to
        # the last N candles in every mode, which made a 30-day test only show
        # trades near the last 2-3 days.
        if cfg.decision_mode == "ai_each" and len(rows) > cfg.max_ai_candles + cfg.lookback_candles:
            rows = rows[-(cfg.max_ai_candles + cfg.lookback_candles):]
            logs.append(f"Giới hạn AI candles: AI từng nến chỉ chạy {cfg.max_ai_candles} nến cuối để tránh tốn token/quá tải.")
        elif cfg.decision_mode in {"ai_once", "rule"}:
            logs.append("Full-range mode: không cắt nến theo max_ai_candles vì AI không gọi theo từng nến.")

        logs.append(f"Requested UTC: {cfg.start_time} → {cfg.end_time}.")
        logs.append(f"Data UTC: {iso_ms(int(rows[0][0]))} → {iso_ms(int(rows[-1][0]))}.")
        logs.append(f"Loaded {len(rows)}/{raw_candle_count} candles {cfg.category}:{cfg.symbol} interval={cfg.interval}.")

        equity = float(cfg.initial_capital)
        peak_equity = equity
        max_drawdown = 0.0
        open_pos: Optional[Position] = None
        trades: List[BacktestTrade] = []
        daily_trades: Dict[str, int] = {}
        daily_losses: Dict[str, int] = {}
        ai_calls = 0
        waits = 0
        blocked = 0
        start_idx = min(max(cfg.lookback_candles, 60), len(rows) - 2)
        rsi_rule = BacktestRsiWatch(self.prompt_meta, cfg.symbol) if self.prompt_meta.get("exact_rsi_candle_strategy") and cfg.interval == "5" else None
        plan_rule: Optional[BacktestPlanEvaluator] = None
        if cfg.decision_mode in {"ai_once", "rule"}:
            if self.backtest_plan or self.prompt_meta.get("rsi_long_below") is not None or self.prompt_meta.get("rsi_short_above") is not None:
                plan_rule = BacktestPlanEvaluator(self.backtest_plan, self.prompt_meta, cfg)
                source = str((self.backtest_plan or {}).get("source") or cfg.decision_mode)
                logs.append(f"Decision mode: {cfg.decision_mode}. Bot dùng plan/rule nội bộ, không gọi AI theo từng nến. Plan source={source}.")
            elif rsi_rule is not None:
                logs.append("Decision mode: rule. Bot dùng RSI Rule Engine nội bộ, không gọi AI theo từng nến.")
            else:
                logs.append("Decision mode: rule/ai_once nhưng không có rule đủ rõ; bot sẽ WAIT thay vì gọi AI theo từng nến.")
        else:
            logs.append(f"Decision mode: ai_each. Có thể gọi AI tối đa {cfg.max_ai_candles} lần theo từng nến.")

        for i in range(start_idx, len(rows)):
            current = rows[i]
            ts = int(current[0])
            day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            daily_trades.setdefault(day, 0)
            daily_losses.setdefault(day, 0)

            if open_pos:
                maybe = self._check_exit(open_pos, current, cfg)
                if maybe:
                    equity += maybe.net_pnl
                    trades.append(maybe)
                    if maybe.net_pnl < 0:
                        daily_losses[day] += 1
                    else:
                        daily_losses[day] = 0
                    peak_equity = max(peak_equity, equity)
                    if peak_equity > 0:
                        max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity)
                    logs.append(f"{maybe.exit_time} EXIT {maybe.side} {maybe.exit_reason} net={maybe.net_pnl:.4f} equity={equity:.4f}")
                    open_pos = None

            if open_pos or equity <= 0:
                continue
            if daily_trades[day] >= int(cfg.max_trades_per_day):
                blocked += 1
                continue
            if daily_losses[day] >= int(cfg.max_losing_streak_per_day):
                blocked += 1
                continue

            window = rows[max(0, i - cfg.lookback_candles + 1): i + 1]
            snapshot = self._snapshot_at(cfg, window)
            if plan_rule is not None:
                raw_decision = plan_rule.decide(snapshot)
            elif rsi_rule is not None:
                raw_decision = rsi_rule.decide(snapshot)
            elif cfg.decision_mode == "rule":
                raw_decision = {"action": "WAIT", "reason": "Rule mode: prompt chưa có rule deterministic đủ rõ để mở lệnh."}
            else:
                ai_calls += 1
                raw_decision = await self.decide_fn({
                    "snapshot": snapshot,
                    "snapshots": {f"{cfg.category}:{cfg.symbol}": snapshot},
                    "config": asdict(cfg),
                    "equity": equity,
                    "trades_so_far": len(trades),
                    "wins_so_far": len([t for t in trades if t.net_pnl > 0]),
                    "losses_so_far": len([t for t in trades if t.net_pnl < 0]),
                    "daily_trades": daily_trades[day],
                    "daily_losing_streak": daily_losses[day],
                    "mode": "backtest",
                }) or {"action": "WAIT", "reason": "Backtest decision empty."}

            action = str(raw_decision.get("action") or "WAIT").upper().strip()
            if action in WAIT_ACTIONS:
                waits += 1
                continue
            side = _action_side(action)
            if not side:
                waits += 1
                continue
            confidence = _float(raw_decision.get("confidence"), 0)
            if 0 < confidence < 1:
                conf_ok = confidence >= cfg.confidence_threshold
            else:
                conf_ok = confidence >= cfg.confidence_threshold or confidence >= 58
            if not conf_ok:
                waits += 1
                continue

            margin = _float(raw_decision.get("margin_usdt") or raw_decision.get("order_margin_usdt"), cfg.order_margin_usdt)
            if margin <= 0:
                margin = cfg.order_margin_usdt
            margin = min(margin, cfg.order_margin_usdt, equity)
            if margin <= 0:
                blocked += 1
                continue
            lev = int(_float(raw_decision.get("leverage"), cfg.leverage) or cfg.leverage)
            lev = max(1, min(lev, cfg.leverage))
            entry_price = self._slip(_float(current[4]), side, cfg.slippage_rate, entry=True)
            tp, sl = self._resolve_tp_sl(entry_price, side, lev, cfg, raw_decision)
            if not self._valid_tp_sl(side, entry_price, tp, sl):
                blocked += 1
                logs.append(f"{iso_ms(ts)} BLOCK invalid TP/SL action={action} tp={tp} sl={sl}")
                continue
            qty = (margin * lev) / entry_price if entry_price else 0
            if qty <= 0:
                blocked += 1
                continue
            open_pos = Position(
                side=side,
                entry_time_ms=ts,
                entry_price=entry_price,
                qty=qty,
                margin=margin,
                leverage=lev,
                take_profit=tp,
                stop_loss=sl,
                entry_reason=str(raw_decision.get("reason") or raw_decision.get("entry_reason") or "")[:900],
                ai_action=raw_decision,
            )
            daily_trades[day] += 1
            logs.append(f"{iso_ms(ts)} OPEN {side} entry={entry_price:.4f} tp={tp:.4f} sl={sl:.4f} margin={margin:.2f} lev={lev}x")

        if open_pos:
            last = rows[-1]
            exit_price = self._slip(_float(last[4]), open_pos.side, cfg.slippage_rate, entry=False)
            trade = self._close_position(open_pos, last, exit_price, cfg, "END_OF_BACKTEST")
            equity += trade.net_pnl
            trades.append(trade)
            logs.append(f"{trade.exit_time} FORCE EXIT {trade.side} net={trade.net_pnl:.4f} equity={equity:.4f}")

        metrics = self._metrics(cfg, trades, equity, max_drawdown, ai_calls + self.plan_ai_calls, waits, blocked)
        config_out = asdict(cfg)
        config_out.update({
            "requested_start_time": cfg.start_time,
            "requested_end_time": cfg.end_time,
            "data_start_time": iso_ms(full_first_candle_ms),
            "data_end_time": iso_ms(full_last_candle_ms),
            "processed_start_time": iso_ms(int(rows[start_idx][0])) if rows and 0 <= start_idx < len(rows) else None,
            "processed_end_time": iso_ms(int(rows[-1][0])) if rows else None,
            "loaded_candles": len(rows),
            "raw_candles": raw_candle_count,
        })
        return BacktestReport(config=config_out, metrics=metrics, trades=[asdict(t) for t in trades], logs=logs[-500:])

    def _resolve_tp_sl(self, entry: float, side: str, lev: int, cfg: BacktestConfig, d: Dict[str, Any]) -> tuple[float, float]:
        tp_raw = d.get("take_profit") or d.get("tp")
        sl_raw = d.get("stop_loss") or d.get("sl")
        tp = _float(tp_raw, 0)
        sl = _float(sl_raw, 0)
        if tp and sl:
            return tp, sl

        tp_pct = _float(d.get("take_profit_pct"), cfg.default_take_profit_pct)
        sl_pct = _float(d.get("stop_loss_pct"), cfg.default_stop_loss_pct)
        mode = str(d.get("tp_sl_mode") or d.get("tp_sl_pct_mode") or "pnl_percent").lower()
        # Futures bot in V54 treats default TP/SL percent as PNL% on margin.
        if cfg.category in {"linear", "inverse"} and ("pnl" in mode or not mode):
            tp_price_pct = (tp_pct / 100.0) / max(1, lev)
            sl_price_pct = (sl_pct / 100.0) / max(1, lev)
        else:
            tp_price_pct = tp_pct / 100.0
            sl_price_pct = sl_pct / 100.0
        if side == "LONG":
            return entry * (1 + tp_price_pct), entry * (1 - sl_price_pct)
        return entry * (1 - tp_price_pct), entry * (1 + sl_price_pct)

    def _valid_tp_sl(self, side: str, entry: float, tp: float, sl: float) -> bool:
        if side == "LONG":
            return tp > entry and sl < entry
        if side == "SHORT":
            return tp < entry and sl > entry
        return False

    def _slip(self, price: float, side: str, slippage: float, *, entry: bool) -> float:
        if side == "LONG":
            return price * (1 + slippage) if entry else price * (1 - slippage)
        return price * (1 - slippage) if entry else price * (1 + slippage)

    def _check_exit(self, pos: Position, row: List[Any], cfg: BacktestConfig) -> Optional[BacktestTrade]:
        high = _float(row[2])
        low = _float(row[3])
        if pos.side == "LONG":
            # Conservative assumption: if both TP and SL are touched inside one candle, count SL first.
            if low <= pos.stop_loss:
                return self._close_position(pos, row, self._slip(pos.stop_loss, pos.side, cfg.slippage_rate, entry=False), cfg, "SL")
            if high >= pos.take_profit:
                return self._close_position(pos, row, self._slip(pos.take_profit, pos.side, cfg.slippage_rate, entry=False), cfg, "TP")
        else:
            if high >= pos.stop_loss:
                return self._close_position(pos, row, self._slip(pos.stop_loss, pos.side, cfg.slippage_rate, entry=False), cfg, "SL")
            if low <= pos.take_profit:
                return self._close_position(pos, row, self._slip(pos.take_profit, pos.side, cfg.slippage_rate, entry=False), cfg, "TP")
        return None

    def _close_position(self, pos: Position, row: List[Any], exit_price: float, cfg: BacktestConfig, reason: str) -> BacktestTrade:
        if pos.side == "LONG":
            gross = (exit_price - pos.entry_price) * pos.qty
        else:
            gross = (pos.entry_price - exit_price) * pos.qty
        notional_entry = pos.entry_price * pos.qty
        notional_exit = exit_price * pos.qty
        fee = (notional_entry + notional_exit) * float(cfg.fee_rate)
        net = gross - fee
        result = "WIN" if net > 0 else "LOSS" if net < 0 else "BE"
        return BacktestTrade(
            symbol=cfg.symbol,
            category=cfg.category,
            side=pos.side,
            entry_time=iso_ms(pos.entry_time_ms),
            exit_time=iso_ms(int(row[0])),
            entry_price=round(pos.entry_price, 6),
            exit_price=round(exit_price, 6),
            qty=round(pos.qty, 8),
            margin=round(pos.margin, 4),
            leverage=pos.leverage,
            gross_pnl=round(gross, 6),
            fee=round(fee, 6),
            net_pnl=round(net, 6),
            net_pnl_pct_on_margin=round((net / pos.margin) * 100, 4) if pos.margin else 0,
            net_pnl_pct_on_capital=round((net / cfg.initial_capital) * 100, 4) if cfg.initial_capital else 0,
            result=result,
            entry_reason=pos.entry_reason,
            exit_reason=reason,
        )

    def _metrics(self, cfg: BacktestConfig, trades: List[BacktestTrade], final_equity: float, max_dd: float, ai_calls: int, waits: int, blocked: int) -> Dict[str, Any]:
        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl < 0]
        pnl = final_equity - float(cfg.initial_capital)
        gross_win = sum(t.net_pnl for t in wins)
        gross_loss = abs(sum(t.net_pnl for t in losses))
        pf = gross_win / gross_loss if gross_loss > 0 else None
        return {
            "initial_capital": round(float(cfg.initial_capital), 4),
            "final_equity": round(final_equity, 4),
            "pnl_usdt": round(pnl, 6),
            "pnl_pct": round((pnl / float(cfg.initial_capital)) * 100, 4) if cfg.initial_capital else 0,
            "total_trades": len(trades),
            "win_trades": len(wins),
            "loss_trades": len(losses),
            "breakeven_trades": len([t for t in trades if t.net_pnl == 0]),
            "winrate": round((len(wins) / len(trades)) * 100, 2) if trades else 0,
            "gross_win_usdt": round(gross_win, 6),
            "gross_loss_usdt": round(gross_loss, 6),
            "avg_win_usdt": round(gross_win / len(wins), 6) if wins else 0,
            "avg_loss_usdt": round(-gross_loss / len(losses), 6) if losses else 0,
            "profit_factor": round(pf, 4) if pf is not None else None,
            "max_drawdown_pct": round(max_dd * 100, 4),
            "ai_calls": ai_calls,
            "waits": waits,
            "blocked": blocked,
            "wait_count": waits,
            "blocked_count": blocked,
        }

# -----------------------------
# V60 Generic Indicator Engine
# -----------------------------
# This section lets a direct JSON backtest plan define arbitrary indicator names
# and conditions instead of being forced through the old RSI parser.

import ast
import operator


def _plan_is_generic_condition_strategy(plan: Dict[str, Any]) -> bool:
    if not isinstance(plan, dict):
        return False
    st = str(plan.get("strategy_type") or plan.get("strategy_name") or "").lower()
    if st == "bollinger_vwap_mean_reversion":
        return False
    if st in {"generic_conditions", "generic_condition_engine", "custom_indicator_conditions"}:
        return True
    if isinstance(plan.get("raw_strategy_json"), dict):
        raw = plan.get("raw_strategy_json") or {}
        if isinstance(raw.get("entry_rules"), dict) or isinstance(raw.get("long_rule"), dict) or isinstance(raw.get("short_rule"), dict):
            return True
    # Direct compact plans may define these sections themselves.
    return isinstance(plan.get("entry_rules"), dict) or isinstance(plan.get("long_rule"), dict) or isinstance(plan.get("short_rule"), dict)


def _rows_from_snapshot_for_generic(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    pack = _first_timeframe_pack(snapshot)
    klines = pack.get("klines") or {}
    source = klines.get("recent_klines_full") or klines.get("recent_klines") or klines.get("recent_candles") or []
    out: List[Dict[str, Any]] = []
    for c in source:
        if not isinstance(c, dict):
            continue
        row = {
            "t": int(c.get("t") or 0),
            "o": _as_float(c.get("o"), None),
            "h": _as_float(c.get("h"), None),
            "l": _as_float(c.get("l"), None),
            "c": _as_float(c.get("c"), None),
            "v": _as_float(c.get("v"), None),
            "color": str(c.get("color") or "doji").lower(),
        }
        if row["t"] and None not in {row["o"], row["h"], row["l"], row["c"], row["v"]}:
            out.append(row)
    out.sort(key=lambda x: x["t"])
    return out


def _ema_series(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    e = sum(values[:period]) / period
    out[period - 1] = e
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def _sma_series(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0:
        return out
    rolling = 0.0
    for i, v in enumerate(values):
        rolling += v
        if i >= period:
            rolling -= values[i - period]
        if i >= period - 1:
            out[i] = rolling / period
    return out


def _wma_series(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    weights = list(range(1, period + 1))
    denom = float(sum(weights))
    for i in range(period - 1, len(values)):
        chunk = values[i - period + 1:i + 1]
        out[i] = sum(v * w for v, w in zip(chunk, weights)) / denom
    return out


def _rsi_series(values: List[float], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) <= period:
        return out
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
    for idx in range(period + 1, len(values)):
        g = gains[idx - 1]
        l = losses[idx - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[idx] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
    return out


def _atr_series(rows: List[Dict[str, Any]], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(rows)
    if len(rows) <= period:
        return out
    trs: List[float] = []
    for i in range(1, len(rows)):
        high = float(rows[i]["h"]); low = float(rows[i]["l"]); prev_close = float(rows[i-1]["c"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr_v = sum(trs[:period]) / period
    out[period] = atr_v
    for i in range(period + 1, len(rows)):
        atr_v = (atr_v * (period - 1) + trs[i - 1]) / period
        out[i] = atr_v
    return out


def _adx_series(rows: List[Dict[str, Any]], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(rows)
    if len(rows) < period * 2 + 1:
        return out
    trs: List[float] = []
    plus_dm: List[float] = []
    minus_dm: List[float] = []
    for i in range(1, len(rows)):
        high = float(rows[i]["h"]); low = float(rows[i]["l"])
        prev_high = float(rows[i-1]["h"]); prev_low = float(rows[i-1]["l"]); prev_close = float(rows[i-1]["c"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        up = high - prev_high
        down = prev_low - low
        trs.append(tr)
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    atr_v = sum(trs[:period]) / period
    p = sum(plus_dm[:period]) / period
    m = sum(minus_dm[:period]) / period
    dxs: List[tuple[int, float]] = []
    for j in range(period, len(trs)):
        atr_v = ((atr_v * (period - 1)) + trs[j]) / period
        p = ((p * (period - 1)) + plus_dm[j]) / period
        m = ((m * (period - 1)) + minus_dm[j]) / period
        if atr_v <= 0:
            continue
        pdi = 100.0 * p / atr_v
        mdi = 100.0 * m / atr_v
        denom = pdi + mdi
        if denom > 0:
            # row index is j+1 because trs starts at row 1
            dxs.append((j + 1, 100.0 * abs(pdi - mdi) / denom))
    if len(dxs) < period:
        return out
    adx_v = sum(x[1] for x in dxs[:period]) / period
    out[dxs[period-1][0]] = adx_v
    for row_idx, dx in dxs[period:]:
        adx_v = ((adx_v * (period - 1)) + dx) / period
        out[row_idx] = adx_v
    return out


def _vwap_series(rows: List[Dict[str, Any]], period: int = 96) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(rows)
    for i in range(len(rows)):
        start = max(0, i - period + 1)
        pv = 0.0; vol = 0.0
        for r in rows[start:i+1]:
            typical = (float(r["h"]) + float(r["l"]) + float(r["c"])) / 3.0
            v = float(r["v"])
            pv += typical * v
            vol += v
        out[i] = pv / vol if vol > 0 else None
    return out


def _roc_series(values: List[float], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    for i in range(period, len(values)):
        base = values[i-period]
        out[i] = ((values[i] - base) / base * 100.0) if base else None
    return out


def _stoch_series(rows: List[Dict[str, Any]], k_period: int = 14, d_period: int = 3) -> tuple[List[Optional[float]], List[Optional[float]]]:
    k: List[Optional[float]] = [None] * len(rows)
    for i in range(k_period - 1, len(rows)):
        chunk = rows[i-k_period+1:i+1]
        lo = min(float(r["l"]) for r in chunk)
        hi = max(float(r["h"]) for r in chunk)
        close = float(rows[i]["c"])
        k[i] = 0.0 if hi == lo else 100.0 * (close - lo) / (hi - lo)
    d: List[Optional[float]] = [None] * len(rows)
    k_vals = [x if x is not None else math.nan for x in k]
    for i in range(len(rows)):
        if i >= d_period - 1:
            chunk = k_vals[i-d_period+1:i+1]
            if all(not math.isnan(x) for x in chunk):
                d[i] = sum(chunk) / d_period
    return k, d


def _cci_series(rows: List[Dict[str, Any]], period: int = 20) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(rows)
    tps = [(float(r["h"]) + float(r["l"]) + float(r["c"])) / 3.0 for r in rows]
    for i in range(period - 1, len(rows)):
        chunk = tps[i-period+1:i+1]
        ma = sum(chunk) / period
        md = sum(abs(x - ma) for x in chunk) / period
        out[i] = None if md == 0 else (tps[i] - ma) / (0.015 * md)
    return out


def _mfi_series(rows: List[Dict[str, Any]], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(rows)
    if len(rows) <= period:
        return out
    tp = [(float(r["h"]) + float(r["l"]) + float(r["c"])) / 3.0 for r in rows]
    mf = [tp[i] * float(rows[i]["v"]) for i in range(len(rows))]
    for i in range(period, len(rows)):
        pos = 0.0; neg = 0.0
        for j in range(i-period+1, i+1):
            if tp[j] > tp[j-1]:
                pos += mf[j]
            elif tp[j] < tp[j-1]:
                neg += mf[j]
        out[i] = 100.0 if neg == 0 else 100.0 - (100.0 / (1.0 + pos / neg))
    return out


def _macd_values(closes: List[float]) -> Dict[str, List[Optional[float]]]:
    e12 = _ema_series(closes, 12)
    e26 = _ema_series(closes, 26)
    line: List[Optional[float]] = [None] * len(closes)
    for i, (a, b) in enumerate(zip(e12, e26)):
        if a is not None and b is not None:
            line[i] = a - b
    line_filled = [x for x in line if x is not None]
    signal_short = _ema_series(line_filled, 9) if line_filled else []
    signal: List[Optional[float]] = [None] * len(closes)
    idxs = [i for i, x in enumerate(line) if x is not None]
    for n, idx in enumerate(idxs):
        if n < len(signal_short):
            signal[idx] = signal_short[n]
    hist: List[Optional[float]] = [None] * len(closes)
    for i in range(len(closes)):
        if line[i] is not None and signal[i] is not None:
            hist[i] = line[i] - signal[i]
    return {"macd": line, "macd_signal": signal, "macd_histogram": hist}


def _get_indicator_specs(plan: Dict[str, Any]) -> Dict[str, Any]:
    raw = plan.get("raw_strategy_json") if isinstance(plan.get("raw_strategy_json"), dict) else plan
    specs = raw.get("indicators") if isinstance(raw.get("indicators"), dict) else {}
    return specs


def _build_generic_context(rows: List[Dict[str, Any]], plan: Dict[str, Any]) -> Dict[str, Any]:
    closes = [float(r["c"]) for r in rows]
    highs = [float(r["h"]) for r in rows]
    lows = [float(r["l"]) for r in rows]
    volumes = [float(r["v"]) for r in rows]
    idx = len(rows) - 1
    cur = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else rows[-1]
    ctx: Dict[str, Any] = {
        "open": float(cur["o"]), "high": float(cur["h"]), "low": float(cur["l"]), "close": float(cur["c"]), "price": float(cur["c"]),
        "volume": float(cur["v"]), "volume_current": float(cur["v"]), "color": cur.get("color"),
        "previous_open": float(prev["o"]), "previous_high": float(prev["h"]), "previous_low": float(prev["l"]), "previous_close": float(prev["c"]), "previous_volume": float(prev["v"]),
        "body": abs(float(cur["c"]) - float(cur["o"])),
        "upper_wick": float(cur["h"]) - max(float(cur["o"]), float(cur["c"])),
        "lower_wick": min(float(cur["o"]), float(cur["c"])) - float(cur["l"]),
        "is_green": cur.get("color") == "green", "is_red": cur.get("color") == "red", "is_doji": cur.get("color") == "doji",
    }
    series: Dict[str, List[Optional[float]]] = {}
    # Default common indicators regardless of JSON so user conditions can use them directly.
    for p in [5, 9, 10, 20, 21, 34, 50, 100, 200]:
        series[f"sma{p}"] = _sma_series(closes, p)
        series[f"ema{p}"] = _ema_series(closes, p)
    series["rsi14"] = _rsi_series(closes, 14)
    series["atr14"] = _atr_series(rows, 14)
    series["adx14"] = _adx_series(rows, 14)
    series["vwap"] = _vwap_series(rows, 96)
    series["volume_ma20"] = _sma_series(volumes, 20)
    macd_map = _macd_values(closes)
    series.update(macd_map)
    k, d = _stoch_series(rows, 14, 3)
    series["stoch_k"] = k; series["stoch_d"] = d
    series["cci20"] = _cci_series(rows, 20)
    series["roc14"] = _roc_series(closes, 14)
    series["mfi14"] = _mfi_series(rows, 14)
    # Bollinger defaults
    mid = _sma_series(closes, 20)
    up: List[Optional[float]] = [None] * len(rows); lo: List[Optional[float]] = [None] * len(rows); width: List[Optional[float]] = [None] * len(rows)
    for i in range(19, len(rows)):
        chunk = closes[i-19:i+1]
        m = mid[i]
        if m is None:
            continue
        sd = (sum((x - m) ** 2 for x in chunk) / 20) ** 0.5
        up[i] = m + 2 * sd; lo[i] = m - 2 * sd; width[i] = ((up[i] - lo[i]) / m * 100.0) if m else None
    series["bb_upper"] = up; series["bb_middle"] = mid; series["bb_lower"] = lo; series["bb_width"] = width

    # User-defined indicators with aliases.
    specs = _get_indicator_specs(plan)
    for alias, spec in specs.items():
        if not isinstance(spec, dict):
            continue
        key = str(alias).lower().replace(" ", "_")
        typ = str(spec.get("type") or alias).lower()
        period = int(_as_float(spec.get("period"), 14) or 14)
        if typ in {"ema", "ema_fast", "ema_mid", "ema_slow", "ema_trend"}:
            series[key] = _ema_series(closes, period)
            series[f"ema{period}"] = series[key]
        elif typ == "sma":
            series[key] = _sma_series(closes, period); series[f"sma{period}"] = series[key]
        elif typ == "wma":
            series[key] = _wma_series(closes, period); series[f"wma{period}"] = series[key]
        elif typ == "rsi":
            series[key] = _rsi_series(closes, period); series[f"rsi{period}"] = series[key]
        elif typ == "atr":
            series[key] = _atr_series(rows, period); series[f"atr{period}"] = series[key]
        elif typ == "adx":
            series[key] = _adx_series(rows, period); series[f"adx{period}"] = series[key]
        elif typ == "vwap":
            series[key] = _vwap_series(rows, int(_as_float(spec.get("period"), 96) or 96))
        elif typ in {"volume_ma", "volume_sma"}:
            series[key] = _sma_series(volumes, period); series[f"volume_ma{period}"] = series[key]
        elif typ in {"roc", "momentum"}:
            series[key] = _roc_series(closes, period); series[f"roc{period}"] = series[key]
        elif typ == "cci":
            series[key] = _cci_series(rows, period); series[f"cci{period}"] = series[key]
        elif typ == "mfi":
            series[key] = _mfi_series(rows, period); series[f"mfi{period}"] = series[key]
        elif typ in {"bollinger", "bollinger_bands", "bb"}:
            p = int(_as_float(spec.get("period"), 20) or 20); stddev = float(_as_float(spec.get("stddev"), 2) or 2)
            mseries = _sma_series(closes, p)
            upper = [None] * len(rows); lower = [None] * len(rows); bwidth = [None] * len(rows)
            for i in range(p - 1, len(rows)):
                chunk = closes[i-p+1:i+1]; m = mseries[i]
                if m is None: continue
                sd = (sum((x - m) ** 2 for x in chunk) / p) ** 0.5
                upper[i] = m + stddev * sd; lower[i] = m - stddev * sd; bwidth[i] = ((upper[i]-lower[i]) / m * 100.0) if m else None
            prefix = key if key not in {"bollinger_bands", "bollinger", "bb"} else "bb"
            series[f"{prefix}_upper"] = upper; series[f"{prefix}_middle"] = mseries; series[f"{prefix}_lower"] = lower; series[f"{prefix}_width"] = bwidth

    for name, vals in series.items():
        if vals and vals[idx] is not None:
            ctx[name] = vals[idx]
        if len(vals) >= 2 and vals[idx-1] is not None:
            ctx[f"{name}_prev"] = vals[idx-1]
        if vals and vals[idx] is not None and len(vals) >= 2 and vals[idx-1] is not None:
            ctx[f"{name}_slope"] = vals[idx] - vals[idx-1]

    if ctx.get("atr14") and ctx.get("close"):
        ctx["atr14_div_price"] = ctx["atr14"] / ctx["close"]
        ctx["atr14_pct"] = ctx["atr14"] / ctx["close"] * 100.0
    for n in [20, 50, 100, 200]:
        key = f"ema{n}"
        if ctx.get(key) and ctx.get("close"):
            ctx[f"distance_from_close_to_{key}_percent"] = abs(ctx["close"] - ctx[key]) / ctx["close"] * 100.0
    return ctx


def _safe_eval_expr(expr: str, ctx: Dict[str, Any]) -> Optional[float]:
    expr = str(expr).strip()
    expr = re.sub(r"\bEMA(\d+)\b", r"ema\1", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\bSMA(\d+)\b", r"sma\1", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\bRSI(\d+)\b", r"rsi\1", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\bATR(\d+)\b", r"atr\1", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\bADX(\d+)\b", r"adx\1", expr, flags=re.IGNORECASE)
    expr = expr.replace("%", "")
    allowed_nodes = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Num, ast.Constant, ast.Name, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.USub, ast.UAdd, ast.Mod)
    try:
        tree = ast.parse(expr, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, allowed_nodes):
                return None
            if isinstance(node, ast.Name) and node.id not in ctx:
                return None
        val = eval(compile(tree, "<condition>", "eval"), {"__builtins__": {}}, ctx)
        return float(val) if val is not None else None
    except Exception:
        return _as_float(ctx.get(expr.lower()), None)


def _compare_values(left: float, op: str, right: float) -> bool:
    if op in {">", "gt"}: return left > right
    if op in {">=", "gte", "ge"}: return left >= right
    if op in {"<", "lt"}: return left < right
    if op in {"<=", "lte", "le"}: return left <= right
    if op in {"==", "=", "eq"}: return abs(left - right) < 1e-12
    if op in {"!=", "ne"}: return abs(left - right) >= 1e-12
    return False


def _condition_dict_ok(cond: Dict[str, Any], ctx: Dict[str, Any]) -> tuple[bool, str]:
    left = cond.get("left") or cond.get("indicator") or cond.get("name")
    op = str(cond.get("op") or cond.get("operator") or ">").lower().strip()
    right = cond.get("right") if "right" in cond else cond.get("value")
    if not left:
        return True, "empty condition ignored"
    if op in {"is_green", "green"}:
        return bool(ctx.get("is_green")), f"{left} is green"
    if op in {"is_red", "red"}:
        return bool(ctx.get("is_red")), f"{left} is red"
    lval = _safe_eval_expr(str(left), ctx)
    rval = _safe_eval_expr(str(right), ctx) if isinstance(right, str) else _as_float(right, None)
    if lval is None or rval is None:
        return False, f"missing value for {left} {op} {right}"
    return _compare_values(lval, op, rval), f"{left} {op} {right} ({lval:.4g} vs {rval:.4g})"


def _condition_string_ok(text: str, ctx: Dict[str, Any], rows: List[Dict[str, Any]]) -> tuple[bool, str]:
    raw = str(text or "").strip()
    low = raw.lower()
    if not raw:
        return True, "empty condition"
    if "current candle is green" in low or low == "green" or "nen xanh" in low:
        return bool(ctx.get("is_green")), raw
    if "current candle is red" in low or low == "red" or "nen do" in low or "nến đỏ" in low:
        return bool(ctx.get("is_red")), raw
    if "current candle is not doji" in low or "not doji" in low or "không phải doji" in low:
        return not bool(ctx.get("is_doji")), raw
    if "current close > previous close" in low or "close > previous close" in low:
        return ctx.get("close", 0) > ctx.get("previous_close", 0), raw
    if "current close < previous close" in low or "close < previous close" in low:
        return ctx.get("close", 0) < ctx.get("previous_close", 0), raw
    m_slope = re.search(r"(ema|sma|wma)(\d+)\s+slope\s+is\s+(up|down|flat_up|flat_down)", low)
    if m_slope:
        key = f"{m_slope.group(1)}{m_slope.group(2)}_slope"
        val = _as_float(ctx.get(key), None)
        if val is None:
            return False, f"missing {key}"
        direction = m_slope.group(3)
        if direction == "up":
            return val > 0, raw
        if direction == "down":
            return val < 0, raw
        if direction == "flat_up":
            return val >= 0, raw
        if direction == "flat_down":
            return val <= 0, raw
    m = re.search(r"at least\s+(\d+)\s+of\s+last\s+(\d+)\s+candles\s+closed\s+(above|below)\s+([A-Za-z0-9_]+)", low)
    if m:
        need, total, direction, ref = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
        val = _safe_eval_expr(ref, ctx)
        if val is None or len(rows) < total:
            return False, raw
        count = sum(1 for r in rows[-total:] if (float(r["c"]) > val if direction == "above" else float(r["c"]) < val))
        return count >= need, f"{raw} ({count}/{total})"
    # Generic binary comparator. Normalize common natural aliases first.
    raw_cmp = re.sub(r"\bcurrent\s+volume\b", "volume_current", raw, flags=re.IGNORECASE)
    raw_cmp = re.sub(r"\bcurrent\s+close\b", "close", raw_cmp, flags=re.IGNORECASE)
    raw_cmp = re.sub(r"\bprevious\s+close\b", "previous_close", raw_cmp, flags=re.IGNORECASE)
    raw_cmp = re.sub(r"\bcandle\s+body\b", "body", raw_cmp, flags=re.IGNORECASE)
    m = re.search(r"(.+?)\s*(>=|<=|==|!=|>|<)\s*(.+)", raw_cmp)
    if m:
        left, op, right = m.group(1).strip(), m.group(2), m.group(3).strip()
        lval = _safe_eval_expr(left, ctx)
        rval = _safe_eval_expr(right, ctx)
        if lval is None or rval is None:
            return False, f"missing value for {raw}"
        return _compare_values(lval, op, rval), f"{raw} ({lval:.4g} {op} {rval:.4g})"
    # Unsupported natural-language condition should not pass silently.
    return False, f"unsupported condition: {raw}"


def _rule_section(plan: Dict[str, Any], side: str) -> Dict[str, Any]:
    raw = plan.get("raw_strategy_json") if isinstance(plan.get("raw_strategy_json"), dict) else plan
    key = "long" if side == "LONG" else "short"
    for k in (f"{key}_rule", key):
        if isinstance(raw.get(k), dict):
            return raw[k]
    er = raw.get("entry_rules") if isinstance(raw.get("entry_rules"), dict) else {}
    if isinstance(er.get(key), dict):
        return er[key]
    return {}


def _conditions_for_section(section: Dict[str, Any]) -> List[Any]:
    for key in ("conditions", "all", "all_required", "entry_conditions_all_required", "required"):
        val = section.get(key)
        if isinstance(val, list):
            return val
    return []


def _section_enabled(section: Dict[str, Any], default: bool = True) -> bool:
    if not section:
        return False
    if section.get("enabled") is False:
        return False
    return default


def _eval_conditions(conditions: List[Any], ctx: Dict[str, Any], rows: List[Dict[str, Any]]) -> tuple[bool, str]:
    details: List[str] = []
    for cond in conditions:
        if isinstance(cond, dict):
            ok, why = _condition_dict_ok(cond, ctx)
        else:
            ok, why = _condition_string_ok(str(cond), ctx, rows)
        if not ok:
            return False, why
        details.append(why)
    return True, "; ".join(details[:4])


def _generic_condition_decide(evaluator: Any, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    rows = _rows_from_snapshot_for_generic(snapshot)
    if len(rows) < 30:
        return {"action": "WAIT", "symbol": evaluator.symbol, "category": evaluator.cfg.category, "confidence": 0, "reason": "Generic: chưa đủ nến để tính indicator."}
    latest_t = int(rows[-1]["t"])
    if not evaluator._cooldown_ok(latest_t):
        return {"action": "WAIT", "symbol": evaluator.symbol, "category": evaluator.cfg.category, "confidence": 0, "reason": "Generic cooldown: chưa đủ số nến nghỉ."}
    ctx = _build_generic_context(rows, evaluator.plan)
    long_sec = _rule_section(evaluator.plan, "LONG")
    short_sec = _rule_section(evaluator.plan, "SHORT")
    global_sec = (evaluator.plan.get("raw_strategy_json") if isinstance(evaluator.plan.get("raw_strategy_json"), dict) else evaluator.plan).get("global_filters")
    if isinstance(global_sec, dict):
        global_conditions = global_sec.get("required") if isinstance(global_sec.get("required"), list) else []
        ok, why = _eval_conditions(global_conditions, ctx, rows)
        if not ok:
            return {"action": "WAIT", "symbol": evaluator.symbol, "category": evaluator.cfg.category, "confidence": 0, "reason": "Generic global filter: " + why}
    candidates: List[tuple[str, str]] = []
    if _section_enabled(long_sec):
        ok, why = _eval_conditions(_conditions_for_section(long_sec), ctx, rows)
        if ok:
            candidates.append(("OPEN_LONG", why))
    if _section_enabled(short_sec):
        ok, why = _eval_conditions(_conditions_for_section(short_sec), ctx, rows)
        if ok:
            candidates.append(("OPEN_SHORT", why))
    if len(candidates) == 1:
        action, why = candidates[0]
        evaluator.state["last_processed_candle_time"] = latest_t
        return evaluator._base(action, f"Generic indicators: {action.replace('OPEN_', '')} matched. {why}")
    if len(candidates) > 1:
        return {"action": "WAIT", "symbol": evaluator.symbol, "category": evaluator.cfg.category, "confidence": 0, "reason": "Generic WAIT: cả LONG và SHORT cùng đúng, tín hiệu mâu thuẫn."}
    return {"action": "WAIT", "symbol": evaluator.symbol, "category": evaluator.cfg.category, "confidence": 0, "reason": "Generic WAIT: chưa đủ điều kiện indicator."}
