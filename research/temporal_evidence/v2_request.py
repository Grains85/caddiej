"""Temporal Evidence V2 request for the synchronized T36S sheet. Research only; never sends.

Exactly the frozen T36S request (t36_request.build: same sheet, model settings,
wire shape) except two passages of the system prompt, one evidence note, the
wording of the seven P5 checkpoints, and three schema descriptions, all aimed at
the five-shot failure (P3-P6 localisation):

1. The positions are named by what they are: P3 lead-arm-parallel backswing,
   P4 top / direction change, P5 lead-arm-parallel downswing, P6 shaft-parallel
   downswing, P7 club-ball contact. The reply key "transition" is stated to be
   legacy terminology for P5 and not the top. P1, P2, P7, P8, P9 paragraphs
   are unchanged, and the schema keys are unchanged.
2. The order rule is strict: P1 < P2 < ... < P9 along one camera, never one
   cell for two positions. The frozen text explicitly allowed sharing.
3. The phase objects for P3, P5 and P6 carry a JSON-schema description with
   that position, so the meaning is next to the key the model writes. Keys,
   types and decoding are unchanged.
4. The P5 ("transition") checkpoint lines are reworded to be read in the P5
   cell. Several production observables time themselves at the top ("as the
   arms finish going back") or the start of the downswing; read literally they
   send the model back to P4, the V1 failure. Ids and scoring are unchanged.

    .venv\\Scripts\\python.exe research\\temporal_evidence\\v2_request.py <shot-prefix>

Writes output/v2/requests/<shot>/request_{system,evidence}.txt and request_schema.json
for a preserved fresh shot. Dry: nothing is sent.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import t36  # noqa: E402
import t36_request as req  # noqa: E402

OUT = Path(__file__).with_name("output") / "v2" / "requests"

_P3_P6_V1 = """\
P3 backswing: before the top. Lead arm about horizontal going back; hands still
rising, about chest high; shaft pointing up, not yet behind the head.
P4 top: the hands at their highest and furthest back. The cell before shows
them still rising; the cell after shows them level or lower. During a pause,
any cell of the pause. Never a cell where the hands are still clearly rising.
P5 transition: after the top. Lead arm about horizontal again coming down;
hands clearly lower than at the top and moving toward the ball; shaft still
above the hands. P3 and P5 can look alike in one picture: the order decides.
P3 comes before the top, P5 after it.
P6 downswing (delivery): after P5, before impact. Shaft about horizontal, hands
near the trail hip, clubhead trailing the hands.
"""

_P3_P6_V2 = """\
P3 = lead-arm-parallel BACKSWING. P4 = TOP / direction change. P5 = lead-arm-parallel DOWNSWING. P6 = shaft-parallel DOWNSWING / delivery. P7 = club-ball CONTACT.
P3 backswing = the LEAD ARM parallel in the BACKSWING: before P4, the club still
going back. Lead arm about horizontal; hands still rising, about chest high;
shaft pointing up, not yet behind the head. Not a cell where the hands are
already at or near their highest: that is P4.
P4 top = the TOP / direction change: the hands at their highest and furthest
back. The cell before shows them still rising; the cell after shows them level
or lower. During a pause, any cell of the pause. Never a cell where the hands
are still clearly rising.
P5 = the LEAD ARM parallel in the DOWNSWING: after P4, coming down. Lead arm
about horizontal again; hands clearly lower than at the top, about chest high
and moving toward the ball; shaft still above the hands. In the reply this
position's key is "transition". That name is legacy terminology and does NOT
mean the top or the direction-change moment: those are P4. A cell where the
hands are still at or near their highest is P4, never P5. P3 and P5 can look
alike in one picture: the order decides. P3 comes before P4, P5 after it.
P6 = the SHAFT parallel in the DOWNSWING / delivery: after P5, before P7.
Shaft about horizontal, hands lower than at P5, near the trail hip, clubhead
trailing the hands. In the reply this position's key is "downswing". That name
does NOT mean the first cell of the downswing and does NOT mean P5. P5 is
judged by the LEAD ARM, P6 by the SHAFT. A cell whose lead arm is horizontal
while the shaft still points up is P5, not P6. If the shaft passed horizontal
between two cells, give the one where it is nearer horizontal; never the P5
cell, never the P7 cell.
"""

# Sits in the evidence, immediately above the checkpoint group whose key is still
# "transition". Checkpoint names (transition_*) stay, so the decoder is unchanged.
LEGACY_KEYS = (
    'LEGACY KEYS. The group headed "transition" is P5, the lead arm parallel in the '
    "DOWNSWING. The word transition is legacy terminology and does NOT mean the top or "
    "the direction-change moment: that moment is P4. Checkpoint names beginning with "
    "transition_ are scored on the P5 cell; they do not move P5 to the top. "
    'The group headed "downswing" is P6, the shaft parallel in the downswing. It does '
    "NOT mean the first cell of the downswing and it does NOT mean P5.\n"
)

# Model-facing wording of the P5 ("transition") checkpoints. The production
# observables time several of them at the top or the very start of the
# downswing, which is P4, not P5. Ids, cameras, order, club flags and direction
# words stay; only the text the model reads changes, so scoring is unchanged.
P5_CHECKPOINTS = (
    (
        "transition_arms_lowering",
        "DTL",
        "the arms dropping down more than they move out, so the hands fall towards the trail hip",
        "in the P5 cell, lead arm parallel coming down: the arms have dropped down "
        "more than they have moved out, the hands on a line falling towards the trail hip",
    ),
    (
        "transition_hand_direction",
        "DTL",
        "the hands moving down and slightly in; not out towards the ball",
        "in the P5 cell: the hands working down and slightly in; not out towards the ball",
    ),
    (
        "transition_trail_shoulder",
        "DTL",
        "the trail shoulder moving down and under at the start of the downswing; "
        "not out towards the ball",
        "in the P5 cell: the trail shoulder working down and under; not out towards the ball",
    ),
    (
        "transition_shallowing",
        "DTL",
        "club shallow in transition: the shaft laying down flatter than it went back, "
        "behind the hands; not tipping out steeper",
        "club shallow in the P5 cell: the shaft lying flatter than it was at P3 going "
        "back, behind the hands; not tipping out steeper",
    ),
    (
        "transition_posture_space",
        "DTL",
        "the space between the pelvis and the ball kept; no thrust of the hips towards "
        "the ball as the downswing starts",
        "in the P5 cell: the space between the pelvis and the ball kept; no thrust of "
        "the hips towards the ball",
    ),
    (
        "transition_pressure_shift",
        "FO",
        "the pelvis shifting towards the target as the arms finish going back; not "
        "staying on the trail side",
        "in the P5 cell: the pelvis already shifted towards the target; not still on "
        "the trail side",
    ),
    (
        "transition_pelvis_initiation",
        "ANY",
        "the lower body starting down first, chest and arms following; not the "
        "shoulders and arms unwinding first",
        "in the P5 cell: the lower body leading, the pelvis more unwound towards the "
        "target than the chest, chest and arms following; not the shoulders and arms "
        "unwound first",
    ),
)

_ORDER_V1 = """\
Along one camera positions never go back in time from P1 to P9. One cell may
stand for two neighbouring positions only when the swing passed both within it.
"""

_ORDER_V2 = """\
Along one camera the positions are strictly in time order, P1 < P2 < P3 < P4
< P5 < P6 < P7 < P8 < P9: each position's cell is later than the cell of every
earlier position you give. Never give one cell to two positions; gaps between
positions are normal. A position given 0 is left out of this rule. CaddieJ
refuses a position that breaks this order, with all its checkpoints.
"""

DESCRIPTIONS = {
    "backswing": "P3: lead-arm-parallel BACKSWING, before P4.",
    "transition": (
        "Legacy key name. This is P5: lead-arm-parallel DOWNSWING, after P4. "
        'The word "transition" does NOT mean the top or the direction-change moment; that is P4.'
    ),
    "downswing": (
        "Legacy key name. This is P6: shaft-parallel DOWNSWING / delivery, after P5 and before P7. "
        "Judged by the shaft, not the lead arm. It is not the first downswing cell and it is not P5."
    ),
}


def system_v2() -> str:
    text = req.system_sync()
    for old, new in ((_P3_P6_V1, _P3_P6_V2), (_ORDER_V1, _ORDER_V2)):
        assert text.count(old) == 1, old[:60]
        text = text.replace(old, new)
    return text


def evidence_v2(text: str) -> str:
    anchor = "\ntransition ("
    if text.count(anchor) != 1:
        raise RuntimeError("the checkpoint group headed transition was not found once")
    for key, camera, old, new in P5_CHECKPOINTS:
        line = f" {key} [{camera}] - "
        if text.count(line + old + ".") != 1:
            raise RuntimeError(
                f"{key}: production wording changed; review the V2 override"
            )
        text = text.replace(line + old + ".", line + new + ".")
    return text.replace(anchor, "\n" + LEGACY_KEYS + "transition (", 1)


def schema_v2(schema: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(schema))
    phases = out["properties"]["phases"]["properties"]
    for key, text in DESCRIPTIONS.items():
        phases[key] = {"description": text, **phases[key]}
    return out


def build(
    rows: list[dict[str, Any]],
    unavailable: Sequence[str],
    sheet: bytes,
    *,
    max_output_tokens: int,
    timeout_seconds: float,
    reasoning_effort: str | None,
) -> Any:
    base = req.build(
        rows,
        unavailable,
        sheet,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        reasoning_effort=reasoning_effort,
    )
    return replace(
        base,
        system=system_v2(),
        evidence=evidence_v2(base.evidence),
        schema=schema_v2(base.schema),
    )


def problems(request: Any) -> list[str]:
    return req.problems(request)


def main(argv: Sequence[str]) -> None:
    import fresh_run as fr

    from caddiej.coaching.benchmark import request_breakdown
    from caddiej.config import Settings

    shot = next(
        p
        for p in (fr.FRESH / "shots").iterdir()
        if p.is_dir() and any(a in p.name for a in argv)
    )
    settings = fr.frozen_settings(Settings())
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    moments = t36.plan(meta)
    rows = t36.provenance(meta, moments)
    sheet, _geometry = t36.render(shot, meta, moments)
    usable, unavailable = req.split_rows(rows)
    request = build(
        usable,
        unavailable,
        sheet,
        max_output_tokens=settings["max_output_tokens"],
        timeout_seconds=settings["timeout_seconds"],
        reasoning_effort=settings["reasoning_effort"],
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
    b = request_breakdown(request, model=settings["model"])
    print(
        f"{shot.name}: model {settings['model']} effort {settings['reasoning_effort']} "
        f"max_out {request.max_output_tokens} timeout {request.timeout_seconds}s; "
        f"text ~{b.text_tokens} tok, image ~{b.image_tokens}, total ~{b.total_tokens} (dry: nothing sent)"
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    main(sys.argv[1:])
