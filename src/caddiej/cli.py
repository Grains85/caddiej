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
        f"  Speech               {_speech(settings)}",
        f"  Lighting             {_lighting(settings)}",
        f"  Celebrations         {_effects(settings)}",
        f"  Assistant API        {_assistant(settings)}",
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
    if settings.coaching_provider == "none":
        return "on, measured faults only (COACHING_PROVIDER=none)"
    if settings.coaching_provider == "scripted":
        return "on, scripted provider (no account, no network)"
    key = "set" if settings.coaching_api_key.get_secret_value() else "NOT SET"
    return (
        f"on, {settings.coaching_provider} {settings.coaching_model} "
        f"(COACHING_API_KEY {key})"
    )


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
