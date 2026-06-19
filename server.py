import os
import uvicorn


def _get_port() -> int:
    raw = os.getenv("PORT", "8000")
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(f"[BOOT] Invalid PORT={raw!r}; falling back to 8000", flush=True)
        return 8000


if __name__ == "__main__":
    port = _get_port()
    print(f"[BOOT] Starting CIG AI Subaccount on 0.0.0.0:{port}", flush=True)
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
