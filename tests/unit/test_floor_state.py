import pytest

from caddiej.floor import FloorMode, FloorService, FloorState


def test_initial_state_renders_nothing() -> None:
    state = FloorState()

    assert state.version == 0
    assert state.mode is FloorMode.STARTING
    assert state.status_text == ""


def test_evolve_increments_version_and_leaves_original_untouched() -> None:
    first = FloorState()
    second = first.evolve(status_text="ONLINE")

    assert second.version == 1
    assert second.status_text == "ONLINE"
    assert first.version == 0
    assert first.status_text == ""


def test_state_is_immutable() -> None:
    state = FloorState()

    with pytest.raises((AttributeError, TypeError)):
        state.status_text = "mutated"  # type: ignore[misc]


def test_service_transitions_publish_increasing_versions() -> None:
    service = FloorService()
    seen: list[FloorState] = []
    service.add_listener(seen.append)

    online = service.set_online("CADDIEJ FLOOR ONLINE")
    stopping = service.set_shutting_down()

    assert online.mode is FloorMode.ONLINE
    assert online.status_text == "CADDIEJ FLOOR ONLINE"
    assert stopping.mode is FloorMode.SHUTTING_DOWN
    assert [state.version for state in seen] == [1, 2]
    assert service.state is stopping


def test_setting_an_unchanged_value_is_not_a_new_version() -> None:
    # Under real event rates a repeated value would otherwise bump the version
    # and re-broadcast to every client for no reason.
    service = FloorService()
    service.set_online("CADDIEJ FLOOR ONLINE")
    seen: list[FloorState] = []
    service.add_listener(seen.append)

    unchanged = service.set_online("CADDIEJ FLOOR ONLINE")

    assert unchanged.version == 1
    assert seen == []

    changed = service.set_status_text("SOMETHING ELSE")

    assert changed.version == 2
    assert len(seen) == 1


def test_removed_listener_stops_receiving_updates() -> None:
    service = FloorService()
    seen: list[FloorState] = []
    remove = service.add_listener(seen.append)

    service.set_online("FIRST")
    remove()
    service.set_status_text("SECOND")

    assert [state.status_text for state in seen] == ["FIRST"]


def test_failing_listener_does_not_break_state_changes() -> None:
    service = FloorService()
    seen: list[FloorState] = []

    def broken(_state: FloorState) -> None:
        raise RuntimeError("listener exploded")

    service.add_listener(broken)
    service.add_listener(seen.append)

    service.set_online("STILL WORKS")

    assert service.state.status_text == "STILL WORKS"
    assert len(seen) == 1
