"""Ties discovery, template matching, generation and storage together.

The safety rule lives here and nowhere else: a scan calls
``store.write(..., overwrite=False)``, so an existing config can never be
clobbered by automation. :meth:`ScanOrchestrator.regenerate` is the only path
that passes ``overwrite=True``, and it is reachable only from an explicit user
action in the panel.

All collaborators are constructor-injected, so a scan can be exercised end to
end against a fake Home Assistant and a temp directory.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .config_store import ConfigExistsError, EsphomeConfigStore, ParentTemplateError
from .discovery import DeviceDiscoveryService
from .generator import YamlGenerator
from .models import Device, DeviceReport, Outcome, ScanReport
from .settings import Settings
from .templates import TemplateMatcher, TemplateRepository
from .yaml_compat import YamlParseError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegenerationPlan:
    """A preview of what "Regenerate all" would touch."""

    #: Have a config that still carries our header -- safe to rebuild.
    untouched: tuple[str, ...] = ()
    #: Have a config written or edited by hand -- rebuilding discards that work.
    edited: tuple[str, ...] = ()
    #: Matched but have no config yet; these get created.
    missing: tuple[str, ...] = ()
    #: No parent template claims them; nothing to regenerate from.
    unmatched: tuple[str, ...] = ()
    error: str | None = None

    @property
    def total(self) -> int:
        """How many files the run would write."""
        return len(self.untouched) + len(self.edited) + len(self.missing)


class ScanOrchestrator:
    """Runs a scan pass and applies the generation policy."""

    def __init__(
        self,
        discovery: DeviceDiscoveryService,
        store: EsphomeConfigStore,
        templates: TemplateRepository,
        generator: YamlGenerator,
        settings: Settings,
        matcher: TemplateMatcher | None = None,
    ) -> None:
        self._discovery = discovery
        self._store = store
        self._templates = templates
        self._generator = generator
        self._settings = settings
        self._matcher = matcher or TemplateMatcher()
        self._last_report: ScanReport | None = None

    @property
    def last_report(self) -> ScanReport | None:
        return self._last_report

    @property
    def matcher(self) -> TemplateMatcher:
        return self._matcher

    @property
    def store(self) -> EsphomeConfigStore:
        return self._store

    # -- scanning --------------------------------------------------------

    def refresh_templates(self) -> None:
        """Re-read the templates directory into the matcher.

        Called before anything consults the matcher, not just from ``scan()``:
        a user can drop a template in without restarting the add-on, and the
        panel must show it (and match against it) straight away rather than
        after the next scheduled pass.
        """
        self._matcher.set_templates(self._templates.load_all())
        for template in self._matcher.templates:
            for warning in template.warnings:
                _LOGGER.warning("Template %s: %s", template.name, warning)

    async def scan(self) -> ScanReport:
        """One full pass: discover, match, and generate what is missing."""
        started = time.monotonic()
        started_at = datetime.now(UTC).isoformat(timespec="seconds")
        errors: list[str] = []

        # Re-read templates and the config dir every pass, so a file added by
        # hand between scans is taken into account.
        self.refresh_templates()
        self._store.invalidate()

        try:
            devices = await self._discovery.list_devices()
        except Exception as err:  # noqa: BLE001 - surface, never crash the loop
            _LOGGER.error("Device discovery failed: %s", err)
            errors.append(f"Device discovery failed: {err}")
            devices = []

        reports = [self._process(device) for device in devices]

        report = ScanReport(
            devices=tuple(reports),
            started_at=started_at,
            finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
            duration_ms=int((time.monotonic() - started) * 1000),
            errors=tuple(errors),
        )
        self._last_report = report
        _LOGGER.info("Scan complete: %s", report.summary)
        return report

    def _process(self, device: Device) -> DeviceReport:
        """Decide and apply the outcome for a single device."""
        existing = self._store.find(device.node_name)
        has_yaml = existing is not None
        match = self._matcher.match(device)

        if has_yaml:
            return DeviceReport(
                device=device,
                outcome=Outcome.SKIPPED_HAS_CONFIG,
                has_yaml=True,
                template_name=match.template.name if match else None,
                match_rule=match.rule if match else None,
                path=str(existing.path),
                message=f"Already configured in {existing.path.name}",
            )

        if match is None:
            return DeviceReport(
                device=device,
                outcome=Outcome.NO_TEMPLATE_MATCH,
                has_yaml=False,
                message=(
                    f"No parent template matches '{device.node_name}'. Its base "
                    f"config must be in the ESPHome directory and carry MAC-suffix "
                    f"logic, or be marked '# x-template: true'."
                ),
            )

        if not self._settings.auto_generate:
            return DeviceReport(
                device=device,
                outcome=Outcome.SKIPPED_AUTO_GENERATE_OFF,
                has_yaml=False,
                template_name=match.template.name,
                match_rule=match.rule,
                message="auto_generate is off; use Generate to create this file.",
            )

        return self._generate(device, match, overwrite=False)

    # -- generation ------------------------------------------------------

    def generate_for(self, device: Device, *, overwrite: bool = False) -> DeviceReport:
        """Generate for one device on demand (the panel's Generate button).

        The cached report is updated with the outcome, so the panel's next
        /api/state poll reflects the new file straight away instead of showing
        the device as still unconfigured until the next full scan.
        """
        self.refresh_templates()
        self._store.invalidate()
        match = self._matcher.match(device)
        if match is None:
            report = DeviceReport(
                device=device,
                outcome=Outcome.NO_TEMPLATE_MATCH,
                has_yaml=self._store.find(device.node_name) is not None,
                message=f"No template matches '{device.node_name}'.",
            )
        else:
            report = self._generate(device, match, overwrite=overwrite)
        self._merge_into_last_report(report)
        return report

    def _generate(self, device, match, *, overwrite: bool) -> DeviceReport:
        template = match.template
        try:
            generated = self._generator.generate(template, device)
        except YamlParseError as err:
            message = f"Template '{template.name}' is not valid YAML: {err}"
            _LOGGER.error(message)
            return DeviceReport(
                device=device, outcome=Outcome.ERROR, has_yaml=False,
                template_name=template.name, match_rule=match.rule, message=message,
            )

        for warning in generated.warnings:
            _LOGGER.warning("%s: %s", device.node_name, warning)

        if self._settings.dry_run and not overwrite:
            target = self._store.target_path(device.node_name)
            _LOGGER.info(
                "[dry run] Would write %s from %s", target.name, template.name
            )
            return DeviceReport(
                device=device, outcome=Outcome.WOULD_GENERATE, has_yaml=False,
                template_name=template.name, match_rule=match.rule,
                path=str(target), warnings=generated.warnings,
                message=f"Dry run: would generate from {template.name}",
            )

        try:
            path = self._store.write(
                device.node_name, generated.content, overwrite=overwrite
            )
        except ConfigExistsError as err:
            return DeviceReport(
                device=device, outcome=Outcome.SKIPPED_HAS_CONFIG, has_yaml=True,
                template_name=template.name, match_rule=match.rule, message=str(err),
            )
        except ParentTemplateError as err:
            _LOGGER.error("%s: %s", device.node_name, err)
            return DeviceReport(
                device=device, outcome=Outcome.ERROR, has_yaml=False,
                template_name=template.name, match_rule=match.rule, message=str(err),
            )
        except OSError as err:
            message = (
                f"Could not write config for '{device.node_name}': {err}. "
                f"Check that {self._store.root} exists and is writable."
            )
            _LOGGER.error(message)
            return DeviceReport(
                device=device, outcome=Outcome.ERROR, has_yaml=False,
                template_name=template.name, match_rule=match.rule, message=message,
            )

        outcome = Outcome.REGENERATED if overwrite else Outcome.GENERATED
        _LOGGER.info(
            "%s %s from template %s (matched by %s '%s')",
            "Regenerated" if overwrite else "Generated",
            path.name, template.name, match.rule, match.pattern,
        )
        return DeviceReport(
            device=device, outcome=outcome, has_yaml=True,
            template_name=template.name, match_rule=match.rule,
            path=str(path), warnings=generated.warnings,
            message=f"{outcome.value.capitalize()} from {template.name}",
        )

    # -- bulk regeneration -----------------------------------------------

    async def plan_regenerate_all(self) -> RegenerationPlan:
        """What a "Regenerate all" would do, without doing any of it.

        Bulk overwriting is the most destructive thing this add-on can do, so
        the panel asks for this first and puts the numbers in the confirmation.
        The distinction that matters is ``edited``: files that no longer carry
        our generated header, meaning someone has written or changed them by
        hand and would lose that work (recoverably -- a backup is still taken).
        """
        self.refresh_templates()
        self._store.invalidate()

        try:
            devices = await self._discovery.list_devices()
        except Exception as err:  # noqa: BLE001 - report, never crash the panel
            _LOGGER.error("Device discovery failed: %s", err)
            return RegenerationPlan(error=str(err))

        untouched: list[str] = []
        edited: list[str] = []
        missing: list[str] = []
        unmatched: list[str] = []

        for device in devices:
            if self._matcher.match(device) is None:
                unmatched.append(device.node_name)
                continue
            existing = self._store.find(device.node_name)
            if existing is None:
                missing.append(device.node_name)
            elif existing.generated:
                untouched.append(device.node_name)
            else:
                edited.append(device.node_name)

        return RegenerationPlan(
            untouched=tuple(untouched),
            edited=tuple(edited),
            missing=tuple(missing),
            unmatched=tuple(unmatched),
        )

    async def regenerate_all(self, *, skip_edited: bool = False) -> ScanReport:
        """Rebuild every matched device's config from its parent template.

        Each file is backed up before being replaced, and parents are still
        protected, so the blast radius is bounded even here. Devices with no
        config yet are generated rather than skipped -- "regenerate all" should
        leave every matched device in step with its parent, not just the ones
        that already had a file.

        ``skip_edited`` leaves hand-written and hand-edited configs alone.
        """
        started = time.monotonic()
        started_at = datetime.now(UTC).isoformat(timespec="seconds")

        self.refresh_templates()
        self._store.invalidate()

        try:
            devices = await self._discovery.list_devices()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Device discovery failed: %s", err)
            return ScanReport(
                started_at=started_at,
                finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
                errors=(f"Device discovery failed: {err}",),
            )

        _LOGGER.info("Regenerating all device configs (%d device(s))", len(devices))
        reports: list[DeviceReport] = []
        for device in devices:
            existing = self._store.find(device.node_name)
            if skip_edited and existing is not None and not existing.generated:
                _LOGGER.info(
                    "Skipping %s: %s was not generated by this add-on",
                    device.node_name, existing.path.name,
                )
                reports.append(
                    DeviceReport(
                        device=device,
                        outcome=Outcome.SKIPPED_HAS_CONFIG,
                        has_yaml=True,
                        path=str(existing.path),
                        message=f"Left alone: {existing.path.name} was edited by hand",
                    )
                )
                continue
            reports.append(self._regenerate_one(device))

        report = ScanReport(
            devices=tuple(reports),
            started_at=started_at,
            finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        self._last_report = report
        _LOGGER.info("Regenerate all complete: %s", report.summary)
        return report

    def _regenerate_one(self, device: Device) -> DeviceReport:
        """One device's contribution to a bulk run, never raising."""
        match = self._matcher.match(device)
        if match is None:
            return DeviceReport(
                device=device,
                outcome=Outcome.NO_TEMPLATE_MATCH,
                has_yaml=self._store.find(device.node_name) is not None,
                message=f"No parent template matches '{device.node_name}'.",
            )
        # overwrite is safe to pass unconditionally: write() creates when there
        # is nothing there, and backs up when there is.
        return self._generate(device, match, overwrite=True)

    async def regenerate(self, node_name: str) -> DeviceReport:
        """Rebuild one device's config, backing up whatever is there now.

        Deliberately re-runs discovery: the file is about to be overwritten, so
        it should be built from what Home Assistant knows *now*, not from a
        possibly stale scan.
        """
        devices = await self._discovery.list_devices()
        device = next((d for d in devices if d.node_name == node_name), None)
        if device is None:
            raise LookupError(f"No discovered device named '{node_name}'")

        # generate_for() updates the cached report for us.
        return self.generate_for(device, overwrite=True)

    def _merge_into_last_report(self, report: DeviceReport) -> None:
        """Keep the cached report in step after a single-device action."""
        if self._last_report is None:
            self._last_report = ScanReport(devices=(report,))
            return
        updated = [
            report if d.device.node_name == report.device.node_name else d
            for d in self._last_report.devices
        ]
        if all(d.device.node_name != report.device.node_name for d in updated):
            updated.append(report)
        self._last_report = ScanReport(
            devices=tuple(updated),
            started_at=self._last_report.started_at,
            finished_at=self._last_report.finished_at,
            duration_ms=self._last_report.duration_ms,
            errors=self._last_report.errors,
        )

    async def find_device(self, node_name: str) -> Device | None:
        devices = await self._discovery.list_devices()
        return next((d for d in devices if d.node_name == node_name), None)
