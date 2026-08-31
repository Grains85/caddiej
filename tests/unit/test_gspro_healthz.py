"""GSPro health is the adapter's snapshot, exposed on `/healthz`."""

from __future__ import annotations

from fastapi.testclient import TestClient
from tests.unit.test_gspro_adapter import Harness

from caddiej.adapters.gspro import GSProHealth
from caddiej.adapters.gspro.fakes import memory_connection
from caddiej.adapters.hud import ConnectionRegistry, create_app
from caddiej.app import _GSProHealth
from caddiej.domain.events import LinkState, ShotIngested
from caddiej.floor import FloorService


def _client(view: _GSProHealth) -> TestClient:
    return TestClient(
        create_app(FloorService(), ConnectionRegistry(), {"gspro": view.snapshot})
    )


def test_healthz_reports_gspro_disabled_when_there_is_no_adapter() -> None:
    body = _client(_GSProHealth()).get("/healthz").json()

    assert body["gspro"] == {
        "link_state": "disabled",
        "shots_ingested": 0,
        "messages_rejected": 0,
        "duplicates_ignored": 0,
        "reused_shot_numbers": 0,
        "late_data_discarded": 0,
        "frames_dropped": 0,
    }


def test_healthz_disabled_snapshot_matches_the_adapter_dataclass() -> None:
    health = GSProHealth(
        link_state=LinkState.DISABLED,
        shots_ingested=0,
        messages_rejected=0,
        duplicates_ignored=0,
        reused_shot_numbers=0,
        late_data_discarded=0,
        frames_dropped=0,
    )
    assert set(_GSProHealth().snapshot()) == set(health.__dataclass_fields__)


async def test_healthz_reports_listening_from_the_adapter() -> None:
    view = _GSProHealth()
    async with Harness() as harness:
        view.attach(harness.adapter)
        await harness.wait_for_link(LinkState.LISTENING)
        snapshot = view.snapshot()

    assert snapshot["link_state"] == "listening"
    assert snapshot["shots_ingested"] == 0
    assert _client(view).get("/healthz").json()["gspro"] == snapshot


async def test_healthz_reports_bridged_from_the_adapter() -> None:
    view = _GSProHealth()
    async with Harness() as harness:
        view.attach(harness.adapter)
        await harness.connect_monitor()
        snapshot = view.snapshot()

    assert snapshot["link_state"] == "bridged"


async def test_healthz_counters_follow_the_adapter() -> None:
    view = _GSProHealth()
    async with Harness() as harness:
        view.attach(harness.adapter)
        monitor = await harness.connect_monitor()
        before = view.snapshot()
        await monitor.hit_shot()
        await harness.expect(ShotIngested)
        after = view.snapshot()

    assert before["shots_ingested"] == 0
    assert after["shots_ingested"] == 1
    assert after["link_state"] == "bridged"


async def test_healthz_reports_simulator_unavailable() -> None:
    view = _GSProHealth()
    async with Harness() as harness:
        view.attach(harness.adapter)
        harness.simulator.reachable = False
        harness.listener.offer(memory_connection("refused").right)
        await harness.wait_for_link(LinkState.SIMULATOR_UNAVAILABLE)
        snapshot = view.snapshot()

    assert snapshot["link_state"] == "simulator_unavailable"


def test_healthz_reports_bind_failed_as_degraded() -> None:
    view = _GSProHealth()

    class _Failed:
        link_state = LinkState.BIND_FAILED

        def health(self) -> GSProHealth:
            return GSProHealth(
                link_state=LinkState.BIND_FAILED,
                shots_ingested=0,
                messages_rejected=0,
                duplicates_ignored=0,
                reused_shot_numbers=0,
                late_data_discarded=0,
                frames_dropped=0,
            )

    view.adapter = _Failed()  # type: ignore[assignment]
    body = _client(view).get("/healthz").json()
    assert body["gspro"]["link_state"] == "bind_failed"
    payload = view.subsystem()
    assert payload.enabled is True
    assert payload.available is False


def test_off_mode_is_a_disabled_subsystem() -> None:
    payload = _GSProHealth().subsystem()
    assert payload.enabled is False
    assert payload.available is False
