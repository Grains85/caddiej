"""Process logging that never blocks the asyncio event loop.

Application code calling `logger.*` only enqueues a record. A listener thread
owns the terminal write and the JSON formatter. If the queue fills, records are
dropped rather than pushing backpressure onto shot forwarding.
"""

from __future__ import annotations

import copy
import json
import logging
import queue
import threading
from datetime import UTC, datetime
from logging.handlers import QueueHandler, QueueListener
from typing import Any

# Enough for a burst of diagnostics. Sized so a wedged console cannot make the
# forwarding pumps wait, and so a healthy process almost never drops.
_QUEUE_SIZE = 1024
_SHUTDOWN_TIMEOUT = 2.0


class JsonFormatter(logging.Formatter):
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
        "gspro_round_file",
        "gspro_shot_id",
        "gspro_parse_failures",
        "protee_log",
        "observer_status",
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
        # Asynchronous logging. How many records the queue refused so a wedged
        # console could not stall the event loop.
        "logging_dropped",
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


class LoggingStats:
    """How many records the queue dropped because the output could not keep up."""

    def __init__(self) -> None:
        self._dropped = 0
        self._lock = threading.Lock()
        self._overflow_notice = threading.Event()

    def note_drop(self) -> None:
        with self._lock:
            self._dropped += 1
            self._overflow_notice.set()

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def consume_overflow_notice(self) -> int:
        """Return the drop count if a warning should be emitted, else 0."""

        if not self._overflow_notice.is_set():
            return 0
        with self._lock:
            self._overflow_notice.clear()
            return self._dropped


class _DroppingQueueHandler(QueueHandler):
    """Enqueue without waiting. A full queue drops the record."""

    def __init__(
        self, log_queue: queue.Queue[logging.LogRecord], stats: LoggingStats
    ) -> None:
        super().__init__(log_queue)
        self._stats = stats

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self._stats.note_drop()

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """Copy the record for the listener without formatting it.

        The base `QueueHandler.prepare` calls `format()`, which bakes
        `LEVEL:logger:message` into `msg`. The listener would then lose the
        original event name and the JSON formatter would write the wrong
        `event`. Extra attributes used by the allowlist are kept.

        `exc_info` is preserved. The queue is in-process, so the record does
        not need to be pickleable, and `JsonFormatter` formats the traceback
        on the listener thread — never on the asyncio event-loop thread that
        called `logger.exception`.
        """

        prepared = copy.copy(record)
        prepared.msg = record.getMessage()
        prepared.args = None
        prepared.exc_text = None
        prepared.stack_info = None
        return prepared

    def handleError(self, record: logging.LogRecord) -> None:
        # The base class writes a traceback to stderr, which is the I/O this
        # handler exists to keep off the event loop.
        self._stats.note_drop()


class _ReportingQueueListener(QueueListener):
    """Listener that reports overflow through the output handler, not the queue."""

    def __init__(
        self,
        log_queue: queue.Queue[logging.LogRecord],
        *handlers: logging.Handler,
        stats: LoggingStats,
    ) -> None:
        super().__init__(log_queue, *handlers, respect_handler_level=False)
        self._stats = stats

    def handle(self, record: logging.LogRecord) -> None:
        try:
            dropped = self._stats.consume_overflow_notice()
            if dropped:
                warning = logging.LogRecord(
                    name="caddiej.runtime.logging",
                    level=logging.WARNING,
                    pathname=__file__,
                    lineno=0,
                    msg="logging_queue_overflow",
                    args=(),
                    exc_info=None,
                )
                setattr(warning, "logging_dropped", dropped)
                super().handle(warning)
            super().handle(record)
        except Exception:
            # A failing output handler must not kill the listener thread.
            self._stats.note_drop()


class LoggingRuntime:
    """Owns the listener thread and the bounded queue."""

    def __init__(
        self,
        listener: _ReportingQueueListener,
        stats: LoggingStats,
        thread: threading.Thread,
    ) -> None:
        self._listener = listener
        self._stats = stats
        self._thread = thread
        self._stopped = False

    @property
    def dropped(self) -> int:
        return self._stats.dropped

    def stop(self, timeout: float = _SHUTDOWN_TIMEOUT) -> None:
        """Flush what we can, then abandon the listener if it will not finish.

        `QueueListener.stop()` joins the listener thread with no timeout. A
        blocked console write would then hang CaddieJ. The join here is
        bounded; remaining records are dropped and the daemon thread is left
        behind for process exit.
        """

        if self._stopped:
            return
        self._stopped = True
        try:
            self._listener.enqueue_sentinel()
        except Exception:
            self._stats.note_drop()
        self._thread.join(timeout)
        if self._thread.is_alive():
            # The output handler is still blocked. Do not wait. The thread is
            # a daemon, so it cannot keep the process alive.
            return
        self._listener._thread = None  # noqa: SLF001 — listener's own bookkeeping


def configure_logging(
    level: str = "INFO",
    *,
    queue_size: int = _QUEUE_SIZE,
    output: logging.Handler | None = None,
) -> LoggingRuntime:
    """Install queue-backed logging. Application `logger.*` calls do no I/O."""

    stats = LoggingStats()
    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=queue_size)
    queue_handler = _DroppingQueueHandler(log_queue, stats)
    queue_handler.setLevel(logging.NOTSET)

    stream = output if output is not None else logging.StreamHandler()
    stream.setFormatter(JsonFormatter())
    stream.setLevel(logging.NOTSET)

    listener = _ReportingQueueListener(log_queue, stream, stats=stats)
    listener.start()
    # QueueListener.start() creates a daemon thread. Do not set daemon after
    # start: that raises, and the thread is already a daemon.
    thread = listener._thread  # noqa: SLF001 — start() created it
    if thread is None:  # pragma: no cover - start() always assigns a thread
        raise RuntimeError("logging listener failed to start")

    logging.basicConfig(level=level, handlers=[queue_handler], force=True)
    return LoggingRuntime(listener, stats, thread)


def shutdown_logging(
    runtime: LoggingRuntime | None, timeout: float = _SHUTDOWN_TIMEOUT
) -> None:
    """Stop the listener if one was started. Always returns within `timeout`."""

    if runtime is None:
        return
    runtime.stop(timeout)
