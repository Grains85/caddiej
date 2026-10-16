# GSPro and a launch monitor

[← Documentation index](README.md)

Production CaddieJ is a **passive observer**. ProTee VX talks to GSPro directly.
CaddieJ reads two files that have been verified on real hardware:

- ProTee's semantic daily log under `%APPDATA%\ProTeeUnited\Logs\`
- GSPro's `currentRound.dat` (UTF-8 JSON despite the extension) during **round play**

```shell
GSPRO_MODE=observe caddiej
```

CaddieJ does not listen on 9210, does not connect to GSPconnect, and does not
change ProTee or GSPro configuration. If CaddieJ stops, both keep working.

ProTee's log also records GSPro's already-received Code 201 player-info JSON,
for example ``Player.DistanceToTarget``. That value is live remaining distance
for the upcoming shot. CaddieJ reads it from the log; it does not intercept
the Open Connect socket. ``Player.Surface`` has only been observed as JSON
null and is not shown. Elevation, wind, and lie percentage are not in these
sources as named current-hole fields. See [Wind and elevation](#wind-and-elevation).

`currentRound.dat` was observed to update after a real shot in a round. It was
observed **not** to update on the Practice Range. Range support is not claimed.
Numeric fields stay raw on `ObservedRoundShot`. The HUD formats two of them as
a hardware-derived presentation assumption (not a schema change):

- `BallSpeed` ≈ metres per second (a real value of ~32.8386 displayed as 118 km/h)
- `TotalDistance` ≈ metres (integer `m` on the overlay)

Surface/result enums stay unnamed integers. Launch-monitor club metrics (club
path, face angle, attack angle, spin, and so on) are not in these sources; a
later adapter can enrich a shot without this path inventing them.

OBS is output only: a Browser Source renders CaddieJ's HUD. Optionally CaddieJ
toggles visibility of an existing OBS Window Capture of ProTee's Shot Video
panel; it does not read OBS, OCR a screen, or scrape GSPro's UI.

## Wind and elevation

These would be useful for club selection on the address HUD. They are **not**
on the projected floor, because no passive source in this repository has a
verified current-hole value with known units.

Observed fields, and why they were not used:

| Source | Field | Observed example | Likely semantics | Units | Status |
| --- | --- | --- | --- | --- | --- |
| ProTee log, Code 201 `Player` | `DistanceToTarget` | `16.8`, `347.0` | remaining distance for the **next** shot | metres (verified against `DistanceToPin`) | used for address |
| ProTee log, Code 201 `Player` | `Handed`, `Club` | `"RH"`, `"DR"` | GSPro auto-club / handedness | n/a | club ignored for recommendation |
| ProTee log, Code 201 `Player` | `Surface` | JSON `null` only | unknown | n/a | ignored |
| `currentRound.dat` | `DistanceToPin` | `16.7867889` | remaining after **that** shot | metres (verified) | used for result |
| `currentRound.dat` | `TotalDistance` | `35.46979` | how far that shot went | metres (verified by hole arithmetic) | used for result |
| `currentRound.dat` | `StartingPOS` / `EndingPOS` / `BallStartPos` `x,y,z` | e.g. `z: 1000.0` | world / trajectory coordinates, not a target-elevation change | unverified | not shown |
| `currentRound.dat` `activeShot.sd.path[].pos` | flight samples | e.g. `1031.43` | the **tail** of the simulated trajectory, in world coordinates | unverified | diagnostic chart, and a putt roll when it passes the checks in [Is there a putt path in here?](#is-there-a-putt-path-in-here) |
| `currentRound.dat` | `isPutt` | `false` on all 196 shots in the capture | whether GSPro called it a putt | n/a | not usable; CaddieJ decides from the club in play |
| `currentRound.dat` | `TargetDirection` | `0.0` on all 196 | aim line | degrees, presumably | not populated, so no aim-referenced path |
| `currentRound.dat` | `BallStopPos` | `(0, 0, 0)` on all 196 | where the ball came to rest | n/a | not populated; `EndingPOS` is used instead |
| Open Connect `ClubData.Lie` | club lie angle | fake LM `0.0` | club lie, not course elevation | degrees (spec) | not a floor value |

No captured Code 201 payload and no captured `currentRound.dat` shot object in
this repository contains a field named `Wind`, `WindSpeed`, `WindDirection`,
`Elevation`, `TargetHeight`, or similar.

A useful real-hardware capture would be one hole with:

1. a known GSPro wind setting (speed and direction) and a known tee-to-green
   elevation change, written down from GSPro's own UI before the shot;
2. the ProTee `GSPro read data` Code 201 line immediately before the shot, copied
   in full including any keys beyond `Handed` / `Club` / `DistanceToTarget` /
   `Surface`;
3. the `currentRound.dat` object for that shot, copied in full.

Until those two files show the same numbers the UI showed, with identifiable
units, CaddieJ will not put wind or elevation on the floor.

## Is there a putt path in here?

Sometimes, and rarely enough that the floor's answer is usually no. This
was settled by reading a real `currentRound.dat` off the hitting bay:
196 shots, 191 of them under twelve metres.

`activeShot.sd.path` is a **rolling buffer holding the tail of the
trajectory**, not the trajectory. The longest shot in the capture
travelled 101 m and left thirteen samples, the first of which was
already 95 m down-range. The last sample is always exactly `EndingPOS`,
so the buffer reliably *ends* at the shot and unreliably begins at it.
Going the other way, for a short shot it reaches back past the start:
the final entry in the capture moved 0.124 m and its path spanned
0.807 m, including a 0.17 m drop in `pos.y`, which is a ball being
placed on a green rather than a ball being rolled.

Nothing else in the file rescues it. `isPutt`, `TargetDirection` and
`BallStopPos` were unpopulated on all 196 shots, so GSPro does not say
that a shot was a putt, does not say where the player aimed, and does
not separately record where the ball stopped.

So a roll is drawn only when the buffer demonstrably contains the whole
of one shot and nothing else: at least three samples, a plan distance of
at least 0.15 m from `StartingPOS` to `EndingPOS`, a first usable sample
within 8 cm of `StartingPOS`, under 5 cm of vertical movement across the
samples, and distances to the resting place that never increase. Failing
any of those is `floor_putt_path_unavailable` with a reason, not a
straightened line. See [After a putt](floor.md#after-a-putt) for how the
trace is placed and rotated when it does pass.

The capture behind those thresholds contains no shot GSPro identified as
a putt, so whether a real putt's buffer passes them is an open question
that only hardware answers. Step H of
[hardware validation](hardware-validation.md) collects it.

## Experimental: sitting in the Open Connect path

GSPro's Open Connect interface runs in the opposite direction to what the name
suggests: **GSPro is the server** and the launch monitor connects to it and pushes
shots. The interposer remains for diagnostics. It is not required for observer
mode.

```shell
GSPRO_MODE=interpose caddiej
```

Then point the launch monitor connector at `GSPRO_BIND_PORT` (9210 by default)
instead of GSPro's own port. Nothing else changes: GSPro sees the same traffic it
would have seen, in the same order, unmodified. If anything goes wrong, point the
connector back at GSPro and CaddieJ is out of the path entirely.

CaddieJ never edits, delays, or invents traffic. A message CaddieJ cannot parse
is still forwarded, and a duplicate shot CaddieJ ignores internally still reaches
GSPro, because GSPro's view of the round is GSPro's to decide. If GSPro cannot be
reached, the launch monitor's connection is closed rather than accepting shots
into a void, so the device behaves exactly as it would if CaddieJ were absent.

A bridged connection that goes quiet for longer than `GSPRO_IDLE_TIMEOUT` is
dropped, so a launch monitor that vanished without closing its socket — a laptop
lid, a dropped wireless link — cannot hold the single bridge slot for the rest of
the evening.

The first real ProTee VX + GSPro session has a known-minimal profile and a
stage list in [First hardware validation](hardware-validation.md).

## Playing a round without GSPro

A scripted round — driver, iron, wedge, putt, on a loop — can be played by a fake
device against a fake GSPro inside the process:

```shell
GSPRO_MODE=simulate caddiej
```

This is a simulated *device*, not a simulated pipeline: the bytes go through the
real framing, parsing, assembly and translation, so what appears on the floor is
what a real round produces. `GSPRO_SIMULATION_STEP_SECONDS` sets the pace. It
binds two loopback ports and is for development only.

## Adding a different launch monitor

Any device that connects to GSPro over Open Connect works through the interposer
above without changes, because CaddieJ reads the conversation rather than the
device.

A device that speaks its own protocol needs a new adapter. The domain models are
vendor-neutral — normalization happens in `adapters/gspro/translate.py` and
nowhere else, and a test enforces that — but the wiring is still named for GSPro:
`GSPRO_MODE`, `_start_gspro` in `app.py`, and the `ShotSource.kind` literal all
assume it. See the adapter checklist in
[CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-new-adapter), and open an issue
first: generalizing that seam is worth doing once, properly.
