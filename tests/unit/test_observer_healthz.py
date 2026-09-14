"""Observer health on /healthz does not fail the process when files are missing."""

from fastapi.testclient import TestClient

from caddiej.adapters.gspro.round_file import GSProRoundFileAdapter
from caddiej.adapters.hud import ConnectionRegistry, create_app
from caddiej.adapters.protee import ProTeeLogAdapter
from caddiej.app import _ProTeeObserverHealth, _RoundObserverHealth
from caddiej.core.events import Event
from caddiej.floor import FloorService


async def _ignore(_event: Event) -> None:
    return None


def test_healthz_reports_disabled_observers_without_failing() -> None:
    client = TestClient(
        create_app(
            FloorService(),
            ConnectionRegistry(),
            {
                "gspro_round_observer": _RoundObserverHealth().snapshot,
                "protee_observer": _ProTeeObserverHealth().snapshot,
            },
        )
    )
    body = client.get("/healthz").json()

    assert body["status"] == "ok"
    assert body["gspro_round_observer"]["status"] == "disabled"
    assert body["protee_observer"]["status"] == "disabled"


def test_healthz_reports_waiting_when_files_are_absent() -> None:
    round_view = _RoundObserverHealth()
    round_view.attach(GSProRoundFileAdapter(None, _ignore))
    protee_view = _ProTeeObserverHealth()
    protee_view.attach(ProTeeLogAdapter(None, _ignore))
    client = TestClient(
        create_app(
            FloorService(),
            ConnectionRegistry(),
            {
                "gspro_round_observer": round_view.snapshot,
                "protee_observer": protee_view.snapshot,
            },
        )
    )
    body = client.get("/healthz").json()

    assert body["status"] == "ok"
    assert body["gspro_round_observer"]["status"] == "waiting"
    assert body["protee_observer"]["status"] == "waiting"
    assert body["gspro_round_observer"]["shots_observed"] == 0
    assert body["gspro_round_observer"]["parse_failures"] == 0
