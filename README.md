# CaddieJ

An event-driven orchestration layer for a home golf simulator.

CaddieJ sits beside GSPro and turns a shot into everything that should happen
around it: a projection on the floor showing where the ball goes and what the
last one did, a clip of the swing, one thing worth fixing said out loud, and the
room reacting while it talks. It runs entirely on your own machine.

The real-time path is deterministic and holds no opinions. What appears on the
floor is arithmetic over what the launch monitor measured; a language model can
be asked to word the coaching more like a person, after the fact, and never gets
in the way of the next ball.

---

## Project status

**Alpha, and honest about it.** CaddieJ is complete enough to run an evening on
and is covered by 1,700+ tests that need no hardware — but almost nothing here
has been run against the real thing yet:

| Path | Developed against | Run against the real thing |
| --- | --- | --- |
| GSPro Open Connect interpose | a fake device and a fake GSPro | not yet |
| Passive ProTee log + GSPro currentRound.dat | unit/integration tests with temp files | files verified on hardware; CaddieJ observer not yet run in the bay |
| Swing video | FFmpeg's generated picture, and a fake FFmpeg | not yet |
| Coaching with a model | a stub SDK and a scripted provider | not yet |
| Speech | a fake voice and a recording sink | not yet |
| Lighting | a stub Hue bridge | not yet |
| Assistant API over MCP | CaddieJ's own client over a real socket | not yet |
| Floor overlay and history | browsers, and real SQLite | yes |

The fakes speak the same protocols as the real systems, so the gap is
integration risk rather than unwritten code. If you run any of these against real
hardware, an issue saying what happened would be the single most useful
contribution to the project right now.

[ROADMAP.md](ROADMAP.md) has the full picture of what is deliberately deferred.

---

## Quick start

Nothing needs to be plugged in. No account, no key, no network.

```shell
git clone <this repository> caddiej
cd caddiej
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -e ".[test]"
```

Play a scripted round against CaddieJ's own fake simulator:

```shell
GSPRO_MODE=simulate caddiej                    # macOS, Linux
$env:GSPRO_MODE="simulate"; caddiej            # Windows PowerShell
```

Open <http://127.0.0.1:8730/> and watch the floor: an address marker sized for
the club, the marker clearing when a ball is placed, then the club path, the
launch numbers, and a coached fault. Ctrl+C stops it.

Two other things worth trying immediately:

```shell
caddiej --check-config    # what would run, where files go, what is installed
FLOOR_DEMO=1 caddiej      # every state the projection can render, on a loop
```

CaddieJ writes `caddiej.sqlite3` and `media/` into the directory you start it
from. `HISTORY_DATABASE_PATH` and `VIDEO_MEDIA_ROOT` move them.

### Requirements

- **Python 3.12 or newer.**
- **A browser**, for the floor overlay. That is the whole hard requirement.
- OBS Studio with its WebSocket v5 server enabled, *if* OBS composites the
  overlay. Displaying the page fullscreen in a browser works just as well.
- FFmpeg on `PATH`, *if* swing video is enabled.
- A Philips Hue bridge, *if* lighting is enabled.

---

## What it does

Everything below the first two rows is off by default and CaddieJ runs the same
way without it. Nothing waits on an optional feature, and a missing one is
reported rather than fatal.

| | Needs | Default |
| --- | --- | --- |
| **[Floor overlay](docs/floor.md)** — ball marker, club path, launch numbers; path angles in metres, drawn in surface coordinates so nothing depends on the projector | a browser | on |
| **[Shot history](docs/history.md)** — every shot in one SQLite file you own, with per-club statistics | nothing | on |
| **[GSPro ingestion](docs/gspro.md)** — CaddieJ sits between the launch monitor and GSPro and forwards everything verbatim | GSPro | off |
| **[Coaching](docs/coaching.md)** — the one thing most worth fixing, from your own recent shots; optionally worded by a model | nothing, or `.[ai]` | on, offline |
| **[Swing video](docs/video.md)** — a clip of every swing cut around impact, never re-encoded | FFmpeg | off |
| **[Speech](docs/speech.md)** — the coached line said out loud by a model on your machine | `.[tts]` | off |
| **[Lighting](docs/lighting.md)** — the room dipping in the gaps between words | `.[hue]` | off |
| **[Celebrations](docs/celebrations.md)** — one reaction across floor, voice, lights and OBS when a hole finishes | nothing | on, inert |
| **[Assistant API](docs/assistant-api.md)** — an outside assistant asking about a session, over MCP or plain HTTP | nothing, or `.[mcp]` | off |

Three of those defaults are deliberate rather than cautious. Video films whoever
is standing on the mat, speech makes noise in your house, and lighting changes
something you set the way you wanted it — each should be a decision somebody
made rather than one they inherited. Frames of your swing are never sent to a
model unless you say so, and nothing is ever uploaded anywhere.

---

## Configuration

CaddieJ is configured through environment variables and a `.env` file in the
working directory. There are no command-line options for settings.

```shell
cp .env.example .env
```

[`.env.example`](.env.example) is the annotated version — every setting with the
reasoning behind its default. [docs/configuration.md](docs/configuration.md) is
the reference table. `.env` is git-ignored and is where credentials belong; none
are ever logged.

A setting CaddieJ cannot accept is named, explained, and refused before anything
starts:

```
CaddieJ could not start: 1 setting is invalid.

  COACHING_PROVIDER='gemini'
    Input should be 'none', 'openai' or 'scripted'
```

---

## Development

```shell
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -e ".[test]"
```

```shell
pytest                     # unit suite plus loopback socket tests; hardware tests self-skip
ruff check .
ruff format --check .
mypy
caddiej --version
caddiej --check-config
```

```shell
pytest tests/unit          # no hardware, no network, ~20 seconds
```

Loopback GSPro and HUD socket tests (`-m loopback`) bind a local port and run
in the default suite. They use CaddieJ's own fakes; they need no GSPro, OBS,
or camera. Hardware and credentialed tests are skipped unless explicitly
enabled:

```shell
CADDIEJ_RUN_INTEGRATION=1 pytest -m integration
```

Nothing in the unit suite needs GSPro, a launch monitor, OBS, a camera, a speech
model, a sound card, a Hue bridge or an API key. Every external system has a
fake that speaks the same protocol and can be made to fail, hang, or disappear
mid-round, which is how the failure paths are tested rather than hoped about.

[CONTRIBUTING.md](CONTRIBUTING.md) has the architectural rules a change has to
follow and a checklist for adding an adapter. Several of those rules are
enforced by tests rather than by review: the domain may not import an adapter,
GSPro's wire format and FFmpeg's arguments each live in exactly one file, the MCP
process cannot import the simulator, and no log field escapes an allowlist that
cannot contain a credential.

---

## Known limitations

- **Imperial units only.** Speeds are mph and distances are yards throughout,
  including on the floor. There is no metric display.
- **No floor calibration.** The tee is a physical point (`FLOOR_TEE_X_M`,
  `FLOOR_TEE_Y_M`) mapped through the same calibration as the club path;
  nothing yet measures where the mat actually is.
- **No scoring.** Open Connect carries shots and a club, not a scorecard, so
  celebrations are wired but nothing triggers them.
- **Windows software path is CI-tested; real GSPro/ProTee validation pending.**
  Default tests, lint, formatting, and type checking run on Windows, macOS, and
  Linux. The camera abstraction includes DirectShow (`dshow`). Nothing here has
  yet been run against a real GSPro machine or a ProTee launch monitor. The
  operator workflow for that first session is
  [docs/hardware-validation.md](docs/hardware-validation.md).
- **English only**, in both the coaching templates and the voice.

---

## Creating a review archive

To produce a clean tarball for code review — without local databases, media,
virtual environments, or secrets — run from the repository root:

```shell
./scripts/package-project.sh
```

This is macOS/Linux developer tooling, not a runtime dependency, and is not
expected to run on Windows. The script writes `dist/CaddieJ-review.txt` (a
plain-text bundle for ChatGPT upload) and `dist/CaddieJ-review.zip` (a
supplementary archive). It excludes `.git/`, `.venv/`, `.env`, runtime SQLite
files, `media/`, and other local-only artifacts, and it aborts if obvious
secrets appear to be present in files that would be packaged.

---

## Documentation

- [docs/](docs/README.md) — using CaddieJ, and every optional integration
- [docs/hardware-validation.md](docs/hardware-validation.md) — first real ProTee VX + GSPro session
- [docs/configuration.md](docs/configuration.md) — the full configuration reference
- [ARCHITECTURE.md](ARCHITECTURE.md) — design, boundaries, and the rules every
  component follows
- [ROADMAP.md](ROADMAP.md) — verifiable milestones and what is deferred
- [CONTRIBUTING.md](CONTRIBUTING.md) — setup, rules, and review expectations
- [SECURITY.md](SECURITY.md) — what CaddieJ exposes, and how to report a problem

## License

[MIT](LICENSE). FFmpeg, if you enable video, is run as a subprocess and never
bundled or linked, so whichever build you install stays under its own licence.
