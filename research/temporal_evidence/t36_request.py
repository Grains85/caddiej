"""The vx14c request for a synchronized T36 sheet (T36S). Research tooling; never sends.

Exactly POC 4's request (system, evidence, typed group schema, model settings)
except the sentences that would now be false: the sheet's DTL_nn and FO_nn are
one capture tick, so nothing may say the views run on separate clocks or that
equal numbers are different moments. Unavailable cells are named as such and
are never offered as evidence.

    .venv\\Scripts\\python.exe research\\temporal_evidence\\t36_request.py <shot-prefix>

Writes output/t36/<shot>/request_{system,evidence}.txt and request_schema.json.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import poc4_vx14c as poc4  # noqa: E402
import poc_vx14 as poc1  # noqa: E402
import t36  # noqa: E402

from caddiej.coaching import vx  # noqa: E402
from caddiej.coaching.provider import ImageInput  # noqa: E402
from caddiej.domain.cues import PanelView  # noqa: E402
from caddiej.domain.shot import ClubKind  # noqa: E402
from caddiej.swing_capture.sheet import SHEET_LABEL  # noqa: E402
from caddiej.swing_capture.store import METRICS_FRAME_ID  # noqa: E402

OUT = Path(__file__).with_name("output") / "t36"

UNSYNCHRONISED = (
    "separate clocks",
    "own clock",
    "NOT synchronised",
    "separate recordings",
    "is a different moment",
)

_SYSTEM_SYNC = (
    (
        "bottom, each camera on its own clock. A cell's ID and time only put it in\norder;",
        "bottom. DTL_nn and FO_nn are the same moment seen by the two cameras. A\ncell's ID and time only put it in "
        "order;",
    ),
    (
        "The two swing views are separate recordings and are NOT synchronised. The same\n"
        "time in SWING_BEHIND and in SWING_SIDE is a different moment of the swing.\n"
        "Never put a down-the-line picture beside a face-on one and treat them as the\n"
        "same instant, and never read one view's picture to fill in what the other view\n"
        "does not show.",
        "The two swing views were captured together: DTL_nn and FO_nn show the same\n"
        "moment of the swing from two cameras. Still never read one view's picture to\n"
        "fill in what the other view does not show.",
    ),
)
_EVIDENCE_SYNC = (
    "DTL and FO are separate clocks, and a time names no moment.",
    "DTL_nn and FO_nn are the same moment from two cameras, and a time names no swing position.",
)


def load_rows(shot_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """The sheet's usable rows with their canonical frame ids, and the unavailable cell IDs."""

    rows = json.loads(
        (t36.ts.OUT / "sheets" / f"{shot_dir.name}_{t36.VARIANT}.json").read_text(
            encoding="utf-8"
        )
    )
    return split_rows(rows)


def split_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    usable, unavailable = [], []
    for row in rows:
        row = dict(row)
        if row.get("available") is False:
            unavailable.append(row["cell_id"])
            continue
        if row["cell_id"] == "ALL_VALUES":
            row["frame_id"] = METRICS_FRAME_ID
        elif "capture_index" in row:
            row["frame_id"] = f"{row['camera_id']}_c{row['capture_index']:02d}"
        usable.append(row)
    return usable, unavailable


def system_sync() -> str:
    text = poc4.system_v14c()
    for old, new in _SYSTEM_SYNC:
        assert old in text, old[:60]
        text = text.replace(old, new)
    return text


def evidence_sync(
    rows: list[dict[str, Any]], unavailable: Sequence[str], cues: Any
) -> str:
    text = poc4.evidence_v14c(rows, cues)
    old, new = _EVIDENCE_SYNC
    assert old in text, old
    text = text.replace(old, new)
    if unavailable:
        anchor = 'Only these may be given in "d" and "f", by number:'
        assert anchor in text
        text = text.replace(
            anchor,
            f"Unavailable, no picture on that camera: {', '.join(unavailable)}. {anchor}",
        )
    return text


def build(
    rows: list[dict[str, Any]],
    unavailable: Sequence[str],
    sheet: bytes,
    *,
    max_output_tokens: int,
    timeout_seconds: float,
    reasoning_effort: str | None,
) -> Any:
    cues = vx.possible_cues(ClubKind.UNKNOWN, has_metrics=True)
    body = poc1.body(rows)
    base = vx.build_request(
        evidence=evidence_sync(rows, unavailable, cues),
        images=(ImageInput(data=sheet, label=SHEET_LABEL, detail="high"),),
        cues=cues,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        reasoning_effort=reasoning_effort,
        frame_ids=tuple(r["cell_id"] for r in rows),
        body_frame_ids=tuple(r["cell_id"] for r in body),
        cell_views={r["cell_id"]: PanelView(r["view"]) for r in body},
    )
    return replace(
        base, system=system_sync(), schema=poc4.schema_v14c(dict(base.schema))
    )


def problems(request: Any) -> list[str]:
    """Anything aimed, true-labelled or claiming unsynchronised cells."""

    sent = request.system + "\n" + request.evidence + "\n" + json.dumps(request.schema)
    return [*poc1.check_neutral(request), *(w for w in UNSYNCHRONISED if w in sent)]


def main(argv: Sequence[str]) -> None:
    from caddiej.coaching.benchmark import request_breakdown
    from caddiej.config import Settings

    shot = next(
        p
        for p in t36.ts.SHOTS.iterdir()
        if p.is_dir() and any(a in p.name for a in argv)
    )
    settings = Settings()
    rows, unavailable = load_rows(shot)
    sheet = (t36.ts.OUT / "sheets" / f"{shot.name}_{t36.VARIANT}.jpg").read_bytes()
    request = build(
        rows,
        unavailable,
        sheet,
        max_output_tokens=settings.coaching_protee_max_output_tokens,
        timeout_seconds=settings.coach_openai_timeout,
        reasoning_effort=settings.coaching_reasoning_effort or None,
    )
    found = problems(request)
    if found:
        raise SystemExit(
            f"request is not neutral or claims unsynchronised cells: {found}"
        )
    out = OUT / shot.name
    out.mkdir(parents=True, exist_ok=True)
    (out / "request_system.txt").write_text(request.system, encoding="utf-8")
    (out / "request_evidence.txt").write_text(request.evidence, encoding="utf-8")
    (out / "request_schema.json").write_text(
        json.dumps(request.schema, indent=1), encoding="utf-8"
    )
    b = request_breakdown(request, model=settings.coaching_model)
    print(
        f"{shot.name}: model {settings.coaching_model} effort {settings.coaching_reasoning_effort or None} "
        f"max_out {request.max_output_tokens} timeout {request.timeout_seconds}s; unavailable {unavailable or '-'}"
    )
    for t in b.texts:
        print(f"  {t.part:<20} {t.characters:6d} chars ~{t.approx_tokens} tok")
    print(
        f"  image ~{b.image_tokens}; text ~{b.text_tokens}; total ~{b.total_tokens}  (dry: nothing sent)"
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    main(sys.argv[1:])
