from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional


def _d(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _f(value: Optional[Decimal]) -> Optional[float]:
    return float(value) if value is not None else None


def normalize_klines(raw: List[Any]) -> List[Dict[str, Decimal]]:
    """Return klines sorted oldest -> newest from Bybit V5 list rows."""
    rows: List[Dict[str, Decimal]] = []
    for item in raw or []:
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            continue
        ts = _d(item[0])
        o = _d(item[1]); h = _d(item[2]); l = _d(item[3]); c = _d(item[4]); v = _d(item[5])
        if None in {ts, o, h, l, c, v}:
            continue
        rows.append({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})
    rows.sort(key=lambda x: x["ts"])
    return rows


def sma(values: List[Decimal], period: int) -> Optional[Decimal]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / Decimal(period)


def ema(values: List[Decimal], period: int) -> Optional[Decimal]:
    if len(values) < period or period <= 0:
        return None
    k = Decimal(2) / Decimal(period + 1)
    e = sum(values[:period]) / Decimal(period)
    for value in values[period:]:
        e = value * k + e * (Decimal(1) - k)
    return e


def rsi(values: List[Decimal], period: int = 14) -> Optional[Decimal]:
    if len(values) <= period:
        return None
    gains: List[Decimal] = []
    losses: List[Decimal] = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, Decimal(0)))
        losses.append(abs(min(diff, Decimal(0))))
    avg_gain = sum(gains[:period]) / Decimal(period)
    avg_loss = sum(losses[:period]) / Decimal(period)
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * Decimal(period - 1) + g) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + l) / Decimal(period)
    if avg_loss == 0:
        return Decimal(100)
    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def atr(rows: List[Dict[str, Decimal]], period: int = 14) -> Optional[Decimal]:
    if len(rows) <= period:
        return None
    trs: List[Decimal] = []
    for i in range(1, len(rows)):
        high = rows[i]["high"]
        low = rows[i]["low"]
        prev_close = rows[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return None
    a = sum(trs[:period]) / Decimal(period)
    for tr in trs[period:]:
        a = (a * Decimal(period - 1) + tr) / Decimal(period)
    return a


def macd(values: List[Decimal]) -> Dict[str, Optional[float]]:
    if len(values) < 35:
        return {"macd": None, "signal": None, "histogram": None, "bias": "unknown"}
    # Build macd line sequence using sliding EMA approximation.
    macd_line: List[Decimal] = []
    for i in range(26, len(values) + 1):
        e12 = ema(values[:i], 12)
        e26 = ema(values[:i], 26)
        if e12 is not None and e26 is not None:
            macd_line.append(e12 - e26)
    signal = ema(macd_line, 9) if len(macd_line) >= 9 else None
    m = macd_line[-1] if macd_line else None
    hist = (m - signal) if m is not None and signal is not None else None
    bias = "bullish" if hist is not None and hist > 0 else "bearish" if hist is not None and hist < 0 else "unknown"
    return {"macd": _f(m), "signal": _f(signal), "histogram": _f(hist), "bias": bias}


def calculate_indicators(raw_klines: List[Any]) -> Dict[str, Any]:
    rows = normalize_klines(raw_klines)
    closes = [r["close"] for r in rows]
    volumes = [r["volume"] for r in rows]
    if not closes:
        return {"status": "no_data"}
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    r14 = rsi(closes, 14)
    a14 = atr(rows, 14)
    v20 = sma(volumes, 20)
    last_close = closes[-1]
    trend = "unknown"
    if e20 is not None and e50 is not None:
        if last_close > e20 > e50:
            trend = "uptrend"
        elif last_close < e20 < e50:
            trend = "downtrend"
        else:
            trend = "sideway/mixed"
    volume_status = "unknown"
    if v20 is not None and volumes:
        if volumes[-1] > v20 * Decimal("1.2"):
            volume_status = "above_average"
        elif volumes[-1] < v20 * Decimal("0.8"):
            volume_status = "below_average"
        else:
            volume_status = "normal"
    return {
        "status": "ok",
        "last_close": _f(last_close),
        "trend": trend,
        "ema20": _f(e20),
        "ema50": _f(e50),
        "ema200": _f(e200),
        "rsi14": _f(r14),
        "atr14": _f(a14),
        "volume_ma20": _f(v20),
        "volume_status": volume_status,
        "macd": macd(closes),
    }
