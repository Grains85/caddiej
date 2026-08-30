"""Letting the room react to what happened, deterministically and locally.

Something finishes a hole, one table says what that is worth, and one director
sends it to every output at once: the projected floor, the voice, the lights, and
a source in OBS. Nothing about it is decided at runtime and nothing about it
leaves the machine.

**No model is asked, about anything.** Not what happened, not whether it was
good, not how loud the response should be. That is not a limitation of this
milestone; it is the design. The mapping from five known names to five fixed
reactions is a dictionary lookup, so a model would add latency and an API bill
to buy nothing — and would cost the two properties a celebration actually needs.
It would occasionally be wrong about what happened, which is unforgivable when
the input is a number somebody can see. And it would never be the same twice,
which is the difference between a landmark and a novelty: the fifth birdie of a
round should feel like the first four, and the one hole in one somebody ever hits
should feel like the thing the room was always going to do.

**The invariant.** At most one effect at a time; every effect ends; every output
is put back. See `director.py`, which is mostly about the third of those.

**What is deliberately not built.** No queue of pending effects, because an
effect is a moment. No composition of two effects, because two celebrations at
once is not a feature. No per-channel tuning in the catalogue, because one dial
per effect is what keeps the table a set of judgements rather than a settings
file. And nothing reacts to a bad shot: the vocabulary in
`caddiej.domain.scoring` cannot name one, which is what stops that from being
added by accident.
"""

from caddiej.effects.catalogue import EFFECTS, effect_for
from caddiej.effects.channel import EffectChannel
from caddiej.effects.channels import FloorChannel, LightsChannel, SpeechChannel
from caddiej.effects.director import EffectDirector, EffectsHealth
from caddiej.effects.effect import Effect, EffectPriority
from caddiej.effects.factory import build_effect_director, obs_clients
from caddiej.effects.obs import ObsChannel

__all__ = [
    "EFFECTS",
    "Effect",
    "EffectChannel",
    "EffectDirector",
    "EffectPriority",
    "EffectsHealth",
    "FloorChannel",
    "LightsChannel",
    "ObsChannel",
    "SpeechChannel",
    "build_effect_director",
    "effect_for",
    "obs_clients",
]
