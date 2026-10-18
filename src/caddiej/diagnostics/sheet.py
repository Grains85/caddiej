"""A contact sheet of the frames a shot already selected.

The sheet is a new image. The evidence files it reads are not rewritten, and
nothing here is attached to a model request.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_CAMERA_LABELS = {
    "swing_behind": "Down the line (swing_behind)",
    "swing_side": "Face on (swing_side)",
}
_DTL = "swing_behind"
_FO = "swing_side"
_CELL_W = 220
_THUMB_H = 140
_CAPTION_H = 78
_PAD = 12


def render_contact_sheet(
    frames: list[dict[str, Any]],
    impact: dict[str, Any] | None,
    destination: Path,
) -> None:
    """Draw selected frames into `destination`.

    Raises when no frame can be drawn. The caller records that as a sheet
    failure and continues.
    """

    from PIL import Image, ImageDraw, ImageFont

    groups = _groups(frames)
    if not any(groups.values()):
        raise ValueError("no selected frames to draw")
    font = ImageFont.load_default()
    sections = _sections(groups)
    width = _PAD * 2 + _columns(sections) * (_CELL_W + _PAD)
    height = _PAD
    layouts: list[tuple[str, list[list[dict[str, Any]]]]] = []
    for title, rows in sections:
        layouts.append((title, rows))
        height += 28
        height += len(rows) * (_THUMB_H + _CAPTION_H + _PAD)
        height += _PAD
    canvas = Image.new("RGB", (max(width, 360), max(height, 80)), (248, 248, 246))
    draw = ImageDraw.Draw(canvas)
    y = _PAD
    drawn = 0
    for title, rows in layouts:
        draw.text((_PAD, y), title, fill=(20, 20, 20), font=font)
        y += 28
        for row in rows:
            x = _PAD
            for frame in row:
                drawn += _cell(canvas, draw, font, frame, impact, x, y)
                x += _CELL_W + _PAD
            y += _THUMB_H + _CAPTION_H + _PAD
        y += _PAD
    if drawn == 0:
        raise ValueError("selected frame files could not be read")
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, format="JPEG", quality=85)


def _groups(frames: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    ordered = sorted(
        frames,
        key=lambda frame: (
            _number(frame.get("timestamp_seconds")),
            str(frame.get("camera_id") or ""),
            _number(frame.get("capture_tick")),
        ),
    )
    for frame in ordered:
        camera = str(frame.get("camera_id") or "unknown")
        grouped.setdefault(camera, []).append(frame)
    return grouped


def _sections(
    groups: dict[str, list[dict[str, Any]]],
) -> list[tuple[str, list[list[dict[str, Any]]]]]:
    order = [camera for camera in (_DTL, _FO) if camera in groups]
    order.extend(sorted(camera for camera in groups if camera not in {_DTL, _FO}))
    sections: list[tuple[str, list[list[dict[str, Any]]]]] = []
    for camera in order:
        label = _CAMERA_LABELS.get(camera, camera)
        frames = groups[camera]
        rows = [frames[index : index + 8] for index in range(0, len(frames), 8)]
        sections.append((label, rows))
    return sections


def _columns(sections: list[tuple[str, list[list[dict[str, Any]]]]]) -> int:
    width = 1
    for _title, rows in sections:
        for row in rows:
            width = max(width, len(row))
    return width


def _cell(
    canvas: Any,
    draw: Any,
    font: Any,
    frame: dict[str, Any],
    impact: dict[str, Any] | None,
    x: int,
    y: int,
) -> int:
    from PIL import Image

    marked = _impact_marked(frame, impact)
    border = (196, 98, 18) if marked else (210, 210, 206)
    draw.rectangle(
        (x, y, x + _CELL_W, y + _THUMB_H + _CAPTION_H),
        outline=border,
        width=3 if marked else 1,
    )
    thumb_box = (x + 6, y + 6, x + _CELL_W - 6, y + _THUMB_H - 6)
    path = frame.get("path")
    opened = False
    if path:
        try:
            with Image.open(path) as source:
                image = source.convert("RGB")
            image.thumbnail((_CELL_W - 12, _THUMB_H - 12))
            canvas.paste(image, (thumb_box[0], thumb_box[1]))
            opened = True
        except OSError:
            opened = False
    if not opened:
        draw.rectangle(thumb_box, fill=(230, 230, 228))
        draw.text((x + 12, y + 48), "image missing", fill=(80, 80, 80), font=font)
    stage = frame.get("stage")
    stage_line = "stage unknown" if not stage else str(stage)
    timestamp = frame.get("timestamp_seconds")
    when = "time unknown" if timestamp is None else f"{float(timestamp):.3f} s"
    lines = [
        when,
        str(frame.get("camera_id") or "camera unknown"),
        stage_line,
        str(frame.get("selection_reason") or "reason unknown"),
    ]
    if marked:
        lines.append("impact estimated")
    text_y = y + _THUMB_H + 4
    for line in lines:
        draw.text((x + 8, text_y), line[:40], fill=(20, 20, 20), font=font)
        text_y += 14
    return 1


def _impact_marked(frame: dict[str, Any], impact: dict[str, Any] | None) -> bool:
    if not impact or impact.get("quality") != "estimated":
        return False
    interval = impact.get("interval_ticks")
    tick = frame.get("capture_tick")
    if not isinstance(interval, list | tuple) or len(interval) != 2 or tick is None:
        return False
    try:
        start = int(interval[0])
        end = int(interval[1])
        value = int(tick)
    except (TypeError, ValueError):
        return False
    return start <= value <= end


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
