# Configuration reference

[← Documentation index](README.md)

Every setting CaddieJ has, what it defaults to, and what it does. For the
reasoning behind a default — why speech is off, why the assistant API refuses to
start without a token — read [`.env.example`](../.env.example), which is the
same list annotated in full.

## How settings are read

CaddieJ is configured entirely through environment variables. There are no
command-line options for any of them.

1. **Environment variables** win.
2. **A `.env` file** in the working directory fills in the rest.
3. **The defaults below** apply to anything left unset.

```shell
cp .env.example .env
```

`.env` is git-ignored and must stay that way: it is where credentials live.

Unknown variables are ignored, so an old setting left in a `.env` file is
harmless. An invalid one is not: CaddieJ names it, says what it would have
accepted, and exits without starting.

```shell
caddiej --check-config
```

That reports what CaddieJ would run with, resolves every path so you can see
where files will be written, and says which optional dependencies are installed.
It never prints a credential — only whether one is set.

Paths accept `~` for your home directory. Relative paths are resolved against
the directory you start CaddieJ from, which is also where `caddiej.sqlite3` and
`media/` appear if you leave them at their defaults.

`LOG_LEVEL` is the one setting read outside this table, because it decides how a
problem with the others is reported. It takes `DEBUG`, `INFO`, `WARNING`,
`ERROR` or `CRITICAL`, defaults to `INFO`, and `--log-level` overrides it.

---

## Floor overlay and HUD server

The web surface the projector renders. Loopback only: the browser and OBS are
local, and nothing here is safe to expose on a network.

| Variable | Default | What it does |
| --- | --- | --- |
| `HUD_HOST` | `127.0.0.1` | Address the overlay is served on. |
| `HUD_PORT` | `8730` | Port the overlay is served on. |
| `HUD_STARTUP_TIMEOUT` | `10.0` | How long the server gets to start listening. |
| `FLOOR_STATUS_TEXT` | `CADDIEJ FLOOR ONLINE` | What the idle floor says. |
| `FLOOR_WIDTH_M` | `1.0` | Physical width of the hitting bay, metres, left to right. Path angles are computed here. The default 1 m makes surface coordinates numerically identical to metres, so a fresh checkout looks as it always has. Not a pixel count. |
| `FLOOR_DEPTH_M` | `1.0` | Physical depth of the hitting bay, metres, back of the surface towards the player. Same default as the width, for the same reason. |
| `FLOOR_HITTING_STRIP_X_M` | `0.5` | Hitting-strip **centre** X, metres — not a corner. The anchor: everything golf-related on the floor is placed from it. Must lie on `FLOOR_WIDTH_M`. |
| `FLOOR_HITTING_STRIP_Y_M` | `0.72` | Hitting-strip **centre** Y, metres, towards the player. Must lie on `FLOOR_DEPTH_M`. |
| `FLOOR_HITTING_STRIP_WIDTH_M` | `0.30` | Hitting-strip width, metres, left to right. A generic turf pad, not a measured room. |
| `FLOOR_HITTING_STRIP_DEPTH_M` | `0.45` | Hitting-strip depth, metres, back towards the player. |
| `FLOOR_HITTING_STRIP_FILL` | `false` | Fill the whole calibrated rectangle instead of outlining it, so the projector lights the physical turf. The ball marker inverts to stay readable on top. |
| `BALL_STATUS_CHAMFER_SCALE` | `6.0` | Uniform scale of the Compact chamfer on the strip's rear edge. 1 is the drawing (17.8 mm rise, 13.9 mm run, 79.8 mm span, 0.8 mm stroke, 9.5 mm type, 1.8 mm lamp). Every one of those, and the gaps between them, is multiplied by this. The feet stay on the rear edge. It does not move the strip and it does not change `FLOOR_WIDTH_M` or `FLOOR_DEPTH_M`. |
| `FLOOR_BALL_IRON_DX_M` | `0.0` | Iron ball position, metres across the bay from the strip centre. Every club that is not a driver or a putter uses this, and so does a club nothing has named. |
| `FLOOR_BALL_IRON_DY_M` | `0.0` | Iron ball position, metres towards the player from the strip centre. Negative is down-range. |
| `FLOOR_BALL_DRIVER_DX_M` | *(inherits iron)* | Driver ball position across the bay. |
| `FLOOR_BALL_DRIVER_DY_M` | *(inherits iron)* | Driver ball position towards the player. Usually negative: a driver is played further forward. |
| `FLOOR_BALL_PUTTER_DX_M` | `0.0` | Putter ball position across the bay. |
| `FLOOR_BALL_PUTTER_DY_M` | `-0.30` | Putter ball position towards the player. Allowed off the strip, and by default is: 0.30 m down-range clears the front edge of a 0.45 m mat. |
| `FLOOR_INFORMATION_DX_M` | `0.0` | Where the anchored panels are drawn from, across the bay: distance to the pin, recommended club, shot distance, remaining distance, and the coaching line under them. |
| `FLOOR_INFORMATION_DY_M` | `-0.66` | The same, towards the player. The default reproduces the placement the overlay has always had on the default 1 m × 1 m bay. |
| `FLOOR_PUTTING_LANE` | `true` | Draw a lane down-range from the putter ball while a putter is in play. |
| `FLOOR_PUTTING_LANE_LENGTH_M` | `0.30` | How far down-range the lane runs, metres. |
| `FLOOR_PUTTING_LANE_WIDTH_M` | `0.24` | How wide the lane is, metres. |
| `FLOOR_TEE_X_M` | *(unset)* | Deprecated. An absolute ball position on the bay, from before the strip was the anchor. Converted to `FLOOR_BALL_IRON_DX_M` by subtracting `FLOOR_HITTING_STRIP_X_M`. Ignored if the offset is set. |
| `FLOOR_TEE_Y_M` | *(unset)* | Deprecated. The same, for `FLOOR_BALL_IRON_DY_M`. |
| `FLOOR_TEE_DRIVER_X_M` | *(unset)* | Deprecated. Converted to `FLOOR_BALL_DRIVER_DX_M` the same way. |
| `FLOOR_TEE_DRIVER_Y_M` | *(unset)* | Deprecated. Converted to `FLOOR_BALL_DRIVER_DY_M` the same way. |
| `FLOOR_TEE_X` | *(unset)* | Deprecated. A 0–1 fraction of `FLOOR_WIDTH_M`, converted to `FLOOR_TEE_X_M` only when that variable is unset. Ignored, with a warning, if both are set. |
| `FLOOR_TEE_Y` | *(unset)* | Deprecated. A 0–1 fraction of `FLOOR_DEPTH_M`, converted to `FLOOR_TEE_Y_M` only when that variable is unset. |
| `FLOOR_CLUB_PATH_LENGTH` | `1.2` | How far the club-path line is drawn, in metres of the physical floor. About a pace on a real bay — not a 0–1 fraction of the overlay. |
| `FLOOR_SHOT_DISPLAY_SECONDS` | `6.0` | How long an interpose/simulated shot's path and numbers stay up. |
| `FLOOR_SHOT_RESULT_MIN_DURATION` | `8` | Observer round-result panel: guaranteed readable for this long. A READY / new ball inside it is deferred and applied the moment it ends; the address marker is not held back. Must not exceed the maximum. |
| `FLOOR_SHOT_RESULT_MAX_DURATION` | `30` | The longest that panel may stay when no new address cycle arrives. A new shot outranks both timers. |
| `FLOOR_DEMO` | `false` | Cycle the floor through every state it can render. Development only. |
| `FLOOR_DEMO_STEP_SECONDS` | `2.5` | How long each demo step lasts. |

`FLOOR_HITTING_STRIP_FILL` lights the bay for as long as the floor is up. It is
not affected by READY, FINDING BALL, a ball on the mat, the ball marker, the
address numbers, a round result, or a Shot Video replay; only shutting CaddieJ
down puts it out.

### Recalibrating after the mat or the projector moves

Measure the new centre of the strip, set `FLOOR_HITTING_STRIP_X_M` and
`FLOOR_HITTING_STRIP_Y_M`, and restart. The three ball positions, both panels,
the putting lane, and the ball-status chamfer on the rear edge are all placed
from that rectangle, so they move with it and keep their spacing.
`BALL_STATUS_CHAMFER_SCALE` is the one exception: it changes how large the
chamfer is, not where the strip is. Nothing else needs touching.

The offsets themselves answer a different question — where the ball sits *on*
the mat, and how far in front of it the numbers are drawn — and that does not
change when the mat does.

Two things are deliberately not anchored, because they belong to the whole
projection rather than to the hitting area: the startup banner and the
celebration layer. Moving the strip does not move them.

`caddiej --check-config` prints the anchor and the three resolved ball
positions in metres, which is what to compare against a tape measure.

## Your bag

How far you carry each club, in metres. CaddieJ recommends the club whose carry
is nearest the distance GSPro says is left to the target, and shows it beside
that distance while the next shot is being addressed.

Carry, not total: roll belongs to the hole, and flight belongs to the club.
Metres, because GSPro's `DistanceToTarget` is metres and nothing converts
anything.

`0` means the club is not in the bag. Every club is `0` by default, and a bag
with nothing in it produces **no recommendation at all** rather than a wrong
one — the floor shows the distance on its own until these are filled in.

| Variable | Default | What it does |
| --- | --- | --- |
| `PLAYER_CLUB_CARRY_DRIVER_M` | `0` | Driver carry, metres. |
| `PLAYER_CLUB_CARRY_3W_M` | `0` | 3 wood carry, metres. |
| `PLAYER_CLUB_CARRY_5W_M` | `0` | 5 wood carry, metres. |
| `PLAYER_CLUB_CARRY_4I_M` | `0` | 4 iron carry, metres. |
| `PLAYER_CLUB_CARRY_5I_M` | `0` | 5 iron carry, metres. |
| `PLAYER_CLUB_CARRY_6I_M` | `0` | 6 iron carry, metres. |
| `PLAYER_CLUB_CARRY_7I_M` | `0` | 7 iron carry, metres. |
| `PLAYER_CLUB_CARRY_8I_M` | `0` | 8 iron carry, metres. |
| `PLAYER_CLUB_CARRY_9I_M` | `0` | 9 iron carry, metres. |
| `PLAYER_CLUB_CARRY_PW_M` | `0` | Pitching wedge carry, metres. |
| `PLAYER_CLUB_CARRY_GW_M` | `0` | Gap wedge carry, metres. |
| `PLAYER_CLUB_CARRY_SW_M` | `0` | Sand wedge carry, metres. |

The label in the variable name is what appears on the floor. Two clubs the same
distance from the target resolve to the longer one.

These are expected to be replaced by carries learned from your own shot history.
When that happens only where the numbers come from changes: the recommendation
and the floor do not read these names.

## Shutdown

| Variable | Default | What it does |
| --- | --- | --- |
| `SHUTDOWN_TIMEOUT` | `5.0` | How long any one step of shutdown gets. |
| `SHUTDOWN_GRACE` | `20.0` | How long all of them get together, before what is left is stepped over. |

A second Ctrl+C leaves immediately, whatever these say.

## GSPro and the launch monitor

See [GSPro and a launch monitor](gspro.md).

| Variable | Default | What it does |
| --- | --- | --- |
| `GSPRO_MODE` | `off` | `off`, `observe` (read ProTee's log and GSPro currentRound.dat; not in their traffic path), `interpose` (experimental Open Connect bridge), or `simulate` (a scripted round, development only). |
| `GSPRO_BIND_HOST` | `127.0.0.1` | Address the launch monitor connects to. |
| `GSPRO_BIND_PORT` | `9210` | Port the launch monitor connects to, instead of GSPro's own. |
| `GSPRO_UPSTREAM_HOST` | `127.0.0.1` | Where GSPro itself is. |
| `GSPRO_UPSTREAM_PORT` | `921` | GSPro's documented Open Connect port. |
| `GSPRO_CONNECT_TIMEOUT` | `5.0` | How long to wait for GSPro to accept a connection. |
| `GSPRO_IDLE_TIMEOUT` | `180.0` | How long a bridged connection may say nothing before it is dropped. `0` disables. |
| `GSPRO_BACKOFF_INITIAL` | `0.5` | How long CaddieJ refuses the launch monitor after failing to reach GSPro. |
| `GSPRO_BACKOFF_MAX` | `5.0` | The ceiling on that wait. |
| `GSPRO_MAX_MESSAGE_BYTES` | `65536` | Largest single message accepted from either side. |
| `GSPRO_SHOT_ASSEMBLY_WINDOW` | `0.25` | How long a shot missing half its data waits for the rest. Never delays forwarding. |
| `GSPRO_BALL_PRESENT_DEBOUNCE` | `0.0` | How long a ball must be seen before the marker is cleared. |
| `GSPRO_BALL_ABSENT_DEBOUNCE` | `1.5` | How long a ball must be gone before the marker returns. |
| `GSPRO_SIMULATION_STEP_SECONDS` | `3.0` | How long the simulated round pauses between steps. |
| `GSPRO_ROUND_OBSERVER_ENABLED` | `true` | When `GSPRO_MODE=observe`, watch GSPro `currentRound.dat`. |
| `GSPRO_ROUND_FILE_PATH` | *(Windows default)* | Path to `currentRound.dat`. Empty derives `%USERPROFILE%\AppData\LocalLow\GSPro\GSPro\currentRound.dat`. Never written. |
| `GSPRO_ROUND_POLL_INTERVAL` | `0.1` | Seconds between read-only polls of the round file. |
| `PROTEE_OBSERVER_ENABLED` | `true` | When `GSPRO_MODE=observe`, follow the ProTee semantic main log. |
| `PROTEE_ROOT_PATH` | *(Windows default)* | ProTee United root. Empty derives `%APPDATA%\ProTeeUnited`. Never written. |
| `PROTEE_LOG_POLL_INTERVAL` | `0.1` | Seconds between read-only tail polls of `Logs\LogFile-YYYY-MM-DD.txt`. |

## Shot history

See [Shot history](history.md).

| Variable | Default | What it does |
| --- | --- | --- |
| `HISTORY_ENABLED` | `true` | Whether shots are recorded at all. |
| `HISTORY_DATABASE_PATH` | `caddiej.sqlite3` | Where the SQLite file lives. |
| `PLAYER_NAME` | *(blank)* | Whose shots these are. Blank stores them unnamed. |
| `SESSION_LABEL` | *(blank)* | A label for this run, if you want one. |
| `HISTORY_QUEUE_SIZE` | `256` | Shots that may be waiting to be written before further ones are refused. |
| `HISTORY_BUSY_TIMEOUT` | `5.0` | How long SQLite waits for a file another process has locked. |
| `HISTORY_DRAIN_TIMEOUT` | `5.0` | How long shutdown waits for queued shots to reach the disk. |
| `HISTORY_RECORD_RAW_PAYLOADS` | `false` | Keep the exact bytes each shot was read from, for diagnosing a device. |
| `HISTORY_RAW_PAYLOAD_MAX_BYTES` | `8192` | Ceiling on one stored payload. |

## Swing video

Off by default. Needs FFmpeg on `PATH`. See [Swing video](video.md).

| Variable | Default | What it does |
| --- | --- | --- |
| `VIDEO_ENABLED` | `false` | Whether anything is filmed. |
| `VIDEO_CAMERA_KIND` | `synthetic` | `synthetic` (FFmpeg's own picture, no hardware), `avfoundation` (macOS), `v4l2` (Linux), `dshow` (Windows). |
| `VIDEO_CAMERA_DEVICE` | `0` | An index, a device node, or a DirectShow friendly name. |
| `VIDEO_CAMERA_ID` | `tee` | Names the camera in filenames, events and health. |
| `VIDEO_CAMERA_VIEW` | `unknown` | `unknown`, `face_on` or `down_the_line`. Told to the coach with the frames, because it decides what a still can prove. |
| `VIDEO_CAMERA_FPS` | `30` | Frames per second to ask the camera for. |
| `VIDEO_CAMERA_WIDTH` | `1280` | Capture width. |
| `VIDEO_CAMERA_HEIGHT` | `720` | Capture height. |
| `VIDEO_CAMERA_LATENCY_SECONDS` | `0.0` | How long after the light hits the sensor a frame is timestamped. `0` means unmeasured. |
| `VIDEO_MEDIA_ROOT` | `media` | Everything video writes lives under here. |
| `VIDEO_FFMPEG_PATH` | `ffmpeg` | The binary to run. |
| `VIDEO_ENCODER` | `libx264` | `h264_videotoolbox`, `h264_nvenc` or `h264_qsv` on a machine with a hardware encoder. |
| `VIDEO_BITRATE` | `8M` | Recording bitrate. |
| `VIDEO_PRE_IMPACT_SECONDS` | `2.0` | How much of the backswing to keep. |
| `VIDEO_POST_IMPACT_SECONDS` | `1.5` | How much of the follow-through to keep. |
| `VIDEO_BUFFER_SECONDS` | `30.0` | How much footage is kept ready to be clipped. |
| `VIDEO_BUFFER_MAX_BYTES` | `2147483648` | The same limit by size. |
| `VIDEO_SEGMENT_SECONDS` | `1.0` | Segment length, and so how precisely a window can be honoured. |
| `VIDEO_KEYFRAME_SECONDS` | `0.5` | Keyframe interval. |
| `VIDEO_SHOT_REPORT_LAG_SECONDS` | `0.0` | How long before the shot message the ball was actually struck. |
| `VIDEO_CLIP_QUEUE_SIZE` | `8` | Clip requests that may be waiting before further ones are refused. |
| `VIDEO_CLIP_RETENTION_DAYS` | `30` | How long finished clips are kept. Older days are deleted at start-up; `0` keeps everything. |
| `VIDEO_EXTRACT_TIMEOUT` | `30.0` | How long one extraction may take. |
| `VIDEO_RESTART_LIMIT` | `5` | Consecutive failures before the camera is left alone. |
| `VIDEO_DRAIN_TIMEOUT` | `5.0` | How long shutdown waits for a clip in progress. |

## Coaching

Ideal Swing 2.9. A shot is assessed only when vision calls are on. See
[Coaching](coaching.md) and [Swing reference](swing-reference.md).

| Variable | Default | What it does |
| --- | --- | --- |
| `COACHING_ENABLED` | `true` | Whether Ideal Swing coaching is assembled. Vision calls stay off until `COACHING_VISION_CALLS` is set. |
| `COACHING_PROVIDER` | `none` | `none` (no model), `scripted` (answers from inside the process), or `openai`. |
| `COACHING_MODEL` | `gpt-5-mini` | Which model to ask. |
| `COACHING_API_KEY` | *(blank)* | **Credential.** Never logged, never committed. |
| `COACHING_API_BASE` | *(blank)* | Any endpoint implementing OpenAI's Responses API, including a local one. |
| `COACHING_FRAME_WIDTH` | `512` | Width of stills extracted from a recorded camera clip. The evidence pipeline does not use this. |
| `COACHING_CALL_LIMIT` | `60` | A ceiling on vision calls for one run. |
| `COACHING_EVIDENCE_BUDGET` | `24` | How many timeline frames the evidence pipeline may keep for one coached shot. This is not `SWING_CAPTURE_SELECTED_FRAMES` (published replay stills, default 16) and not `SWING_CAPTURE_DIAGNOSTIC_TIMELINE_SHOTS` (how many shots keep a full timeline, default 0). |
| `COACHING_EVIDENCE_ALLOCATION` | `dtl_primary` | `dtl_only` (24/0), `dtl_primary` (20 down-the-line and 4 face-on), `balanced` (12/12), or `fo_primary` (8/16), scaled when the budget is not 24. |
| `COACHING_VISION_CALLS` | `false` | When true, a captured shot is assessed against Ideal Swing 2.9. When false, no model is called and no line is spoken in its place. |
| `COACHING_IMAGE_DETAIL` | `high` | `low`, `high`, or `auto`. `high` is the default so hands, shaft and hips stay readable. It uses more tokens per image than `low`. It does not change the image count. |
| `COACHING_FEEDBACK` | `observed` | `exact`, `observed`, or `worded`. All three use Ideal Swing 2.9 and the same session. `exact` speaks the stored correction. `observed` may prefix the assessment observation when it checks out. `worded` adds a second text call for that observation. A model does not rewrite the correction. |
| `COACHING_SHOT_TYPE` | `unknown` | `unknown`, `stock`, or `distance_wedge`. `unknown` leaves shot type unset, so iron checkpoints stay unassessed. Set `stock` for a session of normal full iron shots. A club or a carry does not select it. `distance_wedge` is only the 60–120 yard wedge setup context. |
| `COACH_OPENAI_TIMEOUT_SECONDS` | `30` | Seconds one vision attempt may take, passed to the OpenAI SDK. Up to 120. |
| `COACH_OPENAI_BUDGET_SECONDS` | `45` | Seconds that bound the attempt. At least the timeout, up to 300. |
| `FLOOR_COACH_CUE_IMAGE` | `false` | Draw the cue graphic on the floor. Off until real artwork exists. |

## Speech

Off by default. Needs `pip install -e '.[tts]'`. See [Speech](speech.md).

| Variable | Default | What it does |
| --- | --- | --- |
| `SPEECH_ENABLED` | `false` | Whether anything is said out loud. |
| `SPEECH_ENGINE` | `kokoro` | `kokoro` (a local model), or `fake` (silence of a believable length). |
| `SPEECH_OUTPUT` | `sounddevice` | `sounddevice`, or `none` to render everything and play nothing. |
| `SPEECH_VOICE` | `bm_george` | Any Kokoro voice. The first letter picks the language. |
| `SPEECH_SPEED` | `1.0` | How fast it talks. |
| `SPEECH_DEVICE` | *(blank)* | A PortAudio device index or name. Blank uses the system default. |
| `SPEECH_GAIN` | `1.0` | One constant multiplier on every line, applied at the speaker. Kokoro peaks around a third of full scale, so the headroom is real; `--check-speech` reports what the gain clipped. Maximum 4.0. |
| `SPEECH_QUEUE_SIZE` | `4` | Lines that may be waiting. |
| `SPEECH_MAX_CHARACTERS` | `300` | A ceiling on one sentence. |
| `SPEECH_RENDER_TIMEOUT` | `20.0` | How long rendering one line may take. |
| `SPEECH_OPEN_TIMEOUT` | `90.0` | How long loading the voice may take, including a first-run download. |
| `SPEECH_CACHE_ENABLED` | `true` | Keep rendered lines, because coaching repeats itself. |
| `SPEECH_CACHE_ROOT` | `media/speech` | Where those WAV files live. |
| `SPEECH_CACHE_BYTES` | `33554432` | Cache ceiling by size. |
| `SPEECH_CACHE_FILES` | `512` | Cache ceiling by count. |

## Lighting

Off by default. Needs `pip install -e '.[hue]'`. See [Lighting](lighting.md).

| Variable | Default | What it does |
| --- | --- | --- |
| `LIGHTING_ENABLED` | `false` | Whether CaddieJ touches your lights at all. |
| `LIGHTING_OUTPUT` | `hue` | `hue`, or `none` to run the whole path and change no lamp. |
| `LIGHTING_BRIDGE_HOST` | *(blank)* | The bridge's address. An IP is steadier than a hostname. |
| `LIGHTING_APPLICATION_KEY` | *(blank)* | **Credential.** From the one-time pairing step. |
| `LIGHTING_LIGHTS` | *(blank)* | Which lamps, by name or identifier, comma separated. Blank means all of them. |
| `LIGHTING_MINIMUM` | `0.82` | How far the lights dip, as a fraction of where they were. |
| `LIGHTING_MAXIMUM` | `1.0` | Where they return to. Equal to the minimum switches the reaction off. |
| `LIGHTING_MAX_COMMANDS_PER_SECOND` | `10.0` | The bridge's whole budget, shared across every lamp. Philips' own guidance. |
| `LIGHTING_TRANSITION_SCALE` | `1.0` | How long each command is given, as a multiple of the gap to the next. |
| `LIGHTING_FAILURE_LIMIT` | `3` | Consecutive failures before the bridge is left alone. |
| `LIGHTING_COOLDOWN_SECONDS` | `60.0` | How long it is left alone for. |
| `LIGHTING_COMMAND_TIMEOUT` | `3.0` | How long one command may take. |

## Celebrations

Inert until something scores a hole. See [Celebrations](celebrations.md).

| Variable | Default | What it does |
| --- | --- | --- |
| `EFFECTS_ENABLED` | `true` | Whether the room reacts to a finished hole. |
| `EFFECTS_FLOOR` | `true` | Rings and a word on the projection. |
| `EFFECTS_SPEECH` | `true` | A short line in CaddieJ's voice. |
| `EFFECTS_LIGHTS` | `true` | A dip in the room lights. |
| `EFFECTS_OBS` | `false` | Reveal a source you built in a scene you built. |
| `EFFECTS_OBS_SCENE` | *(blank)* | Which scene that source is in. |
| `EFFECTS_OBS_SOURCE` | *(blank)* | Which source to show for the length of the effect. |
| `EFFECTS_OBS_TEXT_SOURCE` | *(blank)* | An optional text source, captioned with the outcome. |
| `EFFECTS_MAX_SECONDS` | `8.0` | Hard cap on how long any effect may hold the room. |
| `EFFECTS_CHANNEL_TIMEOUT` | `3.0` | How long any single output gets to accept a command. |

## Assistant API

Off by default. See [Assistant API](assistant-api.md).

| Variable | Default | What it does |
| --- | --- | --- |
| `ASSISTANT_API_ENABLED` | `false` | Whether the port is opened at all. |
| `ASSISTANT_API_HOST` | `127.0.0.1` | Loopback, and enforced: a non-loopback value is refused at start-up. |
| `ASSISTANT_API_PORT` | `8731` | Port the API answers on. |
| `ASSISTANT_API_TOKEN` | *(blank)* | **Credential.** Required. Blank with the API enabled means the port is not opened. |
| `ASSISTANT_API_ALLOW_MUTATIONS` | `false` | Whether an assistant may change anything. Off means the route does not exist. |
| `ASSISTANT_API_INCLUDE_COACHING_PROSE` | `false` | Whether the sentences a player read cross the boundary. Faults and severities do either way. |
| `ASSISTANT_API_STARTUP_TIMEOUT` | `10.0` | How long the API server gets to start listening. |

## OBS

Only needed if OBS composites the overlay. See
[The floor overlay](floor.md#using-obs-as-the-compositor).

| Variable | Default | What it does |
| --- | --- | --- |
| `OBS_HOST` | `localhost` | Where OBS is. |
| `OBS_PORT` | `4455` | The WebSocket v5 port. |
| `OBS_PASSWORD` | *(blank)* | **Credential.** From Tools → WebSocket Server Settings. |
| `OBS_CONNECT_TIMEOUT` | `10.0` | How long to wait for OBS to answer. |
| `OBS_PROVISION_FLOOR_SOURCE` | `false` | Whether CaddieJ creates or reconciles the browser source. Off, because it modifies the active scene collection. |
| `OBS_FLOOR_SCENE` | `CaddieJ Test` | Which scene the overlay source belongs to. The scene must already exist. |
| `OBS_FLOOR_SOURCE` | `CaddieJ Floor` | What that browser source is called. |
| `OBS_FLOOR_WIDTH` | `1920` | Browser source width. |
| `OBS_FLOOR_HEIGHT` | `1080` | Browser source height. |
| `FLOOR_SHOT_VIDEO_ENABLED` | `false` | Temporarily reveal the existing ProTee Shot Video Window Capture after a round shot. Off so a missing source cannot surprise a first run. |
| `OBS_SHOT_VIDEO_SOURCE` | `Shot Video` | Name of that existing source in `OBS_FLOOR_SCENE`. CaddieJ does not create it. |
| `FLOOR_SHOT_VIDEO_DELAY_SECONDS` | `0.75` | Wait after the launch monitor's own detection so ProTee can refresh the panel. Tune on hardware; this default is a first guess. |
| `FLOOR_SHOT_VIDEO_DURATION_SECONDS` | `4` | How long the source stays visible before CaddieJ hides it again. |
| `FLOOR_TOP_IMPACT_ENABLED` | `false` | The same for ProTee's Top Impact view. Independent of Shot Video in every respect: own connection, own timers, own failures. |
| `OBS_TOP_IMPACT_SOURCE` | `Top Impact` | Name of that existing source in `OBS_FLOOR_SCENE`. CaddieJ does not create it. |
| `FLOOR_TOP_IMPACT_DELAY_SECONDS` | `1.5` | Wait after the detection. Longer than Shot Video's by default because the impact image is drawn after the swing. |
| `FLOOR_TOP_IMPACT_DURATION_SECONDS` | `4` | How long that source stays visible. |
| `OBS_TEST_SCENE` | `CaddieJ Test` | Scene used by the opt-in OBS integration tests. |
| `OBS_TEST_STATUS_SOURCE` | `CaddieJ Status` | Text source used by those tests. |

## Swing capture

Stills of named OBS panels of ProTee's post-shot presentation, read back
through OBS screenshots. Off unless switched on, and it needs OBS sources
CaddieJ does not create. See [Swing capture](swing-capture.md) and
[Coach V1](coach-v1.md). Prove the sources work with
`caddiej --check-swing-capture` before hitting a shot.

| Variable | Default | What it does |
| --- | --- | --- |
| `SWING_CAPTURE_ENABLED` | `false` | Whether a detected strike takes stills of the presentation at all. |
| `SWING_CAPTURE_PANELS` | `swing_behind,swing_side,impact,delivery` | Which panels to capture, in order. This list is authoritative: a panel left out is not captured and is not treated as missing. `swing` is an alias for `swing_behind`. `delivery` is optional. |
| `SWING_CAPTURE_SCENE` | `CaddieJ Capture` | Private OBS scene that keeps the coach sources and All Values showing, so OBS refreshes them, while nothing is drawn on the floor. Nested transparently in the floor scene and never put on Program. CaddieJ only reads it; it never shows or hides a source. See [swing capture](swing-capture.md). |
| `SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE` | *(blank)* | OBS source for the down-the-line body replay. Empty skips it. |
| `SWING_CAPTURE_PANEL_SWING_BEHIND_KIND` | `dynamic` | `dynamic` or `static`. |
| `SWING_CAPTURE_PANEL_SWING_BEHIND_SUPPORTS` | `body_through_impact,...` | Visual claims the behind view may support. |
| `SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE` | *(blank)* | OBS source for the face-on body replay. Empty skips it. |
| `SWING_CAPTURE_PANEL_SWING_SIDE_KIND` | `dynamic` | `dynamic` or `static`. |
| `SWING_CAPTURE_PANEL_SWING_SIDE_SUPPORTS` | `body_through_impact,...` | Visual claims the side view may support. |
| `SWING_CAPTURE_PANEL_SWING_SOURCE` | *(blank)* | Deprecated alias for the behind source. Used only when the behind source is empty. |
| `SWING_CAPTURE_PANEL_SWING_KIND` | `dynamic` | Kind used when the deprecated alias supplies the behind source. |
| `SWING_CAPTURE_PANEL_SWING_SUPPORTS` | `body_through_impact,...` | Claims used when the deprecated alias supplies the behind source. |
| `SWING_CAPTURE_PANEL_IMPACT_SOURCE` | *(blank)* | OBS source or scene for the impact graphic. |
| `SWING_CAPTURE_PANEL_IMPACT_KIND` | `static` | `dynamic` or `static`. |
| `SWING_CAPTURE_PANEL_IMPACT_SUPPORTS` | *(blank)* | Visual claims this panel may support. |
| `SWING_CAPTURE_PANEL_DELIVERY_SOURCE` | *(blank)* | OBS source or scene for the delivery graphic. |
| `SWING_CAPTURE_PANEL_DELIVERY_KIND` | `static` | `dynamic` or `static`. |
| `SWING_CAPTURE_PANEL_DELIVERY_SUPPORTS` | *(blank)* | Visual claims this panel may support. |
| `SWING_CAPTURE_DYNAMIC_DELAY_SECONDS` | `0.5` | How long after impact a dynamic burst starts. Also accepts `SWING_CAPTURE_INITIAL_DELAY_SECONDS`. |
| `SWING_CAPTURE_DYNAMIC_DURATION_SECONDS` | `4.5` | How long a dynamic burst runs. Long enough to reach the release in both replays; with the default delay it ends with the static pass. Also accepts `SWING_CAPTURE_DURATION_SECONDS`. |
| `SWING_CAPTURE_INITIAL_DELAY_SECONDS` | `0.5` | Alias for `SWING_CAPTURE_DYNAMIC_DELAY_SECONDS`. |
| `SWING_CAPTURE_DURATION_SECONDS` | `4.5` | Alias for `SWING_CAPTURE_DYNAMIC_DURATION_SECONDS`. |
| `SWING_CAPTURE_STATIC_DELAY_SECONDS` | `3.5` | How long after impact a static pass starts. |
| `SWING_CAPTURE_STATIC_DURATION_SECONDS` | `1.5` | How long a static pass runs. |
| `SWING_CAPTURE_STATIC_FPS` | `2.0` | How often a static panel is sampled. |
| `SWING_CAPTURE_FPS` | `10` | How often a dynamic screenshot is requested outside the downswing stretch. Each still is due at a fixed time from the burst's start, so this is the achieved rate on average; each still may be up to one Windows timer tick (15.6 ms) late. |
| `SWING_CAPTURE_DOWNSWING_FPS` | `20` | The body replays' rate inside the downswing stretch, from the top to the release. Extra stills are candidates only: `SWING_CAPTURE_SELECTED_FRAMES` and `COACHING_EVIDENCE_BUDGET` are unchanged. `0`, or anything not faster than `SWING_CAPTURE_FPS`, turns it off. A slow OBS skips stills rather than queueing them; `swing_capture_cadence` logs what each shot achieved. |
| `SWING_CAPTURE_DOWNSWING_DELAY_SECONDS` | `3.3` | When the downswing stretch starts, in seconds from the strike, like the burst. |
| `SWING_CAPTURE_DOWNSWING_DURATION_SECONDS` | `1.1` | How long it lasts. 3.3-4.4 s held the top to the release on every real capture measured, with a margin for tempo. Clipped to the dynamic burst. |
| `SWING_CAPTURE_OUTPUT_DIR` | `media/latest_swing` | The one production swing on disk. Replaced whole on the next shot. |
| `SWING_CAPTURE_SELECTED_FRAMES` | `16` | Total stills kept for one shot, across every panel. Body panels share what remains after one still is reserved for each static panel. A calibrated body camera takes at most one still per aimed moment: eight down the line, seven face-on. |
| `SWING_CAPTURE_DEBUG_KEEP_SHOTS` | `0` | Extra copies under `media/swing_debug`. `0` is production. |
| `SWING_CAPTURE_EVIDENCE_KEEP` | `3` | Shot directories kept under `media/swing_shots`, from 3 to 40. `3` is production. A diagnostic timeline request larger than this raises the keep to match, still capped at 40. |
| `SWING_CAPTURE_DIAGNOSTIC_TIMELINE_SHOTS` | `0` | Timing diagnostic. For the next N published shots of this run (`0`–`40`), every Swing Behind and Swing Side screenshot the burst already took is also written under the shot's `diagnostic/` directory and kept in full. `0` is production and does not raise shot retention. With `COACHING_VISION_CALLS=true` the same timeline is still written so the evidence pipeline can read it, and frames the package did not select are removed afterwards. The pose model is not downloaded. |
| `SWING_CAPTURE_IMAGE_WIDTH` | `0` | Scale each still to this width, aspect ratio kept. `0` means the source's own resolution; otherwise 8–4096. |
| `COACH_METRICS_CAPTURE_ENABLED` | `false` | Take one still of ProTee's All Values grid after each strike, for the coach to read the numbers from. Separate from `SWING_CAPTURE_SELECTED_FRAMES`. Needs swing capture on. |
| `COACH_METRICS_SOURCE` | `All Values` | OBS source showing the All Values panel. Captured at its own resolution, so the digits stay legible. |
| `COACH_METRICS_CAPTURE_DELAY_SECONDS` | `3.0` | Seconds after the strike to take the still, 0–15. One retry a second later if the still is unusable or identical to the previous shot's. |

## Session diagnostics

One report per shot, written after coaching. A failure to write a report does not change the cue or the spoken line. After each finished shot, `media/diagnostics/latest_shot.zip` is replaced with that shot. `caddiej --check-config` prints the path. A manual session zip is still available with `python -m caddiej --export-diagnostics`.

These settings keep different things:

- **Diagnostic metadata** is `report.json` and `report.md`: what was captured, which frames were selected, what the model returned, which cue was chosen, and how long it took.
- **Raw GPT retention** is `gpt_request.json` and `gpt_response.json`. It is controlled by `DIAGNOSTICS_RETAIN_PAYLOADS`. The files do not contain API keys or image bytes. The size cap is `DIAGNOSTICS_PAYLOAD_MAX_BYTES`.
- **Selected evidence images** are the frames chosen for that shot, copied into the diagnostic shot folder. Capture cleanup can delete the originals. `DIAGNOSTICS_KEEP_SHOTS` is how many of those copies are kept. Frames that were not selected are not copied.
- **Automatic ZIP export** writes `latest_shot.zip` for the newest finished shot only. `DIAGNOSTICS_INCLUDE_IMAGES` includes the copied frames. The zip does not contain other shots.

| Variable | Default | What it does |
| --- | --- | --- |
| `DIAGNOSTICS_ENABLED` | `true` | Write the shot report, the contact sheet, and the latest-shot zip. |
| `DIAGNOSTICS_DIR` | `media/diagnostics` | Where the session folder and `latest_shot.zip` are kept. |
| `DIAGNOSTICS_KEEP_SESSIONS` | `1` | How many session folders to retain. `1` keeps the session in progress and removes older session folders. |
| `DIAGNOSTICS_KEEP_SHOTS` | `3` | How many detailed shot folders to keep in the current session, including their copied images and payloads. |
| `DIAGNOSTICS_RETAIN_PAYLOADS` | `true` | Write the raw model request and response next to the report. Image bytes and API keys are omitted. |
| `DIAGNOSTICS_PAYLOAD_MAX_BYTES` | `1500000` | Largest payload file that is kept. A larger one is omitted and the report says so. |
| `DIAGNOSTICS_AUTO_EXPORT_LATEST` | `true` | Replace `latest_shot.zip` when a shot finishes. An older shot cannot replace a newer shot's zip. |
| `DIAGNOSTICS_INCLUDE_IMAGES` | `true` | Put the copied evidence frames inside `latest_shot.zip`. The shot folder still keeps them when this is off. |
