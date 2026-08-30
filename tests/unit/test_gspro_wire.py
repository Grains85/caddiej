"""Validating GSPro messages at the boundary.

The rule being tested: a message is either understood and applied whole, or
rejected and applied not at all.
"""

import json

import pytest

from caddiej.adapters.gspro.errors import GSProProtocolError
from caddiej.adapters.gspro.fakes import (
    MALFORMED_PAYLOADS,
    encode,
    heartbeat,
    player_information,
    shot_acknowledgement,
    shot_message,
)
from caddiej.adapters.gspro.wire import (
    parse_launch_monitor_message,
    parse_simulator_response,
)
from caddiej.domain.events import RejectionReason


def test_a_complete_shot_message_parses() -> None:
    message = parse_launch_monitor_message(encode(shot_message(7)))

    assert message.shot_number == 7
    assert message.device_id == "CaddieJ Fake LM"
    assert message.api_version == "1"
    assert message.declares_ball_data
    assert message.declares_club_data
    assert message.ball_data is not None
    assert message.ball_data.speed == 148.2
    assert message.ball_data.vla == 13.4
    assert message.ball_data.hla == 1.5


def test_a_heartbeat_is_recognised_and_declares_no_shot_data() -> None:
    # The important case: heartbeats carry zero-filled data blocks, so trusting
    # block presence would turn every heartbeat into a shot at zero mph.
    message = parse_launch_monitor_message(encode(heartbeat()))

    assert message.is_heartbeat
    assert message.ball_data is not None
    assert not message.declares_ball_data
    assert not message.declares_club_data


def test_data_present_without_a_flag_is_believed() -> None:
    # No flag means the data is all there is to go on.
    raw = json.dumps({"BallData": {"Speed": 100.0, "HLA": 0.0, "VLA": 12.0}}).encode()

    message = parse_launch_monitor_message(raw)

    assert message.declares_ball_data


def test_a_flag_without_data_declares_nothing() -> None:
    raw = json.dumps({"ShotDataOptions": {"ContainsBallData": True}}).encode()

    message = parse_launch_monitor_message(raw)

    assert not message.declares_ball_data


def test_unknown_fields_are_ignored_so_a_future_gspro_cannot_break_ingestion() -> None:
    raw = json.dumps(
        {
            "DeviceID": "LM",
            "ShotNumber": 1,
            "SomethingAddedIn2027": {"nested": True},
            "BallData": {"Speed": 1.0, "HLA": 0.0, "VLA": 1.0, "NewField": 5},
        }
    ).encode()

    message = parse_launch_monitor_message(raw)

    assert message.shot_number == 1
    assert message.ball_data is not None


def test_a_message_missing_shot_data_options_still_parses() -> None:
    raw = json.dumps(
        {
            "DeviceID": "LM",
            "ShotNumber": 3,
            "BallData": {"Speed": 5.0, "HLA": 0.0, "VLA": 2.0},
        }
    ).encode()

    message = parse_launch_monitor_message(raw)

    assert message.shot_data_options is None
    assert not message.is_heartbeat


def test_ball_data_without_speed_is_rejected() -> None:
    # Speed and the two launch angles are what make a shot a shot.
    raw = json.dumps({"BallData": {"HLA": 0.0, "VLA": 12.0}}).encode()

    with pytest.raises(GSProProtocolError) as caught:
        parse_launch_monitor_message(raw)

    assert caught.value.reason is RejectionReason.SCHEMA_INVALID


def test_spin_may_be_reported_as_back_and_side_instead_of_total() -> None:
    # The spec allows either form.
    raw = json.dumps(
        {
            "BallData": {
                "Speed": 100.0,
                "HLA": 1.0,
                "VLA": 12.0,
                "BackSpin": 2500.0,
                "SideSpin": -800.0,
            }
        }
    ).encode()

    message = parse_launch_monitor_message(raw)

    assert message.ball_data is not None
    assert message.ball_data.total_spin is None
    assert message.ball_data.back_spin == 2500.0


@pytest.mark.parametrize("payload", MALFORMED_PAYLOADS)
def test_malformed_payloads_are_rejected_with_a_reason(payload: bytes) -> None:
    with pytest.raises(GSProProtocolError) as caught:
        parse_launch_monitor_message(payload)

    assert caught.value.reason in {
        RejectionReason.UNPARSEABLE,
        RejectionReason.SCHEMA_INVALID,
    }


def test_a_json_array_is_rejected_rather_than_crashing() -> None:
    with pytest.raises(GSProProtocolError) as caught:
        parse_launch_monitor_message(b'[{"Speed": 1}]')

    assert caught.value.reason is RejectionReason.SCHEMA_INVALID


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_are_rejected(literal: str) -> None:
    # Python's json accepts these; JSON does not, and they would poison every
    # average computed from them later.
    raw = f'{{"BallData": {{"Speed": {literal}, "HLA": 0.0, "VLA": 1.0}}}}'.encode()

    with pytest.raises(GSProProtocolError):
        parse_launch_monitor_message(raw)


def test_invalid_utf8_is_a_rejection_not_an_exception_type_surprise() -> None:
    with pytest.raises(GSProProtocolError):
        parse_launch_monitor_message(b'{"DeviceID": "\xff\xfe unterminated')


def test_a_player_information_response_parses() -> None:
    response = parse_simulator_response(encode(player_information("LH", "PT")))

    assert response.code == 201
    assert response.player is not None
    assert response.player.handed == "LH"
    assert response.player.club == "PT"


def test_a_shot_acknowledgement_parses() -> None:
    response = parse_simulator_response(encode(shot_acknowledgement()))

    assert response.code == 200
    assert response.player is None


def test_a_response_whose_code_is_not_a_number_is_rejected() -> None:
    with pytest.raises(GSProProtocolError) as caught:
        parse_simulator_response(b'{"Code": "not-a-number"}')

    assert caught.value.reason is RejectionReason.SCHEMA_INVALID


def test_a_device_message_carrying_unrelated_fields_is_simply_empty() -> None:
    # Valid JSON that says nothing is not an error; it declares no shot data and
    # the adapter ignores it.
    message = parse_launch_monitor_message(b'{"Code": "not-a-number"}')

    assert not message.declares_ball_data
    assert not message.declares_club_data


def test_a_response_without_a_code_is_rejected() -> None:
    with pytest.raises(GSProProtocolError) as caught:
        parse_simulator_response(b'{"Message": "hello"}')

    assert caught.value.reason is RejectionReason.SCHEMA_INVALID


def test_rejection_details_stay_short_enough_to_log() -> None:
    raw = json.dumps({"BallData": {}}).encode()

    with pytest.raises(GSProProtocolError) as caught:
        parse_launch_monitor_message(raw)

    assert len(caught.value.detail) < 300
