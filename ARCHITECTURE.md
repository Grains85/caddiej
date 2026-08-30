# CaddieJ Architecture

This document describes the permanent architecture of CaddieJ: what it is, what
it deliberately is not, and the rules that every current and future component
must follow.

Sections are marked **Implemented** where they describe code that exists today,
and **Intended** where they describe a committed design that has not been built
yet. Intended sections are binding: they constrain how the feature may be built,
not merely a wish list. See [ROADMAP.md](ROADMAP.md) for sequencing.

This is the reasoning, not the manual. For running CaddieJ, see the
[README](README.md); for every setting, [docs/configuration.md](docs/configuration.md).

## Contents

1. [Purpose and non-goals](#1-purpose-and-non-goals)
2. [Deterministic core vs AI-assisted features](#2-deterministic-core-vs-ai-assisted-features)
3. [CaddieJ owns simulator state](#3-caddiej-owns-simulator-state)
4. [Asyncio event-driven architecture](#4-asyncio-event-driven-architecture)
5. [Adapter boundaries](#5-adapter-boundaries)
6. [OBS and browser overlay architecture](#6-obs-and-browser-overlay-architecture)
7. [GSPro and ProTee data path](#7-gspro-and-protee-data-path-implemented)
8. [Video pipeline](#8-video-pipeline-implemented-for-one-camera)
9. [Swing analysis and coaching](#9-swing-analysis-and-coaching-implemented)
10. [Kokoro voice architecture](#10-kokoro-voice-architecture-implemented)
11. [Lighting architecture](#11-lighting-architecture-implemented)
12. [Effects architecture](#12-effects-architecture-implemented)
13. [Persistence approach](#13-persistence-approach-implemented)
14. [MCP and assistant boundary](#14-mcp-and-assistant-boundary-implemented)
15. [Dependency policy](#15-dependency-policy-upstream-first-plugin-based-zero-core-patches)
16. [Failure isolation principles](#16-failure-isolation-principles)
17. [Security and secrets principles](#17-security-and-secrets-principles)
18. [Repository layout](#repository-layout)

---

## 1. Purpose and non-goals

CaddieJ is an intelligent orchestration layer for a physical golf simulator. It
sits between the simulator software, the room hardware, and the player, and it
decides what should happen when a shot is hit: what appears on the floor and
screen, what is spoken, how the lights react, and what is recorded.

CaddieJ is the component that *owns the round*. Everything else — the simulator,
OBS, cameras, lights, speech, assistants — is either a source of facts or a
destination for output.

### Goals

- Own the authoritative state of the session, round, and shot history.
- React to shots deterministically, immediately, and identically every time.
- Present state through a projected floor overlay and on-screen HUD.
- Enrich the experience with optional AI, voice, lighting, and video features
  that can fail without affecting play.
- Run on one machine, with no infrastructure to operate.

### Non-goals

- CaddieJ is **not** a launch monitor, ball tracker, or physics engine. It does
  not compute ball flight; it consumes shot data from the simulator.
- CaddieJ is **not** a replacement for GSPro or ProTee. It orchestrates them.
- CaddieJ is **not** a general-purpose streaming tool. OBS is used as a
  compositor and display surface, not as a product feature.
- CaddieJ is **not** a chat assistant. Conversational assistants integrate with
  CaddieJ from outside; they are never inside it.
- CaddieJ does **not** require Redis, Docker, Kubernetes, a message broker, or an
  external database. Adding any of these requires a documented reason that
  cannot be satisfied by the standard library plus SQLite.
- CaddieJ is **not** multi-tenant, cloud-hosted, or remotely accessible by
  default. All network surfaces bind to loopback unless explicitly configured
  otherwise.

---

## 2. Deterministic core vs AI-assisted features

This is the most important rule in the project.

**Real-time simulator behavior must never depend on a language model, a network
service, or any component with unbounded latency.**

CaddieJ is therefore split into two tiers with different guarantees:

### The deterministic core

Everything that must happen in response to a shot. Shot ingestion, state
updates, HUD content, scoring, and session recording.

Guarantees:

- No LLM calls, ever.
- No dependency on internet connectivity.
- Bounded, predictable latency.
- Same input produces the same output.
- Text shown or spoken in real time comes from **deterministic templates**, not
  generated prose.

### AI-assisted and enrichment features

Swing analysis, generated commentary, natural-language interaction, ambient
lighting, and video processing.

Guarantees:

- Strictly reactive: they observe events after the fact.
- Freely cancellable, skippable, and degradable.
- May be slow, may fail, may be offline.
- **Nothing in the deterministic core may await them.**

The practical test for any new feature: *if this component hangs for thirty
seconds, can the player still hit the next shot and see correct information?* If
the answer is no, the feature is in the wrong tier.

A specific consequence for voice, and worth stating precisely because §10 looks
at first glance like an exception to it. Anything that has to be *shown or said
as the ball lands* comes from a template. The spoken coaching line is not that:
it arrives seconds after the shot, it is dropped rather than said late if another
ball has been hit, and it is cached the moment it exists. So model-authored
phrasing reaches the speaker, but only through the non-real-time, cached path
this rule already permits — and the floor, which is real time, is never waiting
on it.

Coaching (§9) is the worked example of this split, and the shape it took is
worth copying. It is not "a feature that uses AI"; it is a deterministic feature
that a model is allowed to reword. Naming the fault is arithmetic and lives in
the core, so the floor is correct before anything can hang. The model receives
that verdict and returns two strings, one of which must be chosen from a fixed
list. Nothing reads its answer as anything other than text to display, so the
thirty-second test is passed by construction rather than by care.

---

## 3. CaddieJ owns simulator state

CaddieJ holds the authoritative model of what is happening in the room. No
adapter, overlay, or external assistant is a source of truth.

The intended domain model (**Intended**) owns:

- `Session` — a period of play, its players, and its settings.
- `Round` / `Hole` — course context as reported by the simulator.
- `Shot` — a single shot with its ball and club data, timestamped on ingestion.
- Derived state — current player, shot count, session statistics.

Rules:

- Adapters translate; they do not interpret. An adapter converts an external
  message into a CaddieJ event and stops there.
- **Golf semantics never live in an adapter.** If a change requires knowing what
  a shot means, it belongs in the domain.
- The domain never imports an adapter. Dependencies point inward.
- All output surfaces are *projections*, not independent state. The HUD and the
  voice both render the authoritative model; the lights render the voice, which
  is one step further out and still holds nothing of its own.
- State transitions are driven by in-process domain events. That is
  orchestration, not event sourcing. CaddieJ does not record a general event
  log and does not reconstruct state by replaying one. What is persisted is
  shot and session history (§13). A future diagnostic journal, if one is
  added, is optional and separate from that history.

Because events are the in-process vocabulary of what happened, every event
carries identity and timestamps from the moment it is published (see §4).

---

## 4. Asyncio event-driven architecture

CaddieJ is a single-process asyncio application. Concurrency comes from the event
loop; parallelism, where needed, comes from explicit process pools.

### Event bus

**Implemented:** `caddiej.core.event_bus.AsyncEventBus` is an in-process
publish/subscribe bus that structurally enforces the tiers in §2.

- **Publish fans out and returns.** Each named subscriber has its own
  persistent worker and a bounded queue. A handler that hangs in coaching,
  video, or speech cannot delay PlayFlow or history. Handlers that share
  mutable state share a subscriber name so they stay on one worker and keep
  their order. There is no unbounded `create_task` per event.
- **Metadata is stamped at publish.** Every `Event` carries `event_id` (a
  UUID), `occurred_at` (UTC wall clock), `monotonic_ns` (`time.monotonic_ns`
  in this process, for latency), and `source`. Publishers do not generate
  these; clocks are injectable in tests. This is identity for in-process
  orchestration and debugging, not a promise that events are persisted or
  replayed.
- **Hierarchy dispatch.** Subscribing to a category delivers its subtypes.
  The same handler registered on both a category and a leaf is delivered
  once.
- **Delivery policy is a table, not a guess.** `caddiej.domain.event_policy`
  names the policy for each event class and any subscriber override.
  `CRITICAL` facts — `ShotIngested`, `ShotCompleted`, and the other recorded
  truths — are never dropped. If a critical subscriber cannot accept an
  event, `publish` waits briefly (`critical_timeout`, 50ms) and then refuses:
  the refusal is counted, logged, marked on health as `overrun`, and raised
  as `SubscriberOverrun`. The event loop is not stalled on a full queue.
  `COALESCE` keeps the newest queued event of that type (state
  notifications). `DROP` evicts the oldest queued item so enrichment can
  stay on the latest work. Anything unlisted is critical, so a new event
  type cannot silently become droppable. Enrichment subscribers override
  shot facts to `DROP`; history and play do not.
- **Handler exceptions** are logged, counted, and isolated. A broken
  enrichment subscriber does not prevent siblings receiving the same event.
  Worker tasks are owned by the bus; they are not supervised as
  process-fatal. `TaskSupervisor` still owns the application's long-lived
  tasks (HUD, ingestion, demo).
- **`drain()`** waits until every subscriber queue is empty and no handler
  is in flight, including events a handler publishes. Independent queues
  mean idle is global outstanding work, not a single `Queue.join()`. A
  stuck subscriber cannot make drain infinite; it returns False on timeout.
- **`stop()`** closes every subscriber queue, finishes what is already
  waiting, and cancels any worker that will not return, bounded by the
  same timeout used everywhere else in shutdown. Publish after stop raises.
  A handler that publishes during stop does not raise; the announcement is
  not dispatched, which is why `caddiej.app` drains before it closes
  anything.
- **Health** reports per-subscriber queue depth, dropped / coalesced /
  refused / handler-failure counts, overrun, and oldest queued age.
  Event UUIDs are not metric keys.

### Lifecycle and supervision

**Implemented:** `caddiej.runtime` owns the process.

- `TaskSupervisor` holds every long-lived task. **A supervised task that raises
  is a failure of the process, not of the task.** Nobody awaits these tasks, so
  the supervisor retrieves the exception, reports it, requests shutdown through
  the same event a signal uses, and keeps the error so it reaches the exit code.
  Without this, a dead subsystem would leave CaddieJ running as a zombie that
  still claims to be online.
- `shutdown_signals` installs SIGINT and SIGTERM handlers on the running loop and
  sets an event, so shutdown runs on the normal code path rather than depending
  on cancellation semantics. Handlers are removed when the context exits.
  Installing them takes `KeyboardInterrupt` away from Ctrl+C, so **the second
  signal is a force quit**: without it a wedged shutdown could only be escaped
  with `kill -9` from another window.
- Shutdown has a bounded budget at two levels. Each component is asked to stop
  and given time to drain, then cancelled — `SHUTDOWN_TIMEOUT` configures that.
  The sequence as a whole is capped by `SHUTDOWN_GRACE`, because the sum of the
  per-step budgets exceeds the grace an operating system allows between "please
  stop" and SIGKILL, and without a total cap the last steps are the ones that
  never run on a machine that is shutting down.
- A library that installs its own signal handlers is not allowed to take the
  process over. uvicorn does this inside `serve()`, so the HUD adapter overrides
  `capture_signals` to leave signals to the runtime.

`caddiej.app.run` wires configuration, the event bus, the floor service, and the
HUD server, then tears them down in a `finally`. **Ingestion is stopped first,
then the bus is drained, and only then is anything closed.** Every subsystem
below ingestion handles shots, so a ball struck while they were closing would be
ingested normally and handed to services that had already finished — recorded by
history, refused by video, and given a coaching failure it did not earn. The
subsystems themselves close in reverse dependency order.
`caddiej.__main__` configures structured JSON logging, runs the coroutine, and
translates interruption into an exit code.

The log formatter uses an allowlist of context fields. It fails closed: a field
that is not listed does not reach the log, so a credential cannot be leaked into
logs by passing it through `extra`.

**Intended:** adapters gain a shared `start`/`stop`/`health` contract and
register themselves with the supervisor, rather than the application knowing how
each one is started.

---

## 5. Adapter boundaries

Every external system lives behind an adapter. This is the rule that keeps
CaddieJ testable and keeps third-party APIs from spreading through the codebase.

### Rules

1. **One adapter per external system**, under `caddiej/adapters/<system>/`.
2. **Third-party client libraries are confined to a transport class inside that
   adapter.** No other module may import them. Today `simpleobsws` appears in
   exactly one file, `adapters/obs/client.py`, and only inside
   `_SimpleObsWsTransport`.
3. **Vendor protocol details never escape.** OBS request names such as
   `SetInputSettings` and `GetSceneItemId` exist only inside the transport.
   Callers see CaddieJ verbs like `set_text` and `set_scene_item_visibility`.
4. **The transport is injectable.** Adapters accept a transport factory so tests
   run without the external system. `ObsClient` takes `transport_factory` for
   exactly this reason.
5. **Adapters raise CaddieJ errors**, never vendor exceptions (see §15).
6. **Adapters are replaceable.** Swapping `simpleobsws` for another OBS client
   must touch one class and no tests of CaddieJ behavior.

### Adapter lifecycle (**Intended**)

All adapters share one contract — `start`, `stop`, `health` — plus
connect-with-backoff and reconnection. GSPro, Hue, cameras, and speech each need
this; without a shared contract each would implement it differently. Connection
timeouts and per-request timeouts are configured separately, because a generous
connect budget must not become a stall in a real-time path.

### Ports (**Partly implemented**)

The domain does not call adapters directly. It speaks to ports — and the port is
stated in the domain's own vocabulary, not the implementer's.

`FloorPort` is implemented. It offers four things: show the address for a club,
hide it, show a shot, clear it. It mentions no marker variants, no coordinates,
and no colours, because those are decisions about how to draw a club rather than
facts about golf. `FloorPresenter`, in the floor package, implements it by writing
floor state. That split is the point: the rule "a driver is played from further
forward, so it gets a different marker" is presentation, while "the club in play
decides the address" is domain.

`HistoryPort` is intended and follows the same shape. There is deliberately no
`LaunchMonitorPort`: shots arrive as events, and the domain does not ask a device
for anything.

Neither speech nor lighting became a domain port, and for the same reason. The
domain has nothing to ask of a speaker: it produces an `Utterance` — text, a
priority, and whether it goes stale — and something downstream decides whether
that deserves the room. It has even less to ask of a lamp; lighting is a
**reactor**, which observes `SpeechStarted` and decides for itself. So
`Synthesizer`, `AudioSink` and `LightingPort` belong to their own subsystems
rather than to the domain, exactly as `AdviceProvider` belongs to coaching. The
distinction is worth keeping: a domain port exists so the domain can *act*, and a
subsystem port exists so a vendor can be *replaced*.

---

## 6. OBS and browser overlay architecture

OBS Studio is CaddieJ's compositor and display surface. CaddieJ controls it over
OBS WebSocket v5.

**Implemented:** the OBS adapter connects and authenticates, verifies the
connection by reading OBS and WebSocket versions, sets the contents of a text
source, and shows or hides a scene item. Scene items are addressed by resolving
the numeric scene item id from the scene and source names before changing
visibility, because OBS visibility operates on ids rather than names.

Direct manipulation of OBS text sources is a bootstrap and debug mechanism,
**not** the HUD architecture. The floor projection and on-screen HUD are
browser-based.

### The HUD adapter (**Implemented**)

`adapters/hud` serves the overlay and its state feed. FastAPI and uvicorn are
confined to this adapter exactly as `simpleobsws` is confined to the OBS one: no
framework type appears outside it, and routes contain no logic — they read
`FloorService` and hand work to the connection registry.

- `GET /` serves the overlay page, `/static` its assets, and `GET /healthz`
  reports the served state version and connected client count.
- `WS /ws` is the state feed. On connect the server sends `hello`, then a full
  snapshot, unprompted. Clients never need to ask for state.
- The server binds to loopback by default (`HUD_HOST`, `HUD_PORT`).
- **The feed is same-origin only.** WebSockets are exempt from the same-origin
  policy, so a handshake whose `Origin` does not match the `Host` it connected to
  is rejected before it is accepted; otherwise any page in any local browser
  could read the feed and write to CaddieJ's logs. A missing `Origin` means a
  non-browser client and is allowed.
- Client-supplied text, such as `client_log`, is untrusted and truncated.
- Frame types are not assumed: a non-text frame is answered with an error rather
  than being allowed to fail the connection.

### State ownership across the boundary (**Implemented**)

`caddiej.floor` owns the authoritative render state, and has no web dependency:
the adapter transports state, it does not define it.

- `FloorState` is an immutable snapshot. Its `version` doubles as the feed
  sequence number.
- `FloorService` is the single writer. Listeners are notified synchronously and
  must not await, so a slow or dead client cannot delay a state change. Writes
  must happen on the event loop thread; work in a thread or executor has to hand
  the call back with `loop.call_soon_threadsafe`.
- A write that changes nothing is not a new version and is not broadcast.
- **Snapshots are always complete.** There is no delta protocol: the state is
  small, and full snapshots remove an entire class of divergence bug. Deltas will
  be introduced only if a measurement shows they are needed.
- The client may send `hello`, `resync`, `ping`, and `client_log`. **No client
  message can write state.** Anything else is answered with an error, and unknown
  fields are ignored so a version mismatch cannot break the server.
- Each connection holds one outbound slot with the newest state only. A
  superseded snapshot is dropped rather than queued, so a slow client falls
  behind in freshness but never in correctness, and cannot grow a backlog.

### Floor coordinates and geometry (**Implemented**)

A club-path angle is a direction on the hitting bay. The overlay is a unit
square. Those are different spaces, and they are kept apart:

1. **Shot geometry** — the launch-monitor number, in degrees.
2. **Physical floor plane** — metres. Origin at the far-left back corner; +X
   towards the player's right; +Y towards the player; down-range is −Y. 0° is
   straight down-range and increases clockwise seen from above. Length is
   metres of display line, not carry and not a fraction of the surface.
3. **Calibrated surface** — dimensionless 0..1. The projected rectangle, with
   (0, 0) at that same back-left corner. This is what `FloorState` stores and
   what the wire carries as points.
4. **Display pixels** — `x * width_px`, `y * height_px`. Owned by the browser.
   Projector resolution and OBS canvas size never enter Python.

**Python maps physical points onto the surface. The browser maps surface
points onto pixels.** That is the source of truth. A CSS `matrix3d` on
`#surface` is not calibration: it would run after the endpoints already exist,
and it cannot repair a ray that was derived in the unit square. On a 4 m × 3 m
floor, equal steps in x and y on the surface are 0.4 m and 0.3 m; a "45°"
computed there is atan(4/3) ≈ 53°. The physical endpoint is computed first
and then mapped.

The default calibration is an axis-aligned scale (`x = x_m / width_m`,
`y = y_m / depth_m`). A 1 m × 1 m floor makes the two spaces numerically
identical, so a fresh checkout — and any existing marker the operator placed
by eye — looks the way it always has. A later wizard that measures the actual
projected quadrilateral produces a `HomographyCalibration` from four physical
corners onto the surface quad they occupy; Python applies that to points.
Nothing about that wizard exists yet except the mapping it would write.

No part of golf geometry knows the projector's resolution. A client stretches
the unit square over whatever surface it has, which is why resizing the
output, changing OBS canvas size, or moving to a different projector needs no
change in Python.

Values outside 0..1 mean "off the surface" and are legal for a club path, which
may run past an edge and be clipped. A ball marker is constrained to the surface,
because a placement the player cannot reach is a bug, usually pixels passed where
a fraction belongs. Coordinates must be finite: `NaN` and `Infinity` serialise to
tokens that are not valid JSON, so they would reach the browser as unparseable
text rather than as a visible error.

**Geometry is computed in Python, not in the renderer.** A club path is authored
the way it is measured — a physical start, an angle, and a length in metres —
and the state carries the resulting surface endpoints. The overlay draws a line
between two given points and does no trigonometry, because a renderer that
derives geometry is a renderer that can disagree with Python. The wire format
carries the authored angle and length alongside the endpoints, so a snapshot is
readable on its own; a projector has no console anyone will read.

The current elements are a ball-placement marker with a club variant
(`DRIVER`, `NORMAL`, `PUTTER`), a straight club path, and a shot-metrics
readout. Absence is modelled two different ways, deliberately: metrics and the
club path are `None` until a shot produces them, while the marker always exists
and carries a visibility flag, because it has a position worth keeping across a
hide — hiding it is what happens when the ball is detected, and the overlay
animates out from where the ball actually was.

A metric the device never measured is `None` and is left off the floor. Most
launch monitors do not measure carry — the simulator computes the flight — and
showing a zero would put a number in front of a player that nothing measured.

### What drives the floor during play (**Implemented**)

`domain.play.PlayFlow` is the first part of CaddieJ that holds an opinion about
golf. It subscribes to the facts the room reports and asks `FloorPort` for what a
player should be looking at:

- the club in play decides which address marker is shown;
- a ball on the mat hides the marker, because the projector would otherwise be
  shining onto the ball about to be hit;
- a shot replaces the address with the club's path and the launch numbers, for a
  few seconds;
- once that has been read, the floor returns to showing where the next ball goes.

Two rules are there because of what devices actually do. A device reporting
nothing about readiness is not a device that is *not* ready, so only an explicit
"not ready" suppresses the marker. And when a connection ends, everything the
device said about the mat dies with it: keeping a stale "ball present" would
leave the marker hidden for the rest of the evening with nothing left to clear it.

The club a shot was hit with is attached here, not at the boundary. No launch
monitor names the club in its shot message, so correlating the two is knowledge
only the domain has.

Having attributed a shot, the flow publishes `ShotCompleted`. That is the seam
for anything that cares what a shot *means* rather than how it looks: history,
video, coaching, and through coaching the voice. The difference from
`ShotIngested` is the
club, and it matters more than it sounds — an ingested shot is anonymous. The
floor is written first and the announcement follows, because a player is standing
in front of the projector waiting for a number and a subscriber is not.

### Running the floor without a launch monitor (**Implemented**)

Two development modes, for two different questions.

`floor.demo` (`FLOOR_DEMO`) walks the floor through every state it can render, on
a loop. It answers "does the putter marker read at three metres?" — the only way
to judge a projection is to stand next to it, and waiting for hardware to make
that possible would mean building the renderer blind. It is a writer like any
other: it calls `FloorService` and owns no state.

`adapters/gspro/simulation` (`GSPRO_MODE=simulate`) plays a scripted round —
driver, iron, wedge, putt — with a fake device against a fake GSPro inside the
process. It answers "does a round look right?". Crucially it is a simulated
*device*, not a simulated pipeline: the bytes go through the real framing,
parsing, assembly, translation, event bus and domain, so the parts most likely to
be wrong are the parts that run. A mode that called `FloorService` directly would
look identical on the projector and prove nothing.

Both run as supervised tasks and are cancelled with everything else on shutdown.
They are mutually exclusive, because both write the floor.

**The feed carries state, not events, and this is a hard boundary.** Last-write-
wins is the correct guarantee for "what should the floor look like now" and the
wrong one for "this happened once": a client that is briefly behind will never
observe a transient value. So a one-shot signal — a celebration, a brief
analysing flash, anything a client must see exactly once — may not be modelled as
a transient floor state. It needs a separate channel with its own delivery
guarantee, to be designed when the first such element is built (see
[ROADMAP.md](ROADMAP.md) milestone 8).

Server-side liveness comes from the WebSocket keepalive ping, which CaddieJ
configures explicitly rather than inheriting: it is the only thing that reaps a
peer that stopped responding without closing, such as a suspended Browser Source.

### Reconnection (**Implemented**)

Reload, suspension, and disconnection are normal: OBS can refresh or suspend a
Browser Source at any time.

- The client reconnects with exponential backoff and jitter, and treats silence
  as failure rather than trusting `readyState`, because a suspended page can hold
  a socket that looks open.
- On every reconnect the client **discards local state** and waits for a fresh
  snapshot. Nothing is merged. It renders nothing until the server has spoken.
- The overlay renders only what the current snapshot contains and derives
  nothing, so there is no state in the browser to diverge. It keeps the last
  snapshot solely to repaint when the output changes size, and drops it on
  disconnect, so a reconnect still renders nothing until the server has spoken.
- Browser-side failures are reported back over the feed into CaddieJ's structured
  logs, because a projector has no console anyone will read.

### OBS Browser Source provisioning (**Implemented**)

`ObsClient.ensure_browser_source` creates or reconciles the source. It is
idempotent and **opt-in** (`OBS_PROVISION_FLOOR_SOURCE`), because it modifies the
user's active scene collection.

Provisioning is **best effort**. OBS only hosts the overlay, so an OBS that is
missing, unreachable, or unhappy is reported and abandoned; it never stops the
floor coming online. This is §16 applied to the first optional integration: if a
convenience can take down the deterministic path, the boundary is in the wrong
place. The URL handed to OBS is built from the port the server actually bound,
not from configuration, so it stays correct when the configured port is 0.

- Only the URL, size, and lifecycle toggles are managed, so a user's own changes
  to other settings survive.
- The `shutdown` and `restart_when_active` toggles are forced off: the overlay
  must stay loaded and keep its WebSocket rather than being suspended or
  refreshed when scenes change.
- Settings keys are validated against `GetInputDefaultSettings` before being
  written, because OBS silently ignores unknown keys, which would look like a
  CaddieJ bug after an OBS release renamed one.

### Consequences of this design

- The overlay is a projection of state, not a sequence of imperative commands.
- Visual work — layout, animation, SVG — happens in HTML/CSS/JS, where it
  belongs, not in Python drawing calls.
- The frontend has no dependencies, no build step, and no framework. It is
  ES modules, CSS, and the DOM.
- One page per surface (floor projection, on-screen HUD) sharing one state feed,
  rather than many per-element OBS sources.
- OBS scene and source names remain configuration, never hardcoded.
- **OBS is optional.** The overlay is served independently; OBS only hosts it.
  Displaying the page fullscreen in a browser is a supported output path, which
  is what makes running without OBS later a configuration change rather than a
  rewrite.
- An embedded HTTP server is a library dependency, not infrastructure, and does
  not conflict with the non-goals in §1. It binds to loopback.

### SVG and CSS division of labour (**Implemented**)

Floor elements are SVG inside a single `<svg>` whose `viewBox` is the surface in
pixels, set by the client and updated on resize. A pixel viewBox rather than a
0..1 one is what keeps a circle circular on a surface that is not square.

Python sends surface positions; CSS owns appearance. Marker colour and the pulse
are CSS keyed off `data-variant` and `data-visible` attributes, sizes come from
the client because only it knows the surface in pixels, and animation is CSS
transitions rather than anything Python drives frame by frame. Diagnostics such
as the link indicator live outside `#surface`, so a later visual keystone will
never tilt them.

**Intended:** a calibration wizard that measures the projected quadrilateral
and writes a `HomographyCalibration` — four physical corners onto the surface
quad they occupy — which Python already knows how to apply to points. A
`HudPort` so the domain does not know a browser exists, and per-surface pages
driven by the same feed. CSS `matrix3d` on `#surface` is *not* the owner of
that mapping: path endpoints must already be correct before anything is drawn.

---

## 7. GSPro and ProTee data path (**Implemented**)

ProTee VX is the launch monitor; GSPro is the simulator. They talk over GSPro's
Open Connect interface: a long-lived TCP connection carrying JSON.

The adapter described here exists and is tested end to end against a fake
simulator. Two things remain **Intended**: it has not yet run against real GSPro
or a real ProTee VX, and nothing consumes the shot events yet.

### Why CaddieJ sits in the middle

Open Connect runs in the opposite direction to what its name suggests. **GSPro is
the server**, listening on port 921. The launch monitor is the client: it
connects and pushes shots. GSPro replies only to acknowledge a shot (`200`),
report player information (`201`), or report a failure (`5xx`).

There is no observer interface and no broadcast. So the only way for CaddieJ to
see a shot is to be in the conversation: it accepts the launch monitor's
connection, dials GSPro itself, and forwards traffic verbatim in both directions.

```
ProTee VX  --(Open Connect)-->  CaddieJ  --(Open Connect, verbatim)-->  GSPro
   ^                               |                                      |
   +-------------------------------+--------------------------------------+
                            replies forwarded back

                                   | observed copy (never the original)
                                   v
                       framing -> validation -> translation
                                   |
                              domain events
                                   v
                     domain (authoritative state)
                                   |
          +-------------+----------+-----------+--------------+
          v             v                      v              v
      HUD feed      history            speech (async)   lights (async)
```

This puts CaddieJ in the path of live play, which is the single most important
constraint on the design. It follows three rules:

- **Bytes are forwarded before they are understood.** Parsing, validating, and
  publishing all happen on a separate task, fed by a bounded queue. Nothing
  CaddieJ does can add latency to a player's shot.
- **Bytes are never modified, delayed, dropped, or synthesised.** CaddieJ is a
  witness, not a participant. A message CaddieJ rejects is still forwarded, and
  a duplicate CaddieJ ignores internally still reaches GSPro, because GSPro's
  view of the round is GSPro's to decide.
- **A simulator that cannot be reached is not concealed.** The launch monitor's
  connection is closed rather than accepting shots into a void, so the device's
  own reconnect logic sees exactly what it would see if CaddieJ were absent. The
  fallback is always available: point the device back at GSPro directly.

The cost is a configuration change on the launch monitor connector, which must be
pointed at CaddieJ instead of GSPro. Verifying that ProTee VX allows this is a
prerequisite for using the integration against real hardware.

### The boundary

GSPro's vocabulary — `BallData`, `HLA`, `APIversion`, response code `201` — exists
in exactly one module, `adapters/gspro/wire.py`. Everything beyond the adapter
sees normalized models (`Shot`, `BallLaunch`, `ClubDelivery`, `Club`) and typed
domain events. Tests enforce this rather than trusting it, so adding a second
launch monitor later touches nothing outside the adapter.

Normalized units are fixed and named in every field: mph, degrees, rpm, yards.
That convention matches the OpenShot open schema, so exporting to it later is a
mapping rather than a redesign. CaddieJ takes no dependency on that service.

### What the adapter handles, and why

Each of these exists because a real device does it, not because it might:

- **Framing.** Open Connect has no length prefix and no delimiter. One read may
  hold half a message or three messages. The framer tracks brace depth while
  respecting string literals, and skips junk to the next plausible start, so
  losing one message never costs the connection.
- **Heartbeats carrying zero-filled data blocks.** Devices send `BallData` full
  of zeros roughly once a second with `ContainsBallData` false. The flag wins
  over the block's presence, or every heartbeat would become a shot at 0 mph.
- **Split shots.** Ball and club data can arrive as two messages sharing a shot
  number. They are merged within a bounded window. Ball data alone is still a
  shot; club data alone is not, and is reported as incomplete — but it is not
  remembered as a shot, so ball data arriving late still becomes one.
- **Duplicates, and shot numbers that lie.** A device retransmits after a
  reconnect, so a repeat of a shot already published is ignored. The shot number
  is a hint and never a verdict: it is scoped to one connection, because numbers
  reset when a device restarts, and a number reused with *different* data is
  treated as another shot rather than refused. A device with a broken counter
  still gets every shot; the alternative is losing a session to a device bug.
- **Level flags, not edges.** `LaunchMonitorBallDetected` and
  `LaunchMonitorIsReady` arrive on every heartbeat. The adapter detects edges,
  and debounces ball detection *asymmetrically*: a ball appearing is believed at
  once so the floor stops projecting onto it, while a ball disappearing is given
  time so one missed heartbeat does not flash the marker back on under the
  player's club. A single symmetric delay could do neither, being shorter than
  the interval between heartbeats. Detection is tri-state: a device that never
  reports it must not read as reporting no ball, or the floor would clear its
  marker forever.
- **Connections ending mid-shot.** Per-connection state is not reset when a device
  disconnects, because that device's last messages may still be queued. The end of
  a connection travels through the same queue as its data, so every message is
  read with the state belonging to it, and a shot held waiting for club data is
  published rather than dropped.
- **Club and handedness.** Only GSPro's `201` response ever states these. Vendor
  codes (`DR`, `I7`, `PT`) are mapped to a category, keeping the raw code for
  provenance. An unrecognised code is unknown, not an error.

Shots are timestamped on ingestion, not when a subscriber gets to them. The
adapter holds no golf state: which club a shot was hit with, whether it counts,
and what the floor should show are decisions for the domain.

Interpretation runs on its own task behind a bounded queue, so nothing CaddieJ
does can push back on the wire. When that queue overflows — which means the
interpreter is wedged, and is logged as an error — the *oldest* item is discarded
rather than the newest. The newest is the most time-critical and the most likely
to be a shot; the oldest is almost certainly one of the heartbeats that arrive
every second.

### Testing without hardware

The fake simulator and fake launch monitor ship inside the adapter
(`adapters/gspro/fakes.py`), because they are the substitute for hardware during
development, not merely test fixtures. They can simulate connect, disconnect,
reconnect, club change, ball detected and removed, complete, incomplete and
duplicate shots, malformed payloads, messages split across packets, and a
simulator that is not running. The default test suite needs neither GSPro nor a
launch monitor.

---

## 8. Video pipeline (**Implemented for one camera**)

A rolling buffer of the room on disk, and a clip of every swing cut out of it.
FFmpeg records; CaddieJ decides which seconds a swing is in. Analysis is a later
milestone (§9) and nothing here anticipates it beyond leaving the clip and its
metadata somewhere a later reader can find them.

### Three decisions that shape everything else

**FFmpeg records continuously into segments; CaddieJ never touches a frame.**
The alternative — pulling frames into Python and buffering them — cannot keep up
at the frame rates a golf swing needs, and would put decoding on the event loop
that renders the floor. So a camera is opened by a subprocess and its output
lands on disk as one-second files. CaddieJ's contribution is arithmetic.

**A clip is copied, never re-encoded.** Extraction concatenates whole segments
with `-c copy`, which is the cheapest and most robust thing FFmpeg can be asked
to do. It also means the clip is a whole number of segments and therefore a
little wider than requested — which is a feature, not a compromise: the surplus
is recorded in metadata, so where impact falls is arithmetic rather than a guess
about where a cut landed.

**OpenCV is not in this path at all.** It was in the plan and did not survive
contact with the requirement. `cv2.VideoCapture` reports timestamps that vary by
backend and would hand every frame to Python. Its place is later, reading
finished clips for analysis, where being slow and precise is what is wanted.

### Layers

- `cameras` — what a camera *is*: the arguments that open it plus what it claims
  to deliver. A camera is a description, and the synthetic one is a description
  of a picture FFmpeg generates.
- `ffmpeg` — the only file in CaddieJ where an FFmpeg argument vector exists,
  and the process seam. It plays the part `persistence/database.py` plays for
  `sqlite3`.
- `layout` — every path the subsystem may write, so that "what does this put on
  my disk" has one answer and deleting the media root is always enough.
- `segments` — the rolling buffer as arithmetic: which files cover a window, and
  how a moment on CaddieJ's clock becomes a moment in a recording. Pure and
  synchronous, so the part that decides what a clip contains is testable with no
  subprocess and no waiting.
- `recorder` — one camera, recording, restarting when it dies.
- `clips` — extraction, the metadata beside a clip, and atomic publication.
- `service` — the only part that knows about events, queues, or shutdown.
- `fakes` — a fake FFmpeg for the unit tests. The synthetic camera in `cameras`
  is the other fake, and the more useful one.

### The timestamp model

The hard problem is not recording; it is knowing which two seconds of footage a
shot corresponds to. Four things stand between the ball being struck and a frame
on disk, and each is handled explicitly:

- **The launch monitor reports late.** `VIDEO_SHOT_REPORT_LAG_SECONDS` moves the
  window back by however long the device takes to speak.
- **The camera timestamps late.** `VIDEO_CAMERA_LATENCY_SECONDS` accounts for
  exposure, transfer and encoding. An event appears in footage *later* than it
  happened, so this is added, not subtracted.
- **The clocks drift.** FFmpeg's media clock is not CaddieJ's clock and a camera
  running at a nominal 30 fps is not running at exactly 30. Mapping from a single
  origin would be exact at start-up and hours wrong by the end of an evening, so
  the mapping is **re-anchored on the most recently completed segment**. The
  residual error is bounded by how long ago that segment closed, not by how long
  capture has been running.
- **Nobody has measured any of this.** Both offsets default to zero and a clip is
  marked `TimeQuality.ESTIMATED`, which is why the default window is two seconds
  wide before impact rather than tight. Cutting wide and saying so is worth more
  than a confident wrong frame number.

Monotonic time is used for the mapping, because a clock step during a round would
otherwise misplace a clip. Wall clock is used in metadata, because a monotonic
reading is meaningless outside the process that took it and a clip has to stay
interpretable next week.

### The rolling buffer

FFmpeg writes `seg-%06d.mkv` plus a CSV index it maintains itself, flushed as
each segment closes. Reading FFmpeg's own index is cheaper and far more reliable
than watching a directory and inferring durations. The reader consumes complete
lines only — the file is being appended to by another process, so the last line
may be half written.

Matroska for segments, because a Matroska file interrupted by a crash is still
readable while an MP4 whose index was never written is not, and the interrupted
segment is exactly the one covering the last shot. MP4 for clips, because that is
what everything downstream expects.

The buffer is bounded by both age and size, since a low-bitrate camera and a
high-bitrate one need different limits and neither bound alone is a guarantee.
The newest segment is never evicted: an empty buffer would report as "not
recording" while recording perfectly well.

**A restart discards the buffer.** FFmpeg's media clock and its segment counter
both restart at zero, so footage from a previous run cannot be placed in time and
its filenames are about to be reused. Keeping it would mean serving frames from
the wrong minute.

### Asking for a clip may fail, and failing is reported

A window request answers with the covering segments *or* a reason there are
none, and the reasons are deliberately distinct: nothing was recording, the
footage was already recycled, part of the window is missing after a restart, the
queue was full, extraction failed, extraction timed out. "The swing was not
filmed" and "your buffer is too short" prompt different actions, so they are
different events.

### Staying off the event path

`on_shot_completed` puts a request on a bounded queue and returns, performing no
awaits on the path where a clip is produced. Everything else happens in a worker,
including the part that makes this necessary: **at the moment a shot arrives, the
post-impact footage does not exist yet.** The worker's first job is to wait for
the future. Doing that anywhere else would be the one mistake that makes filming
block play.

The queue is small. Reaching its limit needs a wedged disk — a clip takes under a
second to copy and a golfer needs ten to hit the next one — and the alternative
to refusing is a growing queue of requests for footage that will be recycled
before they are served.

### File lifecycle

A clip is written under a dotted temporary name and moved into place with a
single rename, so nothing watching the directory can open a half-written video,
and `SwingClipReady` is published only afterwards. Its `.json` sidecar is written
first, so a clip appearing means everything about it is ready.

The sidecar holds where impact falls, the requested and captured windows, the
camera, the frame rate, the timing quality, and the FFmpeg command that produced
it. It exists so that a clip stays interpretable with the database gone, on
another machine, by something that was not running when it was recorded — and so
that a clip which looks wrong can be diagnosed from what made it.

Clips are grouped by day rather than by session, because history is optional and
its session row is created lazily; video would otherwise have to depend on
persistence to name a directory. Grouping by session remains available as a
query, through `shot_analyses` (§13).

### Failure isolation

The recorder's `serve` does not raise. A camera that cannot be opened, an FFmpeg
that exits, a device unplugged mid-round: each ends in a state change, a log
line, a backoff, and another attempt, and after enough consecutive failures it
stops trying and says so. This matters concretely, because `TaskSupervisor`
treats a supervised task raising as a reason to shut CaddieJ down — right for the
HUD server, wrong for a webcam — so containment lives in the recorder and the
service, not in the caller. A camera that ran for a while before failing gets its
restart budget back, so a device that drops out once an hour is not switched off
by the end of the evening.

A missing FFmpeg is a supported way to run CaddieJ, not a failure to start it.

### Privacy

Off by default, unlike history. It writes gigabytes and it films whoever is
standing on the mat, and both should be a decision somebody made rather than one
they inherited. Everything lives under one user-owned root, `media/` is ignored
by git, and nothing uploads anything. When analysis arrives (§9), sending a clip
anywhere will be its own explicit decision.

### Testing without a camera

Two depths of fake, because they answer different questions.

The **synthetic camera** is a real capture in every respect that matters: FFmpeg
runs, segments land on disk, clips are cut from them. Only the photons are
missing. It is what makes the pipeline runnable and watchable on a machine with
nothing plugged in — including whatever machine somebody develops this on — and
the generated picture has a running timestamp drawn into it, so a clip can be
checked against its metadata by eye.

The **fake FFmpeg** removes the subprocess entirely. Tests write the segments
they want to exist, hold the clock still, and assert on arithmetic. The whole
unit suite for this subsystem runs in under a second and needs no binary.

### What is deliberately not built

- **More than one camera.** The layout, the events and the naming are all
  per-camera, and a second recorder is a second `CameraRecorder`. What is missing
  is deciding when a set of clips for one shot is complete, which is only worth
  designing when there is a second camera to point.
- **Trimming clips to the exact window.** It would cost truthful metadata to gain
  a tidier file.
- **Audio.** Nearly free, and the impact transient is the best clock-alignment
  signal available. Nothing reads it yet.
- **Clip retention.** The buffer is bounded; the clip library is not. It needs a
  policy about deleting a golfer's own swings, which is a decision, not an
  oversight.
- **Recording the clip in history.** `shot_analyses` is there for it and is
  still unwritten. Coaching records the clip a line was written about (§9), so a
  coached shot can be traced to its footage; a clip of a shot nothing was said
  about is still only a file on disk.

---

## 9. Swing analysis and coaching (**Implemented**)

Coaching turns a shot into one short line on the floor: what the fault was, and
one thing to do about it. It is the first subsystem where a language model
touches something a player reads, so the boundaries around it are the point.

### Two tiers, and the model is the second one

The subsystem is split in two, and the split is what makes the rest safe.

**Naming the fault is arithmetic.** `coaching/faults.py` holds a table of what
can be wrong with a golf shot — face, path, strike, low point, speed, launch,
spin — each with a tolerance, a scale, an ideal that varies by club, and a pair
of labels for its two directions. `diagnose` scores every fault that the shot
actually measured, against both a fixed band and the player's own recent shots
with that club, and picks the worst one. It is offline, instant, free, and
deterministic: the same shot always produces the same verdict. This tier alone
puts a fault on the floor, and it is what a fresh checkout runs as.

**Wording it is the model's only job.** The model is handed the faults CaddieJ
already measured and asked to pick one and phrase it. It cannot introduce a
fault that was not measured, it cannot set the severity of one that was, and it
cannot write the words on the floor — those come from CaddieJ's own templates,
and the model chooses among them by name. What it genuinely contributes is the
spoken sentence, which is the part that reads like a person rather than a
readout.

The consequence is that every failure has the same shape. A missing key, a
refused request, a timeout, a malformed reply, an answer that arrives after the
next shot: all of them end with the templated line still on the floor, a row
recorded saying what happened, and a counter moved in `health()`. Nothing
raises, and nothing reaches the application.

### The flow

```
ShotCompleted ──▶ diagnose (arithmetic, on the event path)
                    │
                    ├──▶ ShotDiagnosed ──▶ floor shows the fault, now
                    │
                    └──▶ queue ──▶ worker
                                     │  wait briefly for SwingClipReady
                                     │  extract frames (only if configured)
                                     │  cache lookup
                                     ├──▶ AnalysisStarted
                                     │      provider ──▶ strict validation
                                     ├──▶ AnalysisCompleted ──▶ floor rewords
                                     └──▶ AnalysisFailed    ──▶ floor unchanged
```

The event path does arithmetic and nothing else. `on_shot_completed` diagnoses,
publishes, enqueues, and returns; it touches no network, no disk and no model.
Everything slow is in the worker, which is why the fault is on the floor before
anything can go wrong.

### There is no catching up

History queues hundreds of shots, because a shot recorded late is still a shot
recorded. Coaching is the opposite: advice about three swings ago is worse than
silence, because the player has moved on and would map the words onto the wrong
swing. So the queue is short, the newest shot displaces the oldest waiting one,
a reply that arrives after the next shot is discarded rather than shown, and
`PlayFlow` drops any line whose shot is no longer the current one.

### What stops it being expensive

Cost is bounded in five independent ways, because any one of them can be got
wrong. A **session ceiling** on total calls. A **minimum interval** between
them, since nobody hits two balls eight seconds apart and reaching it means a
replay or a bug. A **tight output ceiling**, because the answer is two short
strings and a number. A **cache** keyed on a quantised fingerprint of the
diagnosis, so the same swing is not paid for twice. And **frames off by
default** — that one is a privacy decision more than a cost one.

### The provider is a port

`coaching/provider.py` defines what a provider is: one method, four neutral
errors, and no vendor types. `adapters/openai/` is the only file that knows what
the OpenAI SDK looks like, and the SDK is an optional dependency, so CaddieJ
runs, coaches and films with nothing installed. The distinction the four errors
draw is not which error occurred but what to do about it — try again, try again
later, or stop asking — because that is the only thing the service acts on. The
SDK's own retries are switched off: a retry inside one call cannot be counted
against a budget or abandoned when a newer shot arrives.

`ScriptedProvider` and `DemoProvider` implement the same port, which is what
makes `COACHING_PROVIDER=scripted` a real demo mode: a simulated round produces
coaching lines end to end on a machine with no credentials and no internet.

### Nothing it says can change what CaddieJ does

The strongest statement in this section. `AnalysisCompleted` carries two strings
and a number; it has no field that could name an action. `PlayFlow` subscribes
to it, shows the text, times it out, and reads no branch from it. There is no
path by which a sentence a model wrote can affect the simulator, the floor's
geometry, the score, or anything else. Coaching is advisory data attached to a
shot, and the type system says so.

### Testing without an account

Every provider failure is a class, so the awkward cases are arranged rather than
hoped for: a hung provider, a rate limit with a `Retry-After`, a refusal, a
reply that breaks each validation rule, an answer that arrives after the next
shot. The OpenAI adapter is tested against a stand-in for the SDK with the same
exception hierarchy — which also proves it works when the SDK is absent. No test
in the suite can reach the network or spend anything, and `test_coaching_config`
exists to keep that true by accident-proofing the defaults.

### What is deliberately not built

- **Markerless club and body tracking.** The frame seam exists and the
  video-only focuses (tempo, posture, early extension, sway) are already in the
  vocabulary, gated on frames having been sent. What is missing is the tracking
  that would make them measurable rather than a model's impression.
- **Cross-session baselines.** A baseline is the current session's shots with
  the current club. Comparing tonight to last month needs a decision about what
  counts as the same player, not more code.
- **Anything reading a model's answer.** See above. This is a constraint, not a
  gap.

---

## 10. Kokoro voice architecture (**Implemented**)

Kokoro turns a coaching line into sound, on this machine, with no account and no
network. `bm_george` is the default voice.

    analysis.completed
        -> a bounded priority queue
        -> Kokoro renders, in a thread
        -> a sink plays it, to the end
        -> speech.started, then speech.completed

### The guarantee is structural, not careful

One worker, one active task, and playback awaited to completion. Two utterances
overlapping is therefore not a race that is unlikely — it is a state the service
cannot represent. Everything else in this section follows from wanting that
property without giving up responsiveness.

### Interruption is cancellation

`SpeechPriority` is an ordered number, so "does this deserve the room more than
what is playing" is a single comparison. When the answer is yes, the active task
is cancelled. Cancellation rather than a flag the sink polls, for two reasons: it
also works when the interruption lands mid-render, and the sink then silences the
device on its way out — so by the time the worker starts the next line, the
speaker is genuinely free. `play` does not return until it is.

Only `COACHING` is produced today. `ANNOUNCEMENT` and `ALERT` exist so that the
thing which needs to interrupt a coaching line later needs no new mechanism.

### Staleness is a property of the utterance

A line about the last shot is worthless once another ball is in the air, so a
newer coaching line **replaces an older one that has not been spoken yet**. Two
warnings are not like that, so `Utterance.perishable` carries the answer and the
queue reads it.

"Not spoken yet" includes the line currently being rendered, and that boundary is
where the interesting decision is. Rendering takes about a second, which is long
enough for the next shot to arrive during it, and abandoning a sentence nobody has
heard costs nothing. Once it is audible the rule reverses: cutting a two-second
line in half to start another two-second line sounds like a fault, so an equal
priority never interrupts. Only something genuinely more urgent does.

The queue is short and drops from the tail, which is the least urgent and most
recent thing in it. An arriving low-priority line is a candidate like any other,
so a coaching line turning up behind a full queue of warnings is the one refused.

### Off the event loop, twice

Kokoro is a neural network on the CPU of a machine also running a golf simulator,
and PortAudio's calls block. Both are confined to worker threads: the adapter
crosses into one per render, and the sink owns its stream from `start` to `close`
inside a single thread so the async side cannot cancel between two operations on
a live device. The event path itself does nothing but normalise text and queue it.

### The cache is worth having because coaching repeats itself

The advice cache upstream returns the same wording for the same fault, the
templated lines are a fixed table, and a player working on one thing hears one
sentence all evening. Rendered audio is kept in memory bounded **by bytes rather
than entries** — a sentence is a hundred and fifty kilobytes and a word is two —
and optionally on disk as plain WAV, which survives a restart and can be listened
to, the only way audio is really debuggable.

### Two ports, not one

`Synthesizer` and `AudioSink` are separate because rendering and playing fail for
unrelated reasons at unrelated times, and only one of them can be cached.
Splitting them also makes "render everything, play nothing" a configuration
(`SPEECH_OUTPUT=none`) rather than a special case, which is how a headless
machine exercises the whole pipeline.

### Failure is silence and nothing else

No model, no weights, no sound card, no PortAudio, a render that hangs, a device
that dies mid-word, or a bug in the subsystem: every one ends in a counter, a log
line, and a `SpeechCompleted` saying which. Nothing raises, nothing reaches the
supervisor, and the shot, the floor and the history are untouched. `speech` is
also the only subsystem nothing else holds a reference to.

### Privacy

Synthesis is local, so the coaching text never leaves the machine. The only
network access is the first-run model download. Nothing logs the sentence: the
allowlisted fields are an engine name, a voice name and a duration, because a log
that quoted everything CaddieJ said would be a transcript of somebody's practice
session.

### Testing without a model or a speaker

`speech/fakes.py` provides an engine that returns wordless babble of a believable
length and a sink that records what it *started* separately from what it
*finished* —
which is the difference an interruption makes, and therefore the assertion an
interruption test wants. A sink can be held on a gate, so a test puts a sentence
exactly halfway through rather than sleeping and hoping. The Kokoro and PortAudio
adapters are tested against stubs, so the default test run needs neither a speech
model nor a machine learning framework installed.

### What is deliberately not built

- **Speaking the templated line.** Only the coached line is spoken. The templated
  one is already on the floor and reads in less time than it takes to say, so
  speaking every one would make CaddieJ talk after every swing. With
  `COACHING_PROVIDER=none` — the default — the room stays quiet.
- **Streaming synthesis.** Rendering a whole sentence before playing it costs
  under a second and makes the duration known up front, which `SpeechStarted`
  carries and anything ducking music or holding a floor line wants.
- **Volume control.** The system mixer owns this, and duplicating it in
  configuration means two places to look when the room is too loud.
- **Anything reading back.** No microphone, no wake word, no recognition.

---

## 11. Lighting architecture (**Implemented**)

The room reacts, very slightly, while CaddieJ talks. Philips Hue is the first
output behind a `LightingPort`; the subsystem above it knows nothing about
bridges.

Lighting differs from every other subsystem in one way that shapes all of it: it
changes something **outside** CaddieJ that somebody else set deliberately. The
lamps in a living room are the way they are on purpose. So the design is built
around one promise — *it gives the room back* — and most of the care is spent on
the paths where keeping that is hard.

### The lights follow the measured waveform

`SpeechStarted` carries an `Envelope`: a smoothed root-mean-square measurement of
the samples that are about to be played, at twenty frames a second. The lighting
service samples that on its own clock and maps it to brightness.

That the source is the actual audio is the point of the feature, and it rules out
three easier things. **Not a model deciding when a light should change** — an LLM
in a real-time path is against §2 anyway, and it would be inventing a shape
somebody can hear is wrong. **Not a timer over the sentence length** — the same
objection, arrived at by arithmetic instead. **Not the sink reporting as it
writes** — that would put a lighting callback inside the playback thread and make
a bridge able to stall the speaker.

Three decisions make the measurement usable by a lamp rather than merely accurate:
RMS rather than peak, because peak jumps on every consonant and a light driven by
it flickers; normalised against its own peak, so the shape of the words rather
than the volume of the voice is what travels; and smoothed asymmetrically — fast
attack, slow release — so an onset is caught but the gap between two syllables is
ridden over. `math.sumprod` makes it about a millisecond for a three-second line,
which is why it is computed on the event path without a second thought.

**The envelope is on the event, not fetched.** A few dozen floats, three orders of
magnitude smaller than the samples they came from. The alternative is handing out
the audio, and then everything that wants to dim a lamp needs to know about PCM.

### Brightness is always a fraction of the baseline

The baseline is whatever the lights were doing when CaddieJ started talking, and
it is the only honest reference point: somebody at twenty per cent in the evening
and somebody at full daylight both asked for that, and a reaction that ignored it
would be invisible to one and blinding to the other.

**It is read once and held until the room is idle.** Re-reading it during a
reaction would measure CaddieJ's own dimming as the new normal, and every sentence
would leave the room slightly darker than the last — a drift nobody would ever
attribute to the lights. This is the single worst bug the subsystem could have,
and holding the baseline across back-to-back sentences also stops the room
flicking back to full between two of them.

The default range dips by not quite a fifth in the gaps between words and returns
to normal on them. Dipping rather than lifting, because dipping has room to work
at any baseline and lifting has none when the lights are already full. A room that
visibly pulses while a coach talks is a novelty switched off within a week; one
that seems very slightly alive is one nobody consciously notices.

### The command budget is divided, and the bridge does the smoothing

A Hue bridge tolerates roughly ten light commands a second. So the tick is
**derived** from that budget and the number of lamps in use rather than from what
would look best: six lamps get a slower, gentler reaction rather than a bridge
dropping every third command.

Each command asks for a transition as long as the gap to the next one, so the
bridge interpolates and five updates a second look continuous. Sending more
commands to draw the same curve would produce something a bridge refuses. A
deadband drops any update that would move a lamp by less than anybody can see,
which is the coalescing §11 always called for — during the pauses in a sentence the
curve is flat, and a command spent there is a command taken from the part of the
sentence where it would have been visible.

### Giving the room back

Restoring is its own task, never part of a reaction. Reactions are cancelled
constantly — a sentence cut short, a celebration taking over — and a restore
abandoned half way leaves somebody's living room dimmed with nothing left running
that knows what it was. A lock makes two callers arriving at once restore once.
`aclose` waits for it, because this is the one piece of shutdown visible from an
armchair.

Two smaller rules keep it from arguing with a person. A lamp that is **off** is
never touched, and neither is a switch with no brightness — turning somebody's
light on because a coach started talking is not a subtle reaction to anything. And
a modulation command sets brightness only, never `on`, so reaching for a switch
mid-sentence works.

### One gesture, and no golf

The only thing this subsystem accepts other than a voice is a `Flourish`: a depth
and a duration, both fractions of baseline. It does not know what a birdie is.
Deciding that a birdie is worth 0.66 of the baseline for 2.4 seconds belongs to
§12, where the same judgement also decides what the floor and the voice do about
the same birdie — which is the point of having one place for it. A cue outranks a
sentence and keeps the room until it ends.

Depth and length rather than colour or a sequence is deliberate rather than
unfinished. A celebration built on brightening does nothing at all in a room that
is already bright, and "nothing at all for the best shot of somebody's life" is
the worst available failure mode; dimming has headroom at any baseline.

### Where the port lives, and why not in the domain

§7 lists `LightingPort` among the intended domain ports. It is not one, for the
same reason `SpeechPort` is not: the domain never says *dim the lights*. Lighting
is a **reactor** — it observes `SpeechStarted` and decides for itself — so the port
is a subsystem seam next to `Synthesizer` and `AudioSink`, and the domain stays
unaware that any of it exists. Nothing in CaddieJ holds a reference to the
lighting service.

### Failure is a dark bridge and nothing else

A missing bridge, a revoked key, an unplugged lamp, a timeout, a rate-limit
rejection, or a bug in the subsystem: every one ends in a counter and a log line.
Handlers start a task and return, so a bridge that takes three seconds to answer
cannot hold up the bus that is also carrying shots. Past a few consecutive
failures the bridge is **left alone for a minute** and tried again, because an
appliance that has been unplugged would otherwise cost several timeouts per
sentence for the rest of the evening. `restore_failures` is counted separately
from `failures` in health: a dropped modulation command is invisible, while a
dropped restore means somebody's room is dimmer than they left it.

### The transport, and one stated compromise

`adapters/hue` is the only place that speaks HTTP. It uses CLIP v2 —
`hue-application-key` in a header, brightness as a percentage, transitions as
milliseconds inside `dynamics`, and refusals reported inside a `200` — and
**certificate verification is off**. A bridge presents a self-signed certificate
for its own private address and there is nothing to check it against. The
alternatives were the deprecated plaintext v1 API, which is worse, or pinning the
bridge certificate by identifier, which is more code than this milestone
justifies for a device on the same LAN. So the transport is encrypted but
unauthenticated, the application key is handled as a secret per §17, and nothing
crosses it but a brightness. Stated here rather than left as a quiet
`verify=False` in a constructor.

### Testing without a bridge

`lighting/fakes.py` provides lights that record every command with the transition
it was given — the whole history, because nearly every interesting assertion here
is about a sequence — and that can be made to fail at any single call, including
only the restore. The Hue adapter is tested against a stub HTTP client, so the
default test run needs neither a bridge nor the client library installed.

### What is deliberately not built

- **Colour.** Brightness alone, because it is what every lamp has and what a
  subtle effect needs.
- **Hue Entertainment / DTLS.** The streaming API would allow much faster updates
  and brings a different protocol, a different pairing step and a different
  failure model for an effect that is meant not to be noticed.
- **Reacting to shots.** A good drive does not dim the room. Lighting reacts to
  the voice and to a flourish somebody else asked for, and nothing else.
- **Groups.** A `grouped_light` is one command instead of several, and limited to
  about one a second — too slow to follow a voice.
- **Light discovery at runtime.** Lamps are resolved once at start-up; a bridge
  asked which lights exist before every command would spend its budget on
  questions.

---

## 12. Effects architecture (**Implemented**)

Something finishes a hole, one table says what that is worth, and one director
sends it to every output at once: the projected floor, the voice, the lights, and
a source in OBS. Nothing about it is decided at runtime and nothing about it
leaves the machine.

### No model decides anything

Not what happened, not whether it was good, not how loud the response should be.
This is a design decision rather than a limitation of the milestone, and it cuts
the opposite way from §9, where a model earns its place describing a swing.

Mapping five known names onto five fixed reactions is a dictionary lookup. A
model would add latency and an API bill to buy nothing, and would cost the two
properties a celebration actually needs. It would occasionally be **wrong about
what happened**, which is unforgivable when the input is a number somebody can
see on a screen. And it would **never be the same twice**, which is the
difference between a landmark and a novelty: the fifth birdie of a round should
feel like the first four, and the one hole in one somebody ever hits should feel
like the thing the room was always going to do.

### The vocabulary, and what it deliberately cannot say

`HoleResult` — par, birdie, eagle, albatross, hole in one — lives in the domain
with no brightness, no font size and no scene name in it. `HoleCompleted` is
**published by nobody yet**: recognising a birdie needs a hole, a par and a
stroke count, and Open Connect carries shots and a club, not a scorecard. It is
subscribed to already, so the day scoring exists the room reacts without a line
changing near a projector.

There is no bogey, no double, and nothing worse. Not an oversight: nothing in
CaddieJ should be built to react to somebody playing badly, and a vocabulary that
cannot name a disaster cannot later be wired to one by accident.

### An effect is six values

`cue`, `label`, `priority`, `intensity`, `seconds`, `say`. There is deliberately
**no per-channel section** — no brightness, no scene name, no audio file. An
effect says how much of an occasion something is and each channel decides what
that means in its own terms. The alternative, a slot per output, makes adding a
fifth output an edit to every effect and turns the catalogue into a place where
somebody tunes a colour.

`priority` and `intensity` are separate fields even though they correlate for
everything in the catalogue, which is exactly why keeping them apart matters: one
decides who wins when two things happen at once, the other decides how big the
gesture is. The day something is urgent but small — a warning — a single field
would have to be wrong about one of them.

`effects/catalogue.py` is the one table, and the escalation is its point. A par
is almost nothing on purpose: if a par gets a real reaction then a birdie has to
be bigger than a real reaction, and by the time the scale reaches a hole in one
there is nowhere left to go. The bottom is set almost too low so the top can be
genuinely loud.

### The invariant

**At most one effect at a time, every effect ends, and every output is put back.**
A celebration subsystem that cannot promise that is worse than none, because the
failure mode is a projector reading BIRDIE over somebody's next forty shots.

- **The event path never waits.** A handler starts a task and returns. The
  bus isolates subscribers (§4), so a hung celebration cannot delay a shot,
  and the handler itself must still return so the next hole is not sitting
  behind this one on the effects queue.
- **No queue of pending effects.** An effect is a moment. An eagle rendered forty
  seconds late because a queue was draining is not a late celebration, it is a
  wrong one, arriving over whatever the player is doing now.
- **Higher priority replaces; equal or lower is refused.** Refusal for equal
  priority because the running effect is already on screen, and swapping one
  celebration for an equally important one half way through reads as a bug.
- **Everything is capped.** The duration is clamped, so a typo in the catalogue
  cannot hold the room for a minute, and every call into a channel is wrapped in
  a timeout, so an OBS that connects and then stops answering cannot hold an
  effect open.

### The reset is unconditional, and it is its own task

Every channel is reset at the end of every effect — including channels the effect
never touched and channels whose `begin` failed. Remembering which ones were
touched is bookkeeping that only has to be wrong once, in the replacement case,
to leave a source visible forever; unconditional reset costs a call that does
nothing and is far easier to be sure about. So `end` must be idempotent and safe
on a channel that never began.

Resetting runs on its own task rather than inside the effect's, the same lesson as
§11: effects are cancelled routinely, and a reset abandoned half way is the exact
failure this design is about. Replacement is the one case with **no reset at
all**, and that is correct — channels take absolute values, so the incoming effect
overwrites the outgoing one on every output, and resetting in between would
produce a visible flicker and nothing else. A reset is tagged with the generation
it is undoing, so one still in flight when the next effect starts cannot reset
the new effect's outputs a moment after they were set.

### The four channels

Each is a translation and nothing more. The order is the order they begin, and
the floor is first because it is the only one whose latency is a function call
rather than a network.

- **Floor.** A `FloorFlourish` — cue, label, intensity, duration — as a new
  optional field on `FloorState`. It is a **layer, not a mode**: everything the
  round put on the floor stays underneath it, which is what makes the reset clean.
  §6 forbids modelling a momentary flash as transient state, because a client
  reconnecting across a flash misses it with nothing to resend. This is not a
  flash: it lasts seconds, Python decides when it starts and stops, and a client
  that joins in the middle correctly finds a celebration in progress. The
  duration travels on the wire so the browser paces its animation to the length
  the director is holding the state for, rather than to a length the stylesheet
  guessed.
- **Voice.** One short line at `ANNOUNCEMENT` priority, the tier §10 reserved for
  something about the round rather than the swing. It outranks coaching, so a
  birdie cuts off a sentence about the last shot's club path — correctly, because
  the player has stopped listening to that. Not perishable: two holes are two
  announcements. `end` does nothing, because a word already spoken cannot be
  taken back and cutting the voice off when the label fades would clip a syllable.
- **Lights.** One `Flourish`, linear from a barely-there dip to nearly dark.
  `end` does nothing here either: §11 owns its own timing and restores the room
  itself, so a flourish that outlives a cancelled effect by a second finishes
  properly rather than stopping mid-dip.
- **OBS.** A scene item's visibility and a text source's caption, which is the
  whole of what §6's client can be asked. Whatever the source *contains* — a
  stinger, a graphic, an animation, a sound — belongs to the person who built the
  scene; CaddieJ turns it on for the length of the effect and turns it off again.
  Nothing is created and nothing is downloaded.

### Why OBS holds a connection when nothing else does

The overlay is provisioned once at start-up over a connection that lives a second
and is discarded, which is right for a one-off. An effect needs a command at the
start and another a few seconds later, and paying for a connect and an identify
handshake at the front of a celebration would put the handshake inside the moment
it is meant to mark. So the connection is opened on first use and kept.

There is **no reconnect loop**, on purpose: there is nothing to do with an OBS
connection between celebrations, and a loop would spend the evening retrying one
nothing is waiting for. A failed command drops the connection and the next effect
opens a fresh one, so recovery costs the first command after OBS comes back and is
free every other time.

### Placeholder visuals, and why they are geometry

The floor's celebration is expanding rings and one word, in CSS and SVG. No
images, no video, no fonts beyond the page's own. That is partly taste — a
restrained flourish survives being seen four hundred times, and a downloaded
stinger does not — and partly that **nothing here is anybody's intellectual
property**, which a sound effect or an animation pulled off the internet would be.
Both variables the stylesheet needs arrive on every snapshot: the duration, and
the intensity, which grows the rings, the type and the glow together. Anybody who
wants a real stinger points the OBS channel at their own source.

### Failure is a counter and a log line

Every call into every channel is isolated and timed out, so a bridge that has gone
away is no reason for the floor to stay blank. `reset_failures` is counted
separately from `failures` in health, for the same reason as §11: a failed `begin`
means one celebration was quieter than intended, which nobody notices, while a
failed `end` means an output is still showing something and will keep showing it.

### What is deliberately not built

- **A queue of effects.** An effect is a moment; see the invariant.
- **Composition.** Two celebrations at once is not a feature.
- **Per-channel tuning in the catalogue.** One dial per effect is what keeps the
  table a set of judgements rather than a settings file.
- **Reactions to bad shots.** The vocabulary cannot name one.
- **Anything driving `HoleCompleted`.** Scoring is not CaddieJ's yet.

---

## 13. Persistence approach (**Implemented**)

SQLite via the standard library, in one local file the user owns. No external
database, no ORM, no migration framework.

### What is stored

Shot history, not an event log. The plan was for both — an append-only log of
every event, with queryable history derived from it — and the log was dropped
after asking what would read it. Replaying link changes and heartbeats
reconstructs a state nothing wants; what people ask of a golf simulator is what
they hit, with what, and how it varied. A shot table answers that directly, and
adding a log later needs no change to it.

One row per shot, flat and denormalized: a shot is written once and read in
aggregate, so joins would buy nothing and cost every query. It holds the session
it belongs to, when it was observed and when it was recorded, the player name if
there is one, handedness, the club and the vendor's code for it, the normalized
ball and club measurements, and the provenance needed to trace it back to a
device.

Every measurement column is nullable, because every device measures a different
subset, and a zero standing in for "not measured" would corrupt every average
computed from it.

Two tables exist for the future rather than the present. `shot_payloads` keeps
the exact bytes a device sent, when raw capture is switched on; it is separate so
that the debugging artifact can be deleted wholesale without touching the
measurements. `shot_analyses` points at whatever a later milestone produces about
a shot — a swing video, an AI analysis — through an opaque reference, so neither
needs a schema change when it arrives and the shots table never learns what a
video is.

### Migrations

An ordered tuple of statements plus SQLite's `user_version` pragma. That is the
whole strategy. It adds no dependency, the version travels inside the file rather
than beside it, it is set in the same transaction as the statements it describes,
and one line of `sqlite3` tells anyone holding an unfamiliar database what it is.
What it gives up is a record of when each migration ran; nobody debugging a
single-user golf simulator has needed that.

A database written by a newer CaddieJ is **refused, not downgraded**: the newer
version may write a column this one does not, and carrying on would produce rows
it considers corrupt.

### Layers

Each is testable without the ones above it:

- `schema` — the tables, and how they move forward.
- `records` — how a shot becomes a row and comes back a shot. Reading is a full
  reconstruction, so a column written into the wrong place fails a round trip
  rather than being discovered a season later.
- `repository` — every statement CaddieJ runs, and nothing else. Synchronous,
  takes a connection, does no error handling.
- `analytics` — pure arithmetic over records. No SQL, so SQLite is never asked to
  compute an average that Python would round differently.
- `database` — confines SQLite to one thread.
- `service` — the only asynchronous part, and the only one that knows about
  events, queues, or failure.

### Rules

- **SQLite is confined to one thread.** One rather than a pool: a connection
  belongs to the thread that created it, and writes to one file are serial
  anyway, so a pool would take turns through the file lock instead of the queue.
- **Nothing on the event path touches the database.** `on_shot_completed` puts
  the shot on a queue and returns, performing no awaits at all. The bus
  dispatches handlers one at a time, so a handler waiting on an fsync would hold
  up the handler that clears the floor.
- **History is never a reason to stop playing golf.** A database that will not
  open disables history for the run. A write that fails is counted and survived.
  A read that fails answers with nothing.
- **A lost shot is never silently lost.** Drops and failures are counted, logged,
  and reported through `/healthz`. A dropped shot leaves a gap in the ordinals,
  and the gap is the record that something went missing.
- No secret is ever written to the database (§17).

### What a session is, for now

One run of CaddieJ that recorded shots. That is genuinely all it is today: there
is no model of rounds or holes yet, and inventing hole detection would be
fiction. Sessions are therefore owned by the history service rather than the
domain, because "one run writing to this file" is a fact about the store. When
the domain gains rounds and holes, session boundaries move there.

The session row is written with the first shot, not at start-up, and in the same
transaction as that shot. So a run where nobody hit anything leaves nothing
behind, and opening the file only to read it never adds to it — which a later
statistics command will want, and which is why the sentence above can say
"recorded shots" and mean it.

### The port that is not there yet

The plan said persistence would sit behind a `HistoryPort`. It does not, because
nothing in the domain asks history a question: the service subscribes to
`ShotCompleted` and writes what it hears. A port with no caller would be an
abstraction defending against a direction of dependency that does not exist. It
arrives when the domain first needs an answer from history — "was that the
longest drive today?" for a voice line, say.

---

## 14. MCP and assistant boundary (**Implemented**)

An assistant integrates with CaddieJ **from the outside**, through MCP or plain
loopback HTTP. It is never embedded in the core.

The tool reference is [docs/assistant-api.md](docs/assistant-api.md). What follows is why
the boundary has the shape it does.

The principles it holds to:

- CaddieJ exposes a small, stable **façade**: read-only state queries plus a
  narrow set of explicitly non-real-time commands. Six tools today, five of them
  reads.
- **The event bus is not exposed.** Publishing internal events over MCP would
  make every internal refactor a breaking API change.
- Commands reachable from an assistant are, by definition, in the enrichment
  tier. No call can influence shot handling, scoring, or anything with a latency
  guarantee.
- The surface binds to loopback and is authenticated. It is not a remote-control
  API.
- Assistant absence is normal operation. CaddieJ never waits for it.

This is what keeps §2 true in the presence of a conversational assistant: the
assistant can *observe* a curated view and *ask* for a narrow set of things, but
it is structurally incapable of entering the deterministic path.

### Three layers, and why there are three

```
an assistant  --MCP over stdio-->  caddiej.mcp        (a separate process)
                                        |
                                   loopback HTTP, bearer token
                                        |
                                   caddiej.adapters.api   (transport)
                                        |
                                   caddiej.assistant      (the boundary)
                                        |
                                   history, coaching, play
```

Two of the three layers could be argued away, so each has to earn its place.

**`caddiej.assistant` is the API.** Not the HTTP routes and not the MCP tool
definitions — those are two transports over what is declared there, and either
could be replaced without an assistant noticing. It holds hand-written contract
models, a read façade, a command façade, and a literal tool registry.

**The HTTP transport exists so that MCP is not the only way in.** MCP is a
young protocol with a specific client population. A shell script, a
home-automation box, or a different protocol translator is an equally legitimate
consumer, and giving the SDK a monopoly on the surface would have made CaddieJ's
capabilities inseparable from one protocol's fortunes.

**The MCP process exists for isolation.** It is started by somebody else's
assistant runtime, which will restart it, kill it, and run it for weeks. That is
not a lifecycle to give a process holding a SQLite handle, an OBS connection and
an API key. It imports exactly two CaddieJ modules — `assistant.contract` and
`assistant.registry`, neither of which imports anything else from CaddieJ — so
the worst an assistant runtime can do to CaddieJ is stop asking it questions. A
test asserts that import graph, because a convenience import would quietly end
the argument.

### Nothing internal is published

Every model in `assistant/contract.py` is written out by hand. No `Shot`, no
`SessionAnalytics`, no `*Health` dataclass crosses the boundary, and the tedium
is the feature: renaming a domain field cannot silently become a breaking change
to somebody's assistant, because the two are not the same declaration.

`/healthz` is the counter-example, and it is instructive. It is `asdict()` over
internal dataclasses, so its shape is whatever those happen to contain today. It
grew two fields without anybody thinking of it as an API change, which is fine
for a dashboard and would not be fine here.

The projections drop things on purpose. A shot loses its device identifier, its
device session, its sequence number and its raw payload; a club keeps its kind
and loses the vendor's code for it; coaching keeps its named fault and severity
and loses the clip path. A subsystem's health becomes two booleans through a
narrowing callable, so a subsystem that grows a `last_error` does not thereby
grow the assistant API.

### Absence is classified, once

Every answer carries an `availability` of `ok`, `empty` or `unavailable`, because
"nobody has hit a ball yet" and "the database is locked" are different facts that
an assistant will otherwise flatten into the same confident sentence.

This is the most important thing in the boundary and it needed a change further
down to be true. History fails soft — a locked file makes a query return its
default rather than raise — so "no rows" and "the query failed" arrived
identically. `HistoryService` now counts read failures, and the façade compares
that counter across a query, so a failure is reported as a failure. The
cautious direction is the safe one: saying "I could not tell you" when the read
was actually fine costs nothing, and the opposite tells somebody they did not
practise today.

### Capability, not reflection

The tool registry is a literal list. There is no mechanism by which a method
added to `ReadFacade` becomes callable from outside, and a test asserts the names
against a literal set, so a capability can only appear by somebody typing it into
that file.

No argument anywhere is a free-form string except `focus`, which is validated
against a closed enum. Nothing accepts a path, an expression, a query fragment
or a URL. "No arbitrary execution" is therefore a property of the surface rather
than a promise about the handlers.

Reads and mutations are separate objects, and the read façade holds no reference
it could write through. With mutation switched off, `CommandFacade` is not
constructed, the route is not registered, and the MCP tool is not offered: a
read-only deployment is discovered by there being no way to write, not by being
refused.

### Loopback is not authentication

Any web page the operator visits can reach `http://127.0.0.1:8731/`, because a
browser will resolve an attacker's hostname to loopback and then treat the result
as that attacker's origin. So three gates: a bearer token compared in constant
time, refusal of any request carrying an `Origin` header at all, and a loopback
`Host` check. No CORS middleware is installed — absent, not permissive — because
there is no browser client for this API and the headers would only help build
one. A missing token fails closed, and a non-loopback bind is refused at
start-up rather than warned about.

### Failure is the API's problem and nobody else's

The API is built after the domain, started after the room is already working, and
supervised in a task that catches everything: a failure logs and the task exits,
where every other supervised task failing brings CaddieJ down. That asymmetry is
deliberate. A golf simulator that stops projecting because an HTTP port could not
be bound would have made the assistant load-bearing, which is exactly the thing
this boundary exists to prevent.

---

## 15. Dependency policy: upstream-first, plugin-based, zero-core-patches

**Upstream-first.** Prefer a maintained upstream library over writing or copying
one. Do not vendor, fork, or patch dependencies. If an upstream library is
missing something, contribute upstream and pin a version in the meantime.

**Minimal.** Every dependency must justify itself against the standard library.
Runtime dependencies today are `pydantic-settings` for typed configuration and
`simpleobsws` for the OBS transport. Test dependencies are `pytest` and
`pytest-asyncio`. Optional features may add optional dependencies, and CaddieJ
must run without them: `[ai]` brings the OpenAI SDK, `[tts]` brings Kokoro and
PortAudio bindings, `[hue]` brings an async HTTP client, `[mcp]` brings the MCP
SDK. Each is confined to one package, imported inside the branch of the factory
that selects it, and the default test run installs none of them.

`[mcp]` is the odd one and worth stating: it is needed only in the process an
assistant runtime starts, not in the process that runs the golf simulator, which
serves the same six tools over HTTP with nothing extra installed. The MCP
process itself uses `urllib` rather than an HTTP client library, because a
translator that holds no state should not also hold a dependency tree.

**Zero-core-patches.** Adding an integration must not require editing the core.
Achieving that requires stable seams first — ports, a shared adapter contract,
and versioned event schemas — which is why a plugin discovery mechanism is
deliberately deferred until there is a second real consumer to design against
(§17). Building the framework before the seams exist would couple plugins to
internals and freeze them there.

**Exception taxonomy.** Errors are part of the dependency boundary. A root
`CaddieJError`, per-adapter subtrees, and a clear split between *"the external
system is unreachable"* (retry, degrade) and *"the request was rejected"* (fail
fast, tell the user) let callers react correctly without importing vendor
exception types.

---

## 16. Failure isolation principles

CaddieJ runs next to a physical simulator with a player standing in front of it.
Degrading is always better than stopping.

1. **Tier isolation.** Enrichment failures never propagate into the
   deterministic core (§2).
2. **Adapter isolation.** One adapter's failure does not affect others. Losing
   Hue does not affect the HUD; losing cameras does not affect shots.
3. **Subscriber isolation.** A handler exception is logged and contained; other
   handlers still run. A slow handler must not delay others.
4. **Bounded waiting.** Every external interaction has a timeout. Connect
   timeouts and request timeouts are configured separately.
5. **Recover by re-applying state, not by replaying history.** Because outputs
   are projections (§3), a reconnecting overlay or bridge is restored by pushing
   current state. There is no general event log to replay.
6. **Explicit drop policies.** Under load, superseded output is dropped
   deliberately. Facts — shots, sessions — are never dropped silently.
7. **Loud failures, quiet degradation.** Failures are logged with structured
   context; the player-facing surface shows a feature as unavailable rather than
   appearing frozen.
8. **Deterministic shutdown.** Shutdown is bounded and ordered, and it never
   depends on a healthy external system.

---

## 17. Security and secrets principles

1. **Secrets never enter git.** `.env` is ignored; `.env.example` documents keys
   with empty or placeholder values.
2. **Secrets are typed.** Credentials are `SecretStr` in configuration, so they
   do not appear in reprs, dumps, or tracebacks. `OBS_PASSWORD` and
   `COACHING_API_KEY` are both handled this way, and tests assert neither leaks
   into an error message, a repr, or a log record.
3. **Secrets never appear in logs.** Structured logging emits an explicit set of
   context fields rather than arbitrary objects. Log statements are reviewed for
   credential exposure.
4. **Secrets never enter events or persistence.** Events are the in-process
   vocabulary of the session; shot history is written to disk. A secret in
   either place is a secret that will be copied, logged, or stored. A future
   diagnostic journal would make that permanent.
5. **Loopback by default.** The HUD feed and the MCP façade bind to 127.0.0.1.
   Exposing any surface beyond the local machine is an explicit, authenticated
   decision.
6. **Local-network assumptions are documented.** OBS WebSocket authenticates by
   challenge but does not encrypt traffic; CaddieJ assumes OBS is reachable on
   loopback or a trusted LAN.
7. **Third-party upload is opt-in and explicit.** No audio, video, or shot data
   leaves the machine unless a feature that does so is configured on. Coaching
   is the first feature that can: it sends nothing until `COACHING_PROVIDER` is
   set, and even then sends only measurements until `COACHING_SEND_FRAMES` is
   turned on as well. Frames are behind a second switch because they are
   pictures of somebody in their own home, which is a different decision from
   sending a launch angle.
8. **Camera data is sensitive.** Recording is off by default, everything written
   lives under one user-owned root that is ignored by git, and nothing uploads
   anything. Retention and upload have conservative defaults and are documented
   where they are configured.
9. **Speech stays on the machine.** Synthesis is local, so the coaching text is
   never sent anywhere to be spoken; the only network access is the first-run
   model download. Nothing logs the sentence either — the allowlisted fields are
   an engine name, a voice name and a duration — because a log that quoted
   everything CaddieJ said would be a transcript of somebody's practice session.
10. **Least privilege for assistants.** The assistant façade exposes the minimum
    surface required, and never a passthrough to internals. It is off by
    default, requires a bearer token, refuses a non-loopback bind at start-up,
    and treats "may read" and "may write" as two separate decisions. Coaching
    prose is behind a third switch, for the same reason camera frames are.

---

## Repository layout

Implemented today:

```
src/caddiej/
├── __main__.py          # entry point: logging setup, run, exit codes
├── app.py               # lifecycle and dependency wiring
├── config.py            # typed configuration via pydantic-settings
├── core/
│   ├── events.py        # Event base type: id, UTC time, monotonic time, source
│   ├── delivery.py      # Delivery: critical / coalesce / drop
│   └── event_bus.py     # AsyncEventBus: isolated subscribers, bounded queues
├── runtime/
│   ├── lifecycle.py     # TaskSupervisor and shutdown signalling
│   └── backoff.py       # retry pacing with full jitter
├── domain/
│   ├── shot.py          # normalized shot, ball, club and player models
│   ├── media.py         # what a clip is, and where impact falls in it
│   ├── coaching.py      # the fault vocabulary, a diagnosis, a piece of advice
│   ├── speech.py        # what to say, how urgent it is, whether it goes stale
│   ├── statistics.py    # golf arithmetic both history and coaching need
│   ├── events.py        # the typed vocabulary of what happened
│   ├── event_policy.py  # which events are critical, coalesced, or droppable
│   ├── ports.py         # FloorPort: what the domain asks of the floor
│   └── play.py          # PlayFlow: what the floor shows as a hole is played
├── floor/
│   ├── state.py         # FloorState: immutable, versioned render state
│   ├── service.py       # FloorService: the single writer
│   ├── geometry.py      # physical plane, surface map, default / homography
│   ├── presenter.py     # FloorPresenter: golf decisions become geometry
│   └── demo.py          # a scripted walk through every renderable state
├── persistence/
│   ├── schema.py        # the tables, and how they move forward
│   ├── records.py       # a shot becomes a row and comes back a shot
│   ├── repository.py    # every statement CaddieJ runs, and nothing else
│   ├── analytics.py     # pure deterministic aggregates over records
│   ├── database.py      # SQLite confined to one thread
│   └── service.py       # HistoryService: queued writes, fail-soft
├── video/
│   ├── cameras.py       # what a camera is: how to open it, what it delivers
│   ├── ffmpeg.py        # the only FFmpeg argument vectors, and the process seam
│   ├── layout.py        # every path the subsystem may write
│   ├── segments.py      # the rolling buffer as arithmetic
│   ├── recorder.py      # one camera, recording, restarting when it dies
│   ├── clips.py         # extraction, sidecar metadata, atomic publication
│   ├── frames.py        # stills out of a clip, for anything that wants pictures
│   ├── service.py       # VideoService: bounded queue, worker, fail-soft
│   └── fakes.py         # a fake FFmpeg, for tests that need no binary
├── coaching/
│   ├── faults.py        # what can be wrong with a golf shot, and how badly
│   ├── baseline.py      # what is normal for this player, with this club
│   ├── diagnose.py      # the one fault worth mentioning, by arithmetic alone
│   ├── templates.py     # CaddieJ's own words for every fault
│   ├── provider.py      # what a model provider is, in neutral terms
│   ├── evidence.py      # the diagnosis, rendered for a prompt
│   ├── prompt.py        # the instructions and the schema a reply must fit
│   ├── validate.py      # what CaddieJ will and will not say out loud
│   ├── cache.py         # the same swing is not paid for twice
│   ├── frames.py        # the FrameSource seam, and which moments to ask for
│   ├── factory.py       # choosing a provider from configuration
│   ├── service.py       # AnalysisService: queue, budgets, events, fail-soft
│   └── fakes.py         # scripted providers, including the demo mode
├── speech/
│   ├── audio.py         # rendered speech as PCM, and the WAV it is written as
│   ├── envelope.py      # how loud a rendered line is, moment by moment
│   ├── synth.py         # what a speech engine is, in neutral terms
│   ├── sink.py          # what an audio output is, plus one that plays nothing
│   ├── cache.py         # the same sentence is not rendered twice
│   ├── factory.py       # choosing an engine and an output from configuration
│   ├── service.py       # SpeechService: one voice, priority, interruption
│   └── fakes.py         # a voice and a speaker that exist only in the process
├── lighting/
│   ├── port.py          # what a set of lights is, plus one that changes nothing
│   ├── modulation.py    # a measurement becomes a brightness, and nothing else
│   ├── effects.py       # what the room does about a birdie, an eagle, an ace
│   ├── factory.py       # choosing a lighting system from configuration
│   ├── service.py       # LightingService: follow, budget, restore, fail-soft
│   └── fakes.py         # lights that exist only inside the process
├── effects/
│   ├── effect.py        # what an effect is: six values, and no words
│   ├── catalogue.py     # one outcome, one reaction, in one table
│   ├── channel.py       # what an output is, in neutral terms
│   ├── channels.py      # floor, speech, lights and OBS as channels
│   ├── director.py      # one at a time, and always back to normal
│   ├── obs.py           # revealing a source somebody else built
│   ├── factory.py       # choosing channels from configuration
│   └── fakes.py         # channels that only record what they were asked
├── assistant/
│   ├── contract.py      # the published models: the whole API, hand written
│   ├── reads.py         # ReadFacade: internal objects become published ones
│   ├── commands.py      # CommandFacade: the one mutation, validated
│   └── registry.py      # the tools, written out one by one
├── mcp/
│   ├── client.py        # urllib against CaddieJ's own port, and nothing else
│   ├── server.py        # MCP tools over the HTTP contract: a translator
│   └── __main__.py      # the process an assistant runtime starts
└── adapters/
    ├── asgi.py          # the uvicorn wrapper both local servers share
    ├── hud/
    │   ├── protocol.py    # wire format for the state feed
    │   ├── connections.py # per-client outbound slots, coalescing
    │   ├── server.py      # FastAPI routes and the uvicorn wrapper
    │   └── static/        # dependency-free overlay: HTML, CSS, ES modules
    ├── api/
    │   ├── guard.py       # who may ask: token, origin, host
    │   └── server.py      # the assistant API's routes, and nothing else
    ├── openai/
    │   ├── provider.py    # the only module that knows the OpenAI SDK
    │   └── errors.py      # their exceptions become the four CaddieJ acts on
    ├── kokoro/
    │   └── synthesizer.py # the only module that knows what Kokoro looks like
    ├── audio/
    │   └── sink.py        # the only module that knows what a sound card is
    ├── hue/
    │   ├── bridge.py      # the only module that speaks HTTP to a Hue bridge
    │   └── lights.py      # Hue resources become states and brightnesses
    ├── gspro/
    │   ├── wire.py        # GSPro's Open Connect format, and nowhere else
    │   ├── framing.py     # JSON objects out of an undelimited byte stream
    │   ├── translate.py   # wire values to domain values
    │   ├── assembly.py    # split shots, duplicates, debounced flags
    │   ├── interposer.py  # the byte path: forward first, observe after
    │   ├── adapter.py     # frames to domain events
    │   ├── fakes.py       # a fake simulator and a fake launch monitor
    │   └── simulation.py  # a scripted round through the real adapter
    └── obs/
        └── client.py      # ObsClient + isolated simpleobsws transport
tests/
├── unit/                # no external systems, no network
└── integration/         # opt-in: loopback port, and a real OBS instance
```

Intended target layout:

```
src/caddiej/
├── runtime/           # Application object, task supervision, signals, shutdown
├── core/              # events, bus, errors
├── domain/            # authoritative state, deterministic policy
├── ports/             # HudPort, HistoryPort (speech and lighting own theirs)
├── adapters/          # obs, hud, gspro, hue, tts, vision, ai
├── persistence/       # SQLite history (implemented)
├── assistant/         # the published contract and its façades (implemented)
└── config.py          # nested per-adapter settings
```

Dependencies point inward: adapters depend on ports and core; the domain depends
on core only; nothing in the domain imports an adapter.
