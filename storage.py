import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from cryptography.fernet import Fernet
except Exception:  # pragma: no cover
    Fernet = None  # type: ignore


DEFAULT_SETTINGS: Dict[str, Any] = {
    "bybit_api_key": "",
    "bybit_api_secret": "",
    "bybit_api_private_key": "",
    "bybit_auth_type": "auto",
    "bybit_env": "testnet",
    "recv_window": "5000",
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
<<<<<<< HEAD
    "allowed_symbols": "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT",
=======
    "allowed_symbols": "BTCUSDT,ETHUSDT",
>>>>>>> 78582718eada4d79132653992ba32f60d5dbdc92
    "default_category": "auto",
    "dry_run": True,
    "max_leverage": 20,
    "max_margin_per_trade_usdt": "20",
    "max_notional_usdt": "300",
    "max_daily_trades": 5,
    "require_tp_sl": True,
    "default_take_profit_pct": "1.2",
    "default_stop_loss_pct": "0.6",
    "min_seconds_between_trades": 0,
    "loop_interval_seconds": 30,
    "ai_cost_saver": True,
    "last_prompt_trade_key": "",
    "last_prompt_trade_ts": "0",
}

SECRET_FIELDS = {"bybit_api_key", "bybit_api_secret", "bybit_api_private_key", "openai_api_key"}


class StoreError(RuntimeError):
    pass


class UserStore:
    """SQLite-backed user/workspace store.

    API keys are encrypted before being stored when cryptography is available.
    APP_SECRET should be set to a long random value in production/Railway.
    """

    def __init__(self, runtime_dir: Optional[str] = None) -> None:
        root = Path(runtime_dir or os.getenv("RUNTIME_DIR", "./data"))
        root.mkdir(parents=True, exist_ok=True)
        self.runtime_dir = root
        self.db_path = root / "app.db"
        self.app_secret = os.getenv("APP_SECRET", "dev-change-me-immediately")
        self._fernet = self._build_fernet(self.app_secret)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_settings (
                    user_id INTEGER PRIMARY KEY,
                    settings_json TEXT NOT NULL,
                    prompt TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS live_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    ts TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_live_logs_user_id_id ON live_logs(user_id, id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tracked_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    source TEXT NOT NULL DEFAULT 'dry_run',
                    action TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    category TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    current_price TEXT DEFAULT '',
                    qty TEXT DEFAULT '',
                    order_usdt TEXT DEFAULT '',
                    margin_usdt TEXT DEFAULT '',
                    leverage TEXT DEFAULT '1',
                    take_profit TEXT DEFAULT '',
                    stop_loss TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    normalized_json TEXT NOT NULL DEFAULT '{}',
                    execution_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracked_trades_user_status ON tracked_trades(user_id, status, id)")

    def _build_fernet(self, secret: str):
        if Fernet is None:
            return None
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        key = base64.urlsafe_b64encode(digest)
        return Fernet(key)

    def _hash_password(self, password: str, salt: Optional[str] = None) -> tuple[str, str]:
        if len(password) < 6:
            raise StoreError("Mật khẩu tối thiểu 6 ký tự.")
        salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
        return salt, base64.b64encode(digest).decode("ascii")

    def _encrypt(self, value: str) -> str:
        value = value or ""
        if not value:
            return ""
        if self._fernet is None:
            # Fallback is only obfuscation. Install cryptography for real encryption.
            return "plain:" + base64.b64encode(value.encode("utf-8")).decode("ascii")
        return "fernet:" + self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        value = value or ""
        if not value:
            return ""
        if value.startswith("fernet:") and self._fernet is not None:
            return self._fernet.decrypt(value.split(":", 1)[1].encode("ascii")).decode("utf-8")
        if value.startswith("plain:"):
            return base64.b64decode(value.split(":", 1)[1].encode("ascii")).decode("utf-8")
        # Legacy/plaintext compatibility.
        return value

    def _encode_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(settings)
        for field in SECRET_FIELDS:
            if data.get(field):
                data[field] = self._encrypt(str(data[field]))
        return data

    def _decode_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        data = {**DEFAULT_SETTINGS, **settings}
        for field in SECRET_FIELDS:
            if data.get(field):
                data[field] = self._decrypt(str(data[field]))
        return data

    def _redact_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(settings)
        for field in SECRET_FIELDS:
            raw = str(data.get(field) or "")
            data[field + "_set"] = bool(raw)
            if field == "bybit_api_key" and raw:
                data[field + "_masked"] = mask_secret(raw)
            elif field == "bybit_api_private_key" and raw:
                data[field + "_masked"] = "RSA key set"
            elif raw:
                data[field + "_masked"] = "set"
            else:
                data[field + "_masked"] = "not-set"
            data[field] = ""
        return data

    def create_user(self, username: str, password: str) -> Dict[str, Any]:
        username = normalize_username(username)
        if not username:
            raise StoreError("Username không hợp lệ.")
        salt, pw_hash = self._hash_password(password)
        now = int(time.time())
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO users(username, password_salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (username, salt, pw_hash, now),
                )
                user_id = int(cur.lastrowid)
                conn.execute(
                    "INSERT INTO workspace_settings(user_id, settings_json, prompt, updated_at) VALUES (?, ?, '', ?)",
                    (user_id, json.dumps(self._encode_settings(DEFAULT_SETTINGS), ensure_ascii=False), now),
                )
            return {"id": user_id, "username": username}
        except sqlite3.IntegrityError as exc:
            raise StoreError("Username đã tồn tại.") from exc

    def authenticate(self, username: str, password: str) -> Dict[str, Any]:
        username = normalize_username(username)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            raise StoreError("Sai username hoặc mật khẩu.")
        _, digest = self._hash_password(password, row["password_salt"])
        if not hmac.compare_digest(digest, row["password_hash"]):
            raise StoreError("Sai username hoặc mật khẩu.")
        return {"id": int(row["id"]), "username": row["username"]}

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT id, username, created_at FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return None
        return {"id": int(row["id"]), "username": row["username"], "created_at": int(row["created_at"])}

    def get_workspace(self, user_id: int, redact: bool = False) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT settings_json, prompt, updated_at FROM workspace_settings WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            raise StoreError("Workspace không tồn tại.")
        raw_settings = json.loads(row["settings_json"] or "{}")
        settings = self._decode_settings(raw_settings)
        if redact:
            settings = self._redact_settings(settings)
        return {"settings": settings, "prompt": row["prompt"] or "", "updated_at": int(row["updated_at"])}

    def save_prompt(self, user_id: int, prompt: str) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE workspace_settings SET prompt=?, updated_at=? WHERE user_id=?",
                (prompt.strip(), now, user_id),
            )

    def update_settings(self, user_id: int, incoming: Dict[str, Any]) -> Dict[str, Any]:
        ws = self.get_workspace(user_id, redact=False)
        settings = {**DEFAULT_SETTINGS, **ws["settings"]}

        for key, value in incoming.items():
            if key not in DEFAULT_SETTINGS:
                continue
            # Empty secret means keep current secret, not delete it.
            if key in SECRET_FIELDS and (value is None or str(value).strip() == ""):
                continue
            settings[key] = normalize_setting_value(key, value)

        now = int(time.time())
        encoded = self._encode_settings(settings)
        with self._connect() as conn:
            conn.execute(
                "UPDATE workspace_settings SET settings_json=?, updated_at=? WHERE user_id=?",
                (json.dumps(encoded, ensure_ascii=False), now, user_id),
            )
        return self._redact_settings(settings)

    def append_log(self, user_id: int, item: Dict[str, str]) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO live_logs(user_id, ts, level, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, str(item.get("ts") or ""), str(item.get("level") or "INFO"), str(item.get("message") or ""), now),
            )
            # Keep the latest 1000 log lines per user to prevent unlimited DB growth.
            conn.execute(
                """
                DELETE FROM live_logs
                WHERE user_id=? AND id NOT IN (
                    SELECT id FROM live_logs WHERE user_id=? ORDER BY id DESC LIMIT 1000
                )
                """,
                (user_id, user_id),
            )

    def get_logs(self, user_id: int, limit: int = 700) -> list[Dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, level, message FROM live_logs WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def clear_logs(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM live_logs WHERE user_id=?", (user_id,))

    def add_tracked_trade(self, user_id: int, trade: Dict[str, Any]) -> int:
        now = int(time.time())
        payload = {
            "source": str(trade.get("source") or "dry_run"),
            "action": str(trade.get("action") or ""),
            "symbol": str(trade.get("symbol") or "").upper(),
            "category": str(trade.get("category") or ""),
            "side": str(trade.get("side") or ""),
            "entry_price": str(trade.get("entry_price") or ""),
            "current_price": str(trade.get("current_price") or ""),
            "qty": str(trade.get("qty") or ""),
            "order_usdt": str(trade.get("order_usdt") or ""),
            "margin_usdt": str(trade.get("margin_usdt") or ""),
            "leverage": str(trade.get("leverage") or "1"),
            "take_profit": str(trade.get("take_profit") or ""),
            "stop_loss": str(trade.get("stop_loss") or ""),
            "reason": str(trade.get("reason") or "")[:800],
            "normalized_json": json.dumps(trade.get("normalized") or {}, ensure_ascii=False, default=str),
            "execution_json": json.dumps(trade.get("execution") or {}, ensure_ascii=False, default=str),
        }
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO tracked_trades(
                    user_id, status, source, action, symbol, category, side, entry_price, current_price,
                    qty, order_usdt, margin_usdt, leverage, take_profit, stop_loss, reason,
                    normalized_json, execution_json, created_at, updated_at
                ) VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, payload["source"], payload["action"], payload["symbol"], payload["category"], payload["side"],
                    payload["entry_price"], payload["current_price"], payload["qty"], payload["order_usdt"], payload["margin_usdt"],
                    payload["leverage"], payload["take_profit"], payload["stop_loss"], payload["reason"], payload["normalized_json"],
                    payload["execution_json"], now, now,
                ),
            )
            return int(cur.lastrowid)

    def list_tracked_trades(self, user_id: int, limit: int = 80) -> list[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tracked_trades WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]


    def get_tracked_trade(self, user_id: int, trade_id: int) -> Dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tracked_trades WHERE user_id=? AND id=?",
                (user_id, int(trade_id)),
            ).fetchone()
        return dict(row) if row else None

    def update_tracked_trade_status(self, user_id: int, trade_id: int, status: str, current_price: str = "") -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE tracked_trades SET status=?, current_price=COALESCE(NULLIF(?, ''), current_price), updated_at=? WHERE user_id=? AND id=?",
                (status, current_price, now, user_id, int(trade_id)),
            )

    def close_tracked_trades_for_action(self, user_id: int, normalized: Dict[str, Any], current_price: str = "") -> int:
        action = str(normalized.get("action") or "").upper()
        symbol = str(normalized.get("symbol") or "").upper()
        category = str(normalized.get("category") or "")
        if not symbol:
            return 0
        where = "user_id=? AND symbol=? AND status='open'"
        args: list[Any] = [user_id, symbol]
        if action in {"CLOSE_LONG", "CLOSE_SHORT", "CLOSE_ALL"}:
            where += " AND category IN ('linear','inverse')"
            if action == "CLOSE_LONG":
                where += " AND side='long'"
            elif action == "CLOSE_SHORT":
                where += " AND side='short'"
        elif action in {"SPOT_SELL", "SPOT_SELL_ALL"}:
            where += " AND category='spot'"
        else:
            return 0
        now = int(time.time())
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE tracked_trades SET status='closed', current_price=COALESCE(NULLIF(?, ''), current_price), updated_at=? WHERE {where}",
                [current_price, now, *args],
            )
            return int(cur.rowcount or 0)

    def clear_closed_trades(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM tracked_trades WHERE user_id=? AND status!='open'", (user_id,))


def normalize_username(username: str) -> str:
    username = (username or "").strip().lower()
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789_.-"
    return "".join(ch for ch in username if ch in allowed)[:40]


def normalize_setting_value(key: str, value: Any) -> Any:
    if key in {"dry_run", "require_tp_sl", "ai_cost_saver"}:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if key in {"max_leverage", "max_daily_trades", "min_seconds_between_trades", "loop_interval_seconds"}:
        return int(value)
    if key in {"bybit_env", "default_category", "openai_model", "recv_window", "bybit_auth_type"}:
        raw = str(value or "").strip()
        if key == "bybit_auth_type" and raw not in {"auto", "hmac", "rsa"}:
            return "auto"
        return raw
    if key in {"default_take_profit_pct", "default_stop_loss_pct"}:
        raw = str(value or "").strip().replace(",", ".").replace("%", "")
        try:
            num = float(raw)
        except ValueError:
            num = 0.0
        if num < 0:
            num = 0.0
        return str(num).rstrip("0").rstrip(".") if "." in str(num) else str(int(num))
    if key == "allowed_symbols":
        return ",".join([s.strip().upper() for s in str(value or "").split(",") if s.strip()])
    return str(value or "").strip()


def mask_secret(value: str) -> str:
    value = value or ""
    if not value:
        return "not-set"
    if len(value) <= 9:
        return value[:3] + "***"
    return f"{value[:5]}...{value[-4:]}"
