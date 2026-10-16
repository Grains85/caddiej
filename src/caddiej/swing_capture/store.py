"""Where the stills of the newest swing go, and how they replace the last one.

There is one swing on disk and no archive, which is a decision rather than an
omission: what anything downstream wants is the swing that just happened, and
keeping every swing of an evening would fill a disk with pictures of somebody
in their own home that nobody asked to keep.

The whole of this file exists to make "replace" mean replace. A consumer that
opens `latest_swing` must never find four frames of the new swing beside six of
the old one, or a `metadata.json` that describes stills that are still being
written. So the capture is assembled under a hidden staging name and moved into
place as one directory rename at the end, and the directory it displaces is
moved aside and deleted afterwards rather than emptied in place.

There is a moment during that swap when `latest_swing` does not exist. That is
the honest cost of doing it with renames, which are the only cheap operation a
filesystem offers that is close to atomic, and it is microseconds rather than
the seconds a capture takes. A reader that finds nothing should look again; a
reader must never find half.

Nothing here is asynchronous. Writing twenty JPEGs is a fraction of a second of
blocking I/O, and the service calls it on a thread so that fraction is not
spent on the event loop.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from caddiej.domain.cues import PanelKind, PanelView, VisualClaim
from caddiej.domain.media import (
    MetricsStill,
    SwingCameraCapture,
    SwingCapture,
    SwingFrame,
)
from caddiej.domain.swing_phase import SwingPhase
from caddiej.domain.vx import VX_METRICS_PROVENANCE
from caddiej.swing_capture.cadence import Cadence
from caddiej.swing_capture.select import ActiveInterval, CapturedFrame
from caddiej.video.jpeg_size import jpeg_size

logger = logging.getLogger(__name__)

METADATA_NAME = "metadata.json"
METRICS_DIRECTORY = "metrics"
METRICS_FILE = "all_values.jpg"
STALE_NAME = "STALE.json"
EVIDENCE_DIRECTORY = "swing_shots"
EVIDENCE_KEEP = 3
"""Default number of shot directories. The newest, plus two so a coaching
request for the previous one can still read its stills."""
EVIDENCE_KEEP_MAX = 40
"""Hard cap. Diagnostic timelines live inside these directories, so a request
for N timelines retains at least N shots, and neither input may pass this."""
DIAGNOSTIC_DIRECTORY = "diagnostic"
DIAGNOSTIC_KEY = "diagnostic_timeline"

_REPLACE_ATTEMPTS = 8
_REPLACE_PAUSE = 0.05
# `latest_swing` is for looking at. A handle held open in it is a person
# looking, which a retry cannot outwait, so it is not retried for long.
_LATEST_ATTEMPTS = 3
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")


def retention_limit(
    evidence_keep: int = EVIDENCE_KEEP, diagnostic_timeline_shots: int = 0
) -> int:
    """How many shot directories may remain on disk.

    The default is three. A diagnostic timeline is stored inside its shot, so
    asking for N timelines retains at least N directories. Both inputs stop
    at `EVIDENCE_KEEP_MAX`.
    """

    if isinstance(evidence_keep, bool) or not isinstance(evidence_keep, int):
        raise ValueError("evidence_keep must be an integer")
    if isinstance(diagnostic_timeline_shots, bool) or not isinstance(
        diagnostic_timeline_shots, int
    ):
        raise ValueError("diagnostic_timeline_shots must be an integer")
    if not EVIDENCE_KEEP <= evidence_keep <= EVIDENCE_KEEP_MAX:
        raise ValueError(
            f"evidence_keep must be from {EVIDENCE_KEEP} to {EVIDENCE_KEEP_MAX}"
        )
    if not 0 <= diagnostic_timeline_shots <= EVIDENCE_KEEP_MAX:
        raise ValueError(
            "diagnostic_timeline_shots must be from 0 to "
            f"{EVIDENCE_KEEP_MAX}"
        )
    return max(evidence_keep, diagnostic_timeline_shots)


@dataclass(frozen=True, slots=True)
class FrameTrace:
    """One screenshot as it came back, for proving which layer repeated a picture.

    A digest and a size, never the bytes. `changed` compares with the previous
    screenshot of the same panel.
    """

    index: int
    offset_seconds: float
    sha256: str
    size_bytes: int
    width: int
    height: int
    changed: bool
    selected: bool


@dataclass(frozen=True, slots=True)
class CameraResult:
    """What one camera produced, after selection and before it is written."""

    camera_id: str
    source: str
    ok: bool
    captured: int
    failed: int
    frames: tuple[CapturedFrame, ...]
    interval: ActiveInterval | None = None
    detail: str | None = None
    kind: PanelKind = PanelKind.DYNAMIC
    supports: frozenset[VisualClaim] = field(default_factory=frozenset)
    settled: bool = False
    view: PanelView = PanelView.NONE
    blank_rejected: int = 0
    duplicate_frames: int = 0
    distinct_frames: int = 0
    selected_indices: tuple[int, ...] = ()
    """Which captured screenshot each kept frame is, in `frames` order."""

    phases: tuple[SwingPhase, ...] = ()
    """Which swing moment each kept frame was aimed at, in `frames` order.

    Empty when this panel has no phase calibration -- a static graphic, a
    panel nobody has mapped a view onto -- and then the stills are an even
    spread with nothing to say about what they show."""

    timeline: tuple[FrameTrace, ...] = ()

    diagnostic_frames: tuple[CapturedFrame, ...] = ()
    """Every screenshot this panel returned, in capture order, when the timing
    diagnostic is on for this shot. Empty otherwise, which is production."""

    cadence: Cadence | None = None
    """The rate this panel's burst asked for and achieved."""


@dataclass(frozen=True, slots=True)
class Publication:
    """The capture as coaching will read it, and how the inspection copy fared.

    `capture.directory` is the per-shot evidence directory, which nothing but
    CaddieJ ever opens. `latest_swing` is a copy for people to look at, and a
    viewer holding one of its files open must not cost the shot its coaching.
    """

    capture: SwingCapture
    latest_ok: bool
    latest_detail: str | None = None


@dataclass(frozen=True, slots=True)
class MetricsResult:
    """The All Values still as captured, before it is written."""

    source: str
    ok: bool
    data: bytes | None = None
    width: int = 0
    height: int = 0
    captured_at: datetime | None = None
    delay_seconds: float = 0.0
    offset_seconds: float | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class StagedCapture:
    """A capture written under a hidden name, not yet visible to anything."""

    path: Path
    frames: dict[str, tuple[SwingFrame, ...]]
    metrics: MetricsStill | None = None


@dataclass(frozen=True, slots=True)
class CaptureRecord:
    """One finished capture, as the store is asked to publish it."""

    capture_id: UUID
    trigger_id: str
    detected_at: datetime | None
    started_at: datetime
    completed_at: datetime
    cameras: tuple[CameraResult, ...]
    interval: ActiveInterval
    configuration: dict[str, object]
    strike_id: UUID | None = None
    metrics: MetricsResult | None = None
    burst_started_at: datetime | None = None
    """Wall clock at the burst's offset zero. Only read when the timing
    diagnostic is on, and only taken then."""


class LatestSwingStore:
    """Owns one directory and the rule that it is replaced whole."""

    def __init__(
        self,
        directory: Path | str,
        *,
        debug_keep_shots: int = 0,
        debug_directory: Path | str | None = None,
        evidence_keep: int = EVIDENCE_KEEP,
    ) -> None:
        self._directory = Path(directory)
        if debug_keep_shots < 0:
            raise ValueError("debug_keep_shots must be >= 0")
        self._debug_keep = debug_keep_shots
        self._evidence_keep = retention_limit(evidence_keep)
        self._debug_directory = (
            Path(debug_directory)
            if debug_directory is not None
            else self._directory.parent / "swing_debug"
        )
        self._last_evidence: tuple[str, Path] | None = None

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def evidence_root(self) -> Path:
        """Where each shot's own directory lives, beside `latest_swing`."""

        return self._directory.parent / EVIDENCE_DIRECTORY

    def prepare(self) -> None:
        """Make the parent directory, and sweep anything a crash left behind.

        Staging, mirror and discard directories are the only things here that
        can outlive a run, and all are dead the moment the process that made
        them stops. Left alone they would accumulate a copy of every swing that
        was interrupted.
        """

        parent = self._directory.parent
        parent.mkdir(parents=True, exist_ok=True)
        self._sweep_working()
        self._prune_evidence(keep=None)
        self.prune_debug()

    def stage(self, record: CaptureRecord) -> StagedCapture:
        """Write the whole capture under a hidden name. Nothing is visible yet."""

        staging = self._working_path("staging", record.capture_id.hex[:8])
        _remove_tree(staging)
        staging.mkdir(parents=True)

        frames = {
            camera.camera_id: self._write_camera(staging, camera)
            for camera in record.cameras
        }
        metrics = _write_metrics(staging, record.metrics)
        payload = _metadata(record, frames, metrics)
        diagnostic = _write_diagnostic(staging, record, frames)
        if diagnostic is not None:
            payload[DIAGNOSTIC_KEY] = diagnostic
        (staging / METADATA_NAME).write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )
        return StagedCapture(path=staging, frames=frames, metrics=metrics)

    def publish(self, staged: StagedCapture, record: CaptureRecord) -> Publication:
        """Make the staged capture this shot's evidence, then mirror it.

        The evidence directory is a fresh name that nothing else has ever
        seen, so moving the staging directory there cannot be refused by a
        viewer. Only that move can fail the capture. `latest_swing` is then
        replaced by a copy, best effort: Windows refuses to rename a directory
        while anything holds a handle inside it, and an image viewer pointed at
        the last swing does exactly that.
        """

        evidence = self._evidence_path(record)
        evidence.parent.mkdir(parents=True, exist_ok=True)
        _rename(staged.path, evidence)
        self._last_evidence = (record.trigger_id, evidence)
        self._prune_evidence(keep=evidence)
        latest_ok, detail = self._mirror_latest(evidence, record)
        if latest_ok:
            self._archive_debug(record.capture_id)

        capture = SwingCapture(
            capture_id=record.capture_id,
            trigger_id=record.trigger_id,
            directory=evidence,
            started_at=record.started_at,
            detected_at=record.detected_at,
            cameras=tuple(
                SwingCameraCapture(
                    camera_id=camera.camera_id,
                    source=camera.source,
                    ok=camera.ok,
                    frames=staged.frames.get(camera.camera_id, ()),
                    captured=camera.captured,
                    failed=camera.failed,
                    detail=camera.detail,
                    kind=camera.kind,
                    supports=camera.supports,
                    settled=camera.settled,
                    view=camera.view,
                    blank_rejected=camera.blank_rejected,
                    duplicate_frames=camera.duplicate_frames,
                )
                for camera in record.cameras
            ),
            strike_id=record.strike_id,
            metrics=staged.metrics,
            published=self._directory if latest_ok else None,
        )
        return Publication(capture=capture, latest_ok=latest_ok, latest_detail=detail)

    def note_shot(self, trigger_id: str, shot_id: str) -> bool:
        """Record the shot this capture belongs to, if it is still the latest."""

        noted = False
        last = self._last_evidence
        if last is not None and last[0] == trigger_id:
            noted = update_capture_metadata(last[1], trigger_id, {"shot_id": shot_id})
        try:
            mirrored = update_capture_metadata(
                self._directory, trigger_id, {"shot_id": shot_id}
            )
        except OSError:
            mirrored = False
        return noted or mirrored

    def _evidence_path(self, record: CaptureRecord) -> Path:
        stamp = record.started_at.strftime("%Y%m%dT%H%M%S")
        trigger = _SAFE_NAME.sub("_", record.trigger_id)[:16] or "none"
        return self.evidence_root / f"{stamp}-{trigger}-{record.capture_id.hex[:8]}"

    def _prune_evidence(self, keep: Path | None) -> None:
        """At most the configured number of shots. A locked one is tried next time."""

        root = self.evidence_root
        if not root.is_dir():
            return
        shots = sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
        for extra in shots[self._evidence_keep :]:
            if extra == keep:
                continue
            if not _try_remove(extra):
                logger.info(
                    "swing_capture_cleanup_deferred",
                    extra={
                        "swing_capture_directory": str(extra),
                        "swing_capture_detail": "a file is still open; retried later",
                    },
                )

    def _sweep_working(self, keep: Path | None = None) -> None:
        parent = self._directory.parent
        for leftover in parent.glob(f".{self._directory.name}.*"):
            if leftover.is_dir() and leftover != keep:
                _try_remove(leftover)

    def _mirror_latest(
        self, evidence: Path, record: CaptureRecord
    ) -> tuple[bool, str | None]:
        """Replace `latest_swing` with a copy of this shot, or mark it stale."""

        mirror = self._working_path("mirror", record.capture_id.hex[:8])
        self._sweep_working()
        try:
            shutil.copytree(evidence, mirror)
        except OSError as error:
            _try_remove(mirror)
            return self._latest_failed(evidence, record, error)
        discard = self._working_path("discard", uuid4().hex[:8])
        moved_aside = False
        try:
            if self._directory.exists():
                _rename(self._directory, discard, attempts=_LATEST_ATTEMPTS)
                moved_aside = True
            _rename(mirror, self._directory, attempts=_LATEST_ATTEMPTS)
        except OSError as error:
            if moved_aside and not self._directory.exists():
                with contextlib.suppress(OSError):
                    discard.rename(self._directory)
            _try_remove(mirror)
            return self._latest_failed(evidence, record, error)
        _try_remove(discard)
        return True, None

    def _latest_failed(
        self, evidence: Path, record: CaptureRecord, error: OSError
    ) -> tuple[bool, str]:
        """Say, inside the old `latest_swing`, that it is not this shot.

        Writing a new file beside open ones is allowed where renaming their
        directory is not, so the marker usually lands even when the swap could
        not.
        """

        detail = f"{type(error).__name__}: {error}"[:300]
        marker = {
            "stale": True,
            "reason": detail,
            "superseded_by": {
                "capture_id": str(record.capture_id),
                "trigger_id": record.trigger_id,
                "strike_id": None
                if record.strike_id is None
                else str(record.strike_id),
                "directory": evidence.as_posix(),
                "completed_at": record.completed_at.isoformat(),
            },
        }
        if self._directory.is_dir():
            with contextlib.suppress(OSError):
                _write_atomic(
                    self._directory / STALE_NAME, json.dumps(marker, indent=2)
                )
            with contextlib.suppress(OSError, json.JSONDecodeError, TypeError):
                path = self._directory / METADATA_NAME
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    payload.update(marker)
                    _write_atomic(path, json.dumps(payload, indent=2))
        return False, detail

    def discard(self, staged: StagedCapture) -> None:
        """Throw away a capture that lost its race. Never raises."""

        _remove_tree(staged.path)

    def prune_debug(self) -> None:
        """Keep only the newest configured debug copies. Production stays one."""

        if self._debug_keep <= 0 or not self._debug_directory.exists():
            if self._debug_keep <= 0:
                _remove_tree(self._debug_directory)
            return
        kept = sorted(
            (path for path in self._debug_directory.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for extra in kept[self._debug_keep :]:
            _remove_tree(extra)

    def _archive_debug(self, capture_id: UUID) -> None:
        if self._debug_keep <= 0 or not self._directory.exists():
            return
        try:
            self._debug_directory.mkdir(parents=True, exist_ok=True)
            destination = self._debug_directory / capture_id.hex[:8]
            _remove_tree(destination)
            shutil.copytree(self._directory, destination)
        except OSError:
            logger.warning("swing_capture_debug_copy_failed", exc_info=True)
            return
        self.prune_debug()

    def _working_path(self, kind: str, suffix: str) -> Path:
        # A sibling of the published directory, so the move into place is a
        # rename within one filesystem rather than a copy across two.
        return self._directory.parent / f".{self._directory.name}.{kind}-{suffix}"

    def _write_camera(
        self, staging: Path, camera: CameraResult
    ) -> tuple[SwingFrame, ...]:
        directory = staging / camera.camera_id
        directory.mkdir(parents=True, exist_ok=True)
        written: list[SwingFrame] = []
        for index, frame in enumerate(camera.frames, start=1):
            name = f"frame_{index:03d}.jpg"
            (directory / name).write_bytes(frame.data)
            size = jpeg_size(frame.data)
            written.append(
                SwingFrame(
                    camera_id=camera.camera_id,
                    index=index,
                    offset_seconds=round(frame.offset_seconds, 3),
                    path=Path(camera.camera_id) / name,
                    width=size[0] if size is not None else 0,
                    height=size[1] if size is not None else 0,
                    phase=(
                        camera.phases[index - 1]
                        if index - 1 < len(camera.phases)
                        else None
                    ),
                )
            )
        return tuple(written)


def _write_metrics(staging: Path, result: MetricsResult | None) -> MetricsStill | None:
    """One fixed filename, so the directory cannot grow with the session."""

    if result is None:
        return None
    path: Path | None = None
    if result.ok and result.data:
        directory = staging / METRICS_DIRECTORY
        directory.mkdir(parents=True, exist_ok=True)
        (directory / METRICS_FILE).write_bytes(result.data)
        path = Path(METRICS_DIRECTORY) / METRICS_FILE
    return MetricsStill(
        source=result.source,
        ok=path is not None,
        path=path,
        width=result.width,
        height=result.height,
        captured_at=result.captured_at,
        delay_seconds=result.delay_seconds,
        offset_seconds=result.offset_seconds,
        detail=None if path is not None else (result.detail or "no still returned"),
    )


def _write_diagnostic(
    staging: Path,
    record: CaptureRecord,
    frames: dict[str, tuple[SwingFrame, ...]],
) -> dict[str, object] | None:
    """Every screenshot of the timing diagnostic, beside the shot's evidence.

    Under `diagnostic/`, which nothing that builds a sheet or a request reads:
    those resolve stills only through the `frames` lists in the metadata.
    Each file is named by its capture index, so `frame_014.jpg` is timeline
    entry 14 and a gap is a screenshot OBS did not return.
    """

    cameras = [camera for camera in record.cameras if camera.diagnostic_frames]
    if not cameras:
        return None
    burst = record.burst_started_at
    strike_lag = (
        None
        if burst is None or record.detected_at is None
        else (burst - record.detected_at).total_seconds()
    )
    entries: list[dict[str, object]] = []
    for camera in cameras:
        directory = staging / DIAGNOSTIC_DIRECTORY / camera.camera_id
        directory.mkdir(parents=True, exist_ok=True)
        interval = camera.interval
        start = None if interval is None else interval.start_seconds
        kept = {
            captured: written
            for captured, written in zip(
                camera.selected_indices,
                frames.get(camera.camera_id, ()),
                strict=False,
            )
        }
        timeline: list[dict[str, object]] = []
        for position, frame in enumerate(camera.diagnostic_frames):
            index = position + 1
            name = f"frame_{index:03d}.jpg"
            (directory / name).write_bytes(frame.data)
            size = jpeg_size(frame.data) or (0, 0)
            trace = (
                camera.timeline[position] if position < len(camera.timeline) else None
            )
            selected = kept.get(position)
            timeline.append(
                {
                    "camera_id": camera.camera_id,
                    "capture_index": index,
                    "file": f"{DIAGNOSTIC_DIRECTORY}/{camera.camera_id}/{name}",
                    "requested_at": (
                        None
                        if burst is None
                        else (
                            burst + timedelta(seconds=frame.offset_seconds)
                        ).isoformat()
                    ),
                    "offset_seconds": round(frame.offset_seconds, 4),
                    "offset_from_strike_seconds": (
                        None
                        if strike_lag is None
                        else round(strike_lag + frame.offset_seconds, 4)
                    ),
                    "offset_from_replay_start_seconds": (
                        None
                        if start is None
                        else round(frame.offset_seconds - start, 4)
                    ),
                    "width": size[0],
                    "height": size[1],
                    "bytes": len(frame.data),
                    "sha256": None if trace is None else trace.sha256,
                    "changed": None if trace is None else trace.changed,
                    "selected": selected is not None,
                    "selected_file": (
                        None if selected is None else selected.path.as_posix()
                    ),
                    "selected_phase": (
                        None
                        if selected is None or selected.phase is None
                        else selected.phase.value
                    ),
                }
            )
        entries.append(
            {
                "camera_id": camera.camera_id,
                "source": camera.source,
                "view": camera.view.value,
                "captured_frames": camera.captured,
                "failed_frames": camera.failed,
                "retained_frames": len(timeline),
                "replay_start_seconds": None if start is None else round(start, 4),
                "replay_start_detected": None
                if interval is None
                else interval.detected,
                "replay_start_at": (
                    None
                    if burst is None or start is None
                    else (burst + timedelta(seconds=start)).isoformat()
                ),
                "pre_replay_frames": (
                    None if interval is None else interval.pre_replay_frames
                ),
                "replay_start_borrowed_from": (
                    None if interval is None else interval.borrowed_from
                ),
                "cadence": None if camera.cadence is None else camera.cadence.as_dict(),
                "frames": timeline,
            }
        )
    return {
        "directory": DIAGNOSTIC_DIRECTORY,
        "capture_started_at": record.started_at.isoformat(),
        "burst_started_at": None if burst is None else burst.isoformat(),
        "detected_at": (
            None if record.detected_at is None else record.detected_at.isoformat()
        ),
        "fps": record.configuration.get("fps"),
        "detail": (
            "offset_seconds is when the screenshot was requested, from the "
            "burst's offset zero; cameras asked in the same tick share it. "
            "Diagnostic only: never sent to a model or drawn on a sheet."
        ),
        "cameras": entries,
    }


def _metrics_metadata(metrics: MetricsStill | None) -> dict[str, object] | None:
    if metrics is None:
        return None
    return {
        "source": metrics.source,
        "provenance": VX_METRICS_PROVENANCE,
        "ok": metrics.ok,
        "file": None if metrics.path is None else metrics.path.as_posix(),
        "width": metrics.width,
        "height": metrics.height,
        "captured_at": (
            None if metrics.captured_at is None else metrics.captured_at.isoformat()
        ),
        "delay_seconds": metrics.delay_seconds,
        "offset_seconds": metrics.offset_seconds,
        "detail": metrics.detail,
        "counts_against_selected_frames": False,
    }


def _metadata(
    record: CaptureRecord,
    frames: dict[str, tuple[SwingFrame, ...]],
    metrics: MetricsStill | None = None,
) -> dict[str, object]:
    """Everything a later reader needs, and nothing it already has.

    No shot measurements: those are owned by the passive observers and stored
    in history, and copying them here would create a second, staler copy of
    numbers CaddieJ already has a source of truth for. What is here is what
    only the capture knows — when it ran, what it asked OBS for, which stills
    came back, and which of them were kept.
    """

    return {
        "capture_id": str(record.capture_id),
        "trigger_id": record.trigger_id,
        "strike_id": None if record.strike_id is None else str(record.strike_id),
        "shot_id": None,
        "coach_metrics": _metrics_metadata(metrics),
        "detected_at": (
            None if record.detected_at is None else record.detected_at.isoformat()
        ),
        "started_at": record.started_at.isoformat(),
        "completed_at": record.completed_at.isoformat(),
        "ok": all(camera.ok for camera in record.cameras),
        "cameras_ready": sum(1 for camera in record.cameras if camera.ok),
        "selection_interval": {
            "start_seconds": round(record.interval.start_seconds, 3),
            "end_seconds": round(record.interval.end_seconds, 3),
            "detected": record.interval.detected,
            "pre_replay_frames": record.interval.pre_replay_frames,
        },
        "configuration": record.configuration,
        "cameras": [
            {
                "camera_id": camera.camera_id,
                "panel_id": camera.camera_id,
                "source": camera.source,
                "kind": camera.kind.value,
                "view": camera.view.value,
                "supports": sorted(claim.value for claim in camera.supports),
                "settled": camera.settled,
                "blank_rejected": camera.blank_rejected,
                "duplicate_frames": camera.duplicate_frames,
                "ok": camera.ok,
                "detail": camera.detail,
                "captured_frames": camera.captured,
                "failed_frames": camera.failed,
                "distinct_frames": camera.distinct_frames,
                "selected_frames": len(camera.frames),
                "cadence": None if camera.cadence is None else camera.cadence.as_dict(),
                "replay_interval": (
                    None
                    if camera.interval is None
                    else {
                        "start_seconds": round(camera.interval.start_seconds, 3),
                        "end_seconds": round(camera.interval.end_seconds, 3),
                        "detected": camera.interval.detected,
                        "pre_replay_frames": camera.interval.pre_replay_frames,
                        "start_borrowed_from": camera.interval.borrowed_from,
                    }
                ),
                "frames": [
                    _frame_metadata(frame, camera, position)
                    for position, frame in enumerate(frames.get(camera.camera_id, ()))
                ],
                "timeline": [
                    {
                        "index": trace.index,
                        "offset_seconds": round(trace.offset_seconds, 3),
                        "sha256": trace.sha256,
                        "bytes": trace.size_bytes,
                        "width": trace.width,
                        "height": trace.height,
                        "changed": trace.changed,
                        "selected": trace.selected,
                    }
                    for trace in camera.timeline
                ],
            }
            for camera in record.cameras
        ],
    }


def _frame_metadata(
    frame: SwingFrame, camera: CameraResult, position: int
) -> dict[str, object]:
    entry: dict[str, object] = {
        "index": frame.index,
        "file": frame.path.as_posix(),
        "offset_seconds": frame.offset_seconds,
        "width": frame.width,
        "height": frame.height,
        "phase": None if frame.phase is None else frame.phase.value,
    }
    if position < len(camera.selected_indices):
        captured = camera.selected_indices[position]
        entry["captured_index"] = captured + 1
        if captured < len(camera.timeline):
            entry["sha256"] = camera.timeline[captured].sha256
    return entry


def _phase(raw: object) -> SwingPhase | None:
    """A recorded phase name, or None for anything this build does not know.

    Captures written before frames were aimed have no phase at all, and a
    capture written by a later build may name a phase this one has never heard
    of. Both read back as "not aimed at anything", which is true and is better
    than refusing to load the swing over a label.
    """

    if not isinstance(raw, str):
        return None
    try:
        return SwingPhase(raw)
    except ValueError:
        return None


def load_capture(directory: Path | str) -> SwingCapture | None:
    """The published capture, read back from its metadata. None if unreadable.

    For diagnostics that want to run the real coaching path against the last
    real swing. The running service never needs this: it holds the capture it
    published.
    """

    root = Path(directory)
    try:
        payload = json.loads((root / METADATA_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        cameras = tuple(_camera_from(item) for item in payload.get("cameras", ()))
        metrics_raw = payload.get("coach_metrics")
        metrics = None
        if isinstance(metrics_raw, dict):
            file = metrics_raw.get("file")
            captured_at = metrics_raw.get("captured_at")
            metrics = MetricsStill(
                source=str(metrics_raw.get("source", "")),
                ok=bool(metrics_raw.get("ok")),
                path=None if not file else Path(str(file)),
                width=int(metrics_raw.get("width") or 0),
                height=int(metrics_raw.get("height") or 0),
                captured_at=(
                    None if not captured_at else datetime.fromisoformat(captured_at)
                ),
                delay_seconds=float(metrics_raw.get("delay_seconds") or 0.0),
                offset_seconds=metrics_raw.get("offset_seconds"),
                detail=metrics_raw.get("detail"),
            )
        strike = payload.get("strike_id")
        detected = payload.get("detected_at")
        return SwingCapture(
            capture_id=UUID(str(payload["capture_id"])),
            trigger_id=str(payload.get("trigger_id", "")),
            directory=root,
            started_at=datetime.fromisoformat(str(payload["started_at"])),
            detected_at=None if not detected else datetime.fromisoformat(detected),
            cameras=cameras,
            strike_id=None if not strike else UUID(str(strike)),
            metrics=metrics,
        )
    except (KeyError, TypeError, ValueError):
        return None


def newest_capture(latest_directory: Path | str) -> SwingCapture | None:
    """The newest shot's own evidence, or `latest_swing` when there is none."""

    latest = Path(latest_directory)
    root = latest.parent / EVIDENCE_DIRECTORY
    if root.is_dir():
        for shot in sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        ):
            capture = load_capture(shot)
            if capture is not None:
                return capture
    if (latest / STALE_NAME).exists():
        return None
    return load_capture(latest)


def _camera_from(item: Any) -> SwingCameraCapture:
    camera_id = str(item["camera_id"])
    raw_frames = item.get("frames") or ()
    if not isinstance(raw_frames, list | tuple):
        raise TypeError("frames is not a list")
    frames = tuple(
        SwingFrame(
            camera_id=camera_id,
            index=int(frame["index"]),
            offset_seconds=float(frame["offset_seconds"]),
            path=Path(str(frame["file"])),
            width=int(frame.get("width") or 0),
            height=int(frame.get("height") or 0),
            phase=_phase(frame.get("phase")),
        )
        for frame in raw_frames
        if isinstance(frame, dict)
    )
    supports = item.get("supports") or ()
    if not isinstance(supports, list | tuple):
        raise TypeError("supports is not a list")
    return SwingCameraCapture(
        camera_id=camera_id,
        source=str(item.get("source", "")),
        ok=bool(item.get("ok")),
        frames=frames,
        captured=int(str(item.get("captured_frames") or 0)),
        failed=int(str(item.get("failed_frames") or 0)),
        detail=None if item.get("detail") is None else str(item.get("detail")),
        kind=PanelKind(str(item.get("kind", PanelKind.DYNAMIC.value))),
        supports=frozenset(VisualClaim(str(claim)) for claim in supports),
        settled=bool(item.get("settled")),
        view=PanelView(str(item.get("view", PanelView.NONE.value))),
    )


METRICS_FRAME_ID = "all_values"


def frame_id(frame: SwingFrame) -> str:
    """The name a model is given for this still, such as `swing_side_02`.

    The panel and the still's own position among that panel's kept frames,
    which is also the order they are sent in. Derived from what is on disk
    rather than from the request, so the same file has the same id whether
    the frame budget sent all of a panel or only some of it: an id that
    renumbered when a panel was trimmed would name a different picture
    depending on how many others went with it.
    """

    return f"{frame.camera_id}_{frame.index:02d}"


def frame_files(capture: SwingCapture) -> dict[str, Path]:
    """Every still a reply could point at, by id, relative to the capture.

    The whole vocabulary. Nothing outside this mapping resolves to a file, so
    a reply that names anything else — a guessed filename, a path, an id from
    a different swing — names nothing.
    """

    files: dict[str, Path] = {}
    for camera in capture.cameras:
        for frame in camera.frames:
            files[frame_id(frame)] = frame.path
    metrics = capture.metrics
    if metrics is not None and metrics.ok and metrics.path is not None:
        files[METRICS_FRAME_ID] = metrics.path
    return files


def frame_path(capture: SwingCapture, frame_id: str) -> Path | None:
    """The stored still a frame id names, or None for anything else.

    An exact lookup in `frame_files`, never a path built from the id, and the
    answer is checked to sit inside the capture directory and to exist. The
    id has passed through a language model by the time anything asks, so it
    is treated as the untrusted text it is.
    """

    if not isinstance(frame_id, str):
        return None
    relative = frame_files(capture).get(frame_id)
    if relative is None or relative.is_absolute():
        return None
    try:
        root = capture.directory.resolve()
        path = (root / relative).resolve()
    except (OSError, RuntimeError):
        return None
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def update_capture_metadata(
    directory: Path, trigger_id: str, updates: dict[str, object]
) -> bool:
    """Merge fields into a published capture, if it is still this trigger.

    A newer swing replaces the directory whole, so a late note for the previous
    trigger finds a different `trigger_id` and writes nothing.
    """

    path = directory / METADATA_NAME
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("trigger_id") != trigger_id:
        return False
    payload.update(updates)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    last: OSError | None = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            temporary.replace(path)
            return True
        except OSError as error:
            last = error
            time.sleep(_REPLACE_PAUSE * (attempt + 1))
    if last is not None:
        raise last
    return False


def _rename(source: Path, destination: Path, attempts: int = _REPLACE_ATTEMPTS) -> None:
    """Rename, retrying the Windows case where a file is still open."""

    last: OSError | None = None
    for attempt in range(attempts):
        try:
            source.rename(destination)
            return
        except OSError as error:
            last = error
            time.sleep(_REPLACE_PAUSE * (attempt + 1))
    assert last is not None
    raise last


def _try_remove(path: Path) -> bool:
    """One attempt to delete a tree. True when it is gone. Never raises."""

    if not path.exists():
        return True
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex[:8]}.tmp")
    temporary.write_text(text, encoding="utf-8")
    try:
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _remove_tree(path: Path) -> None:
    """Delete a directory tree. Never raises; retries a locked Windows handle."""

    if not path.exists():
        return
    last: OSError | None = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            shutil.rmtree(path)
            return
        except OSError as error:
            last = error
            time.sleep(_REPLACE_PAUSE * (attempt + 1))
    if last is not None:
        shutil.rmtree(path, ignore_errors=True)
