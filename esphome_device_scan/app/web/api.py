"""JSON API behind the panel.

Every handler returns plain JSON and turns failures into an ``{"error": ...}``
body with a real HTTP status, so the frontend can show the actual reason rather
than a generic failure.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from ..config_store import EsphomeConfigStore
from ..esphome_dashboard import DashboardError
from ..logbuf import LogBuffer
from ..models import Device, DeviceReport, Outcome, ScanReport
from ..orchestrator import ScanOrchestrator
from ..scheduler import ScanScheduler
from ..settings import Settings
from .keys import FLASHER, GENERATOR, LOGS, ORCHESTRATOR, SCHEDULER, SETTINGS

_LOGGER = logging.getLogger(__name__)

routes = web.RouteTableDef()


def device_to_json(device: Device) -> dict[str, Any]:
    return {
        "node_name": device.node_name,
        "friendly_name": device.friendly_name,
        "display_name": device.display_name,
        "mac": device.mac_pretty,
        "mac_suffix": device.mac_suffix,
        "status": device.status.value,
        "source": device.source.value,
        "host": device.host,
        "model": device.model,
        "manufacturer": device.manufacturer,
        "sw_version": device.sw_version,
        "name_source": device.name_source,
    }


def report_to_json(report: DeviceReport) -> dict[str, Any]:
    return {
        **device_to_json(report.device),
        "outcome": report.outcome.value,
        "has_yaml": report.has_yaml,
        "template": report.template_name,
        "match_rule": report.match_rule,
        "path": report.path,
        "message": report.message,
        "warnings": list(report.warnings),
    }


def scan_to_json(report: ScanReport | None) -> dict[str, Any]:
    if report is None:
        return {"devices": [], "summary": "No scan has run yet.", "counts": {}}
    return {
        "devices": [report_to_json(d) for d in report.devices],
        "summary": report.summary,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "duration_ms": report.duration_ms,
        "errors": list(report.errors),
        "counts": {
            outcome.value: report.count(outcome) for outcome in Outcome
        },
    }


@routes.get("/api/state")
async def get_state(request: web.Request) -> web.Response:
    """Everything the panel needs for a full render."""
    orchestrator: ScanOrchestrator = request.app[ORCHESTRATOR]
    settings: Settings = request.app[SETTINGS]
    # Pick up templates added since the last scan, so the panel is current
    # even on a first load before any scan has run.
    orchestrator.refresh_templates()
    return web.json_response(
        {
            "scan": scan_to_json(orchestrator.last_report),
            "templates": [
                {
                    "name": t.name,
                    "path": str(t.path),
                    "detected_by": t.detected_by,
                    "prefixes": list(t.match_prefixes)
                    or [p for p in (t.name_prefix,) if p]
                    or [t.stem],
                    "regexes": list(t.match_regexes),
                    "models": list(t.match_models),
                    "priority": t.priority,
                    "mac_policy": t.mac_policy.value if t.mac_policy else None,
                    "implicit": not (t.match_prefixes or t.match_regexes),
                    "warnings": list(t.warnings),
                }
                for t in orchestrator.matcher.templates
            ],
            "settings": {
                "esphome_config_dir": str(settings.esphome_config_dir),
                "scan_interval_minutes": settings.scan_interval_minutes,
                "auto_generate": settings.auto_generate,
                "dry_run": settings.dry_run,
                "mac_policy": settings.mac_policy.value,
                "name_add_mac_suffix_action": settings.name_add_mac_suffix_action.value,
                "esphome_dashboard_url": settings.esphome_dashboard_url or "(auto-discover)",
            },
            "flash": request.app[FLASHER].snapshot(),
        }
    )


@routes.post("/api/scan")
async def post_scan(request: web.Request) -> web.Response:
    scheduler: ScanScheduler = request.app[SCHEDULER]
    report = await scheduler.scan_now()
    return web.json_response(scan_to_json(report))


@routes.get("/api/regenerate-all/plan")
async def get_regenerate_all_plan(request: web.Request) -> web.Response:
    """What a bulk regenerate would touch. Writes nothing.

    The panel fetches this before showing its confirmation, so the warning can
    name real numbers -- particularly how many files were edited by hand and
    would lose that content.
    """
    orchestrator: ScanOrchestrator = request.app[ORCHESTRATOR]
    plan = await orchestrator.plan_regenerate_all()
    return web.json_response(
        {
            "total": plan.total,
            "untouched": list(plan.untouched),
            "edited": list(plan.edited),
            "missing": list(plan.missing),
            "unmatched": list(plan.unmatched),
            "error": plan.error,
        },
        status=500 if plan.error else 200,
    )


@routes.post("/api/regenerate-all")
async def post_regenerate_all(request: web.Request) -> web.Response:
    """Rebuild every matched device's config from its parent template.

    ``?skip_edited=1`` leaves hand-written and hand-edited files alone.
    """
    scheduler: ScanScheduler = request.app[SCHEDULER]
    skip_edited = request.query.get("skip_edited", "").lower() in ("1", "true", "yes")
    report = await scheduler.regenerate_all_now(skip_edited=skip_edited)
    return web.json_response(scan_to_json(report))


async def _selected_templates(request: web.Request) -> set[str]:
    """Template names from the request body, validated against what exists.

    Names come from the browser, so they are checked against the loaded
    templates rather than trusted -- an unknown name is a bad request, not a
    silent no-op that leaves the user wondering why nothing happened.
    """
    try:
        body = await request.json()
    except (ValueError, TypeError) as err:
        raise web.HTTPBadRequest(reason="Expected a JSON body") from err

    names = body.get("templates") if isinstance(body, dict) else None
    if not isinstance(names, list) or not names:
        raise web.HTTPBadRequest(reason="No parent templates selected")

    selected = {str(name) for name in names}
    orchestrator: ScanOrchestrator = request.app[ORCHESTRATOR]
    orchestrator.refresh_templates()
    known = {template.name for template in orchestrator.matcher.templates}

    unknown = selected - known
    if unknown:
        raise web.HTTPBadRequest(
            reason=f"Unknown parent template(s): {', '.join(sorted(unknown))}"
        )
    return selected


@routes.post("/api/regenerate-selected")
async def post_regenerate_selected(request: web.Request) -> web.Response:
    """Rebuild the configs belonging to the selected parent templates."""
    selected = await _selected_templates(request)
    scheduler: ScanScheduler = request.app[SCHEDULER]
    report = await scheduler.regenerate_templates_now(selected)
    return web.json_response(scan_to_json(report))


@routes.post("/api/flash-selected")
async def post_flash_selected(request: web.Request) -> web.Response:
    """Rebuild, then build-and-OTA-flash, every device of the selected parents.

    Returns as soon as the run has started; the panel polls
    ``/api/flash/status`` for progress, because a flash takes minutes.
    """
    selected = await _selected_templates(request)
    scheduler: ScanScheduler = request.app[SCHEDULER]
    flasher = request.app[FLASHER]

    if flasher.busy:
        return web.json_response(
            {"error": "A flash run is already in progress."}, status=409
        )

    # Regenerate first: flashing a device from a stale config would defeat the
    # point of pressing this button.
    report = await scheduler.regenerate_templates_now(selected)

    targets: list[tuple[str, str]] = []
    for entry in report.devices:
        if entry.outcome is Outcome.ERROR or entry.path is None:
            continue
        targets.append((entry.device.node_name, Path(entry.path).name))

    if not targets:
        return web.json_response(
            {"error": "Nothing to flash: no device config was produced."}, status=400
        )

    try:
        await flasher.start(targets)
    except (DashboardError, RuntimeError) as err:
        return web.json_response({"error": str(err)}, status=409)

    return web.json_response(
        {"started": True, "regenerated": scan_to_json(report), "flash": flasher.snapshot()}
    )


@routes.get("/api/flash/status")
async def get_flash_status(request: web.Request) -> web.Response:
    """Progress of the current (or last) flash run."""
    snapshot = request.app[FLASHER].snapshot()
    return web.json_response(snapshot or {"active": False, "tasks": [], "counts": {}})


@routes.post("/api/flash/cancel")
async def post_flash_cancel(request: web.Request) -> web.Response:
    """Stop after the device currently being flashed finishes.

    The in-flight device is deliberately allowed to complete: interrupting an
    OTA write part-way is how a board gets bricked.
    """
    stopped = request.app[FLASHER].cancel()
    return web.json_response({"cancelling": stopped})


@routes.post("/api/generate/{node_name}")
async def post_generate(request: web.Request) -> web.Response:
    """Generate for one device, refusing to overwrite an existing file."""
    return await _generate(request, overwrite=False)


@routes.post("/api/regenerate/{node_name}")
async def post_regenerate(request: web.Request) -> web.Response:
    """Rebuild one device's config, backing up the current file first."""
    return await _generate(request, overwrite=True)


async def _generate(request: web.Request, *, overwrite: bool) -> web.Response:
    orchestrator: ScanOrchestrator = request.app[ORCHESTRATOR]
    node_name = request.match_info["node_name"]
    try:
        if overwrite:
            report = await orchestrator.regenerate(node_name)
        else:
            device = await orchestrator.find_device(node_name)
            if device is None:
                raise LookupError(f"No discovered device named '{node_name}'")
            report = orchestrator.generate_for(device, overwrite=False)
    except LookupError as err:
        return web.json_response({"error": str(err)}, status=404)
    except Exception as err:
        _LOGGER.exception("Generation failed for %s", node_name)
        return web.json_response({"error": str(err)}, status=500)

    status = 200 if report.outcome is not Outcome.ERROR else 500
    return web.json_response(report_to_json(report), status=status)


@routes.get("/api/yaml/{node_name}")
async def get_yaml(request: web.Request) -> web.Response:
    """Current on-disk YAML for a device, for the View button."""
    store: EsphomeConfigStore = request.app[ORCHESTRATOR].store
    node_name = request.match_info["node_name"]
    store.invalidate()

    existing = store.find(node_name)
    if existing is None:
        return web.json_response(
            {"error": f"No config file found for '{node_name}'."}, status=404
        )
    content = store.read(node_name)
    if content is None:
        return web.json_response(
            {"error": f"Config for '{node_name}' exists but could not be read."},
            status=500,
        )
    return web.json_response(
        {"node_name": node_name, "path": str(existing.path), "content": content}
    )


@routes.get("/api/preview/{node_name}")
async def get_preview(request: web.Request) -> web.Response:
    """Render what *would* be generated, without writing anything.

    Lets a user check a template against a real device before committing to a
    file, which matters most when auto_generate is off or a match looks wrong.
    """
    orchestrator: ScanOrchestrator = request.app[ORCHESTRATOR]
    node_name = request.match_info["node_name"]

    device = await orchestrator.find_device(node_name)
    if device is None:
        return web.json_response(
            {"error": f"No discovered device named '{node_name}'."}, status=404
        )
    orchestrator.refresh_templates()
    match = orchestrator.matcher.match(device)
    if match is None:
        return web.json_response(
            {"error": f"No template matches '{node_name}'."}, status=404
        )
    try:
        generated = request.app[GENERATOR].generate(match.template, device)
    except Exception as err:  # noqa: BLE001
        return web.json_response({"error": str(err)}, status=500)

    return web.json_response(
        {
            "node_name": node_name,
            "template": match.template.name,
            "content": generated.content,
            "warnings": list(generated.warnings),
            "edits": [
                {"reason": e.reason, "replacement": e.replacement} for e in generated.edits
            ],
        }
    )


@routes.get("/api/logs")
async def get_logs(request: web.Request) -> web.Response:
    logs: LogBuffer = request.app[LOGS]
    try:
        since = int(request.query.get("since", "0"))
    except ValueError:
        since = 0
    return web.json_response({"entries": logs.entries(since=since)})


@routes.get("/api/health")
async def get_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})
