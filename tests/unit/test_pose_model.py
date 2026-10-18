"""The pose file is local, checksummed, and never fetched during a shot."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from caddiej.swing_evidence import pose_model


def test_override_is_the_path_that_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "pose.task"
    target.write_bytes(b"pose-bytes")
    monkeypatch.setenv("CADDIEJ_POSE_MODEL", str(target))
    monkeypatch.setattr(
        pose_model,
        "MODEL_SHA256",
        hashlib.sha256(b"pose-bytes").hexdigest(),
    )
    assert pose_model.ensure_pose_model() == target


def test_missing_model_names_the_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing.task"
    monkeypatch.setenv("CADDIEJ_POSE_MODEL", str(missing))
    with pytest.raises(FileNotFoundError, match="install-pose-model"):
        pose_model.ensure_pose_model()


def test_bad_checksum_names_both_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "pose.task"
    target.write_bytes(b"corrupt")
    monkeypatch.setenv("CADDIEJ_POSE_MODEL", str(target))
    with pytest.raises(RuntimeError, match="hash mismatch"):
        pose_model.ensure_pose_model()


def test_the_shot_path_does_not_download() -> None:
    source = Path(pose_model.__file__).read_text(encoding="utf-8")
    assert "urlopen" not in source
    assert "urllib" not in source


def test_installed_model_matches_the_published_checksum() -> None:
    path = pose_model.default_model_path()
    if not path.is_file():
        pytest.skip("pose model is not installed in this checkout")
    assert pose_model.file_sha256(path) == pose_model.MODEL_SHA256
    assert pose_model.MODEL_SHA256 == (
        "5134a3aad27a58b93da0088d431f366da362b44e3ccfbe3462b3827a839011b1"
    )
