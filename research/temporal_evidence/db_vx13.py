"""Read-only: the stored vx13 results, as a reference for the POC."""

import json
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
c = sqlite3.connect("file:caddiej.sqlite3?mode=ro", uri=True)
c.row_factory = sqlite3.Row
for r in c.execute("select * from coach_results where prompt_version = 'vx13' order by created_at"):
    score = json.loads(r["swing_score_json"]) if r["swing_score_json"] else None
    checkpoints = None
    if isinstance(score, dict):
        mech = score.get("mechanics") or {}
        checkpoints = mech.get("checkpoints") if isinstance(mech, dict) else None
    print({
        "created_at": r["created_at"], "status": r["status"], "cue": r["cue_id"], "kind": r["kind"],
        "latency_ms": r["latency_ms"], "in": r["input_tokens"], "out": r["output_tokens"],
        "swing_images": r["swing_images"], "swing_score": r["swing_score"],
        "assessed": None if checkpoints is None else sum(
            1 for item in checkpoints if isinstance(item, dict) and item.get("score") is not None
        ),
        "keys": None if checkpoints is None or not checkpoints else sorted(checkpoints[0])[:12],
        "speech": r["speech"], "detail": (r["detail"] or "")[:160],
    })
