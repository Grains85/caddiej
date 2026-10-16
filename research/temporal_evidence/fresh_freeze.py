"""Freeze the fresh-swing experiment BEFORE any fresh swing is captured. Run once.

1. Archives the three saved real shots (4, 5, 6) out of media/swing_shots into
   fresh/baseline/, because production keeps only the newest 3 shot directories
   and the first fresh swing starts pruning them.
2. Freezes the vx14c broad ProTee replay-impact validation prior from those three
   shots ONLY: the by-eye contact tick of each (sync_sampling.TRUTH P7, both
   cameras), its replay offset, the mean, +/- hybrid_replay.R7_HALF.
3. Freezes the model settings, the harness fingerprints and the system/schema
   hashes the five calls must be made with.
4. Writes the pre-registered five-shot criteria.

Refuses to overwrite: nothing here may be recalibrated from the test swings.

    .venv\\Scripts\\python.exe research\\temporal_evidence\\fresh_freeze.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import fresh_run as fr  # noqa: E402
import hybrid_replay as hy  # noqa: E402
import sync_sampling as ss  # noqa: E402

BASELINE = tuple(ss.TRUTH)

CRITERIA: dict[str, Any] = {
    "test_set": (
        "The first 5 shots captured after config.json's frozen_at whose capture is complete "
        "(fresh_run.eligibility: diagnostic timeline on both swing cameras with replay start detected, "
        "impact graphic, All Values still, 18 replay moments), in capture order. A shot is excluded only "
        "for those mechanical capture reasons, decided by `preserve` before any request. Shots after the "
        "fifth are surplus and never sent."
    ),
    "protocol": (
        "Exactly one OpenAI request per test shot (fresh_run.py run <shot> --send). No retries: a failed, "
        "timed-out or unparseable call is recorded and counts. No change between shots to prompt, model, "
        "reasoning effort, T36S, impact prior, validator or schema: --send refuses if any fingerprinted "
        "file, setting, system-prompt hash or schema hash differs from config.json."
    ),
    "grading": {
        "when": "Only after all five raw replies are saved (reply.json read-only, SHA-256 in ledger.jsonl).",
        "what": "Each returned group cell (raw model choice, before the validator), per shot x camera x P1-P9, "
        "judged on that cell's canonical full-resolution original (fresh_run.py review <shot>).",
        "correct": "The original shows the named position as defined in the vx14c prompt.",
        "acceptable": "Within the position's own window but not its best still (e.g. P4 a tick before the "
        "true top, P8 at the late edge of shaft-parallel).",
        "wrong": "Shows a different position, including a neighbouring one (P6 for P7, P5 for P6, ...).",
        "unavailable": "The model returned 0 (or a number naming no picture). shown_on_sheet then records "
        "whether any cell of that camera on this sheet showed the position.",
        "contact_tick": "Per camera: the capture index at which club meets ball, read on the full-resolution "
        "diagnostic originals around the impact window.",
    },
    "metrics": {
        "M1": "P3-P8 phase/view evidence. Slots: 5 shots x 2 cameras x P3..P8 = 60. good = correct|acceptable; "
        "bad = wrong, or unavailable with shown_on_sheet=true; excluded = unavailable with "
        "shown_on_sheet=false. M1 = good / (good + bad). PASS if >= 0.90.",
        "M2": "P6-P9 no meaningful regression (POC4 shot 4 was 8/8 correct-or-acceptable). Same rule over "
        "5 x 2 x P6..P9 = 40 slots. PASS if >= 0.95.",
        "M3": "P7 contact. Per shot x camera (10): pass when the returned P7 cell's capture index is within "
        "+/-1 of that camera's graded contact_tick; a null P7 fails. PASS if >= 9 of 10.",
        "M4": "Accepted checkpoints on wrong biomechanical positions: accepted checkpoints (after the "
        "validator and assess) whose owning group (camera, phase) is graded wrong, divided by all "
        "accepted checkpoints over the five shots. PASS if <= 0.05.",
        "M5": "Wire/schema failures: per shot, no saved payload (transport error, timeout, refusal), a payload "
        "without a phases object, and every checkpoint refused as 'shape' or 'length' by the vx14c "
        "decoder. Summed over five shots. PASS if == 0.",
        "M6": "OpenAI latency: reply.json wall_seconds of each call (request sent to reply decoded). PASS if "
        "every one of the five <= 15.0 s. The median is reported against the preferred ~10 s; "
        "informational, not a gate.",
        "overall": "PASS only if M1-M6 all pass. Computed by fresh_run.py evaluate exactly as above.",
    },
    "thresholds": {
        "M1_min": 0.90,
        "M2_min": 0.95,
        "M3_min_of_10": 9,
        "M4_max": 0.05,
        "M5_max": 0,
        "M6_max_s": 15.0,
        "M6_preferred_median_s": 10.0,
    },
}


def archive(source: Path, target: Path) -> list[Path]:
    kept = []
    for name in BASELINE:
        dest = target / name
        if not (dest / "metadata.json").exists():
            src = source / name
            if not (src / "metadata.json").exists():
                raise SystemExit(
                    f"baseline shot {name} is in neither {source} nor {target}"
                )
            shutil.copytree(src, dest)
            if fr._tree_hashes(src) != fr._tree_hashes(dest):
                raise SystemExit(f"copy of {name} does not match its source")
        kept.append(dest)
    return kept


def contact_times(shots: list[Path]) -> list[dict[str, Any]]:
    out = []
    for shot in shots:
        meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
        cams = {c["camera_id"]: c for c in meta["diagnostic_timeline"]["cameras"]}
        per_cam = {}
        for camera_id in ss.CAMERAS:
            (tick,) = ss.TRUTH[shot.name][camera_id]["P7"]
            frame = next(
                f for f in cams[camera_id]["frames"] if f["capture_index"] == tick
            )
            per_cam[camera_id] = [tick, frame["offset_from_replay_start_seconds"]]
        out.append(
            {
                "shot": shot.name,
                "contact": per_cam,
                "t": mean(v[1] for v in per_cam.values()),
            }
        )
    return out


def prior(contacts: list[dict[str, Any]]) -> dict[str, Any]:
    centre = mean(c["t"] for c in contacts)
    return {
        "range_s": [round(centre - hy.R7_HALF, 4), round(centre + hy.R7_HALF, 4)],
        "centre_s": round(centre, 4),
        "half_width_s": hy.R7_HALF,
        "rule": "mean by-eye contact offset of saved real shots 4, 5, 6 (both cameras) +/- R7_HALF; "
        "replaces hybrid_replay.R7 for every fresh run. TAU unchanged.",
        "source": contacts,
    }


def freeze(
    root: Path = fr.FRESH, source: Path = fr.SOURCE, settings_obj: Any = None
) -> dict[str, Any]:
    if (root / "config.json").exists() or (root / "criteria.json").exists():
        raise SystemExit(
            "already frozen: config.json/criteria.json exist and may not be rewritten"
        )
    if settings_obj is None:
        from caddiej.config import Settings

        settings_obj = Settings()
    shots = archive(source, root / "baseline")
    settings = fr.frozen_settings(settings_obj)
    with tempfile.TemporaryDirectory() as tmp:
        _request, info = fr.prepare(shots[0], Path(tmp), settings)
    config = {
        "frozen_at": datetime.now(UTC).isoformat(),
        "impact_prior": prior(contact_times(shots)),
        "tau_s": {"P2_P3": 0.30, "P5_P8": 0.15, "note": "hybrid_replay.TAU, unchanged"},
        "settings": settings,
        "sheet": {
            "variant": "T36S",
            "moments": 18,
            "max_side": 2048,
            "jpeg_quality": 90,
            "chroma": "4:4:4",
        },
        "request_sha256": {k: info["request_sha256"][k] for k in ("system", "schema")},
        "fingerprints": fr.fingerprints(),
        "baseline": [s.name for s in shots],
    }
    fr._write(root / "criteria.json", {"written_at": config["frozen_at"], **CRITERIA})
    fr._write(root / "config.json", config)
    return config


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    result = freeze()
    print(
        json.dumps(
            {k: result[k] for k in ("frozen_at", "impact_prior", "settings")}, indent=1
        )
    )
