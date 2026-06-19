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
    patterns = [
        r'(?:khung|timeframe|frame|tf)\s*(\d+)\s*([mhd])\b',
        r'(?:khung|timeframe|frame|tf)\s*([mhd])(\d+)\b',
        r'\b([mhd])(\d+)\b',
    ]
    for pattern in patterns:
        m = re.search(pattern, ascii_text, flags=re.IGNORECASE)
        if not m:
            continue
        if len(m.groups()) == 2:
            a, b = m.group(1), m.group(2)
            if a.isdigit():
                return f'{a}{b.lower()}'
            return f'{b}{a.lower()}'
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

    amounts = re.findall(r'(\d+(?:\.\d+)?)\s*(?:u|usdt|usd|do|đo|đô|dollar|dola)\b', original, flags=re.IGNORECASE)
    if amounts:
        amount = _clean_num(amounts[0])
        if out['market'] == 'spot':
            out['spot_order_usdt'] = amount
        elif out['market'] == 'linear':
            out['futures_margin_usdt'] = amount
        else:
            out['spot_order_usdt'] = amount

    out['take_profit_pct'] = _parse_pct([
        r'\btp\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*%?',
        r'take\s*profit\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*%?',
        r'chot\s*loi\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*%?',
    ], ascii_text)
    out['stop_loss_pct'] = _parse_pct([
        r'\bsl\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*%?',
        r'stop\s*loss\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*%?',
        r'cat\s*lo\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*%?',
    ], ascii_text)

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
    if meta.get('rsi_rules'):
        parts.append('RSI: ' + '; '.join(meta['rsi_rules'][:2]))
    if meta.get('indicators'):
        parts.append('Chỉ báo: ' + ', '.join(meta['indicators'][:5]))
    return ' · '.join(parts) if parts else 'Prompt đã lưu nhưng chưa bóc tách được nhiều chỉ dẫn cấu trúc. AI vẫn sẽ đọc nguyên prompt khi phân tích.'
