import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional


class UserRuntimeState:
    """Per-user live runtime: logs, bot status, counters and stop signal."""

    def __init__(self, user_id: int, log_store: Optional[Any] = None) -> None:
        self.user_id = user_id
        self.log_store = log_store
        self.log_history: Deque[Dict[str, str]] = deque(maxlen=700)
        if self.log_store is not None:
            try:
                for item in self.log_store.get_logs(user_id, limit=700):
                    self.log_history.append(item)
            except Exception:
                pass
        self.log_queue: asyncio.Queue[Dict[str, str]] = asyncio.Queue(maxsize=1500)
        self.running: bool = False
        self.started_at: Optional[str] = None
        self.last_action_at: Optional[str] = None
        self.daily_trade_count: int = 0
        self.daily_trade_date: Optional[str] = None
        self.last_trade_ts: float = 0.0
        self.active_task: Optional[asyncio.Task] = None
        self.stop_event: Optional[asyncio.Event] = None

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    async def log(self, level: str, message: str) -> None:
        item = {
            "ts": self.now_iso(),
            "level": level.upper(),
            "message": message,
        }
        self.log_history.append(item)
        if self.log_store is not None:
            try:
                self.log_store.append_log(self.user_id, item)
            except Exception:
                pass
        try:
            self.log_queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    def get_logs(self) -> List[Dict[str, str]]:
        return list(self.log_history)

    def reset_daily_counter_if_needed(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if self.daily_trade_date != today:
            self.daily_trade_date = today
            self.daily_trade_count = 0


class RuntimeManager:
    def __init__(self, log_store: Optional[Any] = None) -> None:
        self.log_store = log_store
        self._states: Dict[int, UserRuntimeState] = {}

    def get(self, user_id: int) -> UserRuntimeState:
        if user_id not in self._states:
            self._states[user_id] = UserRuntimeState(user_id, log_store=self.log_store)
        return self._states[user_id]
