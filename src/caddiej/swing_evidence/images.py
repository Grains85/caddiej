"""Decode a swing JPEG once for the analyses that share it.

Motion analysis uses grayscale converted from a color decode. Impact
estimation uses a JPEG grayscale decode. Those conversions are not the same
pixels, so both are kept. Pose, when it runs, uses RGB converted from that
same color decode, which matches what Pillow produces for these files.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def opencv_providers() -> tuple[str, ...]:
    """Installed distributions that ship the ``cv2`` module."""

    import importlib.metadata as metadata

    names: list[str] = []
    for dist in metadata.distributions():
        name = dist.metadata["Name"] or ""
        if name.lower().startswith("opencv"):
            names.append(name)
    return tuple(sorted(set(names)))


def require_opencv4() -> None:
    """Refuse a missing, wrong-major, or duplicated OpenCV install.

    Swing evidence was measured on OpenCV 4. MediaPipe depends on
    ``opencv-contrib-python`` with no upper bound, and the newest release of
    that package is OpenCV 5. A second distribution such as
    ``opencv-python-headless`` installs another ``cv2``. Either one can
    replace the other without an error at install time. The supported install
    pins one OpenCV 4 build in ``constraints/vision.txt``.
    """

    version = str(getattr(cv2, "__version__", "") or "")
    providers = opencv_providers()
    if len(providers) > 1:
        raise RuntimeError(
            "more than one OpenCV distribution is installed "
            f"({', '.join(providers)}). They both provide cv2. "
            'Install with: python -m pip install -e ".[vision]" '
            "-c constraints/vision.txt"
        )
    if version.startswith("4."):
        return
    installed = ", ".join(providers) if providers else "none"
    raise RuntimeError(
        f"OpenCV {version or 'unknown'} is loaded ({installed}). "
        "CaddieJ requires OpenCV 4 from opencv-contrib-python, with no other "
        "cv2 package installed. Install with: "
        'python -m pip install -e ".[vision]" -c constraints/vision.txt'
    )


def decode_frame(
    path: Path, *, rgb: bool = False
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None] | None:
    """Return motion gray, impact gray, and optional RGB. None if unreadable."""

    try:
        data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    except OSError:
        return None
    if data.size < 32:
        return None
    color = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if color is None or color.size == 0:
        return None
    motion = cv2.cvtColor(color, cv2.COLOR_BGR2GRAY)
    impact = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if impact is None or impact.size == 0:
        return None
    converted = cv2.cvtColor(color, cv2.COLOR_BGR2RGB) if rgb else None
    return motion, impact, converted
