# CaddieJ documentation

[← Back to the project](../README.md)

## Using it

- **[Configuration reference](configuration.md)** — every setting, what it
  defaults to, and how settings are read.
- [The floor overlay](floor.md) — what the projection renders, the demo, and
  using OBS as the compositor.
- [GSPro and a launch monitor](gspro.md) — how CaddieJ sees shots, and how to
  play a round without either.
- [First hardware validation](hardware-validation.md) — the first real
  ProTee VX + GSPro session: minimal `.env`, Windows commands, health
  fields, and what only hardware can answer.
- [Shot history](history.md) — the SQLite file, what is in it, and what is not.

## Optional integrations

Each of these is off unless switched on, and CaddieJ runs the same way without
it. None is required to hit a ball and see it on the floor.

- [Swing video](video.md) — a clip of every swing, cut around impact. Needs
  FFmpeg.
- [Coaching](coaching.md) — naming the one thing worth fixing, and optionally
  letting a model word it. Needs `.[ai]` only for the second part.
- [Speech](speech.md) — saying the coached line out loud, locally. Needs
  `.[tts]`.
- [Lighting](lighting.md) — the room reacting while CaddieJ talks. Needs
  `.[hue]`.
- [Celebrations](celebrations.md) — one reaction across every output when a hole
  finishes.
- [Assistant API](assistant-api.md) — letting an outside assistant ask about a
  session, over MCP or plain HTTP. Needs `.[mcp]` only for the translator.

## Understanding it

- [Architecture](../ARCHITECTURE.md) — the design, the boundaries, and the rules
  every component follows. Sections are marked **Implemented** or **Intended**.
- [Roadmap](../ROADMAP.md) — verifiable milestones and what is deliberately
  deferred.
- [Contributing](../CONTRIBUTING.md) — development setup, the architectural
  rules a change has to follow, and how to add an adapter.
- [Security](../SECURITY.md) — what CaddieJ exposes, and how to report a
  vulnerability.
