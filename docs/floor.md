# The floor overlay

[← Documentation index](README.md)

CaddieJ serves the overlay at <http://127.0.0.1:8730/> and keeps running until
Ctrl+C. Opening that URL in any browser shows a large `CADDIEJ FLOOR ONLINE`
indicator on a transparent background. `GET /healthz` reports the served state
version, the number of connected clients, and what every subsystem is doing.

The page is a rendering client only. It holds no simulator state, renders
nothing until Python sends a snapshot, and reconnects on its own if CaddieJ
restarts or OBS reloads the source.

The overlay renders a hitting-strip rectangle, a ball-placement marker, and
one information area for remaining distance, recommended club, or the last
shot's distance. Positions on the wire are
**calibrated surface coordinates**: x and y run 0 to 1 across the surface, so
nothing in CaddieJ depends on the projector's resolution.

That unit square is not a physical plane. A club-path angle is computed in
metres on `FLOOR_WIDTH_M` × `FLOOR_DEPTH_M` and then mapped onto the surface.
The default bay is 1 m × 1 m, which makes the two spaces the same number, so a
fresh checkout looks the way it always has. Set the real bay (4 × 3 is a
typical projected rectangle) when you want a 10° path to be 10° on the floor.

## The hitting strip is the anchor

The hitting strip is a rectangle on that physical floor.
`FLOOR_HITTING_STRIP_X_M` and `FLOOR_HITTING_STRIP_Y_M` are the
**centre** of it — not a corner, not a fraction of the bay — and
`FLOOR_HITTING_STRIP_WIDTH_M` / `FLOOR_HITTING_STRIP_DEPTH_M` are metres
of turf across and towards the player. The corners go through
`FloorCalibration` like everything else; a homography can turn the
rectangle into a quadrilateral on the surface, and the browser only ever
receives the four mapped points.

That centre is also where everything golf-related on the floor is
measured from. Each anchored element is a metre offset, added in world
coordinates before the surface transform:

```
physical/projector calibration
  ↓  FLOOR_WIDTH_M × FLOOR_DEPTH_M
golf-room metres
  ↓  FLOOR_HITTING_STRIP_X_M, FLOOR_HITTING_STRIP_Y_M
strip centre — the anchor
  ↓  FLOOR_BALL_*_D[XY]_M, FLOOR_INFORMATION_D[XY]_M
where a thing is drawn
```

`FloorLayout` in `floor/layout.py` owns that arithmetic and is the only
place it happens. Moving the mat, or re-aiming the projector at a
different part of the room, is the two anchor variables and nothing
else: the three ball positions, the pre-shot distance, the club
recommendation, the shot distance, the remaining distance and the
putting lane all travel with it and keep their spacing.
`caddiej --check-config` prints the anchor and the resolved positions in
metres, which is what to hold a tape measure against.

Three things are deliberately **not** anchored, because they are
properties of the projection rather than of the hitting area: the
startup banner, the celebrations, and the club path drawn from a shot —
the last of those starts at the ball it was hit from, which is an
anchored position already.

A position that lands outside the projected surface is clamped to the
edge and logged once as `floor_ball_position_off_surface` with the value
that was configured, rather than refused: a putter position half a metre
behind a mat that sits at the back of the bay is a plausible thing to
ask for, and a silently missing marker is not a useful answer.

## Three ball positions

There are three configured ball placements, chosen by the club in play:

| Club | Offset from the strip centre |
| --- | --- |
| Driver | `FLOOR_BALL_DRIVER_DX_M`, `FLOOR_BALL_DRIVER_DY_M` |
| Putter | `FLOOR_BALL_PUTTER_DX_M`, `FLOOR_BALL_PUTTER_DY_M` |
| Everything else, and no club at all | `FLOOR_BALL_IRON_DX_M`, `FLOOR_BALL_IRON_DY_M` |

The mapping is one function, `placement_mode_for`, over the club **kind**
the domain already decided — not a string comparison on whatever ProTee
wrote. A club CaddieJ could not classify falls back to the iron
position, which is the position a player standing on a mat is most
likely to want, and the fallback is a deliberate case rather than a
missing branch.

The putter position is allowed to sit off the mat. Putting from the
hitting strip is a choice about the room, not something the floor should
insist on, and the strip's rectangle has no say in where a ball marker
may go.

Both are mapped onto the surface the same way the club path is: the
browser only ever receives surface coordinates. A later wizard measures
the mat in this space; it is not a CSS transform in the browser.

`FLOOR_HITTING_STRIP_FILL=true` fills the whole polygon rather than outlining
it, which is what makes the projector a work light over the mat in a dark bay.
The fill is a flat near-white with a green cast, because a saturated colour at
that opacity reads as a shape and lights nothing, and the ball marker inverts to
dark so it stays readable on top of it. Nothing animates: something moving under
a player's feet while they are over the ball is a distraction. The strip is not
a substitute for the ball marker.

**The strip stays up for as long as the floor does.** It is lit when CaddieJ
starts and it goes out when CaddieJ stops, and nothing in between touches it —
not READY, not FINDING BALL, not a ball on the mat, not the ball marker, not the
address numbers, not a round result, not a Shot Video replay. It is the light
over a physical room rather than a state of a shot.

It did not always work that way. Lighting the pad used to be something the
address marker did on its way past, which meant hiding the marker put the light
out with it: on hardware the bay went dark the instant ProTee detected a ball,
which is the exact moment somebody is standing in it. Turning the pad on and
off is now its own pair of calls on `FloorPort`, and nothing on the address path
may make either of them.

Defaults are a generic pad, not a measured room. Calibrate them on
the workstation after deploy; changing them requires a CaddieJ restart.

`FLOOR_TEE_X_M` / `FLOOR_TEE_Y_M` and `FLOOR_TEE_DRIVER_X_M` /
`FLOOR_TEE_DRIVER_Y_M` used to be absolute points for the two markers.
A bay that still sets them keeps working — they are converted to iron
and driver offsets by subtracting the anchor at start-up — but they are
no longer in `.env.example`, and setting both them and the offsets is
not supported. The offsets win. The older `FLOOR_TEE_X` / `FLOOR_TEE_Y`
names were 0–1 fractions of the bay and still convert to metres first.
`FLOOR_CLUB_PATH_LENGTH` is metres, default 1.2 m.

## Four layers

The floor is four things drawn at once, and each is decided on its own. They
overlap in space but never in meaning, and no decision about one may change
another:

| | Layer | Lives for | Decided by |
| --- | --- | --- | --- |
| A | **The hitting area** | the whole session | lit at start-up, out at shutdown |
| B | **Address information** — remaining distance, recommended club | while the next shot is being prepared | what GSPro says is left, and the configured bag |
| C | **The shot result** — `SLAG`, `KVAR` | the readable window after a GSPro result | `currentRound.dat` |
| D | **The ball marker** | the placement lifecycle | ball detection and readiness |

B and C are the same part of the surface at two different moments, so exactly
one of them is ever on the floor; that is the only relationship between any two
layers, and it is enforced in Python rather than by stacking order in a
stylesheet. A is independent of everything. D runs on its own and a result above
it does not hold it back. When a ball is found, A gives one short pulse — a
variation on the work light, not a fifth layer that can put it out.

The launch monitor's own words for its state — READY, FINDING BALL — still
travel on the wire, still appear in `GET /snapshot` as `play_status`, still
drive B and D, and are still the first thing to look at when a bay misbehaves.
They are not drawn. They used to be, in the same place as the result, which is
how FINDING BALL came to be sitting on top of a shot distance somebody was
trying to read.

What the player sees instead is the strip's own rear edge, stepped out into a
chamfer that says **BALL READY** or **PLACE BALL**. It is part of the hitting
strip, behind the mat, not a panel and not the information area. Only `READY`
lights the green lamp and says BALL READY. FINDING BALL, a shot just sent, and
a device that has not spoken yet all say PLACE BALL, in the same geometry, with
the lamp subdued. The device words stay on a hidden element as `data-`
attributes, so a live bay can still be inspected in the DOM.

The chamfer is the Compact drawing: 17.8 mm rise, 13.9 mm run, 79.8 mm span,
0.8 mm stroke, 9.5 mm type, 1.8 mm lamp. `BALL_STATUS_CHAMFER_SCALE` multiplies
every one of those, and the gaps, by the same factor. At the default 6 that is
106.8 mm rise, 83.4 mm run, 478.8 mm span, 4.8 mm stroke, 57 mm type, and a
10.8 mm lamp. The feet stay on the rear edge either way. Those millimetres are
measured from the strip's rear edge in the same physical frame as the rest of
the floor, then mapped through `FloorCalibration`. Moving the mat, or changing
`FLOOR_WIDTH_M` / `FLOOR_DEPTH_M`, moves the chamfer with the strip. The scale
does not. A failure to draw it is logged as `ball_status_presentation_failed`
and does not affect the marker, the distance, or the result.

## What the floor does during a shot

Shots drive the floor. The sequence, all decided in Python:

1. the simulator or ProTee log reports the club in play, and the floor shows
   the matching address marker at that club's ball position — driver,
   putter, or iron for everything else and for nothing at all. The hitting
   strip is already lit and stays that way;
2. at READY or FINDING BALL — before a ball has been detected, which is when
   the player is choosing what to hit — the address information shows how far
   is left and which club that calls for;
3. the launch monitor reports a ball on the mat and the marker stands down so
   the projector is not shining onto the ball. The already-lit hitting strip
   gives one short pulse (about 0.65 s) and a check mark at the last marker
   position, then returns to a steady work light. Repeated "ball present"
   observations do not pulse again. The address information stays: it is a
   different layer and a different question;
4. the shot is hit. ProTee's camera trigger is the earliest thing CaddieJ knows
   about it, and it is what starts the Shot Video replay — see
   [Shot Video](#shot-video-replay). The address information goes, and so does
   the target distance behind it: the ball has gone, so the distance it was
   about to be played over is history. An Open Connect shot shows the club's
   path and launch numbers for `FLOOR_SHOT_DISPLAY_SECONDS`;
5. GSPro's round result arrives later, because `currentRound.dat` cannot be
   written until the ball has stopped — measured at 3.1 s after impact for a
   chip and about 6.4 s for a full shot. It then follows the lifecycle below;
6. the floor goes back to showing where the next ball goes, using the
   currently detected club's tee, and the address information returns with the
   distance GSPro sent **after** this shot. A completed shot's club does not own
   the next address: READY forgets it until ProTee names the upcoming club.

### What the marker waits for

The marker answers one question — *where do I put the ball* — so it is
on the floor whenever that question is open, and off it only when it
would be shining at a ball that is already there or sitting under a
shot the player is reading. Two facts decide it, and readiness is not
one of them:

* a ball detected on the mat takes it away;
* a shot on the floor takes it away, until the round result comes up,
  which is a different part of the surface and does not hold it back.

The readiness the device reports is deliberately not in that list, and
that is the regression. `FINDING BALL` is what a real bay says for most
of the time between shots, so a marker that waited for `READY` was a
marker that spent the gap between shots hidden — visible at start-up,
gone after the first strike, and not back until a restart.

Readiness does have one job in the placement cycle. Between the strike
and the next ball, the previous shot's detection is still sticky: ProTee
said `ballPos` and never said the ball left, because the player hit it.
Either readiness state after that shot ends the cycle — `READY` means
the device has found the next ball, `FINDING BALL` means it is looking
for one — and the marker returns until a fresh detection hides it again.
Both halves matter: counting only `READY` is the version that failed on
hardware.

### How long a round result stays

The result panel appears late through no fault of CaddieJ's, so its lifetime is
measured from when it appears rather than from the strike, and it has two ends:

```
GSPro result observed
  ↓
panel up, readable window starts (FLOOR_SHOT_RESULT_MIN_DURATION, default 8 s)
  ↓                                    ↓
next address cycle inside it       nothing asks for the floor
(READY, or a ball on the mat)          ↓
  ↓  remembered, not applied       cap expires (FLOOR_SHOT_RESULT_MAX_DURATION,
  ↓                                default 30 s) and the panel comes off
window ends → panel comes off
```

* **A new shot outranks both timers.** A result or an Open Connect shot replaces
  whatever is up, immediately.
* **The address marker is never held back.** It returns as soon as the room asks
  for it, with the result still above it: they are different parts of the
  surface. Only the address information waits, because it is the same size in
  the same place as the result. `floor_information_mode_changed` records each
  swap between the two.
* **Nobody waits out the cap.** A dismissal that arrives inside the readable
  window is applied the moment the window ends, not at 30 s.

The minimum exists because of a measurement: on a real session the next ball was
seen on the mat as little as 10 s after the strike, and dismissing on that alone
left about four seconds of a number the player had asked for. `MIN` must not
exceed `MAX`; a pair that cannot both hold is rejected at start-up.

Devices that report no ball detection, no readiness, or no club data are handled
by leaving out what they did not measure rather than guessing it. A launch
monitor that does not measure carry gets no carry on the floor.

Open Connect launch numbers stay in FloorState and on `/snapshot`. They are
not drawn on the projected floor. The observer round-result panel is
different: domain values stay raw, and the HUD formats the two distances a
player can use.

The panel leads with those two numbers, at the size that reads from standing
height:

```
      35 m
      SLAG

      17 m
      KVAR
```

- **Shot distance (`SLAG`)** — `currentRound.dat` `TotalDistance`, treated as metres (the
  file does not name a unit; the hitting-bay session used metres). Integer, for
  example `28 m`. Omitted entirely when GSPro gave no usable value.
- **Kvar** — `DistanceToPin` on the same shot record, which is what was left
  once **that** shot has come to rest, not what the player was playing from. Verified
  on a real round two independent ways: along hole 1 the arithmetic closes
  (158.3 m left, a 5.9 m shot, 153.1 m left; then a 175.3 m shot from 153.1 m out
  leaving 23.1 m, which is the overshoot), and ProTee's own log recorded GSPro
  Code 201 `DistanceToTarget` 10.9 then 9.0 in the two seconds after a chip,
  matching the previous shot's `DistanceToPin` 10.855 and the new shot's 9.008.
  Same metres as the shot distance. **Omitted entirely when GSPro gave no usable
  value**, rather than shown as a dash under a label: an optional metric
  the round did not supply is not a measurement that failed. A non-positive value
  is treated as unusable, because what GSPro writes for a holed shot has not been
  observed.
- **Ball speed** — `BallSpeed` is treated as metres per second (a real session
  saw ~32.8386, which is ~118 km/h). It stays on `RoundResultView` and on
  `/snapshot` as `ball_speed_display` for diagnostics. It is **not drawn** on the
  projected floor.

Player name, hole number, shot number, and surface integers also stay in state
and on the snapshot. They are not drawn.

Club speed is not shown, because no passive source has it. The ProTee main log
carries no numeric club metrics at all — a full session was searched for
`ClubSpeed`, `ClubPath`, `FaceAngle` and `AttackAngle` and contains none of them,
only `Detected a right handed Iron`. Deriving it from ball speed would be an
invention, and reading it off ProTee's own UI is not passive observation.

## Address information

While the next shot is being prepared, the same part of the surface shows what
is worth knowing before it:

```
     147 m

       7I
```

- **Distance** — GSPro's `Player.DistanceToTarget`, read out of ProTee's own
  `GSPro read data` log line (Code 201). Metres, shown as an integer. This is a
  read of a line ProTee already wrote; CaddieJ is not in the Open Connect
  socket. No extra label: a large metre figure on the hitting mat is remaining
  distance.
- **Club** — the club in your bag whose carry is nearest that distance.
  See [Your bag](configuration.md#your-bag). Shown as the configured label
  (`7I`), not as a sentence.

Each figure disappears when CaddieJ does not have it, rather than standing there
as a dash: a distance with no bag behind it is one figure, and a bay with
neither is no panel at all.

The distance is the one for the **next** shot, and it is a different fact from
the remaining distance in the result panel even though the two are usually the same
number a few seconds apart. `DistanceToTarget` refreshes when GSPro has finished
flying the previous ball — on 2026-09-16 the value 16.8 arrived 5.2 s after the
camera trigger, and `currentRound.dat` recorded `DistanceToPin` 16.7867889 for
that same shot. Until that refresh arrives CaddieJ has no current target and
shows nothing, rather than leaving up the distance the player just played from.

Which club GSPro itself has auto-selected is on the same Code 201 payload and is
deliberately ignored. Choosing a club is CaddieJ's recommendation to make, from
carries somebody measured, and mirroring GSPro's answer would be neither.

Wind, elevation, lie, temperature and rollout are omitted. Passive sources
observed on real hardware do not carry verified current-hole wind or elevation
change. See [GSPro and a launch monitor](gspro.md#wind-and-elevation).

The ProTee-detected club (`7 IRON` when the log said `7 Iron`) still travels, as
a `data-club` attribute on the panel. It is not drawn beside the recommendation:
ProTee reports the generic word its detector matched — three days of real logs
say `Iron` for every iron in the bag — and a vaguer label next to a specific one
reads as a contradiction.

Those labels are presentation only. `ball_speed_raw`, `total_distance_raw` and
`distance_to_pin_raw` stay on the Python view under the names GSPro used, and are
omitted from the overlay JSON, so a stale cached renderer cannot print the long
floats. The wire carries `shot_distance_display` and
`remaining_distance_display`; those two names say what the player is being shown
and are only spelled that way because the meaning of the number behind each was
verified on a real round. `GET /snapshot` returns the
same payload the Browser Source receives. Surface integers stay unnamed. See
[GSPro and a launch monitor](gspro.md).

## Putting mode

When the club in play is a putter the floor becomes a putting floor. It
is the same four layers, with less in them:

* the marker moves to the putter position, which may be off the mat;
* a lane appears: a thin rectangle from that position running down-range
  towards the target, with a centre line through it.
  `FLOOR_PUTTING_LANE_LENGTH_M` and `FLOOR_PUTTING_LANE_WIDTH_M` are its
  metres, and `FLOOR_PUTTING_LANE=false` turns it off entirely for a bay
  that does not want it;
* the club path from the last full swing and the launch numbers behind
  it are not drawn. A putt has no club path worth a metre of projector,
  and the previous shot's is worse than nothing.

The distances stay. A player putting still wants to know what is left,
and `KVAR` after a putt is the most useful number on the floor.

The lane goes through `FloorCalibration` like the strip does, so it is
four mapped corners on the wire and is correct on a keystoned
projection. It is anchored, so it moves with the strip. Entering and
leaving putting mode is `floor_putting_mode_changed` with
`floor_putting` true or false — one line per transition, not per poll.

## After a putt

The floor can draw the roll a putt actually made, from the ball path
GSPro writes into `currentRound.dat`. In practice it usually will not,
and that is the honest answer rather than a defect to work around.

`activeShot.sd.path` is a **rolling buffer of the tail of the
trajectory**, not the whole of it. Read against a real capture off this
bay — 196 shots, 191 of them under twelve metres — the longest shot
travelled 101 m and left thirteen samples, the first of which was
already 95 m down-range. The last sample is always exactly `EndingPOS`,
so the buffer ends at the shot but does not begin at it. For short shots
it reaches back *past* the shot into whatever happened before: the final
entry in the capture moved 0.124 m and its path spanned 0.807 m,
including a 0.17 m drop in `pos.y` — a ball being placed on a green, not
a ball being rolled.

The rest of that file is no help either. `isPutt` was `false` on all 196
shots, `TargetDirection` was `0.0` on all 196, and `BallStopPos` was
`(0, 0, 0)` on all 196. So the putt is identified by the club in play,
not by GSPro's flag.

`floor/putting.py` therefore draws a trace only when the whole roll fits
inside the buffer and nothing else is in it: at least three samples, a
roll of at least `MIN_ROLL_M` (0.15 m — below that it is a tap-in with
no shape in it), a buffer that starts within 8 cm of `StartingPOS`, less
than `MAX_RISE_M` (5 cm) of vertical movement, and samples that
monotonically approach where the ball stopped. Anything else is refused
with a reason, logged as `floor_putt_path_unavailable` with
`floor_putt_reason` and the sample count. A line on a floor is read as a
measurement whether or not it is one, so an approximation is worse than
an empty floor.

When it is drawn, it is `floor_putt_path_drawn`, and it is placed under
the putter marker rather than at GSPro's world origin, rotated so the
ball's initial roll direction runs down-range. Distances in it are
measured. The side a break falls on rests on one stated assumption:
GSPro is a Unity game, Unity's world is left-handed with +y up, so world
right is world forward turned ninety degrees clockwise seen from above.
It is a roll relative to the line the ball set off on, not an
aim-referenced ball path — nothing in the file says where the player was
aiming.

**This has not yet been confirmed against a real putt.** The capture the
thresholds were derived from contains no shot GSPro called a putt. Step
H of the [hardware checklist](hardware-validation.md) exists to collect
`floor_putt_reason` and `floor_putt_samples` from actual putts; expect
refusals, and expect the reason to say which check refused.

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
stay up. Overlay HTML, JS and CSS are served from a content-hash directory
(`/static/hud/<hud_assets_version>/…`) because OBS's embedded CEF caches ES
modules by pathname and ignores `?v=` query strings. After a CaddieJ
restart, refresh the Browser Source once so CEF loads the new path. The
browser reports `overlay_assets_loaded` with that hash; it must match
`hud_assets_version` on `/healthz`. A mismatch is `overlay_assets_stale`.
Then use OBS **Fullscreen Projector (Preview)** or **Fullscreen
Projector (Program)** on the floor projector output you already have working.
CaddieJ does not change that projector mapping.

`OBS_PROVISION_FLOOR_SOURCE=1` can create or reconcile that Browser Source if
the scene already exists. Manual setup is enough for this path.

OBS Studio needs its WebSocket v5 server enabled, and the password from
**Tools → WebSocket Server Settings** goes in `OBS_PASSWORD`.

## Shot Video replay

CaddieJ can briefly reveal an **existing** OBS Window Capture of ProTee's own
Shot Video panel (impact imagery, club/path lines, ProTee's analysis). It does
not extract video, reproduce overlays, capture the screen, or draw that panel
in the browser HUD.

This is opt-in (`FLOOR_SHOT_VIDEO_ENABLED=true`) and best effort: a missing
source or a down OBS WebSocket is logged and ignored. Golf continues.

Manual OBS layout is authoritative. CaddieJ never creates this capture, never
crops or transforms it, never reorders sources, and never switches scenes.
`CaddieJ Floor` stays the active floor overlay; Shot Video is a temporary
source in the same scene (`OBS_FLOOR_SCENE`).

Required source order in scene `CaddieJ Test` (top to bottom):

| | |
| --- | --- |
| `Shot Video` (`OBS_SHOT_VIDEO_SOURCE`) | Window Capture of ProTee Shot Video. Manually cropped and positioned. Normally hidden. |
| `CaddieJ Floor` (`OBS_FLOOR_SOURCE`) | Browser Source of the floor HUD. |

The replay starts at impact. `ShotDetected` — ProTee's own camera trigger, read
from its log — is the trigger; CaddieJ then waits
`FLOOR_SHOT_VIDEO_DELAY_SECONDS` so ProTee can render its panel, shows the
source for `FLOOR_SHOT_VIDEO_DURATION_SECONDS`, and hides it again. The delay is
a first guess until it is timed against real ProTee Shot Video.

It deliberately does **not** wait for `RoundShotObserved`. That event is GSPro's
completed shot, and `currentRound.dat` cannot be written until the ball has
stopped: measured against ProTee's trigger on a real session, it arrived 3.1 s
later for a chip and about 6.4 s later for a full shot. A swing replay scheduled
from there starts after the player has picked the next ball up. The floor's
result panel still waits for GSPro, because that is when the numbers exist.

So Shot Video needs `PROTEE_OBSERVER_ENABLED=true` and a readable ProTee log.
The chosen line is the first buffer image of a camera trigger, which carries
ProTee's own per-session trigger number and is the earliest per-shot line in the
log. On a session of 16 real shots every trigger was followed by a send to GSPro
77–385 ms later and none was followed by nothing.
`Sending shot to game` was rejected for the job despite being the more obvious
candidate: the same session wrote it **twice** for 5 of those 16 shots, once as a
ball-only send and once as a club-only one, and 150 ms later than the trigger.
`SHOT FOUND: TRIGGERED` does not appear in these logs at all.

On startup and shutdown CaddieJ hides the source so OBS cannot leave it
visible from the previous session.

This is the first feature that makes CaddieJ talk to OBS in normal operation,
so `OBS_PASSWORD` now matters: it must match **Tools → WebSocket Server
Settings**. A blank password against a server with authentication enabled
fails to connect, is logged as `shot_video_obs_unavailable`, and changes
nothing else.

To prove CaddieJ can control the source, without hitting a ball:

```shell
caddiej --check-obs-shot-video
```

It connects, resolves the scene item id, shows the source for two seconds,
hides it again, and reports which OBS request failed if one did. That
separates an OBS control problem from a shot-event problem: if this works but
a real shot produces no replay, look for `shot_video_replay_scheduled` in the
log rather than at OBS.

**The replay is never on the floor HUD's path.** The shot result is drawn as
soon as CaddieJ has it. The delay, the duration, an OBS that cannot be
reached, and an OBS that accepts a connection and then stops answering are all
on a separate task; none of them can hold up a number the player is waiting to
read.

## Top Impact

Top Impact is ProTee VX's strike image, and it is revealed the same way
and on the same trigger, from its own configuration:

| | |
| --- | --- |
| `FLOOR_TOP_IMPACT_ENABLED` | off by default |
| `OBS_TOP_IMPACT_SOURCE` | `Top Impact` |
| `FLOOR_TOP_IMPACT_DELAY_SECONDS` | 1.5 |
| `FLOOR_TOP_IMPACT_DURATION_SECONDS` | 4.0 |

The default delay is longer than Shot Video's because the impact image
is written after the swing: revealing it too early shows the previous
shot's. Both numbers are first guesses until they are timed against real
ProTee output.

The two features share a lifecycle, not an instance.
`caddiej.obs_reveal.TimedObsSource` holds what was duplicated —
resolving the scene item once, hiding it at start-up, the delay, the
duration, newest-shot-wins, ignoring a detection the observer re-read
from a rotated log, cancelling the previous reveal, and one attempt to
hide on shutdown — and `shot_video` and `top_impact` are two
configurations of it with their own OBS client, own lock, own generation
counter and own bus subscription. A bay that has one source and not the
other is normal, and one of them failing says nothing about the other:
an OBS that cannot be reached for Top Impact is
`top_impact_obs_unavailable` and Shot Video carries on.

Every log line either writes is `<feature>_<what happened>`, so the
grep for one is the grep for the other with the prefix changed:
`feature_enabled`, `source_resolved`, `source_missing`,
`obs_unavailable`, `replay_scheduled`, `replay_ignored_duplicate`,
`replay_cancelled`, `replay_failed`, `source_visible`, `source_hidden`,
`obs_timeout`, `left_showing`, `source_recovered`, `refused_capture_scene`.

The same rehearsal exists:

```shell
caddiej --check-obs-top-impact
```

### The floor item and the capture item

Swing capture reads ProTee's Impact view from the private `CaddieJ Capture`
scene, where its item stays enabled. The reveal shows Impact on the floor from
a *separate*, hidden item in `OBS_FLOOR_SCENE`. One source, two scene items:

1. In `CaddieJ Capture`: `Impact`, eye on. CaddieJ never toggles it.
2. In the floor scene: the source `OBS_TOP_IMPACT_SOURCE` names, added with
   Sources > + > Add Existing, above the CaddieJ Browser Source, eye off.
   `OBS_TOP_IMPACT_SOURCE=Impact` is fine: it is the same source as the
   capture item and a different scene item.
3. `FLOOR_TOP_IMPACT_ENABLED=true`. The delay and duration are Impact's own
   and are unaffected by swing capture.

A reveal refuses to run when `OBS_FLOOR_SCENE` is the capture scene, and
settings reject that combination outright. A floor item that cannot be found
is logged as `top_impact_source_missing` with `obs_reveal_missing_count`,
again at most every five minutes while it stays missing rather than once at
start-up, and `/healthz` shows `top_impact` (and `shot_video`) with a `state`
of `unchecked`, `resolved`, `missing`, `obs_unavailable`, `refused` or
`disabled`, how many reveals were shown, and the remediation in `detail`.

`--check-obs-top-impact` reports both roles when swing capture is on: the
floor presentation (shown, then hidden) and the coach evidence (resolved,
visibility never changed), with the Add Existing steps for whichever is
missing.

## Coaching progress and the result

While a ProTee strike is being coached the floor shows which stage it is in:
`CAPTURING SWING` when swing capture starts its burst, `ANALYZING` when the
one coaching request goes out, `COACHING` when the reply is back. It is a
stage word and a bar that sweeps without filling. There is no percentage,
because nothing knows how far through a model request is.

The stage belongs to the newest strike. A stage announced for an older strike
is never drawn; a newer strike, the coached line, a template verdict or a
failure takes it down; and if none arrives it is cleared after the capture
wait plus the coaching budget. It never moves the ball status or the result.

A coached result is four things, not one: the swing score, the still the coach
cited, what was seen above what to do, and four readings. The score is
CaddieJ's own arithmetic on the model's checkpoint scores (see
[Swing Score](swing-score.md)) and is shown whenever one was computed,
including when the model's words were refused and the template line stands
in. The advice also carries data for a later layout: each scored phase as a
label and a value, and the lesson's progress (for example `TRANSITION 78`,
`+43`, `MUCH BETTER TRANSITION`). The current layout does not draw them yet. The still is served from
`/evidence/<capture_id>/<frame_id>`: the capture id must be a canonical UUID
of a stored shot, the frame id must be one that shot's metadata names, and
anything else — including a shot already pruned — is a 404, which the overlay
shows as no picture. Cue artwork stays separate from the player's frame.

**The All Values grid is never the picture.** It still goes to the model in
every request, and it is still in the capture for debugging, but it is 5102 ×
435 pixels of small text and is not a thing to put on a floor. A reply citing
it as its evidence gets the impact still instead if there is one, and no
picture if there is not. No frame is ever chosen for the occasion: the
substitution is the lowest-numbered `impact_*` frame of this capture or
nothing at all.

### The four readings

`CARRY`, `FACE`, `PATH`, `CLUB`, in that order, in a line under the coaching.
They come from the structured metrics the coaching request already produced —
there is no second look at the grid, no re-OCR, and no dependency on GSPro.
Nothing is converted: the unit stays whatever ProTee printed and the direction
stays ProTee's own word, so the floor says `152.1 yd` and `1.7° OUT-TO-IN`
beside a screen that says the same. A reading that was not read is a missing
cell, not a dash and not a zero; three values laid out cleanly is a true
statement about what was readable.

### Where a coached result goes

The coaching used to hang off one anchor point as a centred column, with the
browser sliding it up by however much it overflowed. On the default bay that
looked fine. On a real one — a 0.55 × 0.315 m strip near the middle of the
projection — the column was anchored above the mat and grew *down* across it,
so the score sat on the turf the player was standing on.

An anchor and a lift are not a layout. `floor/coach_layout.py` computes five
regions from the strip's own projected corners, back to front:

| Region | Share of the band | What is in it |
| --- | ---: | --- |
| `score` | 26% | the number, biggest and furthest away |
| `evidence` | 36% | the still |
| `observation` | 24% | what was seen, then what to do |
| `metrics` | 14% | carry, face, path, club speed |
| `protected` | — | the hitting strip and the ball status chamfer behind it |

Nothing drawn may touch `protected`, and `CoachRegions.clears_protected()` is
asserted by a test rather than hoped for in a stylesheet: "does the score
overlap the mat" is a question with an arithmetic answer. The protected band
includes the chamfer depth, derived from the same scale the Ball Status
lettering is drawn at, so enlarging the lettering pushes the coaching back
instead of letting the two collide. Ball Status itself is untouched.

The regions are shares of the room above the strip rather than fixed heights.
A projector aimed at a mat near the back of its image has very little space
there and one aimed near the front has a lot; fixed metres would overflow the
first and strand the second. A bay that leaves less than 12% of the surface
above its mat gets no regions at all, and the overlay falls back to the
centred column — four slivers would be illegible anyway, and a mat that close
to the back edge is a calibration problem.

### How long a result stays up

Until the next ball is actually struck. There is no display timeout and no
setting for one.

There used to be, at nine seconds, and it was indefensible from the moment the
ProTee path existed: the line arrives six to twelve seconds after the strike,
the player is usually still watching the ball flight when it lands, and the
whole result was gone before it had been read once. What replaces it is the
rule coaching staleness already used — **a ball placed on the mat is not a new
shot, and a ball struck is.** So BALL READY changing underneath a result
changes nothing about the result, and the previous swing's score sits on the
floor while the player is choosing a club, which is when it is most use.

It is cleared by the next strike, the next completed shot, the next round
result, and a newer line replacing it. Newest-shot-wins is unchanged.
