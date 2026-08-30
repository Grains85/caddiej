"""What the room does about each way a hole can finish.

This file is a table, and the table being a table is the point. Every judgement
about how much of an occasion something is lives here, in five lines, in plain
numbers somebody can argue with — and nowhere else. The director reads a
priority and a duration and knows no golf; each channel reads an intensity and
knows no golf either. Adding an event is a line here. Deciding a birdie was
oversold is a number here.

**Nothing asks a model.** Not what happened, not whether it was good, and not how
the room should respond. A language model in this path would buy nothing —
mapping five known names onto five fixed reactions is a dictionary lookup — and
would cost the two things that matter most about a celebration: it would
sometimes be wrong about what happened, and it would never be the same twice.
An ace that gets a different response each time is not a landmark, it is a
novelty. So the mapping is static, local, instant and free, and CaddieJ's use of
a model stays where it earns its place: describing a swing, which is genuinely
hard and genuinely varies.

**The escalation is the interesting part.** Par is almost nothing on purpose. If
a par gets a real reaction then a birdie has to be bigger than a real reaction,
and by the time the scale reaches a hole in one there is nowhere left to go —
which is the failure everybody who has ever built one of these has shipped at
least once. So the bottom of the scale is set almost too low, and the top is
allowed to be genuinely loud, because it will happen a handful of times in the
life of a simulator.
"""

from caddiej.domain.scoring import HoleResult
from caddiej.effects.effect import Effect, EffectPriority

EFFECTS: dict[HoleResult, Effect] = {
    # A par is most holes, so this is a nod and nothing more: the smallest
    # intensity any channel will do anything visible with, over quickly, and
    # silent. Saying "par" out loud fourteen times a round is how somebody
    # arrives at switching the whole feature off.
    HoleResult.PAR: Effect(
        cue="par",
        label="PAR",
        priority=EffectPriority.ROUTINE,
        intensity=0.15,
        seconds=1.6,
    ),
    # A good player has several of these in a round. Acknowledged properly, and
    # named out loud, but kept clearly below the tier above.
    HoleResult.BIRDIE: Effect(
        cue="birdie",
        label="BIRDIE",
        priority=EffectPriority.NOTABLE,
        intensity=0.4,
        seconds=2.4,
        say="Birdie.",
    ),
    # A genuinely good hole, and the first tier allowed to take the room from
    # something already using it.
    HoleResult.EAGLE: Effect(
        cue="eagle",
        label="EAGLE",
        priority=EffectPriority.RARE,
        intensity=0.65,
        seconds=3.2,
        say="Eagle.",
    ),
    # Most golfers never have one. Rarer than an eagle but the same class of
    # thing, so it shares the eagle's precedence and is separated by size — the
    # two cannot overlap in practice, since they are different holes.
    HoleResult.ALBATROSS: Effect(
        cue="albatross",
        label="ALBATROSS",
        priority=EffectPriority.RARE,
        intensity=0.85,
        seconds=4.0,
        say="Albatross.",
    ),
    # The one anybody phones somebody about. Everything it is allowed to have:
    # the top of the scale, the longest, and the only priority nothing outranks.
    HoleResult.HOLE_IN_ONE: Effect(
        cue="hole_in_one",
        label="HOLE IN ONE",
        priority=EffectPriority.LANDMARK,
        intensity=1.0,
        seconds=5.0,
        say="Hole in one.",
    ),
}


def effect_for(result: HoleResult) -> Effect | None:
    """The effect for a result, or None if the table has none.

    None rather than a default, because a missing entry means somebody added a
    result and did not decide what the room should do about it. Inventing a
    reaction would hide that; doing nothing and saying so in a log does not.
    """

    return EFFECTS.get(result)
