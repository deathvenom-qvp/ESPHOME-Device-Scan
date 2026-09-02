"""Runs build-and-flash jobs in the background and reports progress.

Flashing is slow -- minutes per device -- so the panel cannot wait on a
request. Instead a :class:`FlashSession` runs the devices one at a time in a
background task while the panel polls :meth:`FlashCoordinator.snapshot`.

Devices are flashed **sequentially, not in parallel**: the Device Builder
add-on compiles in one shared workspace, and several builds at once would
contend for it and for the machine's cores. One at a time is also far easier to
read in a progress dialog.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .esphome_dashboard import DashboardError, EsphomeDashboardClient

_LOGGER = logging.getLogger(__name__)

#: Output lines kept per device for the dialog. Enough to see what failed
#: without holding a whole build log in memory for every device.
LOG_TAIL = 120


class FlashState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass
class FlashTask:
    """One device's build-and-flash."""

    node_name: str
    configuration: str
    state: FlashState = FlashState.PENDING
    message: str = ""
    #: Last output line, so the dialog can show progress on one row.
    detail: str = ""
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_TAIL))

    def to_json(self) -> dict[str, Any]:
        return {
            "node_name": self.node_name,
            "configuration": self.configuration,
            "state": self.state.value,
            "message": self.message,
            "detail": self.detail,
            "lines": list(self.lines),
        }


@dataclass
class FlashSession:
    """A run of build-and-flash across several devices."""

    tasks: list[FlashTask]
    started_at: str
    finished_at: str | None = None
    error: str | None = None
    cancelled: bool = False

    @property
    def active(self) -> bool:
        return self.finished_at is None

    def counts(self) -> dict[str, int]:
        counts = {state.value: 0 for state in FlashState}
        for task in self.tasks:
            counts[task.state.value] += 1
        return counts

    def to_json(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "cancelled": self.cancelled,
            "counts": self.counts(),
            "tasks": [task.to_json() for task in self.tasks],
        }


class FlashCoordinator:
    """Owns the one in-flight flash session, if any."""

    def __init__(self, dashboard: EsphomeDashboardClient) -> None:
        self._dashboard = dashboard
        self._session: FlashSession | None = None
        self._task: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> dict[str, Any] | None:
        """Current session state for the panel, or None if none has run."""
        return self._session.to_json() if self._session else None

    async def start(self, targets: list[tuple[str, str]]) -> FlashSession:
        """Begin flashing ``(node_name, configuration)`` pairs.

        Refuses to start a second run while one is in flight, rather than
        queueing: two concurrent builds would fight over the Device Builder's
        workspace, and the dialog only shows one session.
        """
        if self.busy:
            raise RuntimeError("A flash run is already in progress.")

        session = FlashSession(
            tasks=[FlashTask(node, config) for node, config in targets],
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self._session = session
        self._task = asyncio.create_task(self._run(session), name="edscan-flash")
        return session

    def cancel(self) -> bool:
        """Stop after the current device. Returns False if nothing is running.

        The device being flashed right now is left to finish: interrupting an
        OTA upload mid-write is how you brick a board.
        """
        if not self.busy or self._session is None:
            return False
        self._session.cancelled = True
        return True

    async def _run(self, session: FlashSession) -> None:
        try:
            for task in session.tasks:
                if session.cancelled:
                    task.state = FlashState.CANCELLED
                    task.message = "Cancelled before this device started."
                    continue
                await self._flash_one(task)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.exception("Flash run failed")
            session.error = str(err)
        finally:
            session.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
            _LOGGER.info(
                "Flash run finished: %s",
                ", ".join(f"{n} {c}" for n, c in session.counts().items() if c),
            )

    async def _flash_one(self, task: FlashTask) -> None:
        task.state = FlashState.RUNNING
        task.message = "Building and uploading…"
        _LOGGER.info("Flashing %s (%s)", task.node_name, task.configuration)

        try:
            async for kind, value in self._dashboard.upload(task.configuration):
                if kind == "line":
                    line = str(value)
                    task.lines.append(line)
                    if line.strip():
                        task.detail = line.strip()[-160:]
                elif kind == "exit":
                    code = int(value)  # type: ignore[arg-type]
                    if code == 0:
                        task.state = FlashState.DONE
                        task.message = "Flashed successfully."
                    else:
                        task.state = FlashState.FAILED
                        task.message = f"ESPHome exited with code {code}."
                    return
        except DashboardError as err:
            task.state = FlashState.FAILED
            task.message = str(err)
            _LOGGER.error("Flashing %s failed: %s", task.node_name, err)
            return

        # The stream ended without an exit frame.
        task.state = FlashState.FAILED
        task.message = "The ESPHome dashboard closed the connection unexpectedly."
