"""In-memory log ring buffer that backs the panel's log pane.

Installed as a standard logging handler so anything the service logs -- not
just generation events -- reaches the UI, while the add-on's stdout log keeps
working exactly as Supervisor expects.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any


class LogBuffer(logging.Handler):
    """Bounded, thread-safe record of recent log lines."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._entries: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._counter = 0

    def install(self, level: int = logging.INFO) -> LogBuffer:
        """Attach to the root logger so the panel sees everything logged.

        A buffer that is never installed silently stays empty, so this is the
        single call that makes it live -- both the service entrypoint and the
        tests go through it rather than adding the handler by hand.
        """
        self.setLevel(level)
        root = logging.getLogger()
        if self not in root.handlers:
            root.addHandler(self)
        # A logger filters by its own level before any handler is consulted, so
        # attaching the handler is not enough on its own: the root logger
        # defaults to WARNING and would drop the INFO lines the panel exists to
        # show. Only ever lower the threshold, never raise someone else's.
        if root.level == logging.NOTSET or root.level > level:
            root.setLevel(level)
        return self

    def emit(self, record: logging.LogRecord) -> None:
        # A logging handler must never raise; a broken log pane is a nuisance,
        # a crashed scan loop is an outage.
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - defensive by design
            message = str(record.msg)
        self.add(record.levelname.lower(), message, logger=record.name)

    def add(self, level: str, message: str, logger: str = "app") -> None:
        with self._lock:
            self._counter += 1
            self._entries.append(
                {
                    "id": self._counter,
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "level": level,
                    "logger": logger,
                    "message": message,
                }
            )

    def entries(self, since: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        """Entries newer than ``since``, oldest first."""
        with self._lock:
            selected = [e for e in self._entries if e["id"] > since]
        return selected[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
