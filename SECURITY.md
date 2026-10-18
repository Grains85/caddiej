# Security

CaddieJ runs on one machine in one room and is not designed to be exposed to a
network. This document says what it opens, what it holds, and what to do if you
find something wrong.

## What CaddieJ exposes

| Surface | Default | Authentication |
| --- | --- | --- |
| Floor overlay and `/healthz` | `127.0.0.1:8730`, always on | none |
| Assistant API | `127.0.0.1:8731`, **off** | bearer token, required |
| GSPro bridge | `127.0.0.1:9210`, **off** | none; it forwards a launch monitor to GSPro |

The overlay has no authentication because it is a rendering client for a
projector on the same machine, and it holds nothing an attacker on the loopback
interface could not already read. It should not be bound to anything else.
`HUD_HOST` will let you, and you should not.

The assistant API is stricter, because it answers questions about what somebody
did in their own house and can be given a route that changes state. It refuses to
start without a token, and refuses to start on a non-loopback address — both are
refusals rather than warnings. To reach it from another machine, tunnel over SSH
rather than binding it wider. Writes are a second, separate decision
(`ASSISTANT_API_ALLOW_MUTATIONS`), and with them off the mutating route is not
registered at all.

## Credentials

Four settings hold one: `OBS_PASSWORD`, `COACHING_API_KEY`,
`LIGHTING_APPLICATION_KEY` and `ASSISTANT_API_TOKEN`.

- They live in `.env`, which is git-ignored. Do not commit them.
- They are `SecretStr`, so they do not appear in a traceback or a repr.
- They cannot reach a log. Structured logging emits an explicit allowlist of
  context fields, and a test fails the build if any allowlist entry is named
  like a credential or if any code logs a field that is not on it.
- `caddiej --check-config` reports whether a credential is set, never what it is.

If you think one has leaked into a log, that is a bug worth reporting.

## What leaves the machine

By default, nothing. Shot history, swing video and rendered speech are all local
files. Two settings send data outward, both off by default and both documented
where they are set:

- `COACHING_PROVIDER=openai` with `COACHING_VISION_CALLS=true` sends the
  selected swing frames and the Ideal Swing assessment request to the
  configured endpoint. Vision calls are off unless that switch is set.

## Reporting a vulnerability

Open a GitHub issue for anything that is not sensitive — a missing timeout, a
setting that is unsafe by default, a credential in an unexpected place.

For something that should not be public, use GitHub's **Report a vulnerability**
button on the Security tab, which opens a private advisory. Please include what
you did, what happened, and the CaddieJ version.

This is a small hobby project with no security team and no service-level promise.
Expect a considered reply rather than a fast one.
