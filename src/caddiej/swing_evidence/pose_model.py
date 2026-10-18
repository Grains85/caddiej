"""Local MediaPipe pose model.

The installer and the running application both use this module. It imports
nothing from OpenCV, MediaPipe, or NumPy, and it never downloads the file.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_full/float16/1/pose_landmarker_full.task"
)
MODEL_SHA256 = "5134a3aad27a58b93da0088d431f366da362b44e3ccfbe3462b3827a839011b1"
MODEL_FILENAME = "pose_landmarker_full.task"

_REPO = Path(__file__).resolve().parents[3]


def repo_root() -> Path:
    """The repository that contains `models/` and `scripts/`."""

    return _REPO


def default_model_path() -> Path:
    """The documented model file, or the older research copy if that is the one on disk."""

    primary = _REPO / "models" / MODEL_FILENAME
    legacy = _REPO / "research" / "direct_visual_coach" / "models" / MODEL_FILENAME
    if primary.is_file() or not legacy.is_file():
        return primary
    return legacy


def active_model_path() -> Path:
    """The pose file for this process.

    `CADDIEJ_POSE_MODEL` overrides the default. Nothing here downloads a file.
    """

    override = os.environ.get("CADDIEJ_POSE_MODEL", "").strip()
    if override:
        return Path(override)
    return default_model_path()


def file_sha256(path: Path) -> str:
    """SHA-256 of a file, read in chunks."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_pose_model() -> Path:
    """Return the local pose model after checking its checksum.

    A missing or corrupted file is an installation error. The application
    does not fetch the model while a shot is being coached.
    """

    path = active_model_path()
    if not path.is_file():
        raise FileNotFoundError(
            "pose model is not installed at "
            f"{path}. Run scripts/install-pose-model.py. "
            "CaddieJ does not download it during a shot."
        )
    found = file_sha256(path)
    if found != MODEL_SHA256:
        raise RuntimeError(
            f"pose model hash mismatch at {path}: {found} "
            f"(expected {MODEL_SHA256}). "
            "Run scripts/install-pose-model.py again."
        )
    return path
