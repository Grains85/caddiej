# First hardware validation

[← Documentation index](README.md)

> **File formats below were verified on real hardware. This observer path has
> not yet been run end-to-end in the hitting bay with CaddieJ started.**
> Interpose stages remain experimental.

The production target is passive observation. ProTee talks to GSPro; CaddieJ
reads a log and `currentRound.dat`. Stopping CaddieJ must not affect either.

```
ProTee VX ───────────► GSPro
    │                    │
    │ log                │ currentRound.dat
    │                    │
    └───────► CaddieJ ◄──┘
                 │
                 ▼
                OBS
                 │
                 ▼
          floor projector
```

Open Connect interpose is optional diagnostics, not this path.

## Validation profile

Copy the example, then confirm it:

```powershell
copy config\examples\observe.env .env
caddiej --check-config
```

`--check-config` is the preflight. It already reports that the
configuration is valid, the HUD URL, and (for `GSPRO_MODE=observe`) that
the shot source is passive observation rather than a bind on 9210.

Expect `--check-config` to mention `http://127.0.0.1:8730/` and
`passive observation`.

## Observer path — remaining bay checklist

Do this with ProTee pointed at GSPro, not at CaddieJ.

1. Start GSPro and ProTee as you already do for a round. Confirm a ball
   still plays with CaddieJ **stopped**.
2. `GSPRO_MODE=observe caddiej` from the directory that holds `.env`.
3. Open `http://127.0.0.1:8730/` (or the OBS Browser Source below).
4. `GET http://127.0.0.1:8730/healthz` — top-level `status` stays `ok`
   even if a round is not active. `protee_observer.status` may be
   `waiting` while ProTee is closed; `gspro_round_observer.status` may
   be `waiting` while `currentRound.dat` is absent.
5. Place a ball. HUD should show **READY** and the detected club
   (for example **DRIVER**) plus the existing tee marker.
6. Hit a shot **in a round**, not on the Practice Range.
7. HUD should show raw **BALL SPEED** / **DISTANCE** / **HOLE** / **SHOT**
   / **SURFACE** with no unit suffixes, and a **BALL FLIGHT** trace.
8. Stop CaddieJ. Hit another ball. ProTee and GSPro must still work.

Not yet validated in the bay with CaddieJ running: that this overlay is
readable on the floor projector, and that daily ProTee log rotation
during a session is as the unit tests describe.

OBS Browser Source (manual is enough; do not change the working
projector output):

| | |
| --- | --- |
| URL | `http://127.0.0.1:8730/` |
| Source name | `CaddieJ Floor` |
| Width × height | 1920 × 1080 |
| Scene | existing `CaddieJ Test` |
| Output | existing Fullscreen Projector to the floor projector |

Launch-monitor club metrics (club path, face, attack, spin) are still
unresolved and must not be guessed from this path.

## Interpose profile (experimental)

The older Open Connect interposer profile remains at
`config/examples/hardware-validation.env`. It is not required for observer
mode. `--check-config` never prints a secret. There is no
`--hardware-preflight` command: a second flag would be another thing to
remember for the same facts.

Also note, from the same terminal:

```powershell
python --version
```

Expect Python 3.12 or newer. Windows CI already runs the default test
suite, loopback GSPro/HUD socket tests, `caddiej --version`, and
`caddiej --check-config` on `windows-latest`.

What `--check-config` does not repeat (read them from `.env`):

- floor width, depth, tee, and club-path length — all **metres**
- `HISTORY_RECORD_RAW_PAYLOADS=true`
- `GSPRO_IDLE_TIMEOUT` is omitted so the conservative 180 s default applies
  (see [Hardware assumptions that must be measured](#hardware-assumptions-that-must-be-measured))

## First-run feature policy

Keep these **off** until a real shot has appeared on the overlay and in
history, and reconnect has been shown to work:

| Subsystem | How this profile turns it off |
| --- | --- |
| Swing video | `VIDEO_ENABLED=false` |
| Cloud / OpenAI coaching | `COACHING_PROVIDER=none` |
| Speech | `SPEECH_ENABLED=false` |
| Hue lighting | `LIGHTING_ENABLED=false` |
| Celebrations / effects | `EFFECTS_ENABLED=false` |
| Assistant API | `ASSISTANT_API_ENABLED=false` |
| OBS provisioning | `OBS_PROVISION_FLOOR_SOURCE=false` |

Template coaching (`COACHING_PROVIDER=none`) names measured faults and
does not leave the machine. It is not required for the first path.
Leave the provider at `none`. Do not set `COACHING_PROVIDER=openai`.

OBS may host an **already-created** Browser Source pointed at
`http://127.0.0.1:8730/`. CaddieJ does not need to open a WebSocket to
OBS for that.

## Windows launch, logs, and health

Start from the repository directory that contains `.env`. JSON lines go
to stderr; Tee-Object keeps a durable copy. Logging is already
non-blocking inside CaddieJ — the file is for inspection after the
session, not to protect the event loop.

```powershell
$log = "caddiej-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
caddiej 2>&1 | Tee-Object -FilePath $log
```

Do not put passwords, API keys, or tokens on this command line. This
profile contains none.

In a second PowerShell window, from the same directory:

```powershell
Get-Content .\caddiej-*.log -Wait -Tail 40
```

If more than one log exists, pass the exact filename.

Health, once, then every two seconds. Built-in cmdlets only — no `jq`,
no extra modules:

```powershell
Invoke-RestMethod http://127.0.0.1:8730/healthz | ConvertTo-Json -Depth 8
```

```powershell
while ($true) {
  Invoke-RestMethod http://127.0.0.1:8730/healthz | ConvertTo-Json -Depth 8
  Start-Sleep -Seconds 2
}
```

Open the overlay at <http://127.0.0.1:8730/>.

## Stages

### Stage A — target Windows PC, simulate mode

Prove the Windows install, overlay, and history **without** ProTee or
GSPro. Keep the hardware `.env`, override only the shot source:

```powershell
$env:GSPRO_MODE = "simulate"
caddiej --check-config
caddiej 2>&1 | Tee-Object -FilePath stage-a.log
```

Expect: floor cycles through address, a ball, a path, and numbers;
`caddiej.sqlite3` gains rows; `/healthz` `gspro.link_state` becomes
`bridged` once the fake device is in. Ctrl+C stops it.

Then `$env:GSPRO_MODE = $null` so later stages read `interpose` from
`.env`.

### Stage B — real GSPro + CaddieJ + fake launch monitor

Start GSPro with Open Connect listening on `127.0.0.1:921` (change
`GSPRO_UPSTREAM_*` if yours differs). Start CaddieJ with the hardware
profile (`GSPRO_MODE=interpose`). Point nothing at GSPro except
CaddieJ.

From a second venv-activated terminal, send one heartbeat and one shot
with CaddieJ's own fake device (not a product command — a one-off
against the real interposer):

```powershell
python -c @"
import asyncio
from caddiej.adapters.gspro.fakes import FakeLaunchMonitor
from caddiej.adapters.gspro.interposer import StreamPair

async def main():
    reader, writer = await asyncio.open_connection('127.0.0.1', 9210)
    monitor = FakeLaunchMonitor(StreamPair(reader, writer, peer='stage-b'))
    await monitor.send_heartbeat(ready=True, ball_detected=False)
    await asyncio.sleep(0.3)
    await monitor.send_heartbeat(ready=True, ball_detected=True)
    await asyncio.sleep(0.3)
    await monitor.hit_shot()
    await asyncio.sleep(1)
    writer.close()
    await writer.wait_closed()

asyncio.run(main())
"@
```

Expect: `/healthz` `gspro.link_state` is `bridged` while the fake is
connected; `gspro.shots_ingested` increments; GSPro shows the shot;
the overlay shows a path and numbers; history records one row.

### Stage C — real ProTee VX through CaddieJ into real GSPro

Point the ProTee Open Connect connector at `127.0.0.1:9210` (or the
bind host/port you set). This is the first unproven fact: whether that
connector accepts a host and port that are not GSPro's.

Hit one ball. Watch GSPro, the overlay, `/healthz`, the log, and
history together. Do not enable video, a cloud model, speech, Hue,
effects, or the assistant API.

### Stage D — reconnect / restart / failure

One change at a time. After each, hit a ball if the path is up, and
write down `link_state` and the counters.

1. Disconnect ProTee, reconnect it.
2. Quit and restart GSPro while ProTee is connected.
3. Restart CaddieJ mid-session. Note whether GSPro sends a `Player`
   block and whether the next shot is attributed to a club.
4. Stop GSPro and leave ProTee connected — CaddieJ should not accept
   shots into a void (`simulator_unavailable`, launch-monitor
   connection closed).
5. Let the link sit idle past `GSPRO_IDLE_TIMEOUT` if you are
   measuring half-open / silent-device behaviour.

### Stage E — OBS Browser Source reconnect / reload

If OBS is compositing the overlay, do **not** turn on
`OBS_PROVISION_FLOOR_SOURCE`. Use a Browser Source you already created.

Reload the source. Restart OBS. Confirm the overlay reconnects
(`/healthz` `clients`, `hud_client_connected` in the log) and that a
later shot still draws.

### Stage F — graceful shutdown with an in-flight final shot

Hit a ball and press Ctrl+C while the shot is still on the floor (or
immediately after impact). History `shots_recorded` should still
include that shot after process exit. A second Ctrl+C leaves
immediately; avoid that if you are testing drain.

## Health fields to watch

`GET /healthz` is a JSON object. Top-level keys always include
`status`, `protocol_version` (currently `1`), `state_version`, `mode`,
and `clients`. The profile also includes the subsystem objects below.
Names are taken from the running code. Do not invent others.

### `gspro`

Source: `GSProConnectAdapter.health()`. Absent adapter (`GSPRO_MODE=off`,
or simulate before the fake device exists) is the disabled zero
snapshot with `link_state` `"disabled"`.

| Field | Meaning |
| --- | --- |
| `link_state` | `disabled`, `listening`, `monitor_connected`, `bridged`, `simulator_unavailable`, `bind_failed` |
| `shots_ingested` | Shots published into the domain |
| `messages_rejected` | Frames CaddieJ could not parse (still forwarded) |
| `duplicates_ignored` | Retransmissions CaddieJ ignored internally |
| `reused_shot_numbers` | Device reused a shot number in a way that is not a duplicate |
| `late_data_discarded` | Split-shot half arrived after the shot was already published |
| `frames_dropped` | Interposer queue could not accept a frame |

### `event_bus`

Source: `EventBusHealth` / `SubscriberHealth`.

| Field | Meaning |
| --- | --- |
| `running` | Bus is RUNNING, DRAINING, or STOPPING |
| `dropped` | Sum of subscriber drops |
| `coalesced` | Sum of subscriber coalesces |
| `refused` | Sum of **critical refusals / overruns** (`SubscriberOverrun`) |
| `handler_failures` | Sum of handler exceptions |
| `subscribers` | Per-subscriber objects |

Each subscriber:

| Field | Meaning |
| --- | --- |
| `name` | Subscriber name (`play`, `history`, …) |
| `queue_depth` | Events waiting |
| `queue_max` | Bound |
| `dropped` | This subscriber dropped |
| `coalesced` | This subscriber coalesced |
| `refused` | This subscriber refused a critical event |
| `handler_failures` | Exceptions from this subscriber's handlers |
| `oldest_queued_seconds` | Age of the oldest queued event, or `null` |
| `overrun` | `true` after a critical refusal |

`refused` / `overrun` are the critical-path stop signals. `dropped` and
`coalesced` on enrichment subscribers (coaching, video) are expected
when those subsystems are busy; they should stay 0 on `play` and
`history` during this session.

### `history`

Source: `HistoryHealth`.

| Field | Meaning |
| --- | --- |
| `enabled` | `HISTORY_ENABLED` |
| `available` | Database is usable |
| `path` | SQLite path, or `null` if disabled |
| `schema_version` | Schema CaddieJ opened |
| `session_id` | This run, or `null` |
| `shots_offered` | Shots handed to the writer |
| `shots_recorded` | Shots that reached the disk |
| `shots_queued` | Writer queue depth |
| `shots_dropped` | Writer refused (queue full) |
| `write_failures` | Disk/SQLite write errors |
| `read_failures` | Read errors |
| `last_error` | Last failure text, or `null` |

Compare `history.shots_recorded` to `gspro.shots_ingested` after the
writer has had a moment. They should match for this profile (one
session, nothing else ingesting).

### Logging overflow

`LoggingStats` is **not** on `/healthz`. A full logging queue drops the
record and, on the next line that does get out, writes
`logging_queue_overflow` with `logging_dropped`. Search the log file
for that event name.

## Raw payload capture

`HISTORY_RECORD_RAW_PAYLOADS=true` is on for the first session so a
misparsed or surprising ProTee message can be compared to the bytes
CaddieJ actually associated with a **recognized shot**.

What it stores: the Open Connect JSON frames that were assembled into
that shot, joined with newlines, truncated at
`HISTORY_RAW_PAYLOAD_MAX_BYTES` (8192 here). Undecodable bytes are
replaced, not dropped.

What it does **not** store: heartbeats, GSPro responses, rejected
frames, duplicates, incomplete club-only fragments, or anything that
never became a `ShotIngested`. It is not a packet capture.

Wireshark + Npcap on the loopback/bind path is optional if you need
the full conversation. It is not a CaddieJ dependency and is not
required for Stage C.

## Stop conditions

Stop adding variables. Diagnose immediately if:

- `gspro.frames_dropped` > 0
- `event_bus.refused` > 0, or any subscriber `overrun` is `true`
- `event_bus.handler_failures` > 0 — a handler exception in `play` or
  `history` can mean the authoritative shot was not fully processed even
  when TCP forwarding to GSPro stayed healthy
- `gspro.messages_rejected` > 0 and you cannot explain the frame
- the log contains `gspro_forward_stalled`
- `gspro.link_state` unexpectedly falls back from `bridged` to
  `listening` while ProTee should still be connected
- the log contains `gspro_link_idle` during a normal connected session
- a real shot is missing or duplicated on the overlay, in GSPro, or in
  history
- GSPro visibly receives a shot late (CaddieJ must not add forwarding
  latency)
- `history.shots_recorded` diverges from `gspro.shots_ingested` after
  the writer has drained
- CaddieJ cannot recover from a ProTee or GSPro reconnect

## Hardware assumptions that must be measured

Fakes cannot establish these. Write the answer down during Stage C/D.

- Can the ProTee VX connector target `127.0.0.1:9210`, or another
  custom host and port?
- Is a heartbeat present?
- What is the heartbeat interval?
- What is the heartbeat payload shape?
- Is `LaunchMonitorBallDetected` present, and what does it mean?
- Is `LaunchMonitorIsReady` present, and what does it mean?
- What `Units` value is sent, and what units are the speed/distance
  fields actually in?
- Is a shot one message, or split ball / club?
- If split, what is the time gap?
- How does `ShotNumber` behave (increment, reuse, restart)?
- Does ProTee retransmit after reconnect?
- Does GSPro send a `Player` block on connect?
- Does GSPro send `Player` on 200 as well as 201?
- After a CaddieJ restart, is the next shot attributed to a club?
- Is there added forwarding latency versus ProTee → GSPro direct?
- Does ProTee close with a clean FIN, or can the process disappear
  half-open?

The hardware profile omits `GSPRO_IDLE_TIMEOUT`, so the conservative
180 s default applies. Reduce it only after the ProTee heartbeat /
idle behaviour has been measured. A short unmeasured value can close a
live monitor connection during a normal pause and make CaddieJ less
transparent than direct ProTee → GSPro. Do not treat TCP keepalive as
application-level half-open detection.

## Floor dimensions

`FLOOR_WIDTH_M`, `FLOOR_DEPTH_M`, `FLOOR_TEE_X_M`, `FLOOR_TEE_Y_M`,
and `FLOOR_CLUB_PATH_LENGTH` are metres. The example file keeps the
repository defaults (1 m × 1 m identity). Change them to the real bay
before treating a drawn path angle as the launch-monitor number on a
non-square floor. Do not commit a measured room back to the repository
unless it is a generic default.
