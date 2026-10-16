"""Find one stored swing still by the capture it belongs to and its frame id.

The floor shows the frame the coach cited, so the browser has to be able to
ask for it, and that makes this the one place an HTTP request names a file.
Both halves of the name are treated as hostile. The capture id must be a
canonical UUID, which leaves no room for separators or dots; it selects a
directory only by matching the suffix the store gave it and then the
`capture_id` recorded inside it. The frame id is never joined onto a path at
all: it is looked up in the capture's own vocabulary by `frame_path`, which
also checks the answer stays inside the capture directory and exists.

Anything that fails any of that is simply not found. There is no fallback
picture, no nearest match and no newest-shot substitution, because a frame on
the floor beside a coaching line is a claim about *this* swing, and a wrong
picture is worse than none. Pruned shots disappear the same way.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from caddiej.swing_capture.store import frame_path, load_capture

_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def media_type(path: Path) -> str | None:
    """The image type a still is served as, or None for anything that is not one."""

    return _MEDIA_TYPES.get(path.suffix.lower())


class EvidenceFrames:
    """Resolve `(capture_id, frame_id)` to a still under one evidence root."""

    def __init__(self, evidence_root: Path | str) -> None:
        self._root = Path(evidence_root)

    def resolve(self, capture_id: str, frame_id: str) -> Path | None:
        try:
            parsed = UUID(capture_id)
        except (TypeError, ValueError, AttributeError):
            return None
        if str(parsed) != capture_id or not frame_id:
            return None
        try:
            root = self._root.resolve()
            if not root.is_dir():
                return None
            suffix = f"-{parsed.hex[:8]}"
            candidates = [
                child
                for child in root.iterdir()
                if child.name.endswith(suffix) and child.is_dir()
            ]
        except OSError:
            return None
        for directory in candidates:
            try:
                if not directory.resolve().is_relative_to(root):
                    continue
            except (OSError, RuntimeError):
                continue
            capture = load_capture(directory)
            if capture is None or capture.capture_id != parsed:
                continue
            path = frame_path(capture, frame_id)
            if path is not None and media_type(path) is not None:
                return path
        return None
