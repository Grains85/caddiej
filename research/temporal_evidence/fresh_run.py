"""Fresh-swing validation runner: synchronized T36S + vx14c, one shot at a time. Research only.

Nothing in this file knows any particular shot: no shot names, no cell numbers,
no labels, no expected answers. The experiment's frozen values (impact prior,
model settings, harness fingerprints) and the pre-registered criteria are read
from fresh/config.json and fresh/criteria.json, written once by fresh_freeze.py
before any fresh swing was captured.

Commands (default: never sends):
  preserve                 copy new shot directories out of media/swing_shots
                           before production pruning (keeps 3) can remove them
  run <shot> [--send]      build T36S + the unchanged vx14c request; with --send,
                           make exactly ONE OpenAI request, then analyse
  analyse <shot>           decode/validate/score the saved raw reply; no call
  review <shot>            tiles of the selected canonical originals, for grading
  evaluate                 the five-shot criteria from graded runs; no call
  status                   manifest, runs and replies

<shot> is a directory path, or the name of a preserved shot under fresh/shots/.

    .venv\\Scripts\\python.exe research\\temporal_evidence\\fresh_run.py status
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
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
import hybrid_replay as hy  # noqa: E402
import poc4_vx14c as poc4  # noqa: E402
import poc_vx14 as poc1  # noqa: E402
import t36  # noqa: E402
import t36_request as req  # noqa: E402
import vx14c_replay as c  # noqa: E402

from caddiej.coaching import vx  # noqa: E402
from caddiej.coaching.score import score_swing  # noqa: E402
from caddiej.domain.shot import Handedness  # noqa: E402

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
FRESH = HERE / "fresh"
SOURCE = ROOT / "media" / "swing_shots"
PHASES = poc4.PHASES
POSITIONS = poc1.POSITIONS
GRADES = ("correct", "acceptable", "wrong", "unavailable")
CAMERA_KEY = {"swing_behind": "dtl", "swing_side": "fo"}
PREFIX = {"dtl": "DTL", "fo": "FO"}

# Every file whose change would change the request, the sheet or the verdict.
# fresh_freeze.py records their SHA-256; --send refuses if any differs.
FINGERPRINTED = (
    "research/temporal_evidence/fresh_run.py",
    "research/temporal_evidence/t36.py",
    "research/temporal_evidence/t36_request.py",
    "research/temporal_evidence/temporal_sheets.py",
    "research/temporal_evidence/poc4_vx14c.py",
    "research/temporal_evidence/poc3_hybrid.py",
    "research/temporal_evidence/poc2_vx14b.py",
    "research/temporal_evidence/poc_vx14.py",
    "research/temporal_evidence/hybrid_replay.py",
    "research/temporal_evidence/vx14c_replay.py",
    "src/caddiej/coaching/vx.py",
    "src/caddiej/coaching/checkpoints.py",
    "src/caddiej/coaching/score.py",
    "src/caddiej/coaching/swing_faults.py",
    "src/caddiej/domain/swing_checkpoints.py",
    "src/caddiej/swing_capture/sheet.py",
)


# --- small helpers ------------------------------------------------------------------


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_CELL = re.compile(r"^(DTL|FO)_\d\d$|^IMPACT$|^ALL_VALUES$")


def schema_shape(schema: Any) -> Any:
    """The schema with its lists of sheet cell IDs folded, so an unavailable cell is no schema change."""

    if isinstance(schema, dict):
        return {k: schema_shape(v) for k, v in schema.items()}
    if isinstance(schema, list):
        if schema and all(isinstance(x, str) and _CELL.match(x) for x in schema):
            return "<cells>"
        return [schema_shape(x) for x in schema]
    return schema


def fingerprints() -> dict[str, str]:
    return {name: sha256((ROOT / name).read_bytes()) for name in FINGERPRINTED}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")


def load_frozen(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    config, criteria = root / "config.json", root / "criteria.json"
    if not (config.exists() and criteria.exists()):
        raise SystemExit(
            "fresh/config.json and fresh/criteria.json are missing: run fresh_freeze.py first"
        )
    return _json(config), _json(criteria)


def resolve(shot: str, root: Path) -> Path:
    path = Path(shot)
    if (path / "metadata.json").exists():
        return path.resolve()
    preserved = root / "shots" / shot
    if (preserved / "metadata.json").exists():
        return preserved
    raise SystemExit(f"no shot directory with metadata.json at {shot!r} or {preserved}")


# --- 1. preserve -------------------------------------------------------------------------


def eligibility(meta: dict[str, Any]) -> str | None:
    """None when a shot can be run; otherwise the mechanical capture reason it cannot."""

    timeline = meta.get("diagnostic_timeline")
    if not isinstance(timeline, dict):
        return "no diagnostic timeline (SWING_CAPTURE_DIAGNOSTIC_TIMELINE_SHOTS was 0 or used up)"
    cams = {cam.get("camera_id"): cam for cam in timeline.get("cameras", [])}
    for camera_id in t36.CAMERAS:
        cam = cams.get(camera_id)
        if cam is None or not cam.get("frames"):
            return f"diagnostic timeline has no {camera_id} frames"
        if not cam.get("replay_start_detected"):
            return f"{camera_id}: replay start not detected"
    impact = next(
        (cam for cam in meta.get("cameras", []) if cam.get("camera_id") == "impact"),
        None,
    )
    if impact is None or not impact.get("frames"):
        return "no impact graphic"
    metrics = meta.get("coach_metrics")
    if not isinstance(metrics, dict) or not metrics.get("file"):
        return "no All Values still"
    if len(t36.plan(meta)) != t36.N_MOMENTS:
        return "fewer than 18 replay moments"
    return None


def _tree_hashes(directory: Path) -> dict[str, str]:
    return {
        p.relative_to(directory).as_posix(): sha256(p.read_bytes())
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def preserve(root: Path = FRESH, source: Path = SOURCE) -> list[dict[str, Any]]:
    """Copy every shot captured after the freeze, verified byte for byte, into fresh/shots/.

    The first five eligible ones in capture order are the test set. Idempotent:
    run it after every swing, before the next one can make production prune.
    """

    config, _criteria = load_frozen(root)
    frozen_at = datetime.fromisoformat(config["frozen_at"])
    manifest_path = root / "manifest.json"
    manifest: dict[str, Any] = (
        _json(manifest_path)
        if manifest_path.exists()
        else {"test_set_size": 5, "shots": []}
    )
    known = {entry["name"] for entry in manifest["shots"]}
    added = []
    for shot in sorted(
        p for p in source.iterdir() if p.is_dir() and (p / "metadata.json").exists()
    ):
        if shot.name in known:
            continue
        meta = _json(shot / "metadata.json")
        started = datetime.fromisoformat(meta["started_at"])
        if started <= frozen_at:
            continue
        target = root / "shots" / shot.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(shot, target)
        if _tree_hashes(shot) != _tree_hashes(target):
            raise SystemExit(f"copy of {shot.name} does not match its source")
        reason = eligibility(meta)
        in_set = sum(1 for e in manifest["shots"] if e["role"] == "test")
        role = (
            "excluded"
            if reason
            else ("test" if in_set < manifest["test_set_size"] else "surplus")
        )
        entry = {
            "name": shot.name,
            "started_at": meta["started_at"],
            "preserved_at": datetime.now(UTC).isoformat(),
            "role": role,
            "order": in_set + 1 if role == "test" else None,
            "reason": reason,
            "metadata_sha256": sha256((shot / "metadata.json").read_bytes()),
        }
        manifest["shots"].append(entry)
        added.append(entry)
    _write(manifest_path, manifest)
    return added


# --- 2. one shot: sheet, request, the call -------------------------------------------------


_usable = req.split_rows


def frozen_settings(settings: Any) -> dict[str, Any]:
    return {
        "model": settings.coaching_model,
        "reasoning_effort": settings.coaching_reasoning_effort or None,
        "max_output_tokens": settings.coaching_protee_max_output_tokens,
        "timeout_seconds": settings.coach_openai_timeout,
    }


def prepare(
    shot_dir: Path, out: Path, settings: dict[str, Any]
) -> tuple[Any, dict[str, Any]]:
    """Build T36S and the unchanged vx14c request; write both and the request metadata."""

    started = time.perf_counter()
    meta = _json(shot_dir / "metadata.json")
    moments = t36.plan(meta)
    rows = t36.provenance(meta, moments)
    data, geometry = t36.render(shot_dir, meta, moments)
    render_seconds = time.perf_counter() - started
    out.mkdir(parents=True, exist_ok=True)
    (out / "sheet_T36S.jpg").write_bytes(data)
    _write(out / "cells.json", rows)
    usable, unavailable = _usable(rows)
    request = req.build(
        usable,
        unavailable,
        data,
        max_output_tokens=settings["max_output_tokens"],
        timeout_seconds=settings["timeout_seconds"],
        reasoning_effort=settings["reasoning_effort"],
    )
    found = req.problems(request)
    if found:
        raise SystemExit(
            f"request is not neutral or claims unsynchronised cells: {found}"
        )
    (out / "request_system.txt").write_text(request.system, encoding="utf-8")
    (out / "request_evidence.txt").write_text(request.evidence, encoding="utf-8")
    _write(out / "request_schema.json", request.schema)
    from caddiej.coaching.benchmark import request_breakdown

    breakdown = request_breakdown(request, model=settings["model"])
    info = {
        "shot": shot_dir.name,
        "shot_directory": str(shot_dir),
        "built_at": datetime.now(UTC).isoformat(),
        **settings,
        "sheet": {
            "file": "sheet_T36S.jpg",
            "sha256": sha256(data),
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
            "system": sha256(request.system.encode()),
            "evidence": sha256(request.evidence.encode()),
            "schema": sha256(
                json.dumps(schema_shape(request.schema), sort_keys=True).encode()
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
    _write(out / "request_meta.json", info)
    return request, info


def send_guard(
    out: Path, config: dict[str, Any], info: dict[str, Any], settings: dict[str, Any]
) -> None:
    if (out / "reply.json").exists():
        raise SystemExit(
            f"{out / 'reply.json'} exists: this shot's one request has been made. No retries."
        )
    if settings != config["settings"]:
        raise SystemExit(
            f"settings {settings} differ from the frozen {config['settings']}"
        )
    changed = [
        k for k, v in info["fingerprints"].items() if config["fingerprints"].get(k) != v
    ]
    if changed:
        raise SystemExit(f"harness changed since the freeze: {changed}")
    if info["request_sha256"]["system"] != config["request_sha256"]["system"]:
        raise SystemExit("system prompt differs from the frozen one")
    if info["request_sha256"]["schema"] != config["request_sha256"]["schema"]:
        raise SystemExit("schema differs from the frozen one")


def _provider(settings_obj: Any) -> Any:
    from caddiej.coaching.benchmark import MeteredOpenAIProvider

    return MeteredOpenAIProvider(
        api_key=settings_obj.coaching_api_key.get_secret_value(),
        model=settings_obj.coaching_model,
        base_url=settings_obj.coaching_api_base or None,
    )


async def call_once(
    out: Path, request: Any, root: Path, provider: Any
) -> dict[str, Any]:
    """THE request for this shot. Recorded whatever happens; never repeated."""

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
                    "reply_sha256": sha256(data),
                    "status": record["status"],
                    "at": record["finished_at"],
                }
            )
            + "\n"
        )
    return record


def run(
    shot_dir: Path,
    *,
    send: bool = False,
    root: Path = FRESH,
    settings_obj: Any = None,
    provider_factory: Callable[[Any], Any] = _provider,
) -> dict[str, Any]:
    config, _criteria = load_frozen(root)
    if settings_obj is None:
        from caddiej.config import Settings

        settings_obj = Settings()
    settings = frozen_settings(settings_obj)
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
    send_guard(out, config, info, settings)
    record = asyncio.run(call_once(out, request, root, provider_factory(settings_obj)))
    report = analyse(shot_dir, root=root, settings_obj=settings_obj)
    return {
        "shot": shot_dir.name,
        "mode": "sent",
        "status": record["status"],
        "report": report["compact"],
    }


# --- 3. decode, validate, score ---------------------------------------------------------------


def cell_of(cams: dict[str, Any], key: str, n: Any) -> str | None:
    """A group number -> that camera's printed cell, None for 0, "invalid" otherwise.

    By printed ID, never by list position, so an unavailable cell can never
    shift a number onto its neighbour.
    """

    if isinstance(n, bool) or not isinstance(n, int) or n == 0:
        return None
    cid = f"{PREFIX[key]}_{n:02d}"
    return cid if cid in cams[key]["t"] else "invalid"


def decode(
    payload: dict[str, Any], cams: dict[str, Any]
) -> tuple[
    dict[str, list[str | None]],
    dict[int, str | None],
    list[tuple[Any, str | None, int, Any]],
    int,
    list[str],
]:
    """poc4_vx14c.read_reply, with printed-ID cell lookup and a note per invalid number."""

    groups: dict[str, list[str | None]] = {"dtl": [None] * 9, "fo": [None] * 9}
    any_cam: dict[int, str | None] = {}
    entries: list[tuple[Any, str | None, int, Any]] = []
    returned = 0
    notes: list[str] = []
    phases = payload.get("phases") or {}
    for p, phase in enumerate(PHASES):
        specs = c.specs_of(p)
        g = phases.get(phase.value)
        if not isinstance(g, dict):
            entries += [(s, None, p, vx.REFUSED + "shape") for _c, s in specs]
            continue
        for key, field in (("dtl", "d"), ("fo", "f")):
            cell = cell_of(cams, key, g.get(field))
            if cell == "invalid":
                notes.append(
                    f"{key} {POSITIONS[p]}: number {g.get(field)!r} names no picture"
                )
            groups[key][p] = None if cell == "invalid" else cell
        a = {"d": "dtl", "f": "fo"}.get(str(g.get("a")))
        any_cam[p] = a
        s_list, x_list = g.get("s"), g.get("x")
        if (
            not (
                isinstance(s_list, list)
                and isinstance(x_list, list)
                and len(s_list) == len(specs) == len(x_list)
            )
            or a is None
        ):
            entries += [(s, None, p, vx.REFUSED + "length") for _c, s in specs]
            continue
        for (cam, spec), sc, x in zip(specs, s_list, x_list, strict=True):
            owner = a if cam == "any" else cam
            if sc is None:
                entries.append((spec, owner, p, None))
                continue
            returned += 1
            if not (
                isinstance(sc, list)
                and len(sc) == 2
                and all(isinstance(v, int) for v in sc)
                and x in poc4.DIRECTIONS
            ):
                entries.append((spec, owner, p, vx.REFUSED + "shape"))
                continue
            if groups[owner][p] is None:
                entries.append((spec, owner, p, vx.REFUSED + "no_evidence"))
                continue
            entries.append(
                (
                    spec,
                    owner,
                    p,
                    {
                        "score": sc[0],
                        "conf": sc[1] / 100,
                        "dir": None if x == "none" else x,
                    },
                )
            )
    return groups, any_cam, entries, returned, notes


def _conclude(
    shot_dir: Path, canon: dict[str, Any], accepted: list[Any], settings_obj: Any
) -> dict[str, Any]:
    """The live `_conclude_protee` order (poc_vx14.conclude) on this shot's own capture."""

    saved = poc1.SHOT
    poc1.SHOT = shot_dir
    try:
        concluded: dict[str, Any] = poc1.conclude(canon, accepted, settings_obj)
        return concluded
    finally:
        poc1.SHOT = saved


def analyse(
    shot_dir: Path, *, root: Path = FRESH, settings_obj: Any = None
) -> dict[str, Any]:
    config, _criteria = load_frozen(root)
    if settings_obj is None:
        from caddiej.config import Settings

        settings_obj = Settings()
    out = root / "runs" / shot_dir.name
    record = _json(out / "reply.json")
    rows, _unavailable = _usable(_json(out / "cells.json"))
    info = _json(out / "request_meta.json")
    meta = _json(shot_dir / "metadata.json")
    hy.R7 = tuple(config["impact_prior"]["range_s"])
    cams = hy.sheet(rows)
    by_cell = {r["cell_id"]: r for r in rows if "capture_index" in r}
    payload = record.get("payload") if record.get("status") == "ok" else None
    started = time.perf_counter()
    wire: list[str] = []
    if payload is None:
        wire.append(f"no reply: {record.get('error') or record.get('status')}")
        payload = {}
    elif not isinstance(payload.get("phases"), dict):
        wire.append("payload has no phases object")
    groups, any_cam, entries, returned, decode_notes = decode(payload, cams)
    kept, notes = c.validate_groups(groups, cams)
    entries = poc4.apply_reference(entries, kept)
    accepted, rejected = c.assess(entries, kept, cams)
    wire += [f"{cid}: {why}" for cid, why in rejected if why in ("shape", "length")]
    canon = vx.canonical_reply(
        {
            **vx.decode_wire(
                {k: v for k, v in payload.items() if k in ("metrics", "coach")}, []
            ),
            "checkpoints": {},
            "frames": {},
        },
        {r["cell_id"]: r["frame_id"] for r in rows},
    )
    validate_seconds = time.perf_counter() - started
    metrics = vx.parse_metrics(canon.get("metrics"))
    score = score_swing(
        metrics, club=None, handedness=Handedness.UNKNOWN, checkpoints=accepted
    )
    mech = score.mechanics
    concluded = _conclude(shot_dir, canon, accepted, settings_obj)

    frame_cell = {
        (r["camera_id"], r["frame_id"]): r["cell_id"]
        for r in rows
        if "capture_index" in r
    }
    frame_cam = {r["frame_id"]: r["camera_id"] for r in rows if "capture_index" in r}
    accepted_rows = []
    for a in accepted:
        cam = frame_cam.get(a.frame_id, "")
        accepted_rows.append(
            {
                "checkpoint": a.checkpoint.value,
                "phase": a.phase.value,
                "camera": CAMERA_KEY.get(cam, cam),
                "cell": frame_cell.get((cam, a.frame_id)),
                "frame_id": a.frame_id,
                "score": a.score,
            }
        )
    raw = payload.get("phases") or {}
    group_rows = []
    for key in ("dtl", "fo"):
        for p, phase in enumerate(PHASES):
            cell = groups[key][p]
            row = by_cell.get(cell) if cell else None
            group_rows.append(
                {
                    "position": POSITIONS[p],
                    "phase": phase.value,
                    "camera": key,
                    "raw_number": (raw.get(phase.value) or {}).get(
                        PREFIX[key][0].lower()
                    )
                    if isinstance(raw.get(phase.value), dict)
                    else None,
                    "cell": cell,
                    "kept": kept[key][p] is not None,
                    "capture_index": None if row is None else row["capture_index"],
                    "t": None
                    if row is None
                    else row["offset_from_replay_start_seconds"],
                    "original": None if row is None else row["diagnostic_file"],
                    "accepted_checkpoints": sum(
                        1
                        for x in accepted_rows
                        if x["camera"] == key and x["phase"] == phase.value
                    ),
                }
            )
    usage = record.get("usage") or {}
    from caddiej.adapters.openai.pricing import estimate_cost

    capture_seconds = None
    if meta.get("detected_at") and meta.get("completed_at"):
        capture_seconds = (
            datetime.fromisoformat(meta["completed_at"])
            - datetime.fromisoformat(meta["detected_at"])
        ).total_seconds()
    wall = record.get("wall_seconds")
    compact = {
        "shot": shot_dir.name,
        "status": record.get("status"),
        "error": record.get("error"),
        "wire_failures": wire,
        "decode_notes": decode_notes,
        "validator_notes": notes,
        "groups": {
            f"{g['camera']}_{g['position']}": [g["cell"], g["capture_index"], g["kept"]]
            for g in group_rows
        },
        "returned_checkpoints": returned,
        "accepted": len(accepted),
        "rejected": len(rejected),
        "rejection_reasons": dict(Counter(r for _k, r in rejected)),
        "coverage": None if mech is None else round(mech.coverage, 3),
        "phase_scores": None
        if mech is None
        else {
            ph.phase.value: [
                ph.value,
                round(ph.coverage, 2),
                f"{ph.assessed}/{ph.scoreable}",
            ]
            for ph in mech.phases
        },
        "swing_score": score.value,
        "faults": concluded["faults"],
        "root": concluded["root"],
        "lesson_cue": concluded["lesson_cue"],
        "authority": concluded["authority"],
        "outcome": concluded["outcome"],
        "model_coach": payload.get("coach"),
        "usage": usage,
        "cost_usd": estimate_cost(
            record.get("model") or info["model"],
            usage.get("input_tokens"),
            usage.get("output_tokens"),
        ),
        "timings_s": {
            "capture": capture_seconds,
            "render": info["render_seconds"],
            "openai": wall,
            "validate": round(validate_seconds, 3),
            "strike_to_verdict": None
            if None in (capture_seconds, wall)
            else round(
                capture_seconds + info["render_seconds"] + wall + validate_seconds, 2
            ),
        },
        "reply_sha256": sha256((out / "reply.json").read_bytes()),
    }
    result = {
        "compact": compact,
        "groups": group_rows,
        "accepted": accepted_rows,
        "rejected": [list(r) for r in rejected],
        "impact_prior": list(hy.R7),
    }
    _write(out / "analysis.json", result)
    _write(out / "report.json", compact)
    template = out / "grades_template.json"
    if not template.exists():
        _write(template, grade_template(shot_dir.name, group_rows))
    return result


def grade_template(name: str, group_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "shot": name,
        "instructions": "Copy to grades.json and fill in, from the canonical full-resolution originals only. "
        "grade: correct | acceptable | wrong | unavailable. 'unavailable' only when cell is null; "
        "then shown_on_sheet says whether any cell of that camera on this sheet showed the "
        "position. contact_tick: the capture index of club-ball contact on each camera.",
        "contact_tick": {"dtl": None, "fo": None},
        "groups": {
            key: {
                g["position"]: {
                    "cell": g["cell"],
                    "capture_index": g["capture_index"],
                    "original": g["original"],
                    "grade": None,
                    "shown_on_sheet": None,
                }
                for g in group_rows
                if g["camera"] == key
            }
            for key in ("dtl", "fo")
        },
    }


# --- 4. review tiles ------------------------------------------------------------------------


def review(shot_dir: Path, root: Path = FRESH) -> list[Path]:
    """Each group's selected original, plus the two ticks either side of each P7 pick."""

    from PIL import Image, ImageDraw, ImageFont

    out = root / "runs" / shot_dir.name
    if not (out / "reply.json").exists():
        raise SystemExit("no saved reply: review is for grading after the call")
    analysis = _json(out / "analysis.json")
    meta = _json(shot_dir / "metadata.json")
    written = []
    for camera_id, key in CAMERA_KEY.items():
        cam = next(
            x
            for x in meta["diagnostic_timeline"]["cameras"]
            if x["camera_id"] == camera_id
        )
        frames = {f["capture_index"]: f for f in cam["frames"]}
        tiles: list[tuple[str, int]] = []
        for g in analysis["groups"]:
            if g["camera"] == key and g["capture_index"] is not None:
                tiles.append((f"{g['position']} {g['cell']}", g["capture_index"]))
        p7 = next(
            (
                g["capture_index"]
                for g in analysis["groups"]
                if g["camera"] == key and g["position"] == "P7"
            ),
            None,
        )
        if p7 is not None:
            tiles += [(f"P7{d:+d}", p7 + d) for d in (-2, -1, 1, 2) if p7 + d in frames]
        w, h, cols = 500, 740, 5
        rows = -(-len(tiles) // cols) or 1
        canvas = Image.new("RGB", (cols * (w + 6), rows * (h + 34)), (20, 20, 20))
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.load_default(size=24)
        for n, (label, tick) in enumerate(tiles):
            f = frames[tick]
            with Image.open(shot_dir / f["file"]) as im:
                tile = im.convert("RGB")
                tile.thumbnail((w, h))
            x, y = (n % cols) * (w + 6), (n // cols) * (h + 34)
            canvas.paste(tile, (x, y + 34))
            draw.text(
                (x + 4, y + 4),
                f"{label}  #{tick} +{f['offset_from_replay_start_seconds']:.3f}s",
                fill="white",
                font=font,
            )
        path = out / f"review_{key}.jpg"
        canvas.save(path, quality=92)
        written.append(path)
    return written


# --- 5. the five-shot criteria ------------------------------------------------------------------


def _ratio(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


def evaluate(root: Path = FRESH) -> dict[str, Any]:
    """Every metric exactly as fresh/criteria.json defines it. Refuses partial or edited data."""

    _config, criteria = load_frozen(root)
    thresholds = criteria["thresholds"]
    manifest = _json(root / "manifest.json")
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
        if not reply.exists():
            raise SystemExit(f"{entry['name']}: no reply yet")
        sent = [x for x in ledger if x["shot"] == entry["name"]]
        if len(sent) != 1 or sent[0]["reply_sha256"] != sha256(reply.read_bytes()):
            raise SystemExit(
                f"{entry['name']}: reply missing from the ledger, sent twice, or edited"
            )
        grades = _json(out / "grades.json")
        analysis = _json(out / "analysis.json")
        record = _json(reply)
        wire += len(analysis["compact"]["wire_failures"])
        if record.get("wall_seconds") is not None:
            walls.append(record["wall_seconds"])
        graded = {}
        for g in analysis["groups"]:
            slot = grades["groups"][g["camera"]][g["position"]]
            grade = slot.get("grade")
            if grade not in GRADES:
                raise SystemExit(
                    f"{entry['name']} {g['camera']} {g['position']}: grade {grade!r}"
                )
            if (grade == "unavailable") != (g["cell"] is None):
                raise SystemExit(
                    f"{entry['name']} {g['camera']} {g['position']}: 'unavailable' iff no cell"
                )
            if grade == "unavailable" and not isinstance(
                slot.get("shown_on_sheet"), bool
            ):
                raise SystemExit(
                    f"{entry['name']} {g['camera']} {g['position']}: shown_on_sheet must be true/false"
                )
            graded[(g["camera"], g["position"])] = (
                grade,
                slot.get("shown_on_sheet"),
                g,
            )
        for (cam, pos), (grade, shown, g) in graded.items():
            n = int(pos[1:])
            outcome = (
                "good"
                if grade in ("correct", "acceptable")
                else ("excluded" if grade == "unavailable" and not shown else "bad")
            )
            if 3 <= n <= 8:
                slots["P3_P8"][outcome] += 1
            if 6 <= n <= 9:
                slots["P6_P9"][outcome] += 1
            if pos == "P7":
                p7_total += 1
                contact = grades["contact_tick"][cam]
                if not isinstance(contact, int):
                    raise SystemExit(
                        f"{entry['name']}: contact_tick[{cam}] must be a capture index"
                    )
                p7_ok += (
                    g["capture_index"] is not None
                    and abs(g["capture_index"] - contact) <= 1
                )
        for a in analysis["accepted"]:
            accepted_total += 1
            pos = POSITIONS[[ph.value for ph in PHASES].index(a["phase"])]
            wrong_accepts += graded[(a["camera"], pos)][0] == "wrong"
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
        return _ratio(s["good"], s["good"] + s["bad"])

    m1, m2 = share("P3_P8"), share("P6_P9")
    m4 = _ratio(wrong_accepts, accepted_total)
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
    _write(root / "evaluation.json", result)
    return result


# --- CLI ------------------------------------------------------------------------------------------


def status(root: Path = FRESH) -> dict[str, Any]:
    manifest = (
        _json(root / "manifest.json")
        if (root / "manifest.json").exists()
        else {"shots": []}
    )
    runs = {}
    for e in manifest["shots"]:
        out = root / "runs" / e["name"]
        runs[e["name"]] = {
            "role": e["role"],
            "order": e["order"],
            "reason": e["reason"],
            "built": (out / "request_meta.json").exists(),
            "replied": (out / "reply.json").exists(),
            "graded": (out / "grades.json").exists(),
        }
    return {
        "frozen": (root / "config.json").exists() and (root / "criteria.json").exists(),
        "shots": runs,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preserve")
    r = sub.add_parser("run")
    r.add_argument("shot")
    r.add_argument(
        "--send", action="store_true", help="make this shot's ONE OpenAI request"
    )
    for name in ("analyse", "review"):
        sub.add_parser(name).add_argument("shot")
    sub.add_parser("evaluate")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "preserve":
        result: Any = preserve()
    elif args.command == "run":
        result = run(resolve(args.shot, FRESH), send=args.send)
    elif args.command == "analyse":
        result = analyse(resolve(args.shot, FRESH))["compact"]
    elif args.command == "review":
        result = [str(p) for p in review(resolve(args.shot, FRESH))]
    elif args.command == "evaluate":
        result = evaluate()
    else:
        result = status()
    print(json.dumps(result, indent=1, default=str))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    main()
