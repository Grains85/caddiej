# Swing capture

[← Documentation index](README.md)

> **The phase timing is one capture's measurement.** Where each phase falls in
> each replay was read by eye off one shot of 2026-10-07, and the face-on
> downswing is an estimate from it. The model says which phase each picture
> really shows, so a drift shows up as disagreement, not as a wrong score. See
> [Which moments are aimed at](#which-moments-are-aimed-at) and
> [Tuning it](#tuning-it-after-the-first-shot).

ProTee Labs already films the swing. It owns both cameras, knows when impact
was, and draws two synchronised replays in its own window after every shot.
CaddieJ does not open those cameras, does not read ProTee's encrypted
recordings, and does not click anything in ProTee. It waits for the replay to be
on screen and asks OBS for pictures of it.

Each shot gets its own directory of `SWING_CAPTURE_SELECTED_FRAMES` stills in
total, not per camera. Each still keeps the OBS source and the view it came
from. `latest_swing` is a copy of the newest shot, there for you to open.

```
media/swing_shots/<time>-<trigger>-<id>/   the evidence Coach reads; last 3 kept
  metadata.json
  <one directory per configured panel>
  metrics/all_values.jpg
media/latest_swing/                        a copy of the newest shot, for viewing
```

`latest_swing` is replaced whole when nothing has a file in it open. On
Windows, an image viewer or Explorer preview that holds one of its JPEGs makes
the rename fail with `WinError 5`. The shot is not lost when that happens:
Coach has already been handed the shot's own directory. The old `latest_swing`
gets a `STALE.json`, and its `metadata.json` gains `"stale": true` and
`superseded_by`, which names the shot that could not replace it. The log says
`swing_capture_latest_publish_failed`, which is not `swing_capture_failed`.
The next shot tries again once the file is closed.

`SWING_CAPTURE_PANELS` decides which directories exist. A panel left out of
that list, including `delivery`, is not captured.

It is off by default and needs OBS with its WebSocket server enabled, which is
the same connection the [floor](floor.md) already uses.

## Setting up the sources

CaddieJ photographs sources that already exist. It does not create, crop or
move anything in OBS. On this bay the coach panels are `Swing Behind`
(down-the-line), `Swing Side` (face-on) and `Impact`. `Flight Path` is the
floor replay and is not a coach panel.

OBS only refreshes a window capture that it is showing. If the source is only a
hidden item in the program scene, every screenshot is the same cached picture.
Showing it in the program scene puts it on the floor projector. So the coach
sources are kept showing by a private scene, set up once in OBS:

1. Create a scene named `CaddieJ Capture` (or set `SWING_CAPTURE_SCENE`).
   Add `Swing Behind`, `Swing Side`, `Impact` and `All Values` to it with
   Add > Existing, eye on. Layout does not matter.
2. In the floor scene (`OBS_FLOOR_SCENE`), add `CaddieJ Capture` as a scene
   source, eye on.
3. On that nested scene: Filters > Effect Filters > Color Correction,
   Opacity 0.

OBS counts a source as showing when an enabled scene item leads to it from
something it renders. Opacity does not change that, so the sources refresh on
every tick and nothing is drawn on the floor. Never switch `CaddieJ Capture` to
Program, and never point `OBS_FLOOR_SCENE` at it: settings refuse that while a
floor reveal is on. The hidden floor-scene items for `Swing Behind`,
`Swing Side` and `All Values` can stay hidden or be deleted. **Keep the floor
item the Impact reveal uses** — see below.

### Impact has two scene items

Impact is both coach evidence and something the floor shows on purpose, so
the same OBS source can have two scene items with two jobs:

| Scene | Item | Visibility | Who touches it |
| --- | --- | --- | --- |
| `CaddieJ Capture` | `Impact` | eye **on**, always | swing capture reads it; never toggled |
| floor (`OBS_FLOOR_SCENE`) | `OBS_TOP_IMPACT_SOURCE` | eye **off** at rest | the Top Impact reveal shows it after each strike |

Deleting the floor item silences the reveal: capture keeps working, the coach
still sees Impact, and the projector never shows it. That is the most likely
reason Impact stopped appearing on the floor. To restore it:

1. Select the floor scene. Sources > + > Add Existing > `Impact` (or
   whichever source `OBS_TOP_IMPACT_SOURCE` names, such as `Top Impact`).
2. Move it above the CaddieJ Browser Source and position it where it should
   appear. Leave its eye off.
3. Leave the `Impact` item in `CaddieJ Capture` exactly as it is, eye on.
4. `FLOOR_TOP_IMPACT_ENABLED=true`, then run `caddiej --check-obs-top-impact`.

Swing capture never shows or hides anything, so it cannot expose Impact or
any other coach panel on the floor; only the reveal, from its own delay and
duration, ever does. The check reports both roles — the floor item shown and
hidden, the capture item resolved without changing its visibility — and
`/healthz` reports `top_impact.state` as `missing` with the steps above while
the floor item is absent.

CaddieJ never shows or hides a source and never switches a scene. Per shot it
logs `obs_capture_source_state` (`rendered=true`, `floor_item=hidden` or
`absent`) for each source, and `obs_capture_floor_unchanged` after the burst. A
source that is not refreshed logs `obs_capture_source_not_rendering` with this
setup in its detail.

Then prove it, without hitting a ball, from the directory that contains `.env`:

```shell
caddiej --check-swing-capture
```

The report starts with the `.env` path it actually read and the effective
panel list. It takes one screenshot from each configured source through the
same OBS screenshot call production uses, and writes one JPEG per
configured panel under `.caddiej/diagnostics/swing-capture/<timestamp>/`.
A panel that is not in `SWING_CAPTURE_PANELS` is not photographed. A missing
source names the environment variable responsible. Open the files. Each one
should be the panel it is named for.

A passing check says the sources work. It says nothing about the timing.

## Switching it on

```
SWING_CAPTURE_ENABLED=true
SWING_CAPTURE_PANELS=swing_behind,swing_side,impact
SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE=Swing Behind
SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE=Swing Side
SWING_CAPTURE_PANEL_IMPACT_SOURCE=Impact
SWING_CAPTURE_SELECTED_FRAMES=16
```

`delivery` is captured only when it is named in `SWING_CAPTURE_PANELS` and
`SWING_CAPTURE_PANEL_DELIVERY_SOURCE` is set. This bay does not. The floor's
shot replay is `OBS_SHOT_VIDEO_SOURCE` and is not a coach panel.

`SWING_CAPTURE_PANEL_SWING_SOURCE` remains a deprecated alias for the behind
view. It is used only when the behind source is empty. When both are set, the
explicit behind source wins and the alias is not captured again. The same OBS
source is never captured twice. `SWING_CAPTURE_CAMERA_1_SOURCE` and
`SWING_CAPTURE_CAMERA_2_SOURCE` are not read.

Every setting, including the timings, is in the
[configuration reference](configuration.md#swing-capture). `caddiej
--check-config` prints what the capture will do in one line, and swing capture
needs `PROTEE_OBSERVER_ENABLED=true`: the strike it reacts to is the one the
passive ProTee log observer finds.

## How it works

A detected strike starts a task and the event handler returns immediately.
Nothing in the shot path — the floor, speech, the ProTee observer behind it —
ever waits for a capture, which takes several seconds by design.

Each panel has its own delay, duration and rate. A dynamic panel (the swing
replay) bursts after `SWING_CAPTURE_DYNAMIC_DELAY_SECONDS` at
`SWING_CAPTURE_FPS` for `SWING_CAPTURE_DYNAMIC_DURATION_SECONDS`. A static
panel (impact, and delivery when it is configured) is sampled later and more slowly, then reduced to
the still that settled. The generation is checked every tick, so a newer strike
abandons every panel at once.

### Cadence

Inside `SWING_CAPTURE_DOWNSWING_DELAY_SECONDS` for
`SWING_CAPTURE_DOWNSWING_DURATION_SECONDS` (default 3.3-4.4 s from the strike)
the body replays are sampled at `SWING_CAPTURE_DOWNSWING_FPS` (default 20)
instead of `SWING_CAPTURE_FPS` (default 10). The loading screen, the address
and the backswing do not need more than ten a second; P5 to P8 take about
0.2 s and do. The window is fixed rather than found: on six real captures the
replay started 1.83-2.06 s after the strike, the top came about 1.5 s later
and the release 2.15 s later at most, so 3.3-4.4 s holds every one of them
with room for a slower swing. It needs no knowledge of the replay start while
the burst runs, so the capture is still one pass.

Every still is due at a fixed time from the burst's start. The burst used to
wait one period after the still it had just taken, and on Windows every sleep
ends on the next 15.6 ms timer tick, so 10 a second asked for was 9.1 a second
delivered on every capture measured. Counted from the start, each still is at
most one tick late and the average is the rate asked for: expect stills about
47 or 62 ms apart, averaging 50, in the downswing stretch. If OBS answers more
slowly than that, the deadlines it missed are skipped, not queued, and the
burst still ends on time. Both body panels are still asked in the same tick,
as before.

A panel is never asked again sooner than 70% of its period after its last
request: 35 ms in the downswing stretch, 70 ms outside it. On one real swing a
deadline woke a tick late, the screenshot took 18 ms, and the next deadline
was then close enough to be taken at once, 20 ms after the last: inside one
replay frame, so OBS returned the impact picture twice and the impact could
not be kept. A request held back for spacing waits for it and no longer; the
deadlines after it stay on the schedule.

One `swing_capture_cadence` line per body panel per shot reports the requested
and achieved rates, the interval spread, missed deadlines, overruns, spacing
holds and how long OBS took to answer; `metadata.json` keeps the same under
each camera's `cadence`.

The extra stills are candidates only. The number of stills kept, the evidence
sheet and the request to the model are unchanged; the aimed moments simply
have a nearer still to choose from.

A dynamic burst is still one screenshot per frame of the window. The kept set
is smaller. `SWING_CAPTURE_SELECTED_FRAMES` is the total stills for the shot.
Each static panel is offered one still when that leaves every live body panel
at least one; the rest is split across the body panels. Blank frames are
dropped and not sent. Each body camera is then aimed at named moments on its
own replay clock (below). A camera that missed screenshots keeps fewer frames,
and its unused share is given to a panel that still has pictures.

Nothing decodes an image. The only question asked of a still is whether its
bytes changed, which finds "the part of the window where something was moving",
not impact and not the swing. The one other thing a still's bytes say is how
big it is, and that finds where ProTee's loading screen gives way to the
replay: the largest step up in encoded size (at least 1.2x) after which every
still stays within 0.9 of the replay's own median size. ProTee fades its
replay in under the spinner, so the still before the cut is a crossfade, and
the first full replay still is the one after the largest step, not the first
still that grew.

Both body replays begin on the same capture tick, so a body camera whose own
start was not found is timed from the other body camera's, and
`swing_capture_replay_start_borrowed` says so; `metadata.json` names the
lender in `replay_interval.start_borrowed_from`. The impact panel never lends
or borrows a start. When neither body camera found its start, neither keeps
any still -- `swing_capture_replay_start_not_found` -- rather than an even
spread across a window that begins on the spinner. The impact still and the
All Values still are kept as usual. Any other dynamic panel, whose contents
CaddieJ knows nothing about, is still spread across its window, with
`"detected": false` in `metadata.json`.

With the default timing, the body panels are sampled from 0.5 to 5.0 s and
impact from 3.5 to 5.0 s, so the dynamic and static passes end together and a
burst lasts 5 s. `swing_capture_started` lists each panel's window.

### Which moments are aimed at

The two replays do not start together: on the measured shots the
down-the-line one began about 0.1 s after the face-on one. Each camera is
therefore aimed on its own clock, in seconds after its own replay replaced
ProTee's loading screen. ProTee cuts the
replay a fixed time before the strike, so that start is an anchor however long
the window runs. A body camera with no start of its own borrows the other's;
with no start on either, there is no anchor and no body still is kept.

| Moment | Down the line (s) | Face-on (s) |
| --- | ---: | ---: |
| setup (P1) | 0.00 | 0.00 |
| takeaway (P2) | 0.60 | — |
| backswing (P3) | 0.88 | 0.88 |
| top (P4) | 1.64 | 1.53 |
| transition (P5) | 1.90 | 1.90 |
| delivery (P6) | 1.95 | 1.95 |
| impact (P7) | 2.00 | 2.00 |
| release (P8) | 2.08 | 2.08 |

Measured off the full-resolution originals of the 2026-10-07 shots, and
checked against the ProTee player's progress knob, which moves at a constant
rate and confirms each detected replay start to within 0.02 s. Measured from
its own start, each camera shows the same moment of the downswing at the same
time, so from the change of direction on both views share the same times. The
face-on top is the still with the hands highest, one still before the
down-the-line one. The face-on backswing was first aimed at 0.99 s; three
full-resolution diagnostic swings of 2026-10-08 showed lead-arm-parallel on
the same still in both views, so it now shares the down-the-line 0.88 s. The
other moments held on those swings and were left alone.

An earlier face-on table had the face-on replay 0.22 s ahead from the top on.
It was never measured past the top, and it was wrong: its impact still showed
the start of the downswing with the hands high, and its release still showed
the hands at shoulder height.

At 10 a second (0.109 s apart in practice) P5 to P8 held two stills, so the
downswing aims were compromises: the transition took the change of direction
at 1.75 s, the delivery a still between P5 and P6, and the impact a still
before the strike. With the downswing stretch (above) at 20 a second, three
full-resolution swings of 2026-10-08 showed each position on its own still,
the same still in both views:

| Position | Shot 1 (s) | Shot 2 (s) | Shot 3 (s) |
| --- | ---: | ---: | ---: |
| lead arm parallel (P5) | 1.893 | 1.891 | 1.909 |
| shaft parallel (P6) | 1.939 | 1.953 | 1.958 |
| impact (P7) | 2.002 | 2.001 | 2.006 |
| shaft parallel after impact (P8) | 2.049 | 2.097 | 2.100 |

So P5 to P7 are aimed at 1.90, 1.95 and 2.00 s, and the release stays at
2.08 s, which took the P8 still on two of the three swings and the one just
after it on the third. The top is a slow drift of the hands from about 1.2 to
1.75 s; the change of direction follows it.
The face-on takeaway is not aimed at: it adds only the hip movement, and the
down-the-line takeaway shows the rest. The finish is not aimed at in either
view; a picture the model places there is still scored.

Every label is CaddieJ's aim, not a fact. The model says which phase each
picture actually shows, and a checkpoint cited from a picture of the wrong
moment is refused. A moment past the end of the window is dropped rather than
replaced by an earlier still. A calibrated camera takes at most one still per
aimed moment, so the default 16 is the impact panel, eight down the line and
seven face-on; a smaller budget drops moments by priority (transition, impact,
top, setup, delivery, release, backswing, takeaway).

A body panel's share is capped by how many *different* pictures it returned.
A frozen source gives one still, not four copies of the same one, and logs
`swing_capture_source_frozen`. A still is never chosen twice. If the nearest
still for a moment repeats one already kept, a different still close to that
moment is taken instead.

The capture is written under a hidden staging directory. It is then moved into
`swing_shots` under a fresh name, so nothing can be holding it open. Only that
step can fail the capture. Afterwards, `latest_swing` is swapped for a copy.
Leftover staging, mirror and discard directories are removed at startup and
before each copy.

## When two shots overlap

The newest shot wins, completely. A second ball struck mid-capture cancels the
first capture where it stands, and the check that decides who owns the disk runs
again after the frames are written — so a capture that was still encoding JPEGs
when the next ball was struck throws them away rather than overwriting the
newer swing.

OBS is never a reason golf stops. A missing source, a WebSocket that is down, a
request that hangs: each is logged and abandoned, and the shot is processed the
same way it would be without swing capture at all. One camera failing does not
discard the other one's stills — half a swing is still worth looking at.

## What is in metadata.json

When the capture ran, what it was told to do, and what came back: `shot_id`
once the matching shot arrives, the trigger id, capture timestamps, and for
each panel the OBS source, the view (`behind`, `side`, or `none`), frames
captured, blank frames rejected, duplicates, distinct pictures, and frames
kept. `timeline` has one entry per screenshot: its offset, a short SHA-256,
its size in bytes and pixels, whether it changed from the one before, and
whether it was kept. Each kept frame names its `captured_index` and hash, so
you can match it to its place in the burst. No image bytes are stored there.
`settled` only means something for a static panel. After coaching
sends pictures, `provider_images` lists the image that was actually sent (the
evidence sheet, or the All Values still alone), `provider_frame_ids` names
every original it carried, and `evidence_sheet.cells` is the table from each
printed cell ID to its original frame.

## The Swing Evidence Sheet

The model is sent **one image per shot**: every selected still, the impact
graphic and the All Values grid, drawn side by side onto one canvas and stored
beside the originals as `swing_evidence.jpg`. Seventeen separate images ran
two real requests out of the 30 s timeout; one image is about 4,100 input
tokens instead of an estimated 31,700.

**The originals stay the evidence.** The sheet is a derived transport file.
Nothing on disk is downscaled, replaced or merged, and every checkpoint, fault
and root cause keeps the id of the original frame it was read from
(`swing_behind_08`), never a cell ID. A screen that wants "the picture behind
this fault" resolves that id with `frame_path`, or a printed ID with
`original_for_cell`, and opens the full-resolution original.

| Cell | Original | Kind |
| --- | --- | --- |
| `DTL_P1` … `DTL_P8` | `swing_behind_NN`, aimed at setup … release | body, down the line (blue) |
| `FO_P1`, `FO_P3` … `FO_P8` | `swing_side_NN`, aimed at setup, backswing … release | body, face on (green) |
| `IMPACT` | `impact_01` | ProTee graphic (grey) |
| `ALL_VALUES` | `all_values` | numbers only (brown) |

A cell's P-number is the moment it was **aimed** at, printed as `~transition
@3.8s` on that camera's own clock. A still with no aim keeps its file position
(`DTL_03`). The model still says what each cell shows, the reply is translated
from cell IDs to frame ids before anything reads it, and every existing check
then runs unchanged: a cell shown at another moment is `phase_conflict`, a
cell from the wrong camera is `view` or `view_mismatch`, the impact graphic is
`view_unknown`, and `ALL_VALUES` is `metrics_frame` and can never be the
floor's picture.

**Size is the model's limit, not a choice.** gpt-5-mini fits every image
inside 2048 × 2048 before reading it (measured), so a bigger canvas would only
be shrunk by the API. The layout searches every arrangement for the one that
keeps the smallest cell largest. On the bay that is 2048 × 1679 px: down the
line 3 × 3 at 291 × 429 px per still (sources 1002 × 1480), face on 3 × 3 at
370 × 433 (sources 1272 × 1490), impact 370 × 163, and All Values a full-width
strip 2032 × 173 (source 5102 × 435). JPEG quality 90 with 4:4:4 colour,
about 950 KB, drawn in about 0.15 s. Nothing is stretched, enlarged or
cropped.

**What that costs.** Each swing still reaches the model at about 0.29 of its
captured size, where separately it was read at full size (every still already
fits inside 2048). Fine detail — a shaft against the screen, a lead wrist
angle — is softer than it was. That is the price of one image; the phase and
view checks above are what keep it from becoming a confident wrong score.

If the sheet cannot be drawn (no Pillow, unreadable files), the All Values
still goes alone: the numbers are read and every checkpoint is null. It never
falls back to seventeen images. The sheet is pruned with its shot, and
`latest_swing` gets a copy only while it still holds that shot.

Benchmark a stored shot without hitting a ball — one real request, never part
of the test suite:

```shell
caddiej --check-coach --benchmark-evidence media/swing_shots/<shot>
```

No shot measurements. Those belong to the passive observers and to
[shot history](history.md), and a second, staler copy of them here would be a
second place to be wrong.

## Tuning it after the first shot

`SwingFramesReady` is what coaching reads. Each still is listed to the model
by its cell ID with its label, `SWING_BEHIND / DOWN_THE_LINE`,
`SWING_SIDE / FACE_ON`, `IMPACT`, or `DELIVERY`, and open
`swing_evidence.jpg` to see exactly what it was shown. The timing of the burst
is still a first guess.

The first check is by eye. Hit one shot, open `media/latest_swing`, and look
at whether the frames cover the swing. If it has a `STALE.json`, look in the
newest `media/swing_shots/` directory instead. Before tuning any timing, check
`timeline` in `metadata.json`. The first and last `changed: true` entries show
where the replay actually moved.

| What you see | What to change |
| --- | --- |
| First frames are the static panel, replay starts late in the set | Raise `SWING_CAPTURE_INITIAL_DELAY_SECONDS` |
| Replay is already running in frame 1 | Lower it |
| Last frames are a held final frame | Lower `SWING_CAPTURE_DURATION_SECONDS` |
| The set ends mid-swing | Raise it |
| Frames jump — top of backswing missing entirely | Raise `SWING_CAPTURE_FPS`, then `SWING_CAPTURE_SELECTED_FRAMES` |

The log lines say the rest. `swing_capture_triggered`, `swing_capture_started`,
`swing_replay_interval_detected` (or `..._not_detected`),
`swing_frames_selected` and `swing_capture_completed` carry the offsets and
counts the tuning depends on; per-frame detail is at DEBUG. `/healthz` reports
captures started, completed, failed and cancelled, frames captured and kept, and
the last error, which is the quickest way to tell a wrong source name from a
replay that had not started.

### Keeping the whole timeline

`timeline` lists every screenshot, but only the selected ones are kept as
files. To read P1–P8 off every still, set
`SWING_CAPTURE_DIAGNOSTIC_TIMELINE_SHOTS` to the number of shots (at most 40,
which is also the most shot directories that are kept). The production
default keeps 3 directories. Setting the timeline count to 20 retains 20
shot directories, because the diagnostic files live inside them. One full
diagnostic shot is about 13 MB, so 20 of them are about 261 MB. With the
timeline off, 20 kept shots are the selected frames only, about 57 MB.
For that many published shots of this run, every Swing Behind and Swing Side
screenshot the burst already took is also written to
`media/swing_shots/<shot>/diagnostic/swing_behind/` and
`.../diagnostic/swing_side/`, as `frame_NNN.jpg` by capture index. The timing
goes into `metadata.json` under `diagnostic_timeline`:

- the request time of each still, and its offset from the burst start, the
  strike and that camera's detected replay start;
- its size, digest and whether it changed;
- whether it was selected, as which file and for which phase.

Nothing extra is asked of OBS, and nothing under `diagnostic/` reaches the
sheet or the model. A strike superseded by a newer one does not use up a
shot, and the shots are pruned like any other. The log says
`swing_capture_diagnostic_timeline_enabled` at start-up and
`swing_capture_diagnostic_timeline_retained` for each shot, with
`swing_capture_diagnostic_complete` true when every captured still is on disk.

`COACHING_DIAGNOSTIC_DRY_RUN=true` keeps such a session free. The shot is
captured, selected and drawn on the sheet as usual. The coach then logs
`coaching_request_skipped` with `diagnostic_dry_run` instead of calling the
provider, and the floor sees what it sees for any shot without a coach line.
