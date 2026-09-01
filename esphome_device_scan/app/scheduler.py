"""Periodic scan loop.

Kept deliberately small: it owns *when* a scan runs and nothing about what a
scan does. A scan that raises is logged and the loop continues, so one bad pass
(Home Assistant restarting, say) never takes the add-on down.
"""

from __future__ import annotations

import asyncio
import logging

from .orchestrator import ScanOrchestrator

_LOGGER = logging.getLogger(__name__)


class ScanScheduler:
    """Runs ``orchestrator.scan()`` on an interval, plus on demand."""

    def __init__(
        self,
        orchestrator: ScanOrchestrator,
        interval_seconds: int,
        scan_on_startup: bool = True,
    ) -> None:
        self._orchestrator = orchestrator
        self._interval = max(60, interval_seconds)
        self._scan_on_startup = scan_on_startup
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        # Serialises scheduled and user-triggered scans so two passes never
        # race to write the same file.
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="edscan-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    def trigger(self) -> None:
        """Ask for a scan now without waiting for it."""
        self._wake.set()

    async def scan_now(self):
        """Run a scan immediately and return its report."""
        async with self._lock:
            return await self._orchestrator.scan()

    async def _run(self) -> None:
        if self._scan_on_startup:
            await self._safe_scan()

        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except TimeoutError:
                pass  # interval elapsed: time for a scheduled scan
            except asyncio.CancelledError:
                raise
            self._wake.clear()
            await self._safe_scan()

    async def _safe_scan(self) -> None:
        try:
            async with self._lock:
                await self._orchestrator.scan()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Scan failed; will retry next interval")
