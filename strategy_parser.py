import re
import unicodedata
from typing import Any, Dict, List, Optional


def _strip_accents(text: str) -> str:
    text = text.replace('đ', 'd').replace('Đ', 'D')
    normalized = unicodedata.normalize('NFD', text)
    return ''.join(ch for ch in normalized if unicodedata.category(ch) != 'Mn')


def _clean_num(raw: str) -> str:
    return raw.strip().replace(',', '.').replace('%', '')


def _num_or_none(raw: str) -> Optional[float]:
    try:
        return float(_clean_num(raw))
    except Exception:
        return None


def _find_symbols(text: str, allowed_symbols: Optional[List[str]] = None) -> List[str]:
    upper = text.upper()
    ascii_text = _strip_accents(text.lower())
    found: List[str] = []
    allowed = [s.upper().strip() for s in (allowed_symbols or []) if str(s).strip()]
    alias_map = {
        'BTCUSDT': ['bitcoin', 'btc'],
        'ETHUSDT': ['ethereum', 'ether', 'eth'],
        'SOLUSDT': ['solana', 'sol'],
        'XRPUSDT': ['xrp', 'ripple'],
        'BNBUSDT': ['bnb', 'binance coin'],
        'ADAUSDT': ['ada', 'cardano'],
        'DOGEUSDT': ['doge', 'dogecoin'],
    }
    for sym in allowed:
        if sym in upper:
            found.append(sym)
            continue
        base = sym.replace('USDT', '')
        if re.search(rf'\b{re.escape(base)}\b', upper):
            found.append(sym)
            continue
        for alias in alias_map.get(sym, []):
            if re.search(rf'\b{re.escape(alias)}\b', ascii_text):
                found.append(sym)
                break
    for raw in re.findall(r'\b[A-Z]{2,10}USDT\b', upper):
        if raw not in found:
            found.append(raw)
    return list(dict.fromkeys(found))


def _parse_interval_seconds(ascii_text: str) -> tuple[Optional[int], str]:
    unit_map = {
        's': 1, 'sec': 1, 'secs': 1, 'second': 1, 'seconds': 1, 'giay': 1,
        'm': 60, 'min': 60, 'mins': 60, 'minute': 60, 'minutes': 60, 'phut': 60,
        'h': 3600, 'hr': 3600, 'hrs': 3600, 'hour': 3600, 'hours': 3600, 'tieng': 3600, 'gio': 3600,
        'd': 86400, 'day': 86400, 'days': 86400, 'ngay': 86400,
    }

    # Common shorthand users type in trading prompts:
    # "10 usdt/1h", "10u / 1h", "10 USDT mỗi giờ", "hourly".
    shorthand_patterns = [
        r'/\s*(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds|d|day|days|gio|tieng|phut|giay|ngay)\b',
        r'per\s*(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds|d|day|days)\b',
        r'\b(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds|d|day|days|gio|tieng|phut|giay|ngay)\s*/\s*(?:lenh|trade|order|lan)\b',
    ]
    patterns = [
        r'(?:moi|cu\s*moi|every|each)\s*(\d+(?:\.\d+)?)\s*(giay|phut|tieng|gio|hour|hours|hr|hrs|minute|minutes|min|mins|second|seconds|sec|secs|day|days|ngay|h|m|s|d)\b',
        r'lap\s*lai\s*sau\s*(\d+(?:\.\d+)?)\s*(giay|phut|tieng|gio|hour|hours|minute|minutes|day|days|ngay|h|m|s|d)\b',
        r'chu\s*ky\s*(\d+(?:\.\d+)?)\s*(giay|phut|tieng|gio|hour|hours|minute|minutes|day|days|ngay|h|m|s|d)\b',
        *shorthand_patterns,
    ]
    for pattern in patterns:
        m = re.search(pattern, ascii_text, flags=re.IGNORECASE)
        if m:
            value = _num_or_none(m.group(1))
            unit = m.group(2).lower()
            if value is None:
                continue
            seconds = int(value * unit_map.get(unit, 0))
            if seconds > 0:
                return seconds, f'{m.group(1)} {m.group(2)}'

    # Natural no-number hourly/daily phrases.
    no_number = [
        (['moi gio', 'moi tieng', 'hang gio', 'hourly', 'every hour', 'each hour'], 3600, '1h'),
        (['moi phut', 'hang phut', 'every minute'], 60, '1m'),
        (['moi ngay', 'hang ngay', 'daily', 'every day'], 86400, '1d'),
    ]
    for phrases, seconds, label in no_number:
        if any(p in ascii_text for p in phrases):
            return seconds, label
    return None, ''


def _parse_timeframe(ascii_text: str) -> str:
    found: list[str] = []

    def add(raw: str) -> None:
        raw = raw.upper()
        aliases = {'D1': '1D', '1D': '1D', 'H4': '4H', '4H': '4H', 'H1': '1H', '1H': '1H', 'M30': '30M', '30M': '30M', 'M15': '15M', '15M': '15M'}
        tf = aliases.get(raw, raw)
        if tf not in found:
            found.append(tf)

    for raw in re.findall(r'\b(?:d1|1d|h4|4h|h1|1h|m30|30m|m15|15m)\b', ascii_text, flags=re.IGNORECASE):
        add(raw)

    patterns = [
        r'(?:khung|timeframe|frame|tf)\s*(\d+)\s*([mhd])\b',
        r'(?:khung|timeframe|frame|tf)\s*([mhd])(\d+)\b',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, ascii_text, flags=re.IGNORECASE):
            a, b = m.group(1), m.group(2)
            add(f'{a}{b}' if a.isdigit() else f'{b}{a}')

    if found:
        order = ['1D', '4H', '1H', '30M', '15M']
        found = sorted(found, key=lambda x: order.index(x) if x in order else 99)
        return '/'.join(found[:4])
    return ''

def _parse_pct(label_patterns: List[str], ascii_text: str) -> Optional[str]:
    for pattern in label_patterns:
        m = re.search(pattern, ascii_text, flags=re.IGNORECASE)
        if m:
            val = _clean_num(m.group(1))
            return val
    return None


def parse_strategy_prompt(prompt: str, allowed_symbols: Optional[List[str]] = None) -> Dict[str, Any]:
    original = (prompt or '').strip()
    ascii_text = _strip_accents(original.lower())
    out: Dict[str, Any] = {
        'symbols': _find_symbols(original, allowed_symbols),
        'market': '',
        'interval_seconds': None,
        'interval_label': '',
        'timeframe': '',
        'leverage': None,
        'spot_order_usdt': None,
        'futures_margin_usdt': None,
        'take_profit_pct': None,
        'stop_loss_pct': None,
        'tp_sl_mode': '',
        'requires_explicit_tp_sl': False,
        'rsi_rules': [],
        'indicators': [],
    }

    interval_seconds, interval_label = _parse_interval_seconds(ascii_text)
    out['interval_seconds'] = interval_seconds
    out['interval_label'] = interval_label
    out['timeframe'] = _parse_timeframe(ascii_text)

    if any(k in ascii_text for k in ['spot', 'giao ngay', 'nam giu', 'hold coin']):
        out['market'] = 'spot'
    elif any(k in ascii_text for k in ['future', 'futures', 'hop dong', 'perp', 'perpetual', 'long', 'short', 'don bay', 'leverage']):
        out['market'] = 'linear'

    lev_patterns = [
        r'(?:don\s*bay|leverage|lev)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*x?',
        r'\bx\s*(\d+(?:\.\d+)?)\b',
        r'\b(\d+(?:\.\d+)?)\s*x\b',
    ]
    for p in lev_patterns:
        m = re.search(p, ascii_text, flags=re.IGNORECASE)
        if m:
            num = _num_or_none(m.group(1))
            if num and num > 0:
                out['leverage'] = int(num)
                break

    def _extract_money_with_context(text_original: str) -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        for m in re.finditer(r'(\d+(?:[\.,]\d+)?)\s*(?:u|usdt|usd|do|đo|đô|dollar|dola)\b', text_original, flags=re.IGNORECASE):
            s, e = m.span()
            # Use a local window rather than the whole paragraph/line. Strategy prompts
            # are often pasted as one long paragraph, and using the full line can make
            # a clear "margin futures mỗi lệnh: 8 USDT" get rejected because a later
            # sentence contains "risk mỗi lệnh: 1 USDT".
            ctx_start = max(0, s - 90)
            ctx_end = min(len(text_original), e + 90)
            ctx = _strip_accents(text_original[ctx_start:ctx_end].lower())
            results.append((_clean_num(m.group(1)), ctx))
        return results

    money_items = _extract_money_with_context(original)

    def _find_explicit_futures_margin(text: str) -> Optional[str]:
        """Find only amounts directly labeled as futures/order margin.

        This is deliberately stricter than generic money parsing so numbers such as
        risk_usdt=1, account equity=50, daily target, or max loss are never turned
        into order margin just because a nearby sentence mentions margin_usdt.
        """
        t = _strip_accents(text.lower())
        amount = r'(\d+(?:[\.,]\d+)?)'
        money = r'(?:u|usdt|usd|do|dong|dollar|dola)'
        labels_before = [
            r'margin_usdt',
            r'margin\s*futures\s*(?:moi\s*lenh|cho\s*moi\s*lenh|lenh)?',
            r'margin\s*(?:moi\s*lenh|cho\s*moi\s*lenh|lenh)',
            r'ky\s*quy\s*(?:moi\s*lenh|cho\s*moi\s*lenh|lenh)?',
            r'order\s*margin',
        ]
        labels_after = [
            r'margin_usdt',
            r'margin\s*futures',
            r'margin\s*(?:moi\s*lenh|cho\s*moi\s*lenh|lenh)',
            r'ky\s*quy\s*(?:moi\s*lenh|cho\s*moi\s*lenh|lenh)?',
        ]
        for label in labels_before:
            pattern = rf'\b(?:{label})\b\s*(?:=|:|la|là|mac\s*dinh|default|dung|su\s*dung|use)?\s*{amount}\s*{money}\b'
            m = re.search(pattern, t, flags=re.IGNORECASE)
            if m:
                return _clean_num(m.group(1))
        for label in labels_after:
            pattern = rf'\b{amount}\s*{money}\s*(?:cho|lam|la|as)?\s*(?:{label})\b'
            m = re.search(pattern, t, flags=re.IGNORECASE)
            if m:
                return _clean_num(m.group(1))
        return None

    explicit_futures_margin = _find_explicit_futures_margin(original)

    # These words mean the number is NOT order margin.
    # This prevents bugs like: "Account equity reference: 50" -> Margin Futures 50,
    # or "risk_usdt: 1" -> Margin Futures 1.
    not_margin_context = [
        'account equity', 'equity reference', 'von mau', 'von ban dau', 'von tham chieu',
        'so du', 'tai khoan', 'muc tieu', 'lo toi da', 'muc tieu loi', 'ngay',
        'risk moi lenh', 'risk toi da', 'risk_usdt', 'rui ro', 'chap nhan lo',
        'take-profit', 'take profit', 'stop-loss', 'stop loss', 'tp ', 'sl ',
        'position_notional', 'notional uoc tinh', 'notional', 'max_notional',
    ]
    margin_context = [
        'margin futures moi lenh', 'margin moi lenh', 'margin futures', 'margin_usdt',
        'ky quy moi lenh', 'ky quy', 'margin can dung', 'dung margin',
        'von vao lenh', 'von moi lenh', 'order margin', 'margin required',
    ]
    spot_context = [
        'spot size', 'spot_order_usdt', 'mua spot', 'spot mua', 'mua bitcoin spot',
        'mua btc spot', 'giao ngay', 'spot moi lenh',
    ]

    def _pick_money(require: list[str], avoid: list[str] | None = None) -> Optional[str]:
        avoid = avoid or []
        for amount, ctx in money_items:
            if any(term in ctx for term in require) and not any(term in ctx for term in avoid):
                return amount
        return None

    if out['market'] == 'spot':
        out['spot_order_usdt'] = _pick_money(spot_context)
        if not out['spot_order_usdt'] and len(original) < 350 and money_items:
            out['spot_order_usdt'] = money_items[0][0]
    elif out['market'] == 'linear':
        out['futures_margin_usdt'] = explicit_futures_margin or _pick_money(margin_context, not_margin_context)
        if not out['futures_margin_usdt'] and len(original) < 350 and money_items:
            for amount, ctx in money_items:
                if not any(term in ctx for term in not_margin_context):
                    out['futures_margin_usdt'] = amount
                    break
    else:
        out['spot_order_usdt'] = _pick_money(spot_context)
        out['futures_margin_usdt'] = explicit_futures_margin or _pick_money(margin_context, not_margin_context)

    # Only treat TP/SL as percentage when the prompt explicitly uses % / percent.
    # This prevents strategy text like "take-profit 1.5R" or "1.2 ATR"
    # from being misread as TP 1.5% / TP 1.2%.
    out['take_profit_pct'] = _parse_pct([
        r'\btp\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*(?:%|phan\s*tram|percent)\b',
        r'take\s*profit\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*(?:%|phan\s*tram|percent)\b',
        r'chot\s*loi\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*(?:%|phan\s*tram|percent)\b',
    ], ascii_text)
    out['stop_loss_pct'] = _parse_pct([
        r'\bsl\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*(?:%|phan\s*tram|percent)\b',
        r'stop\s*loss\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*(?:%|phan\s*tram|percent)\b',
        r'cat\s*lo\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*(?:%|phan\s*tram|percent)\b',
    ], ascii_text)

    # Strategy TP/SL mode. If the user describes ATR/R-multiple/structure exits,
    # the execution layer must require explicit AI-computed TP/SL prices and must
    # not silently fall back to dashboard default TP/SL percentages.
    rr_or_atr_terms = [
        '1.5r', '2r', ' rr', 'r:r', 'risk/reward', 'risk reward',
        'atr', 'atr14', 'cau truc', 'structure', 'day gan nhat', 'dinh gan nhat',
        'vung ho tro', 'vung khang cu', 'stop-loss phai co', 'stop loss phai co',
        'take-profit toi thieu', 'take profit toi thieu', 'tp toi thieu',
        'khong duoc vao lenh neu thieu stop_loss', 'khong duoc vao lenh neu thieu stop loss',
        'khong duoc vao lenh neu thieu take_profit', 'khong duoc vao lenh neu thieu take profit',
        'khong dung tp phan tram co dinh', 'khong dung tp/sl mac dinh',
        'khong duoc dung tpsl mac dinh', 'khong duoc dung tp/sl mac dinh',
    ]
    if any(term in ascii_text for term in rr_or_atr_terms):
        out['tp_sl_mode'] = 'explicit_price_required'
        out['requires_explicit_tp_sl'] = True

    indicators = []
    for key in ['rsi', 'ema', 'macd', 'atr', 'volume', 'vol', 'bollinger', 'bb', 'sma']:
        if key in ascii_text:
            indicators.append(key.upper())
    out['indicators'] = list(dict.fromkeys(indicators))

    rsi_rules = []
    rsi_patterns = [
        r'rsi\s*(?:<|duoi|below|under)\s*(\d+(?:\.\d+)?)',
        r'rsi\s*(?:>|tren|above|over)\s*(\d+(?:\.\d+)?)',
        r'rsi\s*(?:<=|nho\s*hon\s*hoac\s*bang)\s*(\d+(?:\.\d+)?)',
        r'rsi\s*(?:>=|lon\s*hon\s*hoac\s*bang)\s*(\d+(?:\.\d+)?)',
    ]
    for p in rsi_patterns:
        m = re.search(p, ascii_text, flags=re.IGNORECASE)
        if m:
            snippet = re.search(r'rsi[^\n,;]{0,40}', ascii_text)
            rsi_rules.append((snippet.group(0) if snippet else f'RSI {m.group(1)}').strip())
    out['rsi_rules'] = list(dict.fromkeys(rsi_rules))
    return out


def summarize_strategy_directives(meta: Dict[str, Any]) -> str:
    if not meta:
        return 'Chưa nhận diện được chỉ dẫn cụ thể từ prompt.'
    parts: List[str] = []
    if meta.get('market') == 'spot':
        parts.append('Thị trường: Spot')
    elif meta.get('market') == 'linear':
        parts.append('Thị trường: Futures linear')
    if meta.get('symbols'):
        parts.append('Coin: ' + ', '.join(meta['symbols']))
    if meta.get('interval_seconds'):
        parts.append(f"Chu kỳ: {meta.get('interval_label')} ({meta['interval_seconds']} giây)")
    if meta.get('timeframe'):
        parts.append('Khung phân tích: ' + str(meta['timeframe']).upper())
    if meta.get('leverage'):
        parts.append(f"Đòn bẩy: {meta['leverage']}x")
    if meta.get('spot_order_usdt'):
        parts.append(f"Spot size: {meta['spot_order_usdt']} USDT")
    if meta.get('futures_margin_usdt'):
        parts.append(f"Margin Futures: {meta['futures_margin_usdt']} USDT")
    if meta.get('take_profit_pct'):
        parts.append(f"TP: {meta['take_profit_pct']}%")
    if meta.get('stop_loss_pct'):
        parts.append(f"SL: {meta['stop_loss_pct']}%")
    if meta.get('requires_explicit_tp_sl'):
        parts.append('TP/SL: yêu cầu giá cụ thể, không dùng mặc định')
    if meta.get('rsi_rules'):
        parts.append('RSI: ' + '; '.join(meta['rsi_rules'][:2]))
    if meta.get('indicators'):
        parts.append('Chỉ báo: ' + ', '.join(meta['indicators'][:5]))
    return ' · '.join(parts) if parts else 'Prompt đã lưu nhưng chưa bóc tách được nhiều chỉ dẫn cấu trúc. AI vẫn sẽ đọc nguyên prompt khi phân tích.'
