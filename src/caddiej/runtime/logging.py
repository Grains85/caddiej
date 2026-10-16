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
        "obs_scene_item_id",
        # Briefly revealing an existing OBS source — ProTee's Shot Video panel,
        # ProTee's Top Impact view. Which feature, what it was configured to do,
        # and which strike asked for it. The event *name* already says which
        # feature (`shot_video_source_visible`, `top_impact_source_visible`);
        # `obs_feature` says it again in a field, so one filter finds every
        # reveal in a session and a narrower one finds a single feature's.
        "obs_feature",
        "obs_reveal_delay_seconds",
        "obs_reveal_duration_seconds",
        # A reveal that cannot find its source keeps saying so, with a count,
        # rather than once at startup where nobody at the bay will read it;
        # and the capture scene is named when a reveal refuses to touch it.
        "obs_reveal_missing_count",
        "obs_capture_scene",
        "shot_trigger_id",
        # Where the coach is with a shot, as the floor is told it: which stage
        # was announced, and which one the floor is holding.
        "coaching_progress_stage",
        "floor_progress_stage",
        # How many launch-monitor detections are waiting for a simulator result
        # to claim them. Nothing to tune; it is how divergence between the two
        # looks from inside the join, and how often it happens on real
        # hardware is not yet known.
        "trigger_backlog",
        # Swing capture. Source names, timings, frame counts and a directory
        # under the media root — the numbers somebody tuning the capture on
        # real hardware has to read, and nothing else. Note what is absent: no
        # image bytes, no filename derived from anything private, and no field
        # that could carry the OBS password.
        "swing_capture_id",
        "swing_capture_sources",
        "swing_capture_source",
        "swing_capture_camera",
        "swing_capture_directory",
        "swing_capture_delay_seconds",
        "swing_capture_duration_seconds",
        "swing_capture_elapsed_seconds",
        "swing_capture_offset_seconds",
        "swing_capture_fps",
        # What a burst asked for and what it achieved, once per body replay
        # per shot (`swing_capture_cadence`).
        "swing_capture_downswing_fps",
        "swing_capture_achieved_fps",
        "swing_capture_downswing_achieved_fps",
        "swing_capture_planned_frames",
        "swing_capture_interval_mean_ms",
        "swing_capture_interval_median_ms",
        "swing_capture_interval_min_ms",
        "swing_capture_interval_max_ms",
        "swing_capture_missed_deadlines",
        "swing_capture_overruns",
        "swing_capture_request_mean_ms",
        "swing_capture_request_max_ms",
        "swing_capture_spacing_holds",
        "swing_capture_frame",
        "swing_capture_frames",
        "swing_capture_failed",
        "swing_capture_selected",
        "swing_capture_interval_start",
        "swing_capture_interval_end",
        "swing_capture_interval_detected",
        # Stills before the replay that were ProTee's loading screen, found by
        # their size: why an interval starts later than the capture did.
        "swing_capture_pre_replay_frames",
        "swing_capture_detail",
        "swing_capture_barren",
        "swing_capture_panel",
        "swing_capture_settled",
        "swing_capture_distinct_frames",
        "swing_capture_diagnostic_shots",
        "swing_capture_diagnostic_complete",
        "swing_capture_first_change_seconds",
        "swing_capture_last_change_seconds",
        "video_anchor",
        "video_anchor_age_seconds",
        "video_report_delay_seconds",
        "hud_host",
        "hud_port",
        "hud_url",
        "hud_clients",
        "hud_connects",
        "hud_dropped",
        "hud_surface",
        "hud_client_message",
        "hud_assets_version",
        "hud_assets_expected",
        "hud_assets_reload_attempt",
        "coach_frames_seen",
        # The one image a ProTee request carries: its size, how many pictures
        # it holds, and whether it was drawn and stored. Never its bytes.
        "coach_sheet_status",
        "coach_sheet_width",
        "coach_sheet_height",
        "coach_sheet_bytes",
        "coach_sheet_cells",
        "coach_sheet_dropped",
        "coach_sheet_render_ms",
        "coach_sheet_stored",
        "coach_checkpoints_scored",
        "coach_checkpoints_scores",
        "coach_checkpoints_rejected",
        "coach_checkpoints_rejections",
        "coach_checkpoints_reasons",
        "coach_checkpoints_capped",
        "coach_club_motion",
        "coach_phase_scores",
        "coach_swing_coverage",
        "coach_swing_confidence",
        "coach_swing_ceiling",
        "coach_faults",
        "coach_fault_count",
        "coach_protee_signs",
        "coach_root_fault",
        "coach_root_reason",
        "coach_root_ranking",
        "coach_root_explains",
        "coach_lesson_fault",
        "coach_lesson_cue",
        "coach_lesson_verdict",
        "coach_lesson_mode",
        "coach_lesson_score",
        "coach_lesson_baseline",
        "coach_lesson_delta",
        "coach_lesson_improved_shots",
        "coach_lesson_next",
        "hud_page_id",
        "hud_client_href",
        "floor_mode",
        "floor_state_version",
        # Round-result presentation. The raw numbers currentRound.dat carried
        # and the strings the overlay was given, so a dash on the floor can be
        # traced to whichever of the two is missing.
        "round_ball_speed_raw",
        "round_total_distance_raw",
        "round_distance_to_pin_raw",
        "round_ball_speed_display",
        "round_shot_distance_display",
        "round_remaining_distance_display",
        # Address presentation, and the two decisions behind it: which of the
        # two things the information area can say it is currently saying, where
        # the pre-shot distance came from, and which club that distance chose.
        # None of them is logged per poll; each is logged when it changes.
        "floor_information_mode",
        "floor_hitting_strip_filled",
        # The rear-edge chamfer. The device's own word, and the words and lamp
        # the strip was asked to show. Logged when that pair changes, and when
        # drawing it fails, so a dark lamp can be traced without guessing.
        "ball_status_source",
        "ball_status_label",
        "ball_status_ready",
        "ball_status_scale",
        # The floor layout, in metres on the bay. The strip anchor everything
        # golf-related hangs off, the three resolved ball positions, and a
        # warning when one of them lands off the projection. These are what a
        # tape measure is compared against when a marker looks wrong, and they
        # are logged at startup and on change rather than per poll.
        "floor_anchor_x_m",
        "floor_anchor_y_m",
        "floor_ball_iron_x_m",
        "floor_ball_iron_y_m",
        "floor_ball_driver_x_m",
        "floor_ball_driver_y_m",
        "floor_ball_putter_x_m",
        "floor_ball_putter_y_m",
        "floor_ball_x",
        "floor_ball_y",
        # Putting mode, and why a putt did or did not leave a line. The refusal
        # reason is a fixed word from `caddiej.floor.putting`, not free text.
        "floor_putting",
        "floor_putt_reason",
        "floor_putt_samples",
        "floor_putt_points",
        "lock_ack_seconds",
        "target_distance_m",
        "target_distance_source",
        "recommended_club",
        "expected_carry_m",
        "club_carry_count",
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
        "gspro_shots_deferred",
        "protee_log",
        "protee_log_offset",
        "protee_log_size",
        "protee_event",
        "observer_status",
        "observer_name",
        "observer_read_failures",
        "observer_events_dropped",
        # Lines found in a log after its name changed at midnight.
        "observer_events_recovered",
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
        "video_camera_view",
        "clip_time_quality",
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
        # Whether the line asked for a change or confirmed one, whether the
        # focus is measurably better than it was, and how many shots coaching
        # has stayed on it. Three fields, and between them the only way to tell
        # from a log whether a session felt like a lesson or like a list of
        # faults.
        "coaching_kind",
        "coaching_improving",
        "coaching_focus_shots",
        "coaching_severity",
        "coaching_display",
        "coaching_cue",
        # Candidate against final. A diagnosis names a candidate focus before
        # anything is asked; the validated line is final and is what the
        # session follows. Both on one line, so a shift from path to curve is
        # read off the log rather than reconstructed from two events.
        "coaching_stage",
        "coaching_candidate_focus",
        "coaching_candidate_cue",
        "coaching_final_focus",
        "coaching_final_cue",
        # The cue a model chose when the numbers it read did not admit it, kept
        # beside the cue CaddieJ coached instead. A registry id, never prose.
        "coaching_model_cue",
        # The observation headline and the id of the still the line cites. A
        # registry phrase and a CaddieJ-made id, never model prose or a path.
        "coaching_observed",
        "coaching_evidence_frame_id",
        # ProTee-only coaching. Ids, counts, names of metrics and how the
        # All Values still was taken; never a metric value, never image bytes.
        "coaching_trigger",
        "coaching_strike_id",
        "coaching_images",
        "coach_metrics_source",
        "coach_metrics_provenance",
        "coach_metrics_status",
        "coach_metrics_delay_seconds",
        "coach_metrics_offset_seconds",
        "coach_metrics_captured_at",
        "coach_metrics_width",
        "coach_metrics_height",
        "coach_metrics_read",
        "coach_metrics_available",
        "coach_metrics_unavailable",
        "coach_metrics_rejected",
        # What the model said it could see of the swing itself. Component
        # names, grade words and frame ids only — never the sentence it wrote
        # about them, for the same reason the readings above are ratings.
        "coach_visual_rated",
        "coach_visual_components",
        "coach_visual_frames",
        "coach_visual_rejected",
        "coach_visual_component",
        "coach_visual_frame",
        # The Swing Score. CaddieJ's own number, the rubric that made it, how
        # much weight was readable, and each component's grade and 0..1 rating.
        # Ratings rather than readings, so the rule above still holds.
        "swing_score",
        "swing_score_rubric",
        "swing_score_weight",
        "swing_score_club",
        "swing_score_components",
        "swing_score_absent",
        "coaching_confidence",
        "coaching_confidence_source",
        "evidence_grades",
        "coaching_reason",
        "coaching_detail",
        "coaching_baseline_shots",
        "coaching_frames",
        "coaching_cache_hit",
        "coaching_latency_ms",
        "coaching_backoff_seconds",
        "coaching_timeout_seconds",
        "coaching_budget_seconds",
        "coaching_attempt",
        "coaching_elapsed_seconds",
        "coaching_response_status",
        "coaching_usage_input",
        "coaching_usage_output",
        "coaching_usage_reasoning",
        "coaching_usage_total",
        "coaching_output_ceiling",
        "coaching_reasoning_effort",
        # An estimate from `adapters/openai/pricing.py`, or null when the model
        # is unpriced. Never stored and never read by coaching.
        "coaching_cost_usd",
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
        "speech_queued_lines",
        "speech_seconds",
        "speech_characters",
        "speech_detail",
        "speech_gain",
        "speech_clipped_samples",
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
