"""Command-line entry point for CaddieJ."""

import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from caddiej.app import run
from caddiej.cli import (
    build_parser,
    describe,
    explain,
    load_settings,
    resolve_log_level,
)


class _JsonFormatter(logging.Formatter):
    """Small dependency-free JSON formatter for application logs."""

    # An explicit allowlist: the formatter fails closed so a future caller cannot
    # leak a credential into logs by passing it through `extra`.
    _CONTEXT_FIELDS = (
        "event_type",
        "obs_host",
        "obs_port",
        "obs_version",
        "obs_websocket_version",
        "obs_rpc_version",
        "obs_scene",
        "obs_source",
        "obs_created",
        "hud_host",
        "hud_port",
        "hud_url",
        "hud_clients",
        "hud_dropped",
        "hud_surface",
        "hud_client_message",
        "floor_mode",
        "floor_state_version",
        "floor_demo_step",
        "floor_demo_steps",
        "hud_client_origin",
        "task_name",
        # Shutdown. How long it was given, and how much was still queued when
        # something ran out of patience. Numbers, and how deep a queue was.
        "shutdown_seconds",
        "queue_depth",
        "subscriber",
        # GSPro adapter. Shot numbers and device ids are diagnostics, not
        # secrets; nothing on this path carries a credential.
        "gspro_link_state",
        "gspro_detail",
        "gspro_device_id",
        "gspro_sequence",
        "gspro_direction",
        "gspro_dropped",
        "gspro_frame_bytes",
        "gspro_reject_reason",
        "gspro_response_code",
        "gspro_backoff_seconds",
        "gspro_ball_speed_mph",
        "gspro_club",
        "gspro_club_code",
        "gspro_handedness",
        "gspro_ready",
        "gspro_peer",
        "gspro_bind",
        "gspro_upstream",
        "gspro_session",
        # Local history. A database path is not a credential, and no shot
        # measurement is either.
        "history_path",
        "history_schema_version",
        "history_session",
        "history_recorded",
        "history_batch",
        "history_queued",
        "history_dropped",
        "shot_id",
        # Swing video. A camera name and a path under the media root are
        # diagnostics; nothing on this path carries a credential, and no frame
        # or filename is derived from one.
        "camera_id",
        "video_camera",
        "video_detail",
        "video_root",
        "video_recycled",
        "video_restarts",
        "video_backoff_seconds",
        "video_queued",
        "video_dropped",
        "clip_id",
        "clip_path",
        "clip_reason",
        "clip_duration",
        "clip_offset",
        "video_frames",
        "ffmpeg_exit",
        # Coaching. A fault name, a severity and a two-word display are
        # diagnostics. Note what is absent: no prompt, no evidence block, no
        # coached sentence, no API key and no field that could carry one. A log
        # is the easiest place to leak a credential, so this list stays a list of
        # labels and numbers.
        "coaching_provider",
        "coaching_model",
        "coaching_focus",
        "coaching_severity",
        "coaching_display",
        "coaching_reason",
        "coaching_detail",
        "coaching_baseline_shots",
        "coaching_frames",
        "coaching_cache_hit",
        "coaching_latency_ms",
        "coaching_backoff_seconds",
        "coaching_queued",
        # Speech. An engine name, a voice name and a duration. Note what is
        # absent: the sentence itself. It is a coaching line rather than a
        # secret, but a log that quotes everything CaddieJ says is a transcript
        # of somebody's practice session, and nothing needs one.
        "speech_engine",
        "speech_output",
        "speech_voice",
        "speech_language",
        "speech_utterance",
        "speech_priority",
        "speech_seconds",
        "speech_characters",
        "speech_detail",
        # Lighting. A bridge address, a lamp count and a brightness fraction.
        # Note what is absent: the application key, and any field that could
        # carry it. A bridge key is a credential in exactly the way an API key
        # is, so this list stays a list of labels and numbers.
        "lighting_output",
        "lighting_bridge",
        "lighting_lights",
        "lighting_named",
        "lighting_missing",
        "lighting_cue",
        "lighting_minimum",
        "lighting_maximum",
        "lighting_cooldown_seconds",
        # Effects. A cue name from a fixed vocabulary, which output it went to,
        # and how long for. Note what is absent: the label. It is authored in the
        # catalogue rather than by anybody, so it is not sensitive — it is simply
        # a restatement of the cue, and a log line that says both is a log line
        # that says one thing twice.
        "effect_cue",
        "effect_playing",
        "effect_priority",
        "effect_seconds",
        "effect_channel",
        "effect_phase",
        # Assistant boundary. A tool name, a refusal reason from a fixed set,
        # and a loopback address. Note what is absent and must stay absent: the
        # bearer token, any header, any request body, and any answer. A log of
        # what an assistant asked and what it was told would be both a
        # credential risk and a transcript of somebody's practice, and the
        # refusal reason alone is what an operator needs to fix a broken setup.
        "assistant_tool",
        "assistant_refusal",
        "assistant_changed",
        "assistant_host",
        "assistant_port",
        "assistant_url",
        "assistant_mutations",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for field in self._CONTEXT_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


def main(argv: Sequence[str] | None = None) -> None:
    """Run CaddieJ from either the module or console entry point.

    Configuration is read here rather than inside the application, so that a
    mistyped setting is a sentence on stderr and an exit code, instead of a
    traceback wrapped in a log line that nobody can read at the moment they most
    need to.
    """

    options = build_parser().parse_args(argv)
    _configure_logging(resolve_log_level(options.log_level))
    logger = logging.getLogger(__name__)

    try:
        settings = load_settings()
    except ValidationError as error:
        print(explain(error), file=sys.stderr)
        raise SystemExit(2) from None

    if options.check_config:
        print(describe(settings))
        return

    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("shutdown_requested")
        raise SystemExit(130) from None
    except Exception:
        logger.exception("application_failed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
