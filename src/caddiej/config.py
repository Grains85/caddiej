"""Application configuration loaded from environment variables."""

import os
import warnings
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AliasChoices, BeforeValidator, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from caddiej.domain.coaching import SEVERITY_FINE
from caddiej.domain.cues import CapturePanel, PanelKind, PanelView, VisualClaim


def _expand(value: object) -> object:
    """Treat a leading `~` as a home directory, as a shell would.

    A shell expands `~` in `HISTORY_DATABASE_PATH=~/golf/rounds.sqlite3`, so the
    same line copied into `.env` — where nothing expands anything — would
    otherwise create a directory literally named `~` beside the working
    directory and quietly write the round into it.
    """

    if isinstance(value, str):
        return Path(value).expanduser()
    if isinstance(value, Path):
        return value.expanduser()
    return value


LocalPath = Annotated[Path, BeforeValidator(_expand)]


def _optional_path(value: object) -> Path | None:
    """Empty means 'derive the default', not a path named empty string."""

    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    expanded = _expand(value)
    if isinstance(expanded, Path):
        return expanded
    return None


OptionalLocalPath = Annotated[Path | None, BeforeValidator(_optional_path)]


def default_gspro_round_file() -> Path | None:
    """GSPro's currentRound.dat, derived from %USERPROFILE% on Windows."""

    profile = os.environ.get("USERPROFILE")
    if not profile:
        return None
    return (
        Path(profile) / "AppData" / "LocalLow" / "GSPro" / "GSPro" / "currentRound.dat"
    )


def default_protee_root() -> Path | None:
    """ProTee United's AppData root, derived from %APPDATA% on Windows."""

    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "ProTeeUnited"
    profile = os.environ.get("USERPROFILE")
    if not profile:
        return None
    return Path(profile) / "AppData" / "Roaming" / "ProTeeUnited"


class Settings(BaseSettings):
    """Runtime settings for CaddieJ."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    obs_host: str = Field(default="localhost", validation_alias="OBS_HOST")
    obs_port: int = Field(default=4455, ge=1, le=65535, validation_alias="OBS_PORT")
    obs_password: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="OBS_PASSWORD",
    )
    obs_connect_timeout: float = Field(
        default=10.0,
        gt=0,
        validation_alias="OBS_CONNECT_TIMEOUT",
    )
    obs_test_scene: str = Field(
        default="CaddieJ Test",
        min_length=1,
        validation_alias="OBS_TEST_SCENE",
    )
    obs_test_status_source: str = Field(
        default="CaddieJ Status",
        min_length=1,
        validation_alias="OBS_TEST_STATUS_SOURCE",
    )

    # HUD server. Loopback by default: the projector browser and OBS are local.
    hud_host: str = Field(default="127.0.0.1", validation_alias="HUD_HOST")
    hud_port: int = Field(default=8730, ge=0, le=65535, validation_alias="HUD_PORT")
    hud_startup_timeout: float = Field(
        default=10.0,
        gt=0,
        validation_alias="HUD_STARTUP_TIMEOUT",
    )
    shutdown_timeout: float = Field(
        default=5.0,
        gt=0,
        validation_alias="SHUTDOWN_TIMEOUT",
    )
    # The budget for taking every subsystem apart, as a whole rather than per
    # step. Each step already has its own timeout, and the sum of them is longer
    # than the grace an operating system gives a process before SIGKILL — so
    # without a cap on the total, a machine shutting down kills CaddieJ part way
    # through and the last steps never run. Sized to fit inside the shortest
    # grace period CaddieJ is likely to meet, which is systemd's default 90s
    # halved twice for safety.
    shutdown_grace: float = Field(
        default=20.0,
        gt=0,
        validation_alias="SHUTDOWN_GRACE",
    )
    floor_status_text: str = Field(
        default="CADDIEJ FLOOR ONLINE",
        validation_alias="FLOOR_STATUS_TEXT",
    )
    # Development aid: walks the floor through every state it can render, so the
    # projection can be reviewed without a launch monitor.
    floor_demo: bool = Field(default=False, validation_alias="FLOOR_DEMO")
    floor_demo_step_seconds: float = Field(
        default=2.5,
        gt=0,
        validation_alias="FLOOR_DEMO_STEP_SECONDS",
    )
    # Physical hitting bay, in metres. Path angles are computed on this
    # rectangle and only then mapped onto the unit square the overlay draws.
    # The default is 1 m × 1 m so a fresh checkout is numerically the identity
    # — marker placement and path endpoints look the way they always have.
    # Set these to the real bay (4 × 3 is a typical projected rectangle) when
    # you want a 10° club path to be 10° on the floor. Neither value is a
    # pixel count; projector resolution never enters this.
    floor_width_m: float = Field(
        default=1.0,
        gt=0.0,
        validation_alias="FLOOR_WIDTH_M",
    )
    floor_depth_m: float = Field(
        default=1.0,
        gt=0.0,
        validation_alias="FLOOR_DEPTH_M",
    )
    # Where the ball goes, in metres on the physical floor. Origin is the
    # far-left corner of the back of the bay; y is towards the player. The
    # default 0.5 m × 0.72 m on the default 1 m × 1 m bay is the same visual
    # placement CaddieJ has always used. A later wizard measures the mat in
    # this same space.
    floor_tee_x_m: float = Field(
        default=0.5,
        ge=0.0,
        validation_alias="FLOOR_TEE_X_M",
    )
    floor_tee_y_m: float = Field(
        default=0.72,
        ge=0.0,
        validation_alias="FLOOR_TEE_Y_M",
    )
    # DRIVER is played further forward; other clubs share the default tee
    # above. Unset driver coordinates inherit the resolved default tee so a
    # bay that only sets FLOOR_TEE_X_M / FLOOR_TEE_Y_M does not jump. Same
    # metre space, same calibration; not CSS pixels.
    floor_tee_driver_x_m: float = Field(
        default=0.5,
        ge=0.0,
        validation_alias="FLOOR_TEE_DRIVER_X_M",
    )
    floor_tee_driver_y_m: float = Field(
        default=0.72,
        ge=0.0,
        validation_alias="FLOOR_TEE_DRIVER_Y_M",
    )
    # Hitting strip: a physical rectangle on the same metre floor as the
    # tees, mapped through FloorCalibration. X and Y are the physical centre of
    # the rectangle in metres, not a corner and not a fraction of the bay.
    #
    # Set either of them and it is used exactly as given; the tee is inherited
    # only for the one that was left out. That is what makes the strip
    # calibratable independently of where the ball is placed on it. Defaults are
    # a generic turf pad, not a measured room.
    floor_hitting_strip_x_m: float = Field(
        default=0.5,
        ge=0.0,
        validation_alias="FLOOR_HITTING_STRIP_X_M",
    )
    floor_hitting_strip_y_m: float = Field(
        default=0.72,
        ge=0.0,
        validation_alias="FLOOR_HITTING_STRIP_Y_M",
    )
    floor_hitting_strip_width_m: float = Field(
        default=0.30,
        gt=0.0,
        validation_alias="FLOOR_HITTING_STRIP_WIDTH_M",
    )
    floor_hitting_strip_depth_m: float = Field(
        default=0.45,
        gt=0.0,
        validation_alias="FLOOR_HITTING_STRIP_DEPTH_M",
    )
    # Fill the calibrated strip rather than outlining it, so the projector lights
    # the physical turf a player is standing on. Off by default: an overlay that
    # is mostly outline is the safer thing to put on an unknown projector.
    floor_hitting_strip_fill: bool = Field(
        default=False,
        validation_alias="FLOOR_HITTING_STRIP_FILL",
    )
    # The Compact chamfer, multiplied. 1 is the drawing in millimetres. A
    # projector makes that drawing too small to read from where the player
    # stands, and this is the one factor applied to every size of it. It does
    # not move the strip and it does not change the bay.
    ball_status_chamfer_scale: float = Field(
        default=6.0,
        gt=0.0,
        validation_alias="BALL_STATUS_CHAMFER_SCALE",
    )
    # Everything golf-related on the floor is placed *from* the strip centre
    # above, as an offset in metres on the same physical plane. +X is across the
    # bay towards the player's right and +Y is towards the player, so down-range
    # is negative. Recalibrating a bay is then FLOOR_HITTING_STRIP_X_M and
    # FLOOR_HITTING_STRIP_Y_M; these say how the content sits on the mat, which
    # does not change when the mat moves. Negative values are expected.
    #
    # Three ball positions, because a driver and a putter are not played from
    # where a 7 iron is. Zero is the centre of the strip.
    floor_ball_iron_dx_m: float = Field(
        default=0.0,
        validation_alias="FLOOR_BALL_IRON_DX_M",
    )
    floor_ball_iron_dy_m: float = Field(
        default=0.0,
        validation_alias="FLOOR_BALL_IRON_DY_M",
    )
    # Unset driver offsets inherit the iron offsets, so a bay that has not
    # thought about a driver yet does not put the ball somewhere surprising.
    floor_ball_driver_dx_m: float = Field(
        default=0.0,
        validation_alias="FLOOR_BALL_DRIVER_DX_M",
    )
    floor_ball_driver_dy_m: float = Field(
        default=0.0,
        validation_alias="FLOOR_BALL_DRIVER_DY_M",
    )
    # The putter is allowed off the mat, and by default is: −0.30 m is 30 cm
    # down-range of the strip centre, which on a typical 45 cm strip is past its
    # front edge. That is the point of having a third position.
    floor_ball_putter_dx_m: float = Field(
        default=0.0,
        validation_alias="FLOOR_BALL_PUTTER_DX_M",
    )
    floor_ball_putter_dy_m: float = Field(
        default=-0.30,
        validation_alias="FLOOR_BALL_PUTTER_DY_M",
    )
    # Where the numbers a player reads while standing at the strip are drawn:
    # target distance, club recommendation, shot distance, remaining distance,
    # and the coaching line under them. The default puts them where they have
    # always been on the default 1 m × 1 m bay. The startup banner, the
    # celebration layer and the link light are deliberately not anchored: they
    # are drawn across the whole projection and have nothing to do with a mat.
    floor_information_dx_m: float = Field(
        default=0.0,
        validation_alias="FLOOR_INFORMATION_DX_M",
    )
    floor_information_dy_m: float = Field(
        default=-0.66,
        validation_alias="FLOOR_INFORMATION_DY_M",
    )
    # Putting mode: a plain lane running down-range from the putter ball while
    # a putter is in play, and nothing else. On by default because it is the
    # only thing that tells a player the floor has noticed the club changed.
    floor_putting_lane: bool = Field(
        default=True,
        validation_alias="FLOOR_PUTTING_LANE",
    )
    floor_putting_lane_length_m: float = Field(
        default=0.30,
        gt=0.0,
        validation_alias="FLOOR_PUTTING_LANE_LENGTH_M",
    )
    floor_putting_lane_width_m: float = Field(
        default=0.24,
        gt=0.0,
        validation_alias="FLOOR_PUTTING_LANE_WIDTH_M",
    )
    # Deprecated aliases: a 0–1 fraction of the bay, converted to metres
    # only when the corresponding _M variable is unset. Not a second source
    # of truth after validation.
    floor_tee_x: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        validation_alias="FLOOR_TEE_X",
    )
    floor_tee_y: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        validation_alias="FLOOR_TEE_Y",
    )
    # How far the club-path line is drawn, in metres of the physical floor.
    # About a pace: long enough to read at the player's feet on a real bay,
    # short enough not to run the length of the hitting strip.
    floor_club_path_length: float = Field(
        default=1.2,
        gt=0.0,
        validation_alias="FLOOR_CLUB_PATH_LENGTH",
    )
    # How long a shot stays on the floor before it makes way for the next ball.
    floor_shot_display_seconds: float = Field(
        default=6.0,
        gt=0,
        validation_alias="FLOOR_SHOT_DISPLAY_SECONDS",
    )
    # Observer round-result panel. It cannot appear until GSPro has finished
    # flying the ball, so its lifetime is measured from later than the strike and
    # both ends are configurable: readable for at least the minimum however
    # quickly the next ball is teed up, and off by the maximum regardless.
    floor_shot_result_min_duration: float = Field(
        default=8.0,
        gt=0,
        validation_alias="FLOOR_SHOT_RESULT_MIN_DURATION",
    )
    floor_shot_result_max_duration: float = Field(
        default=30.0,
        gt=0,
        validation_alias="FLOOR_SHOT_RESULT_MAX_DURATION",
    )

    # The player's bag, in metres of carry. Used for one thing: choosing the
    # club nearest the distance GSPro says is left to the target, which the
    # floor shows while the next shot is being addressed.
    #
    # Zero means the club is not in the bag, and a bag with nothing in it
    # produces no recommendation at all rather than a wrong one. That is why
    # every default is zero: CaddieJ has not measured anybody, and a set of
    # plausible-looking numbers would be a guess wearing the clothes of a
    # measurement. Metres to match GSPro's DistanceToTarget, which is the only
    # distance this is ever compared against.
    #
    # These are intended to be temporary. Carry per club is exactly the sort of
    # thing shot history can learn, and when it does, only the provider changes:
    # nothing in the recommendation or on the floor reads these names.
    player_club_carry_driver_m: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias="PLAYER_CLUB_CARRY_DRIVER_M",
    )
    player_club_carry_3w_m: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias="PLAYER_CLUB_CARRY_3W_M",
    )
    player_club_carry_5w_m: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias="PLAYER_CLUB_CARRY_5W_M",
    )
    player_club_carry_4i_m: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias="PLAYER_CLUB_CARRY_4I_M",
    )
    player_club_carry_5i_m: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias="PLAYER_CLUB_CARRY_5I_M",
    )
    player_club_carry_6i_m: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias="PLAYER_CLUB_CARRY_6I_M",
    )
    player_club_carry_7i_m: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias="PLAYER_CLUB_CARRY_7I_M",
    )
    player_club_carry_8i_m: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias="PLAYER_CLUB_CARRY_8I_M",
    )
    player_club_carry_9i_m: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias="PLAYER_CLUB_CARRY_9I_M",
    )
    player_club_carry_pw_m: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias="PLAYER_CLUB_CARRY_PW_M",
    )
    player_club_carry_gw_m: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias="PLAYER_CLUB_CARRY_GW_M",
    )
    player_club_carry_sw_m: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias="PLAYER_CLUB_CARRY_SW_M",
    )

    # GSPro Open Connect. CaddieJ can sit between the launch monitor connector
    # and GSPro (`interpose`), because Open Connect is a feed into GSPro with no
    # official observer API. That path is experimental: production observation
    # reads ProTee's log and GSPro's currentRound.dat instead, and is not in
    # their traffic path at all.
    # "simulate" runs a scripted round through the real adapter against a fake
    # GSPro inside this process, so the whole path from bytes to floor can be
    # watched without a simulator, a launch monitor, or a golf swing.
    gspro_mode: Literal["off", "interpose", "simulate", "observe"] = Field(
        default="off",
        validation_alias="GSPRO_MODE",
    )
    # What the launch monitor connector should be pointed at.
    gspro_bind_host: str = Field(
        default="127.0.0.1",
        validation_alias="GSPRO_BIND_HOST",
    )
    gspro_bind_port: int = Field(
        default=9210,
        ge=0,
        le=65535,
        validation_alias="GSPRO_BIND_PORT",
    )
    # Where GSPro itself listens. 921 is the documented Open Connect port.
    gspro_upstream_host: str = Field(
        default="127.0.0.1",
        validation_alias="GSPRO_UPSTREAM_HOST",
    )
    gspro_upstream_port: int = Field(
        default=921,
        ge=1,
        le=65535,
        validation_alias="GSPRO_UPSTREAM_PORT",
    )
    gspro_connect_timeout: float = Field(
        default=5.0,
        gt=0,
        validation_alias="GSPRO_CONNECT_TIMEOUT",
    )
    # How long a bridged connection may say nothing before CaddieJ stops
    # believing it is there. A launch monitor that vanished without closing its
    # socket — a laptop lid, a dropped wireless link — leaves a connection that
    # reads as open forever, and CaddieJ serves one device at a time, so that
    # one dead connection refuses every later one until the process restarts.
    # Far longer than any heartbeat interval, because the cost of being wrong is
    # a reconnect and the cost of not checking is the rest of the evening.
    # Zero switches the check off.
    gspro_idle_timeout: float = Field(
        default=180.0,
        ge=0,
        validation_alias="GSPRO_IDLE_TIMEOUT",
    )
    gspro_backoff_initial: float = Field(
        default=0.5,
        gt=0,
        validation_alias="GSPRO_BACKOFF_INITIAL",
    )
    # Deliberately short. This delay is how long CaddieJ refuses a launch
    # monitor after failing to reach GSPro, so it is also how long someone who
    # starts GSPro last waits before shots flow. Long enough to stop a retrying
    # device hammering a dead port, short enough not to look broken.
    gspro_backoff_max: float = Field(
        default=5.0,
        gt=0,
        validation_alias="GSPRO_BACKOFF_MAX",
    )
    gspro_max_message_bytes: int = Field(
        default=65536,
        gt=0,
        validation_alias="GSPRO_MAX_MESSAGE_BYTES",
    )
    # How long a shot missing half its data waits for the rest. Bounded, and it
    # never delays forwarding — only CaddieJ's own reaction.
    gspro_shot_assembly_window: float = Field(
        default=0.25,
        ge=0,
        validation_alias="GSPRO_SHOT_ASSEMBLY_WINDOW",
    )
    # Ball detection is a level flag on every heartbeat, so it is debounced
    # asymmetrically: a ball appearing is believed at once so the floor stops
    # projecting onto it, while a ball disappearing is given time so that
    # detection blinking off for one heartbeat does not flash the marker back on
    # under the player's club. A single delay could do neither, being shorter
    # than the interval between heartbeats.
    gspro_ball_present_debounce: float = Field(
        default=0.0,
        ge=0,
        validation_alias="GSPRO_BALL_PRESENT_DEBOUNCE",
    )
    gspro_ball_absent_debounce: float = Field(
        default=1.5,
        ge=0,
        validation_alias="GSPRO_BALL_ABSENT_DEBOUNCE",
    )
    # How long the simulated round pauses between steps. Slow enough to watch.
    gspro_simulation_step_seconds: float = Field(
        default=3.0,
        gt=0,
        validation_alias="GSPRO_SIMULATION_STEP_SECONDS",
    )
    # Passive observers. Used when GSPRO_MODE=observe. They never bind 9210,
    # never connect to GSPconnect, and never write ProTee or GSPro files.
    gspro_round_observer_enabled: bool = Field(
        default=True,
        validation_alias="GSPRO_ROUND_OBSERVER_ENABLED",
    )
    gspro_round_file_path: OptionalLocalPath = Field(
        default=None,
        validation_alias="GSPRO_ROUND_FILE_PATH",
    )
    gspro_round_poll_interval: float = Field(
        default=0.1,
        gt=0,
        validation_alias="GSPRO_ROUND_POLL_INTERVAL",
    )
    protee_observer_enabled: bool = Field(
        default=True,
        validation_alias="PROTEE_OBSERVER_ENABLED",
    )
    protee_root_path: OptionalLocalPath = Field(
        default=None,
        validation_alias="PROTEE_ROOT_PATH",
    )
    protee_log_poll_interval: float = Field(
        default=0.1,
        gt=0,
        validation_alias="PROTEE_LOG_POLL_INTERVAL",
    )

    # Local shot history in one SQLite file. On by default: a session that was
    # not recorded cannot be recovered afterwards, and the file is the user's to
    # copy, inspect, or delete.
    history_enabled: bool = Field(default=True, validation_alias="HISTORY_ENABLED")
    history_database_path: LocalPath = Field(
        default=Path("caddiej.sqlite3"),
        validation_alias="HISTORY_DATABASE_PATH",
    )
    # A label for the shots, since no launch monitor and no simulator reports who
    # is playing: Open Connect carries handedness and nothing else about a
    # player. Blank means the shots are stored without a name.
    player_name: str = Field(default="", validation_alias="PLAYER_NAME")
    session_label: str = Field(default="", validation_alias="SESSION_LABEL")
    # How many shots may be waiting to be written before further ones are
    # refused. Reaching this needs a wedged disk: a shot takes microseconds to
    # store and a golfer needs at least ten seconds to hit the next one.
    history_queue_size: int = Field(
        default=256,
        gt=0,
        validation_alias="HISTORY_QUEUE_SIZE",
    )
    # How long SQLite waits for a file another process has locked.
    history_busy_timeout: float = Field(
        default=5.0,
        gt=0,
        validation_alias="HISTORY_BUSY_TIMEOUT",
    )
    # How long shutdown waits for queued shots to reach the disk.
    history_drain_timeout: float = Field(
        default=5.0,
        gt=0,
        validation_alias="HISTORY_DRAIN_TIMEOUT",
    )
    # Keep the exact bytes a device sent for each shot. For diagnosing a device
    # that reports something unexpected; off by default because it grows the
    # file for data nothing reads.
    history_record_raw_payloads: bool = Field(
        default=False,
        validation_alias="HISTORY_RECORD_RAW_PAYLOADS",
    )
    history_raw_payload_max_bytes: int = Field(
        default=8192,
        gt=0,
        validation_alias="HISTORY_RAW_PAYLOAD_MAX_BYTES",
    )

    # Swing video. Off by default, and deliberately so: it writes gigabytes to
    # the disk and it films whoever is standing on the mat. Both of those should
    # be a decision somebody made, not a default they inherited.
    video_enabled: bool = Field(default=False, validation_alias="VIDEO_ENABLED")
    # "synthetic" needs no camera at all: FFmpeg generates a test pattern, so the
    # whole path from a shot to a clip on disk can be run and watched on a
    # machine with nothing plugged in.
    video_camera_kind: Literal["synthetic", "avfoundation", "v4l2", "dshow"] = Field(
        default="synthetic",
        validation_alias="VIDEO_CAMERA_KIND",
    )
    # An AVFoundation index on macOS, a /dev/video path on Linux, a DirectShow
    # name on Windows.
    video_camera_device: str = Field(
        default="0", validation_alias="VIDEO_CAMERA_DEVICE"
    )
    video_camera_id: str = Field(
        default="tee",
        min_length=1,
        validation_alias="VIDEO_CAMERA_ID",
    )
    # Where the camera is standing. Nothing can work this out from the footage,
    # and it decides what a still can honestly be read for, so it is left
    # unknown until somebody says — at which point coaching is told, and keeps
    # to what any angle would show until then.
    video_camera_view: Literal["unknown", "face_on", "down_the_line"] = Field(
        default="unknown",
        validation_alias="VIDEO_CAMERA_VIEW",
    )
    video_camera_fps: float = Field(
        default=30.0,
        gt=0,
        validation_alias="VIDEO_CAMERA_FPS",
    )
    video_camera_width: int = Field(
        default=1280,
        gt=0,
        validation_alias="VIDEO_CAMERA_WIDTH",
    )
    video_camera_height: int = Field(
        default=720,
        gt=0,
        validation_alias="VIDEO_CAMERA_HEIGHT",
    )
    # How long after the light reaches the sensor a frame gets its timestamp.
    # Device-specific, usually tens of milliseconds, and zero means nobody has
    # measured it — which is why a clip is cut wide rather than tight.
    video_camera_latency_seconds: float = Field(
        default=0.0,
        ge=0,
        validation_alias="VIDEO_CAMERA_LATENCY_SECONDS",
    )
    video_media_root: LocalPath = Field(
        default=Path("media"),
        validation_alias="VIDEO_MEDIA_ROOT",
    )
    video_ffmpeg_path: str = Field(
        default="ffmpeg",
        min_length=1,
        validation_alias="VIDEO_FFMPEG_PATH",
    )
    # libx264 works everywhere and is the safe default. On a machine with a
    # hardware encoder, using it keeps the CPU for the golf: h264_videotoolbox
    # on macOS, h264_nvenc on NVIDIA, h264_qsv on Intel.
    video_encoder: str = Field(
        default="libx264",
        min_length=1,
        validation_alias="VIDEO_ENCODER",
    )
    video_bitrate: str = Field(
        default="8M",
        min_length=1,
        validation_alias="VIDEO_BITRATE",
    )
    # How much of the swing to keep either side of impact. The pre-impact window
    # is generous because it has to absorb the backswing plus two unknowns: how
    # late the launch monitor reported the shot, and how late the camera
    # timestamped the frame.
    video_pre_impact_seconds: float = Field(
        default=2.0,
        gt=0,
        validation_alias="VIDEO_PRE_IMPACT_SECONDS",
    )
    video_post_impact_seconds: float = Field(
        default=1.5,
        gt=0,
        validation_alias="VIDEO_POST_IMPACT_SECONDS",
    )
    # How much footage is kept ready to be clipped. Well beyond the clip window,
    # because a clip is cut after the shot and a busy disk must not be able to
    # recycle the swing before the worker reaches it.
    video_buffer_seconds: float = Field(
        default=30.0,
        gt=0,
        validation_alias="VIDEO_BUFFER_SECONDS",
    )
    video_buffer_max_bytes: int = Field(
        default=2 * 1024**3,
        gt=0,
        validation_alias="VIDEO_BUFFER_MAX_BYTES",
    )
    # How long finished clips are kept. The buffer is bounded and the stills are
    # swept, but clips are the one thing video writes that is meant to last, so
    # without this a bay that plays every week fills its disk and the failure
    # surfaces as a clip that would not extract. Whole days, matching how they
    # are filed. Zero keeps them all, for anyone archiving them elsewhere.
    video_clip_retention_days: int = Field(
        default=30,
        ge=0,
        validation_alias="VIDEO_CLIP_RETENTION_DAYS",
    )
    # A clip is a whole number of segments, so this is also how precisely the
    # requested window can be honoured.
    video_segment_seconds: float = Field(
        default=1.0,
        gt=0,
        validation_alias="VIDEO_SEGMENT_SECONDS",
    )
    video_keyframe_seconds: float = Field(
        default=0.5,
        gt=0,
        validation_alias="VIDEO_KEYFRAME_SECONDS",
    )
    # How long before the shot message the ball was actually struck. Device
    # specific; a launch monitor that takes a second to report needs this or
    # every clip is centred a second late.
    video_shot_report_lag_seconds: float = Field(
        default=0.0,
        ge=0,
        validation_alias="VIDEO_SHOT_REPORT_LAG_SECONDS",
    )
    # Reaching this needs a wedged disk: a clip takes under a second to copy and
    # a golfer needs at least ten to hit the next one.
    video_clip_queue_size: int = Field(
        default=8,
        gt=0,
        validation_alias="VIDEO_CLIP_QUEUE_SIZE",
    )
    video_extract_timeout: float = Field(
        default=30.0,
        gt=0,
        validation_alias="VIDEO_EXTRACT_TIMEOUT",
    )
    # Consecutive failures to start filming before the camera is left alone.
    video_restart_limit: int = Field(
        default=5,
        ge=0,
        validation_alias="VIDEO_RESTART_LIMIT",
    )
    video_drain_timeout: float = Field(
        default=5.0,
        gt=0,
        validation_alias="VIDEO_DRAIN_TIMEOUT",
    )

    # Coaching. On by default, and safely so: with no provider configured this is
    # arithmetic over the shots hit so far, offline and instant, and it produces
    # the fault line on the floor. Nothing here reaches a network until
    # COACHING_PROVIDER is set to something.
    coaching_enabled: bool = Field(default=True, validation_alias="COACHING_ENABLED")
    # Diagnostic, off in production. ProTee coaching does everything up to the
    # request -- capture, All Values, frame selection, the evidence sheet --
    # and then makes no call, so a capture-calibration session costs nothing.
    coaching_diagnostic_dry_run: bool = Field(
        default=False, validation_alias="COACHING_DIAGNOSTIC_DRY_RUN"
    )
    # "none" asks no model at all and is the default, so a fresh checkout coaches
    # from templates and costs nothing. "scripted" answers from a fixed script
    # inside the process, which is how the whole path — prompt, validation,
    # floor — gets exercised on a machine with no credentials.
    coaching_provider: Literal["none", "openai", "scripted"] = Field(
        default="none",
        validation_alias="COACHING_PROVIDER",
    )
    coaching_model: str = Field(
        default="gpt-5-mini",
        min_length=1,
        validation_alias="COACHING_MODEL",
    )
    # Never in the repository and never in a log: the formatter's allowlist has no
    # field that could carry it, and `SecretStr` keeps it out of a traceback.
    coaching_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="COACHING_API_KEY",
    )
    # For an OpenAI-compatible endpoint, including a local one. Blank uses the
    # provider's own.
    coaching_api_base: str = Field(default="", validation_alias="COACHING_API_BASE")
    # Whether swing frames are sent to the provider. Off by default, and that is
    # a privacy decision rather than a cost one: these are pictures of somebody
    # in their own home, and sending them somewhere should be a choice a person
    # made. With this off, coaching works from the measurements alone.
    coaching_send_frames: bool = Field(
        default=False,
        validation_alias="COACHING_SEND_FRAMES",
    )
    coaching_frame_width: int = Field(
        default=512,
        gt=0,
        validation_alias="COACHING_FRAME_WIDTH",
    )
    # What the player is working on. Held to unless something else is markedly
    # worse, so a session reads as a lesson rather than as a list of whatever
    # went wrong last.
    coaching_focus: str = Field(default="", validation_alias="COACHING_FOCUS")
    # Below this, nothing is said. Raising it makes CaddieJ quieter and more
    # worth listening to; lowering it makes it a nag.
    coaching_severity_fine: float = Field(
        default=SEVERITY_FINE,
        ge=0.0,
        le=1.0,
        validation_alias="COACHING_SEVERITY_FINE",
    )
    # How many of the player's own recent shots per club define their normal.
    coaching_baseline_window: int = Field(
        default=40,
        ge=1,
        validation_alias="COACHING_BASELINE_WINDOW",
    )
    # Short on purpose. Advice for three swings ago is worse than none, so this
    # is a queue of shots still worth coaching, not a backlog.
    coaching_queue_size: int = Field(
        default=2,
        ge=1,
        validation_alias="COACHING_QUEUE_SIZE",
    )
    # How long to wait for the clip of a swing before coaching from the numbers.
    # Must exceed the post-impact window plus extraction, or frames never arrive.
    coaching_clip_wait: float = Field(
        default=8.0,
        gt=0,
        validation_alias="COACHING_CLIP_WAIT",
    )
    coaching_timeout: float = Field(
        default=8.0,
        gt=0,
        validation_alias="COACHING_TIMEOUT",
    )
    # The whole budget from shot to line, retries included. Past this the player
    # has hit another ball and the answer is worthless however good it is.
    coaching_budget_seconds: float = Field(
        default=20.0,
        gt=0,
        validation_alias="COACHING_BUDGET_SECONDS",
    )
    coaching_retry_limit: int = Field(
        default=1,
        ge=0,
        validation_alias="COACHING_RETRY_LIMIT",
    )
    # A floor on how often a call can be made. Nobody hits two balls this close
    # together, so reaching it means a replay or a bug — and this is what stops
    # either of them spending money quickly.
    coaching_min_interval: float = Field(
        default=8.0,
        ge=0,
        validation_alias="COACHING_MIN_INTERVAL",
    )
    # A hard ceiling for one run of CaddieJ. Roughly a long practice session,
    # after which coaching falls back to templates and says so in health.
    coaching_call_limit: int = Field(
        default=60,
        ge=0,
        validation_alias="COACHING_CALL_LIMIT",
    )
    # Enough for two short strings and a number. The ceiling is what makes the
    # cost of a shot predictable, so it is deliberately tight.
    coaching_max_output_tokens: int = Field(
        default=120,
        gt=0,
        validation_alias="COACHING_MAX_OUTPUT_TOKENS",
    )
    # Raised from 6 after the first hardware session, where six was the
    # binding constraint on whether a swing could be diagnosed at all: the
    # capture wrote a takeaway and a face-on top to disk and this cap dropped
    # both before the request. Sixteen matches SWING_CAPTURE_SELECTED_FRAMES:
    # below it the capture's aimed stills are thinned evenly on the way to the
    # model, which is how aiming gets undone. The ceiling still exists because
    # the cost of a shot has to stay predictable.
    coaching_max_images: int = Field(
        default=16,
        ge=0,
        le=16,
        validation_alias="COACHING_MAX_IMAGES",
    )
    coaching_visual_cues: bool = Field(
        default=True,
        validation_alias="COACHING_VISUAL_CUES",
    )
    # What starts a coaching job. `protee` coaches every ProTee strike from its
    # swing capture and the All Values still, with no GSPro involved; `gspro`
    # waits for a measured GSPro shot as before. `auto` is `protee` whenever
    # swing capture is on, because that is what the ProTee path is built from.
    coaching_trigger: Literal["auto", "protee", "gspro"] = Field(
        default="auto",
        validation_alias="COACHING_TRIGGER",
    )
    # The ProTee reply carries every All Values reading as well as the line, so
    # it needs far more room than the two strings the GSPro path returns. On a
    # reasoning model this ceiling covers the reasoning as well as the JSON. A
    # full reply with every metric read is about 500-750 tokens, and every
    # checkpoint scored adds about 2000 more; the rest is room for low-effort
    # reasoning over seven images. 2000 ran out on the bay.
    coaching_protee_max_output_tokens: int = Field(
        default=10000,
        gt=0,
        le=16000,
        validation_alias="COACHING_PROTEE_MAX_OUTPUT_TOKENS",
    )
    # The ProTee request is one multimodal call carrying the swing stills, the
    # All Values grid and a long structured reply, so it is slower than the GSPro
    # path's two short strings. These replace COACHING_TIMEOUT and
    # COACHING_BUDGET_SECONDS on that path only. The timeout is per attempt,
    # passed to the OpenAI SDK; the budget bounds every attempt and the wait
    # between them, measured from when the capture is queued for coaching.
    coach_openai_timeout: float = Field(
        default=30.0,
        gt=0,
        le=120,
        validation_alias="COACH_OPENAI_TIMEOUT_SECONDS",
    )
    coach_openai_budget: float = Field(
        default=45.0,
        gt=0,
        le=300,
        validation_alias="COACH_OPENAI_BUDGET_SECONDS",
    )
    # Sent on the ProTee request only, and only for reasoning models: one that
    # does not reason refuses the parameter, so set it empty for those. `low`
    # because the default model, gpt-5-mini, reasons at medium when nothing is
    # sent. Copying a grid and choosing from a fixed list does not need that,
    # and its reasoning shares the output ceiling with the reply.
    #
    # Which values a model accepts is the API's business and not ours: gpt-5-mini
    # takes `minimal` and refuses `none`, gpt-5.4-mini the other way round. Empty
    # means send nothing, which leaves the model's own default in charge and is
    # therefore the one setting whose behaviour can change without us.
    coaching_reasoning_effort: Literal[
        "", "none", "minimal", "low", "medium", "high"
    ] = Field(default="low", validation_alias="COACHING_REASONING_EFFORT")
    floor_coach_cue_image: bool = Field(
        default=False,
        validation_alias="FLOOR_COACH_CUE_IMAGE",
    )

    # Local text to speech. Off by default, and deliberately so: it makes noise
    # in somebody's house, and the first run downloads a few hundred megabytes
    # of speech model. Both of those should be a decision somebody made, not a
    # default they inherited. Nothing here reaches a network except that first
    # model download; synthesis itself is entirely local.
    speech_enabled: bool = Field(default=False, validation_alias="SPEECH_ENABLED")
    # "fake" renders silence of a believable length, which exercises the queue,
    # the events and the floor on a machine with no model installed.
    speech_engine: Literal["none", "kokoro", "fake"] = Field(
        default="kokoro",
        validation_alias="SPEECH_ENGINE",
    )
    # "none" renders every line and plays nothing. For a headless machine, and
    # for anyone who wants the speech events without the sound.
    speech_output: Literal["none", "sounddevice"] = Field(
        default="sounddevice",
        validation_alias="SPEECH_OUTPUT",
    )
    # Kokoro encodes the language in the first letter of the voice: b is British
    # English, a is American. The rest of the catalogue is in the adapter.
    speech_voice: str = Field(
        default="bm_george",
        min_length=1,
        validation_alias="SPEECH_VOICE",
    )
    speech_speed: float = Field(
        default=1.0,
        ge=0.5,
        le=2.0,
        validation_alias="SPEECH_SPEED",
    )
    # A PortAudio device index or name. Blank uses the system default, which is
    # what almost everybody wants and the only thing that survives replugging a
    # pair of speakers.
    speech_device: str = Field(default="", validation_alias="SPEECH_DEVICE")
    # One constant multiplier on every utterance, applied at the speaker.
    # Kokoro peaks around a third of full scale, so the headroom is real and
    # unused, and a bay with the Windows mixer already at maximum still gets a
    # voice nobody can hear. 1.0 is the engine's own level and the only default
    # that is right on hardware nobody has measured; the bay's own value lives
    # in .env, chosen from the measured peak of its actual voice.
    speech_gain: float = Field(
        default=1.0,
        gt=0.0,
        le=4.0,
        validation_alias="SPEECH_GAIN",
    )
    # Short, like coaching's. A line about three swings ago is worse than
    # silence, and a newer coaching line replaces an older waiting one anyway,
    # so this only ever holds a backlog of genuinely different things to say.
    speech_queue_size: int = Field(
        default=4,
        ge=1,
        validation_alias="SPEECH_QUEUE_SIZE",
    )
    # A ceiling on one sentence, so a bad line cannot occupy the engine, the
    # speaker and the room for a minute. Coached lines are capped at ninety
    # characters upstream; this is the backstop for everything else.
    speech_max_characters: int = Field(
        default=300,
        ge=1,
        validation_alias="SPEECH_MAX_CHARACTERS",
    )
    # Generous, because it also covers a cold model on a busy machine.
    speech_render_timeout: float = Field(
        default=20.0,
        gt=0,
        validation_alias="SPEECH_RENDER_TIMEOUT",
    )
    # Loading a voice, which on a first run includes downloading it. Everything
    # after speech in startup waits behind this, so it is bounded: a model that
    # will not arrive leaves the room quiet, which is a supported way to run
    # CaddieJ, whereas a startup that never finishes is not.
    speech_open_timeout: float = Field(
        default=90.0,
        gt=0,
        validation_alias="SPEECH_OPEN_TIMEOUT",
    )
    # Rendered lines kept as WAV files. Coaching repeats itself by design, so
    # this is the difference between a second of CPU per shot and none. Under
    # the media root by default, so deleting that directory is still enough to
    # remove everything CaddieJ has written.
    speech_cache_enabled: bool = Field(
        default=True,
        validation_alias="SPEECH_CACHE_ENABLED",
    )
    speech_cache_root: LocalPath = Field(
        default=Path("media/speech"),
        validation_alias="SPEECH_CACHE_ROOT",
    )
    speech_cache_bytes: int = Field(
        default=32 * 1024**2,
        gt=0,
        validation_alias="SPEECH_CACHE_BYTES",
    )
    speech_cache_files: int = Field(
        default=512,
        ge=1,
        validation_alias="SPEECH_CACHE_FILES",
    )

    # Lighting. Off by default, and for a different reason from the rest: this is
    # the only feature that reaches out and changes something in somebody's house
    # that they set the way they wanted it. It gives the room back afterwards, but
    # deciding to let a golf simulator touch the lights at all should be a
    # decision somebody made rather than a default they inherited.
    lighting_enabled: bool = Field(default=False, validation_alias="LIGHTING_ENABLED")
    # "none" runs the whole path — envelope, tick, rate limit, restore — and
    # changes no lamp. For a machine with no bridge, and for seeing what the
    # lights would have done.
    lighting_output: Literal["none", "hue"] = Field(
        default="hue",
        validation_alias="LIGHTING_OUTPUT",
    )
    # The bridge's address on the local network. An IP is steadier than a
    # hostname here: bridges answer to mDNS names that come and go.
    lighting_bridge_host: str = Field(
        default="",
        validation_alias="LIGHTING_BRIDGE_HOST",
    )
    # From the one-time pairing step: press the button on the bridge and POST to
    # it, as described in the README. Never in the repository and never in a log:
    # the formatter's allowlist has no field that could carry it, and `SecretStr`
    # keeps it out of a traceback.
    lighting_application_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="LIGHTING_APPLICATION_KEY",
    )
    # Which lamps to use, by name or by identifier, comma separated. Blank means
    # every light the bridge knows about, which is convenient and probably not
    # what somebody with a whole house of them wants.
    lighting_lights: str = Field(default="", validation_alias="LIGHTING_LIGHTS")
    # How far the lights move while CaddieJ talks, as a fraction of whatever they
    # were already at. The default dips by not quite a fifth in the gaps between
    # words and returns to normal on them, which is about as much as a room can
    # do without anybody looking up.
    lighting_minimum: float = Field(
        default=0.82,
        gt=0.0,
        le=2.0,
        validation_alias="LIGHTING_MINIMUM",
    )
    lighting_maximum: float = Field(
        default=1.0,
        gt=0.0,
        le=2.0,
        validation_alias="LIGHTING_MAXIMUM",
    )
    # The bridge's whole command budget, shared across every lamp in use. Ten a
    # second is Philips' own guidance, so this is a limit to respect rather than
    # a preference: the tick is derived from it and the number of lights, and
    # raising it makes a bridge start refusing commands rather than reacting
    # faster.
    lighting_max_commands_per_second: float = Field(
        default=10.0,
        gt=0,
        le=50.0,
        validation_alias="LIGHTING_MAX_COMMANDS_PER_SECOND",
    )
    # How long each command is given to complete, as a multiple of the gap to the
    # next one. At one, the bridge is always interpolating towards the next value
    # and the result is continuous; below one it arrives early and waits, which
    # looks like a staircase.
    lighting_transition_scale: float = Field(
        default=1.0,
        gt=0.0,
        le=4.0,
        validation_alias="LIGHTING_TRANSITION_SCALE",
    )
    # Consecutive failures before the bridge is left alone for a while. A bridge
    # that has been unplugged fails everything, and retrying it once per syllable
    # for an evening is worse than waiting a minute and trying again.
    lighting_failure_limit: int = Field(
        default=3,
        ge=1,
        validation_alias="LIGHTING_FAILURE_LIMIT",
    )
    lighting_cooldown_seconds: float = Field(
        default=60.0,
        gt=0,
        validation_alias="LIGHTING_COOLDOWN_SECONDS",
    )
    # Short. This is a small appliance on the same network, and a command that
    # has not landed within a few seconds is a command whose moment has passed.
    lighting_command_timeout: float = Field(
        default=3.0,
        gt=0,
        validation_alias="LIGHTING_COMMAND_TIMEOUT",
    )

    # Effects. On by default, and safely so: nothing publishes a scoring event
    # yet, so this is inert until something does. It is on rather than off
    # because the floor is CaddieJ's own projector and marking a birdie on it is
    # the product working, not a liberty being taken — unlike the lights, which
    # belong to the house, and OBS, which belongs to a scene somebody built.
    effects_enabled: bool = Field(default=True, validation_alias="EFFECTS_ENABLED")
    # One switch per output, so a celebration can be seen and not heard, or heard
    # and not seen, without turning the subsystem off. Each is independent of
    # whether the underlying subsystem is running at all: switching the lights
    # channel on with no bridge configured changes nothing.
    effects_floor: bool = Field(default=True, validation_alias="EFFECTS_FLOOR")
    effects_speech: bool = Field(default=True, validation_alias="EFFECTS_SPEECH")
    effects_lights: bool = Field(default=True, validation_alias="EFFECTS_LIGHTS")
    # Off by default, and it has to be: this shows a source in a scene somebody
    # built, and CaddieJ does not create either. With nothing there to show, the
    # honest default is not to try.
    effects_obs: bool = Field(default=False, validation_alias="EFFECTS_OBS")
    # The scene and the source to reveal for the length of an effect. Whatever is
    # inside the source is entirely the user's: a stinger, a graphic, an
    # animation, a sound. CaddieJ turns it on and turns it off again.
    effects_obs_scene: str = Field(default="", validation_alias="EFFECTS_OBS_SCENE")
    effects_obs_source: str = Field(default="", validation_alias="EFFECTS_OBS_SOURCE")
    # An optional text source captioned with the effect's label, so a scene can
    # say BIRDIE without a graphic per outcome. Blanked when the effect ends.
    effects_obs_text_source: str = Field(
        default="",
        validation_alias="EFFECTS_OBS_TEXT_SOURCE",
    )
    # The hard cap on how long any effect may hold the room, whatever the
    # catalogue says. A backstop against a typo in a duration rather than a
    # tuning knob: the longest thing in the catalogue is five seconds.
    effects_max_seconds: float = Field(
        default=8.0,
        gt=0,
        le=30.0,
        validation_alias="EFFECTS_MAX_SECONDS",
    )
    # How long any single output gets to accept a command. Short, because this is
    # the timeout that stops an OBS which connects and then stops answering from
    # holding a celebration open.
    effects_channel_timeout: float = Field(
        default=3.0,
        gt=0,
        validation_alias="EFFECTS_CHANNEL_TIMEOUT",
    )

    # Assistant API. Off by default, and this default is not conservatism for
    # its own sake: switching it on opens a port that answers questions about
    # what somebody did in their own house, and that is a decision an operator
    # should make rather than inherit. CaddieJ is complete without it.
    assistant_api_enabled: bool = Field(
        default=False,
        validation_alias="ASSISTANT_API_ENABLED",
    )
    # Loopback, and enforced rather than merely defaulted: unlike HUD_HOST, a
    # non-loopback value here is refused at start-up. The HUD serves static
    # files and a read-only feed; this has a bearer token and a mutation route,
    # so a wildcard bind is a mistake worth refusing rather than warning about.
    assistant_api_host: str = Field(
        default="127.0.0.1",
        validation_alias="ASSISTANT_API_HOST",
    )
    assistant_api_port: int = Field(
        default=8731,
        ge=0,
        le=65535,
        validation_alias="ASSISTANT_API_PORT",
    )
    # No default, and no generated fallback. A token invented at start-up would
    # have to be printed or written somewhere for the MCP server to find, and
    # every mechanism for doing that is worse than asking for one: the operator
    # sets the same value here and in the assistant's environment.
    assistant_api_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="ASSISTANT_API_TOKEN",
    )
    # Whether an assistant may change anything at all. Separate from the switch
    # above so that "let it read" and "let it write" are two decisions, and off
    # by default so the first thing anybody runs is a read-only surface.
    assistant_api_allow_mutations: bool = Field(
        default=False,
        validation_alias="ASSISTANT_API_ALLOW_MUTATIONS",
    )
    # Whether coaching prose crosses the boundary. Off by default for the same
    # reason camera frames are: the named fault and its severity are facts about
    # a golf swing, while the sentences are a record of somebody being told what
    # they are doing wrong in their own home, and a hosted assistant reading
    # those is a different decision from one reading a launch angle.
    assistant_api_include_coaching_prose: bool = Field(
        default=False,
        validation_alias="ASSISTANT_API_INCLUDE_COACHING_PROSE",
    )
    assistant_api_startup_timeout: float = Field(
        default=10.0,
        gt=0,
        validation_alias="ASSISTANT_API_STARTUP_TIMEOUT",
    )

    # OBS Browser Source that renders the floor overlay.
    obs_floor_scene: str = Field(
        default="CaddieJ Test",
        min_length=1,
        validation_alias="OBS_FLOOR_SCENE",
    )
    obs_floor_source: str = Field(
        default="CaddieJ Floor",
        min_length=1,
        validation_alias="OBS_FLOOR_SOURCE",
    )
    obs_floor_width: int = Field(
        default=1920,
        gt=0,
        validation_alias="OBS_FLOOR_WIDTH",
    )
    obs_floor_height: int = Field(
        default=1080,
        gt=0,
        validation_alias="OBS_FLOOR_HEIGHT",
    )
    # Off by default: provisioning mutates the user's OBS scene collection.
    obs_provision_floor_source: bool = Field(
        default=False,
        validation_alias="OBS_PROVISION_FLOOR_SOURCE",
    )
    # Temporarily reveal an existing OBS Window Capture of ProTee's Shot Video
    # panel after each newly observed round shot. Off by default: the source
    # must already exist in OBS_FLOOR_SCENE, stacked above the floor overlay.
    # CaddieJ only toggles visibility; it does not create, crop, or reorder it.
    floor_shot_video_enabled: bool = Field(
        default=False,
        validation_alias="FLOOR_SHOT_VIDEO_ENABLED",
    )
    obs_shot_video_source: str = Field(
        default="Shot Video",
        min_length=1,
        validation_alias="OBS_SHOT_VIDEO_SOURCE",
    )
    floor_shot_video_delay_seconds: float = Field(
        default=0.75,
        ge=0.0,
        validation_alias="FLOOR_SHOT_VIDEO_DELAY_SECONDS",
    )
    floor_shot_video_duration_seconds: float = Field(
        default=4.0,
        gt=0.0,
        validation_alias="FLOOR_SHOT_VIDEO_DURATION_SECONDS",
    )
    # The same again for ProTee's Top Impact view, and deliberately separate
    # settings rather than a list: a bay commonly has one of the two captured
    # and not the other, and each wants its own timing. The default delay is
    # longer than Shot Video's because the impact image is drawn after the
    # swing; correct it from a real shot.
    floor_top_impact_enabled: bool = Field(
        default=False,
        validation_alias="FLOOR_TOP_IMPACT_ENABLED",
    )
    obs_top_impact_source: str = Field(
        default="Top Impact",
        min_length=1,
        validation_alias="OBS_TOP_IMPACT_SOURCE",
    )
    floor_top_impact_delay_seconds: float = Field(
        default=1.5,
        ge=0.0,
        validation_alias="FLOOR_TOP_IMPACT_DELAY_SECONDS",
    )
    floor_top_impact_duration_seconds: float = Field(
        default=4.0,
        gt=0.0,
        validation_alias="FLOOR_TOP_IMPACT_DURATION_SECONDS",
    )

    # Swing capture. Stills of the two Swing Video panels ProTee already draws
    # after every shot, read back through OBS. Off by default: it depends on two
    # OBS sources that CaddieJ does not create, and it writes pictures of
    # somebody in their own home to disk. Both should be decisions a person
    # made. Nothing here opens a camera, reads ProTee's recordings, or touches
    # ProTee in any way.
    swing_capture_enabled: bool = Field(
        default=False,
        validation_alias="SWING_CAPTURE_ENABLED",
    )
    swing_capture_panels: str = Field(
        default="swing_behind,swing_side,impact,delivery",
        validation_alias="SWING_CAPTURE_PANELS",
    )
    # The private OBS scene that keeps the coach sources showing, and so
    # refreshed, while their floor items stay hidden. Never put on Program.
    swing_capture_scene: str = Field(
        default="CaddieJ Capture",
        min_length=1,
        validation_alias="SWING_CAPTURE_SCENE",
    )
    # Deprecated alias for the behind view. Used only when
    # SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE is empty. If both name a source,
    # the explicit behind setting wins and this one is not captured again.
    swing_capture_panel_swing_source: str = Field(
        default="",
        validation_alias="SWING_CAPTURE_PANEL_SWING_SOURCE",
    )
    swing_capture_panel_swing_kind: str = Field(
        default="dynamic",
        validation_alias="SWING_CAPTURE_PANEL_SWING_KIND",
    )
    swing_capture_panel_swing_supports: str = Field(
        default=(
            "body_through_impact,body_in_backswing,whole_body,finish_position,sequence"
        ),
        validation_alias="SWING_CAPTURE_PANEL_SWING_SUPPORTS",
    )
    swing_capture_panel_swing_behind_source: str = Field(
        default="",
        validation_alias="SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE",
    )
    swing_capture_panel_swing_behind_kind: str = Field(
        default="dynamic",
        validation_alias="SWING_CAPTURE_PANEL_SWING_BEHIND_KIND",
    )
    swing_capture_panel_swing_behind_supports: str = Field(
        default=(
            "body_through_impact,body_in_backswing,whole_body,finish_position,sequence"
        ),
        validation_alias="SWING_CAPTURE_PANEL_SWING_BEHIND_SUPPORTS",
    )
    swing_capture_panel_swing_side_source: str = Field(
        default="",
        validation_alias="SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE",
    )
    swing_capture_panel_swing_side_kind: str = Field(
        default="dynamic",
        validation_alias="SWING_CAPTURE_PANEL_SWING_SIDE_KIND",
    )
    swing_capture_panel_swing_side_supports: str = Field(
        default=(
            "body_through_impact,body_in_backswing,whole_body,finish_position,sequence"
        ),
        validation_alias="SWING_CAPTURE_PANEL_SWING_SIDE_SUPPORTS",
    )
    swing_capture_panel_impact_source: str = Field(
        default="",
        validation_alias="SWING_CAPTURE_PANEL_IMPACT_SOURCE",
    )
    swing_capture_panel_impact_kind: str = Field(
        default="static",
        validation_alias="SWING_CAPTURE_PANEL_IMPACT_KIND",
    )
    swing_capture_panel_impact_supports: str = Field(
        default="",
        validation_alias="SWING_CAPTURE_PANEL_IMPACT_SUPPORTS",
    )
    swing_capture_panel_delivery_source: str = Field(
        default="",
        validation_alias="SWING_CAPTURE_PANEL_DELIVERY_SOURCE",
    )
    swing_capture_panel_delivery_kind: str = Field(
        default="static",
        validation_alias="SWING_CAPTURE_PANEL_DELIVERY_KIND",
    )
    swing_capture_panel_delivery_supports: str = Field(
        default="",
        validation_alias="SWING_CAPTURE_PANEL_DELIVERY_SUPPORTS",
    )
    swing_capture_initial_delay_seconds: float = Field(
        default=0.5,
        ge=0.0,
        validation_alias=AliasChoices(
            "SWING_CAPTURE_DYNAMIC_DELAY_SECONDS",
            "SWING_CAPTURE_INITIAL_DELAY_SECONDS",
        ),
    )
    # Long enough to reach the release in both replays with a second to spare:
    # on the bay the replays replace their loading screens about 1.4 s into
    # the burst and the down-the-line release is 2.1 s after that. Ending at
    # 5.0 s, it ends with the static pass, so it adds nothing to a capture.
    swing_capture_duration_seconds: float = Field(
        default=4.5,
        gt=0.0,
        validation_alias=AliasChoices(
            "SWING_CAPTURE_DYNAMIC_DURATION_SECONDS",
            "SWING_CAPTURE_DURATION_SECONDS",
        ),
    )
    swing_capture_static_delay_seconds: float = Field(
        default=3.5,
        ge=0.0,
        validation_alias="SWING_CAPTURE_STATIC_DELAY_SECONDS",
    )
    swing_capture_static_duration_seconds: float = Field(
        default=1.5,
        gt=0.0,
        validation_alias="SWING_CAPTURE_STATIC_DURATION_SECONDS",
    )
    swing_capture_static_fps: float = Field(
        default=2.0,
        gt=0.0,
        le=30.0,
        validation_alias="SWING_CAPTURE_STATIC_FPS",
    )
    swing_capture_debug_keep_shots: int = Field(
        default=0,
        ge=0,
        le=40,
        validation_alias="SWING_CAPTURE_DEBUG_KEEP_SHOTS",
    )
    # How many shot directories to keep under media/swing_shots. Three is the
    # production default: the newest shot and two behind it. Diagnostic
    # timelines are files inside those directories, so raising the timeline
    # setting below also retains at least that many shots. The hard cap is 40.
    swing_capture_evidence_keep: int = Field(
        default=3,
        ge=3,
        le=40,
        validation_alias="SWING_CAPTURE_EVIDENCE_KEEP",
    )
    # Timing diagnostic, off in production. For the next N published shots of
    # this run, every screenshot the burst already took of the down-the-line
    # and face-on replays is also written under the shot's own `diagnostic/`
    # directory, with its timing. Nothing extra is asked of OBS, and nothing
    # extra reaches the model or the sheet. N cannot exceed the retention cap.
    # The shot directories are then kept at least until N of them fit.
    swing_capture_diagnostic_timeline_shots: int = Field(
        default=0,
        ge=0,
        le=40,
        validation_alias="SWING_CAPTURE_DIAGNOSTIC_TIMELINE_SHOTS",
    )
    # How often. This is a screenshot rate, not a camera frame rate: ProTee
    # films at 120 fps and nothing here can recover that. Ten a second is dense
    # enough that the selected frames land near where they were aimed.
    swing_capture_fps: float = Field(
        default=10.0,
        gt=0.0,
        le=60.0,
        validation_alias="SWING_CAPTURE_FPS",
    )
    # A faster stretch of the body burst, from the top to the release. P5 to
    # P8 take about 0.2 s and P6 to P7 a few hundredths, so at ten a second
    # two stills span the whole of it. Twenty puts a still within about 30 ms
    # of each position; faster buys less than OBS's own jitter. The window is
    # seconds from the strike, like the burst: on six real captures the
    # replay started 1.83-2.06 s in, the top was 1.5 s after that and the
    # release 2.15 s at the latest, so 3.3-4.4 s holds all of them with a
    # margin for tempo. Zero, or anything not faster than SWING_CAPTURE_FPS,
    # turns it off.
    swing_capture_downswing_fps: float = Field(
        default=20.0,
        ge=0.0,
        le=60.0,
        validation_alias="SWING_CAPTURE_DOWNSWING_FPS",
    )
    swing_capture_downswing_delay_seconds: float = Field(
        default=3.3,
        ge=0.0,
        validation_alias="SWING_CAPTURE_DOWNSWING_DELAY_SECONDS",
    )
    swing_capture_downswing_duration_seconds: float = Field(
        default=1.1,
        gt=0.0,
        validation_alias="SWING_CAPTURE_DOWNSWING_DURATION_SECONDS",
    )
    # Where the one swing on disk lives. Under the media root by default, so
    # deleting that directory is still enough to remove everything CaddieJ has
    # written. Replaced whole on every shot; there is no archive.
    swing_capture_output_dir: LocalPath = Field(
        default=Path("media/latest_swing"),
        validation_alias="SWING_CAPTURE_OUTPUT_DIR",
    )
    # How many stills are kept for one shot, across every panel. Each static
    # panel is offered one still when that leaves every live body panel at
    # least one; the rest is split across the body panels.
    #
    # Sixteen: the impact panel, eight aimed moments down the line (setup
    # through release) and seven face-on (no takeaway). Thirteen bought six
    # per camera and never the release, so no chicken wing could be seen, and
    # dropped the backswing that the shaft's pitch is read from. A calibrated
    # camera takes at most one still per aimed moment, so a larger number is
    # not spent on near-duplicates; a smaller one drops moments by priority.
    swing_capture_selected_frames: int = Field(
        default=16,
        ge=1,
        le=60,
        validation_alias="SWING_CAPTURE_SELECTED_FRAMES",
    )
    # What to scale each still down to, in pixels of width. Zero asks OBS for
    # the source's own resolution. OBS keeps the aspect ratio and refuses
    # anything outside 8..4096.
    swing_capture_image_width: int = Field(
        default=0,
        ge=0,
        le=4096,
        validation_alias="SWING_CAPTURE_IMAGE_WIDTH",
    )
    # One post-shot still of ProTee's All Values grid, for the coach to read
    # the numbers from. Taken inside the swing capture's own source hold, at a
    # fixed delay from the strike, at the source's own resolution, and never
    # counted against SWING_CAPTURE_SELECTED_FRAMES.
    coach_metrics_capture_enabled: bool = Field(
        default=False,
        validation_alias="COACH_METRICS_CAPTURE_ENABLED",
    )
    coach_metrics_source: str = Field(
        default="All Values",
        validation_alias="COACH_METRICS_SOURCE",
    )
    coach_metrics_capture_delay_seconds: float = Field(
        default=3.0,
        ge=0.0,
        le=15.0,
        validation_alias="COACH_METRICS_CAPTURE_DELAY_SECONDS",
    )

    @model_validator(mode="after")
    def _check_coach_metrics(self) -> Self:
        if self.coach_metrics_capture_enabled and not self.coach_metrics_source.strip():
            raise ValueError(
                "COACH_METRICS_CAPTURE_ENABLED=true needs COACH_METRICS_SOURCE, "
                "the OBS source that shows ProTee's All Values panel"
            )
        if self.coach_openai_budget < self.coach_openai_timeout:
            raise ValueError(
                "COACH_OPENAI_BUDGET_SECONDS must be at least "
                "COACH_OPENAI_TIMEOUT_SECONDS, or no attempt gets its full time"
            )
        return self

    def coach_metrics_source_name(self) -> str:
        """The All Values source to capture, or empty when capture is off."""

        if not self.coach_metrics_capture_enabled:
            return ""
        return self.coach_metrics_source.strip()

    def resolved_coaching_trigger(self) -> Literal["protee", "gspro"]:
        """`auto` is ProTee whenever swing capture is on."""

        if self.coaching_trigger == "auto":
            return "protee" if self.swing_capture_enabled else "gspro"
        return self.coaching_trigger

    @model_validator(mode="after")
    def _check_swing_capture(self) -> Self:
        """Refuse a width OBS would refuse, rather than failing every still.

        OBS accepts 8..4096 for a screenshot width and rejects the request
        otherwise. Zero is CaddieJ's own "do not scale", and a value between
        the two would silently cost every frame of every swing.
        """

        width = self.swing_capture_image_width
        if 0 < width < 8:
            raise ValueError(
                "SWING_CAPTURE_IMAGE_WIDTH must be 0 (the source's own "
                f"resolution) or at least 8 pixels, got {width}"
            )
        wall = max(
            self.swing_capture_initial_delay_seconds
            + self.swing_capture_duration_seconds,
            self.swing_capture_static_delay_seconds
            + self.swing_capture_static_duration_seconds,
        )
        if self.swing_capture_enabled and wall >= self.coaching_clip_wait:
            raise ValueError(
                "swing capture wall time "
                f"({wall:.1f}s) must be less than COACHING_CLIP_WAIT "
                f"({self.coaching_clip_wait:.1f}s)"
            )
        reveals = self.floor_top_impact_enabled or self.floor_shot_video_enabled
        if (
            reveals
            and self.swing_capture_enabled
            and self.obs_floor_scene.strip() == self.swing_capture_scene.strip()
        ):
            # A reveal toggles items in the floor scene; capture needs its
            # items left enabled in a scene nobody watches. One scene cannot
            # be both, and the failure would be a coach going blind mid-session.
            raise ValueError(
                f"OBS_FLOOR_SCENE ({self.obs_floor_scene!r}) must not be the "
                f"swing capture scene ({self.swing_capture_scene!r}) while a "
                "floor reveal is enabled"
            )
        return self

    @model_validator(mode="after")
    def _resolve_physical_tee(self) -> Self:
        """One physical tee. Legacy 0–1 values convert, they do not compete."""

        # Snapshotted before anything below assigns, because writing a field on
        # a validated model adds it to `model_fields_set` — so by the time the
        # offsets are resolved, every value this validator has already settled
        # would look as though somebody had configured it.
        explicit = frozenset(self.model_fields_set)
        x_m = self.floor_tee_x_m
        y_m = self.floor_tee_y_m
        if "floor_tee_x_m" not in explicit and self.floor_tee_x is not None:
            warnings.warn(
                "FLOOR_TEE_X is deprecated; use FLOOR_TEE_X_M (metres on the "
                "physical floor). The 0–1 value was converted using FLOOR_WIDTH_M.",
                DeprecationWarning,
                stacklevel=2,
            )
            x_m = self.floor_tee_x * self.floor_width_m
        elif "floor_tee_x_m" in explicit and self.floor_tee_x is not None:
            warnings.warn(
                "FLOOR_TEE_X is ignored because FLOOR_TEE_X_M is set.",
                DeprecationWarning,
                stacklevel=2,
            )
        if "floor_tee_y_m" not in explicit and self.floor_tee_y is not None:
            warnings.warn(
                "FLOOR_TEE_Y is deprecated; use FLOOR_TEE_Y_M (metres on the "
                "physical floor). The 0–1 value was converted using FLOOR_DEPTH_M.",
                DeprecationWarning,
                stacklevel=2,
            )
            y_m = self.floor_tee_y * self.floor_depth_m
        elif "floor_tee_y_m" in explicit and self.floor_tee_y is not None:
            warnings.warn(
                "FLOOR_TEE_Y is ignored because FLOOR_TEE_Y_M is set.",
                DeprecationWarning,
                stacklevel=2,
            )
        if not 0.0 <= x_m <= self.floor_width_m:
            raise ValueError(
                "FLOOR_TEE_X_M must lie on the configured floor "
                f"(0..{self.floor_width_m} m), got {x_m}"
            )
        if not 0.0 <= y_m <= self.floor_depth_m:
            raise ValueError(
                "FLOOR_TEE_Y_M must lie on the configured floor "
                f"(0..{self.floor_depth_m} m), got {y_m}"
            )
        self.floor_tee_x_m = x_m
        self.floor_tee_y_m = y_m
        driver_x = (
            self.floor_tee_driver_x_m if "floor_tee_driver_x_m" in explicit else x_m
        )
        driver_y = (
            self.floor_tee_driver_y_m if "floor_tee_driver_y_m" in explicit else y_m
        )
        if not 0.0 <= driver_x <= self.floor_width_m:
            raise ValueError(
                "FLOOR_TEE_DRIVER_X_M must lie on the configured floor "
                f"(0..{self.floor_width_m} m), got {driver_x}"
            )
        if not 0.0 <= driver_y <= self.floor_depth_m:
            raise ValueError(
                "FLOOR_TEE_DRIVER_Y_M must lie on the configured floor "
                f"(0..{self.floor_depth_m} m), got {driver_y}"
            )
        self.floor_tee_driver_x_m = driver_x
        self.floor_tee_driver_y_m = driver_y
        strip_x = (
            self.floor_hitting_strip_x_m
            if "floor_hitting_strip_x_m" in explicit
            else x_m
        )
        strip_y = (
            self.floor_hitting_strip_y_m
            if "floor_hitting_strip_y_m" in explicit
            else y_m
        )
        if not 0.0 <= strip_x <= self.floor_width_m:
            raise ValueError(
                "FLOOR_HITTING_STRIP_X_M must lie on the configured floor "
                f"(0..{self.floor_width_m} m), got {strip_x}"
            )
        if not 0.0 <= strip_y <= self.floor_depth_m:
            raise ValueError(
                "FLOOR_HITTING_STRIP_Y_M must lie on the configured floor "
                f"(0..{self.floor_depth_m} m), got {strip_y}"
            )
        self.floor_hitting_strip_x_m = strip_x
        self.floor_hitting_strip_y_m = strip_y
        self._resolve_ball_offsets(explicit, strip_x, strip_y)
        if self.floor_shot_result_min_duration > self.floor_shot_result_max_duration:
            raise ValueError(
                "FLOOR_SHOT_RESULT_MIN_DURATION must not exceed "
                "FLOOR_SHOT_RESULT_MAX_DURATION, got "
                f"{self.floor_shot_result_min_duration} > "
                f"{self.floor_shot_result_max_duration}"
            )
        if self.gspro_round_file_path is None:
            self.gspro_round_file_path = default_gspro_round_file()
        if self.protee_root_path is None:
            self.protee_root_path = default_protee_root()
        return self

    def _resolve_ball_offsets(
        self, explicit: frozenset[str], strip_x: float, strip_y: float
    ) -> None:
        """One ball position per placement mode, as an offset from the strip.

        Two ways of saying the same thing have to be reconciled: the offsets,
        which are what an operator configures now, and the absolute
        `FLOOR_TEE_*_M` points, which is what an existing `.env` has. The
        offsets win where they were given, because they are the ones somebody
        set deliberately; an absolute tee is converted by subtracting the
        anchor, which is exactly the offset it stood for.

        A tee left at its *default* is not converted. It would otherwise turn a
        moved strip into a ball a metre and a half behind it: the defaults
        describe where the mat was, not where the ball sits on the mat, and the
        whole point of anchoring is that the second of those survives moving the
        first.
        """

        tee_set = bool({"floor_tee_x_m", "floor_tee_y_m"} & explicit) or (
            self.floor_tee_x is not None or self.floor_tee_y is not None
        )
        if tee_set:
            if "floor_ball_iron_dx_m" not in explicit:
                self.floor_ball_iron_dx_m = self.floor_tee_x_m - strip_x
            if "floor_ball_iron_dy_m" not in explicit:
                self.floor_ball_iron_dy_m = self.floor_tee_y_m - strip_y

        driver_set = bool({"floor_tee_driver_x_m", "floor_tee_driver_y_m"} & explicit)
        if "floor_ball_driver_dx_m" not in explicit:
            self.floor_ball_driver_dx_m = (
                self.floor_tee_driver_x_m - strip_x
                if driver_set
                else self.floor_ball_iron_dx_m
            )
        if "floor_ball_driver_dy_m" not in explicit:
            self.floor_ball_driver_dy_m = (
                self.floor_tee_driver_y_m - strip_y
                if driver_set
                else self.floor_ball_iron_dy_m
            )

    def club_carries(self) -> dict[str, float]:
        """The configured bag as labelled metres, in the order clubs are hit.

        The label is what the floor shows, so it lives next to the variable it
        came from rather than being reconstructed from a field name somewhere
        downstream. Clubs left at zero are still listed here; dropping them is
        `ClubDistanceProfile.from_carries`' job, because "not in the bag" is a
        fact about a bag rather than about a settings file.
        """

        return {
            "DR": self.player_club_carry_driver_m,
            "3W": self.player_club_carry_3w_m,
            "5W": self.player_club_carry_5w_m,
            "4I": self.player_club_carry_4i_m,
            "5I": self.player_club_carry_5i_m,
            "6I": self.player_club_carry_6i_m,
            "7I": self.player_club_carry_7i_m,
            "8I": self.player_club_carry_8i_m,
            "9I": self.player_club_carry_9i_m,
            "PW": self.player_club_carry_pw_m,
            "GW": self.player_club_carry_gw_m,
            "SW": self.player_club_carry_sw_m,
        }

    def capture_panels(self) -> tuple[CapturePanel, ...]:
        """The configured OBS panels, in capture order. Empty source means skip.

        `SWING_CAPTURE_PANEL_SWING_SOURCE` is a deprecated alias for the behind
        view. It is used only when the behind source is unset. When both are
        set, the explicit behind source wins and the alias is not captured a
        second time. The same OBS source is never captured twice.
        """

        behind_source = self.swing_capture_panel_swing_behind_source.strip()
        legacy = self.swing_capture_panel_swing_source.strip()
        legacy_used = False
        if not behind_source and legacy:
            behind_source = legacy
            legacy_used = True
        elif behind_source and legacy and legacy != behind_source:
            warnings.warn(
                "SWING_CAPTURE_PANEL_SWING_SOURCE is ignored because "
                "SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE is set. The old "
                "name is a deprecated alias for the behind view and is not "
                "captured as a second panel.",
                DeprecationWarning,
                stacklevel=2,
            )
        side_source = self.swing_capture_panel_swing_side_source.strip()
        if side_source and side_source == behind_source:
            warnings.warn(
                "SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE names the same OBS "
                "source as the behind view, so the side panel is not captured "
                "a second time.",
                UserWarning,
                stacklevel=2,
            )
            side_source = ""

        if legacy_used:
            behind_kind = self.swing_capture_panel_swing_kind
            behind_supports = self.swing_capture_panel_swing_supports
            behind_setting = "SWING_CAPTURE_PANEL_SWING_SOURCE"
        else:
            behind_kind = self.swing_capture_panel_swing_behind_kind
            behind_supports = self.swing_capture_panel_swing_behind_supports
            behind_setting = "SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE"

        catalog = {
            "swing_behind": (
                behind_source,
                behind_kind,
                behind_supports,
                PanelView.BEHIND,
                behind_setting,
            ),
            "swing_side": (
                side_source,
                self.swing_capture_panel_swing_side_kind,
                self.swing_capture_panel_swing_side_supports,
                PanelView.SIDE,
                "SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE",
            ),
            "impact": (
                self.swing_capture_panel_impact_source,
                self.swing_capture_panel_impact_kind,
                self.swing_capture_panel_impact_supports,
                PanelView.NONE,
                "SWING_CAPTURE_PANEL_IMPACT_SOURCE",
            ),
            "delivery": (
                self.swing_capture_panel_delivery_source,
                self.swing_capture_panel_delivery_kind,
                self.swing_capture_panel_delivery_supports,
                PanelView.NONE,
                "SWING_CAPTURE_PANEL_DELIVERY_SOURCE",
            ),
        }
        panels: list[CapturePanel] = []
        seen: set[str] = set()
        for panel_id in self.requested_capture_panel_ids():
            source, kind_name, supports_text, view, setting = catalog[panel_id]
            source = source.strip()
            if not source or source in seen:
                continue
            seen.add(source)
            try:
                kind = PanelKind(kind_name.strip().lower())
            except ValueError:
                kind = PanelKind.DYNAMIC
            supports = frozenset(
                VisualClaim(part.strip())
                for part in supports_text.split(",")
                if part.strip()
                and part.strip() in {claim.value for claim in VisualClaim}
            )
            panels.append(
                CapturePanel(
                    panel_id=panel_id,
                    source=source,
                    kind=kind,
                    supports=supports,
                    view=view,
                    setting=setting,
                )
            )
        return tuple(panels)

    def requested_capture_panel_ids(self) -> tuple[str, ...]:
        """Panel ids named in `SWING_CAPTURE_PANELS`, in that order.

        The list is the whole of what may be captured. A role that is not in
        it is not missing; it was left out. `swing` is the old name for
        `swing_behind`.
        """

        order: list[str] = []
        known = {"swing_behind", "swing_side", "impact", "delivery"}
        for name in self.swing_capture_panels.split(","):
            panel_id = name.strip().lower()
            if panel_id == "swing":
                panel_id = "swing_behind"
            if panel_id in known and panel_id not in order:
                order.append(panel_id)
        return tuple(order)

    def absent_capture_roles(self) -> tuple[tuple[str, str], ...]:
        """Requested panels that have no source, as (title, environment variable).

        A panel left out of `SWING_CAPTURE_PANELS` is not reported. Delivery
        is one optional panel among the others, not a required role.
        """

        present = {panel.panel_id for panel in self.capture_panels()}
        roles = {
            "swing_behind": (
                "Swing Behind",
                "SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE",
            ),
            "swing_side": ("Swing Side", "SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE"),
            "impact": ("Impact", "SWING_CAPTURE_PANEL_IMPACT_SOURCE"),
            "delivery": ("Delivery", "SWING_CAPTURE_PANEL_DELIVERY_SOURCE"),
        }
        return tuple(
            roles[panel_id]
            for panel_id in self.requested_capture_panel_ids()
            if panel_id not in present
        )
