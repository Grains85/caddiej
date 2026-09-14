# The floor overlay

[← Documentation index](README.md)

CaddieJ serves the overlay at <http://127.0.0.1:8730/> and keeps running until
Ctrl+C. Opening that URL in any browser shows a large `CADDIEJ FLOOR ONLINE`
indicator on a transparent background. `GET /healthz` reports the served state
version, the number of connected clients, and what every subsystem is doing.

The page is a rendering client only. It holds no simulator state, renders
nothing until Python sends a snapshot, and reconnects on its own if CaddieJ
restarts or OBS reloads the source.

The overlay renders a ball-placement marker, a club-path line, and a shot-metrics
readout. Positions on the wire are **calibrated surface coordinates**: x and y
run 0 to 1 across the surface, so nothing in CaddieJ depends on the projector's
resolution.

That unit square is not a physical plane. A club-path angle is computed in
metres on `FLOOR_WIDTH_M` × `FLOOR_DEPTH_M` and then mapped onto the surface.
The default bay is 1 m × 1 m, which makes the two spaces the same number, so a
fresh checkout looks the way it always has. Set the real bay (4 × 3 is a
typical projected rectangle) when you want a 10° path to be 10° on the floor.

Where the marker sits is configured in metres on that same physical
floor. `FLOOR_TEE_X_M` / `FLOOR_TEE_Y_M` are the default (non-driver)
point. `FLOOR_TEE_DRIVER_X_M` / `FLOOR_TEE_DRIVER_Y_M` are the DRIVER
point. Both are mapped onto the surface the same way the club path is.
The browser only ever receives surface coordinates. A later wizard
measures the mat in this space; it is not a CSS transform in the
browser. Other club groups (putter, and so on) can be added as further
physical points without changing that mapping.

The old `FLOOR_TEE_X` / `FLOOR_TEE_Y` names were 0–1 fractions of the bay.
They still convert to metres if the `_M` variables are unset. Do not set
both. `FLOOR_CLUB_PATH_LENGTH` is metres, default 1.2 m.

## What the floor does during a shot

Shots drive the floor. The sequence, all decided in Python:

1. the simulator or ProTee log reports the club in play, and the floor shows
   the matching address marker at the club's physical tee — DRIVER at the
   driver point, everything else at the default point;
2. the launch monitor reports a ball on the mat, and the marker disappears so the
   projector is not shining onto the ball;
3. the shot is hit. An Open Connect shot shows the club's path and launch
   numbers for `FLOOR_SHOT_DISPLAY_SECONDS`. A GSPro round result stays until
   READY / a new ball, or `FLOOR_SHOT_RESULT_MAX_DURATION` (default 15 s);
4. the floor then goes back to showing where the next ball goes, using the
   currently detected club's tee.

Devices that report no ball detection, no readiness, or no club data are handled
by leaving out what they did not measure rather than guessing it. A launch
monitor that does not measure carry gets no carry on the floor.

Open Connect launch numbers are shown in yards and miles per hour. The observer
round-result panel is different: domain values stay raw, and the HUD formats
only two of them.

- **Ball speed** — `currentRound.dat` `BallSpeed` is treated as metres per
  second (a real session saw ~32.8386, which is ~118 km/h). The overlay shows
  an integer km/h, for example `118 km/h`.
- **Distance** — `TotalDistance` is treated as metres for the overlay (the
  file does not name a unit; the hitting-bay session used metres). The overlay
  shows an integer, for example `28 m`.

Those labels are presentation only. `ball_speed_raw` and `total_distance_raw`
are unchanged. Surface integers stay unnamed. See
[GSPro and a launch monitor](gspro.md).

## Reviewing the floor without a launch monitor

The demo walks the floor through every state it can render — each marker variant,
placements at the edges of the surface, show and hide, straight and offline club
paths, metrics appearing and clearing, and all five celebrations — on a loop
until you stop it:

```shell
FLOOR_DEMO=1 caddiej
```

`FLOOR_DEMO_STEP_SECONDS` sets the pace. This is a development aid: it is a
normal writer of floor state, so what you see is exactly what real shot data
will produce.

## Using OBS as the compositor

CaddieJ can point an OBS Browser Source at the overlay. The target scene must
already exist; provisioning is off by default because it modifies the active
scene collection:

```shell
OBS_PROVISION_FLOOR_SOURCE=1 caddiej
```

This creates or reconciles the `OBS_FLOOR_SOURCE` browser source in
`OBS_FLOOR_SCENE`, managing only its URL, size, and lifecycle toggles. The
toggles are forced off so the overlay stays loaded and keeps its WebSocket
instead of being suspended or refreshed on scene changes.

Provisioning is best effort: if OBS is not running or refuses the request, the
failure is logged and the overlay keeps serving.

The overlay does not depend on OBS. Displaying the page fullscreen in a browser
is a supported way to drive a projector.

## Observer HUD on the floor projector

The production overlay URL is:

`http://127.0.0.1:8730/`

OBS is output only. Add a Browser Source to the existing `CaddieJ Test` scene
(or `OBS_FLOOR_SCENE` if you renamed it):

| | |
| --- | --- |
| Source name | `CaddieJ Floor` (`OBS_FLOOR_SOURCE`) |
| URL | `http://127.0.0.1:8730/` |
| Width × height | `1920` × `1080` (`OBS_FLOOR_WIDTH` / `OBS_FLOOR_HEIGHT`) |

Do not enable shutdown/refresh when not visible if you want the WebSocket to
stay up. Then use OBS **Fullscreen Projector (Preview)** or **Fullscreen
Projector (Program)** on the floor projector output you already have working.
CaddieJ does not change that projector mapping.

`OBS_PROVISION_FLOOR_SOURCE=1` can create or reconcile that Browser Source if
the scene already exists. Manual setup is enough for this path.

OBS Studio needs its WebSocket v5 server enabled, and the password from
**Tools → WebSocket Server Settings** goes in `OBS_PASSWORD`.
