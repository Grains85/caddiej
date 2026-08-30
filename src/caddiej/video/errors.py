"""Failures the video subsystem can produce.

They exist to be caught and turned into events, not to reach the application.
Nothing in here should ever escape `VideoService`: a camera problem is reported
and survived, because a golf simulator that stops when a webcam is unplugged is
worse than one with no cameras at all.
"""


class VideoError(RuntimeError):
    """Base class for anything the video subsystem gives up on."""


class ClipExtractionError(VideoError):
    """FFmpeg was asked for a clip and did not produce a usable one."""
