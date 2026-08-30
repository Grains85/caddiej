# GSPro and a launch monitor

[← Documentation index](README.md)

> **Not yet run against real GSPro or a real launch monitor.** This path is
> developed against a fake device and a fake simulator that speak the same
> protocol. See [Project status](../README.md#project-status).

GSPro's Open Connect interface runs in the opposite direction to what the name
suggests: **GSPro is the server** and the launch monitor connects to it and pushes
shots. There is no observer interface, so the only way for CaddieJ to see a shot
is to sit in the conversation.

CaddieJ therefore accepts the launch monitor's connection itself and forwards
everything to GSPro verbatim, in both directions, before it parses any of it:

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
