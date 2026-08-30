# Lighting

[← Documentation index](README.md)

> **Not yet run against a real Hue bridge.** The rate limit, the restore and the
> failure handling are developed against a stub bridge. See
> [Project status](../README.md#project-status).

While CaddieJ talks, the room can react — a very slight dip in the gaps between
words, back to normal on them. The brightness comes from a measurement of the
audio that is about to be played, so the lights follow the actual words rather
than a timer or a guess.

It is **off by default**, and for a different reason from everything else here:
this is the only feature that reaches out and changes something in your house
that you set the way you wanted it. It gives the room back when it stops talking,
and letting it touch your lights at all should still be your decision.

```shell
pip install -e '.[hue]'
```

You need a bridge address and an application key. The key comes from a one-time
pairing step: press the button on the bridge, then within thirty seconds run

```shell
curl -k -X POST https://YOUR_BRIDGE_IP/api \
     -d '{"devicetype":"caddiej#floor","generateclientkey":true}'
```

and copy the `username` value out of the answer. Put both in `.env`:

```
LIGHTING_ENABLED=true
LIGHTING_BRIDGE_HOST=192.168.1.40
LIGHTING_APPLICATION_KEY=the-key-from-above
LIGHTING_LIGHTS=Sitting room lamp, Corner light
```

`LIGHTING_LIGHTS` takes names as shown in the Hue app, or bridge identifiers,
separated by commas. Leaving it blank means every light in the house, which is
convenient and probably not what you want.

The key is a credential. It lives in `.env`, which git ignores, and nothing logs
it.

## How much it moves

Two settings, both fractions of whatever your lights were already at — so the
same numbers are subtle in a room at twenty per cent and in one at full daylight:

```
LIGHTING_MINIMUM=0.82
LIGHTING_MAXIMUM=1.0
```

The default dips by not quite a fifth at the quietest point of a sentence and
returns to normal at the loudest. Lower `LIGHTING_MINIMUM` for something more
obvious; set the two equal to switch the reaction off while leaving everything
else wired up.

It dims rather than brightens on purpose: dimming has room to work whatever your
lights are set to, while brightening does nothing at all in a room already at
full.

## Respecting the bridge

A Hue bridge tolerates about ten commands a second in total. CaddieJ works out
how often to write from that budget and how many lamps you listed, so four lamps
get a slower, gentler reaction rather than a bridge that starts refusing
commands. Each command asks the bridge to take as long as the gap to the next
one, so the bridge draws the curve between them and a handful of updates a second
looks continuous.

`/healthz` reports how many commands were sent, how many were skipped as too
small to see, and how many failed.

## One gesture, on request

Anything else the lights do arrives as a *flourish*: a depth and a duration, and
nothing else. This subsystem does not know what a birdie is — see
[Celebrations](celebrations.md) for the part that does. A flourish outranks a
sentence and keeps the room until it finishes.

## When it cannot reach the bridge

A wrong address, a revoked key, an unplugged lamp, a timeout: all of them mean
the lights do nothing, and nothing else changes. Speech still speaks, the shot
still reaches the floor and the database. If the bridge fails a few times in a
row it is left alone for a minute and then tried again, so an appliance that has
been unplugged does not cost a timeout every sentence for the rest of the
evening. Putting the room back is retried once before it is given up on, because
the one failure somebody in the room would actually notice is the lights staying
dim.

To see the whole thing work with no bridge at all — the measurement, the timing,
the rate limit, the restore, all of it, changing no lamp:

```shell
LIGHTING_ENABLED=true LIGHTING_OUTPUT=none SPEECH_ENABLED=true \
  SPEECH_ENGINE=fake COACHING_PROVIDER=scripted GSPRO_MODE=simulate caddiej
```
