import json
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional

try:
    from openai import AsyncOpenAI
except Exception:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore


class DecisionEngine:
    """Turns saved strategy prompt or direct user command into strict JSON actions."""

    def __init__(self, *, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")).strip()
        self.model = (model if model is not None else os.getenv("OPENAI_MODEL", "gpt-5.5")).strip()
        self.client = AsyncOpenAI(api_key=self.api_key) if (AsyncOpenAI and self.api_key) else None

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def decide(self, *, prompt: str, snapshot: Dict[str, Any], risk_config: Dict[str, Any], skill_context: str = "", prompt_directives: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.enabled:
            return {"action": "WAIT", "reason": "OPENAI_API_KEY chưa cấu hình nên bot không tự ra quyết định."}
        user = {"mode": "strategy_loop", "user_trading_prompt": prompt, "prompt_directives": prompt_directives or {}, "risk_config": risk_config, "market_snapshot": snapshot}
        return await self._json_call(_decision_system_prompt(skill_context), user)

    async def command_to_action(self, *, command: str, snapshot: Dict[str, Any], risk_config: Dict[str, Any], skill_context: str = "") -> Dict[str, Any]:
        if not self.enabled:
            return {"action": "WAIT", "reason": "OPENAI_API_KEY chưa cấu hình nên không thể phân tích lệnh chat."}
        user = {"mode": "manual_direct_command", "direct_user_command": command, "risk_config": risk_config, "market_snapshot": snapshot}
        return await self._json_call(_command_system_prompt(skill_context), user)

    async def _json_call(self, system: str, user_payload: Dict[str, Any]) -> Dict[str, Any]:
        assert self.client is not None

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]

        base_kwargs = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }

        # Newer OpenAI models reject the legacy `max_tokens` parameter.
        # Use `max_completion_tokens` first, then fallback for older compatible models.
        response = await self._create_chat_completion(base_kwargs, max_output_tokens=650)
        content = response.choices[0].message.content or "{}"
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return {"action": "WAIT", "reason": f"AI trả JSON lỗi: {content[:200]}"}
        if not isinstance(data, dict):
            return {"action": "WAIT", "reason": "AI không trả về object JSON."}
        return data

    async def _create_chat_completion(self, base_kwargs: Dict[str, Any], *, max_output_tokens: int):
        assert self.client is not None

        # Some newer reasoning/chat models only accept the default temperature=1
        # and reject custom values such as 0.05. Keep the safest path first:
        # omit temperature entirely, then fallback across token parameter names.
        attempts = [
            {**base_kwargs, "max_completion_tokens": max_output_tokens},
            {**base_kwargs, "max_tokens": max_output_tokens},
            {**base_kwargs, "temperature": 0.05, "max_completion_tokens": max_output_tokens},
            {**base_kwargs, "temperature": 0.05, "max_tokens": max_output_tokens},
        ]

        last_error: Optional[Exception] = None
        for kwargs in attempts:
            try:
                return await self.client.chat.completions.create(**kwargs)
            except Exception as exc:  # OpenAI SDK raises BadRequestError for unsupported params.
                last_error = exc
                msg = str(exc).lower()
                recoverable_openai_param_error = (
                    "unsupported parameter" in msg
                    or "unsupported value" in msg
                    or "does not support" in msg
                    or "not supported" in msg
                    or "unknown parameter" in msg
                    or "unrecognized request argument" in msg
                )
                if recoverable_openai_param_error:
                    continue
                raise
        assert last_error is not None
        raise last_error


def _allowed_shape() -> str:
    return """
Allowed JSON shape:
{
  "action": "WAIT" | "OPEN_LONG" | "OPEN_SHORT" | "CLOSE_LONG" | "CLOSE_SHORT" | "CLOSE_ALL" | "SPOT_BUY" | "SPOT_SELL" | "SPOT_SELL_ALL",
  "symbol": "BTCUSDT",
  "category": "spot" | "linear" | "inverse",
  "leverage": 1,
  "margin_usdt": 10,
  "order_usdt": 10,
  "qty": "0.001",
  "take_profit": 70000,
  "stop_loss": 65000,
  "take_profit_pct": 3,
  "stop_loss_pct": 1,
  "reason": "short explanation"
}
""".strip()


def _shared_rules() -> str:
    return """
Market selection rules:
- If the user says spot, giao ngay, mua coin nắm giữ, or buy without leverage/short intent: use SPOT_BUY with category="spot".
- If the user says futures, future, hợp đồng, perpetual, long with leverage, short, đòn bẩy/x/leverage: use linear futures with OPEN_LONG/OPEN_SHORT and category="linear" unless the user explicitly says inverse.
- Spot has no short. Never return OPEN_SHORT with category="spot".
- For spot buy, use order_usdt or margin_usdt as the USDT amount to buy. Spot does not use leverage; even if the user mentions leverage with spot, omit leverage or set it to 1 and never convert a clear spot order into futures.
- For spot sell, use qty if user gives coin quantity; use order_usdt if user gives USDT value; use SPOT_SELL_ALL if user says sell all/bán hết.
- For futures, margin_usdt means account margin/capital used for the trade, not notional. If the direct command requests futures/long/short but does not specify leverage, use risk_config.max_leverage as the default leverage.
- For OPEN_LONG: stop_loss must be below current price and take_profit above current price.
- For OPEN_SHORT: stop_loss must be above current price and take_profit below current price.
- For SPOT_BUY: stop_loss must be below current price and take_profit above current price.
- If the user gives TP/SL as percentages, return take_profit_pct and stop_loss_pct instead of absolute prices. If TP/SL are missing, the bot may apply default TP/SL percentages from risk_config.
- Only use symbols listed in risk_config.allowed_symbols.
- Do not exceed risk_config limits.
- Never request withdrawal, transfer, API-key changes, or account movement.
- Strategy loop may return WAIT if the instruction is ambiguous or unsafe.
""".strip()


def _skill_block(skill_context: str) -> str:
    if not skill_context:
        return ""
    return "\nBYBIT SKILL CONTEXT - local/auto-updated cache:\n" + skill_context[:9000] + "\nEND BYBIT SKILL CONTEXT.\n"


def _decision_system_prompt(skill_context: str = "") -> str:
    return f"""
You are a trading decision engine for a Bybit execution bot.
Return ONLY valid JSON. No markdown. No commentary outside JSON.

{_allowed_shape()}

Hard rules:
{_shared_rules()}
{_skill_block(skill_context)}
- Use the provided snapshots. Snapshot keys are formatted like "linear:BTCUSDT" and "spot:BTCUSDT".
- Respect the strategy prompt exactly, including recurring schedule, market type, symbols, leverage, TP/SL, timeframe, and indicator rules such as RSI / EMA / MACD / volume conditions.
- If prompt_directives are provided, treat them as extracted constraints from the prompt and keep them aligned with the raw prompt text.
- Recurring/DCA cadence such as every 1 hour, mỗi 1 tiếng, 10 USDT/1h is enforced by the bot scheduler outside the AI. When invoked, assume the scheduler has decided this is an eligible execution window; do not ignore a clear DCA buy solely because it contains a time interval.
- Prefer indicators_15m: trend, EMA, RSI, MACD, ATR, volume_status.
- If the strategy prompt does not clearly authorize a new trade, return WAIT.
- Do not invent market data. Use only the provided snapshot.
""".strip()


def _command_system_prompt(skill_context: str = "") -> str:
    return f"""
You convert a user's direct natural-language trading command into one execution JSON for a Bybit bot.
Return ONLY valid JSON. No markdown. No commentary outside JSON.

{_allowed_shape()}

Hard rules for direct commands:
{_shared_rules()}
{_skill_block(skill_context)}
- Treat the user command as an execution instruction, not as market advice.
- Execute only the command's explicit intent.
- Do NOT use WAIT as a normal "stand outside / no trade" decision for direct commands.
- If the user gives a clear symbol + side + size, return the corresponding executable action.
- Only if the command is truly missing symbol, side, or size/order amount, return WAIT with a concise reason explaining exactly what is missing.
- Examples:
  "spot mua BTC 20u TP 3% SL 1%" -> SPOT_BUY BTCUSDT category spot order_usdt 20
  "mua spot ETH 50u" -> SPOT_BUY ETHUSDT category spot order_usdt 50
  "bán hết BTC spot" -> SPOT_SELL_ALL BTCUSDT category spot
  "future long BTC 10u đòn 3 TP 70000 SL 65000" -> OPEN_LONG BTCUSDT category linear leverage 3 margin_usdt 10
  "short eth 15 đô x2 tp 2800 sl 3100" -> OPEN_SHORT ETHUSDT category linear leverage 2 margin_usdt 15
  "đóng hết BTC future" -> CLOSE_ALL BTCUSDT category linear
""".strip()


def compact_kline_summary(klines: List[List[Any]]) -> Dict[str, Any]:
    """Convert Bybit reverse-chronological klines into a compact indicator-free summary."""
    rows = []
    for row in klines[:50]:
        try:
            rows.append({"t": int(row[0]), "o": str(row[1]), "h": str(row[2]), "l": str(row[3]), "c": str(row[4]), "v": str(row[5])})
        except Exception:
            continue
    closes = [Decimal(r["c"]) for r in rows if r.get("c")]
    if not closes:
        return {"recent_klines": rows}
    last = closes[0]
    oldest = closes[-1]
    change_pct = ((last - oldest) / oldest * Decimal("100")) if oldest else Decimal("0")
    return {"recent_klines": rows[:20], "last_close": str(last), "change_pct_over_sample": str(change_pct.quantize(Decimal("0.01"))), "sample_size": len(rows)}
