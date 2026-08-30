"""Swing video, in the terms the rest of CaddieJ refers to it by.

A clip is a fact about a shot — that swing was filmed, the file is here, and the
ball was struck this far into it — so it belongs beside the shot rather than
inside the subsystem that produced it. A voice line, an overlay, or a later
analysis step can all subscribe to "a clip exists" without importing a camera.

Nothing here knows about FFmpeg, segments, containers or devices. The one thing
that leaks through from the physical world is `TimeQuality`, and it leaks
deliberately: a consumer that is about to say "impact was at frame 118" needs to
be told how much that claim can be trusted.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID


class CaptureState(StrEnum):
    """What a camera is doing, as far as CaddieJ can tell."""

    DISABLED = "disabled"
    STARTING = "starting"
    RECORDING = "recording"
    RETRYING = "retrying"
    UNAVAILABLE = "unavailable"


class ClipFailure(StrEnum):
    """Why no clip came out. Reported, never silent."""

    NOT_RECORDING = "not_recording"
    """Nothing was being filmed when the shot happened."""

    BUFFER_RECYCLED = "buffer_recycled"
    """The swing was recorded but had already been deleted to make room."""

    INCOMPLETE = "incomplete"
    """Part of the window is missing, usually because capture restarted."""

    QUEUE_FULL = "queue_full"
    """Clips were already backed up, so this one was refused rather than queued."""

    EXTRACTION_FAILED = "extraction_failed"
    TIMED_OUT = "timed_out"

    ABANDONED = "abandoned"
    """CaddieJ stopped before the clip was finished. Said rather than left
    silent, because anything waiting on the clip — a coaching job holding a shot
    open for footage — otherwise waits for something that is never coming."""


class TimeQuality(StrEnum):
    """How much to trust where impact sits inside a clip.

    Three unknowns lie between a ball being struck and a frame being written:
    the camera's own latency, how long the launch monitor took to report the
    shot, and the drift between two clocks. `ESTIMATED` means none of them has
    been measured, which is the honest default and is why a clip is cut wide
    rather than tight.
    """

    ESTIMATED = "estimated"
    CALIBRATED = "calibrated"
    """A measured per-camera latency was applied."""


@dataclass(frozen=True, slots=True)
class ClipRef:
    """Where a swing clip is, and where in it the ball was struck.

    `path` is relative to the media root so that the whole library can be moved
    or copied to another machine without invalidating a stored reference.

    The times are wall clock. A monotonic reading would be more precise, but it
    is meaningless outside the process that took it, and a clip has to stay
    interpretable next week by something that was not running today.
    """

    clip_id: UUID
    shot_id: UUID
    camera_id: str
    path: Path
    recorded_at: datetime
    impact_at: datetime
    impact_offset_seconds: float
    duration_seconds: float
    fps: float
    width: int
    height: int
    time_quality: TimeQuality = TimeQuality.ESTIMATED
