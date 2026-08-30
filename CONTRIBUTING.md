# Contributing to CaddieJ

Thanks for your interest in CaddieJ. This guide covers how to set up the
project, the rules a change has to follow, and how to get it reviewed.

Read [ARCHITECTURE.md](ARCHITECTURE.md) first. CaddieJ has a small number of
hard architectural rules, and the fastest way to have a pull request rejected is
to cross one of them without realizing it. [ROADMAP.md](ROADMAP.md) shows what
is being built next and what is deliberately deferred.

Contributions are accepted under the project's [MIT licence](LICENSE).

The most useful contribution right now is not code. Almost nothing in CaddieJ
has been run against real hardware — see [Project status](README.md#project-status)
— so an issue reporting what happened when you pointed a real launch monitor, a
real camera, or a real Hue bridge at it is worth more than a feature.

---

## Development setup

Requires Python 3.12 or newer.

```shell
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -e ".[test]"
```

Copy the example configuration and fill in local values:

```shell
cp .env.example .env                   # Windows: copy .env.example .env
caddiej --check-config
```

`.env` is git-ignored and must stay that way. Never commit credentials. The
check reports what CaddieJ would run with and which optional dependencies are
installed, and prints no credential — only whether one is set. Every setting is
listed in [docs/configuration.md](docs/configuration.md); a new one has to be
added to `.env.example` and that reference too, and a test fails if it is not.

## Running the tests

Unit tests require no external systems and must always pass:

```shell
pytest tests/unit
```

Integration tests are skipped by default and only run when explicitly enabled.
They need a running, configured OBS instance:

```shell
CADDIEJ_RUN_INTEGRATION=1 pytest -m integration
```

The default `pytest` invocation runs everything, with integration tests
reporting as skipped unless the environment variable is set. That default run is
what CI runs, on Linux, macOS, and Windows across Python 3.12 and 3.13, so a
pull request is expected to keep it green. Integration results should be
mentioned in the description when the change touches an adapter, since CI
cannot run them.

A clean clone is also expected to pass:

```shell
ruff check .
ruff format --check .
mypy
caddiej --version
caddiej --check-config
```

`mypy` is the type checker. It was chosen over Pyright because this tree uses
Protocol method stubs (`def name(self) -> str: ...`) throughout; Pyright treats
those as missing returns and would need a suppression per method, while mypy
understands them. The check covers `src/caddiej` under `strict`. Optional
SDKs (`openai`, `kokoro`, `sounddevice`, `mcp`) and the untyped `simpleobsws`
package are ignored per-module in `pyproject.toml`, not with `# type: ignore`
in source.

---

## Architectural rules

These are not stylistic preferences. A change that violates one of them will be
asked to change approach, not just implementation.

### 1. Nothing in the real-time path may depend on an LLM

Shot ingestion, state updates, HUD content, and scoring are deterministic. No
language model, no network round trip, no unbounded wait. Real-time text —
displayed or spoken — comes from templates, not generated prose. AI features are
strictly after-the-fact subscribers.

The test to apply to your change: *if this component hangs for thirty seconds,
can the player still hit the next shot and see correct information?*

### 2. CaddieJ owns simulator state

Adapters translate external messages into CaddieJ events and stop there. If your
adapter code needs to know what a shot *means*, that logic belongs in the domain
instead. Output surfaces render domain state; they never hold their own.

### 3. Third-party libraries stay inside their adapter

Each external system gets one adapter under `caddiej/adapters/<system>/`, and its
client library is confined to a transport class inside it. Vendor protocol
details — request names, wire formats, vendor exceptions — must not escape.

Today `simpleobsws` appears in exactly one file. Keep it that way, for every
future dependency too.

### 4. Adapters are injectable and testable without the real system

Accept a transport factory, as `ObsClient` does, so unit tests run without OBS,
GSPro, a Hue bridge, or a camera.

### 5. Failure degrades, it does not stop play

Enrichment failures — voice, lights, cameras, AI — are logged and ignored. Every
external interaction has a timeout. Recovery re-applies current state rather
than replaying missed history.

### 6. No new infrastructure

No Redis, Docker, external database, or message broker. SQLite and the standard
library are the intended persistence story. Prefer maintained upstream libraries
over new code; do not vendor, fork, or patch dependencies.

### 7. Secrets stay out of git, logs, events, and persistence

Credentials are `SecretStr` in configuration. Structured logging emits an
explicit set of context fields rather than arbitrary objects. Events are recorded
and may be replayed, so a secret placed in an event becomes a secret written to
disk indefinitely.

---

## Adding a new adapter

A checklist, in the order that tends to work:

1. Confirm the roadmap milestone. If the integration is in the "not planned yet"
   list, open an issue to discuss before writing code.
2. Define what the domain needs as a port, not what the vendor API offers.
3. Create `caddiej/adapters/<system>/` with a transport class holding the client
   library and a CaddieJ-facing class exposing CaddieJ verbs.
4. Translate vendor errors into the CaddieJ error hierarchy, distinguishing
   *unreachable* from *request rejected*.
5. Add nested configuration for the adapter, with secrets as `SecretStr` and
   separate connect and request timeouts.
6. Write unit tests using an injected fake transport, covering request mapping
   and the failure paths.
7. Add an opt-in integration test, skipped unless explicitly enabled.
8. Document the adapter's tier — deterministic or enrichment — and its
   degradation behavior.

---

## Code style

- Python 3.12+ with modern type hints throughout. No `typing.Optional`,
  `typing.List`, or similar legacy aliases.
- `async`/`await` for I/O. Nothing blocking on the event loop; CPU-bound work
  goes to a process pool, not a thread.
- Structured logging with event-style names and context fields. No `print`.
- Comments explain constraints and non-obvious intent. They do not narrate what
  the code does, and they do not explain a change to a reviewer — that belongs in
  the commit message or pull request.
- Keep modules independently testable. If a unit test needs three collaborators
  wired together, the seams are probably wrong.

## Tests

- Every behavioral change needs a test. Bug fixes get a test that fails before
  the fix.
- Unit tests must not require an external system, a real network, or real
  waiting. Inject a clock rather than sleeping.
- Integration tests must be marked `integration` and skipped unless
  `CADDIEJ_RUN_INTEGRATION=1` is set, and must leave the external system in the
  state they found it.
- Test the boundary you own: request mapping, error translation, and degradation
  behavior are the parts that regress.

## Commits and pull requests

- Small, focused commits. One concern per commit.
- Write commit messages that explain *why*, not a restatement of the diff.
- Pull requests should state which roadmap milestone the change serves, which
  architectural tier it touches, and how it was verified — including whether
  integration tests were run against real hardware or software.
- Do not mix a refactor with a behavioral change. Split them.
- If a change requires relaxing an architectural rule, say so explicitly and
  explain why. Sometimes the rule needs a documented exception; that discussion
  should be visible, not buried in an implementation.

## Reporting issues

Useful bug reports include the CaddieJ version or commit, Python version, the
operating system, the versions of the relevant external systems — OBS and OBS
WebSocket, GSPro, ProTee — and structured log output with secrets removed.

For integration failures, state whether the external system was reachable
independently, since that separates a transport problem from a rejected request.
