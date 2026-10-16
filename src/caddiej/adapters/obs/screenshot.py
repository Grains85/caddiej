"""What counts as a screenshot CaddieJ can show to a person or a model.

OBS answers `GetSourceScreenshot` with success for a picture that has no
picture in it. A window capture that is not showing, or that is still pointed
at a window class which no longer exists, has a base size of 0. A crop filter
on that source then emits a 1×1 JPEG, about 631 bytes, and the request status
is still success. Treating that status as a capture is how four panels of a
golf swing became four identical dots.
"""

from caddiej.video.jpeg_size import jpeg_size

# 1×1 is the empty-capture sentinel. A requested imageWidth below 8 is rejected
# by OBS itself; this threshold is about the image OBS actually returned.
_MINIMUM_EDGE = 2


class UnusableScreenshot(Exception):
    """OBS returned an image that is too small to be evidence."""

    def __init__(self, source_name: str, size: tuple[int, int] | None) -> None:
        self.source_name = source_name
        self.size = size
        shown = "unknown" if size is None else f"{size[0]}×{size[1]}"
        super().__init__(f"invalid_capture_dimensions: '{source_name}' is {shown}")


def require_usable_screenshot(source_name: str, data: bytes) -> tuple[int, int]:
    """The decoded size, or an error a caller can log as invalid_capture_dimensions."""

    size = jpeg_size(data)
    if size is None or size[0] < _MINIMUM_EDGE or size[1] < _MINIMUM_EDGE:
        raise UnusableScreenshot(source_name, size)
    return size
