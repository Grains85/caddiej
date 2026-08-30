"""The demo sequence: a development writer, held to the same rules as any other."""

import asyncio
import json

import pytest

from caddiej.adapters.hud.protocol import snapshot_message
from caddiej.domain.scoring import HoleResult
from caddiej.effects.catalogue import EFFECTS
from caddiej.floor import FloorService, FloorState, MarkerVariant, run_demo
from caddiej.floor.demo import DEMO_FLOURISHES, demo_steps


def apply_all(service: FloorService) -> list[FloorState]:
    """Run one pass from the state the application is actually in when it starts.

    The demo begins after startup has put the floor online with a banner, so a
    fresh service is the wrong starting point: it would make the first step —
    clearing that banner — a no-op that the service correctly suppresses.
    """

    service.set_online("CADDIEJ FLOOR ONLINE")
    seen: list[FloorState] = []
    service.add_listener(seen.append)
    for _name, apply in demo_steps():
        apply(service)
    return seen


def test_the_sequence_exercises_every_marker_variant() -> None:
    # The point of the demo is that nothing can only be seen with real hardware.
    service = FloorService()
    seen = apply_all(service)

    shown = {state.marker.variant for state in seen if state.marker.visible}
    assert shown == set(MarkerVariant)


def test_the_sequence_covers_hiding_the_marker() -> None:
    service = FloorService()
    seen = apply_all(service)

    assert any(state.marker.visible for state in seen)
    assert any(not state.marker.visible for state in seen)


def test_the_sequence_covers_metrics_and_club_path_appearing_and_clearing() -> None:
    service = FloorService()
    seen = apply_all(service)

    assert any(state.metrics is not None for state in seen)
    assert any(state.club_path is not None for state in seen)
    assert seen[-1].metrics is None
    assert seen[-1].club_path is None


def test_the_sequence_reaches_the_edges_of_the_surface() -> None:
    service = FloorService()
    seen = apply_all(service)

    positions = [state.marker.position for state in seen]
    assert any(point.x == 0.0 and point.y == 0.0 for point in positions)
    assert any(point.x == 1.0 and point.y == 1.0 for point in positions)


def test_every_step_of_a_pass_changes_the_floor() -> None:
    # A step that changes nothing looks identical to a stalled overlay, so each
    # one must move the floor somewhere new.
    service = FloorService()
    seen = apply_all(service)

    assert len(seen) == len(demo_steps())
    first_version = seen[0].version
    assert [state.version for state in seen] == list(
        range(first_version, first_version + len(seen))
    )


def test_the_step_names_are_unique_so_logs_identify_the_state() -> None:
    names = [name for name, _apply in demo_steps()]

    assert len(names) == len(set(names))


def test_the_sequence_shows_every_celebration_and_then_clears_it() -> None:
    # Judging "is a birdie too much?" needs the actual projector, so every tier
    # has to be reachable without a scorecard existing.
    service = FloorService()
    seen = apply_all(service)

    shown = [state.flourish.cue for state in seen if state.flourish is not None]
    assert shown == [cue for cue, _label, _intensity, _seconds in DEMO_FLOURISHES]
    assert seen[-1].flourish is None


def test_the_demo_celebrations_still_match_the_effects_catalogue() -> None:
    """The demo writes its own values because the effects package depends on the
    floor package and the dependency may not run both ways. This is what stops
    the two from drifting apart quietly."""

    catalogued = [
        (effect.cue, effect.label, effect.intensity, effect.seconds)
        for effect in (EFFECTS[result] for result in HoleResult)
    ]

    assert list(DEMO_FLOURISHES) == catalogued


async def test_the_demo_advances_over_time_and_stops_when_cancelled() -> None:
    service = FloorService()
    versions: list[int] = []
    service.add_listener(lambda state: versions.append(state.version))

    task = asyncio.create_task(run_demo(service, step_seconds=0.001))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(versions) > 1, "the demo should have advanced through several states"


async def test_the_demo_loops_rather_than_finishing() -> None:
    # It runs unattended while someone stands at the projector, so it must not
    # stop after one pass.
    service = FloorService()
    steps = demo_steps()

    task = asyncio.create_task(run_demo(service, step_seconds=0))
    await asyncio.sleep(0.05)
    running = not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert running
    assert service.state.version > len(steps)


def test_every_state_the_demo_produces_survives_the_wire() -> None:
    # The demo is the widest set of states CaddieJ can currently produce, so it
    # doubles as a serialisation sweep: every one must reach a browser as JSON.
    service = FloorService()

    for state in apply_all(service):
        raw = snapshot_message(state).model_dump_json()
        parsed = json.loads(raw)
        assert parsed["seq"] == state.version
        assert parsed["state"]["marker"]["variant"] == state.marker.variant.value
