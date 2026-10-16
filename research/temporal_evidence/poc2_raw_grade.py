"""Grade POC 2's raw locate as given, before the order validator dropped DTL. No call."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import poc_vx14 as poc1  # noqa: E402

HERE = Path(__file__).parent
record = json.loads((HERE / "output" / "poc2" / "reply.json").read_text(encoding="utf-8"))
raw = record["payload"]["locate"]
located = {
    "dtl": [None if e is None else e[0] for e in raw],
    "fo": [None if e is None else e[1] for e in raw],
}
rows = poc1.load_cells()
for row in poc1.grade(located, rows):
    print(f"{row['camera']:<4}{row['position']:<4}{str(row['cell']):<8}#{row['capture_index']}  "
          f"truth {row['accepted_cells']}  {row['verdict']}")
