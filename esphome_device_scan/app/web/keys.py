"""Typed keys for the aiohttp application mapping.

``web.AppKey`` is aiohttp's supported way to stash dependencies on an
application: it keeps the values type-checked and avoids the ``NotAppKeyWarning``
that bare string keys now raise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from ..flashing import FlashCoordinator
    from ..generator import YamlGenerator
    from ..logbuf import LogBuffer
    from ..orchestrator import ScanOrchestrator
    from ..scheduler import ScanScheduler
    from ..settings import Settings

SETTINGS: web.AppKey[Settings] = web.AppKey("settings")
ORCHESTRATOR: web.AppKey[ScanOrchestrator] = web.AppKey("orchestrator")
SCHEDULER: web.AppKey[ScanScheduler] = web.AppKey("scheduler")
GENERATOR: web.AppKey[YamlGenerator] = web.AppKey("generator")
LOGS: web.AppKey[LogBuffer] = web.AppKey("logs")
FLASHER: web.AppKey[FlashCoordinator] = web.AppKey("flasher")
