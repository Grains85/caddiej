import json

import pytest

from caddiej.adapters.hud.protocol import (
    PROTOCOL_VERSION,
    AddressPayload,
    BallMarkerPayload,
    BallStatusPayload,
    ClientMessage,
    ClubPathPayload,
    ErrorMessage,
    FloorStatePayload,
    FlourishPayload,
    HittingStripPayload,
    LockAcknowledgementPayload,
    PongMessage,
    RoundResultPayload,
    ShotMetricsPayload,
    hello_message,
    snapshot_message,
)
from caddiej.floor import (
    AddressView,
    BallMarker,
    BallStatus,
    ClubPath,
    FloorFlourish,
    FloorMode,
    FloorPoint,
    FloorState,
    HittingStrip,
    LockAcknowledgement,
    MarkerVariant,
    RoundResultView,
    ShotMetrics,
)


def test_hello_carries_the_protocol_version() -> None:
    payload = json.loads(hello_message().model_dump_json())

    assert payload["type"] == "hello"
    assert payload["v"] == PROTOCOL_VERSION
    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["server_time"]
    assert payload["hud_assets_version"] == ""


def test_hello_can_carry_the_overlay_asset_hash() -> None:
    payload = json.loads(
        hello_message(hud_assets_version="abc123abc123").model_dump_json()
    )

    assert payload["hud_assets_version"] == "abc123abc123"


def test_snapshot_sequence_tracks_the_state_version() -> None:
    state = FloorState().evolve(
        mode=FloorMode.ONLINE, status_text="CADDIEJ FLOOR ONLINE"
    )

    payload = json.loads(snapshot_message(state).model_dump_json())

    assert payload["type"] == "snapshot"
    assert payload["seq"] == state.version == 1
    assert payload["state"] == {
        "version": 1,
        "mode": "online",
        "status_text": "CADDIEJ FLOOR ONLINE",
        "marker": {
            "position": {"x": 0.5, "y": 0.5},
            "variant": "normal",
            "visible": False,
        },
        "metrics": None,
        "club_path": None,
        "advice": None,
        "flourish": None,
        "play_status": "",
        "club_label": "",
        "ball_status": None,
        "round_result": None,
        "ball_flight": None,
        "hitting_strip": None,
        "address": None,
        "lock_acknowledgement": None,
        # Three layers the presenter has not written yet. Null rather than
        # absent: the overlay falls back to the stylesheet's own placement for
        # anchors, and draws nothing for a lane or a roll.
        "anchors": None,
        "putting_lane": None,
        "putt_path": None,
        "progress": None,
    }


def test_a_populated_floor_serialises_completely() -> None:
    state = FloorState().evolve(
        mode=FloorMode.ONLINE,
        marker=BallMarker(
            position=FloorPoint(x=0.25, y=0.75),
            variant=MarkerVariant.DRIVER,
            visible=True,
        ),
        metrics=ShotMetrics(
            ball_speed_mph=148.2, launch_angle_degrees=12.6, carry_yards=268.4
        ),
        club_path=ClubPath(
            start=FloorPoint(x=0.5, y=0.8), angle_degrees=90.0, length=0.2
        ),
    )

    payload = json.loads(snapshot_message(state).model_dump_json())["state"]

    assert payload["marker"] == {
        "position": {"x": 0.25, "y": 0.75},
        "variant": "driver",
        "visible": True,
    }
    assert payload["metrics"] == {
        "ball_speed_mph": 148.2,
        "launch_angle_degrees": 12.6,
        "carry_yards": 268.4,
    }
    assert payload["club_path"]["start"] == {"x": 0.5, "y": 0.8}
    assert payload["club_path"]["angle_degrees"] == 90.0
    assert payload["club_path"]["length"] == 0.2


def test_a_metric_the_device_never_measured_travels_as_null() -> None:
    # Most launch monitors do not measure carry. The overlay leaves the row out;
    # sending zero would put a number on the floor that nothing measured.
    state = FloorState().evolve(
        metrics=ShotMetrics(ball_speed_mph=148.2, launch_angle_degrees=12.6)
    )

    payload = json.loads(snapshot_message(state).model_dump_json())["state"]

    assert payload["metrics"] == {
        "ball_speed_mph": 148.2,
        "launch_angle_degrees": 12.6,
        "carry_yards": None,
    }


def test_the_club_path_end_point_is_computed_before_it_is_sent() -> None:
    # The renderer must not do trigonometry: a renderer that computes geometry
    # is a renderer that can disagree with Python.
    state = FloorState().evolve(
        club_path=ClubPath(
            start=FloorPoint(x=0.5, y=0.8), angle_degrees=0.0, length=0.3
        )
    )

    end = json.loads(snapshot_message(state).model_dump_json())["state"]["club_path"][
        "end"
    ]

    assert end["x"] == pytest.approx(0.5)
    assert end["y"] == pytest.approx(0.5)


def test_every_snapshot_is_valid_json_for_a_browser() -> None:
    # NaN and Infinity serialise to tokens JSON.parse rejects, which would leave
    # the overlay blank with only a client-side parse error to go on.
    state = FloorState().evolve(
        marker=BallMarker(
            position=FloorPoint(x=1.0, y=0.0),
            variant=MarkerVariant.PUTTER,
            visible=True,
        ),
        club_path=ClubPath(
            start=FloorPoint(x=0.5, y=0.5), angle_degrees=123.456, length=2.5
        ),
        metrics=ShotMetrics(
            ball_speed_mph=0.0, launch_angle_degrees=-3.5, carry_yards=1e6
        ),
    )

    raw = snapshot_message(state).model_dump_json()

    assert "NaN" not in raw
    assert "Infinity" not in raw
    json.loads(raw)


def test_snapshot_payload_covers_every_state_field() -> None:
    # A new FloorState field must be added to the wire payload deliberately,
    # rather than silently failing to reach the browser.
    state_fields = set(FloorState.__dataclass_fields__)
    payload_fields = set(FloorStatePayload.model_fields)

    assert state_fields == payload_fields


def test_nested_payloads_cover_every_field_of_their_value_objects() -> None:
    # The same guard one level down, so adding a field to a marker or a set of
    # metrics cannot silently stop at the wire.
    assert set(BallMarker.__dataclass_fields__) == set(BallMarkerPayload.model_fields)
    assert set(ShotMetrics.__dataclass_fields__) == set(ShotMetricsPayload.model_fields)
    # End is stored, not derived at the wire: the renderer must be given both
    # points. The dataclass and the payload therefore name the same fields.
    assert set(ClubPath.__dataclass_fields__) == set(ClubPathPayload.model_fields)
    assert set(FloorFlourish.__dataclass_fields__) == set(FlourishPayload.model_fields)
    # Raw floats stay on the view and are omitted from the overlay JSON.
    payload_fields = set(RoundResultPayload.model_fields)
    view_fields = set(RoundResultView.__dataclass_fields__)
    assert payload_fields <= view_fields
    assert "ball_speed_raw" not in payload_fields
    assert "total_distance_raw" not in payload_fields
    assert "distance_to_pin_raw" not in payload_fields
    assert payload_fields == view_fields - {
        "ball_speed_raw",
        "total_distance_raw",
        "distance_to_pin_raw",
    }
    assert set(HittingStrip.__dataclass_fields__) == set(
        HittingStripPayload.model_fields
    )
    assert set(AddressView.__dataclass_fields__) == set(AddressPayload.model_fields)
    assert set(LockAcknowledgement.__dataclass_fields__) == set(
        LockAcknowledgementPayload.model_fields
    )
    assert set(BallStatus.__dataclass_fields__) == set(BallStatusPayload.model_fields)


def test_a_ball_lock_acknowledgement_travels_with_the_duration_it_will_be_held_for() -> (
    None
):
    state = FloorState().evolve(lock_acknowledgement=LockAcknowledgement(seconds=0.65))

    payload = json.loads(snapshot_message(state).model_dump_json())["state"]

    assert payload["lock_acknowledgement"] == {"seconds": 0.65}


def test_a_celebration_travels_with_the_duration_it_will_be_held_for() -> None:
    # The browser paces its own animation to this, so that the label and the
    # lights finish together rather than each running to a length of its own.
    state = FloorState().evolve(
        flourish=FloorFlourish(
            cue="hole_in_one", label="HOLE IN ONE", intensity=1.0, seconds=5.0
        )
    )

    payload = json.loads(snapshot_message(state).model_dump_json())["state"]

    assert payload["flourish"] == {
        "cue": "hole_in_one",
        "label": "HOLE IN ONE",
        "intensity": 1.0,
        "seconds": 5.0,
    }


def test_client_message_ignores_unknown_fields() -> None:
    message = ClientMessage.model_validate_json(
        '{"type": "hello", "surface": "floor", "future_field": 42}'
    )

    assert message.type == "hello"
    assert message.surface == "floor"


def test_control_messages_are_tagged() -> None:
    assert json.loads(PongMessage().model_dump_json())["type"] == "pong"
    error = json.loads(ErrorMessage(detail="nope").model_dump_json())
    assert error == {"v": PROTOCOL_VERSION, "type": "error", "detail": "nope"}


def test_the_hud_payload_shows_integer_kmh_and_metres_not_raw_floats() -> None:
    # This is the JSON the Browser Source receives. Raw GSPro floats stay on
    # RoundResultView; they must not appear in the round-result object at all.
    raw_speed = 32.83863067626953
    raw_distance = 28.0288715
    state = FloorState().evolve(
        round_result=RoundResultView(
            shot_id="D",
            ball_speed_raw=raw_speed,
            total_distance_raw=raw_distance,
        )
    )

    payload = json.loads(snapshot_message(state).model_dump_json())["state"]
    panel = payload["round_result"]
    wire = json.dumps(panel)
    # Mirrors state.js: only a string display value is assigned to the DOM.
    dom_ball_speed = (
        panel["ball_speed_display"]
        if isinstance(panel["ball_speed_display"], str)
        else "—"
    )
    dom_distance = (
        panel["shot_distance_display"]
        if isinstance(panel["shot_distance_display"], str)
        else "—"
    )

    assert "ball_speed_raw" not in panel
    assert "total_distance_raw" not in panel
    assert panel["ball_speed_display"] == "118 km/h"
    assert panel["shot_distance_display"] == "28 m"
    assert dom_ball_speed == "118 km/h"
    assert dom_distance == "28 m"
    assert "32.83863067626953" not in wire
    assert "28.0288715" not in wire
    assert state.round_result is not None
    assert state.round_result.ball_speed_raw == raw_speed
    assert state.round_result.total_distance_raw == raw_distance


def test_protocol_formats_even_when_the_view_omitted_display_strings() -> None:
    result = RoundResultView(shot_id="D", ball_speed_raw=32.84, total_distance_raw=28.0)
    payload = json.loads(RoundResultPayload.from_result(result).model_dump_json())

    assert payload["ball_speed_display"] == "118 km/h"
    assert payload["shot_distance_display"] == "28 m"


def test_the_address_hud_payload_is_display_strings_not_raw_floats() -> None:
    state = FloorState().evolve(
        address=AddressView(
            club_display="7 IRON",
            distance_display="147 m",
            recommended_club_display="7I",
        )
    )
    panel = json.loads(snapshot_message(state).model_dump_json())["state"]["address"]

    assert panel == {
        "club_display": "7 IRON",
        "distance_display": "147 m",
        "recommended_club_display": "7I",
    }
    assert "distance_raw" not in panel
    assert "347.0" not in json.dumps(panel)
    # The rule, not the number: which club a distance calls for is decided in
    # the domain, so no carry the decision was made from crosses the wire.
    assert "expected_carry_m" not in panel
