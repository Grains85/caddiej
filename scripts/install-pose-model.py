"""Install the MediaPipe pose model and check its checksum.

The running application does not download this file. Run this once, after
the virtual environment exists:

    python scripts/install-pose-model.py

The file is written to models/pose_landmarker_full.task. When
CADDIEJ_POSE_MODEL is set, the file is written there instead, which is the
path the application reads. A file that is already present and matches the
checksum is left in place.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from caddiej.swing_evidence.pose_model import (  # noqa: E402
    MODEL_SHA256,
    MODEL_URL,
    active_model_path,
    file_sha256,
)


def main() -> int:
    destination = active_model_path()
    if destination.is_file() and file_sha256(destination) == MODEL_SHA256:
        print(f"pose model already installed: {destination}")
        print(f"sha256 {MODEL_SHA256}")
        return 0
    if destination.is_file():
        print(
            f"replacing pose model with a bad checksum: {destination}", file=sys.stderr
        )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"could not create {destination.parent}: {exc}", file=sys.stderr)
        return 1
    temporary = destination.with_suffix(".task.part")
    print(f"downloading {MODEL_URL}")
    try:
        with urllib.request.urlopen(MODEL_URL, timeout=120) as response:
            temporary.write_bytes(response.read())
        found = file_sha256(temporary)
        if found != MODEL_SHA256:
            temporary.unlink(missing_ok=True)
            print(f"checksum mismatch: {found}", file=sys.stderr)
            print(f"expected:          {MODEL_SHA256}", file=sys.stderr)
            return 1
        temporary.replace(destination)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        print(f"download failed: {exc}", file=sys.stderr)
        return 1
    print(f"installed {destination}")
    print(f"sha256 {MODEL_SHA256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
