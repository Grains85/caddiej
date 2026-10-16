"""Prepare the next unlabeled swing for a manual contact label.

The runner only remembers which shot is being reviewed. It does not record
an intended swing, an expected fault, or a model result, and it does not
call a model.

    .venv\\Scripts\\python.exe research\\direct_visual_coach\\testhelprunner.py
    .venv\\Scripts\\python.exe research\\direct_visual_coach\\testhelprunner.py --contact-tick 41
    .venv\\Scripts\\python.exe research\\direct_visual_coach\\testhelprunner.py --contact-between 40 41
    .venv\\Scripts\\python.exe research\\direct_visual_coach\\testhelprunner.py --reset
    .venv\\Scripts\\python.exe research\\direct_visual_coach\\testhelprunner.py --debug
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import contact_label

STATE_NAME = ".testhelper_state.json"
CAPTURES = Path(__file__).resolve().parents[2] / "media" / "swing_shots"
VALIDATION_START = date(2026, 10, 9)
_LABEL_COMMAND = "python research/direct_visual_coach/testhelprunner.py"


@dataclass(frozen=True)
class Candidate:
    """One complete new swing that can receive a contact label."""

    shot_id: str
    path: Path
    started_at: datetime


def review(
    *,
    shots_root: Path | None = None,
    labels_root: Path | None = None,
) -> dict[str, Any]:
    """Select or continue the current shot and refresh its contact sheets."""

    shots = _shots(shots_root)
    labels = _labels(labels_root)
    chosen, count, continued = _choose(shots, labels)
    _write_state(labels, chosen.shot_id)
    prepared = contact_label.run(chosen.shot_id, shots_root=shots, labels_root=labels)
    sheet_paths = [Path(sheet["path"]).resolve() for sheet in prepared["sheets"]]
    commands = [open_command(path) for path in sheet_paths]
    text = _review_text(
        shot_id=chosen.shot_id,
        unlabeled_count=count,
        continued=continued,
        sheet_paths=sheet_paths,
        commands=commands,
    )
    return {
        "shot_id": chosen.shot_id,
        "unlabeled_count": count,
        "continued": continued,
        "sheets": [str(path) for path in sheet_paths],
        "open_commands": commands,
        "text": text,
    }


def label_current(
    *,
    contact_tick: int | None = None,
    contact_between: tuple[int, int] | None = None,
    shots_root: Path | None = None,
    labels_root: Path | None = None,
) -> str:
    """Write a manual contact label for the shot currently under review."""

    if contact_tick is None and contact_between is None:
        raise SystemExit("pass --contact-tick or --contact-between")
    labels = _labels(labels_root)
    shot_id = _read_state(labels)
    if shot_id is None:
        raise SystemExit(
            "no swing is waiting for a contact label; run testhelprunner.py first"
        )
    prepared = contact_label.run(
        shot_id,
        contact_tick=contact_tick,
        contact_between=contact_between,
        shots_root=_shots(shots_root),
        labels_root=labels,
    )
    document = prepared["contact_label"]
    if not isinstance(document, dict):
        raise SystemExit("contact label was not written")
    _clear_state(labels)
    return _saved_text(document)


def reset(*, labels_root: Path | None = None) -> str:
    """Forget the shot under review. Labels, sheets, and captures stay."""

    _clear_state(_labels(labels_root))
    return "Cleared the current shot."


def debug(
    *,
    shots_root: Path | None = None,
    labels_root: Path | None = None,
) -> str:
    """Explain every swing directory. Does not write state, sheets, or labels."""

    shots = _shots(shots_root)
    labels = _labels(labels_root)
    if not shots.is_dir():
        raise SystemExit(f"swing capture directory does not exist: {shots}")
    lines: list[str] = []
    for path in sorted(item for item in shots.iterdir() if item.is_dir()):
        if path.name.startswith("."):
            continue
        candidate, reason = _assess(path)
        if candidate is None:
            lines.append(f"{path.name} SKIPPED: {reason}")
        elif _is_labeled(labels, path.name):
            lines.append(f"{path.name} SKIPPED: contact label already recorded")
        else:
            lines.append(f"{path.name} ELIGIBLE")
    if not lines:
        lines.append("no swing directories")
    return "\n".join(lines)


def open_command(path: Path) -> str:
    """One Git Bash command that opens a sheet in the Windows image viewer."""

    return f'start "" "{path.resolve()}"'


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare the newest unlabeled swing for a manual contact label. "
            "Nothing is sent to a model."
        )
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--contact-tick",
        type=int,
        help="exact contact tick for the swing currently under review",
    )
    group.add_argument(
        "--contact-between",
        type=int,
        nargs=2,
        metavar=("N", "M"),
        help="contact is after tick N and before tick M",
    )
    group.add_argument(
        "--reset",
        action="store_true",
        help="forget the current shot without deleting captures or labels",
    )
    group.add_argument(
        "--debug",
        action="store_true",
        help="print why each swing was kept or skipped, without writing anything",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.debug:
        print(debug())
        return
    if args.reset:
        print(reset())
        return
    if args.contact_tick is not None or args.contact_between is not None:
        between = tuple(args.contact_between) if args.contact_between else None
        print(
            label_current(
                contact_tick=args.contact_tick,
                contact_between=between,
            )
        )
        return
    print(review()["text"])


def _choose(shots_root: Path, labels_root: Path) -> tuple[Candidate, int, bool]:
    waiting = [
        item
        for item in _candidates(shots_root)
        if not _is_labeled(labels_root, item.shot_id)
    ]
    current = _read_state(labels_root)
    by_id = {item.shot_id: item for item in waiting}
    if current is not None and current in by_id:
        return by_id[current], len(waiting), True
    if not waiting:
        raise SystemExit(
            "no unlabeled new swings to review; rerun with --debug to see each shot"
        )
    newest = max(waiting, key=lambda item: item.started_at)
    return newest, len(waiting), False


def _candidates(shots_root: Path) -> list[Candidate]:
    if not shots_root.is_dir():
        raise SystemExit(f"swing capture directory does not exist: {shots_root}")
    found: list[Candidate] = []
    for path in shots_root.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        item, _reason = _assess(path)
        if item is not None:
            found.append(item)
    return found


def _assess(path: Path) -> tuple[Candidate | None, str]:
    """Validate the capture first, then apply the validation-date cutoff."""

    meta_path = path / "metadata.json"
    if not meta_path.is_file():
        return None, "no metadata.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "metadata could not be read"
    if not isinstance(meta, dict):
        return None, "metadata is not an object"
    started = _started_at(meta)
    if started is None:
        return None, "no capture timestamp"
    try:
        frames = contact_label.replay_frames(meta)
    except ValueError as exc:
        return None, str(exc)
    diagnostic = (path / "diagnostic").resolve()
    for frame in frames:
        file = (path / frame.file).resolve()
        try:
            file.relative_to(diagnostic)
        except ValueError:
            return None, f"frame path escapes the shot: {frame.file}"
        if not file.is_file():
            return None, f"missing diagnostic frame {frame.file}"
    if started.date() < VALIDATION_START:
        return None, "before validation cutoff"
    return Candidate(path.name, path, started), ""


def _started_at(meta: Mapping[str, Any]) -> datetime | None:
    raw = meta.get("started_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _is_labeled(labels_root: Path, shot_id: str) -> bool:
    return (labels_root / shot_id / "contact_label.json").is_file()


def _state_path(labels_root: Path) -> Path:
    return labels_root / STATE_NAME


def _read_state(labels_root: Path) -> str | None:
    path = _state_path(labels_root)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("contact helper state could not be read") from exc
    if not isinstance(raw, dict) or set(raw) != {"shot_id"}:
        raise SystemExit("contact helper state may contain only a shot id")
    shot_id = raw.get("shot_id")
    if not isinstance(shot_id, str) or not shot_id.strip():
        raise SystemExit("contact helper state has no shot id")
    return shot_id


def _write_state(labels_root: Path, shot_id: str) -> None:
    labels_root.mkdir(parents=True, exist_ok=True)
    document = {"shot_id": shot_id}
    _state_path(labels_root).write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
    )


def _clear_state(labels_root: Path) -> None:
    path = _state_path(labels_root)
    if path.is_file():
        path.unlink()


def _shots(shots_root: Path | None) -> Path:
    return CAPTURES if shots_root is None else shots_root


def _labels(labels_root: Path | None) -> Path:
    return contact_label.LABELS if labels_root is None else labels_root


def _review_text(
    *,
    shot_id: str,
    unlabeled_count: int,
    continued: bool,
    sheet_paths: Sequence[Path],
    commands: Sequence[str],
) -> str:
    lines: list[str] = []
    if not continued and unlabeled_count > 1:
        lines.append(f"Found {unlabeled_count} unlabeled new shots.")
        lines.append(f"Selected newest: {shot_id}")
        lines.append("")
    elif not continued:
        lines.append("Found newest unlabeled swing:")
        lines.append(shot_id)
        lines.append("")
    lines.extend(
        [
            "SHOT:",
            shot_id,
            "",
            "Generated:",
            *[str(path) for path in sheet_paths],
            "",
            "Review the sheets and find either:",
            "",
            "A) exact contact tick N",
            "",
            "or",
            "",
            "B) last clearly pre-contact tick N and first clearly post-contact tick M",
            "",
            "OPEN REVIEW SHEETS:",
            "",
            *commands,
            "",
            "LABEL CONTACT:",
            "",
            "Exact frame:",
            f"{_LABEL_COMMAND} --contact-tick <N>",
            "",
            "Between frames:",
            f"{_LABEL_COMMAND} --contact-between <N> <M>",
        ]
    )
    return "\n".join(lines)


def _saved_text(document: Mapping[str, Any]) -> str:
    lines = [
        "Saved manual contact label:",
        f"shot: {document['shot_id']}",
    ]
    if "contact_tick" in document:
        lines.append(f"contact_tick: {document['contact_tick']}")
    else:
        lines.append(f"contact_between: {document['contact_between']}")
    lines.extend(["", "READY FOR NEXT SWING"])
    return "\n".join(lines)


if __name__ == "__main__":
    main()
