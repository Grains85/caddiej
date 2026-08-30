# Shot history

[← Documentation index](README.md)

Every shot CaddieJ makes sense of is written to a SQLite file, `caddiej.sqlite3`
in the working directory by default. It is an ordinary file: copy it, open it
with the `sqlite3` shell, back it up, delete it.

```shell
HISTORY_DATABASE_PATH=~/golf/rounds.sqlite3 PLAYER_NAME="Your name" caddiej
```

A shot is stored with the session it belongs to, when it happened, the player
name if one is configured, handedness, the club, the normalized ball and club
measurements, and enough provenance to trace it back to the device that reported
it. Measurements a device did not report are stored as null rather than zero, so
an average is only ever computed from shots that were actually measured.

A *session* is one run of CaddieJ. There is no model of rounds or holes yet, and
guessing at hole boundaries would be invention rather than data. A run that
recorded no shots leaves nothing in the file.

Sessions can be summarised without any interpretation: mean, median, minimum,
maximum and standard deviation for every measurement, the same again per club,
and one row per shot of the direction, carry and spin a dispersion plot needs.
The arithmetic is plain Python over the stored rows, so the same shots always
produce the same summary.

Nothing on the shot path waits for the disk. A completed shot is queued and
written by a separate task, and if the database cannot be opened, or a write
fails, history disables itself and says so through `/healthz` rather than
interrupting play. Drops are counted and leave a gap in the shot numbering, so a
lost shot is visible rather than silent.

Set `HISTORY_ENABLED=false` to record nothing.

For diagnosing a launch monitor that reports something unexpected,
`HISTORY_RECORD_RAW_PAYLOADS=true` keeps the exact bytes each shot was read from,
beside the shot. They are opaque, nothing reads them, and they can be deleted
without affecting anything else.

The file stays on the machine that wrote it. Nothing uploads it anywhere.
