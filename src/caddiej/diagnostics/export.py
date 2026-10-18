"""Zip diagnostic reports so they can be copied off the simulator PC.

The archive contains files already written. It does not call a model, and it
does not read environment files or credentials.
"""

from __future__ import annotations

import json
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_SKIP_NAMES = {".env", ".env.local", "credentials.json"}
LATEST_ZIP_NAME = "latest_shot.zip"


def export_latest_shot(
    root: Path,
    shot_directory: Path,
    manifest: dict[str, Any],
    *,
    include_images: bool = True,
) -> Path:
    """Replace `latest_shot.zip` with one finished shot.

    The zip is written to a temp file, opened and checked, then renamed over
    the previous zip. A failed check leaves the previous zip in place and
    removes the temp file.
    """

    base = Path(root)
    source = Path(shot_directory)
    base.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_name": "caddiej_latest_shot",
        "schema_version": "1",
        "shot_id": manifest.get("shot_id"),
        "status": manifest.get("status"),
        "sequence": manifest.get("sequence"),
        "created_at": manifest.get("created_at") or datetime.now(UTC).isoformat(),
        "session_id": manifest.get("session_id"),
    }
    sequence = document["sequence"]
    temporary = base / f".latest_shot.{os.getpid()}.{sequence}.tmp"
    target = base / LATEST_ZIP_NAME
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(document, indent=2, default=str) + "\n",
            )
            if source.is_dir():
                for path in sorted(source.rglob("*")):
                    if not path.is_file() or _blocked(path):
                        continue
                    relative = path.relative_to(source)
                    if not include_images and relative.parts[:1] == ("images",):
                        continue
                    archive.write(path, relative.as_posix())
        if not _valid_latest(temporary):
            raise OSError("diagnostic zip failed validation")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return target


def _valid_latest(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            broken = archive.testzip()
            names = set(archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, KeyError):
        return False
    if broken is not None or "report.json" not in names:
        return False
    return manifest.get("shot_id") is not None and manifest.get("sequence") is not None


def export_session(
    root: Path,
    destination: Path | None = None,
    *,
    session_id: str | None = None,
    include_images: bool = False,
) -> Path:
    """Write a zip of `session_id`, or of the newest session when it is omitted."""

    base = Path(root)
    session = _resolve_session(base, session_id)
    target = destination or (base / "exports" / f"{session.name}.zip")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    missing: list[str] = []
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for path in sorted(session.rglob("*")):
                if not path.is_file() or _blocked(path):
                    continue
                archive.write(path, path.relative_to(session).as_posix())
            if include_images:
                _add_images(archive, session, missing)
        if missing:
            with zipfile.ZipFile(
                temporary, "a", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr(
                    "missing.txt",
                    "\n".join(missing) + "\n",
                )
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def latest_session_id(root: Path) -> str | None:
    session = _newest(_session_dirs(Path(root)))
    return None if session is None else session.name


def _resolve_session(root: Path, session_id: str | None) -> Path:
    if session_id:
        from caddiej.diagnostics.store import safe_segment

        path = root / safe_segment(session_id)
        if not path.is_dir():
            raise FileNotFoundError(f"no diagnostic session {session_id}")
        return path
    session = _newest(_session_dirs(root))
    if session is None:
        raise FileNotFoundError(f"no diagnostic session under {root}")
    return session


def _session_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in root.iterdir():
        if not path.is_dir() or path.name == "exports":
            continue
        if (path / "session.json").is_file() or (path / "shots").is_dir():
            found.append(path)
    return found


def _newest(paths: list[Path]) -> Path | None:
    if not paths:
        return None

    def stamp(path: Path) -> float:
        marker = path / "session.json"
        try:
            return (marker if marker.is_file() else path).stat().st_mtime
        except OSError:
            return 0.0

    return max(paths, key=stamp)


def _blocked(path: Path) -> bool:
    name = path.name.lower()
    if name in _SKIP_NAMES or name.startswith(".env"):
        return True
    return False


def _add_images(archive: zipfile.ZipFile, session: Path, missing: list[str]) -> None:
    shots = session / "shots"
    if not shots.is_dir():
        return
    for report_path in sorted(shots.glob("*/report.json")):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            missing.append(f"{report_path.name}: report could not be read")
            continue
        capture_dir = (report.get("capture") or {}).get("directory")
        frames = (report.get("evidence") or {}).get("selected_frames") or ()
        shot_name = report_path.parent.name
        for frame in frames:
            added = _add_frame(archive, shot_name, capture_dir, frame)
            if added is not None:
                missing.append(added)


def _add_frame(
    archive: zipfile.ZipFile,
    shot_name: str,
    capture_dir: Any,
    frame: dict[str, Any],
) -> str | None:
    relative = frame.get("relative_file")
    if not relative or not capture_dir:
        return None
    root = Path(str(capture_dir)).resolve()
    candidate = (root / str(relative)).resolve()
    if root not in candidate.parents or ".." in Path(str(relative)).parts:
        return f"{relative}: path leaves the capture directory"
    if candidate.suffix.lower() not in _IMAGE_SUFFIXES:
        return f"{relative}: not an evidence image"
    if not candidate.is_file():
        return f"{relative}: file is missing"
    arcname = Path("images") / shot_name / Path(str(relative)).as_posix()
    try:
        payload = candidate.read_bytes()
    except OSError as error:
        return f"{relative}: {error}"
    archive.writestr(arcname.as_posix(), payload)
    return None
