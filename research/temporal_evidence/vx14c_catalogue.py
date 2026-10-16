"""Offline: every scoreable checkpoint per phase/view list, with what it asks the picture. No call."""

import sys

from caddiej.coaching import vx

sys.stdout.reconfigure(encoding="utf-8")
for key, specs in vx.WIRE_LISTS.items():
    print(f"\n== {key}")
    for s in specs:
        phases = "/".join(p.value for p in sorted(s.phases, key=list(vx.PHASE_ORDER).index))
        devs = ",".join(d.value for d in s.deviations) or "-"
        print(f"  {s.checkpoint_id.value} [{phases}] club={s.club} w={s.weight} dev={devs}")
        print(f"     {s.observable}")
