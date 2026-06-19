from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional


OPENING_ACTIONS = {"SPOT_BUY", "OPEN_LONG", "OPEN_SHORT"}
CLOSING_ACTIONS = {"SPOT_SELL", "SPOT_SELL_ALL", "CLOSE_LONG", "CLOSE_SHORT", "CLOSE_ALL"}


def d(value: Any, default: str = "0") -> Decimal:
    try:
        if value in (None, ""):
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def side_from_action(action: str) -> str:
    action = (action or "").upper()
    if action == "OPEN_SHORT":
        return "short"
    if action in {"SPOT_BUY", "OPEN_LONG"}:
        return "long"
    if action in {"SPOT_SELL", "SPOT_SELL_ALL"}:
        return "sell"
    return ""


def pnl_snapshot(trade: Dict[str, Any], current_price: Optional[Any]) -> Dict[str, Any]:
    entry = d(trade.get("entry_price"))
    cur = d(current_price) if current_price not in (None, "") else Decimal("0")
    leverage = max(Decimal("1"), d(trade.get("leverage"), "1"))
    side = str(trade.get("side") or "").lower()
    category = str(trade.get("category") or "").lower()
    tp = d(trade.get("take_profit"), "0") if trade.get("take_profit") not in (None, "") else None
    sl = d(trade.get("stop_loss"), "0") if trade.get("stop_loss") not in (None, "") else None

    out: Dict[str, Any] = {
        "current_price": str(cur) if cur > 0 else None,
        "pnl_pct": None,
        "effective_pnl_pct": None,
        "pnl_usdt_est": None,
        "distance_to_tp_pct": None,
        "distance_to_sl_pct": None,
        "tp_sl_state": "tracking",
    }
    if entry <= 0 or cur <= 0:
        return out

    direction = Decimal("-1") if side == "short" else Decimal("1")
    price_pnl_pct = ((cur - entry) / entry) * Decimal("100") * direction
    effective_pnl_pct = price_pnl_pct * (leverage if category != "spot" else Decimal("1"))
    notional_base = d(trade.get("margin_usdt") or trade.get("order_usdt") or "0")
    pnl_usdt = notional_base * effective_pnl_pct / Decimal("100") if notional_base > 0 else Decimal("0")

    out["pnl_pct"] = str(price_pnl_pct.quantize(Decimal("0.01")))
    out["effective_pnl_pct"] = str(effective_pnl_pct.quantize(Decimal("0.01")))
    out["pnl_usdt_est"] = str(pnl_usdt.quantize(Decimal("0.0001")))

    if tp and tp > 0:
        if side == "short":
            out["distance_to_tp_pct"] = str(((cur - tp) / cur * Decimal("100")).quantize(Decimal("0.01")))
        else:
            out["distance_to_tp_pct"] = str(((tp - cur) / cur * Decimal("100")).quantize(Decimal("0.01")))
    if sl and sl > 0:
        if side == "short":
            out["distance_to_sl_pct"] = str(((sl - cur) / cur * Decimal("100")).quantize(Decimal("0.01")))
        else:
            out["distance_to_sl_pct"] = str(((cur - sl) / cur * Decimal("100")).quantize(Decimal("0.01")))

    hit_tp = False
    hit_sl = False
    if tp and tp > 0:
        hit_tp = cur <= tp if side == "short" else cur >= tp
    if sl and sl > 0:
        hit_sl = cur >= sl if side == "short" else cur <= sl
    if hit_tp:
        out["tp_sl_state"] = "tp_hit"
    elif hit_sl:
        out["tp_sl_state"] = "sl_hit"
    return out
