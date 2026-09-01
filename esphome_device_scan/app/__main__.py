"""Composition root.

The only place that constructs concrete implementations and wires them
together. Every other module depends on the interfaces it is handed, which is
what lets the whole pipeline run against a fake Home Assistant in tests.

Also usable offline for a dry run:

    python -m app --once --dry-run \
        --esphome-dir ./scratch/esphome --templates-dir ./templates
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from dataclasses import replace
from pathlib import Path

import aiohttp

from .config_store import EsphomeConfigStore
from .discovery import DeviceDiscoveryService
from .generator import YamlGenerator
from .ha_client import SupervisorHaClient
from .logbuf import LogBuffer
from .orchestrator import ScanOrchestrator
from .scheduler import ScanScheduler
from .settings import Settings, load_settings
from .templates import TemplateRepository
from .web.server import create_app, start_server

_LOGGER = logging.getLogger("app")


def configure_logging(level: int, buffer: LogBuffer) -> None:
    """Log to stdout for the Supervisor log, and to the buffer for the panel."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(stream)

    buffer.install(level)

    # aiohttp's access log is pure noise behind ingress.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="esphome-device-scan")
    parser.add_argument(
        "--once", action="store_true",
        help="run a single scan, print the report and exit (no web server)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="never write files, just report what would be generated",
    )
    parser.add_argument("--esphome-dir", help="override the ESPHome config directory")
    parser.add_argument("--templates-dir", help="override the templates directory")
    return parser.parse_args(argv)


def build_settings(args: argparse.Namespace) -> Settings:
    settings = load_settings()
    overrides: dict[str, object] = {}
    if args.dry_run:
        overrides["dry_run"] = True
    if args.esphome_dir:
        overrides["esphome_config_dir"] = Path(args.esphome_dir)
    if args.templates_dir:
        overrides["templates_dir"] = Path(args.templates_dir)
    return replace(settings, **overrides) if overrides else settings


async def run(args: argparse.Namespace) -> int:
    logs = LogBuffer()
    settings = build_settings(args)
    configure_logging(settings.log_level, logs)

    _LOGGER.info("ESPHome Device Scan starting")
    _LOGGER.info("ESPHome config dir: %s", settings.esphome_config_dir)
    _LOGGER.info("Templates dir:      %s", settings.templates_dir)
    if settings.dry_run:
        _LOGGER.warning("Dry run is on: no files will be written.")
    if not settings.auto_generate:
        _LOGGER.info("auto_generate is off: use the panel to generate on demand.")

    templates = TemplateRepository(settings.templates_dir, settings.seed_templates_dir)
    templates.ensure_seeded()

    store = EsphomeConfigStore(settings.esphome_config_dir)
    generator = YamlGenerator(
        settings.mac_policy, settings.name_add_mac_suffix_action
    )

    async with aiohttp.ClientSession() as session:
        ha = SupervisorHaClient(
            session,
            base_url=settings.supervisor_base_url,
            token=settings.supervisor_token,
        )
        if not settings.supervisor_token:
            _LOGGER.error(
                "SUPERVISOR_TOKEN is not set. Device discovery will find nothing. "
                "This add-on needs 'homeassistant_api: true'."
            )
        elif not await ha.verify():
            # Not fatal: Home Assistant may still be starting up, and the
            # scheduler will retry. But say so plainly now rather than let the
            # panel just show an empty device list.
            _LOGGER.warning(
                "Could not reach the Home Assistant API yet. Scans will retry; "
                "if this persists, run scripts/probe_ha.py to see why."
            )

        orchestrator = ScanOrchestrator(
            discovery=DeviceDiscoveryService(ha),
            store=store,
            templates=templates,
            generator=generator,
            settings=settings,
        )

        if args.once:
            report = await orchestrator.scan()
            print(report.summary)
            for entry in report.devices:
                print(
                    f"  {entry.device.node_name:<28} {entry.outcome.value:<26}"
                    f" {entry.message or ''}"
                )
            return 0

        scheduler = ScanScheduler(
            orchestrator,
            settings.scan_interval_seconds,
            scan_on_startup=settings.scan_on_startup,
        )
        await scheduler.start()

        app = create_app(settings, orchestrator, scheduler, generator, logs)
        runner = await start_server(app, settings)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Not available on every platform; Ctrl-C still works without it.
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        _LOGGER.info(
            "Ready. Scanning every %d minute(s).", settings.scan_interval_minutes
        )
        try:
            await stop.wait()
        finally:
            _LOGGER.info("Shutting down")
            await scheduler.stop()
            await runner.cleanup()
    return 0


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
