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
| `FLOOR_TEE_X_M` | `0.5` | Default (non-driver) ball marker across the physical floor, metres from the far-left back corner. Must lie on `FLOOR_WIDTH_M`. Unknown club uses this too. |
| `FLOOR_TEE_Y_M` | `0.72` | Default (non-driver) ball marker towards the player, metres. Must lie on `FLOOR_DEPTH_M`. |
| `FLOOR_TEE_DRIVER_X_M` | *(inherits default tee)* | DRIVER marker X, metres. Unset inherits `FLOOR_TEE_X_M`. Must lie on `FLOOR_WIDTH_M`. |
| `FLOOR_TEE_DRIVER_Y_M` | *(inherits default tee)* | DRIVER marker Y, metres. Unset inherits `FLOOR_TEE_Y_M`. Must lie on `FLOOR_DEPTH_M`. |
| `FLOOR_TEE_X` | *(unset)* | Deprecated. A 0–1 fraction of `FLOOR_WIDTH_M`, converted to `FLOOR_TEE_X_M` only when that variable is unset. Ignored, with a warning, if both are set. |
| `FLOOR_TEE_Y` | *(unset)* | Deprecated. A 0–1 fraction of `FLOOR_DEPTH_M`, converted to `FLOOR_TEE_Y_M` only when that variable is unset. |
| `FLOOR_CLUB_PATH_LENGTH` | `1.2` | How far the club-path line is drawn, in metres of the physical floor. About a pace on a real bay — not a 0–1 fraction of the overlay. |
| `FLOOR_SHOT_DISPLAY_SECONDS` | `6.0` | How long an interpose/simulated shot's path and numbers stay up. |
| `FLOOR_SHOT_RESULT_MAX_DURATION` | `15` | Observer round-result panel: stay until READY / a new ball, or this many seconds, whichever happens first. |
| `FLOOR_DEMO` | `false` | Cycle the floor through every state it can render. Development only. |
| `FLOOR_DEMO_STEP_SECONDS` | `2.5` | How long each demo step lasts. |

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
| `VIDEO_EXTRACT_TIMEOUT` | `30.0` | How long one extraction may take. |
| `VIDEO_RESTART_LIMIT` | `5` | Consecutive failures before the camera is left alone. |
| `VIDEO_DRAIN_TIMEOUT` | `5.0` | How long shutdown waits for a clip in progress. |

## Coaching

On by default, and offline by default. See [Coaching](coaching.md).

| Variable | Default | What it does |
| --- | --- | --- |
| `COACHING_ENABLED` | `true` | Whether a fault is named at all. |
| `COACHING_PROVIDER` | `none` | `none` (measured faults only), `scripted` (answers from inside the process), or `openai`. |
| `COACHING_MODEL` | `gpt-5-mini` | Which model to ask. |
| `COACHING_API_KEY` | *(blank)* | **Credential.** Never logged, never committed. |
| `COACHING_API_BASE` | *(blank)* | Any endpoint implementing OpenAI's Responses API, including a local one. |
| `COACHING_SEND_FRAMES` | `false` | Whether frames of your swing go to the provider. A privacy decision. |
| `COACHING_FRAME_WIDTH` | `512` | How large those frames are, if sent. |
| `COACHING_DISPLAY_SECONDS` | `9.0` | How long a coaching line stays on the floor. |
| `COACHING_FOCUS` | *(blank)* | What you are working on: `face_control`, `path`, `strike`, `low_point`, `speed`, `launch`, `spin`, `consistency`, `tempo`, `posture`, `early_extension`, `sway`. |
| `COACHING_SEVERITY_FINE` | `0.25` | Below this severity, nothing is said. |
| `COACHING_BASELINE_WINDOW` | `40` | How many of your own recent shots per club define what is normal. |
| `COACHING_QUEUE_SIZE` | `2` | Shots still worth coaching. Deliberately short. |
| `COACHING_CLIP_WAIT` | `8.0` | How long to wait for the swing clip before coaching from numbers alone. |
| `COACHING_TIMEOUT` | `8.0` | How long one call to the provider gets. |
| `COACHING_BUDGET_SECONDS` | `20.0` | The whole budget from shot to line, retries included. |
| `COACHING_RETRY_LIMIT` | `1` | Retries after a failed call. |
| `COACHING_MIN_INTERVAL` | `8.0` | A floor on how often a call can be made. |
| `COACHING_CALL_LIMIT` | `60` | A ceiling on calls for one run. |
| `COACHING_MAX_OUTPUT_TOKENS` | `60` | A tight cap, so the cost of a shot is predictable. |

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
| `OBS_TEST_SCENE` | `CaddieJ Test` | Scene used by the opt-in OBS integration tests. |
| `OBS_TEST_STATUS_SOURCE` | `CaddieJ Status` | Text source used by those tests. |
