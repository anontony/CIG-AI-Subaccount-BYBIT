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
        r'(?:vong\s*lap|loop|quet|scan|chu\s*ky)[^\n]{0,40}?(?:sau\s*moi|moi|every|each)\s*(\d+(?:\.\d+)?)\s*(giay|phut|tieng|gio|hour|hours|hr|hrs|minute|minutes|min|mins|second|seconds|sec|secs|day|days|ngay|h|m|s|d)\b',
        r'lap\s*lai\s*sau\s*(\d+(?:\.\d+)?)\s*(giay|phut|tieng|gio|hour|hours|minute|minutes|day|days|ngay|h|m|s|d)\b',
        r'chu\s*ky\s*(?:quet|scan|bot|chien\s*luoc)?\s*(\d+(?:\.\d+)?)\s*(giay|phut|tieng|gio|hour|hours|minute|minutes|day|days|ngay|h|m|s|d)\b',
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
        aliases = {'D1': '1D', '1D': '1D', 'H4': '4H', '4H': '4H', 'H1': '1H', '1H': '1H', 'M30': '30M', '30M': '30M', 'M15': '15M', '15M': '15M', 'M5': '5M', '5M': '5M'}
        tf = aliases.get(raw, raw)
        if tf not in found:
            found.append(tf)

    for raw in re.findall(r'\b(?:d1|1d|h4|4h|h1|1h|m30|30m|m15|15m|m5|5m)\b', ascii_text, flags=re.IGNORECASE):
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
        order = ['1D', '4H', '1H', '30M', '15M', '5M']
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
        'primary_timeframe': '',
        'strict_prompt_only': False,
        'prompt_only_mode': False,
        'allowed_timeframes': [],
        'allowed_indicators': [],
        'exact_rsi_candle_strategy': False,
        'rsi_long_below': None,
        'rsi_short_above': None,
        'candle_confirm_count': None,
    }

    interval_seconds, interval_label = _parse_interval_seconds(ascii_text)
    out['interval_seconds'] = interval_seconds
    out['interval_label'] = interval_label
    out['timeframe'] = _parse_timeframe(ascii_text)
    tf_text = str(out.get('timeframe') or '').upper()
    if '5M' in tf_text:
        out['primary_timeframe'] = '5m'
    elif '15M' in tf_text:
        out['primary_timeframe'] = '15m'
    elif '1H' in tf_text:
        out['primary_timeframe'] = '1h'
    elif '4H' in tf_text:
        out['primary_timeframe'] = '4h'
    elif '1D' in tf_text:
        out['primary_timeframe'] = '1d'

    if any(k in ascii_text for k in ['spot', 'giao ngay', 'nam giu', 'hold coin']):
        out['market'] = 'spot'
    elif any(k in ascii_text for k in ['future', 'futures', 'hop dong', 'perp', 'perpetual', 'long', 'short', 'don bay', 'donbay', 'bay x', 'bayx', 'leverage']):
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

    if out.get('leverage') and not out.get('market'):
        # A prompt that uses leverage/x20 is a derivatives/futures prompt unless it explicitly says spot.
        out['market'] = 'linear'

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
            r'von\s*(?:moi\s*lenh|cho\s*moi\s*lenh|lenh)',
            r'vốn\s*(?:mỗi\s*lệnh|cho\s*mỗi\s*lệnh|lệnh)',
        ]
        labels_after = [
            r'margin_usdt',
            r'margin\s*futures',
            r'margin\s*(?:moi\s*lenh|cho\s*moi\s*lenh|lenh)',
            r'ky\s*quy\s*(?:moi\s*lenh|cho\s*moi\s*lenh|lenh)?',
            r'von\s*(?:moi\s*lenh|cho\s*moi\s*lenh|lenh)',
            r'vốn\s*(?:mỗi\s*lệnh|cho\s*mỗi\s*lệnh|lệnh)',
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
        r'\btp\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*(?:%|phan\s*tram\b|percent\b)',
        r'take\s*profit\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*(?:%|phan\s*tram\b|percent\b)',
        r'chot\s*loi\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*(?:%|phan\s*tram\b|percent\b)',
    ], ascii_text)
    out['stop_loss_pct'] = _parse_pct([
        r'\bsl\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*(?:%|phan\s*tram\b|percent\b)',
        r'stop\s*loss\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*(?:%|phan\s*tram\b|percent\b)',
        r'cat\s*lo\s*[:=]?\s*(\d+(?:[\.,]\d+)?)\s*(?:%|phan\s*tram\b|percent\b)',
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
    out['allowed_indicators'] = list(out['indicators'])

    # V47 Prompt-Only Mode:
    # If the user explicitly names indicators/timeframe, the app must not inject
    # unmentioned strategy logic into the AI decision. Example: "RSI 5m 66-21"
    # must be evaluated with RSI on 5m only, not D1/H4/H1 EMA/MACD.
    if out.get('primary_timeframe'):
        out['allowed_timeframes'] = [out['primary_timeframe']]
    elif out.get('timeframe'):
        tf_map = {'5M':'5m','15M':'15m','30M':'30m','1H':'1h','4H':'4h','1D':'1d'}
        out['allowed_timeframes'] = [tf_map[x] for x in str(out.get('timeframe')).upper().split('/') if x in tf_map]
    indicator_words = ['rsi','ema','macd','atr','volume','vol','bollinger','bb','sma','vwap']
    if out.get('allowed_timeframes') or any(k in ascii_text for k in indicator_words):
        out['prompt_only_mode'] = True
        out['strict_prompt_only'] = True

    # Parse common Vietnamese RSI shorthand: "quá mua/quá bán ở 66-21",
    # "RSI oversold 21 overbought 66", etc. High number = overbought/short,
    # low number = oversold/long when both terms appear.
    if 'rsi' in ascii_text and any(term in ascii_text for term in ['qua mua','qua ban','overbought','oversold']):
        nums = []
        window_match = re.search(r'rsi[^\n]{0,120}', ascii_text)
        window = window_match.group(0) if window_match else ascii_text
        for raw in re.findall(r'\b(\d{1,3}(?:\.\d+)?)\b', window):
            val = _num_or_none(raw)
            if val is not None and 0 <= val <= 100:
                nums.append(val)
        if 'qua mua' in ascii_text or 'overbought' in ascii_text:
            m = re.search(r'(?:qua\s*mua|overbought)[^0-9]{0,30}(\d{1,3}(?:\.\d+)?)', ascii_text)
            if m:
                out['rsi_short_above'] = _num_or_none(m.group(1))
        if 'qua ban' in ascii_text or 'oversold' in ascii_text:
            m = re.search(r'(?:qua\s*ban|oversold)[^0-9]{0,30}(\d{1,3}(?:\.\d+)?)', ascii_text)
            if m:
                out['rsi_long_below'] = _num_or_none(m.group(1))
        if len(nums) >= 2 and ('qua mua' in ascii_text or 'overbought' in ascii_text) and ('qua ban' in ascii_text or 'oversold' in ascii_text):
            # In shorthand like "quá mua/quá bán 66-21", use the explicit pair
            # first. Do not let unrelated numbers such as 10 USDT or x20 become RSI thresholds.
            pair = re.search(r'\b(\d{1,3}(?:\.\d+)?)\s*[-/]\s*(\d{1,3}(?:\.\d+)?)\b', window)
            if pair:
                a = _num_or_none(pair.group(1)); b = _num_or_none(pair.group(2))
                vals = [x for x in (a, b) if x is not None and 0 <= x <= 100]
            else:
                current_low = out.get('rsi_long_below')
                current_high = out.get('rsi_short_above')
                if current_low is not None and current_high is not None and 10 <= float(current_low) <= 45 and 55 <= float(current_high) <= 90:
                    vals = [float(current_high), float(current_low)]
                else:
                    lows = [x for x in nums if 10 <= x <= 45]
                    highs = [x for x in nums if 55 <= x <= 90]
                    vals = ([max(highs)] if highs else []) + ([min(lows)] if lows else [])
            if len(vals) >= 2:
                out['rsi_short_above'] = max(vals)
                out['rsi_long_below'] = min(vals)

    rsi_rules = []
    rsi_patterns = [
        r'rsi[^0-9<>]{0,50}(?:<|duoi|nho\s*hon|below|under)\s*(\d+(?:\.\d+)?)',
        r'rsi[^0-9<>]{0,50}(?:>|tren|lon\s*hon|above|over)\s*(\d+(?:\.\d+)?)',
        r'rsi[^0-9<>]{0,50}(?:<=|nho\s*hon\s*hoac\s*bang)\s*(\d+(?:\.\d+)?)',
        r'rsi[^0-9<>]{0,50}(?:>=|lon\s*hon\s*hoac\s*bang)\s*(\d+(?:\.\d+)?)',
    ]
    for p in rsi_patterns:
        m = re.search(p, ascii_text, flags=re.IGNORECASE)
        if m:
            snippet = re.search(r'rsi[^\n,;]{0,40}', ascii_text)
            rsi_rules.append((snippet.group(0) if snippet else f'RSI {m.group(1)}').strip())
    out['rsi_rules'] = list(dict.fromkeys(rsi_rules))

    # Generic RSI threshold extraction for prompts such as:
    # "RSI nhỏ hơn 27 thì Long" / "RSI lớn hơn 66 thì Short".
    lm = re.search(r'rsi[^\n]{0,90}(?:<|duoi|nho\s*hon|below|under)\s*(\d+(?:\.\d+)?)', ascii_text, flags=re.IGNORECASE)
    sm = re.search(r'rsi[^\n]{0,90}(?:>|tren|lon\s*hon|above|over)\s*(\d+(?:\.\d+)?)', ascii_text, flags=re.IGNORECASE)
    if lm and out.get('rsi_long_below') is None:
        out['rsi_long_below'] = _num_or_none(lm.group(1))
    if sm and out.get('rsi_short_above') is None:
        out['rsi_short_above'] = _num_or_none(sm.group(1))

    # Candle confirmation directives for low-timeframe RSI strategies, e.g.
    # "RSI 5m < 27 + 2 nến xanh thì Long".
    candle_rules = []
    if any(term in ascii_text for term in ['2 cay nen', 'hai cay nen', '2 nen', 'hai nen']):
        if any(term in ascii_text for term in ['xanh', 'green']):
            candle_rules.append('2_green_candles')
        if any(term in ascii_text for term in ['do', 'red']):
            candle_rules.append('2_red_candles')
    if candle_rules:
        out['candle_rules'] = candle_rules

    # Strict low-timeframe RSI+candle strategies must not be rewritten by any
    # default D1/H4/H1 strategy prompt. Example: "RSI 5m < 27 + 2 green candles => Long; RSI 5m > 66 + 2 red candles => Short".
    # When this is detected, execution code uses only 5m RSI + candle colors;
    # unmentioned EMA/MACD/D1/H4/H1 data can be logged but must not authorize a trade.
    low_tf_rsi = (('5m' in ascii_text or 'm5' in ascii_text or '5 phut' in ascii_text or 'khung thoi gian 5' in ascii_text) and 'rsi' in ascii_text)
    two_candle = any(term in ascii_text for term in ['2 cay', '2 nen', 'hai cay', 'hai nen'])
    if low_tf_rsi and two_candle:
        long_match = re.search(r'rsi[^\n]{0,90}(?:<|duoi|nho\s*hon|below|under)\s*(\d+(?:\.\d+)?)[^\n]{0,160}(?:xanh|green)[^\n]{0,160}(?:long|mua)', ascii_text, flags=re.IGNORECASE)
        if not long_match:
            long_match = re.search(r'(?:long|mua)[^\n]{0,160}rsi[^\n]{0,90}(?:<|duoi|nho\s*hon|below|under)\s*(\d+(?:\.\d+)?)', ascii_text, flags=re.IGNORECASE)
        short_match = re.search(r'rsi[^\n]{0,90}(?:>|tren|lon\s*hon|above|over)\s*(\d+(?:\.\d+)?)[^\n]{0,160}(?:do|red)[^\n]{0,160}(?:short|ban)', ascii_text, flags=re.IGNORECASE)
        if not short_match:
            short_match = re.search(r'(?:short|ban)[^\n]{0,160}rsi[^\n]{0,90}(?:>|tren|lon\s*hon|above|over)\s*(\d+(?:\.\d+)?)', ascii_text, flags=re.IGNORECASE)
        if long_match:
            out['rsi_long_below'] = _num_or_none(long_match.group(1))
        if short_match:
            out['rsi_short_above'] = _num_or_none(short_match.group(1))
        if long_match or short_match or out.get('rsi_long_below') is not None or out.get('rsi_short_above') is not None:
            out['exact_rsi_candle_strategy'] = True
            out['strict_prompt_only'] = True
            out['prompt_only_mode'] = True
            out['primary_timeframe'] = out.get('primary_timeframe') or '5m'
            out['allowed_timeframes'] = ['5m']
            out['allowed_indicators'] = ['RSI']
            out['candle_confirm_count'] = 2
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
    if meta.get('prompt_only_mode'):
        tfs = ','.join(meta.get('allowed_timeframes') or []) or (meta.get('primary_timeframe') or '-')
        inds = ','.join(meta.get('allowed_indicators') or []) or '-'
        parts.append(f'Prompt-only: chỉ dùng {tfs} / {inds}')
    return ' · '.join(parts) if parts else 'Prompt đã lưu nhưng chưa bóc tách được nhiều chỉ dẫn cấu trúc. AI vẫn sẽ đọc nguyên prompt khi phân tích.'
