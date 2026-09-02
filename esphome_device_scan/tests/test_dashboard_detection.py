"""Finding the ESPHome Device Builder add-on.

Detection is the part most likely to fail on a real install -- the add-on's
slug depends on which repository it came from, and its container address is not
knowable in advance. These drive a fake Supervisor and a fake network so every
strategy and every failure mode is exercised without either.
"""

from __future__ import annotations

import aiohttp
import pytest

from app.esphome_dashboard import (
    DASHBOARD_PORT,
    DashboardError,
    EsphomeDashboardClient,
)


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status = status
        self._payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class FakeSession:
    """Answers only for URLs it is told about; everything else is unreachable."""

    def __init__(self, routes: dict[str, tuple[int, dict] | Exception]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        for prefix, result in self.routes.items():
            if url.startswith(prefix):
                if isinstance(result, Exception):
                    raise result
                status, payload = result
                return FakeResponse(status, payload)
        raise aiohttp.ClientConnectionError(f"nothing listening at {url}")


def discovery_payload(host: str, port: int = DASHBOARD_PORT, slug: str = "5c53de3b_esphome"):
    return {
        "data": {
            "discovery": [
                {"addon": slug, "service": "esphome",
                 "config": {"host": host, "port": port}},
            ],
            "services": {"esphome": [slug]},
        }
    }


# -- Supervisor discovery, the authoritative path ---------------------------


async def test_supervisor_discovery_is_used_first() -> None:
    """The same source Home Assistant's own ESPHome integration uses."""
    session = FakeSession({
        "http://supervisor/discovery": (200, discovery_payload("172.30.33.4")),
        "http://172.30.33.4:6052": (200, {}),
    })
    client = EsphomeDashboardClient(session, supervisor_token="tok")

    location = await client.locate()
    assert location.base_url == "http://172.30.33.4:6052"
    assert location.source == "discovery"
    assert location.slug == "5c53de3b_esphome"


async def test_discovery_honours_a_non_default_port() -> None:
    session = FakeSession({
        "http://supervisor/discovery": (200, discovery_payload("esphome-host", 6099)),
        "http://esphome-host:6099": (200, {}),
    })
    location = await EsphomeDashboardClient(session, supervisor_token="tok").locate()
    assert location.base_url == "http://esphome-host:6099"


async def test_discovery_accepts_the_renamed_app_field() -> None:
    """Supervisor renamed the record's 'addon' field to 'app'."""
    payload = {"data": {"discovery": [
        {"app": "custom_esphome", "service": "esphome",
         "config": {"host": "h", "port": 6052}}
    ]}}
    session = FakeSession({
        "http://supervisor/discovery": (200, payload),
        "http://h:6052": (200, {}),
    })
    location = await EsphomeDashboardClient(session, supervisor_token="tok").locate()
    assert location.slug == "custom_esphome"


async def test_other_services_in_discovery_are_ignored() -> None:
    payload = {"data": {"discovery": [
        {"addon": "core_mosquitto", "service": "mqtt",
         "config": {"host": "mqtt", "port": 1883}}
    ]}}
    session = FakeSession({
        "http://supervisor/discovery": (200, payload),
        "http://5c53de3b-esphome:6052": (200, {}),
    })
    location = await EsphomeDashboardClient(session, supervisor_token="tok").locate()
    assert "mqtt" not in location.base_url


# -- fallbacks ---------------------------------------------------------------


async def test_a_custom_slug_is_found_via_the_service_map() -> None:
    """The whole point: an add-on from a repository we have never heard of."""
    payload = {"data": {"discovery": [], "services": {"esphome": ["deadbeef_esphome"]}}}
    session = FakeSession({
        "http://supervisor/discovery": (200, payload),
        "http://supervisor/addons/deadbeef_esphome/info":
            (200, {"data": {"hostname": "deadbeef-esphome", "state": "started"}}),
        "http://deadbeef-esphome:6052": (200, {}),
    })
    location = await EsphomeDashboardClient(session, supervisor_token="tok").locate()

    assert location.base_url == "http://deadbeef-esphome:6052"
    assert location.source == "addon-info"
    assert location.slug == "deadbeef_esphome"


async def test_a_known_slug_is_found_when_discovery_says_nothing() -> None:
    session = FakeSession({
        "http://supervisor/discovery": (200, {"data": {}}),
        "http://supervisor/addons/5c53de3b_esphome/info":
            (200, {"data": {"hostname": "5c53de3b-esphome", "state": "started"}}),
        "http://5c53de3b-esphome:6052": (200, {}),
    })
    location = await EsphomeDashboardClient(session, supervisor_token="tok").locate()
    assert location.source == "addon-info"


async def test_a_hostname_probe_works_with_no_supervisor_at_all() -> None:
    """Running outside Supervisor, or with the API unavailable."""
    session = FakeSession({"http://5c53de3b-esphome:6052": (200, {})})
    location = await EsphomeDashboardClient(session).locate()

    assert location.base_url == "http://5c53de3b-esphome:6052"
    assert location.source == "hostname-probe"


async def test_a_plain_esphome_hostname_is_tried() -> None:
    session = FakeSession({"http://esphome:6052": (200, {})})
    location = await EsphomeDashboardClient(session).locate()
    assert location.base_url == "http://esphome:6052"


# -- probing -----------------------------------------------------------------


async def test_an_authenticated_dashboard_still_counts_as_found() -> None:
    """401 proves it is there, just behind a login."""
    session = FakeSession({
        "http://supervisor/discovery": (200, discovery_payload("h")),
        "http://h:6052": (401, {}),
    })
    assert (await EsphomeDashboardClient(session, supervisor_token="tok").locate())


async def test_a_broken_dashboard_is_not_accepted() -> None:
    """A 5xx means something is wrong; keep looking rather than commit to it."""
    session = FakeSession({
        "http://supervisor/discovery": (200, discovery_payload("broken")),
        "http://broken:6052": (500, {}),
        "http://5c53de3b-esphome:6052": (200, {}),
    })
    location = await EsphomeDashboardClient(session, supervisor_token="tok").locate()
    assert "broken" not in location.base_url


async def test_probing_falls_through_to_another_path() -> None:
    """One endpoint being retired must not break detection."""
    class VersionGone(FakeSession):
        def get(self, url, **kwargs):
            self.requested.append(url)
            if url.endswith("/version"):
                return FakeResponse(404, {})
            if url.startswith("http://h:6052/devices"):
                return FakeResponse(200, {})
            raise aiohttp.ClientConnectionError(url)

    session = VersionGone({})
    client = EsphomeDashboardClient(session)
    client._configured_url = "http://h:6052"
    assert (await client.locate()).base_url == "http://h:6052"


# -- the configured override -------------------------------------------------


@pytest.mark.parametrize("configured,expected", [
    ("http://box:6052", "http://box:6052"),
    ("http://box:6052/", "http://box:6052"),
    ("box:6052", "http://box:6052"),
    ("box", "http://box:6052"),          # bare host gets the default port
    ("https://box:443", "https://box:443"),
])
async def test_the_configured_url_is_normalised(configured, expected) -> None:
    session = FakeSession({expected: (200, {})})
    location = await EsphomeDashboardClient(session, configured_url=configured).locate()
    assert location.base_url == expected
    assert location.source == "option"


async def test_a_configured_url_that_does_not_answer_names_itself() -> None:
    """Not silently falling through: the user said where it is, and was wrong."""
    client = EsphomeDashboardClient(FakeSession({}), configured_url="http://nope:6052")
    with pytest.raises(DashboardError, match="nope:6052"):
        await client.locate()


# -- failure and caching -----------------------------------------------------


async def test_not_finding_it_explains_what_was_tried() -> None:
    client = EsphomeDashboardClient(FakeSession({}), supervisor_token="tok")
    with pytest.raises(DashboardError) as err:
        await client.locate()

    message = str(err.value)
    assert "esphome_dashboard_url" in message
    assert "Tried:" in message


async def test_a_failure_is_not_cached() -> None:
    """An ESPHome add-on started later must be found without a restart."""
    session = FakeSession({})
    client = EsphomeDashboardClient(session)
    with pytest.raises(DashboardError):
        await client.locate()

    session.routes["http://5c53de3b-esphome:6052"] = (200, {})
    assert (await client.locate()).source == "hostname-probe"


async def test_a_success_is_cached() -> None:
    session = FakeSession({"http://esphome:6052": (200, {})})
    client = EsphomeDashboardClient(session)
    await client.locate()
    before = len(session.requested)

    await client.locate()
    assert len(session.requested) == before, "a cached location was re-probed"


async def test_forget_forces_rediscovery() -> None:
    session = FakeSession({"http://esphome:6052": (200, {})})
    client = EsphomeDashboardClient(session)
    await client.locate()
    client.forget()
    before = len(session.requested)

    await client.locate()
    assert len(session.requested) > before


# -- diagnostics -------------------------------------------------------------


async def test_diagnostics_reports_a_find() -> None:
    session = FakeSession({
        "http://supervisor/discovery": (200, discovery_payload("172.30.33.4")),
        "http://172.30.33.4:6052": (200, {}),
    })
    report = await EsphomeDashboardClient(session, supervisor_token="tok").diagnostics()

    assert report["found"] is True
    assert report["base_url"] == "http://172.30.33.4:6052"
    assert "Supervisor discovery" in report["description"]


async def test_diagnostics_reports_a_miss_without_raising() -> None:
    report = await EsphomeDashboardClient(FakeSession({})).diagnostics()

    assert report["found"] is False
    assert "error" in report
    assert report["attempts"]
