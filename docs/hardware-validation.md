# First hardware validation

[← Documentation index](README.md)

> **File formats and the passive HUD → OBS → floor projector path have been
> verified on real hardware.** These presentation refinements (km/h, metres,
> result lifetime, driver tee) should be re-checked in the bay after deploy.
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
5. Place a ball. The HUD should show the existing tee marker, the lit hitting
   strip, and — once GSPro has supplied a target — **TO PIN**. READY and the
   detected club are in `/snapshot` and the log, deliberately not on the floor.
6. Hit a shot **in a round**, not on the Practice Range.
7. HUD should show **BALL SPEED** as integer km/h (for example `118 km/h`),
   **DISTANCE** as integer metres, **HOLE** / **SHOT** / **SURFACE** as raw
   integers, and a **BALL FLIGHT** trace. The result stays until the next
   ball / READY, or 15 seconds.
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

If you also capture ProTee's Shot Video panel, add a Window Capture named
`Shot Video` in the **same** scene, **above** `CaddieJ Floor` in the source
stack. Crop and position it by hand. Leave it hidden. CaddieJ only toggles
visibility (`FLOOR_SHOT_VIDEO_ENABLED`); it does not create, crop, or reorder
that source. See [The floor overlay](floor.md#protee-shot-video-overlay).

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
| ProTee Shot Video overlay | `FLOOR_SHOT_VIDEO_ENABLED=false` |

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

### Stage E2 — ProTee Shot Video overlay (opt-in)

Only after the floor overlay path above is trusted. In OBS, scene
`CaddieJ Test`, source order **top to bottom**:

1. `Shot Video` — Window Capture of ProTee's Shot Video panel, cropped
   and positioned by hand, normally hidden, **above** the floor overlay.
2. `CaddieJ Floor` — the Browser Source already in use.

CaddieJ does not create this capture. Set in `.env`:

```
FLOOR_SHOT_VIDEO_ENABLED=true
OBS_SHOT_VIDEO_SOURCE=Shot Video
FLOOR_SHOT_VIDEO_DELAY_SECONDS=0.75
FLOOR_SHOT_VIDEO_DURATION_SECONDS=4
```

`OBS_PASSWORD` must match **Tools → WebSocket Server Settings**. Until this
feature, CaddieJ never opened that connection in observer mode, so a blank
password was harmless; now it is the difference between a replay and a warning
in the log.

Prove the OBS half first, with no ball involved:

```powershell
caddiej --check-obs-shot-video
```

Expect `resolved scene item id …`, then two seconds of `Shot Video` visible in
OBS, then hidden. A failure names the OBS request that failed. Do this before
blaming the shot path.

Start OBS, open ProTee with Shot Video available, then start CaddieJ.
Confirm `Shot Video` becomes hidden at startup. Hit one real shot in a
round. The source should stay hidden briefly, appear for about the
configured duration, then hide. The delay is a first guess until it is
timed against real ProTee Shot Video. A second shot must show the
**new** panel, not the previous one. Ctrl+C should exit cleanly and
leave `Shot Video` hidden.

The replay is triggered by ProTee's camera trigger — impact — so it also needs
`PROTEE_OBSERVER_ENABLED=true` and a readable ProTee log. The thing to judge on
the bay is *when* the panel appears: it should be roughly
`FLOOR_SHOT_VIDEO_DELAY_SECONDS` after you hear the strike, not after GSPro has
finished flying the ball. Timed from the logs, GSPro's result is 3–6 s behind the
strike, so the difference should be obvious to watch.

The chain in the log, for one shot, is:

```
shot_video_feature_enabled   → the scene and source CaddieJ will use
shot_video_source_resolved   → obs_scene_item_id, so OBS agreed the name exists
shot_video_replay_scheduled  → shot_trigger_id, ProTee's own trigger number
shot_video_source_visible    → shot_trigger_id, after the delay
shot_video_source_hidden     → shot_trigger_id, after the duration
```

`shot_trigger_id` should increment by one per shot. If nothing is scheduled at
all, the detection never arrived: check `protee_observer` on `/healthz` and that
the log contains `Received trigger <n> buffer image 1 of the <m>`.

`shot_video_source_missing` means the name does not match anything in the
scene; `shot_video_obs_unavailable` means the WebSocket did not open. Either
way CaddieJ keeps running and the floor is unaffected.

### Stage E3 — lit hitting strip, shot distance, and result lifetime

Four things to look at on the turf, in one pass. Measure the physical mat, set
the four metre dials to the **centre**, width and depth of it, and turn the fill
on:

```
FLOOR_HITTING_STRIP_X_M=0.48
FLOOR_HITTING_STRIP_Y_M=0.85
FLOOR_HITTING_STRIP_WIDTH_M=0.30
FLOOR_HITTING_STRIP_DEPTH_M=0.45
FLOOR_HITTING_STRIP_FILL=true
FLOOR_SHOT_RESULT_MIN_DURATION=8
FLOOR_SHOT_RESULT_MAX_DURATION=30
```

Restart CaddieJ, then in OBS right-click the Browser Source →
**Refresh cache of current page**, because the overlay files are served from a
content-hashed path and this changes them.

1. **The pad is lit.** With the room dark and no ball on the mat, the whole
   calibrated rectangle should be bright enough to work in, with the ball marker
   dark on top of it and still obvious. Adjust X/Y until the light sits on the
   turf; each is a centre coordinate, so a 0.05 change moves the pad 5 cm and
   does not resize it. Width and depth do not move it.
2. **Shot Video appears near impact.** Hit one shot and watch when the panel
   comes up — see Stage E2.
3. **Shot distance appears as soon as the data exists.** The panel leads with
   `SHOT DISTANCE`; it cannot appear before GSPro has finished the ball, so
   expect it a few seconds after impact rather than instantly.
4. **`TO PIN` is there when GSPro supplied it.** On a round shot it should be the
   distance left *after* the shot. If the figure is absent, GSPro gave no usable
   `DistanceToPin` — that is correct behaviour, not a missing value, and the
   panel shows one figure rather than a dash. Confirm against the log:

   ```
   floor_round_result_presented
     round_distance_to_pin_raw          ← what currentRound.dat had
     round_remaining_distance_display   ← what the overlay was given
   ```

5. **The result stays long enough to read.** Hit a shot, then tee the next ball
   up straight away. The result must not vanish the moment the ball lands on the
   mat: it stays for `FLOOR_SHOT_RESULT_MIN_DURATION`, and the ball marker
   returns underneath it in the meantime. Leave the mat alone after another shot
   and it should stay up until `FLOOR_SHOT_RESULT_MAX_DURATION`.

Ball speed is kept on `/snapshot` as `ball_speed_display` and is not drawn on
the floor. Club speed is absent because no passive source reports it.

### Stage E4 — the address HUD, and a strip that stays lit

Everything in this stage was written in response to what the bay actually did in
the 2026-09-16 session: the pad went dark at READY, and FINDING BALL sat on top
of the shot numbers. Both were code deciding two things with one branch.

Optionally fill in your bag first, in metres of **carry**. Any club left at `0`
is not in the bag, and a bag with nothing in it means no club is recommended —
which is a valid way to run this stage, it just skips steps 8 and 16.

```
PLAYER_CLUB_CARRY_DRIVER_M=220
PLAYER_CLUB_CARRY_7I_M=135
PLAYER_CLUB_CARRY_PW_M=98
```

Then:

1. Start ProTee, GSPro, OBS, and CaddieJ, with `FLOOR_HITTING_STRIP_FILL=true`.
2. In OBS, right-click Browser Source `CaddieJ Floor` → **Refresh cache of
   current page**. The overlay markup, renderer and stylesheet all changed, so
   the content-hashed asset directory changed with them. Confirm
   `hud_assets_version` on `/healthz` matches `overlay_assets_loaded` in the log
   — if `overlay_assets_stale` appears instead, the refresh did not take.
3. The hitting strip is lit, before any ball or club has been seen.
4. Wait for ProTee to reach READY.
5. **The strip is still lit.** This is the regression to watch: it used to go
   out here.
6. The rear edge of the strip steps out into **BALL READY**, with a green lamp.
   The snapshot still carries `"play_status": "READY"`. The words READY and
   FINDING BALL, on their own, are not on the floor. Before the device has
   said READY, the same chamfer says **PLACE BALL** and the lamp stays dim.
7. A large remaining-distance figure shows the current distance, if GSPro has
   sent one, with the recommended club under it if you configured a bag. Before
   the first shot of a hole this arrives on the GSPro handshake; after that it
   arrives a few seconds after each shot. Check `address_target_updated` in the
   log for the number and its source. Nothing configured means no club and no
   dash.
8. Put a ball down, and let ProTee go through FINDING BALL.
9. The words **FINDING BALL** are nowhere on the floor. While ProTee is looking,
   the chamfer says **PLACE BALL**.
10. The strip is still lit. The ball marker has stood down. The strip gives
    **one** short pulse and a brief check mark, then goes back to a steady
    light. A second look at the same ball must not pulse again.
11. Hit the shot.
12. Shot Video still appears about `FLOOR_SHOT_VIDEO_DELAY_SECONDS` after the
    strike — measured at 0.76 s on 2026-09-16, and nothing in this stage touched
    that path.
13. Wait for the GSPro ball to stop.
14. **SLAG** and **KVAR** appear as large metre figures, with no km/h, no
    address text behind or over them, and no READY/FINDING BALL written across
    them. The chamfer stays on the rear edge of the strip and does not cover
    those figures. The address panel is taken down for the whole guaranteed
    readable window; `floor_information_mode_changed` records the swap in each
    direction.
15. The strip is lit throughout all of it.
16. After the readable window, the address information comes back with the
    **new** distance — the one GSPro sent after this shot, not the one you just
    played from. If GSPro sent nothing, the panel stays away rather than showing
    the old number.

If remaining distance never appears at all, the passive source is the thing
to check. It is GSPro Code 201 inside ProTee's own log, so:

```powershell
Select-String -Path "$env:APPDATA\ProTeeUnited\Logs\LogFile-*.txt" `
  -Pattern 'DistanceToTarget' | Select-Object -Last 5
```

A real line ends with a `|` after the JSON — that field terminator is why this
parser matched nothing at all before 2026-09-16.

### Stage E5 — coaching on the floor, and a voice in the room

Only after Stage E4. This is the first stage that costs money and the first that
makes a noise, so it is also the first that should be turned off again
afterwards if anything in it surprises you.

Two things are switched on together because separately neither is the feature:

```
COACHING_ENABLED=true
COACHING_PROVIDER=openai
COACHING_CALL_LIMIT=40

SPEECH_ENABLED=true
SPEECH_ENGINE=kokoro
SPEECH_VOICE=bm_george
```

`COACHING_MODEL` is left at its default, `gpt-5-mini`, which is a deliberate
choice for this stage: the model's whole job here is wording, and a larger one
would cost more for the same two short strings. `COACHING_API_KEY` goes in
`.env` and nowhere else — never on a command line, never in a log, never pasted
into a chat window. Leave `COACHING_SEND_FRAMES` at `false` for this stage:
measurements first, pictures in E6.

**What to expect from this stage on passive hardware, honestly.** The ProTee log
carries no ball or club measurements at all, and `currentRound.dat` carries a
ball speed. So in observe mode the only thing coaching can measure is how
repeatable your ball speed is with a club, and it needs about six shots with
that club before it can say anything at all. Expect `coaching_kind: none` for
the first few, then lines about consistency — and nothing about face, path,
strike or attack angle, because nothing on this path measures them. E6 is where
coaching about the *swing* becomes possible, because a camera can see what the
log does not report.

Prove the voice first, before starting the rest of CaddieJ. This uses the same
Kokoro path a coached line will use, and does not start the HUD, observers or
OpenAI:

```powershell
caddiej --check-speech
```

Expect `engine ready         kokoro voice bm_george`, then `playing`, then
`played`. You should hear "Caddie J is ready." A failure names the step —
`engine open` is usually the `[tts]` extra or a first-run model download,
`output open` is PortAudio or a missing speaker, `synthesis` or `playback` is
the engine or the sound card failing after they loaded.

Then confirm the full process loaded before hitting anything:

```powershell
caddiej --check-config
Invoke-RestMethod http://127.0.0.1:8730/healthz | ConvertTo-Json -Depth 8
```

Expect `coaching.available` to be `true` and `speech.available` to be `true`.
`speech.available` false with `coaching` fine means the voice did not load —
usually the `[tts]` extra, PortAudio, or a first-run model download that hit
`SPEECH_OPEN_TIMEOUT`. `speech.last_error` says which.

Then, for **one** shot:

1. Address the ball. Distance and recommended club, as in E4.
2. Hit it. Nothing about coaching may change when Shot Video appears or when
   GSPro finishes the ball — if the result is late, coaching is not the reason.
3. `SLAG` / `KVAR` appear and are readable for the guaranteed window.
   Coaching waits during that window. Teeing the next ball, or ProTee
   reporting ready, takes the result down when the window ends. It does
   not discard the coaching line.
4. **After** that window, one large coaching phrase — two to four words.
   On the first shots of an observe-mode session this phrase is absent on
   purpose: the round file has a ball speed and no club data, and the log
   says which measurements were missing.
5. When a model line was validated, a British male voice says one short
   sentence. The floor phrase is the headline; the sentence is not written
   on the floor. A templated line is not spoken, and the log says so.
6. The phrase stays for its own lifetime, including while the next address
   is up. It leaves when that lifetime ends, or when the next shot is
   completed. `BALL READY` / `PLACE BALL` do not clear it.

Then a second and third shot, which is the part that cannot be tested any other
way, because it is about what CaddieJ remembers:

7. **Repeat the same fault.** The focus should stay where it was rather than
   moving to the next-worst thing. `coaching_focus_shots` climbs.
8. **Improve it.** The line should turn into a reinforcement naming what
   worked. `coaching_improving` is `true`, and `coaching_kind` is
   `reinforcement`.
9. **Hit again quickly, before the previous sentence has been spoken.** The old
   line must be dropped rather than said late. A sentence already audible is
   allowed to finish; one still waiting is not.

The chain in the log, for one shot, all carrying the same `shot_id`:

```
shot_detected                  → ProTee's camera trigger (shot_trigger_id)
gspro_round_shot_observed      → the round file had a new entry
shot_completed                 → shot_id and shot_trigger_id, the join
coaching_shot_diagnosed        → coaching_focus, coaching_kind, coaching_severity
coaching_session_updated
swing_capture_triggered        → or swing_capture_skipped, with swing_capture_detail
coaching_evidence_ready        → evidence_grades and coaching_detail
coaching_request_started       → or coaching_request_skipped
coaching_advice_ready          → or coaching_advice_suppressed
play_advice_waiting_for_result → held while SLAG/KVAR are guaranteed
play_advice_shown              → or play_advice_suppressed
speech_queued                  → or speech_suppressed
speech_started
speech_completed
```

Three of those lines are the ones worth reading carefully when something looks
wrong. `coaching_shot_diagnosed` with `coaching_kind: none` means nothing about
the shot could be judged — expected on the first shots of a session in observe
mode, where the round file supplies a ball speed and little else. That case
also logs `coaching_advice_suppressed` and `play_advice_suppressed` with the
missing measurements, and `speech_suppressed`. `play_advice_dropped` means a
newer completed shot arrived first, which is correct. A ball placed during
the result window is not that, and must not drop the line.
`coaching_unavailable` names the provider problem in `coaching_reason`; the
templated phrase stays on the floor either way, and that is the invariant to
check — the floor must never be blank because a model failed.

Nothing in this stage may delay a ball. If it does, the thing to look at is
`event_bus.subscribers`, specifically `coaching` and `speech`: `dropped` and
`coalesced` climbing there is the system protecting play, and `play` or
`history` climbing is a bug.

### Stage E5.5 — swing-video frames from an existing clip (no OpenAI)

Only after E5. This is **not** ProTee Shot Video (the OBS Window Capture
already working in E2). This is CaddieJ's own analysis clip: an MP4 it
recorded under `VIDEO_MEDIA_ROOT/clips/` with a sibling `.json` sidecar that
says where impact falls.

The check does not start the HUD, OBS, observers, coaching, or a camera.
It does not hit a ball. It does not call OpenAI. It inspects footage that
is already on disk.

You need at least one finished CaddieJ clip first. If `VIDEO_ENABLED` has
always been false, there is nothing to inspect — record one swing with
`VIDEO_ENABLED=true` (or `GSPRO_MODE=simulate` against the synthetic
camera), stop CaddieJ, then run the check. Set `VIDEO_CAMERA_VIEW` to
where the camera actually stands; the check reports it and does not
guess from the picture.

```powershell
caddiej --check-video-frames
```

Expect a selected path under `media/clips/YYYY-MM-DD/`, `impact timing`
of `estimated` or `calibrated`, four stills with time offsets such as
`-1.30 s` / `-0.45 s` / `+0.00 s` / `+0.35 s` (widened when timing is
estimated), then:

```
OpenAI calls: 0
Frames uploaded: 0
```

The four JPEGs and a contact sheet land under:

```
.caddiej/diagnostics/video-frames/<timestamp>/
  frame-01.jpg
  frame-02.jpg
  frame-03.jpg
  frame-04.jpg
  contact-sheet.html
```

Open the contact sheet or the JPEGs. Confirm they are four different
moments of the same swing, in time order, and that they are footage
from CaddieJ's camera — not ProTee's replay overlay. The labels are
seek offsets relative to the sidecar's impact, not detected swing
positions. Do not read a frame as address, the top, or impact.

`estimated` means nobody has measured camera latency or launch-monitor
report lag, so the check (and later coaching) spreads the seeks. That
is honest, not a failure. `calibrated` appears only when
`VIDEO_CAMERA_LATENCY_SECONDS` is set.

A failure names the step: `clip` if nothing under `clips/` has a
sidecar and a finished `moov`, `decoder` if FFmpeg is missing,
`extraction` if a still could not be decoded. It will not pick an
arbitrary MP4 from ProTee or the rest of the disk.

### Stage E6 — sending swing frames (opt-in, pictures of your house)

Only after E5, and only after `--check-video-frames` has extracted four
inspectable stills from a real CaddieJ clip.

```
VIDEO_ENABLED=true
VIDEO_CAMERA_VIEW=face_on
COACHING_SEND_FRAMES=true
```

Check that `COACHING_MODEL` accepts images before relying on this; a
text-only model will refuse the request and `coaching_unavailable` will say so
in `coaching_reason`, with the templated line still on the floor.

Set `VIDEO_CAMERA_VIEW` to where the camera actually is. Nothing can work it out
from the footage, and left `unknown` the model is told so and keeps off anything
that depends on the angle — which is safe, and also means you will not get
posture or plane observations.

What to check:

1. `coaching_request_started` reports `coaching_frames: 4`. Not more, whatever
   the clip length.
2. No image bytes anywhere in the log. Search it: `Select-String -Path
   .\caddiej-*.log -Pattern 'base64','data:image'` should find nothing.
3. Coaching may now name something only a picture shows — posture, tempo,
   standing up through the ball. It must not state a number it read off a
   frame, and it must not contradict a measured fault.
4. `coaching_footage_not_ready` means the clip did not arrive inside
   `COACHING_CLIP_WAIT`. The shot is still coached from its numbers, which is
   the intended fallback, and `coaching_frames` is then `0`.

### Stage E7 — stills of ProTee's own swing replay (opt-in)

Only after E2, because it uses the same OBS WebSocket connection, and
independent of E5.5/E6: those are CaddieJ's own camera, this is ProTee's
replay photographed through OBS. Nothing here opens a camera, reads
ProTee's recordings, or clicks anything in ProTee.

Photograph the panels named in `SWING_CAPTURE_PANELS`. This bay uses three. Do not create a source, and do not capture the floor's Flight Path replay as coach evidence.

| Panel | OBS source | Why |
| --- | --- | --- |
| `swing_behind` | `Swing Behind` | Down-the-line body replay. Dynamic. |
| `swing_side` | `Swing Side` | Face-on body replay. Dynamic. |
| `impact` | `Impact` | Strike image. Static. Not a body camera. |

`delivery` stays available for a bay that puts it in `SWING_CAPTURE_PANELS`. This bay does not.

```
SWING_CAPTURE_ENABLED=true
SWING_CAPTURE_PANELS=swing_behind,swing_side,impact
SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE=Swing Behind
SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE=Swing Side
SWING_CAPTURE_PANEL_IMPACT_SOURCE=Impact
SWING_CAPTURE_SELECTED_FRAMES=16
```

`SWING_CAPTURE_PANEL_SWING_SOURCE` is a deprecated alias for Swing Behind and
is used only when the behind source is empty. Do not set it as well.

`SWING_CAPTURE_CAMERA_1_SOURCE` and `SWING_CAPTURE_CAMERA_2_SOURCE` are not
read. A source that is not in the active scene may come back black.

Then, before hitting anything:

```powershell
caddiej --check-swing-capture
```

The first lines name the `.env` that was read and the effective panels, which
are exactly `SWING_CAPTURE_PANELS`. It then takes one screenshot from each of
those sources and writes one JPEG per panel under
`.caddiej\diagnostics\swing-capture\<timestamp>\`. A panel left out of the
list, including delivery, is not reported and not photographed. Floor replay
stays on `OBS_SHOT_VIDEO_SOURCE` and is not part of this check. Black means
that source was not rendering. A failure names the source and the environment
variable responsible; the names are compared exactly, spaces included.

A passing check proves the sources work. It says nothing about the timing,
which has been measured off one shot (see
[Which moments are aimed at](swing-capture.md#which-moments-are-aimed-at)).

Now start CaddieJ and hit one shot. `media\latest_swing\` should hold
`metadata.json` and one directory per configured panel.
What matters is whether the stills are the panels named above:

| What the frames show | What to change |
| --- | --- |
| A static panel at the start; the replay begins late in the set | Raise `SWING_CAPTURE_DYNAMIC_DELAY_SECONDS` |
| The replay already running in frame 1 | Lower it |
| A held final frame repeated at the end | Lower `SWING_CAPTURE_DYNAMIC_DURATION_SECONDS` |
| A static panel reports `settled=false` | Raise `SWING_CAPTURE_STATIC_DELAY_SECONDS` |
| The set ends mid-swing | Raise it |
| The last down-the-line still is before the release | Raise `SWING_CAPTURE_DYNAMIC_DURATION_SECONDS` |
| The top of the backswing missing entirely | Raise `SWING_CAPTURE_FPS`, then `SWING_CAPTURE_SELECTED_FRAMES` |

`metadata.json` carries the offsets, the configuration that produced them, and
`selection_interval.detected`. `false` there means the byte-difference trimming
found nothing to trim and the frames are spread across the whole window — which
happens when both panels were live the whole time, and is a working capture,
not a failure.

Then hit a second shot and confirm `latest_swing` is the new swing and not a
mixture: `capture_id` and `trigger_id` in `metadata.json` both change, and the
frame count per camera is whole. Confirm at the same time that E2's Shot Video
source, the marker lifecycle and the floor are unchanged — swing capture runs
on its own task and must cost the shot path nothing.

If the timing needs tuning, these are the lines that say why:

```powershell
Select-String -Path .\caddiej-*.log -Pattern 'swing_capture_|swing_replay_|swing_frames_'
```

`swing_capture_triggered` and `swing_capture_started` give the delay actually
waited, `swing_replay_interval_detected` (or `..._not_detected`) the part of the
window where the picture was moving, `swing_frames_selected` what was kept, and
`swing_capture_completed` how long the whole thing took. Per-frame detail is at
DEBUG. `swing_capture_camera_unavailable` appears once per camera per shot, not
once per frame, and carries the reason.

`/healthz` reports the same thing as counters under `swing_capture`: captures
started, completed, failed and cancelled, frames captured and kept, and the last
error. A `captures_cancelled` that climbs with every shot means captures are
still running when the next ball is struck — expected if the window is long, and
harmless, because the newest shot wins.

### Stage E8 — Coach V1 (Git Bash)

Prove the production path without a golf shot, then one real ball.

```bash
cd /c/Users/johan/git/caddiej
caddiej --check-coach-cues
caddiej --check-coach
caddiej --check-swing-capture
```

`--check-coach` runs `diagnose()`, evidence rendering, the configured
provider, validation, cue selection and `AnalysisService`. It does not
bypass that code. Then start CaddieJ and hit one full swing:

```bash
caddiej 2>&1 | tee caddiej-coach-$(date +%Y%m%d-%H%M%S).log
```

In a second window:

```bash
grep -E 'shot_id|shot_trigger_id|coaching_|swing_capture_|speech_' caddiej-coach-*.log
```

One identity should trace:

| Stage | Log event |
| --- | --- |
| Shot | `shot_completed` (`shot_id` and `shot_trigger_id`) |
| Diagnosis | `coaching_shot_diagnosed` |
| Session | `coaching_session_updated` |
| Capture | `swing_capture_triggered` → `swing_capture_completed`, or `swing_capture_skipped` |
| Evidence | `coaching_evidence_ready` (`evidence_grades`, `coaching_detail`) |
| OpenAI | `coaching_request_started`, or `coaching_request_skipped` |
| Advice | `coaching_advice_ready`, or `coaching_advice_suppressed` |
| Floor | `play_advice_shown`, or `play_advice_suppressed` |
| Speech | `speech_started` / `speech_completed`, or `speech_suppressed` |

`--check-coach` calls the real provider when one is configured. It does not
draw on the projector and it does not speak. `caddiej --check-speech` is the
Kokoro / `bm_george` check. `SWING_CAPTURE_CAMERA_1_SOURCE` is not read;
panels come from `SWING_CAPTURE_PANEL_*_SOURCE`. An enabled capture with no
panel logs `swing_capture_skipped` and coaching continues from the numbers.

A newer ball must produce `coaching_advice_ignored` / `SUPERSEDED` rather
than a stale line, and `speech` for the older shot must not start after
the newer diagnosis. OpenAI, OBS and speech failures must leave play
running; look for `coaching_advice_failed` / `swing_capture_failed` /
`speech_failed` without a floor stall.

`settled=false` on a static panel means raise
`SWING_CAPTURE_STATIC_DELAY_SECONDS`. `coaching_footage_not_ready` means
the capture lost the race against `COACHING_CLIP_WAIT`.

### Reading a missing figure on the round-result panel

The floor no longer draws a dash. If `SLAG` or `KVAR` is simply absent, one log
line settles where the value was lost, because it carries both halves of the
conversion:

```
floor_round_result_presented
  round_total_distance_raw / round_distance_to_pin_raw    ← what the file had
  round_shot_distance_display / round_remaining_distance_display
```

`KVAR` is an optional figure and is hidden rather than dashed, so a missing
one is not a dash to chase. `round_distance_to_pin_raw` says whether GSPro
supplied anything at all. `ball_speed_display` still appears on `/snapshot`; it
is not drawn.

Raw present and display present means Python is correct, and the browser is
rendering something stale. Compare `hud_assets_version` on `/healthz` against
`overlay_assets_loaded` in the log (or `overlay_assets_stale` if they differ).
Overlay files are served from `/static/hud/<hash>/`, not a `?v=` query
string, because OBS CEF ignores the latter. After a CaddieJ restart,
right-click the Browser Source → **Refresh cache of current page**.

```powershell
Invoke-RestMethod http://127.0.0.1:8730/snapshot | ConvertTo-Json -Depth 8
```

`round_result.ball_speed_display` in that JSON is exactly what the browser was
sent. Raw floats are deliberately absent from it; the overlay has no fallback
and must not grow one.

### Stage F — graceful shutdown with an in-flight final shot

Hit a ball and press Ctrl+C while the shot is still on the floor (or
immediately after impact). History `shots_recorded` should still
include that shot after process exit. A second Ctrl+C leaves
immediately; avoid that if you are testing drain.

## Floor and shot-visual checklist (Git Bash)

Ten steps against the real simulator, for the milestone that made the
hitting strip the floor's anchor, gave the putter its own ball position
and lane, fixed the marker lifecycle, and added the Top Impact reveal.
Each step names the structured log event to grep for when it fails.

Nothing below is proven by the test suite. The suite proves the logic;
these steps prove the room.

Run from the repository directory that holds `.env`:

```bash
cd /c/Users/johan/git/caddiej
python -m pip install -e '.[dev]'
caddiej --check-config
```

Read the `Floor layout` line it prints — the anchor and the three
resolved ball positions, in metres — and hold a tape measure against it
before starting. Then run CaddieJ with a log you can grep:

```bash
caddiej 2>&1 | tee caddiej-floor-$(date +%Y%m%d-%H%M%S).log
```

In a second Git Bash window, follow it:

```bash
tail -f caddiej-floor-*.log
```

**A. Start CaddieJ.** The projector shows the banner, then the strip and
a marker.

```bash
grep -E 'caddiej_ready|floor_layout_configured|floor_hitting_area_lit' caddiej-floor-*.log
```

`floor_layout_configured` carries `floor_anchor_*_m` and the three
`floor_ball_*_m` positions. If the marker is off the projection
entirely, look for `floor_ball_position_off_surface`: the position was
clamped to the edge and the configured value is in that line.

**B. Verify the iron marker.** Put an iron in your hands and let ProTee
detect it. The marker is the plain variant and sits at the iron
position.

```bash
grep play_club_in_play caddiej-floor-*.log
```

`gspro_club` is the kind CaddieJ decided on and `gspro_club_code` is
what ProTee actually wrote. Placement follows the kind, so a marker in
the wrong place with the right club name on the floor means the kind
was `unknown` and the iron fallback was used.

**C. Select a driver.** Same grep: `gspro_club` becomes `driver` and the
marker moves to the driver position and changes variant.

**D. Select a putter.** The marker becomes the putter variant, moves to
the putter position — which may be off the mat — and a lane appears
running down-range from it.

```bash
grep floor_putting_mode_changed caddiej-floor-*.log
```

`floor_putting` is `true` on the way in and `false` on the way out. No
line at all means the club never reached the domain as a putter: check
step B's grep for what ProTee actually said.

**E. Hit at least three consecutive normal shots.** The marker must
disappear as the ball is found and come back after every shot. This is
the regression; count the marker on the floor, because there is no log
line per marker.

```bash
grep -E 'floor_ball_lock_acknowledged|floor_information_mode_changed' caddiej-floor-*.log
```

The strip pulses once per ball found, which is `floor_ball_lock_acknowledged`,
so that line appearing three times and the marker returning twice is
the shape of the failure. What the floor is currently showing, marker
included, is always available directly:

```bash
curl -s http://127.0.0.1:8730/snapshot | python -m json.tool | grep -A 4 '"marker"'
```

If the marker fails to return, the question is whether the ball is
still being reported present. Both `READY FOR SHOT` and `FINDING BALL`
now end the previous shot's placement cycle, so a marker that stays
hidden means no readiness line reached CaddieJ after the shot — check
`protee_observer.last_event_at` in `/healthz`.

**F. Verify Shot Video appears.**

```bash
grep -E 'shot_video_(feature_enabled|source_resolved|replay_scheduled|source_visible|source_hidden|source_missing|obs_unavailable)' caddiej-floor-*.log
```

Nothing at all means the feature is off or no detection arrived;
`shot_video_source_missing` means the name in OBS does not match. Prove
the OBS half on its own with `caddiej --check-obs-shot-video`.

**G. Verify Top Impact appears.** Exactly the same, under its own name:

```bash
grep -E 'top_impact_(feature_enabled|source_resolved|replay_scheduled|source_visible|source_hidden|source_missing|obs_unavailable)' caddiej-floor-*.log
```

```bash
caddiej --check-obs-top-impact
```

One grep for both features, when you want to see them side by side:

```bash
grep -E '(shot_video|top_impact)_(replay_scheduled|source_visible)' caddiej-floor-*.log
```

**H. Hit a putt.** Expect the lane, and expect **no** rolled line in
most cases — see [GSPro round observation](gspro.md) for why.

```bash
grep -E 'floor_putt_path_(drawn|unavailable)' caddiej-floor-*.log
```

`floor_putt_reason` on the `unavailable` line says which check refused
it, and `floor_putt_samples` says how many path samples GSPro had
written. Capture both for a few putts: that is the measurement that
decides whether a real putt path is ever renderable from this file.

**I. Move the hitting strip by a clearly visible amount.** Stop CaddieJ,
change one number, restart:

```bash
grep -n '^FLOOR_HITTING_STRIP_Y_M=' .env
# edit it by 0.5 m or more, then
caddiej --check-config | grep 'Floor layout'
```

All three ball positions, the pre-shot distance, the club
recommendation, the shot distance, the remaining distance and the
putting lane must move together by that amount and keep their spacing.
The startup banner and celebrations deliberately do not move.

```bash
grep floor_layout_configured caddiej-floor-*.log | tail -1
```

If some elements moved and others did not, that is the anchoring bug
this step exists to catch — note which stayed put.

**J. Restore the final physical calibration.** Put
`FLOOR_HITTING_STRIP_*` back to the measured values, restart, and
confirm the `Floor layout` line matches the tape measure again.

## Health fields to watch

`GET /healthz` is a JSON object. Top-level keys always include
`status`, `protocol_version` (currently `1`), `state_version`, `mode`,
`clients`, `hud_static_root` (the overlay files this process is serving),
and `hud_assets_version` (content hash of overlay HTML/JS/CSS). `GET /snapshot`
is the same JSON the Browser Source receives on the WebSocket. The profile
also includes the subsystem objects below.
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

`FLOOR_WIDTH_M`, `FLOOR_DEPTH_M`, `FLOOR_HITTING_STRIP_X_M`,
`FLOOR_HITTING_STRIP_Y_M`, `FLOOR_HITTING_STRIP_WIDTH_M`,
`FLOOR_HITTING_STRIP_DEPTH_M`, `FLOOR_CLUB_PATH_LENGTH`, every
`FLOOR_BALL_*` and `FLOOR_INFORMATION_*` offset, and every
`PLAYER_CLUB_CARRY_*_M` are metres. The example file keeps the
repository defaults (1 m × 1 m identity, strip centred where the tee
used to be). Change them to the real bay before treating a drawn path
angle as the launch-monitor number on a non-square floor. Do not commit
a measured room back to the repository unless it is a generic default.
Changing these values requires a CaddieJ restart.

The strip centre is the anchor: the three ball positions, the
information panels and the putting lane are offsets from it, so moving
the mat or the projector is those two variables and nothing else.
`caddiej --check-config` prints the anchor and the three resolved ball
positions in metres, which is what to hold a tape measure against.

`FLOOR_TEE_X_M`, `FLOOR_TEE_Y_M`, `FLOOR_TEE_DRIVER_X_M` and
`FLOOR_TEE_DRIVER_Y_M` still work and are converted to offsets by
subtracting the anchor, but they are no longer in the example file. Do
not set both them and the offsets on a validation machine: the offsets
win, and having two ways to say it is how a bay ends up with the marker
somewhere nobody chose.
