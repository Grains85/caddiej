"""Read-only look for stored coaching results of the saved shots."""

import sqlite3
import sys

c = sqlite3.connect("file:caddiej.sqlite3?mode=ro", uri=True)
tables = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
print(tables)
for t in tables:
    cols = [r[1] for r in c.execute(f"pragma table_info({t})")]
    n = c.execute(f"select count(*) from {t}").fetchone()[0]
    print(f"{t} rows={n} cols={cols}")
needle = sys.argv[1] if len(sys.argv) > 1 else "007e5459"
for t in tables:
    cols = [r[1] for r in c.execute(f"pragma table_info({t})")]
    for col in cols:
        try:
            hits = c.execute(f"select count(*) from {t} where cast({col} as text) like ?", (f"%{needle}%",)).fetchone()[0]
        except sqlite3.Error:
            continue
        if hits:
            print(f"HIT {t}.{col}: {hits}")
