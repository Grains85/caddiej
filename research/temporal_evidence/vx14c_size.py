"""Offline: output size of vx14c wire variants, filled with POC 1's content. No call.

Calibrated with the same token proxy as hybrid_replay (POC 1-3 actual output tokens).
"""

import json
import sys
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).parent))
import hybrid_replay as hy  # noqa: E402
import vx14c_replay as c  # noqa: E402

HERE = Path(__file__).parent


def variant(payload, groups, any_cam, entries, mode):
    by_spec = {s.checkpoint_id.value: it for s, _c, _p, it in entries}
    phases = {}
    for p in range(9):
        s_list, x_list, strings = [], [], []
        for _cam, s in c.specs_of(p):
            it = by_spec.get(s.checkpoint_id.value)
            ok = isinstance(it, dict)
            d = (it.get("dir") or "none") if ok else "none"
            s_list.append([it["score"], round(it["conf"] * 100)] if ok else None)
            if mode == "all_dirs" or s.deviations:
                x_list.append(d)
            strings.append(f"{it['score']} {round(it['conf'] * 100)} {d}" if ok else None)

        def num(cell):
            return 0 if cell is None else int(cell.split("_")[1])

        head = {"d": num(groups["dtl"][p]), "f": num(groups["fo"][p]), "a": "f" if any_cam.get(p) == "fo" else "d"}
        if mode == "pattern":
            phases[c.PHASES[p].value] = {**head, "e": strings}
        else:
            phases[c.PHASES[p].value] = {**head, "s": s_list, "x": x_list}
    return {"phases": phases, "metrics": payload["metrics"], "coach": payload["coach"]}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    replies = [json.loads((HERE / f"output/{d}/reply.json").read_text(encoding="utf-8")) for d in ("poc", "poc2", "poc3")]
    ratio = mean(r["usage"]["output_tokens"] / hy.proxy(r["payload"]) for r in replies)
    p1 = replies[0]["payload"]
    groups, any_cam, entries, _s, _l = c.from_poc1(p1)
    out = {}
    for mode in ("all_dirs", "deviation_dirs_only", "pattern"):
        w = variant(p1, groups, any_cam, entries, mode)
        out[mode] = {"total": round(hy.proxy(w) * ratio),
                     "phases": round(hy.proxy(w["phases"]) * ratio)}
    out["fixed_parts"] = {"metrics": round(hy.proxy(p1["metrics"]) * ratio), "coach": round(hy.proxy(p1["coach"]) * ratio)}
    out["coarse_locate_extra"] = round(hy.proxy({"locate": [[13, 13]] * 9}) * ratio)
    (HERE / "output/vx14c/size.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
