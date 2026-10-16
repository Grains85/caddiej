"""Freeze Temporal Evidence V2. Run once, and only when the protocol is accepted.

Copies the frozen V1 model settings, T36S sheet, baseline impact prior and
M1-M6 thresholds. Records the V2 request hashes and the fingerprints of every
file that can change a request, a sheet or a verdict. Refuses to overwrite.

Does not capture a swing and does not call OpenAI.

    .venv\\Scripts\\python.exe research\\temporal_evidence\\fresh_v2_freeze.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import fresh_run as fr  # noqa: E402
import fresh_v2_run as v2  # noqa: E402
import v2_grade as grade  # noqa: E402

THRESHOLDS = {
    "M1_min": 0.90,
    "M2_min": 0.95,
    "M3_min_of_10": 9,
    "M4_max": 0.05,
    "M5_max": 0,
    "M6_max_s": 15.0,
    "M6_preferred_median_s": 10.0,
}

REQUIRED_SETTINGS = {
    "model": "gpt-5-mini",
    "reasoning_effort": "minimal",
    "max_output_tokens": 10000,
    "timeout_seconds": 30.0,
}


def criteria(frozen_at: str) -> dict[str, Any]:
    return {
        "written_at": frozen_at,
        "test_set": (
            "The first 5 shots captured after config.json's frozen_at whose capture is "
            "complete (fresh_run.eligibility), in capture order. Excluded only for those "
            "mechanical reasons, decided by preserve before any request. Shots after the "
            "fifth are surplus and never sent. Shots captured before this freeze, including "
            "the V1 test set, are not eligible."
        ),
        "protocol": (
            "Exactly one OpenAI request per test shot (fresh_v2_run.py run <shot> --send). "
            "No retries. No change between shots to the V2 prompt, model, reasoning effort, "
            "T36S, impact prior, V2 validator or schema: --send refuses if any fingerprinted "
            "file, setting, system-prompt hash or schema hash differs from config.json."
        ),
        "grading": {
            "when": (
                "Canonical labels are sealed (fresh_v2_run.py label <shot>) BEFORE the "
                "request. label refuses once reply.json exists. The sealed file is copied "
                "into the run and its SHA-256 is written to the ledger with the reply. "
                "evaluate refuses a label or reply whose bytes no longer match the ledger."
            ),
            "what": (
                "Per camera, from the canonical full-resolution originals, not from the "
                "model's cells: contact_tick and, for P1-P9, the best capture ticks and "
                "the acceptable capture ticks. Every P6 acceptable tick lies after every "
                "P5 best tick and before contact_tick. Labels hold capture ticks only, "
                "never a sheet cell ID. The model's raw cell is then compared with "
                "those labels."
            ),
            "contact_tick": grade.CONTACT_RULE,
            "p6": grade.P6_RULE,
            "correct": "The model's capture tick is one of the pre-recorded best ticks.",
            "acceptable": "The model's capture tick is one of the pre-recorded acceptable ticks.",
            "wrong": "The model's capture tick is neither best nor acceptable.",
            "unavailable": (
                "The model returned no cell, or a number naming no picture. "
                "shown_on_sheet is true when any acceptable tick for that position is "
                "an available cell of that camera on the T36S sheet."
            ),
        },
        "metrics": {
            "M1": (
                "P3-P8. Slots: 5 shots x 2 cameras x P3..P8 = 60. good = correct|acceptable; "
                "bad = wrong, or unavailable with the position shown on the sheet; excluded = "
                "unavailable and not shown. M1 = good / (good + bad). PASS if >= 0.90."
            ),
            "M2": "Same rule over P6-P9 (40 slots). PASS if >= 0.95.",
            "M3": (
                "P7. Per shot x camera (10): pass when the returned P7 capture index is "
                "within +/-1 of that camera's pre-recorded contact_tick. A null P7 fails. "
                "PASS if >= 9 of 10."
            ),
            "M4": (
                "Accepted checkpoints (after validation, every position P1-P9, as in V1) "
                "whose owning raw group is graded wrong, divided by all accepted "
                "checkpoints. PASS if <= 0.05."
            ),
            "M5": "Wire/schema failures, summed, as in V1. PASS if == 0.",
            "M6": (
                "OpenAI latency: reply.json wall_seconds. PASS if every call <= 15.0 s. "
                "The median is reported against the preferred ~10 s and is not a gate."
            ),
            "overall": "PASS only if M1-M6 all pass.",
        },
        "thresholds": dict(THRESHOLDS),
    }


def freeze(root: Path = v2.ROOT) -> dict[str, Any]:
    if (root / "config.json").exists() or (root / "criteria.json").exists():
        raise SystemExit(
            "already frozen: fresh_v2 config.json/criteria.json exist and may not be rewritten"
        )
    v1 = fr._json(fr.FRESH / "config.json")
    v1_criteria = fr._json(fr.FRESH / "criteria.json")
    if v1["settings"] != REQUIRED_SETTINGS:
        raise SystemExit(
            f"V1 settings {v1['settings']} are not the required V2 settings"
        )
    if v1_criteria["thresholds"] != THRESHOLDS:
        raise SystemExit("V1 thresholds differ from the V2 thresholds")
    if v1["sheet"]["variant"] != "T36S":
        raise SystemExit("V1 sheet is not T36S")
    shot = fr.FRESH / "baseline" / v1["baseline"][0]
    with tempfile.TemporaryDirectory() as tmp:
        _request, info = v2.prepare(shot, Path(tmp), v1["settings"])
    frozen_at = datetime.now(UTC).isoformat()
    config = {
        "frozen_at": frozen_at,
        "experiment": "temporal-evidence-v2",
        "impact_prior": v1["impact_prior"],
        "tau_s": v1["tau_s"],
        "settings": dict(v1["settings"]),
        "sheet": dict(v1["sheet"]),
        "request_sha256": {k: info["request_sha256"][k] for k in ("system", "schema")},
        "fingerprints": v2.fingerprints(),
        "baseline": list(v1["baseline"]),
        "prior_source": "research/temporal_evidence/fresh/config.json impact_prior, unchanged",
    }
    fr._write(root / "criteria.json", criteria(frozen_at))
    fr._write(root / "config.json", config)
    return config


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    result = freeze()
    print(
        f"frozen {result['frozen_at']} model {result['settings']['model']} "
        f"effort {result['settings']['reasoning_effort']} "
        f"prior {result['impact_prior']['range_s']}"
    )
