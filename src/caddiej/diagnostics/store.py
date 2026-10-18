"""Paths, atomic writes, and secret scrubbing for diagnostic files.

A report is written by replacing a finished temp file. A reader on Windows
never sees a half-written JSON document, and the writer does not keep the
destination open.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

_INVALID = re.compile(r'[<>:"|?*\x00-\x1f\\/]')


def _secret_key(key: str) -> bool:
    """Credential field names. `input_tokens` is a count, not a credential."""

    folded = re.sub(r"[^a-z0-9]", "", str(key).lower()).replace("tokens", "")
    return any(
        piece in folded
        for piece in (
            "authorization",
            "apikey",
            "bearer",
            "password",
            "secret",
            "token",
            "credential",
        )
    )


_SECRET_TEXT = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+)")

SHOT_SCHEMA_NAME = "caddiej_shot_diagnostic"
SHOT_SCHEMA_VERSION = "1"
SESSION_SCHEMA_NAME = "caddiej_session_diagnostic"
SESSION_SCHEMA_VERSION = "1"

STATUSES = frozenset(
    {"success", "failed", "skipped", "cancelled", "superseded", "unavailable"}
)


def safe_segment(value: str) -> str:
    """One path segment that is legal on Windows and cannot climb directories."""

    text = _INVALID.sub("_", str(value).strip())
    text = text.strip(". ")
    if text in {"", ".", ".."}:
        return "unknown"
    return text[:80]


def session_dir(root: Path, session_id: str) -> Path:
    return Path(root) / safe_segment(session_id)


def shot_dir(root: Path, session_id: str, shot_id: str) -> Path:
    return session_dir(root, session_id) / "shots" / safe_segment(shot_id)


def write_bytes(path: Path, payload: bytes) -> None:
    """Replace `path` with `payload`. The destination is not held open."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def write_text(path: Path, text: str) -> None:
    write_bytes(path, text.encode("utf-8"))


def write_json(path: Path, payload: Any) -> None:
    write_text(
        path, json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n"
    )


def remove_tree(path: Path, root: Path) -> bool:
    """Delete `path` when it is inside `root`. A locked file is left in place."""

    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    if resolved != base and base not in resolved.parents:
        return False
    try:
        shutil.rmtree(resolved)
    except OSError:
        return False
    return True


def scrub(value: Any) -> Any:
    """Drop credentials. Diagnostic files are meant to be shared."""

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if _secret_key(str(key)):
                cleaned[str(key)] = "[redacted]"
            else:
                cleaned[str(key)] = scrub(item)
        return cleaned
    if isinstance(value, list | tuple):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub("[redacted]", value)
    return value


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def omit_image_bytes(value: Any) -> Any:
    """Drop embedded image bodies. The files are stored beside the report."""

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key).lower()
            if name in {"data", "b64_json", "image_base64", "base64"} and isinstance(
                item, str | bytes
            ):
                cleaned[str(key)] = "[image bytes omitted]"
            elif (
                name in {"url", "image_url"}
                and isinstance(item, str)
                and item.startswith("data:")
            ):
                cleaned[str(key)] = "[image bytes omitted]"
            else:
                cleaned[str(key)] = omit_image_bytes(item)
        return cleaned
    if isinstance(value, list | tuple):
        return [omit_image_bytes(item) for item in value]
    return value


def copy_inside(source: Path, destination: Path, roots: list[Path]) -> bool:
    """Copy one file into the diagnostic shot folder.

    The source has to sit inside a capture or evidence directory this shot
    already recorded. A path outside those directories is ignored. The
    original file is not deleted.
    """

    try:
        resolved = Path(source).resolve()
    except OSError:
        return False
    if not resolved.is_file():
        return False
    allowed = False
    for root in roots:
        try:
            base = Path(root).resolve()
        except OSError:
            continue
        if resolved == base or base in resolved.parents:
            allowed = True
            break
    if not allowed:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(resolved, temporary)
        os.replace(temporary, destination)
    except OSError:
        return False
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return destination.is_file()


def bounded_json(payload: Any, max_bytes: int) -> dict[str, Any]:
    """A JSON object that fits the retention cap, or an explicit omission."""

    body = scrub(omit_image_bytes(payload))
    encoded = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) <= max_bytes:
        return {
            "retained": True,
            "truncated": False,
            "bytes": len(encoded),
            "body": body,
        }
    return {
        "retained": False,
        "truncated": False,
        "bytes": len(encoded),
        "body": None,
        "detail": (
            f"payload is {len(encoded)} bytes, above the "
            f"{max_bytes} byte retention limit"
        ),
    }
