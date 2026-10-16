"""DIAGNOSTIC ONLY, no call: how much of POC 3's checkpoint loss was the wire encoding.

Lenient decode of the saved reply, never used for the pre-registered verdict:
a direction of 0 is read as null; in an _any list a leading [cam, x, y] array whose
cam is 1 or 2 is read as the camera digit and bare integers elsewhere are dropped;
an all-null entry is null. Everything else goes through the same hybrid validator.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hybrid_replay as hy  # noqa: E402
import poc3_hybrid as poc3  # noqa: E402
import poc_vx14 as poc1  # noqa: E402

from caddiej.coaching import vx  # noqa: E402
from caddiej.config import Settings  # noqa: E402


def clean(payload):
    out = json.loads(json.dumps(payload))
    for key, specs in vx.WIRE_LISTS.items():
        lst = out["checkpoints"].get(key)
        if not isinstance(lst, list):
            continue
        fixed = []
        cam = None
        for i, e in enumerate(lst):
            if key.endswith("_any") and i == 0 and isinstance(e, list) and e and e[0] in (1, 2) \
                    and len([x for x in lst if isinstance(x, list) or x is None]) == len(specs) + 1:
                cam = e[0]
                continue
            if isinstance(e, int):
                continue
            if isinstance(e, list) and all(x is None for x in e):
                e = None
            if isinstance(e, list) and len(e) == 3 and e[2] == 0:
                e = [e[0], e[1], None]
            fixed.append(e)
        out["checkpoints"][key] = ([cam] + fixed) if key.endswith("_any") and cam else fixed
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings()
    rows = poc1.load_cells()
    cams = hy.sheet(rows)
    record = json.loads(poc3.REPLY.read_text(encoding="utf-8"))
    payload = clean(record["payload"])
    raw_locate, evidence = poc3.grids(payload, cams)
    kept, _notes = hy.validate_locate(raw_locate, cams)
    entries, _used = poc3.checkpoint_entries(payload, evidence, cams, kept)
    accepted, rejected, unanchored = hy.assess(entries, cams, kept)
    canon = vx.canonical_reply({**vx.decode_wire({k: v for k, v in payload.items() if k in ("metrics", "coach")}, []),
                                "checkpoints": {}, "frames": {}}, {r["cell_id"]: r["frame_id"] for r in rows})
    s = hy.summary("POC3 salvage (diagnostic)", accepted, rejected, canon, rows, settings,
                   {"unanchored_accepts": unanchored})
    (poc3.OUT / "salvage.json").write_text(json.dumps(s, indent=1, default=str), encoding="utf-8")
    print(json.dumps(s, indent=1, default=str))


if __name__ == "__main__":
    main()
