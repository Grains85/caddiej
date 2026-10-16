"""Is impact at replay +2.00 s a property of ProTee's clip or of the golfer?

Two independent sources, no OpenAI, no production code:

1. ProTee's own log: for every trigger (ProTee's ball detection at the
   strike), when the swing clip was created and how long it is.
2. CaddieJ's saved captures: per camera, the replay start CaddieJ detected,
   and the offset from it of the still judged by eye to be impact, plus
   tempo markers (takeaway onset, top) judged on the same stills.

Usage: .venv\\Scripts\\python.exe research\\temporal_evidence\\replay_anchor.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev

ROOT = Path(__file__).resolve().parents[2]
LOG = Path.home() / "AppData/Roaming/ProTeeUnited/Logs/LogFile-2026-10-08.txt"
SHOTS = ROOT / "media" / "swing_shots"

TRIGGER = re.compile(r"^(\S+ \S+)\|INFO\|\d+\|Received trigger (\d+) buffer image 1 of the (\d+)")
CREATED = re.compile(r"^(\S+ \S+)\|INFO\|\d+\|Video from (.+?) has been created")
DURATION = re.compile(r"^(\S+ \S+)\|INFO\|\d+\|Video duration (\S+) - FPS: (\d+)")

# By eye on the full-resolution diagnostic originals (capture indices).
EYE = {
    "20261008T092949-4-d05a29c4": {
        "swing_behind": {"takeaway_onset": 17, "top_first": 28, "impact": 41},
        "swing_side": {"takeaway_onset": 17, "top_first": 30, "impact": 41},
    },
    "20261008T093010-5-6669cd32": {"swing_behind": {"impact": 41}},
    "20261008T093033-6-819a363f": {"swing_behind": {"impact": 40}},
}


def stamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f")


def protee_log() -> None:
    triggers: list[tuple[datetime, str, str]] = []
    clips: list[tuple[datetime, str]] = []
    durations: list[tuple[str, str]] = []
    for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        if m := TRIGGER.match(line):
            triggers.append((stamp(m[1]), m[2], m[3]))
        elif m := CREATED.match(line):
            clips.append((stamp(m[1]), m[2]))
        elif m := DURATION.match(line):
            durations.append((m[2], m[3]))
    print(f"ProTee log {LOG.name}: {len(triggers)} triggers, {len(clips)} clips created")
    print(f"  clip durations/fps seen: {sorted(set(durations))}")
    delays = []
    for i, (t, trig, images) in enumerate(triggers):
        nxt = triggers[i + 1][0] if i + 1 < len(triggers) else None
        mine = [c for c in clips if c[0] >= t and (nxt is None or c[0] < nxt)]
        d = [round((c[0] - t).total_seconds(), 3) for c in mine]
        delays.extend(d)
        print(f"  {t:%H:%M:%S.%f}"[:-3] + f"  trigger {trig:>3} (buffer {images})  clips +{d} s")
    if delays:
        print(f"  trigger -> clip created: mean {mean(delays):.3f} s, sd {pstdev(delays):.3f}, range {min(delays):.3f}..{max(delays):.3f}")


def captures() -> None:
    print()
    print("CaddieJ captures: replay start and by-eye moments, seconds from that camera's replay start")
    for shot in sorted(p for p in SHOTS.iterdir() if p.is_dir()):
        meta_path = shot / "metadata.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        timeline = meta.get("diagnostic_timeline") or {}
        if not timeline.get("cameras"):
            continue
        detected = datetime.fromisoformat(meta["detected_at"])
        print(f"  {shot.name}  trigger {meta.get('trigger_id')}  detected {detected:%H:%M:%S.%f}"[:-3])
        for cam in timeline["cameras"]:
            frames = {f["capture_index"]: f for f in cam["frames"]}
            start = next((f for f in cam["frames"] if f.get("offset_from_replay_start_seconds") == 0), None)
            eye = EYE.get(shot.name, {}).get(cam["camera_id"], {})
            parts = []
            for name, index in eye.items():
                f = frames.get(index)
                if f is None:
                    continue
                prev = frames.get(index - 1)
                nxt = frames.get(index + 1)
                spread = [
                    round(x["offset_from_replay_start_seconds"], 3)
                    for x in (prev, nxt)
                    if x and x.get("offset_from_replay_start_seconds") is not None
                ]
                parts.append(f"{name} #{index} +{f['offset_from_replay_start_seconds']:.3f} (neighbours {spread})")
            print(
                f"    {cam['camera_id']:<13} replay start #{start['capture_index'] if start else '?'}"
                f" at burst +{start['offset_seconds'] if start else float('nan'):.3f} s;  " + ";  ".join(parts)
            )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    protee_log()
    captures()
