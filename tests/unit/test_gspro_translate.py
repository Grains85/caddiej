"""Mapping GSPro's vocabulary onto CaddieJ's.

Translation must not interpret. It converts units and names; it does not decide
what a shot means.
"""

import pytest

from caddiej.adapters.gspro.fakes import club_data, encode, player_information
from caddiej.adapters.gspro.translate import (
    carry_to_yards,
    player_from_response,
    to_ball_launch,
    to_club,
    to_club_delivery,
    to_handedness,
    to_monitor_status,
)
from caddiej.adapters.gspro.wire import (
    WireBallData,
    WireClubData,
    parse_launch_monitor_message,
    parse_simulator_response,
)
from caddiej.domain.shot import ClubKind, Detection, Handedness


def ball(**overrides: float) -> WireBallData:
    payload = {
        "Speed": 148.2,
        "HLA": 1.5,
        "VLA": 13.4,
        "SpinAxis": -4.2,
        "TotalSpin": 2750.0,
        "CarryDistance": 268.4,
    }
    payload.update({key.capitalize(): value for key, value in overrides.items()})
    return WireBallData.model_validate(payload)


def test_ball_data_maps_onto_named_units() -> None:
    launch = to_ball_launch(ball(), "Yards")

    assert launch.speed_mph == 148.2
    assert launch.launch_angle_degrees == 13.4
    assert launch.launch_direction_degrees == 1.5
    assert launch.total_spin_rpm == 2750.0
    assert launch.spin_axis_degrees == -4.2
    assert launch.carry_yards == 268.4


def test_a_metric_device_has_its_carry_converted() -> None:
    # Speeds and angles are unit-independent in Open Connect; distances are not.
    launch = to_ball_launch(ball(), "Meters")

    assert launch.carry_yards == pytest.approx(293.5, abs=0.1)
    assert launch.speed_mph == 148.2


@pytest.mark.parametrize("units", ["Meters", "meters", " METRES ", "metre"])
def test_metric_unit_spellings_are_all_recognised(units: str) -> None:
    assert carry_to_yards(100.0, units) == pytest.approx(109.36, abs=0.01)


@pytest.mark.parametrize("units", ["Yards", None, "", "unknown"])
def test_anything_not_metric_is_treated_as_yards(units: str | None) -> None:
    assert carry_to_yards(100.0, units) == 100.0


def test_a_missing_carry_stays_missing() -> None:
    assert carry_to_yards(None, "Meters") is None


def test_club_data_maps_including_impact_position() -> None:
    delivery = to_club_delivery(WireClubData.model_validate(club_data()))

    assert delivery.speed_mph == 104.5
    assert delivery.angle_of_attack_degrees == -1.8
    assert delivery.face_to_target_degrees == 1.2
    assert delivery.path_degrees == -2.4
    assert delivery.impact_vertical_mm == 2.0
    assert delivery.impact_horizontal_mm == -3.0


def test_face_to_path_is_derived_from_face_and_path() -> None:
    delivery = to_club_delivery(
        WireClubData.model_validate(club_data(face_to_target=2.0, path=-3.0))
    )

    assert delivery.face_to_path_degrees == pytest.approx(5.0)


def test_face_to_path_is_absent_when_either_input_is() -> None:
    delivery = to_club_delivery(WireClubData.model_validate({"Speed": 90.0}))

    assert delivery.face_to_path_degrees is None


@pytest.mark.parametrize(
    "code,expected",
    [
        ("DR", ClubKind.DRIVER),
        ("PT", ClubKind.PUTTER),
        ("W3", ClubKind.WOOD),
        ("H4", ClubKind.HYBRID),
        ("I7", ClubKind.IRON),
        ("PW", ClubKind.WEDGE),
        ("GW", ClubKind.WEDGE),
        ("SW", ClubKind.WEDGE),
        ("LW", ClubKind.WEDGE),
        ("dr", ClubKind.DRIVER),
        (" PT ", ClubKind.PUTTER),
    ],
)
def test_gspro_club_codes_map_to_categories(code: str, expected: ClubKind) -> None:
    club = to_club(code)

    assert club is not None
    assert club.kind is expected


def test_an_unrecognised_club_code_is_unknown_rather_than_an_error() -> None:
    # A GSPro release adding a club must not stop shots being ingested.
    club = to_club("XQ")

    assert club is not None
    assert club.kind is ClubKind.UNKNOWN
    assert club.vendor_code == "XQ"


def test_the_vendor_code_is_kept_for_provenance() -> None:
    assert to_club("I7") is not None
    club = to_club("I7")
    assert club is not None and club.vendor_code == "I7"


@pytest.mark.parametrize("code", [None, "", "   "])
def test_no_club_code_means_no_club(code: str | None) -> None:
    assert to_club(code) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("RH", Handedness.RIGHT),
        ("LH", Handedness.LEFT),
        ("right", Handedness.RIGHT),
        ("LEFT", Handedness.LEFT),
        ("whatever", Handedness.UNKNOWN),
        (None, Handedness.UNKNOWN),
    ],
)
def test_handedness_mapping(value: str | None, expected: Handedness) -> None:
    assert to_handedness(value) is expected


def test_player_information_yields_handedness_and_club() -> None:
    response = parse_simulator_response(encode(player_information("LH", "PT")))

    handedness, club = player_from_response(response)

    assert handedness is Handedness.LEFT
    assert club is not None and club.kind is ClubKind.PUTTER


def test_a_response_with_no_player_block_changes_nothing() -> None:
    response = parse_simulator_response(b'{"Code": 200, "Message": "ok"}')

    assert player_from_response(response) == (None, None)


def test_a_partial_player_block_reports_only_what_it_said() -> None:
    # Absence means unchanged, not unknown, so the caller can ignore it.
    response = parse_simulator_response(b'{"Code": 201, "Player": {"Club": "W3"}}')

    handedness, club = player_from_response(response)

    assert handedness is None
    assert club is not None and club.kind is ClubKind.WOOD


def test_ball_detection_is_unknown_when_the_device_never_says() -> None:
    # A device that does not report detection must not read as reporting none.
    message = parse_launch_monitor_message(
        b'{"ShotDataOptions": {"LaunchMonitorIsReady": true}}'
    )

    status = to_monitor_status(message)

    assert status.ready is True
    assert status.ball is Detection.UNKNOWN


@pytest.mark.parametrize(
    "flag,expected",
    [("true", Detection.PRESENT), ("false", Detection.ABSENT)],
)
def test_ball_detection_flags_map_to_detection(flag: str, expected: Detection) -> None:
    message = parse_launch_monitor_message(
        f'{{"ShotDataOptions": {{"LaunchMonitorBallDetected": {flag}}}}}'.encode()
    )

    assert to_monitor_status(message).ball is expected


def test_a_message_without_options_reports_nothing_about_the_device() -> None:
    message = parse_launch_monitor_message(b'{"DeviceID": "LM"}')

    status = to_monitor_status(message)

    assert status.ready is None
    assert status.ball is Detection.UNKNOWN
