# Celebrations

[← Documentation index](README.md)

When a hole finishes, one thing happens across every output at once: a word and
expanding rings on the floor, a short line in CaddieJ's voice, a dip in the room
lights, and a source switched on in OBS. There are five, escalating:

| Hole | On the floor | Said | How much of an occasion | For |
| --- | --- | --- | --- | --- |
| Par | `PAR` | nothing | barely there | 1.6s |
| Birdie | `BIRDIE` | "Birdie." | noticeable | 2.4s |
| Eagle | `EAGLE` | "Eagle." | loud | 3.2s |
| Albatross | `ALBATROSS` | "Albatross." | louder | 4.0s |
| Hole in one | `HOLE IN ONE` | "Hole in one." | everything | 5.0s |

A par is deliberately almost nothing. If a par got a real reaction, a birdie
would have to be bigger than a real reaction, and by the time the scale reached a
hole in one there would be nowhere left to go.

**Nothing triggers these yet.** Recognising a birdie needs a hole, a par and a
stroke count, and GSPro's Open Connect feed carries shots and a club, not a
scorecard. The wiring exists so that when scoring does, the room reacts without
anything changing near a projector.

## Nothing about it is guessed

No model decides what happened, whether it was good, or how loud the response
should be. Five known names map onto five fixed reactions in one local table, by
arithmetic, with no network involved. A birdie looks the same on its fifth
appearance as its first — which is the difference between a landmark and a
novelty — and the response can never be wrong about what happened, because it did
not have an opinion.

## One at a time, and always back to normal

- **Exactly one effect runs.** A more important one takes over immediately; an
  equally or less important one is refused, because swapping one celebration for
  another half way through reads as a bug.
- **Nothing is queued.** An eagle arriving forty seconds late over whatever the
  player is doing now is not a late celebration, it is a wrong one.
- **Everything is capped.** Durations are clamped and every call into an output is
  timed out, so neither a mistake in the table nor an OBS that stops answering
  can hold the room.
- **Every output is put back**, at the end of every effect, whether or not that
  output was reached and whether or not it failed on the way in. The floor's
  celebration is a layer over whatever the round already had on it, so clearing it
  leaves the marker, the club path and the numbers exactly as they were.

The voice is the one exception, and only in the obvious direction: a word already
spoken cannot be taken back, so a cancelled effect lets the line finish rather
than clipping a syllable.

## The visuals are deliberately plain

Rings and one word, drawn in CSS and SVG at whatever size the surface is. No
media is downloaded and nothing here is anybody else's work — which is also why
it is restrained enough to survive being seen four hundred times.

If you want a real broadcast graphic, build it as a source in OBS and name it:

```shell
EFFECTS_OBS=true EFFECTS_OBS_SCENE=Golf EFFECTS_OBS_SOURCE="Birdie Stinger" \
  EFFECTS_OBS_TEXT_SOURCE="Celebration Text"
```

CaddieJ makes that source visible for the length of the effect, sets the caption,
and hides it again. What the source contains is entirely yours.

## Seeing them without a scorecard

Each channel can be switched off on its own — `EFFECTS_FLOOR`, `EFFECTS_SPEECH`,
`EFFECTS_LIGHTS`, `EFFECTS_OBS` — and the whole subsystem with `EFFECTS_ENABLED`.
The floor demo cycles through all five celebrations along with every other
renderable state, so the projection can be judged with no round in progress:

```shell
FLOOR_DEMO=true caddiej
```

`/healthz` reports an `effects` block: what is playing, how many effects have
run, how many were refused or replaced, and how many failures happened —
counting failures to put an output *back* separately, since that is the only kind
anybody in the room would see.
