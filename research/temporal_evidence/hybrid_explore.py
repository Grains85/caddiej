"""Offline: list shapes, and how POC 1's checkpoint cells relate to its locate. No call."""

import json
import sys
from collections import Counter
from pathlib import Path

from caddiej.coaching import vx

HERE = Path(__file__).parent
sys.stdout.reconfigure(encoding="utf-8")

print("WIRE_LISTS:")
for key, specs in vx.WIRE_LISTS.items():
    print(f"  {key:<16} {len(specs)}  " + ", ".join(
        f"{s.checkpoint_id.value}[{'/'.join(p.value for p in sorted(s.phases, key=lambda p: list(vx.PHASE_ORDER).index(p)))}]"
        for s in specs))
print("total", sum(len(v) for v in vx.WIRE_LISTS.values()), "lists", len(vx.WIRE_LISTS))

p1 = json.loads((HERE / "output/poc/reply.json").read_text(encoding="utf-8"))["payload"]
print("\nPOC1 locate", json.dumps(p1["locate"]))
for key, entries in p1["checkpoints"].items():
    cells = [e[1] for e in (entries or []) if e is not None]
    print(f"  {key:<16} {dict(Counter(cells))}")
