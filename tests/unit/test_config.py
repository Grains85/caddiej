from pathlib import Path

import pytest
from pydantic import ValidationError

from caddiej.config import Settings


def test_settings_load_obs_environment(monkeypatch) -> None:
    monkeypatch.setenv("OBS_HOST", "simulator.local")
    monkeypatch.setenv("OBS_PORT", "4456")
    monkeypatch.setenv("OBS_PASSWORD", "secret")
    monkeypatch.setenv("OBS_CONNECT_TIMEOUT", "2.5")
    monkeypatch.setenv("OBS_TEST_SCENE", "Test Scene")
    monkeypatch.setenv("OBS_TEST_STATUS_SOURCE", "Status Source")

    settings = Settings(_env_file=None)

    assert settings.obs_host == "simulator.local"
    assert settings.obs_port == 4456
    assert settings.obs_password.get_secret_value() == "secret"
    assert settings.obs_connect_timeout == 2.5
    assert settings.obs_test_scene == "Test Scene"
    assert settings.obs_test_status_source == "Status Source"


def test_password_is_not_exposed_in_settings_repr() -> None:
    settings = Settings(OBS_PASSWORD="do-not-log", _env_file=None)

    assert "do-not-log" not in repr(settings)


def test_history_is_recorded_unless_it_is_switched_off() -> None:
    # A session that was not recorded cannot be recovered afterwards, so the
    # default has to be on.
    settings = Settings(_env_file=None)

    assert settings.history_enabled is True
    assert settings.history_database_path == Path("caddiej.sqlite3")
    assert settings.history_record_raw_payloads is False


def test_history_settings_load_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("HISTORY_ENABLED", "false")
    monkeypatch.setenv("HISTORY_DATABASE_PATH", "rounds/history.sqlite3")
    monkeypatch.setenv("HISTORY_QUEUE_SIZE", "16")
    monkeypatch.setenv("HISTORY_RECORD_RAW_PAYLOADS", "true")
    monkeypatch.setenv("HISTORY_RAW_PAYLOAD_MAX_BYTES", "2048")
    monkeypatch.setenv("PLAYER_NAME", "Johan")
    monkeypatch.setenv("SESSION_LABEL", "Wednesday range")

    settings = Settings(_env_file=None)

    assert settings.history_enabled is False
    assert settings.history_database_path == Path("rounds/history.sqlite3")
    assert settings.history_queue_size == 16
    assert settings.history_record_raw_payloads is True
    assert settings.history_raw_payload_max_bytes == 2048
    assert settings.player_name == "Johan"
    assert settings.session_label == "Wednesday range"


def test_filming_is_off_until_somebody_asks_for_it() -> None:
    # Unlike history, this is off by default: it writes gigabytes to the disk and
    # it films whoever is standing on the mat. Both should be a decision somebody
    # made rather than one they inherited.
    settings = Settings(_env_file=None)

    assert settings.video_enabled is False
    assert settings.video_media_root == Path("media")
    # And when it is switched on, it needs no camera to be useful: FFmpeg
    # generates a test pattern so the whole path can be watched working.
    assert settings.video_camera_kind == "synthetic"


def test_the_clip_window_is_wide_enough_for_a_backswing() -> None:
    # The pre-impact window has to absorb the backswing plus two unknowns: how
    # late the launch monitor reported the shot, and how late the camera
    # timestamped the frame.
    settings = Settings(_env_file=None)

    assert settings.video_pre_impact_seconds >= 1.5
    assert settings.video_post_impact_seconds >= 1.0
    # And the buffer has to hold far more than one clip, because a clip is cut
    # after the shot rather than during it.
    assert (
        settings.video_buffer_seconds
        > (settings.video_pre_impact_seconds + settings.video_post_impact_seconds) * 4
    )


def test_video_settings_load_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("VIDEO_ENABLED", "true")
    monkeypatch.setenv("VIDEO_CAMERA_KIND", "avfoundation")
    monkeypatch.setenv("VIDEO_CAMERA_DEVICE", "1")
    monkeypatch.setenv("VIDEO_CAMERA_ID", "face-on")
    monkeypatch.setenv("VIDEO_CAMERA_FPS", "120")
    monkeypatch.setenv("VIDEO_CAMERA_LATENCY_SECONDS", "0.08")
    monkeypatch.setenv("VIDEO_MEDIA_ROOT", "/Volumes/Swings")
    monkeypatch.setenv("VIDEO_ENCODER", "h264_videotoolbox")
    monkeypatch.setenv("VIDEO_PRE_IMPACT_SECONDS", "3")
    monkeypatch.setenv("VIDEO_POST_IMPACT_SECONDS", "2")
    monkeypatch.setenv("VIDEO_SHOT_REPORT_LAG_SECONDS", "0.4")

    settings = Settings(_env_file=None)

    assert settings.video_enabled is True
    assert settings.video_camera_kind == "avfoundation"
    assert settings.video_camera_device == "1"
    assert settings.video_camera_id == "face-on"
    assert settings.video_camera_fps == 120
    assert settings.video_camera_latency_seconds == 0.08
    assert settings.video_media_root == Path("/Volumes/Swings")
    assert settings.video_encoder == "h264_videotoolbox"
    assert settings.video_pre_impact_seconds == 3
    assert settings.video_post_impact_seconds == 2
    assert settings.video_shot_report_lag_seconds == 0.4


def test_the_tee_defaults_to_today_s_visual_placement() -> None:
    settings = Settings(_env_file=None)

    assert settings.floor_tee_x_m == 0.5
    assert settings.floor_tee_y_m == 0.72
    assert settings.floor_tee_driver_x_m == 0.5
    assert settings.floor_tee_driver_y_m == 0.72
    assert settings.floor_club_path_length == 1.2
    assert settings.floor_shot_result_min_duration == 8.0
    assert settings.floor_shot_result_max_duration == 30.0
    assert settings.floor_hitting_strip_x_m == 0.5
    assert settings.floor_hitting_strip_y_m == 0.72
    assert settings.floor_hitting_strip_width_m == 0.30
    assert settings.floor_hitting_strip_depth_m == 0.45
    # An overlay that is mostly outline is the safer thing to put on an unknown
    # projector, so lighting the turf is something an operator asks for.
    assert settings.floor_hitting_strip_fill is False


def test_a_physical_tee_off_the_floor_is_rejected() -> None:
    with pytest.raises(ValidationError, match="FLOOR_TEE_X_M"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            FLOOR_WIDTH_M=2.0,
            FLOOR_DEPTH_M=2.0,
            FLOOR_TEE_X_M=2.5,
            FLOOR_TEE_Y_M=0.5,
        )


def test_legacy_normalized_tee_converts_to_metres() -> None:
    with pytest.warns(DeprecationWarning, match="FLOOR_TEE_X is deprecated"):
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            FLOOR_WIDTH_M=4.0,
            FLOOR_DEPTH_M=3.0,
            FLOOR_TEE_X=0.5,
            FLOOR_TEE_Y=0.72,
        )

    assert settings.floor_tee_x_m == pytest.approx(2.0)
    assert settings.floor_tee_y_m == pytest.approx(2.16)


def test_physical_tee_wins_when_both_forms_are_set() -> None:
    with pytest.warns(DeprecationWarning, match="ignored"):
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            FLOOR_TEE_X_M=0.4,
            FLOOR_TEE_Y_M=0.6,
            FLOOR_TEE_X=0.9,
            FLOOR_TEE_Y=0.1,
        )

    assert settings.floor_tee_x_m == 0.4
    assert settings.floor_tee_y_m == 0.6


def test_an_unset_driver_tee_inherits_the_default_tee() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        FLOOR_TEE_X_M=0.4,
        FLOOR_TEE_Y_M=0.6,
    )

    assert settings.floor_tee_driver_x_m == 0.4
    assert settings.floor_tee_driver_y_m == 0.6


def test_an_unset_hitting_strip_inherits_the_default_tee() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        FLOOR_TEE_X_M=0.4,
        FLOOR_TEE_Y_M=0.6,
    )

    assert settings.floor_hitting_strip_x_m == 0.4
    assert settings.floor_hitting_strip_y_m == 0.6
    assert settings.floor_hitting_strip_width_m == 0.30
    assert settings.floor_hitting_strip_depth_m == 0.45


def test_a_set_hitting_strip_is_independent_of_the_tee() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        FLOOR_TEE_X_M=0.4,
        FLOOR_TEE_Y_M=0.6,
        FLOOR_HITTING_STRIP_X_M=0.42,
        FLOOR_HITTING_STRIP_Y_M=0.58,
        FLOOR_HITTING_STRIP_WIDTH_M=0.28,
        FLOOR_HITTING_STRIP_DEPTH_M=0.50,
    )

    assert settings.floor_hitting_strip_x_m == 0.42
    assert settings.floor_hitting_strip_y_m == 0.58
    assert settings.floor_hitting_strip_width_m == 0.28
    assert settings.floor_hitting_strip_depth_m == 0.50


def test_one_explicit_strip_coordinate_does_not_drag_the_other_from_the_tee() -> None:
    """Calibrating the pad across the bay must not move it up and down with it."""

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        FLOOR_TEE_X_M=0.4,
        FLOOR_TEE_Y_M=0.6,
        FLOOR_HITTING_STRIP_X_M=0.48,
    )

    assert settings.floor_hitting_strip_x_m == 0.48
    assert settings.floor_hitting_strip_y_m == 0.6


class TestTheBag:
    """Carries per club. Zero is not a short club; it is a club you do not own."""

    def test_nothing_is_configured_by_default(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        assert set(settings.club_carries().values()) == {0.0}

    def test_the_labels_are_the_ones_the_floor_shows(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        assert list(settings.club_carries()) == [
            "DR",
            "3W",
            "5W",
            "4I",
            "5I",
            "6I",
            "7I",
            "8I",
            "9I",
            "PW",
            "GW",
            "SW",
        ]

    def test_a_configured_club_arrives_under_its_label(self) -> None:
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            PLAYER_CLUB_CARRY_7I_M=135.0,
            PLAYER_CLUB_CARRY_DRIVER_M=220.0,
        )

        carries = settings.club_carries()

        assert carries["7I"] == 135.0
        assert carries["DR"] == 220.0
        assert carries["PW"] == 0.0

    def test_a_negative_carry_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="PLAYER_CLUB_CARRY_7I_M"):
            Settings(
                _env_file=None,  # type: ignore[call-arg]
                PLAYER_CLUB_CARRY_7I_M=-1.0,
            )


def test_the_hitting_strip_can_be_asked_to_light_the_turf() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        FLOOR_HITTING_STRIP_FILL=True,
    )

    assert settings.floor_hitting_strip_fill is True


def test_a_result_window_longer_than_its_cap_is_rejected() -> None:
    """The two ends of one lifecycle, so a pair that cannot both hold is refused
    here rather than producing a panel whose behaviour depends on task order."""

    with pytest.raises(ValidationError, match="FLOOR_SHOT_RESULT_MIN_DURATION"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            FLOOR_SHOT_RESULT_MIN_DURATION=40.0,
            FLOOR_SHOT_RESULT_MAX_DURATION=30.0,
        )


def test_a_hitting_strip_centre_off_the_floor_is_rejected() -> None:
    with pytest.raises(ValidationError, match="FLOOR_HITTING_STRIP_Y_M"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            FLOOR_WIDTH_M=2.0,
            FLOOR_DEPTH_M=2.0,
            FLOOR_HITTING_STRIP_X_M=0.5,
            FLOOR_HITTING_STRIP_Y_M=2.5,
        )


def test_a_set_driver_tee_is_independent_of_the_default() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        FLOOR_TEE_X_M=0.4,
        FLOOR_TEE_Y_M=0.6,
        FLOOR_TEE_DRIVER_X_M=0.35,
        FLOOR_TEE_DRIVER_Y_M=0.5,
    )

    assert settings.floor_tee_x_m == 0.4
    assert settings.floor_tee_driver_x_m == 0.35
    assert settings.floor_tee_driver_y_m == 0.5


def test_a_driver_tee_off_the_floor_is_rejected() -> None:
    with pytest.raises(ValidationError, match="FLOOR_TEE_DRIVER_Y_M"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            FLOOR_WIDTH_M=2.0,
            FLOOR_DEPTH_M=2.0,
            FLOOR_TEE_DRIVER_X_M=0.5,
            FLOOR_TEE_DRIVER_Y_M=2.5,
        )


def test_observe_mode_is_a_valid_shot_source() -> None:
    settings = Settings(_env_file=None, GSPRO_MODE="observe")  # type: ignore[call-arg]

    assert settings.gspro_mode == "observe"
    assert settings.gspro_round_observer_enabled is True
    assert settings.protee_observer_enabled is True
    assert settings.gspro_round_poll_interval == 0.1


def test_shot_video_replay_defaults_are_safe() -> None:
    settings = Settings(_env_file=None)

    assert settings.floor_shot_video_enabled is False
    assert settings.obs_shot_video_source == "Shot Video"
    assert settings.floor_shot_video_delay_seconds == 0.75
    assert settings.floor_shot_video_duration_seconds == 4.0
    assert settings.obs_floor_scene == "CaddieJ Test"


def test_shot_video_replay_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("FLOOR_SHOT_VIDEO_ENABLED", "true")
    monkeypatch.setenv("OBS_SHOT_VIDEO_SOURCE", "ProTee Replay")
    monkeypatch.setenv("FLOOR_SHOT_VIDEO_DELAY_SECONDS", "0.5")
    monkeypatch.setenv("FLOOR_SHOT_VIDEO_DURATION_SECONDS", "3")

    settings = Settings(_env_file=None)

    assert settings.floor_shot_video_enabled is True
    assert settings.obs_shot_video_source == "ProTee Replay"
    assert settings.floor_shot_video_delay_seconds == 0.5
    assert settings.floor_shot_video_duration_seconds == 3.0


def test_reveal_sources_are_the_impact_and_delivery_panels() -> None:
    """Shot Video and Top Impact already exist. They are not a body replay."""

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        SWING_CAPTURE_PANEL_IMPACT_SOURCE="Top Impact",
        SWING_CAPTURE_PANEL_DELIVERY_SOURCE="Shot Video",
        SWING_CAPTURE_CAMERA_1_SOURCE="CaddieJ Swing Camera 1",
    )
    panels = {panel.panel_id: panel for panel in settings.capture_panels()}

    assert set(panels) == {"impact", "delivery"}
    assert panels["impact"].source == "Top Impact"
    assert panels["impact"].kind.value == "static"
    assert panels["impact"].supports == frozenset()
    assert panels["delivery"].source == "Shot Video"
    assert panels["delivery"].kind.value == "static"
    assert panels["delivery"].supports == frozenset()
