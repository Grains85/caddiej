# CaddieJ Roadmap

Milestones are deliberately small and **independently verifiable**: each one has
an explicit "done when" that can be checked without building the next milestone.
Each milestone should leave the repository working, tested, and shippable.

The constraints in [ARCHITECTURE.md](ARCHITECTURE.md) apply to every milestone.
Where a milestone builds a component that architecture document marks
**Intended**, that section is the specification.

## Sequencing principle

Foundation work comes before integrations. Several integrations — GSPro, voice,
cameras, AI — each independently require adapter lifecycle management, event
metadata, task supervision, and a domain model. Building any of them first would
push golf semantics into an adapter and encode assumptions that later have to be
unwound. The substrate is built once, then integrations become small.

---

## Current state

Complete:

- Project layout under `src/caddiej` with `python -m caddiej` and a `caddiej`
  console entry point.
- Typed configuration via `pydantic-settings`, with `OBS_PASSWORD` as
  `SecretStr`.
- Structured JSON logging.
- `AsyncEventBus`: in-process publish/subscribe with per-subscriber workers
  and bounded queues, event identity and timing stamped at publish,
  hierarchy-aware dispatch, and an explicit critical / coalesce / drop
  policy. A slow enrichment subscriber cannot delay PlayFlow or history.
  Critical shot events are refused and counted rather than silently
  dropped. Drain and stop stay bounded.
- OBS adapter over OBS WebSocket v5 with `simpleobsws` confined to one transport
  class: connect, authenticate, read version information, set text source
  contents, and set scene item visibility.
- A typed OBS error hierarchy that distinguishes transport failure,
  identification failure, authentication failure, and missing resources.
- Idempotent, opt-in provisioning of the floor Browser Source, with settings
  validated against what OBS reports it accepts.
- `runtime`: `TaskSupervisor` for long-lived tasks and loop-level SIGINT/SIGTERM
  handling with a bounded shutdown budget.
- `floor`: immutable, versioned render state with a single writer. A club path
  is a physical ray in metres — 0° down-range, clockwise positive — mapped onto
  calibrated surface coordinates afterwards. The default 1 m × 1 m rectangle
  is the identity, so a fresh checkout looks as it always has. A
  `HomographyCalibration` seam accepts four point correspondences; no wizard
  writes one yet. Marker variants `DRIVER`/`NORMAL`/`PUTTER`, show/hide, and a
  shot-metrics readout.
- `floor.demo`: an opt-in looping sequence through every renderable state
  (`FLOOR_DEMO`), so the projection can be reviewed without a launch monitor.
- `floor.presenter`: implements the domain's `FloorPort`, turning "the club in
  play is a putter" into a marker variant and a shot into a path and numbers.
- `adapters/hud`: loopback FastAPI server with a health endpoint, static file
  serving, and a WebSocket state feed that sends `hello` and a full snapshot on
  connect, coalescing superseded snapshots per client.
- A dependency-free browser overlay that renders only server state, reconnects
  with backoff and a silence watchdog, discards local state on reconnect,
  repaints when the output changes size, and reports its own errors back into
  CaddieJ's logs. Floor elements are SVG in a pixel viewBox; appearance and
  animation are CSS.
- `domain`: normalized shot, club, and player models in fixed named units, and a
  typed event vocabulary for shots, the launch monitor link, and the player.
- `domain.ports.FloorPort` and `domain.play.PlayFlow`: the first deterministic
  policy in CaddieJ. Club selection, ball detection, readiness, link loss, and
  shots decide what the floor shows, stated in golf terms with no knowledge of
  markers, coordinates, or pixels.
- `adapters/gspro`: an Open Connect interposer that accepts the launch monitor,
  forwards verbatim to GSPro before parsing anything, and publishes normalized
  events. Includes streaming JSON framing, boundary validation, split-shot
  assembly, per-connection duplicate suppression, tolerance of devices that reuse
  shot numbers, asymmetric ball-detection debounce, and club and handedness
  tracking. GSPro's wire format is confined to one module.
- `adapters/gspro/fakes`: a fake GSPro server and fake launch monitor covering
  every session scenario, so the integration is developed without hardware.
- `adapters/gspro/simulation`: a scripted round played by the fake device against
  the fake simulator through the real adapter (`GSPRO_MODE=simulate`), so the
  whole path from bytes to floor can be watched without GSPro or a swing.
- `runtime/backoff`: shared retry pacing with full jitter for reconnecting
  adapters.
- `persistence`: local shot history in one SQLite file, with `user_version`
  migrations, a repository holding all SQL, deterministic analytics as pure
  functions, SQLite confined to one thread, and a queued writer so nothing on the
  event path waits for a disk. Per-club and whole-session aggregates, and
  dispersion-ready samples. Opt-in raw payload capture for diagnosing a device.
- `video`: a rolling FFmpeg buffer of one camera on disk and a clip of every
  swing cut out of it by stream copy, with a sidecar stating where impact falls,
  drift-corrected mapping between FFmpeg's clock and CaddieJ's, atomic
  publication, a bounded clip queue off the event path, and camera failures that
  are reported and survived. A synthetic camera makes the whole path runnable
  with no hardware.
- `coaching`: one named fault per shot and a line for it on the floor, worked out
  by arithmetic over the shots already hit — per-club ideals, the player's own
  averages, handedness applied to both — with no model, no key and no network
  involved. Optionally a language model rewords that same fault, constrained to
  the faults CaddieJ measured and to CaddieJ's own display text, with strict
  validation, a fingerprint cache, five separate cost limits, and a fallback to
  the templated line on every failure. Outcomes, including failures, are stored
  against the shot.
- `adapters/openai`: the OpenAI SDK confined to one module and optional to
  install, behind a provider port with four neutral errors. A scripted provider
  makes the whole path runnable with no credentials.
- `speech`: the coached line said out loud, locally. One voice at a time by
  construction, a bounded priority queue in which a newer coaching line replaces
  an older waiting one, interruption of a quieter line by a louder one that
  leaves the device silent before the next starts, and rendered audio cached in
  memory by byte budget and on disk as WAV. `speech.started` and
  `speech.completed`, the second published even when nothing was said, with an
  outcome saying why.
- `adapters/kokoro` and `adapters/audio`: Kokoro and PortAudio each confined to
  one module and each optional to install, behind a `Synthesizer` and an
  `AudioSink` port. Both run their blocking work in threads. Fakes make the whole
  path runnable with no model, no speaker and no machine learning framework.
- `lighting`: the room reacting while CaddieJ talks, driven by a smoothed
  root-mean-square measurement of the samples about to be played rather than by a
  timer or a model. Brightness as a fraction of whatever the lights were already
  at, that baseline read once and held so no sentence can leave the room darker
  than the last, a command rate derived from the bridge's budget and the number
  of lamps, updates too small to see dropped, and the room given back on its own
  task so cancelling a reaction can never abandon a restore. It also takes a
  `Flourish` — a depth and a duration — from anybody who asks, and knows no golf
  terminology at all.
- `adapters/hue`: Hue CLIP v2 confined to one module and optional to install,
  behind a `LightingPort`, with one-time bridge pairing and the application key
  handled as a secret. A recording fake and a stub HTTP client make the whole
  path runnable with no bridge.
- `effects`: par, birdie, eagle, albatross and a hole in one, each one entry in
  one local table, sent to the floor, the voice, the lights and OBS at once by a
  director that decides everything by arithmetic and no model. One effect at a
  time, a higher priority replacing a lower one and an equal one refused, every
  duration clamped, every channel call timed out, and every output put back
  afterwards on its own task whatever happened during it. Nothing publishes
  `HoleCompleted` until scoring exists. The floor's celebration is rings and one
  word in CSS and SVG — no downloaded media anywhere.
- `assistant`, `adapters/api` and `mcp`: a versioned, authenticated, loopback
  surface an outside assistant can ask about a session, with five read tools and
  one mutation. The published models are hand written and hold nothing internal;
  every answer classifies absence as `ok`, `empty` or `unavailable`; the tool
  list is a literal, not reflection over the façades. Three gates in front of it
  — bearer token, no `Origin`, loopback `Host` — and no CORS anywhere. Reading
  and writing are separate switches, coaching prose is a third, and all three
  are off by default. The MCP server is a separate process importing two
  dependency-free CaddieJ modules, so an assistant runtime holds no database
  handle and no credentials. A failure to start is logged and stepped over.
- CI on ubuntu-latest, macos-latest, and windows-latest, Python 3.12 and 3.13,
  with ruff (lint and format) and mypy (`strict`) required. Windows software
  path is CI-tested; real GSPro/ProTee validation is still pending.
- Verified against real OBS 32.2.2 / OBS WebSocket 5.7.4.
- 1815 unit tests with no external dependencies, network, camera, AI account,
  speech model, sound card, lighting bridge or MCP SDK, plus 97 integration
  tests, 49 of them gated behind `CADDIEJ_RUN_INTEGRATION=1`, covering a real
  OBS instance, the HUD server over a real loopback socket, the GSPro adapter
  against the fake simulator over real sockets, a full simulated round from
  socket bytes to floor state and into a database, the video pipeline running
  real FFmpeg against a generated camera, a finished hole reaching every output
  at once, and an assistant tool call travelling over a real socket through the
  real token check into a real SQLite history and back.

Not started: a floor calibration *wizard*. Path angles are already computed
on a physical rectangle (`FLOOR_WIDTH_M` × `FLOOR_DEPTH_M`); the tee is still
placed by eye because nothing yet measures where the mat is, and a
perspective homography of the projected quad is a seam without an author. The GSPro integration has not run
against real GSPro or a real ProTee VX, swing video has not run against a real
camera, coaching has not run against a real OpenAI account, speech has not run
against a real Kokoro model or a real sound card, and lighting has not run
against a real Hue bridge — only against fakes, a scripted provider, and local
stubs speaking the same protocols. The assistant API has not been driven by a
real MCP client, only by CaddieJ's own client over a real socket.

Milestone 2 below is partly delivered: task supervision, signal handling, and the
bounded shutdown budget exist, and the OBS status sequence is no longer part of
startup. An `Application` object, validated-before-connect startup errors, and a
separate demo command remain.

Milestone 8 below is delivered for the elements it lists. Real shot data drives
the floor end to end. Celebrations, listed there as needing a separate channel
because last-write-wins state cannot carry a one-shot signal, turned out not to
need one: a celebration lasts seconds and Python controls both ends of it, so it
is a layer of floor state like any other. A momentary flash still would.

---

## Milestone 1 — Repository hygiene

Delivered except for the initial commit. The MIT licence and its metadata are
in place. CI runs the default test suite, lint, formatting, and type checking
on Python 3.12 and 3.13 across Linux, macOS, and Windows. `ruff` and `mypy`
(`strict`, `src/caddiej`) are configured in `pyproject.toml`. mypy was chosen
over Pyright: this tree is written with Protocol method stubs, which Pyright
treats as missing returns.

Remaining:

- Initial commit; confirm `.env` remains untracked and `.env.example` is
  committed.

**Done when:** a clean clone passes lint, type check, and the unit suite in CI,
and `git status` shows no untracked secrets. Windows software path is
CI-tested; real GSPro/ProTee validation is still pending.

## Milestone 2 — Runtime lifecycle

Replace the demo-shaped entry path with a real application lifecycle.

- A `runtime` layer with an `Application` object that owns startup and shutdown.
- A supervising `TaskGroup` for long-lived tasks; no task is unowned.
- SIGINT and SIGTERM handlers that set a shutdown event, so cleanup runs on the
  normal path rather than relying on cancellation.
- A bounded shutdown budget with ordered adapter teardown.
- Configuration validated before any connection attempt, reporting a clean
  startup error rather than a traceback.
- Move the current OBS status sequence out of `app.py` into an explicit demo
  command.

**Done when:** tests assert clean shutdown while an operation is in flight, both
signals shut down within the budget, and an invalid configuration produces a
readable startup error.

## Milestone 3 — Error taxonomy

- Introduce `CaddieJError` and `AdapterError`.
- Restructure OBS errors so *"unreachable"* and *"request rejected"* are
  siblings, not ancestor and descendant, so retry logic can distinguish them.
- Map OBS WebSocket status codes to named errors rather than one collapsed case.
- Remove catch-all wrappers that convert programming errors into adapter errors.

**Done when:** a caller can select unreachable-versus-rejected with a single
`except` clause, and tests cover both branches plus the code mapping.

## Milestone 4 — Event redesign (**Delivered**)

The previous bus — one unbounded queue, one worker, handlers awaited in
order — could not express the guarantees §2 and §4 require.

Delivered:

- Every event carries `event_id`, `occurred_at` (UTC), `monotonic_ns`, and
  `source`, stamped at publish. Call sites do not generate metadata. Clocks
  are injectable.
- Hierarchy-aware dispatch: a category subscription receives its subtypes,
  once.
- Persistent per-subscriber workers with bounded queues. A blocked
  enrichment handler does not delay a sibling. Shared state shares a
  subscriber name so ordering is preserved where it matters.
- Delivery policy lives in `domain.event_policy`, not in class-name
  inference. Critical facts refuse rather than vanish; state notifications
  coalesce; stale enrichment drops the oldest queued work. Refusals,
  drops, coalesces and handler failures are counted on health.
- `drain()` waits for every subscriber queue and for handler cascades.
  `stop()` stays bounded if a subscriber is stuck. TaskSupervisor still
  owns application tasks; the bus owns its workers.

Deliberately not built: event sourcing, a persisted event journal, Redis
or any other broker.

**Done when:** tests prove a subscriber that blocks does not delay another
subscriber, dropped and refused events are counted, a failing handler
still leaves siblings running, critical shots are never silently dropped,
and shutdown stays bounded. All hold.

## Milestone 5 — Adapter contract

- A shared adapter protocol: `start`, `stop`, `health`.
- Connect-with-backoff and reconnection provided once, reused by all adapters.
- Separate connect and request timeouts in configuration.
- OBS adapter migrated onto the contract, including reconnect after OBS restart.
- Nested per-adapter configuration models, with the OBS demo scene and source
  settings moved out of the core configuration namespace.

**Done when:** OBS is restarted underneath a running CaddieJ and the adapter
reconnects without a process restart, verified by an opt-in integration test.

## Milestone 6 — Domain core

CaddieJ starts owning state.

Begun: `Shot` and `Player` models exist, and `PlayFlow` is a small state machine
over the current club, ball, readiness and shot, with an injectable sleep so its
timing is tested without waiting. It imports no adapter and is driven entirely
through events.

Remaining:

- `Session`, `Round` and `Hole`, and the state machine that spans them.
- Deterministic, template-driven reactions to shot events.

**Done when:** a scripted sequence of synthetic shot events drives the full
domain state machine in unit tests, with no external system and no sleeping.

## Milestone 7 — Sessions and persistence (**Delivered**)

- SQLite in one local file, using the standard library. Sessions and shots, with
  the player, club, handedness, normalized ball and club data, provenance, an
  optional raw payload, and a table for referencing later analysis.
- Migrations as an ordered tuple plus `user_version`; a newer database is refused
  rather than downgraded.
- Repository holding all SQL, analytics as pure functions over records.
- Database access confined to one thread, and writes queued so nothing on the
  event path waits for a disk.
- Deterministic analytics: count, mean, median, min, max, sample deviation and
  spread, per club and across a session, plus dispersion-ready samples.
- `ShotCompleted` published by the domain once a shot has been attributed, which
  is what history subscribes to.

**Delivered:** a session is recorded, the process is restarted, and history is
queryable; a shot survives the round trip as the shot it was.

Deliberately not built:

- **The append-only event log.** The plan was for one, with history derived from
  it. Replaying link changes and heartbeats reconstructs a state nothing wants,
  and what people ask of a golf simulator is what they hit and how it varied. A
  shot table answers that directly; a log can be added later without changing it.
- **A `HistoryPort`.** Nothing in the domain asks history a question yet, so the
  port would have no caller. It arrives with the first one.
- **Rounds and holes.** A session is one run of CaddieJ, which is what it
  genuinely is today. Hole detection would be invention.

## Milestone 8 — Floor overlay and HUD feed

The visual architecture the project is aiming for.

Delivered as milestone 3A: the loopback server, the static overlay, the snapshot
feed, reconnect recovery, opt-in Browser Source provisioning, and documented OBS
setup. OBS text output is no longer part of startup. Snapshots are full rather
than delta-based, deliberately.

Delivered as milestone 3B: floor elements as a projection of physical geometry —
the ball marker with club variants and show/hide, the club path computed in
metres and mapped onto the surface in Python, and the shot-metrics readout —
plus the demo sequence that makes all of it reviewable on the real projector
without a launch monitor. Projector and OBS canvas size never enter that
calculation.

Delivered as milestone 3C: `FloorPort` in the domain and `FloorPresenter` over
`FloorService`, so floor state is now a projection of what the domain believes
rather than something `app.py` sets. `PlayFlow` drives it from real events.

Remaining:

- A delivery channel for one-shot signals. The state feed is last-write-wins,
  which is correct for state and wrong for "this happened once", so celebrations
  and transient flashes cannot be modelled as brief floor states.
- Reconciling a browser source that already exists in a different scene, which
  needs an add-existing-input-to-scene verb in the OBS adapter.
- Club placement zones, and floor elements that need more domain than exists:
  the analysing state, celebrations. Coaching text has since arrived and is
  rendered (milestone 12).
- A second surface (on-screen HUD) sharing the same feed.
- A calibration wizard that measures the projected quadrilateral and writes a
  `HomographyCalibration`. Path angles are already physical; what remains is
  measuring where the mat sits and how the projection keys the rectangle, so
  the tee no longer has to be placed by eye. CSS `matrix3d` is not that
  mapping.

**Done when:** the overlay renders live domain state in an OBS Browser Source,
and reloading the browser recovers full state from a fresh snapshot.

## Milestone 9 — GSPro and ProTee shot ingestion

Delivered against a fake local simulator (**Milestone 9A**):

- GSPro Open Connect adapter, built as an interposer between the launch monitor
  connector and GSPro, because Open Connect is a feed *into* GSPro with no
  observer interface. Traffic is forwarded verbatim, before it is parsed.
- Streaming JSON framing that survives split, concatenated, oversized, and junk
  input without losing the connection.
- Schema validation at the boundary; malformed messages rejected and logged,
  never partially applied, and always still forwarded.
- Normalized shot, club, and player models in fixed named units, plus typed
  domain events. GSPro's vocabulary is confined to one module and tests enforce
  it.
- Split-shot assembly, duplicate suppression scoped per connection, tolerance of
  devices that reuse shot numbers, asymmetric ball-detection debounce, and club
  and handedness changes from GSPro's `201`.
- Connection lifecycle: one device at a time, both sides closed together, and a
  cooldown before redialling a simulator that is not running. A connection ending
  mid-shot publishes the shot rather than losing it.
- A fake GSPro server and fake launch monitor covering connect, disconnect,
  reconnect, club change, ball detected and removed, complete, incomplete and
  duplicate shots, and malformed payloads. The default suite needs no GSPro.
- A scripted round through the real adapter (`GSPRO_MODE=simulate`), exercising
  every layer from socket bytes to floor state in one process.
- Shot events consumed in the domain: the club in play is held and correlated
  with shots, and ball detection and shot metrics drive the floor.

Remaining (**Milestone 9B**):

- Verify the ProTee VX connector can be pointed at a host and port other than
  GSPro's. This is a prerequisite for using the integration on real hardware; the
  interposer topology depends on it. The operator workflow is
  [docs/hardware-validation.md](docs/hardware-validation.md); it has not been
  run against real hardware.
- Plausibility bounds on measurements. Domain models reject the impossible;
  nothing yet rejects the merely absurd.

**Done when:** a real shot through ProTee VX and GSPro appears in domain state,
on the overlay, and in history — and a GSPro restart recovers automatically.

## Milestone 10 — Voice (**Delivered**)

- Kokoro behind a `Synthesizer` port with `bm_george` as the default voice, and
  playback behind a separate `AudioSink` port. Both are optional dependencies;
  neither is imported unless speech is switched on.
- A dedicated speech queue and worker: one voice at a time by construction, a
  bounded priority queue, and a newer coaching line replacing an older waiting
  one rather than being spoken two swings late.
- Interruption by cancellation, so a future high-priority event can take the
  room mid-sentence and the device is silent before the next line starts.
- Rendered audio cached in memory by byte budget and optionally on disk as WAV,
  because coaching repeats itself by design.
- `speech.started` and `speech.completed`, the latter published for every
  utterance including those that never made a sound, with an outcome saying why.
- Full degradation: no model, no weights, no sound card, a hung render or a dead
  device all mean silence, a counter and a log line, and nothing else.

**Delivered.** Speech is off by default (`SPEECH_ENABLED`), because it makes
noise in somebody's house and its first run downloads a speech model. Fakes make
the whole path — queue, priority, interruption, cache, events — testable without
a model, a speaker, or a machine learning framework installed.

**Not built:** the templated line is not spoken, only the coached one; there is
no streaming synthesis, no volume control (the system mixer owns that), and
nothing listens.

## Milestone 11 — Swing camera capture (**Delivered for one camera**)

Capture only; no analysis.

Delivered:

- A camera abstraction that is a description rather than a frame source, with
  macOS, Linux and Windows variants and a synthetic camera that needs no hardware.
- FFmpeg recording continuously into one-second segments in a subprocess, with
  the argument vectors confined to one module. Nothing decodes a frame in Python.
- A rolling buffer bounded by both age and size, indexed from FFmpeg's own
  segment list, discarded on restart because its clock restarts with it.
- Clips cut around impact by stream copy, published atomically, with a sidecar
  stating where impact falls, the requested and captured windows, and how much
  to trust the timing.
- Configurable pre- and post-impact windows, camera latency, and launch-monitor
  reporting lag, with drift-corrected mapping between FFmpeg's clock and
  CaddieJ's.
- `SwingClipReady`, `SwingClipFailed` and `CameraStateChanged`, and a bounded
  clip queue so nothing on the event path waits for FFmpeg.
- Camera and FFmpeg failures reported through events and `/healthz` and survived,
  including a missing FFmpeg entirely.
- A fake FFmpeg, so the whole subsystem is unit-tested without a binary, and an
  integration test running real FFmpeg against a generated camera.

Deliberately deferred:

- **A second camera.** Everything is named per camera and a second recorder is a
  second object; what is missing is deciding when a set of clips for one shot is
  complete, which is worth designing when there is a second camera to point.
- **Linking clips into history.** `shot_analyses` exists for it and is still
  unwritten. Coaching now records the clip a line was written about, so a
  coached shot can be traced to its footage; a clip of a shot nothing was said
  about is still only a file on disk.
- **Clip retention.** The buffer is bounded; the clip library is not. Deleting
  somebody's own swings needs a policy, not a default.
- **Audio**, and **trimming clips to the exact window**.

**Done when:** each shot yields a retrievable clip, and a camera or disk failure
leaves shot handling unaffected. Both hold; the clip is not yet associated with
its shot record in the database.

## Milestone 12 — AI swing analysis (**Delivered**)

Delivered:

- A deterministic coaching tier that needs no model at all: a fault table with
  per-club ideals, tolerances and directional labels, scored against both a
  fixed band and the player's own recent shots with that club, producing one
  named fault and a templated line. Offline, instant, and the same answer every
  time. This is what a fresh checkout runs as.
- Handedness applied throughout, including to the player's own averages, so a
  left-hander is never told the opposite of what they did.
- An `AdviceProvider` port with four neutral errors chosen for what to do about
  them rather than what they were, and an OpenAI adapter behind it as an
  optional dependency, with the SDK's own retries switched off so retrying stays
  a decision the service makes.
- Strict validation of everything a model returns: it may only name a fault
  CaddieJ measured, may not overrule a measured severity, and may not write the
  floor text — it chooses among CaddieJ's own lines by name. The sentence is
  checked for one instruction, no numbers, and nothing unrenderable. Every
  rejection falls back to the templated line.
- A short queue in which the newest shot displaces the oldest, and staleness
  checks at every step, because advice about three swings ago is worse than
  silence.
- Five independent cost limits — session ceiling, minimum interval, output cap,
  fingerprint cache, and frames off by default — plus a bounded timeout and
  retries only for failures a retry could fix.
- `ShotDiagnosed`, `AnalysisStarted`, `AnalysisCompleted` and `AnalysisFailed`,
  with the floor showing the deterministic line immediately and rewording it
  only if a model answers in time and about the right swing.
- Every outcome, including every failure, written to history against the shot
  with the model, prompt version, tokens, latency and clip path, so a session
  reviewed later still means something.
- A scripted provider that makes `COACHING_PROVIDER=scripted` a real demo mode,
  and a stand-in for the SDK so the adapter is tested — including with the SDK
  absent — without an account. No test in the suite can spend anything.

Deliberately deferred:

- **Speaking it.** The sentence is produced, validated as sayable, and stored.
  Milestone 10 says it.
- **Markerless club and body tracking.** The frame seam and the video-only
  focuses exist and are gated on frames having been sent; what is missing is the
  tracking that would make them measurable rather than a model's impression.
- **Cross-session baselines.** A baseline is this session's shots with this club.
  Comparing tonight to last month needs a decision about what counts as the same
  player.

**Done when:** analysis appears on the overlay after the fact, and with the
network blocked or the feature disabled, play and every other feature are
unchanged. Both hold: the deterministic line is on the floor before any call is
made, and every way the call can fail leaves it there.

## Milestone 13 — Hue lighting (**Delivered**)

Delivered:

- `lighting`: the room reacts while CaddieJ speaks, from a smoothed
  root-mean-square measurement of the rendered audio carried on `speech.started`.
  Brightness is always a fraction of the baseline the lights were already at, and
  that baseline is read once and held until the room is idle — so no sentence can
  leave the room darker than the last, and back-to-back lines do not flick back
  to full in between.
- Configurable modulation floor and ceiling, dipping rather than lifting by
  default because dimming has headroom at any baseline and brightening has none
  at full.
- The command rate is derived from the bridge's budget and the number of lamps
  rather than configured directly, each command asks for a transition covering
  the gap to the next so the bridge draws the curve, and any update too small to
  see is dropped.
- The room is given back on its own task, never inside a reaction, so cancelling
  one — an interrupted sentence, a cue taking over, shutdown — can never abandon a
  restore. Lamps that are off, and switches with no brightness, are never touched,
  and a modulation command never asserts `on`.
- A generic effect hook: one `Flourish`, a depth and a duration, played on
  request and outranking a sentence until it ends. The subsystem knows what a dip
  is and not what a birdie is; deciding a birdie is worth a particular dip
  belongs to Milestone 14, alongside the same decision for the floor and the
  voice.
- `adapters/hue`: Hue CLIP v2 confined to one module and optional to install,
  behind a `LightingPort`. One-time bridge pairing, the application key handled as
  a secret and absent from the log allowlist, lamps chosen by name or identifier
  and resolved once at start-up. Certificate verification is off against a
  bridge's self-signed certificate, stated as a bounded compromise rather than
  left implicit.
- Repeated failures put the bridge in a cooldown rather than retrying it once per
  syllable, and `restore_failures` is reported separately from `failures` because
  only one of them is visible from an armchair.
- Recording and broken fake lights plus a stub HTTP client, so every path
  including a failed restore is tested with no bridge and no client library.

Deliberately not built: colour, Hue Entertainment / DTLS, reactions to shots,
grouped lights, and runtime light discovery. See ARCHITECTURE §11.

**Done when:** lights react to shots, a burst of rapid changes collapses into
the final intent, and an unreachable bridge is logged and ignored. The middle and
last hold. The first was reconsidered: the lights react to the *voice* rather
than to shots, because a good drive should not dim the room, and the shot-shaped
reaction people actually want is a finished hole — which waits on scoring.

## Milestone 14 — Deterministic effects (**Delivered**)

Delivered:

- `domain.scoring`: `HoleResult` — par, birdie, eagle, albatross, hole in one —
  and a `HoleCompleted` event, in golf's own words with no brightness or font
  size anywhere near them. No bogey and nothing worse, so nothing can later be
  wired to react to somebody playing badly.
- `effects.catalogue`: one local table from result to effect, and the whole of
  the decision. A par is almost nothing on purpose, because a scale that spends
  itself on a par has nowhere to go by the time it reaches a hole in one.
- An effect is a cue, a label, a priority, an intensity, a duration and
  optionally a line to say — with no per-channel section, so each channel decides
  what an intensity means in its own terms and a fifth output is not an edit to
  every effect.
- `EffectDirector`: one effect at a time, a higher priority replacing whatever is
  running and an equal or lower one refused, no queue because an effect is a
  moment rather than a task, durations clamped so a typo cannot hold the room,
  and every call into a channel timed out so an output that stops answering
  cannot hold an effect open. The event path starts a task and returns.
- Every channel is reset at the end of every effect, including ones the effect
  never reached and ones whose start failed, on its own task and tagged with the
  generation it is undoing — so a reset in flight cannot clear the effect that
  replaced it. Replacement itself resets nothing, deliberately: channels take
  absolute values, so resetting between two effects would only produce a flicker.
- Four channels, each a translation and nothing more: a `FloorFlourish` layered
  over whatever the round already put on the floor, one line at announcement
  priority through the existing voice queue, one `Flourish` for the lights, and a
  scene item's visibility plus a text source's caption in OBS. OBS is the only
  connection CaddieJ keeps, opened on first use and dropped on failure, because a
  connect and an identify handshake inside a celebration would be inside the
  moment it marks.
- Placeholder visuals are expanding rings and one word, in CSS and SVG, paced by
  the duration on the wire and sized by the intensity. No images, no video, no
  fonts and no sound are downloaded; anybody wanting a real stinger points the
  OBS channel at their own source.
- Per-channel toggles, a duration cap and a channel timeout in configuration, an
  `effects` block in `/healthz` counting plays, refusals, replacements and resets
  with reset failures counted separately, and recording and broken fake channels
  so priority, cancellation, timeout and reset are all tested with no projector,
  no bridge, no speaker and no OBS.

Deliberately not built: a queue of effects, composition of two at once,
per-channel tuning in the catalogue, and reactions to bad shots. See ARCHITECTURE
§12.

**Done when:** each of the five results produces one reaction across every
enabled output, two arriving together produce exactly one, and every output is
demonstrably back to normal afterwards — including when a channel fails, hangs or
is cancelled half way. All hold. What is still missing is upstream: nothing
publishes `HoleCompleted`, because CaddieJ does not keep a scorecard yet.

## Milestone 15 — Assistant façade and MCP (**Delivered**)

- A loopback, authenticated façade exposing read-only state queries plus a
  narrow set of explicitly non-real-time commands. Five reads —
  `get_caddiej_status`, `get_last_shot`, `get_session_stats`,
  `get_current_club`, `get_recent_coaching` — and one mutation,
  `set_training_focus`.
- An MCP server wrapping that façade, in its own process, for any MCP client.
  No specific assistant is a dependency of anything, and plain HTTP is a
  first-class way in.
- The event bus is not exposed; internals stay refactorable. Nothing internal
  crosses the boundary at all: every published model is hand written.

See ARCHITECTURE §14 for the design and [docs/assistant-api.md](docs/assistant-api.md) for
the tools.

**Done when:** an assistant can query session state and issue permitted
commands, no exposed operation can influence shot handling, and running without
an assistant is unchanged. All hold. An end-to-end test drives the tools over a
real socket into a real database, and asserts the last of those three directly
by stopping the API mid-session and continuing to play.

What is deliberately not here: any tool that starts or stops anything, changes a
club, publishes an event, or reads a file path; shot-by-shot dispersion, which
exists for drawing pictures; and any surface an assistant could use to reach the
event bus. Further tools are cheap to add — a `ToolSpec`, a façade method, a
route, a handler — and should stay individually justified rather than arriving in
batches.

## Milestone 16 — Plugin seams

Deferred until here on purpose: with two or more real consumers of the ports,
the seams can be designed against evidence rather than speculation.

- Versioned event schemas.
- A documented, stable port surface.
- Discovery for out-of-core integrations, so adding one requires no core edit.

**Done when:** an example integration lives entirely outside `caddiej` core and
works without patching it.

---

## Explicitly not planned yet

Deferred deliberately, not overlooked:

- Redis, Docker, external databases, message brokers, or a DI container.
- FastAPI as an application framework. The HUD feed and the assistant API are
  small, loopback-bound surfaces whose routes hold no logic.
- A remotely reachable assistant API, an OpenAPI schema for it, CORS, or a
  browser client. Tunnel over SSH if the port is needed elsewhere.
- LLM-generated real-time speech or any LLM in the deterministic path.
- An interactive floor calibration wizard, or advanced animation before the
  state feed exists. Physical path geometry and the homography seam already
  exist; measuring the projected quad does not.
- Multi-canvas OBS abstractions, Hue Entertainment / DTLS effects, and lighting
  colour.
- Remote access, multi-user, cloud sync, or hosted components.
- A plugin framework before Milestone 16.
