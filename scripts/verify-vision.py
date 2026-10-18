"""Check that one OpenCV 4 build, MediaPipe, and the pose model are usable.

Exits 0 when OpenCV 4 is the only cv2 provider and MediaPipe imports. A
missing pose model is reported and still exits 0 unless --require-model is
set: motion analysis can run without it, and swing stages stay unresolved.

    python scripts/verify-vision.py
    python scripts/verify-vision.py --require-model
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from caddiej.swing_evidence.images import (  # noqa: E402
    opencv_providers,
    require_opencv4,
)
from caddiej.swing_evidence.pose_model import (  # noqa: E402
    MODEL_SHA256,
    ensure_pose_model,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the swing-evidence install.")
    parser.add_argument(
        "--require-model",
        action="store_true",
        help="exit 1 when the pose model is missing or its checksum does not match",
    )
    args = parser.parse_args()
    failed = False
    try:
        import cv2

        require_opencv4()
    except Exception as exc:
        print(f"opencv: FAIL {exc}", file=sys.stderr)
        failed = True
    else:
        providers = ", ".join(opencv_providers()) or "unknown distribution"
        print(f"opencv: {cv2.__version__} ({providers})")
        print(f"cv2 file: {getattr(cv2, '__file__', '')}")
    try:
        import mediapipe

        print(f"mediapipe: {getattr(mediapipe, '__version__', 'unknown')}")
    except Exception as exc:
        print(f"mediapipe: FAIL {exc}", file=sys.stderr)
        failed = True
    try:
        checked = ensure_pose_model()
    except Exception as exc:
        print(f"pose model: FAIL {exc}", file=sys.stderr)
        if args.require_model:
            failed = True
    else:
        print(f"pose model: {checked}")
        print(f"sha256 {MODEL_SHA256}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
