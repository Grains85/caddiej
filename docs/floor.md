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

Where the marker sits is still configured by eye — `FLOOR_TEE_X` and
`FLOOR_TEE_Y` — until a wizard measures the mat. A later homography of the
projected quadrilateral will reuse that same mapping; it is not a CSS
transform in the browser.

## What the floor does during a shot

Shots drive the floor. The sequence, all decided in Python:

1. the simulator reports the club in play, and the floor shows the matching
   address marker — wide for a driver, flat for a putter, standard for anything
   in between;
2. the launch monitor reports a ball on the mat, and the marker disappears so the
   projector is not shining onto the ball;
3. the shot is hit, and the floor shows the club's path and the launch numbers;
4. a few seconds later the floor clears and goes back to showing where the next
   ball goes. `FLOOR_SHOT_DISPLAY_SECONDS` sets how long.

Devices that report no ball detection, no readiness, or no club data are handled
by leaving out what they did not measure rather than guessing it. A launch
monitor that does not measure carry gets no carry on the floor.

Distances and speeds are shown in yards and miles per hour. There is no metric
display; see [Known limitations](../README.md#known-limitations).

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

OBS Studio needs its WebSocket v5 server enabled, and the password from
**Tools → WebSocket Server Settings** goes in `OBS_PASSWORD`.
