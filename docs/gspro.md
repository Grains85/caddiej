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

`currentRound.dat` was observed to update after a real shot in a round. It was
observed **not** to update on the Practice Range. Range support is not claimed.
Numeric fields stay raw on `ObservedRoundShot`. The HUD formats two of them as
a hardware-derived presentation assumption (not a schema change):

- `BallSpeed` ≈ metres per second (a real value of ~32.8386 displayed as 118 km/h)
- `TotalDistance` ≈ metres (integer `m` on the overlay)

Surface/result enums stay unnamed integers. Launch-monitor club metrics (club
path, face angle, attack angle, spin, and so on) are not in these sources; a
later adapter can enrich a shot without this path inventing them.

OBS is output only: a Browser Source renders CaddieJ's HUD. CaddieJ does not
read OBS, OCR a screen, or scrape GSPro's UI.

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
