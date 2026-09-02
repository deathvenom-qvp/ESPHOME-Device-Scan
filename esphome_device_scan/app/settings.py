"""Add-on configuration.

Options reach us two ways: ``run.sh`` exports them as ``EDSCAN_*`` environment
variables under Supervisor, and ``/data/options.json`` is the raw Supervisor
file. Environment wins so the offline dry run can override anything without
writing an options file.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .models import MacPolicy, MacSuffixAction

_LOGGER = logging.getLogger(__name__)

OPTIONS_PATH = Path("/data/options.json")

#: Supervisor's ingress proxy. Only this address may reach the web server.
INGRESS_PEER = "172.30.32.2"

DEFAULTS: dict[str, object] = {
    "esphome_config_dir": "/homeassistant/esphome",
    "scan_interval_minutes": 15,
    "auto_generate": True,
    "scan_on_startup": True,
    "mac_policy": "suffix3",
    "name_add_mac_suffix_action": "set_false",
    "dry_run": False,
    "log_level": "info",
}

#: Add-on log levels (bashio's vocabulary) mapped onto Python's.
LOG_LEVELS = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
    return default


def _as_int(value: object, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(str(value).strip())))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    """Resolved, validated add-on options."""

    esphome_config_dir: Path
    scan_interval_minutes: int
    auto_generate: bool
    scan_on_startup: bool
    mac_policy: MacPolicy
    name_add_mac_suffix_action: MacSuffixAction
    dry_run: bool
    log_level: int
    supervisor_token: str | None = None
    supervisor_base_url: str = "http://supervisor/core"
    web_host: str = "0.0.0.0"  # noqa: S104 - ingress requires binding all ifaces
    web_port: int = 8099
    #: When True, reject requests that do not come from the ingress proxy.
    enforce_ingress_peer: bool = True

    @property
    def scan_interval_seconds(self) -> int:
        return self.scan_interval_minutes * 60


def _load_options_file(path: Path = OPTIONS_PATH) -> dict[str, object]:
    """Read Supervisor's options.json, tolerating absence or corruption."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as err:
        _LOGGER.warning("Could not read %s (%s); falling back to defaults", path, err)
        return {}


def load_settings(
    env: dict[str, str] | None = None,
    options_path: Path = OPTIONS_PATH,
) -> Settings:
    """Build Settings from options.json overlaid with EDSCAN_* env vars.

    ``env`` is injectable so tests can drive every branch without touching the
    real process environment.
    """
    environ = dict(os.environ if env is None else env)
    options = {**DEFAULTS, **_load_options_file(options_path)}

    def opt(key: str) -> object:
        return environ.get(f"EDSCAN_{key.upper()}", options.get(key))

    raw_policy = str(opt("mac_policy")).strip().lower()
    try:
        mac_policy = MacPolicy(raw_policy)
    except ValueError:
        _LOGGER.warning("Unknown mac_policy %r; using 'suffix3'", raw_policy)
        mac_policy = MacPolicy.SUFFIX3

    raw_action = str(opt("name_add_mac_suffix_action")).strip().lower()
    try:
        suffix_action = MacSuffixAction(raw_action)
    except ValueError:
        _LOGGER.warning(
            "Unknown name_add_mac_suffix_action %r; using 'set_false'", raw_action
        )
        suffix_action = MacSuffixAction.SET_FALSE

    raw_level = str(opt("log_level")).strip().lower()
    if raw_level not in LOG_LEVELS:
        _LOGGER.warning("Unknown log_level %r; using 'info'", raw_level)
    log_level = LOG_LEVELS.get(raw_level, logging.INFO)

    return Settings(
        esphome_config_dir=Path(str(opt("esphome_config_dir"))).expanduser(),
        scan_interval_minutes=_as_int(opt("scan_interval_minutes"), 15, 1, 1440),
        auto_generate=_as_bool(opt("auto_generate"), True),
        scan_on_startup=_as_bool(opt("scan_on_startup"), True),
        mac_policy=mac_policy,
        name_add_mac_suffix_action=suffix_action,
        dry_run=_as_bool(opt("dry_run"), False),
        log_level=log_level,
        supervisor_token=environ.get("SUPERVISOR_TOKEN"),
        supervisor_base_url=environ.get("EDSCAN_SUPERVISOR_URL", "http://supervisor/core"),
        web_port=_as_int(environ.get("EDSCAN_WEB_PORT", 8099), 8099, 1, 65535),
        enforce_ingress_peer=_as_bool(environ.get("EDSCAN_ENFORCE_INGRESS", True), True),
    )
