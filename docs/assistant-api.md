# CaddieJ Assistant API

[← Documentation index](README.md)

The surface an outside assistant uses to ask CaddieJ about a golf session. Six
tools: five that read, one that changes what coaching leans towards.

This document is the reference for the tools themselves. The reasoning behind the
design is [ARCHITECTURE.md](../ARCHITECTURE.md) §14. The source of truth for the
shapes is `caddiej/assistant/contract.py`, and for the tool list
`caddiej/assistant/registry.py`.

> **Not yet driven by a real MCP client**, only by CaddieJ's own client over a
> real socket. See [Project status](../README.md#project-status).

## What this is, and what it is not

It is a small, versioned, authenticated read surface over a golf simulator,
plus one preference an assistant may set.

It is not a remote control. Nothing here can start a round, change a club,
publish an event, alter a measurement, move the floor, speak, touch the lights,
or influence how a shot is handled. Those are the deterministic core's business,
and the boundary is arranged so that an assistant is structurally unable to
reach them rather than merely discouraged from trying.

CaddieJ is complete without it. The API is off by default, it starts after the
room is already working, and if it fails to start CaddieJ logs the failure and
carries on serving the floor. Nothing in the simulator waits for an assistant,
ever.

## The shape of it

Three layers, and two processes:

```
an assistant  --MCP over stdio-->  caddiej.mcp        (its own process)
                                        |
                                   loopback HTTP, bearer token
                                        |
                                   caddiej.adapters.api   (inside CaddieJ)
                                        |
                                   ReadFacade / CommandFacade
                                        |
                                   history, coaching, play
```

The MCP process is a translator and nothing else. It imports exactly two CaddieJ
modules — `assistant.contract` and `assistant.registry`, neither of which
imports anything else from CaddieJ — so it holds no database handle, no OBS
connection, no API key, and no way to reach anything but one authenticated port.
A test asserts that import graph, because it is the whole isolation argument.

MCP is not required. The HTTP surface is the contract, and anything that can send
an authenticated request can use it: `curl`, a shell script, a home-automation
box, a different protocol translator. Nothing in CaddieJ names a particular
assistant, and no specific assistant repository is a dependency of anything here.

## Turning it on

Three settings, at least. See `.env.example` for all of them.

```shell
ASSISTANT_API_ENABLED=true
ASSISTANT_API_TOKEN=<a long random string>
ASSISTANT_API_ALLOW_MUTATIONS=false
```

Generate a token with something that is actually random:

```shell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

There is no default token and none is generated at start-up, because an invented
token would have to be printed or written somewhere for an assistant to find it.
With the API enabled and the token blank, CaddieJ refuses to open the port and
says so in the log. A non-loopback `ASSISTANT_API_HOST` is refused the same way.

CaddieJ then answers on `http://127.0.0.1:8731/v1/`, and logs
`assistant_api_listening` with the URL to point a client at.

## Connecting an assistant over MCP

Install the extra in whatever environment the assistant runtime starts processes
from, and configure it as an stdio MCP server:

```shell
python -m pip install -e ".[mcp]"
```

```json
{
  "mcpServers": {
    "caddiej": {
      "command": "caddiej-mcp",
      "env": {
        "CADDIEJ_API_URL": "http://127.0.0.1:8731/v1",
        "CADDIEJ_API_TOKEN": "the same token as ASSISTANT_API_TOKEN"
      }
    }
  }
}
```

`caddiej-mcp` and `python -m caddiej.mcp` are the same program. It reads four
environment variables and no `.env` file, because it is started by somebody
else's runtime from whatever directory that runtime happens to be in:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CADDIEJ_API_TOKEN` | — | Required. Must match `ASSISTANT_API_TOKEN`. |
| `CADDIEJ_API_URL` | `http://127.0.0.1:8731/v1` | Where CaddieJ answers. |
| `CADDIEJ_API_TIMEOUT` | `5` | Seconds per request. |
| `CADDIEJ_MCP_READ_ONLY` | unset | Set to `1` to drop the mutating tool. |

It starts whether or not CaddieJ is running. An assistant runtime launches this
at login and keeps it for the session, so refusing to start because a golf
simulator is not switched on would mean the tools existed only if the room
happened to be in use at the time.

## Availability, which matters more than any single field

Every answer carries `availability`:

| Value | Meaning | What to say |
| --- | --- | --- |
| `ok` | The answer is the answer. | Answer the question. |
| `empty` | CaddieJ is working; there is genuinely nothing yet. | Nobody has hit a ball, or nothing has been coached. |
| `unavailable` | The subsystem is off, failed to open, or errored. | Say CaddieJ could not tell you — not that there is no golf. |

Reporting `unavailable` as `empty` is the most likely way this boundary produces
a wrong answer: an assistant telling somebody they have not practised today when
the truth is that a database file was locked. The distinction is decided once, in
`ReadFacade`, rather than inferred by each caller from a null.

## The tools

Paths are relative to `http://127.0.0.1:8731/v1`. Units are in field names
throughout, and a null measurement means the device did not measure it — never
zero.

### `get_caddiej_status` — `GET /status`

Whether CaddieJ is running and which of its parts work. Takes no arguments,
always answers `ok`, and is the tool to call when anything else is degraded.

| Field | Type | Notes |
| --- | --- | --- |
| `contract_version` | integer | Matches the `v1` in the path. |
| `caddiej_version` | string | |
| `uptime_seconds` | number | |
| `session_id` | string, null | Null when history is off or failed to open. |
| `player_name` | string, null | A label the operator configured. There is no roster. |
| `training_focus` | string, null | The current preference, if any. |
| `subsystems` | map of `{enabled, available}` | Two booleans per subsystem, no counters. |
| `capabilities` | list of string | Which tools will return data right now. |

`session_id` is present from start-up while `get_session_stats` for that same
session is still `empty`. Both are correct: a session is prepared at start-up
and its row is written with the first shot, so a run nobody hit a ball in leaves
nothing behind. Read `capabilities` before assuming a tool will return data.

### `get_last_shot` — `GET /last-shot`

The most recent shot of a session.

Arguments: `session_id` (string, optional) — a session UUID. Omit for the
session in progress.

| Field | Type | Notes |
| --- | --- | --- |
| `shot` | object, null | Absent unless `availability` is `ok`. |
| `shot.shot_id` | string | |
| `shot.ordinal` | integer | Which shot of the session, counting from one. |
| `shot.observed_at` | timestamp | UTC, when the launch monitor saw it. |
| `shot.club` | string, null | `driver`, `iron`, `putter`… Never a vendor's own code. |
| `shot.handedness` | string | |
| `shot.ball` | object, null | `speed_mph`, `launch_angle_degrees`, `launch_direction_degrees`, then `carry_yards`, `total_spin_rpm`, `back_spin_rpm`, `side_spin_rpm`, `spin_axis_degrees`. |
| `shot.club_delivery` | object, null | `speed_mph`, `angle_of_attack_degrees`, `path_degrees`, `face_to_target_degrees`, `face_to_path_degrees`, `loft_degrees`, `impact_horizontal_mm`, `impact_vertical_mm`. |

Most club-delivery fields are null on most devices, because most launch monitors
do not measure them. `face_to_path_degrees` is computed by CaddieJ and is the
number that explains which way the ball curved.

There is no device identifier, no device session, no sequence number and no raw
payload, and there never will be. Those are diagnostics for somebody holding the
launch monitor.

### `get_session_stats` — `GET /session-stats`

How a session has gone.

Arguments: `session_id` (string, optional).

| Field | Type | Notes |
| --- | --- | --- |
| `session_id`, `label`, `started_at` | | |
| `shots` | integer | |
| `first_shot_at`, `last_shot_at` | timestamp, null | |
| `overall` | list of metric | In CaddieJ's own metric order, so two calls agree. |
| `by_club` | list of `{club, shots, metrics}` | Busiest club first. |
| `faults` | map of string to integer | How many shots each named fault accounted for. |

A metric is `{name, unit, count, mean, median, minimum, maximum,
standard_deviation}`. `count` is how many shots carried that measurement, not
how many shots were hit: devices report different subsets, and a device that
reports carry for drives and not for putts would otherwise look like it had hit
some very soft drives.

`faults` leaves out shots with nothing wrong, so an empty map after a long
session means it went well.

Shot-by-shot dispersion is deliberately absent. It is one row per shot, it
exists so something can draw a picture, and four hundred of them in a language
model's context window is an expensive way to answer "how am I hitting it".

### `get_current_club` — `GET /current-club`

Live state rather than history. Takes no arguments and is never `unavailable`:
this lives in the domain, in CaddieJ's own process.

| Field | Type | Notes |
| --- | --- | --- |
| `club` | string, null | `empty` before a round starts and after a link drops. |
| `ball_on_mat` | boolean, null | Null when nothing knows. |
| `launch_monitor_ready` | boolean, null | |
| `handedness` | string, null | Survives a link loss. |

`ball_on_mat` and `launch_monitor_ready` are null rather than false when no
device reports them. A device that says nothing is not a device saying no.

### `get_recent_coaching` — `GET /recent-coaching`

The last few things CaddieJ concluded about the player's swing, oldest of the
recent ones first.

Arguments: `limit` (integer, 1 to 50, default 5), `session_id` (string,
optional).

| Field | Type | Notes |
| --- | --- | --- |
| `prose_included` | boolean | Whether `display` and `speech` are populated. |
| `lines` | list | Each `{shot_id, focus, severity, band, source, created_at, display, speech}`. |

`severity` runs from zero to one and `band` is CaddieJ's own reading of it —
`fine`, `watch` or `fault` — so an assistant does not have to invent thresholds.
A severity in the `fine` band was not worth mentioning to the player and should
not be presented as a problem. A `focus` of `none` means the shot had nothing
wrong with it, so a run of them is a compliment rather than missing data.
`source` is `template` if CaddieJ wrote the words and `model` if a language
model did.

`display` and `speech` are null unless `ASSISTANT_API_INCLUDE_COACHING_PROSE` is
on, and `prose_included` says which. With the gate shut the named faults and
severities are still there, which is enough to say what somebody has been
fighting without reproducing a transcript of their practice session. The clip
path is never included either way: an assistant that could read one would be a
file oracle rather than a caddie.

### `set_training_focus` — `POST /training-focus`

The one mutation. Present only when `ASSISTANT_API_ALLOW_MUTATIONS` is on — the
route is not registered otherwise, and the MCP server does not offer the tool,
so a read-only CaddieJ is discovered by not being given a way to write.

Arguments: `focus` (string or null). One of `face_control`, `path`, `strike`,
`low_point`, `speed`, `launch`, `spin`, `consistency`, `tempo`, `posture`,
`early_extension`, `sway`. Null clears the preference.

| Field | Type | Notes |
| --- | --- | --- |
| `training_focus` | string, null | The new value. |
| `previous_focus` | string, null | So an assistant can say what it changed. |
| `changed` | boolean | False when it was already set to that. |

It biases which fault coaching leans towards on subsequent shots. It does not
alter measurements, scoring, what the floor shows, or anything in flight, and a
shot with a genuinely worse fault is still coached on that fault. Setting it
twice with the same value is harmless.

A misspelling is refused, and the refusal names the legal values — twice, in
fact: the MCP process checks before spending a round trip, and CaddieJ checks
again because it is the authority.

### `GET /tools`

Not an MCP tool. Reports the contract version and the registry entry for every
tool this deployment actually serves, so a client can check what it is talking
to before it calls anything. Mutating tools appear only when they are registered.

## Errors

Every refusal has the same shape — `{"error": "...", "detail": "..."}` — whatever
caught it:

| Status | `error` | Cause |
| --- | --- | --- |
| 400 | `invalid_argument` | A malformed `session_id`, a `limit` out of range, an unknown focus, an argument this API does not have. |
| 401 | `missing_token`, `bad_token` | No bearer token, or the wrong one. |
| 403 | `browser_origin` | The request carried an `Origin` header. |
| 403 | `foreign_host` | The `Host` header does not name this machine. |
| 404 | `no_such_tool` | A mutating path on a read-only deployment, or a path this version does not serve. |
| 503 | `no_token_configured` | The API is running with no token. Fails closed. |

`detail` names the argument at fault, because an assistant told which argument
was wrong can correct itself while one given a status code tells the user CaddieJ
failed. It is always CaddieJ's own text: never an exception message, never a
traceback, and never an echo of what was sent.

An unrecognised argument is a refusal rather than something quietly ignored. A
caller sending one believes something untrue about this API, and accepting it is
how an assistant comes to think it turned a feature on.

Through MCP these arrive as tool errors with the same sentences, plus one the
HTTP layer cannot produce: a CaddieJ that is not running is reported as not
running, so an assistant says "the simulator is not started" rather than "you
have no golf data".

## Security

Loopback is necessary and nowhere near sufficient. Any web page the operator
visits can make requests to `http://127.0.0.1:8731/`, because a browser will
resolve an attacker's hostname to loopback and then treat the result as that
attacker's origin. So there are three gates, each covering a different way in:

1. **A bearer token**, compared in constant time. The real defence. It is in a
   header rather than a URL, so it does not land in a log or a referrer.
2. **No `Origin` header at all.** An MCP server or `curl` sends none; a browser
   always sends one, so the presence of the header is itself the signal. This
   closes the browser path even if a token leaked.
3. **A loopback `Host`.** Cheap, and it refuses a request that arrived claiming
   to be for somebody else's hostname.

No CORS middleware is installed — not permissive, not restrictive, absent. There
is no browser client for this API and adding the headers would only help build
one. There is no OpenAPI schema either: this document and the contract module are
the description, and a generated second one would drift from them.

To reach the API from another machine, tunnel to it over SSH. Binding it to a
non-loopback address is refused at start-up.

Three things never cross the boundary, whatever the settings say: any secret
(the formatter's allowlist has no field that could carry one), any path on the
operator's disk, and any launch monitor diagnostic. Coaching prose and camera
frames are each behind their own switch, both off by default.

## Versioning

`CONTRACT_VERSION` is a major version and appears in the URL path. Within one
major, only additive changes are allowed: an optional field with a default, or a
new enum member that clients must tolerate. Renaming, removing, retyping or
narrowing anything is a new major. Golden files in
`tests/unit/test_assistant_contract.py` hold the serialised shape of every
model, so breaking one of those rules fails a test rather than somebody's
evening.

## Adding a tool

Deliberately several steps, in this order:

1. Add or extend a model in `assistant/contract.py`, additively.
2. Add the method to `ReadFacade` or `CommandFacade`, projecting internal
   objects field by field. Never return an internal type.
3. Write the `ToolSpec` into `assistant/registry.py`. The registry is a literal
   list, not reflection over the façades, so a new façade method is not callable
   from outside until somebody types it in here.
4. Add the route in `adapters/api/server.py`, taking its path from the registry.
5. Add the handler in `mcp/server.py`.
6. Update this document.

The `description` in a `ToolSpec` is not documentation about code. It is the
prompt: it is what a language model reads to decide whether to call the tool and
how to read the answer, so it is written for that reader — what the tool tells
you, when it will have nothing to say, and what it deliberately does not cover.
