"""The command line, and what CaddieJ says before it starts.

Everything here runs before the application exists, which is what shapes it.
A person who has just cloned CaddieJ meets this file first, and the two things
they are most likely to get wrong — a setting they typed by hand and an optional
dependency they have not installed — are both invisible from inside the running
program. So configuration is validated here and reported in prose, rather than
reaching the event loop as a traceback.

Prose is the exception, not the rule. Once CaddieJ is running it speaks in
structured JSON, because those lines are read by machines and by somebody
searching a log a week later. These lines are read by a person at a terminal who
does not have the application yet.
"""

import argparse
import importlib.util
import os
import shutil
from pathlib import Path

from pydantic import SecretStr, ValidationError

from caddiej import __version__
from caddiej.config import Settings

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Only checked when `--check-config` asks. Each one is optional by design, so a
# missing package is a fact about this machine rather than a fault.
_EXTRAS = (
    ("openai", "openai", "ai", "COACHING_PROVIDER=openai"),
    ("kokoro", "kokoro", "tts", "SPEECH_ENABLED=true"),
    ("sounddevice", "sounddevice", "tts", "SPEECH_OUTPUT=sounddevice"),
    ("httpx", "httpx", "hue", "LIGHTING_OUTPUT=hue"),
    ("mcp", "mcp", "mcp", "the caddiej-mcp translator"),
)


def build_parser() -> argparse.ArgumentParser:
    """The whole command line. Deliberately small.

    CaddieJ is configured by environment variables and `.env`, not by flags, so
    there is exactly one option per thing a person needs before the application
    is running: what version this is, how loud it should be, and whether the
    configuration it is about to use makes sense.
    """

    parser = argparse.ArgumentParser(
        prog="caddiej",
        description=(
            "Run CaddieJ, the orchestration layer for a home golf simulator. "
            "It serves the floor projection overlay on loopback and keeps "
            "running until Ctrl+C."
        ),
        epilog=(
            "CaddieJ is configured through environment variables and a .env "
            "file in the working directory, not through options. Copy "
            ".env.example to .env for annotated defaults, and see "
            "docs/configuration.md for the full reference. Running CaddieJ "
            "writes its shot history and media into the working directory "
            "unless HISTORY_DATABASE_PATH and VIDEO_MEDIA_ROOT say otherwise."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"caddiej {__version__}",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help=(
            "report the configuration CaddieJ would run with, and which "
            "optional dependencies are installed, then exit"
        ),
    )
    parser.add_argument(
        "--check-obs-shot-video",
        action="store_true",
        help=(
            "show the configured OBS Shot Video source for a moment and hide it "
            "again, to prove CaddieJ can control it, then exit"
        ),
    )
    parser.add_argument(
        "--check-obs-top-impact",
        action="store_true",
        help=(
            "show the configured OBS Top Impact source for a moment and hide it "
            "again, to prove CaddieJ can control it, then exit"
        ),
    )
    parser.add_argument(
        "--check-swing-capture",
        action="store_true",
        help=(
            "take one screenshot from each configured swing capture source, "
            "save them where they can be opened, and report their size, then "
            "exit; no golf shot is required"
        ),
    )
    parser.add_argument(
        "--check-speech",
        action="store_true",
        help=(
            "speak one short line through the configured voice and speaker, "
            "to prove CaddieJ can talk, then exit"
        ),
    )
    parser.add_argument(
        "--check-video-frames",
        action="store_true",
        help=(
            "extract stills from the newest recorded swing clip, to prove "
            "FFmpeg can read it, then exit"
        ),
    )
    reference = parser.add_argument_group(
        "swing reference",
        "Options for --analyze-reference-swing. The command reads a saved "
        "SwingEvidencePackage. It does not call a model unless "
        "--reference-call-model is set.",
    )
    reference.add_argument(
        "--analyze-reference-swing",
        action="store_true",
        help=(
            "assess one saved SwingEvidencePackage with ideal swing 2.9 and "
            "the coaching engine, then exit"
        ),
    )
    reference.add_argument(
        "--reference-package",
        type=Path,
        default=None,
        metavar="PATH",
        help="evidence_package.json, or a directory that contains it",
    )
    reference.add_argument(
        "--reference-shot-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="raw capture directory the package's relative_file values are under",
    )
    reference.add_argument(
        "--reference-output",
        type=Path,
        default=None,
        metavar="DIR",
        help="where request, image index, decision and session state are written",
    )
    reference.add_argument(
        "--reference-mock",
        default=None,
        metavar="PATH",
        help=(
            "a saved assessment JSON, or 'insufficient' for a reply that "
            "scores nothing; no model call"
        ),
    )
    reference.add_argument(
        "--reference-call-model",
        action="store_true",
        help="make one vision-model request with the configured coaching model",
    )
    reference.add_argument(
        "--reference-delivered",
        action="store_true",
        help=(
            "record that the golfer was told the previous line; without this "
            "the next request does not claim the cue was delivered"
        ),
    )
    reference.add_argument(
        "--reference-sequence",
        type=int,
        default=1,
        metavar="N",
        help="session sequence for this shot; a repeated or older value is ignored",
    )
    reference.add_argument(
        "--reference-state",
        type=Path,
        default=None,
        metavar="PATH",
        help="session_state.json from an earlier reference analysis",
    )
    reference.add_argument(
        "--reference-previous",
        type=Path,
        default=None,
        metavar="PATH",
        help="previous coaching context JSON; only the active focus and delivered cue",
    )
    reference.add_argument(
        "--reference-shot-id",
        default=None,
        help="shot id for --reference-mock insufficient; default is the package id",
    )
    reference.add_argument("--reference-club", default="unknown")
    reference.add_argument("--reference-shot-type", default="unknown")
    reference.add_argument("--reference-distance", type=float, default=None)
    reference.add_argument("--reference-handedness", default="unknown")
    reference.add_argument("--reference-target", default="unknown")
    reference.add_argument("--reference-evidence-context", default="unknown")
    parser.add_argument(
        "--export-diagnostics",
        action="store_true",
        help=(
            "write a zip of the latest diagnostic session, or of "
            "--diagnostics-session, then exit"
        ),
    )
    parser.add_argument(
        "--diagnostics-session",
        default=None,
        help="session id to export; omit for the newest session",
    )
    parser.add_argument(
        "--diagnostics-output",
        type=Path,
        default=None,
        metavar="ZIP",
        help="where to write the diagnostic zip",
    )
    parser.add_argument(
        "--diagnostics-include-images",
        action="store_true",
        help="copy the selected evidence frames into the zip when the files still exist",
    )
    parser.add_argument(
        "--log-level",
        choices=_LOG_LEVELS,
        default=None,
        help=(
            "how much to log; defaults to the LOG_LEVEL environment variable, or INFO"
        ),
    )
    return parser


def resolve_log_level(requested: str | None) -> str:
    """The flag, then the environment, then INFO.

    Not a `Settings` field, because it has to be known before configuration is
    validated: the log level decides how a configuration problem is reported.
    """

    if requested:
        return requested
    from_environment = os.environ.get("LOG_LEVEL", "").strip().upper()
    if from_environment in _LOG_LEVELS:
        return from_environment
    return "INFO"


def load_settings() -> Settings:
    """Read the configuration, or raise `ValidationError` for `explain`."""

    return Settings()


def explain(error: ValidationError) -> str:
    """A configuration error as a person can act on it.

    Names the variable, says what was wrong, and never quotes the value of a
    secret: a mistyped API key is reported as a mistyped API key rather than
    printed to a terminal and into somebody's scrollback.
    """

    secrets = _secret_aliases()
    problems = error.errors()
    heading = (
        "CaddieJ could not start: "
        f"{len(problems)} setting{'' if len(problems) == 1 else 's'} "
        f"{'is' if len(problems) == 1 else 'are'} invalid."
    )
    lines = [heading, ""]
    for problem in problems:
        name = ".".join(str(part) for part in problem["loc"]) or "(unknown)"
        if name in secrets:
            lines.append(f"  {name}")
        else:
            lines.append(f"  {name}={problem.get('input')!r}")
        lines.append(f"    {problem['msg']}")
    lines.extend(
        [
            "",
            "Settings are read from the environment and from a .env file in the",
            "working directory. Copy .env.example to .env for annotated defaults,",
            "or see docs/configuration.md for the full reference.",
        ]
    )
    return "\n".join(lines)


def describe(settings: Settings, root: Path | None = None) -> str:
    """What CaddieJ would do if it were started with this configuration.

    The answer to "is it me or is it broken?", which is otherwise only
    answerable by starting CaddieJ and reading JSON. Reports what is switched
    on, where files will be written, and which optional dependencies are
    present — and reports a credential as set or not set, never as itself.
    """

    here = root or Path.cwd()
    env_file = here / ".env"
    source = str(env_file) if env_file.is_file() else "environment only (no .env file)"

    lines = [
        f"caddiej {__version__} — configuration check",
        "",
        f"  Settings read from   {source}",
        f"  Floor overlay        http://{settings.hud_host}:{settings.hud_port}/",
        f"  Shot source          {_shot_source(settings)}",
        f"  Shot history         {_history(settings, here)}",
        f"  Swing video          {_video(settings, here)}",
        f"  Coaching             {_coaching(settings)}",
        *_diagnostics_lines(settings, here),
        f"  Speech               {_speech(settings)}",
        f"  Lighting             {_lighting(settings)}",
        f"  Celebrations         {_effects(settings)}",
        f"  Assistant API        {_assistant(settings)}",
        f"  ProTee Shot Video    {_shot_video(settings)}",
        f"  ProTee Top Impact    {_top_impact(settings)}",
        f"  Floor layout         {_floor_layout(settings)}",
        f"  Swing capture        {_swing_capture(settings, here)}",
        "",
        "Optional dependencies:",
    ]
    lines.extend(_extras(settings))
    lines.extend(["", "Configuration is valid."])
    return "\n".join(lines)


def _shot_source(settings: Settings) -> str:
    if settings.gspro_mode == "interpose":
        return (
            f"GSPro Open Connect — listening on {settings.gspro_bind_host}:"
            f"{settings.gspro_bind_port}, forwarding to "
            f"{settings.gspro_upstream_host}:{settings.gspro_upstream_port}"
        )
    if settings.gspro_mode == "simulate":
        return "simulated round (development only; no launch monitor involved)"
    if settings.gspro_mode == "observe":
        round_file = (
            str(settings.gspro_round_file_path)
            if settings.gspro_round_file_path is not None
            else "(no currentRound.dat path)"
        )
        protee = (
            str(settings.protee_root_path)
            if settings.protee_root_path is not None
            else "(no ProTee root path)"
        )
        return (
            "passive observation — ProTee log "
            f"{protee}, GSPro currentRound.dat {round_file} "
            "(does not listen on 9210)"
        )
    demo = " — floor demo on" if settings.floor_demo else ""
    return f"none (GSPRO_MODE=off){demo}"


def _history(settings: Settings, here: Path) -> str:
    if not settings.history_enabled:
        return "off (HISTORY_ENABLED=false)"
    return f"on → {_where(settings.history_database_path, here)}"


def _video(settings: Settings, here: Path) -> str:
    if not settings.video_enabled:
        return "off (VIDEO_ENABLED=false)"
    return (
        f"on — {settings.video_camera_kind} camera "
        f"{settings.video_camera_device!r} → {_where(settings.video_media_root, here)}"
    )


def _coaching(settings: Settings) -> str:
    if not settings.coaching_enabled:
        return "off (COACHING_ENABLED=false)"
    calls = "on" if settings.coaching_vision_calls else "off"
    if settings.coaching_provider == "none":
        provider = "COACHING_PROVIDER=none"
    elif settings.coaching_provider == "scripted":
        provider = "scripted provider"
    else:
        key = "set" if settings.coaching_api_key.get_secret_value() else "NOT SET"
        provider = (
            f"{settings.coaching_provider} {settings.coaching_model} "
            f"(COACHING_API_KEY {key})"
        )
    return (
        f"Ideal Swing 2.9, {provider}, vision calls {calls} "
        f"(COACHING_VISION_CALLS), image detail {settings.coaching_image_detail}, "
        f"feedback {settings.coaching_feedback}, "
        f"shot type {settings.coaching_shot_type}"
    )


def _diagnostics_lines(settings: Settings, here: Path) -> list[str]:
    enabled = "yes" if settings.diagnostics_enabled else "no"
    payloads = "yes" if settings.diagnostics_retain_payloads else "no"
    auto = "yes" if settings.diagnostics_auto_export_latest else "no"
    images = "yes" if settings.diagnostics_include_images else "no"
    latest = _where(settings.diagnostics_dir / "latest_shot.zip", here)
    return [
        "  Diagnostics:",
        f"    Enabled:               {enabled}",
        f"    Retain GPT payloads:   {payloads}",
        f"    Retained shots:        {settings.diagnostics_keep_shots}",
        f"    Auto-export latest:    {auto}",
        f"    Include images:        {images}",
        f"    Latest ZIP:            {latest}",
    ]


def _speech(settings: Settings) -> str:
    if not settings.speech_enabled:
        return "off (SPEECH_ENABLED=false)"
    return (
        f"on — {settings.speech_engine} voice {settings.speech_voice}, "
        f"output {settings.speech_output}"
    )


def _lighting(settings: Settings) -> str:
    if not settings.lighting_enabled:
        return "off (LIGHTING_ENABLED=false)"
    if settings.lighting_output == "none":
        return "on, but no lamp is touched (LIGHTING_OUTPUT=none)"
    host = settings.lighting_bridge_host or "NOT SET"
    key = "set" if settings.lighting_application_key.get_secret_value() else "NOT SET"
    return f"on — Hue bridge {host} (LIGHTING_APPLICATION_KEY {key})"


def _effects(settings: Settings) -> str:
    if not settings.effects_enabled:
        return "off (EFFECTS_ENABLED=false)"
    channels = [
        name
        for name, on in (
            ("floor", settings.effects_floor),
            ("speech", settings.effects_speech),
            ("lights", settings.effects_lights),
            ("obs", settings.effects_obs),
        )
        if on
    ]
    wired = ", ".join(channels) if channels else "no channels"
    return f"{wired} — inert until something scores a hole"


def _assistant(settings: Settings) -> str:
    if not settings.assistant_api_enabled:
        return "off (ASSISTANT_API_ENABLED=false)"
    if not settings.assistant_api_token.get_secret_value().strip():
        return "REFUSED — ASSISTANT_API_TOKEN is empty, so no port will be opened"
    writes = "read and write" if settings.assistant_api_allow_mutations else "read only"
    return (
        f"on — http://{settings.assistant_api_host}:{settings.assistant_api_port}/v1"
        f" ({writes})"
    )


def _shot_video(settings: Settings) -> str:
    """Whether a shot will briefly reveal the OBS source, and which one.

    Reports the scene and source names because the whole feature is two strings
    matching what somebody typed into OBS by hand, and a mismatch is invisible
    until a shot fails to produce a replay.
    """

    if not settings.floor_shot_video_enabled:
        return "off (FLOOR_SHOT_VIDEO_ENABLED=false)"
    password = "set" if settings.obs_password.get_secret_value() else "NOT SET"
    # "after impact" because the delay used to be measured from GSPro's completed
    # shot, and somebody reading this line needs to know which clock it is on.
    observer = (
        "" if settings.protee_observer_enabled else ", needs PROTEE_OBSERVER_ENABLED"
    )
    return (
        f"on — {settings.obs_floor_scene!r} / {settings.obs_shot_video_source!r} "
        f"{settings.floor_shot_video_delay_seconds:g}s after impact for "
        f"{settings.floor_shot_video_duration_seconds:g}s "
        f"(OBS_PASSWORD {password}{observer})"
    )


def _top_impact(settings: Settings) -> str:
    """The same two strings again, for the other ProTee view."""

    if not settings.floor_top_impact_enabled:
        return "off (FLOOR_TOP_IMPACT_ENABLED=false)"
    password = "set" if settings.obs_password.get_secret_value() else "NOT SET"
    observer = (
        "" if settings.protee_observer_enabled else ", needs PROTEE_OBSERVER_ENABLED"
    )
    return (
        f"on — {settings.obs_floor_scene!r} / {settings.obs_top_impact_source!r} "
        f"{settings.floor_top_impact_delay_seconds:g}s after impact for "
        f"{settings.floor_top_impact_duration_seconds:g}s "
        f"(OBS_PASSWORD {password}{observer})"
    )


def _floor_layout(settings: Settings) -> str:
    """The anchor, and the three ball positions it resolves to, in metres.

    Absolute rather than as offsets, because this line exists to be compared
    against a tape measure on a real bay — and because an older `.env` that
    still sets FLOOR_TEE_X_M has had it folded into an offset by now, and the
    only honest way to show what that did is to show where the ball ends up.
    """

    strip_x = settings.floor_hitting_strip_x_m
    strip_y = settings.floor_hitting_strip_y_m
    places = [
        ("iron", settings.floor_ball_iron_dx_m, settings.floor_ball_iron_dy_m),
        ("driver", settings.floor_ball_driver_dx_m, settings.floor_ball_driver_dy_m),
        ("putter", settings.floor_ball_putter_dx_m, settings.floor_ball_putter_dy_m),
    ]
    balls = ", ".join(
        f"{name} {strip_x + dx:.2f},{strip_y + dy:.2f}" for name, dx, dy in places
    )
    lane = (
        f"{settings.floor_putting_lane_length_m:g}×"
        f"{settings.floor_putting_lane_width_m:g} m"
        if settings.floor_putting_lane
        else "off"
    )
    return (
        f"strip {strip_x:.2f},{strip_y:.2f} m "
        f"({settings.floor_hitting_strip_width_m:g}×"
        f"{settings.floor_hitting_strip_depth_m:g} m) — "
        f"ball {balls} — putting lane {lane}"
    )


def _swing_capture(settings: Settings, here: Path) -> str:
    """Whether stills of the configured OBS panels are taken, from what, and where.

    Names every panel because each source string has to match what was typed
    into OBS, and reports the timing because every value in it is a first guess
    until a real shot corrects it.
    """

    if not settings.swing_capture_enabled:
        return "off (SWING_CAPTURE_ENABLED=false)"
    observer = (
        "" if settings.protee_observer_enabled else ", needs PROTEE_OBSERVER_ENABLED"
    )
    panels = settings.capture_panels()
    named = (
        ", ".join(
            f"{panel.panel_id}={panel.source!r} ({panel.kind.value})"
            for panel in panels
        )
        or "no panel source named"
    )
    return (
        f"on — {named}, "
        f"dynamic {settings.swing_capture_initial_delay_seconds:g}+"
        f"{settings.swing_capture_duration_seconds:g}s at "
        f"{settings.swing_capture_fps:g} fps{_downswing(settings)}, "
        f"static {settings.swing_capture_static_delay_seconds:g}+"
        f"{settings.swing_capture_static_duration_seconds:g}s → "
        f"{_where(settings.swing_capture_output_dir, here)} "
        f"(keeping {settings.swing_capture_selected_frames} stills total"
        f"{observer})"
    )


def _downswing(settings: Settings) -> str:
    fps = settings.swing_capture_downswing_fps
    if fps <= settings.swing_capture_fps:
        return ""
    start = settings.swing_capture_downswing_delay_seconds
    end = start + settings.swing_capture_downswing_duration_seconds
    return f" ({fps:g} fps {start:g}-{end:g}s)"


def _extras(settings: Settings) -> list[str]:
    ffmpeg = shutil.which(settings.video_ffmpeg_path)
    lines = [
        _extra_line(
            settings.video_ffmpeg_path,
            ffmpeg or "not found on PATH",
            "VIDEO_ENABLED=true",
        )
    ]
    for label, module, extra, needed_for in _EXTRAS:
        found = importlib.util.find_spec(module) is not None
        state = "installed" if found else f"not installed (pip install -e '.[{extra}]')"
        lines.append(_extra_line(label, state, needed_for))
    return lines


def _extra_line(label: str, state: str, needed_for: str) -> str:
    return f"  {label:<14} {state:<48} needed for {needed_for}"


def _where(path: Path, here: Path) -> str:
    """An absolute path, because "caddiej.sqlite3" does not say where.

    Which directory CaddieJ was started from is the thing people get wrong, so
    the check resolves it rather than repeating what they configured.
    """

    return str(path if path.is_absolute() else here / path)


def _secret_aliases() -> frozenset[str]:
    """Which environment variables hold a credential, asked of the model itself."""

    aliases = set()
    for field in Settings.model_fields.values():
        if field.annotation is SecretStr and isinstance(field.validation_alias, str):
            aliases.add(field.validation_alias)
    return frozenset(aliases)
