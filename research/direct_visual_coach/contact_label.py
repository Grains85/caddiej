"""Manual contact labels for the visual-coach research path.

The helper lays out every replay-era Down-the-Line frame so a person can
look at them. It writes a contact tick or a contact interval only when that
person passes one. It does not guess contact, and it does not call a model.

    .venv\\Scripts\\python.exe research\\direct_visual_coach\\contact_label.py <shot-id>
    .venv\\Scripts\\python.exe research\\direct_visual_coach\\contact_label.py <shot-id> --contact-tick 41
    .venv\\Scripts\\python.exe research\\direct_visual_coach\\contact_label.py <shot-id> --contact-between 40 41
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import direct_visual_test as coach
from PIL import Image, ImageDraw, ImageFont

LABELS = Path(__file__).resolve().parent / "results" / "contact_labels"
LABEL_SOURCE = "manual_visual_review"
SHEET_COLUMNS = 4
SHEET_ROWS = 4
FRAMES_PER_SHEET = SHEET_COLUMNS * SHEET_ROWS
_MARGIN = 40
_GAP = 32
_LABEL_H = 88
_REPLAY_EPSILON = 1e-6

_FORBIDDEN_LABEL_WORDS = (
    "phase",
    "impact",
    "contact",
    "fault",
    "classifier",
    "prediction",
    "swing",
    "P1",
    "P2",
    "P3",
    "P4",
    "P5",
    "P6",
    "P7",
    "P8",
    "P9",
)


@dataclass(frozen=True)
class ReplayFrame:
    """One Down-the-Line frame at or after the stored replay start."""

    tick: int
    replay_seconds: float
    file: str


def replay_frames(meta: Mapping[str, Any]) -> list[ReplayFrame]:
    """Replay-era Down-the-Line frames, oldest tick first.

    A frame is replay-era when its stored offset from replay start is at
    least zero. Phase names are not read.
    """

    timeline = meta.get("diagnostic_timeline")
    if not isinstance(timeline, dict):
        raise ValueError("shot has no diagnostic timeline")
    cameras = timeline.get("cameras")
    if not isinstance(cameras, list):
        raise ValueError("diagnostic timeline has no cameras")
    behind = next(
        (
            camera
            for camera in cameras
            if isinstance(camera, dict) and camera.get("camera_id") == "swing_behind"
        ),
        None,
    )
    raw_frames = behind.get("frames") if isinstance(behind, dict) else None
    if not isinstance(raw_frames, list) or not raw_frames:
        raise ValueError("diagnostic timeline has no swing_behind frames")
    frames: list[ReplayFrame] = []
    for raw in raw_frames:
        if not isinstance(raw, dict):
            continue
        replay = raw.get("offset_from_replay_start_seconds")
        if isinstance(replay, bool) or not isinstance(replay, int | float):
            continue
        if float(replay) < -_REPLAY_EPSILON:
            continue
        tick = raw.get("capture_index")
        file = str(raw.get("file") or "").replace("\\", "/")
        if isinstance(tick, bool) or not isinstance(tick, int):
            raise ValueError("replay-era frame has no capture_index")
        if not file.startswith("diagnostic/swing_behind/") or ".." in Path(file).parts:
            raise ValueError(f"replay-era frame {tick} is not a swing_behind file")
        frames.append(ReplayFrame(tick, float(replay), file))
    frames.sort(key=lambda frame: (frame.tick, frame.replay_seconds))
    if not frames:
        raise ValueError("shot has no replay-era Down-the-Line frames")
    return frames


def paginate(frames: Sequence[ReplayFrame]) -> list[list[ReplayFrame]]:
    """Chronological sheets of at most 16 frames, four columns by four rows."""

    if not frames:
        raise ValueError("no frames to page")
    return [
        list(frames[start : start + FRAMES_PER_SHEET])
        for start in range(0, len(frames), FRAMES_PER_SHEET)
    ]


def frame_caption(tick: int, replay_seconds: float) -> str:
    """The only text drawn on a review frame."""

    return f"tick {tick}\n{replay_seconds:+.2f} s"


def write_sheets(
    shot: Path,
    frames: Sequence[ReplayFrame],
    destination: Path,
) -> list[dict[str, Any]]:
    """Write numbered review sheets. Existing contact_label.json is left as it is."""

    pages = paginate(frames)
    destination.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, Any]] = []
    for number, page in enumerate(pages, start=1):
        path = destination / f"contact_sheet_{number:02d}.png"
        captions = [frame_caption(frame.tick, frame.replay_seconds) for frame in page]
        width, height = _save_page(shot, page, captions, path)
        written.append(
            {
                "path": str(path),
                "ticks": [frame.tick for frame in page],
                "captions": captions,
                "width": width,
                "height": height,
            }
        )
    for leftover in destination.glob("contact_sheet_*.png"):
        suffix = leftover.stem.removeprefix("contact_sheet_")
        if suffix.isdigit() and int(suffix) > len(pages):
            leftover.unlink()
    return written


def write_contact_label(
    shot_id: str,
    frames: Sequence[ReplayFrame],
    destination: Path,
    *,
    contact_tick: int | None = None,
    contact_between: tuple[int, int] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Write one manual label. Exactly one of the two contact fields is set."""

    if (contact_tick is None) == (contact_between is None):
        raise ValueError("pass exactly one of contact_tick and contact_between")
    known = {frame.tick for frame in frames}
    document: dict[str, Any] = {
        "shot_id": shot_id,
        "label_source": LABEL_SOURCE,
        "created_at": created_at or datetime.now(UTC).isoformat(),
    }
    if contact_tick is not None:
        _require_ticks((contact_tick,), known)
        document["contact_tick"] = contact_tick
    else:
        assert contact_between is not None
        _require_between(contact_between[0], contact_between[1], known)
        document["contact_between"] = [contact_between[0], contact_between[1]]
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "contact_label.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return document


def load_contact_label(
    path: Path, shot_id: str, known_ticks: set[int]
) -> dict[str, Any]:
    """Read a stored label. Extra fields and a missing contact field are rejected."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a contact label")
    allowed = {"shot_id", "label_source", "created_at", "contact_tick", "contact_between"}
    extra = set(raw) - allowed
    if extra:
        raise ValueError(f"contact label contains {sorted(extra)}")
    if raw.get("shot_id") != shot_id:
        raise ValueError(f"contact label shot_id is {raw.get('shot_id')!r}")
    if raw.get("label_source") != LABEL_SOURCE:
        raise ValueError("contact label source is not a manual visual review")
    has_tick = "contact_tick" in raw
    has_between = "contact_between" in raw
    if has_tick == has_between:
        raise ValueError("contact label needs exactly one of contact_tick and contact_between")
    if has_tick:
        tick = raw["contact_tick"]
        if isinstance(tick, bool) or not isinstance(tick, int):
            raise ValueError("contact_tick is not an integer")
        _require_ticks((tick,), known_ticks)
    else:
        pair = raw["contact_between"]
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(isinstance(item, bool) or not isinstance(item, int) for item in pair)
        ):
            raise ValueError("contact_between must be two capture ticks")
        _require_between(pair[0], pair[1], known_ticks)
    return raw


def run(
    shot_id: str,
    *,
    contact_tick: int | None = None,
    contact_between: tuple[int, int] | None = None,
    shots_root: Path | None = None,
    labels_root: Path | None = None,
) -> dict[str, Any]:
    """Refresh the review sheets. Write a label only when one was passed."""

    if contact_tick is not None and contact_between is not None:
        raise SystemExit("pass only one of --contact-tick and --contact-between")
    shots = coach.SHOTS if shots_root is None else shots_root
    labels = LABELS if labels_root is None else labels_root
    shot = coach.find_shot(shot_id, shots)
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    frames = replay_frames(meta)
    destination = labels / shot.name
    sheets = write_sheets(shot, frames, destination)
    label = None
    if contact_tick is not None or contact_between is not None:
        label = write_contact_label(
            shot.name,
            frames,
            destination,
            contact_tick=contact_tick,
            contact_between=contact_between,
        )
    return {
        "shot_id": shot.name,
        "frame_count": len(frames),
        "ticks": [frame.tick for frame in frames],
        "replay_seconds": [frame.replay_seconds for frame in frames],
        "sheets": sheets,
        "contact_label": label,
        "label_path": str(destination / "contact_label.json"),
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Write Down-the-Line review sheets. "
            "A contact label is written only when you pass one."
        )
    )
    parser.add_argument("shot", help="fresh_v2 shot id")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--contact-tick",
        type=int,
        help="capture tick of club-ball contact, from visual review",
    )
    group.add_argument(
        "--contact-between",
        type=int,
        nargs=2,
        metavar=("N", "M"),
        help="contact is after tick N and before tick M",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    between = tuple(args.contact_between) if args.contact_between else None
    result = run(
        args.shot,
        contact_tick=args.contact_tick,
        contact_between=between,
    )
    first = result["replay_seconds"][0]
    last = result["replay_seconds"][-1]
    print(f"shot: {result['shot_id']}")
    print(f"replay_frames: {result['frame_count']}")
    print(f"ticks: {result['ticks'][0]}..{result['ticks'][-1]}")
    print(f"replay_time: {first:+.4f} s .. {last:+.4f} s")
    for sheet in result["sheets"]:
        print(f"sheet: {sheet['path']} {sheet['width']}x{sheet['height']}")
    if result["contact_label"] is None:
        print("contact_label: not written")
    else:
        print(f"contact_label: {result['label_path']}")


def _require_ticks(ticks: Sequence[int], known: set[int]) -> None:
    missing = [tick for tick in ticks if tick not in known]
    if missing:
        raise SystemExit(
            f"capture tick {missing[0]} is not in the replay-era timeline"
        )


def _require_between(start: int, end: int, known: set[int]) -> None:
    if end <= start:
        raise SystemExit("contact interval requires the later tick to be higher")
    _require_ticks((start, end), known)


def _save_page(
    shot: Path,
    page: Sequence[ReplayFrame],
    captions: Sequence[str],
    path: Path,
) -> tuple[int, int]:
    images: list[Image.Image] = []
    try:
        for frame in page:
            file = _frame_file(shot, frame.file)
            opened = Image.open(file)
            opened.load()
            rgb = opened.convert("RGB")
            if rgb is not opened:
                opened.close()
            images.append(rgb)
        return _draw_page(path, images, captions)
    finally:
        for image in images:
            image.close()


def _draw_page(
    path: Path, images: Sequence[Image.Image], captions: Sequence[str]
) -> tuple[int, int]:
    columns = SHEET_COLUMNS
    rows = (len(images) + columns - 1) // columns
    if rows > SHEET_ROWS:
        raise ValueError("a contact sheet has more than four rows")
    cell_w = max(image.width for image in images)
    cell_h = max(image.height for image in images)
    row_h = _LABEL_H + cell_h
    width = _MARGIN * 2 + columns * cell_w + (columns - 1) * _GAP
    height = _MARGIN * 2 + rows * row_h + max(0, rows - 1) * _GAP
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    font = _font(32)
    for index, (image, caption) in enumerate(zip(images, captions, strict=True)):
        column = index % columns
        row = index // columns
        x = _MARGIN + column * (cell_w + _GAP)
        y = _MARGIN + row * (row_h + _GAP)
        _draw_caption(draw, caption, x, y, cell_w, font)
        paste_x = x + (cell_w - image.width) // 2
        paste_y = y + _LABEL_H + (cell_h - image.height) // 2
        canvas.paste(image, (paste_x, paste_y))
    temporary = path.with_suffix(path.suffix + ".tmp")
    canvas.save(temporary, format="PNG")
    temporary.replace(path)
    return width, height


def _draw_caption(
    draw: ImageDraw.ImageDraw,
    caption: str,
    x: int,
    y: int,
    cell_w: int,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    lines = caption.split("\n")
    boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    heights = [box[3] - box[1] for box in boxes]
    gap = 4
    block = sum(heights) + gap * (len(lines) - 1)
    cursor = y + (_LABEL_H - block) / 2
    for line, box, line_h in zip(lines, boxes, heights, strict=True):
        text_w = box[2] - box[0]
        draw.text(
            (x + (cell_w - text_w) / 2, cursor - box[1]),
            line,
            fill=(16, 16, 16),
            font=font,
        )
        cursor += line_h + gap


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        file = Path(r"C:\Windows\Fonts") / name
        if file.is_file():
            return ImageFont.truetype(str(file), size)
    return ImageFont.load_default()


def _frame_file(shot: Path, relative: str) -> Path:
    path = (shot / relative).resolve()
    root = (shot / "diagnostic").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"frame path escapes the shot: {relative}") from exc
    if not path.is_file():
        raise ValueError(f"missing frame {relative}")
    return path


if __name__ == "__main__":
    main()
