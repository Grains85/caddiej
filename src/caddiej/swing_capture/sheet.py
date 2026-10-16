"""The Swing Evidence Sheet: one composed image per shot, for the model only.

A shot's selected stills used to reach OpenAI as up to seventeen separate
images, and two consecutive requests on the bay ran out the thirty-second
timeout. They now reach it as ONE image: every selected still, the impact
graphic and the All Values grid drawn onto a single canvas, each in its own
cell under a short printed ID (`DTL_P5`, `FO_P8`, `IMPACT`, `ALL_VALUES`).

**The sheet is transport, never evidence.** The originals written by the
capture stay exactly where they are, untouched, and remain the canonical
evidence: a checkpoint, a fault and a root cause all keep the id of the
original frame they were read from (`swing_behind_05`), never a cell id and
never a crop of this picture. The sheet is a derived file beside them, made so
the model can be shown everything at once, and `cells` is the deterministic
table from each printed ID back to the original it shows. A future screen that
wants to show the player "the frame behind TRAIL SHOULDER OUT" asks that table
-- or `original_for_cell` -- and opens the original.

**The model's limit decides the size.** gpt-5-mini fits every image inside
2048 x 2048 pixels before reading it (measured on 2026-10-07: a 3072 square and
a 2048 square cost the same 4916 tokens, a 4096 x 1024 strip costs as much as
2048 x 512), so a larger canvas would only be shrunk by the API, out of sight.
The layout therefore searches for the arrangement that keeps the SMALLEST cell
as large as possible inside that square, and is honest about the cost: seventeen
images reached the model as roughly 26 megapixels and one image is at most 4.2,
so every swing still is shown at roughly 0.3 of its captured size. Nothing is
stretched, nothing is upscaled, and nothing is cropped -- the club sweeps most
of each frame over a swing, and no fixed crop is safe for every phase.

**An aimed phase is printed as an aim.** A cell reads `DTL_P5 ~transition
@3.8s`: the camera, the moment CaddieJ aimed at, and the time on that camera's
own clock. The tilde means what it meant in `stills.frame_label`. The model
still says what each cell really shows, and a checkpoint read from a cell that
shows another moment is still refused.

Pillow is imported only inside `compose_sheet`, because it arrives with the
`ai` extra. Without it no sheet can be drawn, `render_evidence_sheet` returns
None, and the coach falls back to reading the numbers alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import math
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from caddiej.domain.cues import PanelKind, PanelView
from caddiej.domain.media import SwingCapture
from caddiej.domain.swing_phase import PHASE_ORDER, SwingPhase
from caddiej.swing_capture.stills import provider_frames
from caddiej.swing_capture.store import (
    METADATA_NAME,
    METRICS_FRAME_ID,
    frame_id,
    frame_path,
)

logger = logging.getLogger(__name__)

SHEET_FILE = "swing_evidence.jpg"
SHEET_LABEL = "SWING_EVIDENCE_SHEET"
SHEET_PANEL_ID = "evidence_sheet"
METRICS_CELL = "ALL_VALUES"
IMPACT_CELL = "IMPACT"

MAX_SIDE = 2048
"""gpt-5-mini's own limit; see the module docstring."""

JPEG_QUALITY = 90
"""High, with full-resolution colour (4:4:4). A real shot's sheet is about
1 MB; the stills it is drawn from are already JPEG, and a second aggressive
encode is how a shaft turns into a smear."""

_MAX_STILL_BYTES = 8 * 1024 * 1024
_MARGIN = 8
_GAP = 8
_BLOCK_GAP = 16
_LABEL = 30
_HEADER = 36
_STRIP_SHARE = 0.25
_ID_FONT = 22
_CAPTION_FONT = 18
_HEADER_FONT = 19
_BACKGROUND = (24, 24, 24)
_BANDS: Mapping[str, tuple[int, int, int]] = {
    "behind": (25, 78, 140),
    "side": (36, 110, 50),
    "panel": (70, 70, 70),
    "metrics": (110, 70, 20),
}
_HEADER_TEXT = (
    "CADDIEJ SWING EVIDENCE    DTL = SWING_BEHIND, down the line    "
    "FO = SWING_SIDE, face on    ~phase = aimed by the clock, not verified    "
    "DTL and FO are separate recordings on separate clocks"
)


class CellKind(StrEnum):
    BODY = "body"
    """A swing camera: the only kind a checkpoint may be read from."""

    PANEL = "panel"
    """A static ProTee graphic, such as the impact view."""

    METRICS = "metrics"
    """The All Values grid: numbers, never a body."""


@dataclass(frozen=True, slots=True)
class SheetCell:
    """One picture on the sheet, and the original it was drawn from.

    `frame_id` is the canonical id of that original (`swing_behind_05`,
    `impact_01`, `all_values`) and `path` is where it lives inside the shot's
    own directory. `cell_id` is only what is printed on the sheet.
    """

    cell_id: str
    frame_id: str
    path: Path
    kind: CellKind
    camera_id: str
    view: PanelView = PanelView.NONE
    phase: SwingPhase | None = None
    offset_seconds: float | None = None
    width: int = 0
    height: int = 0
    label: str = ""
    """The still's own label (`SWING_SIDE / FACE_ON ~transition @3.8s`), as
    the per-image transport announced it; what the prompt lists beside the ID."""

    @property
    def caption(self) -> str:
        """What is printed after the ID, and listed beside it in the prompt."""

        if self.kind is CellKind.METRICS:
            return "ProTee All Values grid (numbers only)"
        if self.kind is CellKind.PANEL:
            return f"ProTee {self.camera_id} graphic"
        if self.phase is None:
            return "" if self.offset_seconds is None else f"@{self.offset_seconds:.1f}s"
        return f"~{self.phase.value} @{self.offset_seconds or 0.0:.1f}s"

    @property
    def title(self) -> str:
        caption = self.caption
        return f"{self.cell_id} {caption}" if caption else self.cell_id


# --- which cells, and what they are called -----------------------------------


def _prefix(view: PanelView, camera_id: str) -> str:
    if view is PanelView.BEHIND:
        return "DTL"
    if view is PanelView.SIDE:
        return "FO"
    return camera_id.upper()


def _unique(wanted: str, used: set[str]) -> str:
    name = wanted
    suffix = 2
    while name in used:
        name = f"{wanted}_{suffix}"
        suffix += 1
    used.add(name)
    return name


def plan_cells(capture: SwingCapture, limit: int) -> tuple[SheetCell, ...]:
    """Every picture this shot's sheet will carry, in sheet order. Pure.

    The swing and panel stills are exactly the ones the per-image transport
    sent -- `provider_frames`, under the same `limit` -- so the sheet changes
    how the evidence travels and never which evidence it is. The All Values
    still is added on top and, as before, never counts against `limit`.

    A swing still's ID is its camera and the checkpoint position it was AIMED
    at: `DTL_P5` is the down-the-line still aimed at the transition, `FO_P8`
    the face-on still aimed at the release. The same input always gives the
    same IDs. A still with no aim has its file's position instead
    (`DTL_03`), which can never be mistaken for a P-number.
    """

    cameras = {camera.camera_id: camera for camera in capture.cameras}
    used: set[str] = set()
    cells: list[SheetCell] = []
    for frame, label in provider_frames(capture, limit):
        camera = cameras.get(frame.camera_id)
        if camera is None:
            continue
        static = camera.kind is PanelKind.STATIC
        kind = (
            CellKind.PANEL if static or camera.view is PanelView.NONE else CellKind.BODY
        )
        prefix = _prefix(camera.view, camera.camera_id)
        if kind is CellKind.PANEL:
            wanted = prefix
        elif frame.phase is not None:
            wanted = f"{prefix}_P{PHASE_ORDER.index(frame.phase) + 1}"
        else:
            wanted = f"{prefix}_{frame.index:02d}"
        cells.append(
            SheetCell(
                cell_id=_unique(wanted, used),
                frame_id=frame_id(frame),
                path=frame.path,
                kind=kind,
                camera_id=camera.camera_id,
                view=camera.view,
                phase=frame.phase,
                offset_seconds=frame.offset_seconds,
                width=frame.width,
                height=frame.height,
                label=label,
            )
        )
    metrics = capture.metrics
    if metrics is not None and metrics.ok and metrics.path is not None:
        cells.append(
            SheetCell(
                cell_id=_unique(METRICS_CELL, used),
                frame_id=METRICS_FRAME_ID,
                path=metrics.path,
                kind=CellKind.METRICS,
                camera_id="coach_metrics",
                width=metrics.width,
                height=metrics.height,
                label=METRICS_CELL,
            )
        )
    return tuple(cells)


# --- where each cell goes ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Box:
    x: int
    y: int
    width: int
    height: int

    def as_list(self) -> list[int]:
        return [self.x, self.y, self.width, self.height]


@dataclass(frozen=True, slots=True)
class Placement:
    """Where one cell's label and picture were drawn, in sheet pixels."""

    cell_id: str
    label: Box
    image: Box
    scale: float
    """The picture's size on the sheet over its captured size."""


@dataclass(frozen=True, slots=True)
class SheetLayout:
    width: int
    height: int
    header: Box
    placements: tuple[Placement, ...]

    def placement(self, cell_id: str) -> Placement | None:
        for item in self.placements:
            if item.cell_id == cell_id:
                return item
        return None


@dataclass(frozen=True, slots=True)
class _Block:
    cells: tuple[tuple[SheetCell, int, int], ...]
    aspect: float
    """Slot width over slot height, from the block's first swing still."""

    reference: int
    """That still's captured height: the size at which a slot is scale 1."""


def _block(cells: Sequence[tuple[SheetCell, int, int]]) -> _Block:
    body = [item for item in cells if item[0].kind is CellKind.BODY]
    _cell, width, height = (body or list(cells))[0]
    return _Block(cells=tuple(cells), aspect=width / height, reference=height)


def _rows(block: _Block, columns: int) -> int:
    return math.ceil(len(block.cells) / columns)


def _largest(fits: Callable[[float], bool]) -> float:
    """The largest scale in (0, 1] that `fits`, to well under a pixel."""

    if fits(1.0):
        return 1.0
    low, high = 0.0, 1.0
    for _ in range(40):
        middle = (low + high) / 2
        if fits(middle):
            low = middle
        else:
            high = middle
    return low


def _side_by_side(
    blocks: Sequence[_Block], columns: Sequence[int], width: int, height: int
) -> list[int] | None:
    caps = []
    for block, count in zip(blocks, columns, strict=True):
        rows = _rows(block, count)
        caps.append((height - (rows - 1) * _GAP) / rows - _LABEL)
    if any(cap < 1 for cap in caps):
        return None

    def heights(scale: float) -> list[float]:
        return [
            min(scale * block.reference, cap, block.reference)
            for block, cap in zip(blocks, caps, strict=True)
        ]

    def fits(scale: float) -> bool:
        used = (
            sum(
                count * math.floor(block.aspect * h) + (count - 1) * _GAP
                for block, count, h in zip(blocks, columns, heights(scale), strict=True)
            )
            + (len(blocks) - 1) * _BLOCK_GAP
        )
        return used <= width

    return [math.floor(h) for h in heights(_largest(fits))]


def _stacked(
    blocks: Sequence[_Block], columns: Sequence[int], width: int, height: int
) -> list[int] | None:
    caps = [
        (width - (count - 1) * _GAP) / (count * block.aspect)
        for block, count in zip(blocks, columns, strict=True)
    ]

    def heights(scale: float) -> list[float]:
        return [
            min(scale * block.reference, cap, block.reference)
            for block, cap in zip(blocks, caps, strict=True)
        ]

    def fits(scale: float) -> bool:
        used = (
            sum(
                _rows(block, count) * (math.floor(h) + _LABEL)
                + (_rows(block, count) - 1) * _GAP
                for block, count, h in zip(blocks, columns, heights(scale), strict=True)
            )
            + (len(blocks) - 1) * _BLOCK_GAP
        )
        return used <= height

    if not fits(0.0):
        return None
    return [math.floor(h) for h in heights(_largest(fits))]


def layout_sheet(
    cells: Sequence[tuple[SheetCell, int, int]], max_side: int = MAX_SIDE
) -> SheetLayout:
    """Where every cell goes, given each picture's real pixel size. Pure.

    Down-the-line stills form one block, face-on stills and the static panels
    another, and the All Values grid runs full width along the bottom, where it
    is shown no smaller than the API would show it alone. Every way of putting
    the two blocks side by side or one above the other, with every column
    count, is tried, and the winner is the one whose smallest swing still is
    largest; a tie goes to the larger down-the-line still, because the shaft
    checkpoints are read there, and then to the most picture overall. The
    canvas is then trimmed to what was drawn, so unused rows cost no tokens.
    """

    if not cells:
        raise ValueError("an evidence sheet needs at least one picture")
    for _cell, width, height in cells:
        if width <= 0 or height <= 0:
            raise ValueError("every picture needs a real size")
    behind = [item for item in cells if item[0].view is PanelView.BEHIND]
    rest = [
        item
        for item in cells
        if item[0].view is not PanelView.BEHIND and item[0].kind is not CellKind.METRICS
    ]
    strip = [item for item in cells if item[0].kind is CellKind.METRICS]
    blocks = [_block(group) for group in (behind, rest) if group]

    inner = max_side - 2 * _MARGIN
    strip_sizes = []
    for _cell, width, height in strip:
        scale = min(1.0, inner / width, max_side * _STRIP_SHARE / height)
        strip_sizes.append(
            (max(1, math.floor(width * scale)), max(1, math.floor(height * scale)))
        )
    strip_height = sum(_LABEL + h for _w, h in strip_sizes) + _GAP * len(strip_sizes)
    body_height = max_side - 2 * _MARGIN - _HEADER - _GAP - strip_height

    chosen: tuple[tuple[float, float, float], str, list[int], list[int]] | None = None
    if blocks:
        for arrangement in ("side_by_side", "stacked"):
            if arrangement == "stacked" and len(blocks) < 2:
                continue
            counts = [range(1, len(block.cells) + 1) for block in blocks]
            for columns in _product(counts):
                place = _side_by_side if arrangement == "side_by_side" else _stacked
                heights = place(blocks, columns, inner, body_height)
                if heights is None or any(h < 1 for h in heights):
                    continue
                scales = [h / block.reference for h, block in zip(heights, blocks)]
                area = sum(
                    len(block.cells) * h * math.floor(block.aspect * h)
                    for block, h in zip(blocks, heights)
                )
                key = (round(min(scales), 2), scales[0], float(area))
                if chosen is None or key > chosen[0]:
                    chosen = (key, arrangement, list(columns), heights)
        if chosen is None:
            raise ValueError("the pictures do not fit on one sheet")

    sized: list[tuple[_Block, int, int, int]] = []
    if chosen is not None:
        _key, arrangement, picked, picked_heights = chosen
        for block, count, h in zip(blocks, picked, picked_heights, strict=True):
            sized.append((block, count, h, math.floor(block.aspect * h)))
    else:
        arrangement = "side_by_side"

    def block_width(item: tuple[_Block, int, int, int]) -> int:
        _block_, count, _h, slot = item
        return count * slot + (count - 1) * _GAP

    def block_height(item: tuple[_Block, int, int, int]) -> int:
        block, count, h, _slot = item
        rows = _rows(block, count)
        return rows * (h + _LABEL) + (rows - 1) * _GAP

    if arrangement == "side_by_side":
        body_w = sum(block_width(item) for item in sized) + max(0, len(sized) - 1) * (
            _BLOCK_GAP
        )
        body_h = max((block_height(item) for item in sized), default=0)
    else:
        body_w = max(block_width(item) for item in sized)
        body_h = sum(block_height(item) for item in sized) + (len(sized) - 1) * (
            _BLOCK_GAP
        )
    content_w = max([body_w, *(w for w, _h in strip_sizes)])
    canvas_w = content_w + 2 * _MARGIN

    placements: list[Placement] = []
    top = _MARGIN + _HEADER + _GAP
    if arrangement == "side_by_side":
        x = _MARGIN + (content_w - body_w) // 2
        for item in sized:
            placements.extend(_place_block(item, x, top))
            x += block_width(item) + _BLOCK_GAP
    else:
        y = top
        for item in sized:
            x = _MARGIN + (content_w - block_width(item)) // 2
            placements.extend(_place_block(item, x, y))
            y += block_height(item) + _BLOCK_GAP
    y = top + body_h + (_GAP if sized else 0)
    for (cell, width, _height), (w, h) in zip(strip, strip_sizes):
        x = _MARGIN + (content_w - w) // 2
        placements.append(
            Placement(
                cell_id=cell.cell_id,
                label=Box(x, y, w, _LABEL),
                image=Box(x, y + _LABEL, w, h),
                scale=w / width,
            )
        )
        y += _LABEL + h + _GAP
    canvas_h = y - (_GAP if strip else 0) + _MARGIN
    return SheetLayout(
        width=canvas_w,
        height=canvas_h,
        header=Box(_MARGIN, _MARGIN, content_w, _HEADER),
        placements=tuple(placements),
    )


def _product(ranges: Sequence[range]) -> list[tuple[int, ...]]:
    combos: list[tuple[int, ...]] = [()]
    for choices in ranges:
        combos = [(*combo, choice) for combo in combos for choice in choices]
    return combos


def _place_block(
    item: tuple[_Block, int, int, int], left: int, top: int
) -> list[Placement]:
    block, columns, height, slot = item
    placed = []
    for position, (cell, width, src_height) in enumerate(block.cells):
        row, column = divmod(position, columns)
        x = left + column * (slot + _GAP)
        y = top + row * (height + _LABEL + _GAP)
        scale = min(slot / width, height / src_height, 1.0)
        w = max(1, math.floor(width * scale))
        h = max(1, math.floor(src_height * scale))
        placed.append(
            Placement(
                cell_id=cell.cell_id,
                label=Box(x, y, slot, _LABEL),
                image=Box(x + (slot - w) // 2, y + _LABEL + (height - h) // 2, w, h),
                scale=w / width,
            )
        )
    return placed


# --- drawing it --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ComposedSheet:
    """The encoded picture, before it is stored."""

    data: bytes
    layout: SheetLayout
    cells: tuple[SheetCell, ...]
    dropped: tuple[str, ...] = ()


Compose = Callable[[Sequence[tuple[SheetCell, bytes]], int], ComposedSheet]


def _band(cell: SheetCell) -> tuple[int, int, int]:
    if cell.kind is CellKind.METRICS:
        return _BANDS["metrics"]
    if cell.view is PanelView.BEHIND:
        return _BANDS["behind"]
    if cell.view is PanelView.SIDE:
        return _BANDS["side"]
    return _BANDS["panel"]


def compose_sheet(
    items: Sequence[tuple[SheetCell, bytes]], max_side: int = MAX_SIDE
) -> ComposedSheet:
    """Decode, lay out, draw and encode. Raises if nothing could be drawn.

    A picture Pillow cannot open is left off the sheet and named in `dropped`,
    and so cannot be cited: an ID on the sheet always has a picture under it.
    """

    from PIL import Image, ImageDraw, ImageFont

    opened: list[tuple[SheetCell, Any]] = []
    dropped: list[str] = []
    for cell, data in items:
        try:
            opened.append((cell, Image.open(io.BytesIO(data))))
        except Exception:
            dropped.append(cell.cell_id)
    while opened:
        layout = layout_sheet(
            [(cell, image.width, image.height) for cell, image in opened], max_side
        )
        canvas = Image.new("RGB", (layout.width, layout.height), _BACKGROUND)
        draw = ImageDraw.Draw(canvas)
        id_font = ImageFont.load_default(size=_ID_FONT)
        caption_font = ImageFont.load_default(size=_CAPTION_FONT)
        header_font = ImageFont.load_default(size=_HEADER_FONT)
        failed: str | None = None
        for cell, image in opened:
            placement = layout.placement(cell.cell_id)
            assert placement is not None
            box = placement.image
            try:
                image.draft("RGB", (box.width, box.height))
                picture = image.convert("RGB")
                if picture.size != (box.width, box.height):
                    picture = picture.resize(
                        (box.width, box.height), Image.Resampling.LANCZOS
                    )
            except Exception:
                failed = cell.cell_id
                break
            canvas.paste(picture, (box.x, box.y))
            label = placement.label
            draw.rectangle(
                (
                    label.x,
                    label.y,
                    label.x + label.width - 1,
                    label.y + label.height - 1,
                ),
                fill=_band(cell),
            )
            text_y = label.y + (label.height - _ID_FONT) // 2 - 1
            draw.text((label.x + 6, text_y), cell.cell_id, fill="white", font=id_font)
            caption_x = label.x + 6 + draw.textlength(cell.cell_id, font=id_font) + 10
            room = label.x + label.width - 4 - caption_x
            caption = _fit(draw, cell.caption, caption_font, room)
            if caption:
                draw.text(
                    (caption_x, label.y + (label.height - _CAPTION_FONT) // 2),
                    caption,
                    fill=(230, 230, 230),
                    font=caption_font,
                )
        if failed is not None:
            dropped.append(failed)
            opened = [item for item in opened if item[0].cell_id != failed]
            continue
        header = layout.header
        draw.text(
            (header.x + 4, header.y + (header.height - _HEADER_FONT) // 2),
            _fit(draw, _HEADER_TEXT, header_font, header.width - 8),
            fill=(240, 240, 240),
            font=header_font,
        )
        output = io.BytesIO()
        canvas.save(output, "JPEG", quality=JPEG_QUALITY, subsampling=0)
        return ComposedSheet(
            data=output.getvalue(),
            layout=layout,
            cells=tuple(cell for cell, _image in opened),
            dropped=tuple(dropped),
        )
    raise ValueError("no picture on this sheet could be decoded")


def _fit(draw: Any, text: str, font: Any, room: float) -> str:
    if room <= 0 or not text:
        return ""
    while text and draw.textlength(text, font=font) > room:
        text = text[:-1]
    return text.rstrip()


# --- the sheet as the coach holds it ------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceSheet:
    """The one image a request carries, and how to get from it to the originals."""

    data: bytes
    width: int
    height: int
    cells: tuple[SheetCell, ...]
    layout: SheetLayout
    file: Path | None = None
    """`swing_evidence.jpg`, relative to the shot's directory, once stored."""

    dropped: tuple[str, ...] = ()
    render_seconds: float = 0.0
    stored: bool = False
    quality: int = JPEG_QUALITY
    mirrored: bool = False

    @property
    def mapping(self) -> dict[str, str]:
        """Printed cell ID -> canonical original frame id, in sheet order."""

        return {cell.cell_id: cell.frame_id for cell in self.cells}

    @property
    def has_metrics(self) -> bool:
        return any(cell.kind is CellKind.METRICS for cell in self.cells)

    @property
    def pictures(self) -> tuple[SheetCell, ...]:
        """Every cell that is not the All Values grid."""

        return tuple(cell for cell in self.cells if cell.kind is not CellKind.METRICS)

    def cell(self, cell_id: str) -> SheetCell | None:
        for cell in self.cells:
            if cell.cell_id == cell_id:
                return cell
        return None

    def metadata(self) -> dict[str, object]:
        """What `metadata.json` keeps: enough to go from any printed ID to the
        original file without opening the sheet."""

        cells: list[dict[str, object]] = []
        for cell in self.cells:
            placement = self.layout.placement(cell.cell_id)
            cells.append(
                {
                    "cell_id": cell.cell_id,
                    "frame_id": cell.frame_id,
                    "file": cell.path.as_posix(),
                    "kind": cell.kind.value,
                    "camera_id": cell.camera_id,
                    "view": cell.view.value,
                    "aimed_phase": None if cell.phase is None else cell.phase.value,
                    "offset_seconds": cell.offset_seconds,
                    "box": None if placement is None else placement.image.as_list(),
                    "scale": None if placement is None else round(placement.scale, 3),
                }
            )
        return {
            "file": None if self.file is None else self.file.as_posix(),
            "role": "transport",
            "canonical_evidence": "the original frames named in cells",
            "format": "jpeg",
            "quality": self.quality,
            "chroma": "4:4:4",
            "width": self.width,
            "height": self.height,
            "bytes": len(self.data),
            "render_ms": round(self.render_seconds * 1000),
            "cells": cells,
            "dropped": list(self.dropped),
        }


async def render_evidence_sheet(
    capture: SwingCapture,
    cells: Sequence[SheetCell],
    *,
    compose: Compose = compose_sheet,
    max_side: int = MAX_SIDE,
) -> EvidenceSheet | None:
    """Draw this shot's sheet from its originals and store it beside them.

    Never raises. Every way this can fail -- no Pillow, a file gone, a
    picture that will not decode, a full disk -- is a log line and None, and
    the coach then works from what it has. A sheet that was drawn but could
    not be stored is still returned: the request does not depend on the file.
    """

    if not cells:
        return None
    started = time.monotonic()
    try:
        sheet = await asyncio.to_thread(
            _render, capture, tuple(cells), compose, max_side
        )
    except Exception as error:
        logger.warning(
            "swing_capture_evidence_sheet_failed",
            extra={
                "shot_trigger_id": capture.trigger_id,
                "swing_capture_directory": str(capture.directory),
                "swing_capture_detail": f"{type(error).__name__}: {error}"[:200],
            },
        )
        return None
    return replace(sheet, render_seconds=time.monotonic() - started)


def _render(
    capture: SwingCapture,
    cells: tuple[SheetCell, ...],
    compose: Compose,
    max_side: int,
) -> EvidenceSheet:
    items: list[tuple[SheetCell, bytes]] = []
    unreadable: list[str] = []
    for cell in cells:
        data = _read_original(capture, cell)
        if data is None:
            unreadable.append(cell.cell_id)
        else:
            items.append((cell, data))
    if not items:
        raise FileNotFoundError("none of this shot's originals could be read")
    composed = compose(items, max_side)
    stored = _store(capture.directory, composed.data)
    mirrored = False
    if stored and capture.published is not None:
        mirrored = _mirror(capture.published, capture.trigger_id, composed.data)
    return EvidenceSheet(
        data=composed.data,
        width=composed.layout.width,
        height=composed.layout.height,
        cells=composed.cells,
        layout=composed.layout,
        file=Path(SHEET_FILE) if stored else None,
        dropped=(*unreadable, *composed.dropped),
        stored=stored,
        mirrored=mirrored,
    )


def _read_original(capture: SwingCapture, cell: SheetCell) -> bytes | None:
    path = frame_path(capture, cell.frame_id)
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data or len(data) > _MAX_STILL_BYTES:
        return None
    return data


def _store(directory: Path, data: bytes) -> bool:
    """Write the sheet beside the originals, whole or not at all."""

    target = directory / SHEET_FILE
    temporary = directory / f".{SHEET_FILE}.{uuid4().hex[:8]}.tmp"
    try:
        temporary.write_bytes(data)
        os.replace(temporary, target)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        logger.warning(
            "swing_capture_evidence_sheet_unstored",
            extra={"swing_capture_directory": str(directory)},
            exc_info=True,
        )
        return False
    return True


def _mirror(latest: Path, trigger_id: str, data: bytes) -> bool:
    """A copy in `latest_swing` for people to look at, if it is still this shot.

    Never read back by anything. A newer swing may already have replaced the
    directory, and an older shot's sheet must not appear beside its stills.
    """

    try:
        payload = json.loads((latest / METADATA_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("trigger_id") != trigger_id:
        return False
    return _store(latest, data)


# --- from a printed ID back to the original -----------------------------------


def stored_cells(directory: Path) -> dict[str, dict[str, Any]]:
    """The cell table a stored shot recorded, by printed ID. Empty if none."""

    try:
        payload = json.loads((directory / METADATA_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    sheet = payload.get("evidence_sheet") if isinstance(payload, dict) else None
    cells = sheet.get("cells") if isinstance(sheet, dict) else None
    if not isinstance(cells, list):
        return {}
    return {
        str(item["cell_id"]): item
        for item in cells
        if isinstance(item, dict) and isinstance(item.get("cell_id"), str)
    }


def original_for_cell(capture: SwingCapture, cell_id: str) -> Path | None:
    """The canonical original behind a printed ID, or None.

    Resolved through the stored table and then `frame_path`, which checks the
    answer is a real file inside this shot's own directory. Never the sheet,
    and never a crop of it.
    """

    entry = stored_cells(capture.directory).get(cell_id)
    if entry is None:
        return None
    frame = entry.get("frame_id")
    if not isinstance(frame, str):
        return None
    return frame_path(capture, frame)
