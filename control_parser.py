import re
import unicodedata
from typing import Any, Dict, List

SECRET_WORDS = ["api key", "api secret", "secret", "openai", "sk-", "bybit"]


def strip_accents(text: str) -> str:
    text = text.replace("đ", "d").replace("Đ", "D")
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _num(raw: str) -> str:
    return raw.strip().replace(",", "")


def _bool_from_text(ascii_text: str) -> Any:
    if any(x in ascii_text for x in ["bat", "mo", "enable", "on", "true", "dung", "co"]):
        return True
    if any(x in ascii_text for x in ["tat", "disable", "off", "false", "khong"]):
        return False
    return None


def _extract_after(text: str, keys: List[str]) -> str:
    low = text.lower()
    for key in keys:
        pos = low.find(key.lower())
        if pos >= 0:
            return text[pos + len(key):].strip(" :：=\n\t")
    return ""


def redact_command_for_log(command: str) -> str:
    safe = command.strip()
    # Redact common key formats and direct assignments to key/secret fields.
    safe = re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-***", safe)
    safe = re.sub(r"(?i)(api\s*key|api\s*secret|secret|openai\s*key|bybit\s*key)\s*[:=]\s*\S+", r"\1=***", safe)
    if len(safe) > 260:
        safe = safe[:260] + "..."
    return safe


def parse_control_command(command: str, current_settings: Dict[str, Any], current_prompt: str = "") -> Dict[str, Any]:
    """Parse Vietnamese natural commands that modify bot workspace state.

    Returns:
      {"matched": bool, "settings": {}, "prompt": optional str, "message": str, "warnings": []}

    This parser intentionally handles bot-configuration commands before trading execution,
    so users can type commands such as:
    - đổi đòn bẩy tối đa thành 10x
    - chỉ trade BTCUSDT, ETHUSDT
    - đổi prompt thành: ...
    - bật dry run / tắt dry run
    - đổi môi trường sang testnet
    """
    raw = (command or "").strip()
    text = raw.lower()
    ascii_text = strip_accents(text)
    settings: Dict[str, Any] = {}
    warnings: List[str] = []
    prompt_update = None
    matched_reasons: List[str] = []

    # Prompt replacement / update
    prompt_value = _extract_after(raw, [
        "đổi prompt thành", "doi prompt thanh", "set prompt to", "prompt:", "prompt =", "prompt=",
        "cập nhật prompt thành", "cap nhat prompt thanh", "thay prompt thành", "thay prompt thanh",
    ])
    if prompt_value:
        prompt_update = prompt_value.strip()
        matched_reasons.append("Đã nhận lệnh thay prompt.")

    # Add to current prompt
    append_value = _extract_after(raw, [
        "thêm vào prompt", "them vao prompt", "bổ sung prompt", "bo sung prompt", "append prompt",
    ])
    if append_value:
        base = (current_prompt or "").strip()
        prompt_update = (base + "\n" + append_value.strip()).strip() if base else append_value.strip()
        matched_reasons.append("Đã nhận lệnh bổ sung prompt.")

    # Nếu user đang thay/bổ sung prompt, không bóc tách thêm các setting nằm bên trong nội dung prompt.
    # Ví dụ: "đổi prompt thành: Chỉ trade BTCUSDT..." chỉ thay prompt, không tự sửa allowed_symbols.
    if prompt_update is not None:
        return {
            "matched": True,
            "settings": settings,
            "prompt": prompt_update,
            "message": " ".join(matched_reasons),
            "warnings": warnings,
        }

    # Allowed symbols: "chỉ trade BTCUSDT, ETHUSDT" / "allowed symbols BTC, ETH" / "thêm XRP vào symbol"
    only_symbols_match = re.search(r"(?:chi\s*(?:trade|giao\s*dich)|allowed\s*symbols?|symbols?|cap\s*nhat\s*symbol|doi\s*symbol|danh\s*sach\s*symbol)\s*(?:la|thanh|:|=|gom|gồm)?\s*([a-z0-9,\s/]+)", ascii_text)
    if only_symbols_match:
        symbols = _normalize_symbols(only_symbols_match.group(1))
        if symbols:
            settings["allowed_symbols"] = ",".join(symbols)
            matched_reasons.append("Đã cập nhật danh sách coin được phép giao dịch.")

    add_sym_match = re.search(r"(?:them|add)\s+([a-z0-9,\s/]+)\s+(?:vao\s+)?(?:symbol|coin|allowed)", ascii_text)
    if add_sym_match:
        current = _normalize_symbols(str(current_settings.get("allowed_symbols") or ""))
        added = _normalize_symbols(add_sym_match.group(1))
        merged = list(dict.fromkeys(current + added))
        if added:
            settings["allowed_symbols"] = ",".join(merged)
            matched_reasons.append("Đã thêm coin vào danh sách được phép giao dịch.")

    remove_sym_match = re.search(r"(?:xoa|bo|remove)\s+([a-z0-9,\s/]+)\s+(?:khoi\s+)?(?:symbol|coin|allowed)", ascii_text)
    if remove_sym_match:
        current = _normalize_symbols(str(current_settings.get("allowed_symbols") or ""))
        removed = set(_normalize_symbols(remove_sym_match.group(1)))
        remain = [x for x in current if x not in removed]
        if removed:
            settings["allowed_symbols"] = ",".join(remain)
            matched_reasons.append("Đã xoá coin khỏi danh sách được phép giao dịch.")

    # Max leverage
    lev = _find_number([
        r"(?:don\s*bay\s*toi\s*da|max\s*leverage|leverage\s*toi\s*da|max\s*don\s*bay)\D*(\d+)",
        r"(?:gioi\s*han\s*don\s*bay)\D*(\d+)",
    ], ascii_text)
    if lev:
        settings["max_leverage"] = int(float(lev))
        matched_reasons.append(f"Đã đặt đòn bẩy tối đa {settings['max_leverage']}x.")

    # Max margin per trade
    margin = _find_number([
        r"(?:von\s*toi\s*da\s*(?:moi\s*lenh)?|max\s*margin(?:\s*per\s*trade)?|margin\s*toi\s*da)\D*(\d+(?:\.\d+)?)",
        r"(?:moi\s*lenh\s*toi\s*da)\D*(\d+(?:\.\d+)?)",
    ], ascii_text)
    if margin:
        settings["max_margin_per_trade_usdt"] = _num(margin)
        matched_reasons.append(f"Đã đặt vốn tối đa mỗi lệnh {settings['max_margin_per_trade_usdt']} USDT.")

    # Max notional
    notional = _find_number([
        r"(?:notional\s*toi\s*da|max\s*notional|gia\s*tri\s*lenh\s*toi\s*da)\D*(\d+(?:\.\d+)?)",
    ], ascii_text)
    if notional:
        settings["max_notional_usdt"] = _num(notional)
        matched_reasons.append(f"Đã đặt giá trị vị thế tối đa {settings['max_notional_usdt']} USDT.")

    # Daily trades
    daily = _find_number([
        r"(?:toi\s*da|max)\D*(\d+)\D*(?:lenh\s*/?\s*ngay|lenh\s*mot\s*ngay|daily\s*trades)",
        r"(?:max\s*daily\s*trades)\D*(\d+)",
    ], ascii_text)
    if daily:
        settings["max_daily_trades"] = int(float(daily))
        matched_reasons.append(f"Đã đặt số lệnh tối đa mỗi ngày {settings['max_daily_trades']}.")

    # Loop interval / cooldown
    interval = _find_number([
        r"(?:chu\s*ky\s*bot|vong\s*lap|loop\s*interval|tan\s*suat\s*quyet\s*dinh)\D*(\d+)",
    ], ascii_text)
    if interval:
        settings["loop_interval_seconds"] = int(float(interval))
        matched_reasons.append(f"Đã đặt chu kỳ bot {settings['loop_interval_seconds']} giây.")

    cooldown = _find_number([
        r"(?:cooldown|khoang\s*cach\s*giua\s*cac\s*lenh|min\s*seconds\s*between\s*trades)\D*(\d+)",
    ], ascii_text)
    if cooldown:
        settings["min_seconds_between_trades"] = int(float(cooldown))
        matched_reasons.append(f"Đã đặt thời gian nghỉ giữa các lệnh {settings['min_seconds_between_trades']} giây.")

    # Env
    if re.search(r"\btestnet\b", ascii_text) and any(k in ascii_text for k in ["env", "moi truong", "bybit"]):
        settings["bybit_env"] = "testnet"
        matched_reasons.append("Đã chuyển môi trường Bybit sang testnet.")
    if re.search(r"\bmainnet\b", ascii_text) and any(k in ascii_text for k in ["env", "moi truong", "bybit"]):
        settings["bybit_env"] = "mainnet"
        matched_reasons.append("Đã chuyển môi trường Bybit sang mainnet.")
        warnings.append("Mainnet là tài khoản thật. Nếu tắt mô phỏng, bot sẽ gửi order thật khi lệnh qua Risk Guard.")

    # Category / market mode
    if any(k in ascii_text for k in ["category", "loai hop dong", "san pham", "che do", "thi truong", "market"]):
        if "auto" in ascii_text or "tu dong" in ascii_text or "spot va future" in ascii_text or "spot va futures" in ascii_text:
            settings["default_category"] = "auto"
            matched_reasons.append("Đã đặt chế độ thị trường tự động: AI tự chọn Spot hoặc Futures theo lệnh user.")
        elif "linear" in ascii_text or "future" in ascii_text or "futures" in ascii_text or "perp" in ascii_text or "hop dong" in ascii_text:
            settings["default_category"] = "linear"
            matched_reasons.append("Đã đặt chế độ mặc định là Futures linear.")
        elif "inverse" in ascii_text:
            settings["default_category"] = "inverse"
            matched_reasons.append("Đã đặt loại hợp đồng mặc định là inverse.")
        elif "spot" in ascii_text or "giao ngay" in ascii_text:
            settings["default_category"] = "spot"
            matched_reasons.append("Đã đặt chế độ mặc định là Spot.")

    # Dry-run / simulation toggle.
    if "dry" in ascii_text or "mo phong" in ascii_text or "gia lap" in ascii_text or "lenh that" in ascii_text or "live" in ascii_text:
        bool_value = _bool_from_text(ascii_text)
        wants_live = any(x in ascii_text for x in ["bat live", "lenh that", "trade that", "live trading", "tat dry", "tat mo phong", "tat gia lap"])
        wants_sim = any(x in ascii_text for x in ["bat dry", "bat mo phong", "mo mo phong", "enable dry", "dry run", "gia lap"])
        if wants_live:
            settings["dry_run"] = False
            warnings.append("Đã tắt mô phỏng. Lệnh sau khi qua Risk Guard có thể gửi order thật lên Bybit nếu API có quyền Trade.")
            matched_reasons.append("Đã chuyển sang chế độ lệnh thật.")
        elif wants_sim or bool_value is True:
            settings["dry_run"] = True
            matched_reasons.append("Đã bật chế độ mô phỏng DRY_RUN.")
        elif bool_value is False:
            settings["dry_run"] = False
            warnings.append("Đã tắt mô phỏng. Hãy chắc chắn đang dùng sub-account vốn nhỏ và API không có quyền Withdraw.")
            matched_reasons.append("Đã chuyển sang chế độ lệnh thật.")

    # Default TP/SL percentages
    tp_default = _find_number([
        r"(?:tp|take\s*profit|chot\s*loi)\D*(?:mac\s*dinh|default)?\D*(\d+(?:\.\d+)?)\s*%?",
        r"(?:mac\s*dinh\s*tp|default\s*tp)\D*(\d+(?:\.\d+)?)\s*%?",
    ], ascii_text)
    sl_default = _find_number([
        r"(?:sl|stop\s*loss|cat\s*lo)\D*(?:mac\s*dinh|default)?\D*(\d+(?:\.\d+)?)\s*%?",
        r"(?:mac\s*dinh\s*sl|default\s*sl)\D*(\d+(?:\.\d+)?)\s*%?",
    ], ascii_text)
    if tp_default and any(k in ascii_text for k in ["mac dinh", "default", "tp", "take profit", "chot loi"]):
        settings["default_take_profit_pct"] = _num(tp_default)
        matched_reasons.append(f"Đã đặt TP mặc định {settings['default_take_profit_pct']}%.")
    if sl_default and any(k in ascii_text for k in ["mac dinh", "default", "sl", "stop loss", "cat lo"]):
        settings["default_stop_loss_pct"] = _num(sl_default)
        matched_reasons.append(f"Đã đặt SL mặc định {settings['default_stop_loss_pct']}%.")

    # Require TP/SL
    if any(k in ascii_text for k in ["tp/sl", "tp sl", "take profit", "stop loss", "chot loi", "cat lo"]):
        if any(k in ascii_text for k in ["bat buoc", "require", "can co"]):
            settings["require_tp_sl"] = True
            matched_reasons.append("Đã bật yêu cầu bắt buộc TP/SL.")
        elif any(k in ascii_text for k in ["khong bat buoc", "bo bat buoc", "tat yeu cau"]):
            settings["require_tp_sl"] = False
            matched_reasons.append("Đã tắt yêu cầu bắt buộc TP/SL.")
            warnings.append("Tắt TP/SL làm tăng rủi ro. Nên chỉ dùng khi hiểu rõ chiến lược.")

    # OpenAI model
    model_value = _extract_after(raw, ["đổi model thành", "doi model thanh", "openai model", "model:", "model="])
    if model_value and len(model_value.split()) <= 3:
        settings["openai_model"] = model_value.strip()
        matched_reasons.append(f"Đã cập nhật model OpenAI thành {settings['openai_model']}.")

    matched = bool(settings or prompt_update)
    return {
        "matched": matched,
        "settings": settings,
        "prompt": prompt_update,
        "message": " ".join(matched_reasons) if matched_reasons else "Không nhận diện là lệnh chỉnh bot.",
        "warnings": warnings,
    }


def _find_number(patterns: List[str], text: str) -> str:
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            return _num(m.group(1))
    return ""


def _normalize_symbols(raw: str) -> List[str]:
    # Normalize known coins to USDT perpetual symbols.
    raw = strip_accents(raw.upper())
    candidates = re.split(r"[,/\s]+", raw)
    out: List[str] = []
    stopwords = {"VA", "VOI", "THEM", "CHI", "TRADE", "GIAO", "DICH", "SYMBOL", "COIN", "ALLOWED", "VAO", "KHOI"}
    for item in candidates:
        item = item.strip().upper()
        if not item or item in stopwords:
            continue
        item = re.sub(r"[^A-Z0-9]", "", item)
        if not item or item in stopwords:
            continue
        if item in {"BTC", "ETH", "SOL", "XRP", "ADA", "BNB", "DOGE", "LINK", "AVAX", "TON", "MNT"}:
            item = item + "USDT"
        if re.match(r"^[A-Z0-9]{2,15}USDT$", item) and item not in out:
            out.append(item)
    return out
