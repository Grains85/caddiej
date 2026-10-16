"""Which cell each raw vx14 checkpoint entry cited, against what was located. No network."""

import json
import sys
from pathlib import Path

from caddiej.coaching import vx

REPLY = Path(__file__).with_name("output") / "poc" / "reply.json"

payload = json.loads(REPLY.read_text(encoding="utf-8"))["payload"]
locate = payload["locate"]
phase_of = {}
for camera, cells in locate.items():
    for phase, cell in zip(vx.PHASE_ORDER, cells, strict=True):
        if cell:
            phase_of.setdefault(cell, []).append(phase.value)
sys.stdout.reconfigure(encoding="utf-8")
print("located:", {c: p for c, p in phase_of.items()})
for key, specs in vx.WIRE_LISTS.items():
    entries = payload["checkpoints"].get(key) or []
    cited = []
    for spec, entry in zip(specs, entries, strict=False):
        if entry is None:
            cited.append(f"{spec.checkpoint_id.value}=null")
            continue
        cell = entry[1]
        ok = any(p in {x.value for x in spec.phases} for p in phase_of.get(cell, []))
        cited.append(f"{spec.checkpoint_id.value}={entry[0]}@{cell}{'' if ok else ' !' + ','.join(phase_of.get(cell, ['unlocated']))}")
    print(f"{key}: " + "; ".join(cited))
print("coach:", payload["coach"])
