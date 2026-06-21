import re
import unicodedata
from typing import Any, Dict, List


def _strip_accents(text: str) -> str:
    text = text.replace("đ", "d").replace("Đ", "D")
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _clean_number(raw: str) -> str:
    return raw.strip().replace(",", "")


COIN_ALIASES = {
    "BTC": ["bitcoin", "bit coin", "btc"],
    "ETH": ["ethereum", "ether", "eth"],
    "SOL": ["solana", "sol"],
    "ADA": ["cardano", "ada"],
    "DOGE": ["dogecoin", "doge"],
    "XRP": ["ripple", "xrp"],
    "BNB": ["bnb", "binance coin"],
}


def _find_symbol(command: str, allowed_symbols: List[str]) -> str:
    text = command.upper()
    ascii_text = _strip_accents(command.lower())
    allowed = [s.upper().strip() for s in allowed_symbols if s.strip()]
    for sym in allowed:
        if sym in text:
            return sym
        base = sym.replace("USDT", "")
        if re.search(rf"\b{re.escape(base)}\b", text):
            return sym
        for alias in COIN_ALIASES.get(base, []):
            if re.search(rf"\b{re.escape(alias)}\b", ascii_text):
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




def is_clear_trade_execution_command(command: str) -> bool:
    """Return True for commands that should be treated as trading execution, not bot settings.

    This prevents phrases like "đóng hết lệnh future btc" from being routed
    through workspace/control parsing or AI strategy reasoning.
    """
    ascii_text = _strip_accents((command or "").lower())
    if not ascii_text.strip():
        return False

    # Bot-control phrases should remain bot-control, even if they contain words like "trade".
    control_markers = [
        "doi prompt", "cap nhat prompt", "thay prompt", "them vao prompt", "bo sung prompt",
        "allowed symbols", "danh sach symbol", "chi trade", "doi symbol", "cap nhat symbol",
        "max leverage", "don bay toi da", "margin toi da", "max margin",
        "dry run", "mo phong", "testnet", "mainnet", "openai model", "model:", "model=",
        "bat bot", "tat bot", "stop bot", "start bot",
    ]
    if any(k in ascii_text for k in control_markers):
        return False

    close_markers = [
        "dong lenh", "dong het lenh", "dong tat ca lenh", "dong het", "dong vi the",
        "thoat lenh", "thoat vi the", "cat lenh", "tat lenh",
        "close position", "close all", "close order", "close trade", "close futures", "close future",
    ]
    open_markers = [
        "open long", "open short", "vao long", "vao short", "future long", "future short",
        "futures long", "futures short", "long btc", "short btc", "long eth", "short eth",
        "mua btc", "mua bitcoin", "buy btc", "buy bitcoin", "ban btc", "sell btc",
        "spot mua", "mua spot", "ban spot", "sell spot",
    ]
    return any(k in ascii_text for k in close_markers + open_markers)

def parse_direct_command(command: str, allowed_symbols: List[str], default_category: str = "auto") -> Dict[str, Any]:
    """Conservative Vietnamese/English fallback parser for simple trade commands."""
    original = command.strip()
    text = original.lower()
    ascii_text = _strip_accents(text)
    symbol = _find_symbol(original, allowed_symbols)

    close_intent = any(w in ascii_text for w in [
        "dong", "close", "thoat", "tat lenh", "cat lenh",
        "dong het", "dong tat ca", "dong vi the", "thoat vi the", "close all", "close position"
    ])

    if not symbol:
        # For closing commands, if the workspace has only one allowed symbol, use it.
        # This lets commands like "đóng hết lệnh future" work in a BTC-only workspace.
        allowed_clean = [s.upper().strip() for s in allowed_symbols if str(s).strip()]
        if close_intent and len(allowed_clean) == 1:
            symbol = allowed_clean[0]
        else:
            return {"action": "WAIT", "reason": "Không nhận diện được symbol nằm trong allowed_symbols."}

    category = _infer_market(ascii_text, default_category)
    # Explicit future/perp/linear words force futures for close commands.
    if close_intent and any(w in ascii_text for w in ["future", "futures", "perp", "perpetual", "linear", "hop dong", "don bay", "leverage", "x20", "x10"]):
        category = "linear"
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
