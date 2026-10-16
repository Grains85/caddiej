"""Stills of ProTee's own swing replay, read back through OBS.

A subsystem rather than an adapter, on the same reasoning as `video` and
`coaching`: OBS is an adapter and lives in `adapters/obs`, while deciding when
to take pictures, which of them are worth keeping and what replaces what is
CaddieJ's own.

What this deliberately does not do is as much of the design as what it does.
It does not open the swing cameras — ProTee owns those and must remain the only
process that does. It does not read ProTee's encrypted recordings. It does not
click anything in ProTee, or crop anything in OBS: the two cropped regions are
sources somebody set up by hand, and CaddieJ only asks them for a picture. And
it reads no numbers off the screen; the passive ProTee and GSPro observers
remain the one source of structured shot data.
"""

from caddiej.swing_capture.diagnose import SwingCaptureProbe, probe_swing_capture
from caddiej.swing_capture.select import (
    ActiveInterval,
    CapturedFrame,
    active_interval,
    difference,
    likely_blank,
    replay_start,
    select,
    settled_frame,
)
from caddiej.swing_capture.service import (
    SwingCaptureHealth,
    SwingCaptureService,
    build_swing_capture_service,
)
from caddiej.swing_capture.stills import (
    ordered_frames,
    read_labeled_stills,
    read_stills,
)
from caddiej.swing_capture.store import (
    CameraResult,
    CaptureRecord,
    LatestSwingStore,
    StagedCapture,
)

__all__ = [
    "ActiveInterval",
    "CameraResult",
    "CaptureRecord",
    "CapturedFrame",
    "LatestSwingStore",
    "StagedCapture",
    "SwingCaptureHealth",
    "SwingCaptureProbe",
    "SwingCaptureService",
    "active_interval",
    "build_swing_capture_service",
    "difference",
    "likely_blank",
    "ordered_frames",
    "probe_swing_capture",
    "read_labeled_stills",
    "read_stills",
    "replay_start",
    "select",
    "settled_frame",
]
