import re
import unicodedata
from typing import Any, Dict, List


def _strip_accents(text: str) -> str:
    text = text.replace("đ", "d").replace("Đ", "D")
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _clean_number(raw: str) -> str:
    return raw.strip().replace(",", "")


def _find_symbol(command: str, allowed_symbols: List[str]) -> str:
    text = command.upper()
    allowed = [s.upper().strip() for s in allowed_symbols if s.strip()]
    for sym in allowed:
        if sym in text:
            return sym
        base = sym.replace("USDT", "")
        if re.search(rf"\b{re.escape(base)}\b", text):
            return sym
    return ""


def _find_first(patterns: List[str], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean_number(match.group(1))
    return ""


def _value_was_percent(label: str, value: str, ascii_text: str) -> bool:
    if not value:
        return False
    patterns = [
        rf"\b{label}\s*[:=]?\s*{re.escape(value)}\s*%",
    ]
    if label == "tp":
        patterns.append(rf"chot\s*loi\s*[:=]?\s*{re.escape(value)}\s*%")
    if label == "sl":
        patterns.append(rf"cat\s*lo\s*[:=]?\s*{re.escape(value)}\s*%")
    return any(re.search(pat, ascii_text, flags=re.IGNORECASE) for pat in patterns)


def _infer_market(ascii_text: str, default_category: str) -> str:
    if any(w in ascii_text for w in ["spot", "giao ngay", "nam giu", "hold coin"]):
        return "spot"
    if any(w in ascii_text for w in ["future", "futures", "perp", "perpetual", "hop dong", "don bay", "leverage", " short", " x"]):
        return "linear"
    return "linear" if default_category in {"auto", "linear", "inverse"} else default_category


def parse_direct_command(command: str, allowed_symbols: List[str], default_category: str = "auto") -> Dict[str, Any]:
    """Conservative Vietnamese/English fallback parser for simple trade commands."""
    original = command.strip()
    text = original.lower()
    ascii_text = _strip_accents(text)
    symbol = _find_symbol(original, allowed_symbols)

    if not symbol:
        return {"action": "WAIT", "reason": "Không nhận diện được symbol nằm trong allowed_symbols."}

    category = _infer_market(ascii_text, default_category)
    close_intent = any(w in ascii_text for w in ["dong", "close", "thoat", "tat lenh", "cat lenh"])
    sell_all = close_intent or any(w in ascii_text for w in ["ban het", "sell all", "xoa het spot"])

    if category == "spot":
        if sell_all:
            return {"action": "SPOT_SELL_ALL", "symbol": symbol, "category": "spot", "reason": "Rule parser: bán hết spot."}
        is_buy = bool(re.search(r"\b(mua|buy|gom|spot buy)\b", ascii_text))
        is_sell = bool(re.search(r"\b(ban|sell)\b", ascii_text))
        if is_buy and is_sell:
            return {"action": "WAIT", "symbol": symbol, "reason": "Lệnh spot có cả mua và bán nên không an toàn."}
        if not is_buy and not is_sell:
            return {"action": "WAIT", "symbol": symbol, "reason": "Không nhận diện được mua/bán spot."}
        amount = _find_first([
            r"(?:order|von|vốn|size|mua|buy)\s*[:=]?\s*(\d+(?:\.\d+)?)",
            r"(\d+(?:\.\d+)?)\s*(?:u|usdt|usd|do|đô|dola|dollar)\b",
        ], text)
        qty = _find_first([r"(?:qty|so luong|số lượng)\s*[:=]?\s*(\d+(?:\.\d+)?)"], ascii_text)
        tp = _find_first([r"\btp\s*[:=]?\s*(\d+(?:\.\d+)?)", r"chot\s*loi\s*[:=]?\s*(\d+(?:\.\d+)?)"], ascii_text)
        sl = _find_first([r"\bsl\s*[:=]?\s*(\d+(?:\.\d+)?)", r"cat\s*lo\s*[:=]?\s*(\d+(?:\.\d+)?)"], ascii_text)
        if is_buy:
            if not amount:
                return {"action": "WAIT", "symbol": symbol, "reason": "SPOT_BUY thiếu số USDT muốn mua."}
            out = {"action": "SPOT_BUY", "symbol": symbol, "category": "spot", "order_usdt": amount, "reason": "Rule parser: spot buy."}
            if tp:
                out["take_profit_pct" if _value_was_percent("tp", tp, ascii_text) else "take_profit"] = tp
            if sl:
                out["stop_loss_pct" if _value_was_percent("sl", sl, ascii_text) else "stop_loss"] = sl
            return out
        if not qty and not amount:
            return {"action": "WAIT", "symbol": symbol, "reason": "SPOT_SELL cần qty coin, order_usdt hoặc bán hết."}
        out = {"action": "SPOT_SELL", "symbol": symbol, "category": "spot", "reason": "Rule parser: spot sell."}
        if qty: out["qty"] = qty
        if amount: out["order_usdt"] = amount
        return out

    if close_intent:
        if re.search(r"\blong\b", ascii_text):
            return {"action": "CLOSE_LONG", "symbol": symbol, "category": category, "reason": "Rule parser: close long command."}
        if re.search(r"\bshort\b", ascii_text):
            return {"action": "CLOSE_SHORT", "symbol": symbol, "category": category, "reason": "Rule parser: close short command."}
        return {"action": "CLOSE_ALL", "symbol": symbol, "category": category, "reason": "Rule parser: close all command."}

    is_long = bool(re.search(r"\b(mua|buy|long)\b", ascii_text))
    is_short = bool(re.search(r"\b(short|sell|ban)\b", ascii_text))
    if is_long and is_short:
        return {"action": "WAIT", "symbol": symbol, "reason": "Lệnh có cả long và short/sell nên không an toàn."}
    if not is_long and not is_short:
        return {"action": "WAIT", "symbol": symbol, "reason": "Không nhận diện được hướng lệnh mua/long hoặc short/bán."}

    margin = _find_first([
        r"(?:margin|von|vốn|size|order)\s*[:=]?\s*(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*(?:u|usdt|usd|do|đô|dola|dollar)\b",
    ], text)
    leverage = _find_first([
        r"(?:don|đòn|don bay|đòn bẩy|leverage|lev|x)\s*[:=]?\s*(\d+)",
        r"\bx\s*(\d+)\b",
        r"\b(\d+)\s*x\b",
    ], ascii_text)
    tp = _find_first([r"\btp\s*[:=]?\s*(\d+(?:\.\d+)?)", r"take\s*profit\s*[:=]?\s*(\d+(?:\.\d+)?)", r"chot\s*loi\s*[:=]?\s*(\d+(?:\.\d+)?)"], ascii_text)
    sl = _find_first([r"\bsl\s*[:=]?\s*(\d+(?:\.\d+)?)", r"stop\s*loss\s*[:=]?\s*(\d+(?:\.\d+)?)", r"cat\s*lo\s*[:=]?\s*(\d+(?:\.\d+)?)"], ascii_text)

    missing = []
    if not margin: missing.append("margin_usdt")
    if not leverage: missing.append("leverage")
    if missing:
        return {"action": "WAIT", "symbol": symbol, "reason": "Lệnh thiếu: " + ", ".join(missing)}

    out = {
        "action": "OPEN_LONG" if is_long else "OPEN_SHORT",
        "symbol": symbol,
        "category": category,
        "leverage": int(leverage),
        "margin_usdt": margin,
        "reason": "Rule parser fallback parsed direct futures command.",
    }
    if tp:
        out["take_profit_pct" if _value_was_percent("tp", tp, ascii_text) else "take_profit"] = tp
    if sl:
        out["stop_loss_pct" if _value_was_percent("sl", sl, ascii_text) else "stop_loss"] = sl
    return out
