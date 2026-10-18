"""Collect one shot's facts and write them off the coaching path.

Callers enqueue a note and return. A background thread writes the report.
A failure here is logged and swallowed so a shot can still be coached.
"""

from __future__ import annotations

import copy
import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from caddiej.diagnostics.decision import explain_checkpoints
from caddiej.diagnostics.export import export_latest_shot
from caddiej.diagnostics.report import build_shot_report, render_shot_markdown
from caddiej.diagnostics.session import build_session_summary, render_session_markdown
from caddiej.diagnostics.sheet import render_contact_sheet
from caddiej.diagnostics.store import (
    bounded_json,
    copy_inside,
    remove_tree,
    safe_segment,
    session_dir,
    sha256_text,
    shot_dir,
    write_json,
    write_text,
)

logger = logging.getLogger(__name__)

_BALL_FIELDS = (
    "speed_mph",
    "launch_angle_degrees",
    "launch_direction_degrees",
    "total_spin_rpm",
    "spin_axis_degrees",
    "back_spin_rpm",
    "side_spin_rpm",
    "carry_yards",
)
_CLUB_FIELDS = (
    "speed_mph",
    "speed_at_impact_mph",
    "angle_of_attack_degrees",
    "path_degrees",
    "face_to_target_degrees",
    "loft_degrees",
    "lie_degrees",
    "closure_rate_degrees_per_second",
    "impact_horizontal_mm",
    "impact_vertical_mm",
)


class DiagnosticJournal:
    """In-memory shot facts and the files written from them."""

    def __init__(
        self,
        root: Path | str,
        *,
        session_id: str,
        enabled: bool = True,
        keep_sessions: int = 1,
        keep_shots: int = 3,
        retain_payloads: bool = True,
        payload_max_bytes: int = 1_500_000,
        auto_export: bool = True,
        include_images: bool = True,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._root = Path(root)
        self._session_id = str(session_id)
        self._enabled = enabled
        self._keep_sessions = max(1, keep_sessions)
        self._keep_shots = max(1, keep_shots)
        self._retain = retain_payloads
        self._payload_max_bytes = payload_max_bytes
        self._auto_export = auto_export
        self._include_images = include_images
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._shots: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._rollup: dict[str, dict[str, Any]] = {}
        self._rollup_order: list[str] = []
        self._next_sequence = 0
        self._exported_sequence = -1
        self._pending_removals: list[Path] = []
        self._by_trigger: dict[str, str] = {}
        self._pending_measurements: dict[str, dict[str, Any]] = {}
        self._pending_outcomes: dict[str, dict[str, Any]] = {}
        self._dirty: set[str] = set()
        self._pending = 0
        self._idle = threading.Event()
        self._idle.set()
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=256)
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._session_id

    def bind_session(self, session_id: str) -> None:
        """Use the history session id once it exists, before any shot is filed."""

        if not self._enabled:
            return
        with self._lock:
            if self._order:
                return
            self._session_id = str(session_id)

    def note_strike(
        self,
        *,
        strike_id: str,
        trigger_id: str,
        monotonic_seconds: float | None,
        occurred_at: datetime | None,
    ) -> None:
        """The launch-monitor strike. `strike_id` is the coaching shot id."""

        def mutate(facts: dict[str, Any]) -> None:
            facts["strike_id"] = strike_id
            facts["trigger_id"] = trigger_id or facts.get("trigger_id")
            _relate(facts, "strike_id", strike_id, "ShotDetected.event_id")
            _relate(facts, "trigger_id", trigger_id, "ShotDetected.trigger_id")
            _mark(facts, "shot_detected", monotonic_seconds, occurred_at)
            if trigger_id:
                self._by_trigger[str(trigger_id)] = facts["shot_id"]
                self._apply_pending(facts, str(trigger_id))

        self._update(str(strike_id), mutate)

    def note_simulator_shot(
        self,
        shot: Any,
        trigger_id: str | None,
        *,
        monotonic_seconds: float | None,
        occurred_at: datetime | None,
    ) -> None:
        """GSPro's shot, joined onto the coaching shot by `trigger_id` when one exists."""

        if not self._enabled or self._closed:
            return
        try:
            payload = _measurement_payload(shot)
            with self._lock:
                key = self._by_trigger.get(str(trigger_id)) if trigger_id else None
                if key is None and trigger_id:
                    self._pending_measurements[str(trigger_id)] = {
                        "payload": payload,
                        "monotonic_seconds": monotonic_seconds,
                        "occurred_at": occurred_at,
                    }
                    return
                if key is None:
                    key = str(shot.shot_id)
                facts = self._ensure(key)
                _apply_measurements(facts, payload)
                _mark(facts, "shot_completed", monotonic_seconds, occurred_at)
                if trigger_id:
                    self._by_trigger[str(trigger_id)] = key
            self._kick(key)
        except Exception:
            logger.exception("diagnostics_note_failed")

    def note_capture(self, capture: Any) -> None:
        """A published swing capture. Filed under its coaching id."""

        def mutate(facts: dict[str, Any]) -> None:
            shot_id = str(capture.coaching_id)
            facts["shot_id"] = shot_id
            facts["capture_id"] = str(capture.capture_id)
            trigger = str(getattr(capture, "trigger_id", "") or "")
            strike = getattr(capture, "strike_id", None)
            facts["trigger_id"] = trigger or facts.get("trigger_id")
            if strike is not None:
                facts["strike_id"] = str(strike)
            _relate(facts, "capture_id", facts["capture_id"], "SwingCapture.capture_id")
            _relate(facts, "coaching_shot_id", shot_id, "SwingCapture.coaching_id")
            _relate(facts, "trigger_id", trigger, "SwingCapture.trigger_id")
            cameras = [_camera(camera) for camera in capture.cameras]
            missing = [
                camera for camera in cameras if camera["source"] and not camera["ok"]
            ]
            if not cameras:
                status, detail = "failed", "no cameras were recorded"
            elif missing:
                names = ", ".join(camera["camera_id"] for camera in missing)
                status, detail = "failed", f"missing camera: {names}"
            else:
                status, detail = "success", ""
            started = getattr(capture, "started_at", None)
            detected = getattr(capture, "detected_at", None)
            facts["capture"] = {
                "status": status,
                "detail": detail,
                "cameras": cameras,
                "frame_count": sum(camera["kept"] for camera in cameras),
                "captured_count": sum(camera["captured"] for camera in cameras),
                "started_at": None if started is None else started.isoformat(),
                "detected_at": None if detected is None else detected.isoformat(),
                "directory": str(capture.directory),
            }
            if started is not None:
                facts["milestones"]["capture_started"] = {
                    "utc": started.isoformat(),
                    "monotonic_seconds": None,
                    "quality": "wall_clock",
                }
            if missing:
                _warn(facts, facts["capture"]["detail"])
            if trigger:
                self._by_trigger[trigger] = shot_id
                self._apply_pending(facts, trigger)

        coaching_id = str(capture.coaching_id)
        self._update(coaching_id, mutate)

    def note_capture_outcome(
        self,
        *,
        status: str,
        reason: str,
        trigger_id: str = "",
        strike_id: str | None = None,
        capture_id: str | None = None,
    ) -> None:
        """A capture that was cancelled, superseded, or failed before it was published."""

        if not self._enabled or self._closed:
            return
        try:
            with self._lock:
                key = str(strike_id) if strike_id else None
                if key is None and trigger_id:
                    key = self._by_trigger.get(str(trigger_id))
                if key is None and trigger_id:
                    self._pending_outcomes[str(trigger_id)] = {
                        "status": status,
                        "reason": reason,
                        "capture_id": capture_id,
                    }
                    if capture_id is None:
                        return
                    key = str(capture_id)
                if key is None:
                    return
                facts = self._ensure(key)
                if trigger_id:
                    self._by_trigger[str(trigger_id)] = key
                    facts["trigger_id"] = trigger_id
                if capture_id and not facts.get("capture_id"):
                    facts["capture_id"] = str(capture_id)
                    _relate(facts, "capture_id", capture_id, "capture outcome")
                if facts.get("capture") is None:
                    facts["capture"] = {
                        "status": status,
                        "detail": reason,
                        "cameras": [],
                        "frame_count": 0,
                        "captured_count": 0,
                        "started_at": None,
                        "detected_at": None,
                        "directory": None,
                    }
                _terminal(facts, status, reason)
            self._kick(key)
        except Exception:
            logger.exception("diagnostics_note_failed")

    def note_evidence(self, shot_id: str, package: Any, directory: Path) -> None:
        def mutate(facts: dict[str, Any]) -> None:
            frames = _frames(package, Path(directory))
            stages = _stages(package)
            impact = _impact(package.impact_estimate)
            package_status = str(getattr(package, "status", "") or "")
            errors = [str(item) for item in getattr(package, "errors", ()) or ()]
            warnings = [str(item) for item in getattr(package, "warnings", ()) or ()]
            if package_status == "failed" or errors:
                status = "failed"
            elif package_status == "insufficient_evidence":
                status = "failed"
            elif package_status in {"ok", "partial"} and frames:
                status = "success"
            elif package_status == "partial":
                status = "failed"
            else:
                status = "unavailable"
            detail = str(getattr(package, "fallback", "") or "")
            if package_status == "partial" and status == "success":
                warnings = [*warnings, "evidence package status is partial"]
            facts["evidence"] = {
                "status": status,
                "detail": detail,
                "package_status": package_status,
                "selected_frames": frames,
                "stages": stages,
                "impact": impact,
                "warnings": warnings,
                "errors": errors,
                "directory": str(directory),
            }
            for warning in warnings:
                _warn(facts, warning)
            for error in errors:
                _error(facts, error)
            if not facts.get("capture_id"):
                facts["capture"] = facts.get("capture")

        self._update(str(shot_id), mutate)

    def note_evidence_timing(
        self, shot_id: str, timings: dict[str, float | None]
    ) -> None:
        def mutate(facts: dict[str, Any]) -> None:
            for key, seconds in timings.items():
                if seconds is None:
                    continue
                name = key[:-8] + "_ms" if key.endswith("_seconds") else key
                facts["evidence_timing_ms"][name] = int(round(float(seconds) * 1000))

        self._update(str(shot_id), mutate)

    def note_gpt(
        self,
        shot_id: str,
        *,
        prepared: Any,
        reply: Any,
        provider: str | None,
        simulated: bool,
        checkpoints: list[str],
        image_detail: str | None = None,
        error: str | None = None,
    ) -> None:
        def mutate(facts: dict[str, Any]) -> None:
            images = _request_images(prepared)
            facts["simulated_provider"] = simulated
            model = ""
            if reply is not None and getattr(reply, "model", None):
                model = str(reply.model)
            elif provider:
                model = ""
            facts["gpt"] = {
                "status": "failed" if error else "success",
                "detail": error or "",
                "provider": provider,
                "model": model or None,
                "image_detail": image_detail
                or (
                    getattr(prepared, "image_detail", None)
                    if prepared is not None
                    else None
                ),
                "image_count": len(images),
                "images": images,
                "reference_version": getattr(prepared, "reference_version", None)
                if prepared is not None
                else None,
                "checkpoints_requested": list(checkpoints),
                "input_tokens": getattr(reply, "input_tokens", None) if reply else None,
                "output_tokens": getattr(reply, "output_tokens", None)
                if reply
                else None,
                "cached_tokens": getattr(reply, "cached_tokens", None)
                if reply
                else None,
                "provider_reported_latency_seconds": (
                    None if reply is None else getattr(reply, "latency_seconds", None)
                ),
                "provider_latency_quality": (
                    None
                    if reply is None or getattr(reply, "latency_seconds", None) is None
                    else ("estimated" if simulated else "measured")
                ),
                "validation_status": "skipped" if error else "unavailable",
                "validation_errors": [error] if error else [],
                "recovery": [],
                "accepted_findings": [],
                "rejected_findings": [],
                "parsed_assessment": None,
            }
            if prepared is not None and not facts["gpt"]["image_detail"]:
                facts["gpt"]["image_detail"] = _image_detail(prepared)
            if error:
                _error(facts, error)
            if self._retain and prepared is not None:
                facts["payload_request"] = bounded_json(
                    _request_payload(prepared, provider, checkpoints),
                    self._payload_max_bytes,
                )
            if self._retain and reply is not None:
                facts["payload_response"] = bounded_json(
                    dict(getattr(reply, "payload", {}) or {}),
                    self._payload_max_bytes,
                )

        self._update(str(shot_id), mutate)

    def note_assessment(
        self,
        shot_id: str,
        *,
        result: Any,
        approved_correction: str,
        previous_focus: str | None,
        previous: Any,
        reference: Any,
        observation: str,
        observation_included: bool,
    ) -> None:
        def mutate(facts: dict[str, Any]) -> None:
            assessment = (
                result.assessment if isinstance(result.assessment, dict) else None
            )
            findings = {
                str(item["checkpoint_id"]): item
                for item in (assessment or {}).get("findings") or []
                if isinstance(item, dict) and item.get("checkpoint_id")
            }
            ranking = (
                explain_checkpoints(reference, findings)
                if reference is not None and findings
                else None
            )
            errors = [str(item) for item in result.errors]
            accepted = result.decision_status == "ok" and not errors
            rows = list((ranking or {}).get("checkpoints") or [])
            if accepted:
                accepted_rows = [row for row in rows if row.get("eligible")]
                rejected_rows = [row for row in rows if not row.get("eligible")]
                validation = "success"
            else:
                accepted_rows = []
                reason = errors[0] if errors else "assessment was not accepted"
                rejected_rows = [{**row, "reason": reason} for row in rows]
                validation = (
                    "failed" if assessment is not None or errors else "unavailable"
                )
            gpt = facts.get("gpt") or {}
            gpt["validation_status"] = validation
            gpt["validation_errors"] = errors
            gpt["recovery"] = [str(item) for item in result.recovery]
            gpt["accepted_findings"] = accepted_rows
            gpt["rejected_findings"] = rejected_rows
            gpt["parsed_assessment"] = assessment
            if "status" not in gpt:
                gpt["status"] = "success" if assessment is not None else "unavailable"
            facts["gpt"] = gpt
            for error in errors:
                _error(facts, error)
            delivered_previous = (
                previous is not None
                and getattr(previous, "delivery_status", None) == "delivered"
            )
            progress = getattr(result.state, "progress", None)
            facts["decision"] = {
                "status": "success" if result.decision_status == "ok" else "failed",
                "detail": str(result.reason or ""),
                "decision_type": result.decision_type,
                "decision_status": result.decision_status,
                "reason": result.reason,
                "selected_checkpoint": result.active_focus,
                "previous_focus": (
                    getattr(previous, "active_focus", None)
                    if previous is not None
                    else previous_focus
                ),
                "previous_delivery_status": (
                    None
                    if previous is None
                    else getattr(previous, "delivery_status", None)
                ),
                "previous_cue": (
                    getattr(previous, "previous_cue", None)
                    if delivered_previous
                    else None
                ),
                "progress": progress,
                "rule_id": result.rule_id,
                "approved_correction": approved_correction,
                "validated_observation": observation if observation_included else "",
                "observation_included": observation_included,
                "hud_text": result.hud_text,
                "speech_text": result.speech_text,
                "ranking": ranking,
            }
            if _locked(facts):
                return
            if result.decision_status == "ok":
                _terminal(facts, "success", str(result.reason or "assessment accepted"))
            else:
                _terminal(
                    facts,
                    "failed",
                    errors[0] if errors else str(result.reason or "no coaching line"),
                )

        self._update(str(shot_id), mutate)

    def note_failure(self, shot_id: str, status: str, reason: str) -> None:
        def mutate(facts: dict[str, Any]) -> None:
            _terminal(facts, status, reason, force=True)
            if status == "failed":
                _error(facts, reason)
            else:
                _warn(facts, reason)
            if facts.get("gpt") is None:
                facts["gpt"] = _empty_gpt(
                    "cancelled" if status in {"superseded", "cancelled"} else "skipped",
                    reason,
                )
            if facts.get("decision") is None:
                facts["decision"] = {
                    "status": "cancelled"
                    if status in {"superseded", "cancelled"}
                    else "skipped",
                    "detail": reason,
                    "decision_type": None,
                    "reason": reason,
                    "selected_checkpoint": None,
                    "hud_text": "",
                    "speech_text": "",
                    "ranking": None,
                }

        self._update(str(shot_id), mutate)

    def note_trace(self, trace: Any) -> None:
        def mutate(facts: dict[str, Any]) -> None:
            facts["simulated_provider"] = bool(getattr(trace, "simulated", False))
            for name, at in getattr(trace, "marks", {}).items():
                facts["milestones"].setdefault(
                    name,
                    {
                        "monotonic_seconds": at,
                        "utc": None,
                        "quality": "measured",
                    },
                )
            if getattr(trace, "capture_id", ""):
                facts["capture_id"] = trace.capture_id
            if getattr(trace, "errors", ()):
                for error in trace.errors:
                    _error(facts, str(error))

        self._update(str(trace.shot_id), mutate)

    def note_hud(self, shot_id: str, status: str, text: str, detail: str) -> None:
        def mutate(facts: dict[str, Any]) -> None:
            facts["hud"] = {"status": status, "text": text, "detail": detail}
            if status == "success":
                _mark(facts, "hud_shown", self._clock(), datetime.now(UTC))

        self._update(str(shot_id), mutate)

    def note_tts(
        self,
        shot_id: str | None,
        *,
        status: str,
        detail: str,
        monotonic_seconds: float | None,
    ) -> None:
        if shot_id is None:
            return

        def mutate(facts: dict[str, Any]) -> None:
            facts["tts_request"] = {"status": status, "detail": detail}
            if status == "success":
                _mark(facts, "tts_requested", monotonic_seconds, None)

        self._update(str(shot_id), mutate)

    def note_playback_started(self, shot_id: str | None) -> None:
        if shot_id is None:
            return

        def mutate(facts: dict[str, Any]) -> None:
            facts["playback_started"] = True
            facts["milestones"].setdefault(
                "tts_started",
                {
                    "monotonic_seconds": self._clock(),
                    "utc": datetime.now(UTC).isoformat(),
                    "quality": "measured",
                    "observed": True,
                },
            )

        self._update(str(shot_id), mutate)

    def note_playback_finished(
        self,
        shot_id: str | None,
        *,
        outcome: str,
        spoken_seconds: float,
        detail: str | None,
    ) -> None:
        if shot_id is None:
            return

        def mutate(facts: dict[str, Any]) -> None:
            started = bool(facts.get("playback_started")) or _has_mark(
                facts, "tts_started"
            )
            if outcome == "spoken" and started:
                status = "success"
                text = detail or "playback completed"
                seconds: float | None = spoken_seconds
            elif outcome == "spoken":
                status = "unavailable"
                text = "completion reported spoken audio without a playback start event"
                seconds = None
            elif outcome == "superseded":
                status = "superseded"
                text = detail or "speech was superseded"
                seconds = None
            elif outcome == "dropped":
                status = "failed"
                text = detail or "the speech queue dropped the line"
                seconds = None
            elif outcome == "failed":
                status = "failed"
                text = detail or "speech failed"
                seconds = None
            else:
                status = "unavailable"
                text = detail or outcome
                seconds = None
            facts["tts_playback"] = {
                "status": status,
                "outcome": outcome,
                "spoken_seconds": seconds,
                "detail": text,
            }
            if status == "failed":
                _error(facts, text)

        self._update(str(shot_id), mutate)

    def flush(self, timeout: float = 10.0) -> None:
        """Wait until queued writes have finished. Used by tests and export."""

        if not self._enabled:
            return
        if not self._idle.wait(timeout):
            logger.warning(
                "diagnostics_flush_timed_out",
                extra={"diagnostics_session": self._session_id},
            )
        try:
            self._write_dirty()
        except Exception:
            logger.exception("diagnostics_write_failed")

    def close(self) -> None:
        """Finish reports for shots still open, then stop the writer."""

        if not self._enabled or self._closed:
            self._closed = True
            return
        self._closed = True
        thread = self._thread
        if thread is not None:
            try:
                self._queue.put(None, timeout=1)
            except queue.Full:
                pass
            thread.join(timeout=5)
        try:
            self._finalize_open_shots()
            self._write_dirty()
            self._prune()
        except Exception:
            logger.exception("diagnostics_close_failed")

    def _update(self, shot_id: str, mutate: Callable[[dict[str, Any]], None]) -> None:
        if not self._enabled or self._closed or not shot_id:
            return
        try:
            with self._lock:
                facts = self._ensure(shot_id)
                mutate(facts)
            self._kick(shot_id)
        except Exception:
            logger.exception("diagnostics_note_failed")

    def _ensure(self, shot_id: str) -> dict[str, Any]:
        facts = self._shots.get(shot_id)
        if facts is None:
            self._next_sequence += 1
            facts = _blank(shot_id, self._session_id)
            facts["sequence"] = self._next_sequence
            self._shots[shot_id] = facts
            self._order.append(shot_id)
            self._rollup_order.append(shot_id)
        return facts

    def _apply_pending(self, facts: dict[str, Any], trigger_id: str) -> None:
        measurement = self._pending_measurements.pop(trigger_id, None)
        if measurement is not None:
            _apply_measurements(facts, measurement["payload"])
            _mark(
                facts,
                "shot_completed",
                measurement.get("monotonic_seconds"),
                measurement.get("occurred_at"),
            )
        outcome = self._pending_outcomes.pop(trigger_id, None)
        if outcome is not None and facts.get("capture") is None:
            facts["capture"] = {
                "status": outcome["status"],
                "detail": outcome["reason"],
                "cameras": [],
                "frame_count": 0,
                "captured_count": 0,
                "started_at": None,
                "detected_at": None,
                "directory": None,
            }
            if outcome.get("capture_id") and not facts.get("capture_id"):
                facts["capture_id"] = str(outcome["capture_id"])
            _terminal(facts, str(outcome["status"]), str(outcome["reason"]))

    def _kick(self, shot_id: str) -> None:
        with self._lock:
            self._dirty.add(shot_id)
            self._pending += 1
            self._idle.clear()
        self._ensure_thread()
        try:
            self._queue.put_nowait(shot_id)
        except queue.Full:
            with self._lock:
                self._pending = max(0, self._pending - 1)
                if self._pending == 0:
                    self._idle.set()
            logger.warning(
                "diagnostics_write_deferred",
                extra={
                    "shot_id": shot_id,
                    "diagnostics_session": self._session_id,
                },
            )

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        thread = threading.Thread(
            target=self._run,
            name="caddiej-diagnostics",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._write_dirty()
            except Exception:
                logger.exception("diagnostics_write_failed")
            finally:
                with self._lock:
                    self._pending = max(0, self._pending - 1)
                    if self._pending == 0:
                        self._idle.set()

    def _write_dirty(self) -> None:
        with self._lock:
            shot_ids = list(self._dirty)
            self._dirty.clear()
        for shot_id in shot_ids:
            self._write_shot(shot_id)
        self._prune()
        self._write_session()
        self._export_newest(shot_ids)

    def _write_shot(self, shot_id: str) -> None:
        with self._lock:
            facts = self._shots.get(shot_id)
            if facts is None:
                return
            snapshot = copy.deepcopy(facts)
        self._maybe_sheet(shot_id, snapshot)
        self._retain_images(shot_id, snapshot)
        with self._lock:
            current = self._shots.get(shot_id)
            if current is not None and snapshot.get("contact_sheet"):
                current["contact_sheet"] = snapshot["contact_sheet"]
                for warning in snapshot.get("warnings") or []:
                    _warn(current, str(warning))
            if (
                current is not None
                and isinstance(current.get("evidence"), dict)
                and snapshot.get("evidence")
            ):
                current["evidence"]["selected_frames"] = snapshot["evidence"].get(
                    "selected_frames"
                )
                current["evidence"]["request_only_images"] = snapshot["evidence"].get(
                    "request_only_images"
                )
            if current is not None and snapshot.get("payload_request") is not None:
                current["payload_request"] = snapshot["payload_request"]
            if current is not None:
                snapshot = copy.deepcopy(current)
        report = build_shot_report(snapshot)
        directory = shot_dir(self._root, snapshot["session_id"], shot_id)
        try:
            write_json(directory / "report.json", report)
            write_text(directory / "report.md", render_shot_markdown(report))
            _write_payloads(directory, snapshot)
        except OSError:
            logger.warning(
                "diagnostics_report_not_written",
                extra={
                    "shot_id": shot_id,
                    "diagnostics_session": str(snapshot.get("session_id")),
                    "diagnostics_status": str(report.get("status")),
                },
            )
            return
        with self._lock:
            self._rollup[shot_id] = _compact_report(report)
        logger.info(
            "diagnostics_report_written",
            extra={
                "shot_id": shot_id,
                "diagnostics_session": str(snapshot.get("session_id")),
                "diagnostics_path": str(directory / "report.json"),
                "diagnostics_status": str(report.get("status")),
            },
        )

    def _maybe_sheet(self, shot_id: str, snapshot: dict[str, Any]) -> None:
        existing = snapshot.get("contact_sheet") or {}
        if existing.get("status") in {"success", "failed", "skipped"}:
            return
        evidence = snapshot.get("evidence") or {}
        frames = list(evidence.get("selected_frames") or [])
        if not frames:
            if evidence.get("status") in {
                "failed",
                "skipped",
                "cancelled",
                "unavailable",
            }:
                snapshot["contact_sheet"] = {
                    "status": "skipped",
                    "file": None,
                    "detail": "no selected frames",
                }
            return
        destination = (
            shot_dir(self._root, snapshot["session_id"], shot_id) / "contact_sheet.jpg"
        )
        try:
            render_contact_sheet(frames, evidence.get("impact"), destination)
        except Exception as error:
            snapshot["contact_sheet"] = {
                "status": "failed" if not _missing_pillow(error) else "skipped",
                "file": None,
                "detail": f"{type(error).__name__}: {error}"[:300],
            }
            snapshot.setdefault("warnings", []).append(
                f"contact sheet was not written: {snapshot['contact_sheet']['detail']}"
            )
            return
        snapshot["contact_sheet"] = {
            "status": "success",
            "file": "contact_sheet.jpg",
            "detail": "",
        }

    def _write_session(self) -> None:
        with self._lock:
            if not self._rollup:
                return
            session_id = self._session_id
            reports = [
                self._rollup[shot_id]
                for shot_id in self._rollup_order
                if shot_id in self._rollup
            ]
            retained = [shot_id for shot_id in self._order if shot_id in self._shots]
        summary = build_session_summary(session_id, reports)
        summary["retained_shots"] = len(retained)
        summary["retained_shot_ids"] = list(retained)
        directory = session_dir(self._root, session_id)
        try:
            write_json(directory / "session.json", summary)
            write_text(directory / "summary.md", render_session_markdown(summary))
        except OSError:
            logger.warning(
                "diagnostics_session_not_written",
                extra={"diagnostics_session": session_id},
            )

    def _finalize_open_shots(self) -> None:
        with self._lock:
            for shot_id in self._order:
                facts = self._shots.get(shot_id)
                if facts is None or facts.get("terminal"):
                    continue
                if facts.get("capture") or facts.get("evidence") or facts.get("gpt"):
                    _terminal(
                        facts,
                        "unavailable",
                        "session ended before this shot finished",
                    )
                    _warn(facts, "session ended before this shot finished")
                else:
                    _terminal(facts, "skipped", "coaching did not assess this shot")
                self._dirty.add(shot_id)

    def _prune(self) -> None:
        self._retry_removals()
        with self._lock:
            overflow = (
                self._order[: -self._keep_shots]
                if len(self._order) > self._keep_shots
                else []
            )
            for shot_id in overflow:
                self._shots.pop(shot_id, None)
            if overflow:
                self._order = self._order[len(overflow) :]
            session_id = self._session_id
        for shot_id in overflow:
            self._delete_owned(shot_dir(self._root, session_id, shot_id))
        if not self._root.is_dir():
            return
        current = session_dir(self._root, session_id)
        others: list[Path] = []
        for path in self._root.iterdir():
            if not path.is_dir() or path.name in {"exports"}:
                continue
            try:
                if path.resolve() == current.resolve():
                    continue
            except OSError:
                continue
            if (path / "session.json").is_file() or (path / "shots").is_dir():
                others.append(path)
        others.sort(key=_mtime)
        allowed = max(0, self._keep_sessions - 1)
        doomed = others if allowed == 0 else others[: max(0, len(others) - allowed)]
        for path in doomed:
            self._delete_owned(path)
        self._cleanup_temp_zips()

    def _delete_owned(self, path: Path) -> None:
        """Delete a diagnostic directory. Capture files live outside this root."""

        if remove_tree(path, self._root):
            return
        if path.exists():
            self._remember_removal(path)

    def _remember_removal(self, path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved not in self._pending_removals:
            self._pending_removals.append(resolved)
            logger.warning(
                "diagnostics_cleanup_locked",
                extra={
                    "diagnostics_session": self._session_id,
                    "diagnostics_path": str(path),
                    "diagnostics_status": "failed",
                },
            )

    def _retry_removals(self) -> None:
        pending = list(self._pending_removals)
        self._pending_removals = []
        for path in pending:
            if not path.exists():
                continue
            if path.is_dir():
                self._delete_owned(path)
            else:
                try:
                    path.unlink()
                except OSError:
                    self._remember_removal(path)

    def _cleanup_temp_zips(self) -> None:
        if not self._root.is_dir():
            return
        for path in self._root.glob(".latest_shot.*.tmp"):
            try:
                path.unlink()
            except OSError:
                self._remember_removal(path)

    def _export_newest(self, touched: list[str]) -> None:
        """Zip the newest finished shot. An older completion does not replace it."""

        if not self._auto_export:
            return
        with self._lock:
            best_id: str | None = None
            best_sequence = -1
            for shot_id, facts in self._shots.items():
                if not facts.get("terminal"):
                    continue
                sequence = int(facts.get("sequence") or 0)
                if sequence > best_sequence:
                    best_sequence = sequence
                    best_id = shot_id
            if best_id is None or best_sequence < self._exported_sequence:
                return
            if best_sequence == self._exported_sequence and best_id not in touched:
                return
            facts = self._shots[best_id]
            session_id = str(facts["session_id"])
            status = str((facts.get("terminal") or {}).get("status") or "unavailable")
        directory = shot_dir(self._root, session_id, best_id)
        if not (directory / "report.json").is_file():
            return
        manifest = {
            "shot_id": best_id,
            "status": status,
            "sequence": best_sequence,
            "created_at": datetime.now(UTC).isoformat(),
            "session_id": session_id,
        }
        try:
            export_latest_shot(
                self._root,
                directory,
                manifest,
                include_images=self._include_images,
            )
        except Exception:
            logger.warning(
                "diagnostics_export_failed",
                extra={
                    "shot_id": best_id,
                    "diagnostics_session": session_id,
                    "diagnostics_status": status,
                },
            )
            return
        with self._lock:
            if best_sequence >= self._exported_sequence:
                self._exported_sequence = best_sequence

    def _retain_images(self, shot_id: str, snapshot: dict[str, Any]) -> None:
        """Copy the selected evidence files into this shot's diagnostic folder.

        Capture cleanup can remove the originals later. A copied file stays
        with the report. An image is `sent` only when that shot's model
        request listed it.
        """

        evidence = snapshot.get("evidence")
        if not isinstance(evidence, dict):
            return
        frames = list(evidence.get("selected_frames") or [])
        directory = shot_dir(self._root, str(snapshot.get("session_id")), shot_id)
        roots = _recorded_roots(snapshot)
        sent = _sent_keys(snapshot.get("gpt"))
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            owned = _owned_image(directory, frame)
            if owned is None:
                source = _source_file(frame, roots)
                name = _owned_name(frame)
                destination = directory / "images" / name
                if source is not None and copy_inside(source, destination, roots):
                    owned = f"images/{name}"
            key = _image_key(frame)
            frame["diagnostic_file"] = owned
            frame["requested"] = key in sent
            if owned is None:
                frame["transmission"] = "missing"
            elif key in sent:
                frame["transmission"] = "sent"
            else:
                frame["transmission"] = "not_sent"
        evidence["selected_frames"] = frames
        evidence["request_only_images"] = _copy_request_only(
            snapshot, directory, roots, frames
        )
        _point_payload_at_copies(snapshot)


def _blank(shot_id: str, session_id: str) -> dict[str, Any]:
    return {
        "shot_id": shot_id,
        "session_id": session_id,
        "capture_id": None,
        "trigger_id": None,
        "strike_id": None,
        "simulator_shot_id": None,
        "relationships": [
            {
                "name": "diagnostic_file_key",
                "value": safe_segment(shot_id),
                "source": "shot directory name; the coaching shot id when one exists",
            }
        ],
        "milestones": {},
        "club": None,
        "measurements": None,
        "capture": None,
        "evidence": None,
        "gpt": None,
        "decision": None,
        "hud": None,
        "tts_request": None,
        "tts_playback": None,
        "evidence_timing_ms": {},
        "simulated_provider": False,
        "terminal": None,
        "errors": [],
        "warnings": [],
        "contact_sheet": None,
        "playback_started": False,
    }


def _measurement_payload(shot: Any) -> dict[str, Any]:
    club = getattr(shot, "club", None)
    ball = getattr(shot, "ball", None)
    delivery = getattr(shot, "club_delivery", None)
    present: dict[str, Any] = {}
    missing: list[str] = []
    if ball is None:
        missing.append("ball")
    else:
        for name in _BALL_FIELDS:
            value = getattr(ball, name)
            if value is None:
                missing.append(f"ball.{name}")
            else:
                present[f"ball.{name}"] = value
    if delivery is None:
        missing.append("club_delivery")
    else:
        for name in _CLUB_FIELDS:
            value = getattr(delivery, name)
            if value is None:
                missing.append(f"club_delivery.{name}")
            else:
                present[f"club_delivery.{name}"] = value
    club_payload = None
    if club is not None:
        kind = getattr(getattr(club, "kind", None), "value", None)
        club_payload = {
            "kind": kind,
            "vendor_code": getattr(club, "vendor_code", None),
            "quality": "measured",
        }
    return {
        "simulator_shot_id": str(shot.shot_id),
        "club": club_payload,
        "measurements": {
            "quality": "measured",
            "present": present,
            "missing": missing,
        },
        "handedness": getattr(getattr(shot, "handedness", None), "value", None),
    }


def _apply_measurements(facts: dict[str, Any], payload: dict[str, Any]) -> None:
    facts["simulator_shot_id"] = payload["simulator_shot_id"]
    facts["club"] = payload["club"]
    facts["measurements"] = payload["measurements"]
    _relate(facts, "simulator_shot_id", payload["simulator_shot_id"], "Shot.shot_id")
    if payload.get("handedness"):
        _relate(facts, "handedness", payload["handedness"], "Shot.handedness")


def _camera(camera: Any) -> dict[str, Any]:
    view = getattr(camera, "view", None)
    return {
        "camera_id": camera.camera_id,
        "source": camera.source,
        "ok": bool(camera.ok),
        "captured": camera.captured,
        "kept": len(camera.frames),
        "failed": camera.failed,
        "detail": camera.detail,
        "view": getattr(view, "value", None),
    }


def _frames(package: Any, directory: Path) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    root = directory.resolve()
    for frame in package.selected_frames:
        reference = frame.reference
        relative = str(reference.relative_file)
        path = (directory / relative).resolve()
        inside = root == path or root in path.parents
        frames.append(
            {
                "camera_id": str(reference.camera_id),
                "relative_file": relative,
                "path": str(path) if inside else None,
                "capture_tick": int(reference.capture_tick),
                "timestamp_seconds": float(reference.timestamp_seconds),
                "stage": frame.stage,
                "selection_reason": str(frame.selection_reason),
                "sha256": reference.sha256,
            }
        )
    return frames


def _stages(package: Any) -> list[dict[str, Any]]:
    rows = []
    for stage in package.stage_estimates:
        rows.append(
            {
                "camera_id": stage.camera_id,
                "status": stage.status,
                "unresolved_stages": list(stage.unresolved_stages),
                "boundaries": [
                    {
                        "stage": boundary.stage,
                        "time_interval_seconds": list(boundary.time_interval_seconds),
                        "confidence": boundary.confidence,
                        "confidence_scale": boundary.confidence_scale,
                        "quality": "estimated",
                    }
                    for boundary in stage.boundaries
                ],
            }
        )
    return rows


def _impact(impact: Any) -> dict[str, Any]:
    ticks = impact.contact_interval_ticks
    seconds = impact.contact_interval_seconds
    status = str(impact.status)
    return {
        "status": status,
        "quality": "estimated" if status == "estimated" else "unavailable",
        "classification": impact.classification,
        "confidence": impact.confidence,
        "confidence_scale": impact.confidence_scale,
        "method": impact.method,
        "camera_id": impact.camera_id,
        "interval_ticks": None if ticks is None else [ticks[0], ticks[1]],
        "interval_seconds": None if seconds is None else [seconds[0], seconds[1]],
        "reason": impact.reason,
    }


def _request_images(prepared: Any) -> list[dict[str, Any]]:
    if prepared is None:
        return []
    images = []
    for index, image in enumerate(getattr(prepared, "images", ())):
        images.append(
            {
                "order": index,
                "image_id": image.image_id,
                "camera_id": image.camera_id,
                "relative_file": image.relative_file,
                "capture_tick": image.capture_tick,
                "timestamp_seconds": image.timestamp_seconds,
                "bytes": len(image.data),
            }
        )
    return images


def _image_detail(prepared: Any) -> str | None:
    images = getattr(prepared, "images", ())
    if not images:
        return None
    detail = getattr(images[0], "detail", None)
    return None if detail is None else str(detail)


def _request_payload(
    prepared: Any, provider: str | None, checkpoints: list[str]
) -> dict[str, Any]:
    prefix = str(getattr(prepared, "reference_prefix", "") or "")
    return {
        "provider": provider,
        "reference_version": prepared.reference_version,
        "reference_prefix_sha256": sha256_text(prefix) if prefix else None,
        "reference_prefix_omitted": (
            "The Swing Reference Model prefix is not copied. "
            "It is config/ideal_swing.json at the version above."
        ),
        "image_bytes_omitted": (
            "Original evidence files are not copied into this payload. "
            "Image ids are in request order."
        ),
        "system": prepared.system,
        "instructions": prepared.instructions,
        "evidence": prepared.manifest,
        "checkpoints_requested": checkpoints,
        "images": _request_images(prepared),
    }


def _empty_gpt(status: str, detail: str) -> dict[str, Any]:
    return {
        "status": status,
        "detail": detail,
        "validation_status": "skipped",
        "validation_errors": [],
        "accepted_findings": [],
        "rejected_findings": [],
        "parsed_assessment": None,
        "images": [],
        "image_count": 0,
    }


def _mark(
    facts: dict[str, Any],
    name: str,
    monotonic_seconds: float | None,
    occurred_at: datetime | None,
) -> None:
    if monotonic_seconds is None and occurred_at is None:
        return
    facts["milestones"].setdefault(
        name,
        {
            "monotonic_seconds": monotonic_seconds,
            "utc": None if occurred_at is None else occurred_at.isoformat(),
            "quality": "measured" if monotonic_seconds is not None else "wall_clock",
        },
    )


def _has_mark(facts: dict[str, Any], name: str) -> bool:
    mark = facts["milestones"].get(name) or {}
    return mark.get("monotonic_seconds") is not None or mark.get("observed") is True


def _relate(facts: dict[str, Any], name: str, value: Any, source: str) -> None:
    if value is None or value == "":
        return
    row = {"name": name, "value": str(value), "source": source}
    if row not in facts["relationships"]:
        facts["relationships"].append(row)


def _warn(facts: dict[str, Any], message: str) -> None:
    if message and message not in facts["warnings"]:
        facts["warnings"].append(message)


def _error(facts: dict[str, Any], message: str) -> None:
    if message and message not in facts["errors"]:
        facts["errors"].append(message)


def _locked(facts: dict[str, Any]) -> bool:
    status = (facts.get("terminal") or {}).get("status")
    return status in {"superseded", "cancelled"}


def _terminal(
    facts: dict[str, Any], status: str, reason: str, *, force: bool = False
) -> None:
    if not force and _locked(facts):
        return
    facts["terminal"] = {"status": status, "reason": reason}


def _write_payloads(directory: Path, snapshot: dict[str, Any]) -> None:
    request = snapshot.get("payload_request")
    response = snapshot.get("payload_response")
    if request is not None:
        write_json(directory / "gpt_request.json", request)
    if response is not None:
        write_json(directory / "gpt_response.json", response)


def _missing_pillow(error: Exception) -> bool:
    return isinstance(error, ImportError) and "PIL" in str(error)


def _compact_report(report: dict[str, Any]) -> dict[str, Any]:
    """Session totals without images, prompts, or model payloads."""

    timing = report.get("timing") or {}
    spans = []
    for span in timing.get("spans") or ():
        if not isinstance(span, dict):
            continue
        if span.get("quality") == "measured" and isinstance(
            span.get("milliseconds"), int
        ):
            spans.append(
                {
                    "name": span.get("name"),
                    "milliseconds": span["milliseconds"],
                    "quality": "measured",
                }
            )
    decision = report.get("decision") or {}
    delivery = report.get("delivery") or {}
    identity = report.get("identity") or {}
    return {
        "status": report.get("status"),
        "identity": {"shot_id": identity.get("shot_id")},
        "capture": {"status": (report.get("capture") or {}).get("status")},
        "decision": {
            "status": decision.get("status"),
            "decision_type": decision.get("decision_type"),
            "selected_checkpoint": decision.get("selected_checkpoint"),
            "progress": decision.get("progress"),
            "hud_text": decision.get("hud_text") or "",
            "speech_text": decision.get("speech_text") or "",
        },
        "delivery": {
            "hud": {"status": (delivery.get("hud") or {}).get("status")},
            "tts_playback": {
                "status": (delivery.get("tts_playback") or {}).get("status")
            },
        },
        "timing": {"spans": spans},
        "errors": [str(item) for item in report.get("errors") or ()],
        "warnings": [str(item) for item in report.get("warnings") or ()],
    }


def _recorded_roots(snapshot: dict[str, Any]) -> list[Path]:
    roots: list[Path] = []
    for section in (snapshot.get("evidence"), snapshot.get("capture")):
        if isinstance(section, dict) and section.get("directory"):
            roots.append(Path(str(section["directory"])))
    return roots


def _sent_keys(gpt: Any) -> set[tuple[str, str, str]]:
    if not isinstance(gpt, dict):
        return set()
    return {
        _image_key(image)
        for image in gpt.get("images") or ()
        if isinstance(image, dict)
    }


def _image_key(frame: dict[str, Any]) -> tuple[str, str, str]:
    tick = frame.get("capture_tick")
    return (
        str(frame.get("camera_id") or ""),
        str(frame.get("relative_file") or ""),
        "" if tick is None else str(tick),
    )


def _owned_image(directory: Path, frame: dict[str, Any]) -> str | None:
    relative = frame.get("diagnostic_file")
    if not relative:
        return None
    path = directory / str(relative)
    try:
        resolved = path.resolve()
        base = directory.resolve()
    except OSError:
        return None
    if resolved != base and base not in resolved.parents:
        return None
    if not resolved.is_file():
        return None
    return Path(str(relative)).as_posix()


def _source_file(frame: dict[str, Any], roots: list[Path]) -> Path | None:
    raw = frame.get("path")
    if raw and Path(str(raw)).is_file():
        return Path(str(raw))
    relative = frame.get("relative_file")
    if not relative:
        return None
    for root in roots:
        candidate = root / str(relative)
        if candidate.is_file():
            return candidate
    return None


def _owned_name(frame: dict[str, Any]) -> str:
    camera = safe_segment(str(frame.get("camera_id") or "camera"))
    tick = frame.get("capture_tick")
    tick_name = safe_segment("tick" if tick is None else str(tick))
    file_name = Path(str(frame.get("relative_file") or "frame.jpg")).name
    suffix = Path(file_name).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    stem = safe_segment(Path(file_name).stem) or "frame"
    return f"{camera}_{tick_name}_{stem}{suffix}"


def _copy_request_only(
    snapshot: dict[str, Any],
    directory: Path,
    roots: list[Path],
    frames: list[Any],
) -> list[dict[str, Any]]:
    """Images the model request listed that were not in the selected set."""

    gpt = snapshot.get("gpt")
    if not isinstance(gpt, dict):
        return []
    seen = {_image_key(frame) for frame in frames if isinstance(frame, dict)}
    extras: list[dict[str, Any]] = []
    for image in gpt.get("images") or []:
        if not isinstance(image, dict) or _image_key(image) in seen:
            continue
        record = dict(image)
        owned = _owned_image(directory, record)
        if owned is None:
            source = _source_file(record, roots)
            name = _owned_name(record)
            destination = directory / "images" / name
            if source is not None and copy_inside(source, destination, roots):
                owned = f"images/{name}"
        record["diagnostic_file"] = owned
        record["requested"] = True
        record["transmission"] = "sent" if owned else "missing"
        record["selection_reason"] = "in the model request, not in the selected set"
        extras.append(record)
    return extras


def _point_payload_at_copies(snapshot: dict[str, Any]) -> None:
    request = snapshot.get("payload_request")
    if not isinstance(request, dict):
        return
    body = request.get("body")
    if not isinstance(body, dict):
        return
    images = body.get("images")
    if not isinstance(images, list):
        return
    evidence = snapshot.get("evidence") or {}
    owned = {
        _image_key(frame): frame.get("diagnostic_file")
        for frame in list(evidence.get("selected_frames") or [])
        + list(evidence.get("request_only_images") or [])
        if isinstance(frame, dict)
    }
    for image in images:
        if not isinstance(image, dict):
            continue
        image.pop("data", None)
        image["diagnostic_file"] = owned.get(_image_key(image))


def _mtime(path: Path) -> float:
    try:
        marker = path / "session.json"
        return (marker if marker.is_file() else path).stat().st_mtime
    except OSError:
        return 0.0
