"""A Swing Evidence Sheet for tests whose stills are bytes, not pictures.

Most coaching tests write a few bytes with a JPEG header as each still. Those
cannot be decoded, and should not have to be: what those tests check is what
the coach does with the cells, not what Pillow draws. This compose lays the
sheet out for real, from the sizes the capture recorded, and encodes it as a
short placeholder naming its cells. Everything else -- reading the originals,
storing `swing_evidence.jpg`, the cell table in metadata.json -- is the
production path. `test_evidence_sheet.py` draws real sheets from real JPEGs.
"""

from __future__ import annotations

from collections.abc import Sequence

from caddiej.domain.media import SwingCapture
from caddiej.swing_capture.sheet import (
    MAX_SIDE,
    ComposedSheet,
    EvidenceSheet,
    SheetCell,
    layout_sheet,
    render_evidence_sheet,
)

PLACEHOLDER = b"\xff\xd8SHEET:"


def placeholder_compose(
    items: Sequence[tuple[SheetCell, bytes]], max_side: int = MAX_SIDE
) -> ComposedSheet:
    cells = tuple(cell for cell, _ in items)
    layout = layout_sheet(
        [(cell, cell.width or 640, cell.height or 360) for cell in cells], max_side
    )
    data = PLACEHOLDER + ",".join(cell.cell_id for cell in cells).encode()
    return ComposedSheet(data=data, layout=layout, cells=cells)


async def placeholder_sheet(
    capture: SwingCapture, cells: Sequence[SheetCell]
) -> EvidenceSheet | None:
    return await render_evidence_sheet(capture, cells, compose=placeholder_compose)


def sheet_cells(data: bytes) -> tuple[str, ...]:
    """The cell IDs a placeholder sheet carries, in order."""

    assert data.startswith(PLACEHOLDER), "not a placeholder evidence sheet"
    names = data[len(PLACEHOLDER) :].decode()
    return tuple(names.split(",")) if names else ()
