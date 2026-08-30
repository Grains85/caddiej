"""The published surface, pinned.

These are the tests that make the contract a contract. Every one of them exists
to fail when somebody changes a field name, a type, or a default, because the
whole point of writing the wire models out by hand is that such a change should
be a decision rather than a consequence.

The golden dictionaries below are deliberately literal and deliberately
complete. Reading one tells you exactly what an assistant receives, which makes
this file the second place to look after the tool reference.
"""

import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from caddiej.assistant.contract import (
    CONTRACT_VERSION,
    TRAINING_FOCUSES,
    Availability,
    BallPayload,
    ClubDeliveryPayload,
    CoachingLinePayload,
    CurrentClubResult,
    ErrorResult,
    LastShotResult,
    MetricPayload,
    RecentCoachingResult,
    SessionStatsResult,
    SetTrainingFocusArguments,
    ShotPayload,
    StatusResult,
    SubsystemPayload,
    TrainingFocusResult,
)
from caddiej.domain.coaching import CoachingFocus

MOMENT = "2026-04-01T18:30:00Z"


def imported_by(*modules: str) -> set[str]:
    """Which CaddieJ modules a fresh interpreter loads to import these.

    Shared with the MCP tests, which ask the same question of a different set of
    modules and for the same reason.
    """

    program = (
        "import sys, json\n"
        f"for name in {list(modules)!r}:\n"
        "    __import__(name)\n"
        "print(json.dumps([n for n in sys.modules if n.startswith('caddiej')]))"
    )
    finished = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(json.loads(finished.stdout))


# --- the shapes -----------------------------------------------------------


def test_a_shot_serialises_to_exactly_these_fields() -> None:
    payload = ShotPayload(
        shot_id="6f1c7e0e-0000-4000-8000-000000000001",
        ordinal=12,
        observed_at=MOMENT,
        club="driver",
        handedness="right",
        ball=BallPayload(
            speed_mph=148.2,
            launch_angle_degrees=12.6,
            launch_direction_degrees=1.5,
            carry_yards=268.4,
            total_spin_rpm=2480.0,
            back_spin_rpm=2400.0,
            side_spin_rpm=-180.0,
            spin_axis_degrees=-4.2,
        ),
        club_delivery=ClubDeliveryPayload(
            speed_mph=112.4,
            angle_of_attack_degrees=3.2,
            path_degrees=-2.4,
            face_to_target_degrees=1.1,
            face_to_path_degrees=3.5,
            loft_degrees=10.5,
            impact_horizontal_mm=-4.0,
            impact_vertical_mm=2.0,
        ),
    )

    assert payload.model_dump(mode="json") == {
        "shot_id": "6f1c7e0e-0000-4000-8000-000000000001",
        "ordinal": 12,
        "observed_at": "2026-04-01T18:30:00Z",
        "club": "driver",
        "handedness": "right",
        "ball": {
            "speed_mph": 148.2,
            "launch_angle_degrees": 12.6,
            "launch_direction_degrees": 1.5,
            "carry_yards": 268.4,
            "total_spin_rpm": 2480.0,
            "back_spin_rpm": 2400.0,
            "side_spin_rpm": -180.0,
            "spin_axis_degrees": -4.2,
        },
        "club_delivery": {
            "speed_mph": 112.4,
            "angle_of_attack_degrees": 3.2,
            "path_degrees": -2.4,
            "face_to_target_degrees": 1.1,
            "face_to_path_degrees": 3.5,
            "loft_degrees": 10.5,
            "impact_horizontal_mm": -4.0,
            "impact_vertical_mm": 2.0,
        },
    }


def test_a_shot_carries_no_device_provenance() -> None:
    # Not an omission to be tidied up later. A device id, a device session and a
    # sequence number are diagnostics for somebody holding the launch monitor,
    # and the raw payload is a debugging capture. None of them belongs in a
    # conversation about golf, so none of them has a field to travel in.
    forbidden = {
        "device_id",
        "device_session_id",
        "device_sequence",
        "raw_payload",
        "source",
        "reported_units",
        "api_version",
        "vendor_code",
        "observed_monotonic",
    }

    assert forbidden.isdisjoint(ShotPayload.model_fields)


def test_a_status_serialises_to_exactly_these_fields() -> None:
    payload = StatusResult(
        caddiej_version="0.1.0",
        uptime_seconds=42.5,
        session_id="6f1c7e0e-0000-4000-8000-000000000002",
        player_name="Johan",
        training_focus="path",
        subsystems={"history": SubsystemPayload(enabled=True, available=True)},
        capabilities=["get_caddiej_status"],
    )

    assert payload.model_dump(mode="json") == {
        "availability": "ok",
        "contract_version": CONTRACT_VERSION,
        "caddiej_version": "0.1.0",
        "uptime_seconds": 42.5,
        "session_id": "6f1c7e0e-0000-4000-8000-000000000002",
        "player_name": "Johan",
        "training_focus": "path",
        "subsystems": {"history": {"enabled": True, "available": True}},
        "capabilities": ["get_caddiej_status"],
    }


def test_a_subsystem_reports_two_booleans_and_nothing_else() -> None:
    # The counters on `/healthz` are telemetry whose shape changes as subsystems
    # grow. Publishing them here would make every one of those changes a
    # breaking change to somebody's assistant.
    assert set(SubsystemPayload.model_fields) == {"enabled", "available"}


def test_a_coaching_line_serialises_to_exactly_these_fields() -> None:
    payload = CoachingLinePayload(
        shot_id="6f1c7e0e-0000-4000-8000-000000000003",
        focus="face_control",
        severity=0.62,
        band="fault",
        source="model",
        created_at=MOMENT,
        display="FACE OPEN",
        speech="Square the face a little earlier.",
    )

    assert payload.model_dump(mode="json") == {
        "shot_id": "6f1c7e0e-0000-4000-8000-000000000003",
        "focus": "face_control",
        "severity": 0.62,
        "band": "fault",
        "source": "model",
        "created_at": "2026-04-01T18:30:00Z",
        "display": "FACE OPEN",
        "speech": "Square the face a little earlier.",
    }


def test_a_coaching_line_cannot_carry_a_path_to_anything() -> None:
    # A clip path is a location on the operator's disk. An assistant that could
    # read one would be a file oracle rather than a caddie.
    assert "clip_path" not in CoachingLinePayload.model_fields


def test_a_metric_serialises_to_exactly_these_fields() -> None:
    payload = MetricPayload(
        name="carry",
        unit="yards",
        count=3,
        mean=268.4,
        median=268.0,
        minimum=260.0,
        maximum=275.0,
        standard_deviation=7.5,
    )

    assert payload.model_dump(mode="json") == {
        "name": "carry",
        "unit": "yards",
        "count": 3,
        "mean": 268.4,
        "median": 268.0,
        "minimum": 260.0,
        "maximum": 275.0,
        "standard_deviation": 7.5,
    }


def test_session_stats_leave_out_shot_by_shot_dispersion() -> None:
    # One row per shot, existing so that something which knows the shape of a
    # surface can draw a picture. Four hundred of them in a context window is an
    # expensive way to answer "how am I hitting it".
    assert "dispersion" not in SessionStatsResult.model_fields


def test_an_error_says_what_and_why_and_nothing_more() -> None:
    assert ErrorResult(error="invalid_argument", detail="no").model_dump() == {
        "error": "invalid_argument",
        "detail": "no",
    }


# --- absence is never ambiguous -------------------------------------------


@pytest.mark.parametrize(
    "model",
    [LastShotResult, SessionStatsResult, CurrentClubResult, RecentCoachingResult],
)
def test_every_answer_can_say_why_it_is_empty(model: type) -> None:
    assert "availability" in model.model_fields


def test_the_three_availabilities_are_the_whole_vocabulary() -> None:
    # `empty` and `unavailable` are the two an assistant must never conflate:
    # one is a fact about golf, the other about a computer.
    assert {member.value for member in Availability} == {
        "ok",
        "empty",
        "unavailable",
    }


def test_an_empty_answer_defaults_to_carrying_nothing() -> None:
    result = LastShotResult(availability=Availability.EMPTY)

    assert result.shot is None
    assert result.model_dump(mode="json") == {"availability": "empty", "shot": None}


def test_empty_session_stats_are_zeroes_rather_than_nulls() -> None:
    # A count of shots is genuinely zero when nobody has hit any; a null there
    # would make every consumer handle a case that does not exist.
    result = SessionStatsResult(availability=Availability.EMPTY)

    assert result.shots == 0
    assert result.overall == []
    assert result.by_club == []
    assert result.faults == {}


# --- the way in -----------------------------------------------------------


def test_an_unrecognised_argument_is_refused_rather_than_ignored() -> None:
    # An argument CaddieJ does not know means the caller believes something
    # untrue about this API. Accepting it quietly is how an assistant comes to
    # think it turned a feature on.
    with pytest.raises(ValidationError):
        SetTrainingFocusArguments.model_validate({"focus": "path", "force": True})


def test_the_mutation_accepts_one_argument_and_it_may_be_null() -> None:
    assert set(SetTrainingFocusArguments.model_fields) == {"focus"}
    assert SetTrainingFocusArguments().focus is None


def test_the_mutation_reports_what_it_changed_from() -> None:
    result = TrainingFocusResult(
        availability=Availability.OK,
        training_focus="path",
        previous_focus="strike",
        changed=True,
    )

    assert result.previous_focus == "strike"
    assert result.changed is True


def test_wire_models_are_frozen() -> None:
    # Nothing downstream should be able to edit an answer and pass it on as
    # CaddieJ's.
    result = LastShotResult(availability=Availability.EMPTY)

    with pytest.raises(ValidationError):
        result.availability = Availability.OK  # type: ignore[misc]


# --- the properties that keep this module shareable -----------------------


def test_the_focus_literals_match_the_domain_enum() -> None:
    # The contract spells the list out so it imports nothing from CaddieJ; this
    # is what stops the two from drifting.
    assert TRAINING_FOCUSES == tuple(
        focus.value for focus in CoachingFocus if focus is not CoachingFocus.NONE
    )


def test_none_is_not_something_to_work_on() -> None:
    assert CoachingFocus.NONE.value not in TRAINING_FOCUSES


def test_the_contract_and_registry_import_nothing_else_of_caddiej() -> None:
    """The property that lets the MCP process share these two modules.

    Checked in a fresh interpreter rather than in this one, because by the time
    this test runs the suite has imported most of CaddieJ and every transitive
    import would already be satisfied. A subprocess is the only place the
    question means anything.
    """

    loaded = imported_by("caddiej.assistant.contract", "caddiej.assistant.registry")
    unexpected = loaded - {
        "caddiej",
        "caddiej.assistant",
        "caddiej.assistant.contract",
        "caddiej.assistant.registry",
    }

    assert unexpected == set(), (
        "the contract and registry are shared with a separate process; "
        f"these imports would travel with them: {sorted(unexpected)}"
    )
