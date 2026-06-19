import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

COOKIE_NAME = "bybit_ai_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7


def _secret() -> bytes:
    return os.getenv("APP_SECRET", "dev-change-me-immediately").encode("utf-8")


def create_session_token(user_id: int) -> str:
    payload = {"uid": int(user_id), "exp": int(time.time()) + SESSION_TTL_SECONDS}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_session_token(token: str) -> Optional[int]:
    try:
        body, sig = token.split(".", 1)
        expected = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return int(payload.get("uid"))
    except Exception:
        return None
