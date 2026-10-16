"""Contact-anchored simplified classifier. Research only.

The simplified classifier is unchanged except for one temporal fact: where
club-ball contact sits on the capture timeline. That fact comes from an
explicit tick, an explicit interval, a manual contact label, or the preserved
shot's fixture tick. Catalog text, frame selection, schema, and the review
sheet are reused. The previous classifier reply is not part of the request.

Nothing is sent unless --send is given.

    .venv\\Scripts\\python.exe research\\direct_visual_coach\\simple_visual_classifier_contact_anchor.py <shot-id>
    .venv\\Scripts\\python.exe research\\direct_visual_coach\\simple_visual_classifier_contact_anchor.py <shot-id> --send
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import contact_label
import direct_visual_test as coach
import experiment_runner as matrix
import simple_visual_classifier as base
from PIL import Image

from caddiej.adapters.openai.pricing import estimate_cost
from caddiej.coaching.benchmark import request_breakdown
from caddiej.coaching.provider import AdviceRequest

PRESERVED_SHOT_ID = "20261008T191545-2-09132997"
CONTACT_ANCHOR_TICK = 41
RESULTS = (
    Path(__file__).resolve().parent
    / "results"
    / "simple_visual_classifier_contact_anchor"
)
_REPO = Path(__file__).resolve().parents[2]

def exact_anchor_text(tick: int) -> str:
    """Chronology for one contact tick. Tick itself is not forced."""

    return f"""\
KNOWN CONTACT ANCHOR

Club-ball contact occurs around capture_tick {tick}.
capture_tick {tick} itself is not necessarily one of the supplied images.

Use this only as a temporal anchor:
- every supplied capture_tick lower than {tick} is before club-ball contact
- every supplied capture_tick higher than {tick} is after club-ball contact
- do not classify a capture_tick below {tick} as after_strike
- do not classify a capture_tick above {tick} as setup, moving_away, direction_change_region, moving_toward_ball, or uncertain
- a capture_tick above {tick} is after_strike

For a capture_tick below {tick}, still distinguish the visible direction. It may be setup, moving_away, direction_change_region, moving_toward_ball, or uncertain.

The anchor establishes chronology only.
It does not locate the change of direction.
It does not establish downswing mechanics, club path, shaft steepness, hand path, release, or any catalog fault.
The visible mechanics still have to be judged from the images.
"""


def interval_anchor_text(start: int, end: int) -> str:
    """Chronology for a contact interval. The interval is not collapsed."""

    return f"""\
KNOWN CONTACT ANCHOR

The club-ball contact occurs after capture_tick {start} and before capture_tick {end}.

Use this only as a temporal anchor:
- every supplied capture_tick at or below {start} is before club-ball contact
- every supplied capture_tick at or above {end} is after club-ball contact
- do not classify a capture_tick at or below {start} as after_strike
- a capture_tick at or above {end} is after_strike
- do not classify a capture_tick at or above {end} as setup, moving_away, direction_change_region, moving_toward_ball, or uncertain
- a supplied capture_tick strictly between {start} and {end} is not assigned by this anchor

For a capture_tick at or below {start}, still distinguish the visible direction. It may be setup, moving_away, direction_change_region, moving_toward_ball, or uncertain.

The anchor establishes chronology only.
It does not locate the change of direction.
It does not choose a contact frame inside the interval.
It does not establish downswing mechanics, club path, shaft steepness, hand path, release, or any catalog fault.
The visible mechanics still have to be judged from the images.
"""


CONTACT_ANCHOR_TEXT = exact_anchor_text(CONTACT_ANCHOR_TICK)


@dataclass(frozen=True)
class ContactAnchor:
    """The one temporal contact fact supplied to the classifier."""

    kind: str
    tick: int | None
    between: tuple[int, int] | None
    source: str

    def text(self) -> str:
        if self.kind == "tick":
            if self.tick is None:
                raise ValueError("exact contact anchor has no tick")
            return exact_anchor_text(self.tick)
        if self.between is None:
            raise ValueError("interval contact anchor has no bounds")
        return interval_anchor_text(self.between[0], self.between[1])


def preserved_fixture() -> ContactAnchor:
    """Tick 41, and only as the fallback for the preserved shot."""

    return ContactAnchor("tick", CONTACT_ANCHOR_TICK, None, "preserved_fixture")


def resolve_contact(
    shot_id: str,
    meta: Mapping[str, Any],
    *,
    contact_tick: int | None = None,
    contact_between: tuple[int, int] | None = None,
    labels_root: Path | None = None,
) -> ContactAnchor:
    """Pick the contact fact before any classifier request is built.

    Order: explicit tick, explicit interval, stored contact_label.json,
    then the preserved shot's fixture tick. Anything else fails here.
    """

    if contact_tick is not None and contact_between is not None:
        raise SystemExit("pass only one of --contact-tick and --contact-between")
    known = {frame.tick for frame in contact_label.replay_frames(meta)}
    if contact_tick is not None:
        contact_label._require_ticks((contact_tick,), known)
        return ContactAnchor("tick", contact_tick, None, "cli")
    if contact_between is not None:
        contact_label._require_between(contact_between[0], contact_between[1], known)
        return ContactAnchor("between", None, (contact_between[0], contact_between[1]), "cli")
    root = contact_label.LABELS if labels_root is None else labels_root
    path = root / shot_id / "contact_label.json"
    if path.is_file():
        raw = contact_label.load_contact_label(path, shot_id, known)
        if "contact_tick" in raw:
            return ContactAnchor("tick", int(raw["contact_tick"]), None, "contact_label")
        pair = raw["contact_between"]
        return ContactAnchor("between", None, (int(pair[0]), int(pair[1])), "contact_label")
    if shot_id == PRESERVED_SHOT_ID:
        return preserved_fixture()
    raise SystemExit(
        f"no contact label for {shot_id}; "
        "pass --contact-tick or --contact-between, or record one with contact_label.py"
    )


def build_request(
    moments: Sequence[coach.Moment],
    loaded: Sequence[coach.LoadedFrame],
    anchor: ContactAnchor | None = None,
) -> AdviceRequest:
    """The simplified request plus the contact-anchor paragraph."""

    chosen = preserved_fixture() if anchor is None else anchor
    text = chosen.text()
    request = base.build_request(moments, loaded)
    marker = "temporal_orientation must contain each of these capture ticks once."
    if marker not in request.evidence:
        raise RuntimeError("simplified evidence layout has no tick-list marker")
    evidence = request.evidence.replace(marker, marker + "\n\n" + text.strip(), 1)
    instructions = request.instructions
    if not instructions.endswith("\n"):
        instructions += "\n"
    return AdviceRequest(
        system=request.system,
        instructions=instructions + "\n" + text,
        evidence=evidence,
        schema=request.schema,
        images=request.images,
        max_output_tokens=request.max_output_tokens,
        timeout_seconds=request.timeout_seconds,
        schema_name=request.schema_name,
        reasoning_effort=request.reasoning_effort,
    )


def validate_reply(
    payload: Mapping[str, Any],
    sent_ticks: set[int],
    anchor: ContactAnchor | None = None,
) -> list[str]:
    """Simplified checks, plus the contact-anchor orientation rules.

    The payload is not rewritten. A fault candidate is not accepted or
    rejected because of the anchor. Only its cited ticks must have been sent,
    which the simplified validator already checks.
    """

    chosen = preserved_fixture() if anchor is None else anchor
    problems = base.validate_reply(payload, sent_ticks)
    problems.extend(_anchor_problems(payload, chosen))
    return problems


def build_comparison(
    old_reply: Mapping[str, Any],
    new_reply: Mapping[str, Any],
    anchor: ContactAnchor | None = None,
) -> dict[str, Any]:
    """Describe the two saved replies. Neither one is treated as the truth."""

    old_payload = _payload(old_reply)
    new_payload = _payload(new_reply)
    old_faults = _faults(old_payload)
    new_faults = _faults(new_payload)
    old_ids = set(old_faults)
    new_ids = set(new_faults)
    remained = []
    for fault_id in sorted(old_ids & new_ids):
        old_ticks = old_faults[fault_id]["evidence_ticks"]
        new_ticks = new_faults[fault_id]["evidence_ticks"]
        remained.append(
            {
                "fault_id": fault_id,
                "old_confidence": old_faults[fault_id]["confidence"],
                "new_confidence": new_faults[fault_id]["confidence"],
                "old_evidence_ticks": old_ticks,
                "new_evidence_ticks": new_ticks,
                "evidence_ticks_moved": old_ticks != new_ticks,
            }
        )
    chosen = preserved_fixture() if anchor is None else anchor
    old_early = _early_after_strike(old_payload, chosen)
    new_early = _early_after_strike(new_payload, chosen)
    return {
        "note": (
            "Descriptive comparison only. Neither result is ground truth, "
            "and neither is marked as the winner."
        ),
        "contact_anchor_tick": chosen.tick,
        "contact_between": list(chosen.between) if chosen.between else None,
        "old_simple_classifier": _arm(old_reply),
        "contact_anchored_classifier": _arm(new_reply),
        "false_early_after_strike_boundary_disappeared": bool(old_early)
        and not new_early,
        "old_after_strike_before_contact": old_early,
        "new_after_strike_before_contact": new_early,
        "faults_remaining": remained,
        "faults_removed": sorted(old_ids - new_ids),
        "faults_added": sorted(new_ids - old_ids),
    }


def execute(
    shot_id: str,
    *,
    send: bool = False,
    contact_tick: int | None = None,
    contact_between: tuple[int, int] | None = None,
    shots_root: Path | None = None,
    results_root: Path | None = None,
    labels_root: Path | None = None,
    complete: Callable[[AdviceRequest], Any] | None = None,
) -> dict[str, Any]:
    """Build the anchored request. OpenAI is called only when send is true."""

    shots = coach.SHOTS if shots_root is None else shots_root
    results = RESULTS if results_root is None else results_root
    _assert_isolated(results)
    shot = coach.find_shot(shot_id, shots)
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    resolved = resolve_contact(
        shot.name,
        meta,
        contact_tick=contact_tick,
        contact_between=contact_between,
        labels_root=labels_root,
    )
    plan = base.select_dtl_large(meta, shot)
    moments = plan.moments
    loaded = coach.load_frames(shot, moments)
    request = build_request(moments, loaded, resolved)
    breakdown = request_breakdown(
        request, model=coach.MODEL, default_detail=coach.DETAIL
    )
    out = results / shot.name
    out.mkdir(parents=True, exist_ok=True)
    document = base._request_document(shot.name, loaded, request, breakdown)
    document["experiment"] = "simple_visual_classifier_contact_anchor"
    document["contact_anchor_tick"] = resolved.tick
    document["contact_between"] = list(resolved.between) if resolved.between else None
    document["contact_source"] = resolved.source
    document["contact_anchor_text"] = resolved.text()
    base._write(out / "request.json", document)
    sheet = out / "review_sheet.png"
    base.write_review_sheet(sheet, loaded)
    width, height = _sheet_size(sheet)
    ticks = [moment.capture_tick for moment in moments]
    if not send:
        report = _report(
            shot.name,
            ticks,
            len(loaded),
            breakdown.total_tokens,
            sent=False,
            result_directory=out,
            sheet_size=(width, height),
            anchor=resolved,
        )
        base._write(out / "report.json", report)
        return report

    reply = (
        complete(request)
        if complete is not None
        else asyncio.run(coach.send_openai(request))
    )
    payload = dict(reply.payload)
    problems = validate_reply(payload, set(ticks), resolved)
    reply_document = {
        "model": reply.model,
        "reasoning_effort": coach.REASONING_EFFORT,
        "latency_seconds": reply.latency_seconds,
        "usage": {
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "reasoning_tokens": reply.reasoning_tokens,
        },
        "payload": payload,
    }
    base._write(out / "reply.json", reply_document)
    report = _report(
        shot.name,
        ticks,
        len(loaded),
        breakdown.total_tokens,
        sent=True,
        result_directory=out,
        sheet_size=(width, height),
        anchor=resolved,
        payload=payload,
        problems=problems,
        model=reply.model,
        latency_seconds=reply.latency_seconds,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
    )
    base._write(out / "report.json", report)
    old_reply_path = base.RESULTS / shot.name / "reply.json"
    if old_reply_path.is_file():
        old_reply = json.loads(old_reply_path.read_text(encoding="utf-8"))
        comparison = build_comparison(old_reply, reply_document, resolved)
        base._write(out / "comparison.json", comparison)
        (out / "comparison.md").write_text(_comparison_md(comparison), encoding="utf-8")
    return report


def format_summary(report: Mapping[str, Any]) -> str:
    ticks = report.get("selected_ticks")
    tick_text = " ".join(str(tick) for tick in ticks) if isinstance(ticks, list) else ""
    fields = report.get("catalog_fields_sent")
    field_text = ", ".join(fields) if isinstance(fields, list) else ""
    lines = [
        f"experiment: {report.get('experiment')}",
        _anchor_summary(report),
        f"model: {report.get('model')}",
        f"reasoning_effort: {report.get('reasoning_effort')}",
        f"selected_ticks: {tick_text}",
        f"temporal_moments: {report.get('temporal_moments')}",
        f"image_count: {report.get('image_count')}",
        f"catalog_fault_count: {report.get('catalog_fault_count')}",
        f"catalog_fields_sent: {field_text}",
        f"estimated_input_tokens: {report.get('estimated_input_tokens')}",
        f"estimated_input_only_cost_usd: {report.get('estimated_cost_usd')}",
        f"result_directory: {report.get('result_directory')}",
        (
            "review_sheet: "
            f"{report.get('review_sheet_width')}x{report.get('review_sheet_height')}"
        ),
        f"sent: {'yes' if report.get('sent') else 'no'}",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=("Contact-anchored simplified classifier. Sends only with --send.")
    )
    parser.add_argument("shot", help="fresh_v2 shot id")
    parser.add_argument(
        "--send",
        action="store_true",
        help="send the request to OpenAI; without this flag nothing is sent",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--contact-tick",
        type=int,
        help="exact club-ball contact capture tick",
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
    report = execute(
        args.shot,
        send=args.send,
        contact_tick=args.contact_tick,
        contact_between=between,
    )
    print(format_summary(report))
    print("CONTACT ANCHOR ADDITION")
    print(report["contact_anchor_text"])
    print(f"wrote: {RESULTS / report['shot']}")


def _assert_isolated(results_root: Path) -> None:
    resolved = results_root.resolve()
    protected = (
        base.RESULTS.resolve(),
        coach.RESULTS.resolve(),
        matrix.RESULTS.resolve(),
        contact_label.LABELS.resolve(),
        (_REPO / "src").resolve(),
        (_REPO / "research" / "temporal_evidence").resolve(),
    )
    for other in protected:
        if resolved == other or _within(resolved, other) or _within(other, resolved):
            raise RuntimeError(f"contact-anchor results cannot overwrite {other}")


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _anchor_problems(payload: Mapping[str, Any], anchor: ContactAnchor) -> list[str]:
    raw = payload.get("temporal_orientation")
    if not isinstance(raw, list):
        return []
    problems: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        tick = item.get("capture_tick")
        state = item.get("state")
        if isinstance(tick, bool) or not isinstance(tick, int):
            continue
        if not isinstance(state, str):
            continue
        where = f"temporal_orientation[{index}]"
        if anchor.kind == "tick":
            contact = anchor.tick
            if contact is None:
                continue
            if tick < contact and state == "after_strike":
                problems.append(
                    f"{where} capture_tick {tick} is before contact anchor {contact}, "
                    "so after_strike is invalid"
                )
            elif tick > contact and state != "after_strike":
                problems.append(
                    f"{where} capture_tick {tick} is after contact anchor {contact}, "
                    f"so state must be after_strike, not {state}"
                )
            continue
        if anchor.between is None:
            continue
        start, end = anchor.between
        if tick <= start and state == "after_strike":
            problems.append(
                f"{where} capture_tick {tick} is at or before contact interval "
                f"{start}, so after_strike is invalid"
            )
        elif tick >= end and state != "after_strike":
            problems.append(
                f"{where} capture_tick {tick} is at or after contact interval "
                f"{end}, so state must be after_strike, not {state}"
            )
    return problems


def _sheet_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def _report(
    shot: str,
    ticks: Sequence[int],
    image_count: int,
    estimated_tokens: int,
    *,
    sent: bool,
    result_directory: Path,
    sheet_size: tuple[int, int],
    anchor: ContactAnchor | None = None,
    payload: Mapping[str, Any] | None = None,
    problems: Sequence[str] | None = None,
    model: str | None = None,
    latency_seconds: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> dict[str, Any]:
    report = base._report(
        shot,
        ticks,
        image_count,
        estimated_tokens,
        sent=sent,
        result_directory=result_directory,
        payload=payload,
        problems=problems,
        model=model,
        latency_seconds=latency_seconds,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    listed = list(problems or [])
    chosen = preserved_fixture() if anchor is None else anchor
    report["experiment"] = "simple_visual_classifier_contact_anchor"
    report["arm"] = "contact_anchored_classifier"
    report["control"] = "simple_visual_classifier"
    report["contact_anchor_tick"] = chosen.tick
    report["contact_between"] = list(chosen.between) if chosen.between else None
    report["contact_source"] = chosen.source
    report["contact_anchor_text"] = chosen.text()
    report["review_sheet_width"] = sheet_size[0]
    report["review_sheet_height"] = sheet_size[1]
    report["validation_ok"] = not listed
    report["comparison"] = "written_after_send" if sent else "deferred_until_send"
    if sent and listed:
        report["model_status"] = report.get("status")
        report["status"] = "invalid"
    return report


def _payload(reply: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = reply.get("payload")
    if isinstance(payload, Mapping):
        return payload
    return reply


def _arm(reply: Mapping[str, Any]) -> dict[str, Any]:
    payload = _payload(reply)
    usage = reply.get("usage") if isinstance(reply.get("usage"), Mapping) else {}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    model = reply.get("model")
    model_name = model if isinstance(model, str) else coach.MODEL
    return {
        "temporal_orientation": _orientation_rows(payload),
        "fault_candidates": list(_faults(payload).values()),
        "latency_seconds": reply.get("latency_seconds"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": estimate_cost(
            model_name,
            input_tokens if isinstance(input_tokens, int) else None,
            output_tokens if isinstance(output_tokens, int) else None,
        ),
    }


def _orientation_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("temporal_orientation")
    if not isinstance(raw, list):
        return []
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "capture_tick": item.get("capture_tick"),
                "state": item.get("state"),
            }
        )
    return rows


def _faults(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = payload.get("fault_candidates")
    if not isinstance(raw, list):
        return {}
    found: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("fault_id"), str):
            continue
        evidence = item.get("evidence")
        ticks = []
        if isinstance(evidence, list):
            for entry in evidence:
                if isinstance(entry, dict) and isinstance(
                    entry.get("capture_tick"), int
                ):
                    ticks.append(entry["capture_tick"])
        found[item["fault_id"]] = {
            "fault_id": item["fault_id"],
            "confidence": item.get("confidence"),
            "evidence_ticks": ticks,
        }
    return found


def _early_after_strike(
    payload: Mapping[str, Any], anchor: ContactAnchor | None = None
) -> list[int]:
    chosen = preserved_fixture() if anchor is None else anchor
    ticks = []
    for row in _orientation_rows(payload):
        tick = row.get("capture_tick")
        if isinstance(tick, bool) or not isinstance(tick, int):
            continue
        if row.get("state") != "after_strike":
            continue
        if chosen.kind == "tick" and chosen.tick is not None and tick < chosen.tick:
            ticks.append(tick)
        elif chosen.between is not None and tick <= chosen.between[0]:
            ticks.append(tick)
    return ticks


def _anchor_summary(report: Mapping[str, Any]) -> str:
    between = report.get("contact_between")
    if isinstance(between, list):
        return f"contact_between: {between[0]} {between[1]}"
    return f"contact_anchor: {report.get('contact_anchor_tick')}"


def _comparison_md(comparison: Mapping[str, Any]) -> str:
    lines = [
        "# Contact-anchor comparison",
        "",
        str(comparison.get("note")),
        "",
        "## False early after_strike",
        "",
        (
            "Disappeared: "
            f"{comparison.get('false_early_after_strike_boundary_disappeared')}"
        ),
        (
            "Old ticks before contact classified after_strike: "
            f"{comparison.get('old_after_strike_before_contact')}"
        ),
        (
            "New ticks before contact classified after_strike: "
            f"{comparison.get('new_after_strike_before_contact')}"
        ),
        "",
        "## Faults",
        "",
        f"Removed: {comparison.get('faults_removed')}",
        f"Added: {comparison.get('faults_added')}",
    ]
    remained = comparison.get("faults_remaining")
    if isinstance(remained, list):
        for item in remained:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{item.get('fault_id')}: confidence "
                f"{item.get('old_confidence')} -> {item.get('new_confidence')}; "
                f"ticks {item.get('old_evidence_ticks')} -> "
                f"{item.get('new_evidence_ticks')}"
            )
    lines.extend(["", "## Runs", ""])
    for key, title in (
        ("old_simple_classifier", "OLD SIMPLE CLASSIFIER"),
        ("contact_anchored_classifier", "CONTACT-ANCHORED CLASSIFIER"),
    ):
        arm = comparison.get(key)
        lines.append(f"### {title}")
        if isinstance(arm, Mapping):
            lines.append(f"latency_seconds: {arm.get('latency_seconds')}")
            lines.append(f"input_tokens: {arm.get('input_tokens')}")
            lines.append(f"output_tokens: {arm.get('output_tokens')}")
            lines.append(f"estimated_cost_usd: {arm.get('estimated_cost_usd')}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
