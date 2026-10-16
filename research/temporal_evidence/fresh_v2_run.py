"""Temporal Evidence V2 runner. Research only. Nothing is sent unless --send is given.

A separate root from the frozen V1 experiment (fresh/). The sheet, model,
reasoning effort, token limit, timeout, impact prior and M1-M6 thresholds are
the frozen V1 ones. The request is v2_request and the validator is v2_order.

Canonical labels are sealed from the full-resolution originals before the one
request for a shot. Once reply.json exists, labelling is refused, and evaluate
rejects a label file that no longer matches the hash recorded with the reply.

    .venv\\Scripts\\python.exe research\\temporal_evidence\\fresh_v2_run.py status

Freeze first, with fresh_v2_freeze.py. This file does not freeze and does not
send on its own.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import fresh_run as fr  # noqa: E402
import t36  # noqa: E402
import v2_grade as grade  # noqa: E402
import v2_order  # noqa: E402
import v2_request as rq  # noqa: E402

ROOT = Path(__file__).parent / "fresh_v2"
V2_FILES = (
    "research/temporal_evidence/fresh_v2_run.py",
    "research/temporal_evidence/fresh_v2_freeze.py",
    "research/temporal_evidence/v2_order.py",
    "research/temporal_evidence/v2_request.py",
    "research/temporal_evidence/v2_grade.py",
)
FINGERPRINTED = (*fr.FINGERPRINTED, *V2_FILES)
CAMERA_ID = {"dtl": "swing_behind", "fo": "swing_side"}


def fingerprints() -> dict[str, str]:
    return {name: fr.sha256((fr.ROOT / name).read_bytes()) for name in FINGERPRINTED}


def capture_ticks(meta: dict[str, Any]) -> dict[str, list[int]]:
    cameras = {c["camera_id"]: c for c in meta["diagnostic_timeline"]["cameras"]}
    return {
        key: [f["capture_index"] for f in cameras[camera_id]["frames"]]
        for key, camera_id in CAMERA_ID.items()
    }


def sheet_ticks(rows: list[dict[str, Any]]) -> dict[str, list[int]]:
    return {
        key: [
            r["capture_index"]
            for r in rows
            if r.get("camera_id") == camera_id
            and "capture_index" in r
            and r.get("available") is not False
        ]
        for key, camera_id in CAMERA_ID.items()
    }


# --- sheet and request -------------------------------------------------------------------


def prepare(
    shot_dir: Path, out: Path, settings: dict[str, Any]
) -> tuple[Any, dict[str, Any]]:
    """T36S, then the V2 request. Same sheet geometry as V1."""

    started = time.perf_counter()
    meta = fr._json(shot_dir / "metadata.json")
    moments = t36.plan(meta)
    rows = t36.provenance(meta, moments)
    data, geometry = t36.render(shot_dir, meta, moments)
    render_seconds = time.perf_counter() - started
    out.mkdir(parents=True, exist_ok=True)
    (out / "sheet_T36S.jpg").write_bytes(data)
    fr._write(out / "cells.json", rows)
    usable, unavailable = fr._usable(rows)
    request = rq.build(
        usable,
        unavailable,
        data,
        max_output_tokens=settings["max_output_tokens"],
        timeout_seconds=settings["timeout_seconds"],
        reasoning_effort=settings["reasoning_effort"],
    )
    found = rq.problems(request)
    if found:
        raise SystemExit(
            f"request is not neutral or claims unsynchronised cells: {found}"
        )
    (out / "request_system.txt").write_text(request.system, encoding="utf-8")
    (out / "request_evidence.txt").write_text(request.evidence, encoding="utf-8")
    fr._write(out / "request_schema.json", request.schema)
    from caddiej.coaching.benchmark import request_breakdown

    breakdown = request_breakdown(request, model=settings["model"])
    info = {
        "shot": shot_dir.name,
        "shot_directory": str(shot_dir),
        "built_at": datetime.now(UTC).isoformat(),
        **settings,
        "sheet": {
            "file": "sheet_T36S.jpg",
            "sha256": fr.sha256(data),
            "bytes": len(data),
            "width": geometry["width"],
            "height": geometry["height"],
            "variant": t36.VARIANT,
            "tolerance_s": t36.tick_tolerance(meta),
            "unavailable": unavailable,
            "moments": [
                {"number": m.number, "tick": m.tick, "t": m.t} for m in moments
            ],
        },
        "request_sha256": {
            "system": fr.sha256(request.system.encode()),
            "evidence": fr.sha256(request.evidence.encode()),
            "schema": fr.sha256(
                json.dumps(fr.schema_shape(request.schema), sort_keys=True).encode()
            ),
        },
        "estimated_tokens": {
            "image": breakdown.image_tokens,
            "text": breakdown.text_tokens,
            "total": breakdown.total_tokens,
        },
        "render_seconds": round(render_seconds, 3),
        "fingerprints": fingerprints(),
    }
    fr._write(out / "request_meta.json", info)
    return request, info


def analyse(
    shot_dir: Path, *, root: Path = ROOT, settings_obj: Any = None
) -> dict[str, Any]:
    """fresh_run.analyse with the V2 strict-order validator in front of V1."""

    import vx14c_replay as replay

    saved = replay.validate_groups
    replay.validate_groups = v2_order.validate_groups
    try:
        return fr.analyse(shot_dir, root=root, settings_obj=settings_obj)
    finally:
        replay.validate_groups = saved


# --- canonical labels, before any model selection ---------------------------------------


def _labels_path(root: Path, name: str) -> Path:
    return root / "labels" / f"{name}.json"


def label(shot_dir: Path, root: Path = ROOT) -> dict[str, Any]:
    """Write a blank label, or seal a filled one. Refuses once a reply exists."""

    reply = root / "runs" / shot_dir.name / "reply.json"
    if reply.exists():
        raise SystemExit(
            f"{reply} exists: canonical labels have to be recorded before the model "
            "selection is inspected. Not overwritten."
        )
    path = _labels_path(root, shot_dir.name)
    meta = fr._json(shot_dir / "metadata.json")
    if not path.exists():
        fr._write(path, grade.template(shot_dir.name, capture_ticks(meta)))
        return {"shot": shot_dir.name, "status": "template", "file": str(path)}
    doc = fr._json(path)
    found = grade.problems(doc, capture_ticks(meta))
    if found:
        raise SystemExit("canonical labels incomplete:\n" + "\n".join(found))
    doc["sealed"] = True
    doc["sealed_at"] = datetime.now(UTC).isoformat()
    fr._write(path, doc)
    return {"shot": shot_dir.name, "status": "sealed", "file": str(path)}


def _sealed_labels(shot_dir: Path, root: Path) -> tuple[Path, dict[str, Any]]:
    path = _labels_path(root, shot_dir.name)
    if not path.exists():
        raise SystemExit(
            f"no canonical labels at {path}. Run: fresh_v2_run.py label {shot_dir.name}"
        )
    doc = fr._json(path)
    found = grade.problems(doc, capture_ticks(fr._json(shot_dir / "metadata.json")))
    if found or not doc.get("sealed"):
        raise SystemExit(
            "canonical labels are not sealed"
            + (
                ":\n" + "\n".join(found)
                if found
                else ". Run label again after filling them."
            )
        )
    return path, doc


# --- the one call -----------------------------------------------------------------------------


async def _call_once(
    out: Path, request: Any, root: Path, provider: Any, canonical_sha256: str
) -> dict[str, Any]:
    """The request for this shot. Recorded whatever happens; never repeated."""

    reply_path = out / "reply.json"
    reply_path.write_text(
        json.dumps(
            {"status": "in_flight", "started_at": datetime.now(UTC).isoformat()}
        ),
        encoding="utf-8",
    )
    started = time.perf_counter()
    reply, error = None, None
    try:
        reply = await provider.complete(request)
    except Exception as exc:  # recorded, never retried
        error = f"{type(exc).__name__}: {exc}"
    wall = time.perf_counter() - started
    usage = provider.usage()
    await provider.aclose()
    record = {
        "status": "ok" if reply is not None else "error",
        "error": error,
        "model": getattr(provider, "model", None),
        "reasoning_effort": request.reasoning_effort,
        "max_output_tokens": request.max_output_tokens,
        "timeout_seconds": request.timeout_seconds,
        "wall_seconds": round(wall, 3),
        "finished_at": datetime.now(UTC).isoformat(),
        "usage": None if usage is None else asdict(usage),
        "payload": None if reply is None else reply.payload,
    }
    data = json.dumps(record, indent=1, default=str).encode("utf-8")
    reply_path.write_bytes(data)
    os.chmod(reply_path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    with (root / "ledger.jsonl").open("a", encoding="utf-8") as ledger:
        ledger.write(
            json.dumps(
                {
                    "shot": out.name,
                    "reply_sha256": fr.sha256(data),
                    "canonical_sha256": canonical_sha256,
                    "status": record["status"],
                    "at": record["finished_at"],
                }
            )
            + "\n"
        )
    return record


def _seal_into_run(src: Path, out: Path) -> str:
    """Copy the sealed labels beside the request, before the call, and freeze the bytes."""

    data = src.read_bytes()
    dest = out / "canonical.json"
    if dest.exists():
        if dest.read_bytes() != data:
            raise SystemExit(
                f"{dest} already holds different labels: labels changed after an "
                "earlier send attempt. Not overwritten."
            )
        return fr.sha256(data)
    dest.write_bytes(data)
    os.chmod(dest, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    return fr.sha256(data)


def run(
    shot_dir: Path,
    *,
    send: bool = False,
    root: Path = ROOT,
    settings_obj: Any = None,
    provider_factory: Callable[[Any], Any] = fr._provider,
) -> dict[str, Any]:
    config, _criteria = fr.load_frozen(root)
    if settings_obj is None:
        from caddiej.config import Settings

        settings_obj = Settings()
    settings = fr.frozen_settings(settings_obj)
    out = root / "runs" / shot_dir.name
    if (out / "reply.json").exists() and not send:
        return {
            "shot": shot_dir.name,
            "mode": "dry",
            "note": "reply already saved; sheet and request left as sent",
        }
    request, info = prepare(shot_dir, out, settings)
    if not send:
        return {"shot": shot_dir.name, "mode": "dry", "request_meta": info}
    labels, _doc = _sealed_labels(shot_dir, root)
    fr.send_guard(out, config, info, settings)
    canonical_sha256 = _seal_into_run(labels, out)
    info["canonical_sha256"] = canonical_sha256
    fr._write(out / "request_meta.json", info)
    record = asyncio.run(
        _call_once(out, request, root, provider_factory(settings_obj), canonical_sha256)
    )
    report = analyse(shot_dir, root=root, settings_obj=settings_obj)
    return {
        "shot": shot_dir.name,
        "mode": "sent",
        "status": record["status"],
        "report": report["compact"],
    }


def review(shot_dir: Path, root: Path = ROOT) -> list[Path]:
    """Model selections. Refuses until the canonical labels are already in the run."""

    if not (root / "runs" / shot_dir.name / "canonical.json").exists():
        raise SystemExit(
            "canonical labels are not in the run yet. Label the originals before "
            "looking at the model's cells."
        )
    return fr.review(shot_dir, root)


# --- criteria ------------------------------------------------------------------------------------


def _outcome(verdict: str, shown: bool | None) -> str:
    if verdict in ("correct", "acceptable"):
        return "good"
    if verdict == "unavailable" and not shown:
        return "excluded"
    return "bad"


def evaluate(root: Path = ROOT) -> dict[str, Any]:
    """M1-M6 from sealed canonical labels. Refuses a partial set or an edited reply or label."""

    _config, criteria = fr.load_frozen(root)
    thresholds = criteria["thresholds"]
    manifest = fr._json(root / "manifest.json")
    test = sorted(
        (e for e in manifest["shots"] if e["role"] == "test"), key=lambda e: e["order"]
    )
    if len(test) != manifest["test_set_size"]:
        raise SystemExit(
            f"{len(test)} test shots preserved; {manifest['test_set_size']} required"
        )
    ledger = [
        json.loads(line)
        for line in (root / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    slots: dict[str, Counter[str]] = {"P3_P8": Counter(), "P6_P9": Counter()}
    p7_ok = p7_total = wrong_accepts = accepted_total = wire = 0
    walls: list[float] = []
    per_shot = []
    for entry in test:
        out = root / "runs" / entry["name"]
        reply = out / "reply.json"
        canonical_path = out / "canonical.json"
        if not reply.exists():
            raise SystemExit(f"{entry['name']}: no reply yet")
        if not canonical_path.exists():
            raise SystemExit(
                f"{entry['name']}: no canonical labels recorded with the call"
            )
        sent = [x for x in ledger if x["shot"] == entry["name"]]
        if (
            len(sent) != 1
            or sent[0]["reply_sha256"] != fr.sha256(reply.read_bytes())
            or sent[0].get("canonical_sha256") != fr.sha256(canonical_path.read_bytes())
        ):
            raise SystemExit(
                f"{entry['name']}: reply or canonical labels missing from the ledger, "
                "sent twice, or edited"
            )
        canonical = fr._json(canonical_path)
        analysis = fr._json(out / "analysis.json")
        record = fr._json(reply)
        rows = fr._json(out / "cells.json")
        on_sheet = sheet_ticks(rows)
        wire += len(analysis["compact"]["wire_failures"])
        if record.get("wall_seconds") is not None:
            walls.append(record["wall_seconds"])
        graded = {}
        for g in analysis["groups"]:
            slot = canonical[g["camera"]]["positions"][g["position"]]
            verdict, shown = grade.grade_pick(
                g["capture_index"],
                slot["best"],
                slot["acceptable"],
                on_sheet[g["camera"]],
            )
            graded[(g["camera"], g["position"])] = verdict
            n = int(g["position"][1:])
            outcome = _outcome(verdict, shown)
            if 3 <= n <= 8:
                slots["P3_P8"][outcome] += 1
            if 6 <= n <= 9:
                slots["P6_P9"][outcome] += 1
            if g["position"] == "P7":
                p7_total += 1
                contact = canonical[g["camera"]]["contact_tick"]
                p7_ok += (
                    g["capture_index"] is not None
                    and abs(g["capture_index"] - contact) <= 1
                )
        for a in analysis["accepted"]:
            pos = fr.POSITIONS[[ph.value for ph in fr.PHASES].index(a["phase"])]
            accepted_total += 1
            wrong_accepts += graded[(a["camera"], pos)] == "wrong"
        per_shot.append(
            {
                "shot": entry["name"],
                "accepted": len(analysis["accepted"]),
                "wire_failures": len(analysis["compact"]["wire_failures"]),
                "openai_s": record.get("wall_seconds"),
            }
        )

    def share(name: str) -> float | None:
        s = slots[name]
        total = s["good"] + s["bad"]
        return None if total == 0 else s["good"] / total

    m1, m2 = share("P3_P8"), share("P6_P9")
    m4 = None if accepted_total == 0 else wrong_accepts / accepted_total
    calls_ok = len(walls) == len(test)
    metrics = {
        "M1_P3_P8_correct_or_acceptable": [
            m1,
            m1 is not None and m1 >= thresholds["M1_min"],
            dict(slots["P3_P8"]),
        ],
        "M2_P6_P9_correct_or_acceptable": [
            m2,
            m2 is not None and m2 >= thresholds["M2_min"],
            dict(slots["P6_P9"]),
        ],
        "M3_P7_within_one_tick": [
            f"{p7_ok}/{p7_total}",
            p7_ok >= thresholds["M3_min_of_10"],
        ],
        "M4_wrong_position_accepts": [
            m4,
            m4 is not None and m4 <= thresholds["M4_max"],
            f"{wrong_accepts}/{accepted_total}",
        ],
        "M5_wire_schema_failures": [wire, wire <= thresholds["M5_max"]],
        "M6_openai_latency_max_s": [
            max(walls) if walls else None,
            calls_ok and max(walls) <= thresholds["M6_max_s"],
            {
                "median_s": median(walls) if walls else None,
                "preferred_median_s": thresholds["M6_preferred_median_s"],
            },
        ],
    }
    result = {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "metrics": metrics,
        "pass": all(v[1] for v in metrics.values()),
        "per_shot": per_shot,
    }
    fr._write(root / "evaluation.json", result)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preserve")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("shot")
    run_parser.add_argument(
        "--send", action="store_true", help="make this shot's ONE OpenAI request"
    )
    for name in ("analyse", "review", "label"):
        sub.add_parser(name).add_argument("shot")
    sub.add_parser("evaluate")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "preserve":
        result: Any = fr.preserve(ROOT)
    elif args.command == "run":
        result = run(fr.resolve(args.shot, ROOT), send=args.send)
    elif args.command == "analyse":
        result = analyse(fr.resolve(args.shot, ROOT))["compact"]
    elif args.command == "review":
        result = [str(p) for p in review(fr.resolve(args.shot, ROOT))]
    elif args.command == "label":
        result = label(fr.resolve(args.shot, ROOT))
    elif args.command == "evaluate":
        result = evaluate()
    else:
        result = fr.status(ROOT)
    print(json.dumps(result, indent=1, default=str))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    main()
