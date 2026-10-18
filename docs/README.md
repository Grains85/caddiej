# CaddieJ documentation

[← Back to the project](../README.md)

## Using it

- **[Configuration reference](configuration.md)** — every setting, what it
  defaults to, and how settings are read.
- [The floor overlay](floor.md) — what the projection renders, the demo,
  OBS as compositor, and the opt-in ProTee Shot Video source.
- [GSPro and a launch monitor](gspro.md) — passive observation of ProTee's log
  and GSPro currentRound.dat, plus the experimental Open Connect interposer.
- [First hardware validation](hardware-validation.md) — the first real
  ProTee VX + GSPro session: minimal `.env`, Windows commands, health
  fields, and what only hardware can answer.
- [Windows validation](windows-validation.md) — clean environment, OpenCV and
  MediaPipe, a saved swing, then the live coach. Paid vision calls stay off
  until the last, optional step.
- [Shot history](history.md) — the SQLite file, what is in it, and what is not.

## Optional integrations

Each of these is off unless switched on, and CaddieJ runs the same way without
it. None is required to hit a ball and see it on the floor.

- [Swing video](video.md) — a clip of every swing, cut around impact. Needs
  FFmpeg.
- [Swing capture](swing-capture.md) — stills of the two Swing Video panels
  ProTee already draws, read back through OBS. Needs two cropped OBS sources.
- [Coaching](coaching.md) — Ideal Swing 2.9, from a captured swing to one
  spoken correction.
- [Swing reference](swing-reference.md) — the live path, the evidence budget,
  and how to install MediaPipe. Vision calls stay off until
  `COACHING_VISION_CALLS=true`.
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
