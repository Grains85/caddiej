"""Swing video: a rolling buffer of the room, and a clip of every swing.

A subsystem rather than an adapter, on the same reasoning as `persistence`.
FFmpeg is an external binary and OpenCV is a library; neither is a system with a
protocol, a connection to keep alive, or vendor errors to translate. So
`video/ffmpeg.py` plays the part `persistence/database.py` plays for `sqlite3`:
the one place the dependency is allowed to exist.

Nothing in here is in the real-time path. Filming is triggered by a shot that has
already been hit, and every failure it can have — no camera, no FFmpeg, a full
disk — is reported and survived.
"""

from caddiej.video.cameras import (
    CameraKind,
    CameraSpec,
    avfoundation_camera,
    build_camera,
    dshow_camera,
    synthetic_camera,
    v4l2_camera,
)
from caddiej.video.clips import ClipExtractor, ClipRequest
from caddiej.video.errors import ClipExtractionError, VideoError
from caddiej.video.ffmpeg import CaptureProfile
from caddiej.video.frames import FrameExtractor
from caddiej.video.layout import MediaLayout
from caddiej.video.recorder import CameraHealth, CameraRecorder
from caddiej.video.segments import Gap, Segment, SegmentIndex, SegmentWindow
from caddiej.video.service import VideoHealth, VideoService, build_video_service

__all__ = [
    "CameraHealth",
    "CameraKind",
    "CameraRecorder",
    "CameraSpec",
    "CaptureProfile",
    "ClipExtractionError",
    "ClipExtractor",
    "ClipRequest",
    "FrameExtractor",
    "Gap",
    "MediaLayout",
    "Segment",
    "SegmentIndex",
    "SegmentWindow",
    "VideoError",
    "VideoHealth",
    "VideoService",
    "avfoundation_camera",
    "build_camera",
    "build_video_service",
    "dshow_camera",
    "synthetic_camera",
    "v4l2_camera",
]
