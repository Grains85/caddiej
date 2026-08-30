"""Application configuration loaded from environment variables."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from caddiej.domain.coaching import SEVERITY_FINE


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
    # Where the ball goes, in calibrated surface coordinates (0..1). Set by
    # eye for now: a later wizard will measure the mat. y is towards the
    # player, so a value near the bottom puts the marker where someone stands.
    floor_tee_x: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias="FLOOR_TEE_X",
    )
    floor_tee_y: float = Field(
        default=0.72,
        ge=0.0,
        le=1.0,
        validation_alias="FLOOR_TEE_Y",
    )
    # How far the club-path line reaches, in metres of the physical floor.
    # On the default 1 m × 1 m bay this is the same number it always was.
    floor_club_path_length: float = Field(
        default=0.35,
        gt=0.0,
        validation_alias="FLOOR_CLUB_PATH_LENGTH",
    )
    # How long a shot stays on the floor before it makes way for the next ball.
    floor_shot_display_seconds: float = Field(
        default=6.0,
        gt=0,
        validation_alias="FLOOR_SHOT_DISPLAY_SECONDS",
    )

    # GSPro Open Connect. CaddieJ sits between the launch monitor connector and
    # GSPro, because Open Connect is a feed into GSPro with no observer
    # interface: the only way to see shots is to be in the conversation. Off by
    # default, since enabling it means the launch monitor must be pointed at
    # CaddieJ instead of GSPro.
    # "simulate" runs a scripted round through the real adapter against a fake
    # GSPro inside this process, so the whole path from bytes to floor can be
    # watched without a simulator, a launch monitor, or a golf swing.
    gspro_mode: Literal["off", "interpose", "simulate"] = Field(
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
    # How long a coaching line stays on the floor. Longer than the shot display,
    # because a sentence takes longer to read than a number.
    coaching_display_seconds: float = Field(
        default=9.0,
        gt=0,
        validation_alias="COACHING_DISPLAY_SECONDS",
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
        default=60,
        gt=0,
        validation_alias="COACHING_MAX_OUTPUT_TOKENS",
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
