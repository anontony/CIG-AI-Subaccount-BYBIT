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
        self.model = (model if model is not None else os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip()
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
        }

        # Prefer strict function calling. It is more reliable for this bot than
        # plain JSON mode because some models can satisfy JSON mode with `{}`.
        # If tool calling is rejected by the selected model/provider, fallback to
        # structured/json/plain completion below.
        content = "{}"
        tool_debug_error = ""
        try:
            response = await self._create_tool_call_completion(base_kwargs, max_output_tokens=1400)
            content = _extract_tool_or_text_content(response) or "{}"
        except Exception as exc:
            tool_debug_error = f"tool_call_failed: {type(exc).__name__}: {str(exc)[:300]}"
            response = await self._create_chat_completion(base_kwargs, max_output_tokens=1400)
            content = _extract_tool_or_text_content(response) or "{}"
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return {
                "action": "WAIT",
                "reason": f"AI trả JSON lỗi: {content[:200]}",
                "_debug": {"raw_ai_response": content[:4000], "parse_error": "json_decode_error"},
            }
        if not isinstance(data, dict):
            return {
                "action": "WAIT",
                "reason": "AI không trả về object JSON.",
                "_debug": {"raw_ai_response": content[:4000], "parse_error": "not_json_object"},
            }

        # Guard against empty JSON like `{}`. First retry once with a much stricter
        # correction prompt. If the model still returns `{}`, convert it into a
        # deterministic WAIT using the snapshot that was sent to AI.
        if not data or not str(data.get("action") or "").strip():
            retry_content = await self._retry_empty_json(system, user_payload, content)
            if retry_content:
                try:
                    retry_data = json.loads(retry_content)
                    if isinstance(retry_data, dict) and str(retry_data.get("action") or "").strip():
                        data = retry_data
                        content = retry_content
                    else:
                        data = {}
                except json.JSONDecodeError:
                    data = {}
            if not data or not str(data.get("action") or "").strip():
                fallback_model_content = await self._retry_with_fallback_models(system, user_payload)
                if fallback_model_content:
                    try:
                        fallback_model_data = json.loads(fallback_model_content)
                        if isinstance(fallback_model_data, dict) and str(fallback_model_data.get("action") or "").strip():
                            data = fallback_model_data
                            content = fallback_model_content
                    except json.JSONDecodeError:
                        pass

            if not data or not str(data.get("action") or "").strip():
                data = _deterministic_signal_from_snapshot(user_payload)
                # Do not keep RAW AI RESPONSE as literal `{}` after recovery;
                # otherwise the live log makes it look like the bot ignored the
                # recovery. Mark it explicitly as an AI-empty recovery.
                content = "AI_EMPTY_OBJECT_RECOVERED -> " + json.dumps(data, ensure_ascii=False)

        # Keep an internal debug copy for bot logs. Downstream execution code
        # ignores keys prefixed with underscore, but the app can log this to
        # show exactly what the model returned before normalization.
        data["_debug"] = {
            "raw_ai_response": content[:4000],
            "model": self.model,
            "mode": user_payload.get("mode"),
            "tool_call_debug": tool_debug_error if 'tool_debug_error' in locals() else "",
        }

        # Normalize empty / placeholder WAIT reasons so the dashboard never shows
        # "No reason provided". This keeps logs useful even when the model
        # returns a minimal WAIT object.
        action = str(data.get("action") or "WAIT").upper().strip()
        reason = str(data.get("reason") or "").strip()
        bad_reasons = {"", "no reason provided", "none", "null", "n/a", "na"}
        if action in {"WAIT", "HOLD", "NO_TRADE"} and reason.lower() in bad_reasons:
            directives = user_payload.get("prompt_directives") or {}
            requires_explicit = isinstance(directives, dict) and bool(directives.get("requires_explicit_tp_sl"))
            # The model sometimes returns WAIT without a reason even when the
            # user prompt explicitly asks for details. Do not show a vague
            # generic reason. Return an actionable fallback that lists at least
            # two concrete blockers and clearly says it is a safety fallback.
            if requires_explicit:
                data["reason"] = (
                    "KHÔNG VÀO LỆNH: AI không trả lý do chi tiết; bot chặn lệnh vì "
                    "1) chưa có stop_loss và take_profit cụ thể theo ATR/RR/structure, "
                    "2) chưa xác nhận đủ điều kiện entry/pullback trên H1 và xu hướng D1/H4 từ dữ liệu hiện tại."
                )
            else:
                data["reason"] = (
                    "KHÔNG VÀO LỆNH: AI không trả lý do chi tiết; bot giữ WAIT vì "
                    "1) tín hiệu chưa đủ rõ, 2) dữ liệu hiện tại chưa đủ an toàn để mở lệnh."
                )
        return data

    async def _retry_empty_json(self, system: str, user_payload: Dict[str, Any], previous_content: str) -> str:
        """Retry once when the model returns `{}`.

        V41 does not retry with the same huge raw user prompt. It retries with a
        compact, strongly-instructed payload focused only on the selected symbol
        and the extracted directives. This fixes cases where a long strategy
        prompt overwhelms the model and it returns an empty JSON object.
        """
        assert self.client is not None
        compact_payload = _compact_retry_payload(user_payload)
        retry_system = _retry_system_prompt()
        messages = [
            {"role": "system", "content": retry_system},
            {"role": "user", "content": json.dumps(compact_payload, ensure_ascii=False)},
        ]
        try:
            response = await self._plain_chat_completion(
                {"model": self.model, "messages": messages},
                max_output_tokens=2200,
                temperature=0.0,
            )
            content = response.choices[0].message.content or ""
            if content.strip() and content.strip() != "{}":
                return content
        except Exception:
            pass

        # Final ultra-short retry WITHOUT response_format/json mode. Some models
        # satisfy JSON mode with an empty object, so this last attempt uses plain
        # chat and a tiny payload. It should return a useful WAIT/OPEN JSON.
        short_messages = [
            {"role": "system", "content": "Return ONLY one valid JSON object. It MUST contain action, category, symbol, confidence, reason. Never return {}. If uncertain, return WAIT with two concrete Vietnamese reasons from the snapshot."},
            {"role": "user", "content": json.dumps(compact_payload, ensure_ascii=False)},
        ]
        try:
            response = await self._plain_chat_completion(
                {"model": self.model, "messages": short_messages},
                max_output_tokens=1200,
                temperature=0.0,
            )
            return response.choices[0].message.content or ""
        except Exception:
            return ""


    async def _create_tool_call_completion(self, base_kwargs: Dict[str, Any], *, max_output_tokens: int):
        """Force the model to call `submit_trading_signal` with strict args.

        This is the most reliable path for trading automation. The assistant
        message content can be empty when the model calls a function, so callers
        must parse tool_calls[0].function.arguments.
        """
        assert self.client is not None
        tool = _trading_signal_tool()
        token_variants = [
            {"max_completion_tokens": max_output_tokens},
            {"max_tokens": max_output_tokens},
        ]
        temp_variants = [
            {"temperature": 0.0},
            {},
        ]
        last_error: Optional[Exception] = None
        for token_kwargs in token_variants:
            for temp_kwargs in temp_variants:
                kwargs = {**base_kwargs, **token_kwargs, **temp_kwargs}
                kwargs["tools"] = [tool]
                kwargs["tool_choice"] = {"type": "function", "function": {"name": "submit_trading_signal"}}
                kwargs["parallel_tool_calls"] = False
                try:
                    return await self.client.chat.completions.create(**kwargs)
                except Exception as exc:
                    last_error = exc
                    msg = str(exc).lower()
                    if (
                        "unsupported parameter" in msg
                        or "unsupported value" in msg
                        or "does not support" in msg
                        or "not supported" in msg
                        or "unknown parameter" in msg
                        or "unrecognized request argument" in msg
                        or "tools" in msg
                        or "tool_choice" in msg
                        or "parallel_tool_calls" in msg
                        or "max_tokens" in msg
                        or "max_completion_tokens" in msg
                        or "temperature" in msg
                        or "schema" in msg
                        or "strict" in msg
                    ):
                        continue
                    raise
        assert last_error is not None
        raise last_error


    async def _retry_with_fallback_models(self, system: str, user_payload: Dict[str, Any]) -> str:
        """Try known stable trading-parser models when the selected model returns `{}`.

        This covers cases where the configured model is too weak for structured
        trading output, does not support the current response format well, or
        spends the budget internally and emits an empty object.
        """
        assert self.client is not None
        configured = [m.strip() for m in os.getenv("OPENAI_FALLBACK_MODELS", "gpt-4o-mini,gpt-4.1-mini").split(",") if m.strip()]
        models: List[str] = []
        for model in configured:
            if model and model not in models and model != self.model:
                models.append(model)
        if not models:
            return ""

        compact_payload = _compact_retry_payload(user_payload)
        messages = [
            {"role": "system", "content": _retry_system_prompt()},
            {"role": "user", "content": json.dumps(compact_payload, ensure_ascii=False)},
        ]
        original_model = self.model
        for model in models:
            try:
                base_kwargs = {"model": model, "messages": messages}
                try:
                    response = await self._create_tool_call_completion(base_kwargs, max_output_tokens=1800)
                    content = _extract_tool_or_text_content(response) or ""
                except Exception:
                    response = await self._plain_chat_completion(base_kwargs, max_output_tokens=1600, temperature=0.0)
                    content = _extract_tool_or_text_content(response) or ""
                if not content.strip() or content.strip() == "{}":
                    continue
                data = json.loads(content)
                if isinstance(data, dict) and str(data.get("action") or "").strip():
                    data.setdefault("_fallback_model", model)
                    data.setdefault("reason", "Fallback model tạo tín hiệu hợp lệ.")
                    return json.dumps(data, ensure_ascii=False)
            except Exception:
                continue
        self.model = original_model
        return ""


    async def _plain_chat_completion(self, base_kwargs: Dict[str, Any], *, max_output_tokens: int, temperature: float = 0.0):
        """Call chat completions with no response_format.

        This is intentionally separate from `_create_chat_completion`. It avoids
        JSON mode / schema mode because some models can return `{}` while still
        technically satisfying JSON mode. Used only for retry/recovery paths.
        """
        assert self.client is not None
        token_variants = [
            {"max_completion_tokens": max_output_tokens},
            {"max_tokens": max_output_tokens},
        ]
        temp_variants = [
            {"temperature": temperature},
            {},
        ]
        last_error: Optional[Exception] = None
        for token_kwargs in token_variants:
            for temp_kwargs in temp_variants:
                kwargs = {**base_kwargs, **token_kwargs, **temp_kwargs}
                try:
                    return await self.client.chat.completions.create(**kwargs)
                except Exception as exc:
                    last_error = exc
                    msg = str(exc).lower()
                    if "temperature" in msg or "max_tokens" in msg or "max_completion_tokens" in msg or "unsupported parameter" in msg or "does not support" in msg:
                        continue
                    raise
        assert last_error is not None
        raise last_error


    async def _create_chat_completion(self, base_kwargs: Dict[str, Any], *, max_output_tokens: int):
        assert self.client is not None

        # Prefer structured outputs to prevent empty `{}` responses. Fallback to
        # legacy JSON mode and then plain chat if the selected model rejects a
        # response_format variant.
        response_formats = [
            _trading_signal_response_format(),
            {"type": "json_object"},
            None,
        ]
        token_variants = [
            {"max_completion_tokens": max_output_tokens},
            {"max_tokens": max_output_tokens},
        ]
        temp_variants = [
            {},
            {"temperature": 0.05},
        ]

        last_error: Optional[Exception] = None
        for response_format in response_formats:
            for token_kwargs in token_variants:
                for temp_kwargs in temp_variants:
                    kwargs = {**base_kwargs, **token_kwargs, **temp_kwargs}
                    if response_format is not None:
                        kwargs["response_format"] = response_format
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
                            or "response_format" in msg
                            or "json_schema" in msg
                            or "max_tokens" in msg
                            or "max_completion_tokens" in msg
                            or "temperature" in msg
                        )
                        if recoverable_openai_param_error:
                            continue
                        raise
        assert last_error is not None
        raise last_error


def _fmt_num(value: Any, ndigits: int = 2) -> str:
    try:
        return str(round(float(value), ndigits))
    except Exception:
        return "unknown"


def _tf_line(tf_data: Any, label: str) -> str:
    if not isinstance(tf_data, dict):
        return f"{label} thiếu dữ liệu"
    ind = tf_data.get("indicators") if isinstance(tf_data.get("indicators"), dict) else tf_data
    struct = tf_data.get("structure") if isinstance(tf_data.get("structure"), dict) else {}
    trend = ind.get("trend", "unknown")
    rsi = _fmt_num(ind.get("rsi14"))
    macd = ind.get("macd") if isinstance(ind.get("macd"), dict) else {}
    macd_bias = macd.get("bias", "unknown")
    atr = _fmt_num(ind.get("atr14"))
    support = _fmt_num(struct.get("recent_support") or struct.get("swing_low"))
    resistance = _fmt_num(struct.get("recent_resistance") or struct.get("swing_high"))
    return f"{label} trend={trend}, RSI={rsi}, MACD={macd_bias}, ATR={atr}, hỗ trợ={support}, kháng cự={resistance}"


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _get_btc_snapshot(user_payload: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = user_payload.get("market_snapshot") if isinstance(user_payload, dict) else {}
    symbols = snapshot.get("symbols") if isinstance(snapshot, dict) else {}
    btc = symbols.get("linear:BTCUSDT") if isinstance(symbols, dict) else {}
    return btc if isinstance(btc, dict) else {}


def _tf_parts(btc: Dict[str, Any], tf: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    tfs = btc.get("timeframes") if isinstance(btc.get("timeframes"), dict) else {}
    node = tfs.get(tf) if isinstance(tfs, dict) else {}
    if not isinstance(node, dict):
        return {}, {}
    ind = node.get("indicators") if isinstance(node.get("indicators"), dict) else node
    struct = node.get("structure") if isinstance(node.get("structure"), dict) else {}
    return (ind if isinstance(ind, dict) else {}), (struct if isinstance(struct, dict) else {})


def _deterministic_signal_from_snapshot(user_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Last-resort local decision when the model returns `{}` repeatedly.

    It is conservative. It can create a small SHORT only when the relaxed strategy
    conditions are plainly met and concrete SL/TP can be computed from H1
    structure/ATR. Otherwise it returns WAIT with concrete reasons.
    """
    btc = _get_btc_snapshot(user_payload)
    if not btc:
        return {
            "action": "WAIT",
            "category": "linear",
            "symbol": "BTCUSDT",
            "confidence": 0,
            "reason": "KHÔNG VÀO LỆNH: AI trả object rỗng {}; bot không thấy snapshot linear:BTCUSDT hợp lệ, nên chặn lệnh an toàn.",
            "_empty_ai_object": True,
            "_deterministic_recovery": True,
        }

    price = _float_or_none(btc.get("price"))
    d1, _ = _tf_parts(btc, "1d")
    h4, _ = _tf_parts(btc, "4h")
    h1, h1s = _tf_parts(btc, "1h")
    m15, _ = _tf_parts(btc, "15m")

    d1_trend = str(d1.get("trend") or "unknown")
    h4_trend = str(h4.get("trend") or "unknown")
    h1_trend = str(h1.get("trend") or "unknown")
    h1_rsi = _float_or_none(h1.get("rsi14"))
    h1_atr = _float_or_none(h1.get("atr14"))
    h1_ema20 = _float_or_none(h1.get("ema20"))
    h1_ema50 = _float_or_none(h1.get("ema50"))
    h1_ema200 = _float_or_none(h1.get("ema200"))
    swing_high = _float_or_none(h1s.get("swing_high") or h1s.get("recent_resistance"))
    swing_low = _float_or_none(h1s.get("swing_low") or h1s.get("recent_support"))
    m15_macd = m15.get("macd") if isinstance(m15.get("macd"), dict) else {}
    h1_macd = h1.get("macd") if isinstance(h1.get("macd"), dict) else {}
    h1_macd_bias = str(h1_macd.get("bias") or "unknown")
    m15_macd_bias = str(m15_macd.get("bias") or "unknown")

    # Relaxed short criteria from the user's latest prompt. Keep conservative:
    # downtrend context + H1 RSI in short zone + price near EMA band + computable SL/TP.
    down_context = ("down" in d1_trend.lower()) or ("down" in h4_trend.lower())
    rsi_ok_short = h1_rsi is not None and 50 <= h1_rsi <= 68
    ema_candidates = [x for x in (h1_ema20, h1_ema50, h1_ema200) if x and price]
    near_ema = bool(price and h1_atr and ema_candidates and min(abs(price - e) for e in ema_candidates) <= max(h1_atr * 1.25, price * 0.004))
    can_compute = bool(price and h1_atr and (swing_high or h1_ema200) and (swing_low or h1s.get("recent_support")))
    bullish_too_strong = h1_macd_bias.lower() == "bullish" and m15_macd_bias.lower() == "bullish" and h1_rsi is not None and h1_rsi > 65

    if down_context and rsi_ok_short and near_ema and can_compute and not bullish_too_strong:
        # SL above the nearest practical resistance/EMA/ATR buffer, not the
        # farthest swing in the whole lookback. Using the farthest swing can
        # make RR impossible and prevent the deterministic fallback from ever
        # helping.
        above_candidates = [x for x in (swing_high, h1_ema200, h1_ema50, h1_ema20, price + h1_atr * 0.85) if x is not None and x > price]
        if not above_candidates:
            above_candidates = [price + h1_atr * 0.85]
        sl_base = min(above_candidates, key=lambda x: abs(x - price))
        stop_loss = round(sl_base + h1_atr * 0.12, 1)
        risk_dist = stop_loss - price
        if risk_dist > 0:
            take_profit = round(price - risk_dist * 1.25, 1)
            # Avoid aiming below major support too aggressively; cap near swing_low if present.
            if swing_low and take_profit < swing_low:
                take_profit = round(max(swing_low, price - risk_dist * 1.2), 1)
            if take_profit < price:
                rr = round((price - take_profit) / risk_dist, 2)
                if rr >= 1.2:
                    return {
                        "action": "OPEN_SHORT",
                        "category": "linear",
                        "symbol": "BTCUSDT",
                        "leverage": 10,
                        "entry_type": "market",
                        "margin_usdt": 8,
                        "risk_usdt": 1,
                        "stop_loss": stop_loss,
                        "take_profit": take_profit,
                        "confidence": 55,
                        "reason": (
                            f"AI trả object rỗng nên bot dùng bộ phục hồi nội bộ: D1/H4 có bối cảnh giảm "
                            f"({d1_trend}/{h4_trend}), H1 RSI={_fmt_num(h1_rsi)} nằm vùng short 50-68, "
                            f"giá gần EMA/kháng cự H1; SL={stop_loss}, TP={take_profit}, RR≈{rr}R."
                        ),
                        "_empty_ai_object": True,
                        "_deterministic_recovery": True,
                    }

    blockers = []
    # Explain why deterministic recovery did not open a trade.
    if down_context and rsi_ok_short and near_ema and can_compute and not bullish_too_strong:
        blockers.append("bộ phục hồi tính thử SHORT nhưng R:R hoặc TP/SL theo ATR/structure chưa đạt ngưỡng an toàn")
    if not down_context:
        blockers.append(f"D1/H4 chưa cho bối cảnh giảm rõ ({d1_trend}/{h4_trend})")
    if not rsi_ok_short:
        blockers.append(f"RSI H1={_fmt_num(h1_rsi)} chưa nằm vùng short nới 50-68")
    if not near_ema:
        blockers.append("giá H1 chưa đủ gần EMA20/EMA50/EMA200 hoặc vùng entry rõ")
    if bullish_too_strong:
        blockers.append("MACD H1/15m vẫn bullish mạnh, dễ bị short ngược nhịp hồi")
    if not can_compute:
        blockers.append("chưa đủ ATR/structure H1 để tính SL/TP cụ thể")
    if len(blockers) < 2:
        blockers.append("AI không trả action/reason nên bot giữ an toàn")

    return {
        "action": "WAIT",
        "category": "linear",
        "symbol": "BTCUSDT",
        "confidence": 0,
        "reason": "KHÔNG VÀO LỆNH: AI trả object rỗng {}; bot dùng phục hồi nội bộ và chặn lệnh vì " + "; ".join(f"{i+1}) {b}" for i, b in enumerate(blockers[:4])) + ".",
        "_empty_ai_object": True,
        "_deterministic_recovery": True,
    }


def _snapshot_wait_reason(user_payload: Dict[str, Any]) -> str:
    """Build a concrete WAIT reason when the AI returns empty `{}`.

    This is not a trading recommendation; it is a safety explanation based on the
    same snapshot sent to the model. It prevents the dashboard from showing a
    vague fallback when the model produced no JSON fields.
    """
    snapshot = user_payload.get("market_snapshot") if isinstance(user_payload, dict) else {}
    symbols = snapshot.get("symbols") if isinstance(snapshot, dict) else {}
    btc = symbols.get("linear:BTCUSDT") if isinstance(symbols, dict) else {}
    if not isinstance(btc, dict):
        return "KHÔNG VÀO LỆNH: AI trả object rỗng {}; bot chặn lệnh vì 1) thiếu dữ liệu BTCUSDT linear hợp lệ trong snapshot, 2) không có stop_loss/take_profit cụ thể."

    price = _fmt_num(btc.get("price"))
    timeframes = btc.get("timeframes") if isinstance(btc.get("timeframes"), dict) else {}
    d1 = _tf_line(timeframes.get("1d"), "D1")
    h4 = _tf_line(timeframes.get("4h"), "H4")
    h1 = _tf_line(timeframes.get("1h"), "H1")

    # Name concrete blockers. Keep this conservative: an empty AI response must
    # never become an opening signal.
    return (
        f"KHÔNG VÀO LỆNH: AI trả object rỗng {{}}; bot chặn lệnh an toàn. "
        f"Giá BTCUSDT={price}. 1) Bối cảnh đa khung chưa đủ đồng thuận: {d1}; {h4}; {h1}. "
        f"2) AI không trả stop_loss và take_profit cụ thể theo ATR/RR/structure, nên bot không được dùng TP/SL mặc định."
    )



def _compact_retry_payload(user_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build a compact retry payload for models that returned `{}`.

    It keeps the trading decision grounded but removes excess text and extra
    symbols. The retry should choose WAIT unless an entry has concrete SL/TP.
    """
    snapshot = user_payload.get("market_snapshot") if isinstance(user_payload, dict) else {}
    symbols = snapshot.get("symbols") if isinstance(snapshot, dict) else {}
    btc = symbols.get("linear:BTCUSDT") if isinstance(symbols, dict) else None
    if btc is None and isinstance(symbols, dict) and symbols:
        btc = next(iter(symbols.values()))
    prompt = str(user_payload.get("user_trading_prompt") or "")
    directives = user_payload.get("prompt_directives") if isinstance(user_payload.get("prompt_directives"), dict) else {}
    return {
        "task": "Return exactly one trading signal JSON. Prefer WAIT unless the setup is clear and concrete SL/TP can be computed.",
        "strategy_summary": {
            "market": "linear futures",
            "symbol": "BTCUSDT",
            "leverage_default": 10,
            "margin_usdt_default": 8,
            "risk_usdt_default": 1,
            "min_confidence": 55,
            "min_rr": 1.2,
            "timeframes": "Use D1/H4 for context, H1 for entry and ATR/SL/TP, 15m only as confirmation.",
            "short_relaxed": "Short is allowed when D1 or H4 downtrend and H1 has weak pullback/rejection near EMA/support-resistance, with concrete SL above swing/resistance and TP >= 1.2R.",
            "long_relaxed": "Long is allowed only when H4 is not strongly downtrend or H1 turns clearly bullish, with concrete SL below swing/support and TP >= 1.2R.",
            "hard_blocks": [
                "Never return {}",
                "Never open without concrete stop_loss and take_profit",
                "Never use default TP/SL",
                "Never open if RR < 1.2R",
                "If unclear, WAIT with at least 2 concrete reasons from snapshot",
            ],
        },
        "extracted_directives": directives,
        "raw_prompt_excerpt": prompt[:2500],
        "risk_config": user_payload.get("risk_config") or {},
        "market_snapshot": {"symbols": {"linear:BTCUSDT": btc} if btc is not None else {}},
        "required_output_shape": {
            "action": "WAIT or OPEN_LONG or OPEN_SHORT",
            "category": "linear",
            "symbol": "BTCUSDT",
            "leverage": "10 or null for WAIT",
            "margin_usdt": "8 or null for WAIT",
            "risk_usdt": "1 or null for WAIT",
            "stop_loss": "numeric price or null for WAIT",
            "take_profit": "numeric price or null for WAIT",
            "confidence": "0-100",
            "reason": "Vietnamese; for WAIT list at least 2 failed conditions; for OPEN include trend/entry/SL/TP/RR",
        },
    }


def _retry_system_prompt() -> str:
    return """
You are the emergency correction layer for a Bybit trading bot.
The previous model response was invalid empty JSON {}.
Return ONLY one valid JSON object. No markdown. No prose outside JSON.

MANDATORY:
- action must be one of: WAIT, OPEN_LONG, OPEN_SHORT.
- category must be "linear".
- symbol must be "BTCUSDT".
- reason must be Vietnamese and non-empty.
- Never return {}.
- If action is WAIT: stop_loss and take_profit may be null, confidence must be 0, reason must list at least 2 concrete failed conditions from the snapshot.
- If action is OPEN_LONG or OPEN_SHORT: leverage=10, margin_usdt=8, risk_usdt=1, stop_loss and take_profit must be concrete numeric prices, confidence >=55, RR must be >=1.2R.
- Do not invent market data. Use only provided snapshot.
- If SL/TP cannot be calculated from H1 ATR/structure/support/resistance, return WAIT.
""".strip()

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
  "reason": "Vietnamese explanation with at least 2 concrete failed conditions when action is WAIT"
}
""".strip()




def _extract_tool_or_text_content(response: Any) -> str:
    """Return tool-call arguments when present, otherwise assistant text."""
    try:
        choice = response.choices[0]
        msg = choice.message
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            fn = getattr(tool_calls[0], "function", None)
            args = getattr(fn, "arguments", "") if fn is not None else ""
            if args:
                return str(args)
        content = getattr(msg, "content", "") or ""
        return str(content)
    except Exception:
        return ""


def _trading_signal_schema() -> Dict[str, Any]:
    props = {
        "action": {"type": "string", "enum": ["WAIT", "OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT", "CLOSE_ALL", "SPOT_BUY", "SPOT_SELL", "SPOT_SELL_ALL"]},
        "symbol": {"type": ["string", "null"]},
        "category": {"type": ["string", "null"], "enum": ["spot", "linear", "inverse", None]},
        "leverage": {"type": ["number", "string", "null"]},
        "margin_usdt": {"type": ["number", "string", "null"]},
        "risk_usdt": {"type": ["number", "string", "null"]},
        "order_usdt": {"type": ["number", "string", "null"]},
        "qty": {"type": ["number", "string", "null"]},
        "take_profit": {"type": ["number", "string", "null"]},
        "stop_loss": {"type": ["number", "string", "null"]},
        "take_profit_pct": {"type": ["number", "string", "null"]},
        "stop_loss_pct": {"type": ["number", "string", "null"]},
        "confidence": {"type": ["number", "string", "null"]},
        "reason": {"type": "string"},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": props,
        "required": list(props.keys()),
    }


def _trading_signal_tool() -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "submit_trading_signal",
            "description": "Submit exactly one Bybit trading signal or WAIT decision for BTCUSDT.",
            "strict": True,
            "parameters": _trading_signal_schema(),
        },
    }


def _trading_signal_response_format() -> Dict[str, Any]:
    """Strict structured-output schema using the same schema as function calling."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "trading_signal_strict_v43",
            "strict": True,
            "schema": _trading_signal_schema(),
        },
    }


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
- If the user gives TP/SL as explicit percentages, return take_profit_pct and stop_loss_pct. If the strategy uses ATR, R-multiple, structure, support/resistance, or says TP/SL must be explicit, you MUST return concrete take_profit and stop_loss prices. Do not rely on default TP/SL for strategy prompts.
- Only use symbols listed in risk_config.allowed_symbols.
- Do not exceed risk_config limits.
- Never request withdrawal, transfer, API-key changes, or account movement.
- Strategy loop may return WAIT if the instruction is ambiguous or unsafe.
""".strip()


def _skill_block(skill_context: str) -> str:
    if not skill_context:
        return ""
    return "\nBYBIT SKILL CONTEXT - local/auto-updated cache:\n" + skill_context[:1800] + "\nEND BYBIT SKILL CONTEXT.\n"


def _decision_system_prompt(skill_context: str = "") -> str:
    return f"""
You are a trading decision engine for a Bybit execution bot.
Return ONLY valid JSON. No markdown. No commentary outside JSON. Never return {{}}. Always include action and reason.

{_allowed_shape()}

Hard rules:
{_shared_rules()}
{_skill_block(skill_context)}
- Use the provided snapshots. Snapshot keys are formatted like "linear:BTCUSDT" and "spot:BTCUSDT".
- Respect the strategy prompt exactly, including recurring schedule, market type, symbols, leverage, TP/SL, timeframe, and indicator rules such as RSI / EMA / MACD / volume conditions.
- If prompt_directives are provided, treat them as extracted constraints from the prompt and keep them aligned with the raw prompt text.
- If prompt_directives.requires_explicit_tp_sl is true, you MUST NOT return OPEN_LONG or OPEN_SHORT unless both stop_loss and take_profit are concrete numeric prices. If you cannot compute them from the snapshot, return WAIT with a clear Vietnamese reason.
- WAIT reason rule: when action is WAIT, reason MUST be Vietnamese and MUST list at least 2 concrete failed conditions from the current snapshot/prompt. Use the format: "KHÔNG VÀO LỆNH: 1) ...; 2) ...". Never return reason as empty, null, none, or "No reason provided". Do not use vague text such as "setup chưa đủ điều kiện" without naming the failed conditions. Include the exact missing/failed data points whenever available, for example: D1/H4 trend unknown, H1 pullback not present, RSI not in zone, MACD not confirmed, ATR insufficient, or cannot calculate concrete SL/TP.
- Recurring/DCA cadence such as every 1 hour, mỗi 1 tiếng, 10 USDT/1h is enforced by the bot scheduler outside the AI. When invoked, assume the scheduler has decided this is an eligible execution window; do not ignore a clear DCA buy solely because it contains a time interval.
- Prefer multi-timeframe data when available: timeframes.1d, timeframes.4h, and timeframes.1h. Use 1d/4h for trend context, 1h for entry/pullback and ATR-based TP/SL, and 15m only as extra short-term confirmation.
- If the strategy prompt does not clearly authorize a new trade, return WAIT. If the setup is valid but you cannot compute concrete stop_loss and take_profit prices, return WAIT with a reason naming at least 2 blockers instead of returning an opening action without TP/SL.
- For strategy prompts using D1/H4/H1 + EMA/RSI/MACD/ATR, first inspect snapshot.timeframes["1d"], snapshot.timeframes["4h"], and snapshot.timeframes["1h"]. A good WAIT reason must mention which of these failed: D1/H4 trend, H1 pullback, candle confirmation, RSI zone, MACD confirmation, ATR/TP/SL calculation, R:R >= 1.5R, daily trade/risk limits, existing position.
- Do not invent market data. Use only the provided snapshot. Never output OPEN_LONG or OPEN_SHORT without stop_loss and take_profit when the prompt requires ATR/RR/structure exits. If timeframes.1h.structure has swing_low/swing_high/support/resistance and timeframes.1h.indicators has atr14, use them to calculate concrete stop_loss/take_profit and then check R:R before opening.
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
