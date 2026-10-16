"""The words CaddieJ can produce without asking anything.

Every line here is deterministic, offline and instant, which is what makes the
whole analysis subsystem safe to depend on: when a model is missing, slow, over
budget or talking nonsense, this is what the player reads, and it is a correct
statement about a measurement rather than an apology.

Two rules about the phrasing.

**No left and no right.** "Swing more left" is wrong for half the people who will
ever read it, and the mirroring that fixed the measurements cannot fix a word.
Every instruction is expressed as something to feel or do, not a direction.

**No numbers.** The measurements are already on the floor beside this line. A
sentence restating them competes with them, and a sentence rounding them
differently contradicts them.
"""

from collections.abc import Mapping

from caddiej.domain.coaching import AdviceKind, CoachingFocus

# Displays for the faults no measurement can rank, so a model that was given
# frames and chose one of them still produces a line the floor can draw.
# Kept to two words, like every other display: this is read at a glance from
# standing height, not studied.
OBSERVED_DISPLAYS: Mapping[CoachingFocus, str] = {
    CoachingFocus.TEMPO: "CHECK TEMPO",
    CoachingFocus.POSTURE: "CHECK POSTURE",
    CoachingFocus.EARLY_EXTENSION: "STANDING UP",
    CoachingFocus.SWAY: "SWAYING BACK",
    CoachingFocus.CONSISTENCY: "SHOTS SCATTERED",
}

# What the floor says when there is nothing to correct. A fault names itself
# from the measurement that found it; a good shot has no such label, because
# "face open" is what a number outside a band is called and there is no word
# for a number inside one. So these are written down, one per focus, and they
# name the thing that went well rather than praising the shot in general.
REINFORCEMENT_DISPLAYS: Mapping[CoachingFocus, str] = {
    CoachingFocus.FACE_CONTROL: "GOOD FACE",
    CoachingFocus.PATH: "GOOD PATH",
    CoachingFocus.STRIKE: "GOOD STRIKE",
    CoachingFocus.LOW_POINT: "CLEAN CONTACT",
    CoachingFocus.SPEED: "GOOD SPEED",
    CoachingFocus.START_LINE: "GOOD START",
    CoachingFocus.CURVE: "GOOD SHAPE",
    CoachingFocus.LAUNCH: "GOOD LAUNCH",
    CoachingFocus.SPIN: "GOOD SPIN",
    CoachingFocus.CONSISTENCY: "REPEATING WELL",
    CoachingFocus.TEMPO: "GOOD TEMPO",
    CoachingFocus.POSTURE: "GOOD POSTURE",
    CoachingFocus.EARLY_EXTENSION: "HELD YOUR ANGLES",
    CoachingFocus.SWAY: "STAYED CENTRED",
}

# One instruction per direction of each fault. Positive first, matching the
# order of `Fault.labels`, so that the table below reads against `faults.py`.
SPEECH: Mapping[CoachingFocus, tuple[str, str]] = {
    CoachingFocus.FACE_CONTROL: (
        "Square the face a little earlier.",
        "Let the face stay open a fraction longer.",
    ),
    CoachingFocus.PATH: (
        "Feel the club work more across the ball.",
        "Feel the club come more from the inside.",
    ),
    CoachingFocus.STRIKE: (
        "Stand a fraction closer to the ball.",
        # The same sentence in the other direction, so that the spoken line
        # explains the action on the floor rather than paraphrasing it. "Give
        # yourself a little more room" did not say to move, or which way.
        "Stand a fraction farther from the ball.",
    ),
    CoachingFocus.START_LINE: (
        "Let the face square up a fraction earlier.",
        "Let the face hold on a fraction longer.",
    ),
    CoachingFocus.CURVE: (
        "Let the toe pass through a little sooner.",
        "Hold the face steadier through the ball.",
    ),
    CoachingFocus.LOW_POINT: (
        "Try to strike down into the ball more.",
        "Catch it slightly on the way up.",
    ),
    CoachingFocus.SPEED: (
        "Ease back to a smoother tempo.",
        "Let this one go a little more freely.",
    ),
    CoachingFocus.LAUNCH: (
        "Keep your hands ahead through impact.",
        "Stay behind it and let the loft work.",
    ),
    CoachingFocus.SPIN: (
        "Sweep it a little cleaner off the turf.",
        "Strike down a fraction more crisply.",
    ),
    CoachingFocus.CONSISTENCY: (
        "Keep the same tempo on every ball.",
        "Keep the same tempo on every ball.",
    ),
}

# What to say when nothing needs correcting. Steady first, then the wording for
# a focus that has measurably improved, because those are two different things
# worth saying: one confirms a movement, the other rewards a change somebody
# has just made and is the single most useful sentence a coach has.
#
# Every line is an observation and then one instruction to repeat what worked.
# None of them is a compliment on its own: "great shot" reinforces nothing,
# because it does not say what to do again.
REINFORCEMENT: Mapping[CoachingFocus, tuple[str, str]] = {
    CoachingFocus.FACE_CONTROL: (
        "Good face control. Keep that release.",
        "Much better face control. Keep that release.",
    ),
    CoachingFocus.PATH: (
        "Good path. Keep that feeling.",
        "That's a better path. Keep that feeling.",
    ),
    CoachingFocus.STRIKE: (
        "Good strike. Same move again.",
        "Much better strike. Do that again.",
    ),
    CoachingFocus.LOW_POINT: (
        "Clean contact. Keep that low point.",
        "Better contact. Keep that low point.",
    ),
    CoachingFocus.SPEED: (
        "Good speed. Keep that tempo.",
        "Better tempo on that one. Keep it.",
    ),
    CoachingFocus.START_LINE: (
        "Good start line. Keep that feeling.",
        "Much better start line. Keep that feeling.",
    ),
    CoachingFocus.CURVE: (
        "Good shape. Keep that release.",
        "Much straighter. Keep that release.",
    ),
    CoachingFocus.LAUNCH: (
        "Good flight. Keep that strike.",
        "Better flight. Keep that strike.",
    ),
    CoachingFocus.SPIN: (
        "Good spin. Strike it just like that.",
        "Better spin. Strike it just like that.",
    ),
    CoachingFocus.CONSISTENCY: (
        "Repeating nicely. Keep that tempo.",
        "Much more repeatable. Keep that tempo.",
    ),
    CoachingFocus.TEMPO: (
        "Good tempo. Keep that rhythm.",
        "Better tempo. Keep that rhythm.",
    ),
    CoachingFocus.POSTURE: (
        "Good posture. Hold that through the ball.",
        "Better posture. Hold that through the ball.",
    ),
    CoachingFocus.EARLY_EXTENSION: (
        "You held your angles. Keep that.",
        "Much better through impact. Keep that.",
    ),
    CoachingFocus.SWAY: (
        "Nicely centred. Keep that feeling.",
        "Much steadier. Keep that feeling.",
    ),
}


def display_for(
    focus: CoachingFocus,
    label: str | None = None,
    kind: AdviceKind = AdviceKind.CORRECTION,
) -> str:
    """The two to four words the floor shows.

    A measured fault names itself: the label is already the player's own
    language, so upper-casing it is the whole of the presentation. A fault only
    a model can see has no label, so it falls back to the table above — and a
    shot with nothing wrong has no label either, because a measurement inside
    its band produces no word for itself.
    """

    if focus is CoachingFocus.NONE:
        return ""
    if kind is AdviceKind.REINFORCEMENT:
        return REINFORCEMENT_DISPLAYS.get(focus, "WELL STRUCK")
    if label:
        return label.upper()
    return OBSERVED_DISPLAYS.get(focus, focus.value.replace("_", " ").upper())


def speech_for(
    focus: CoachingFocus,
    positive: bool = True,
    kind: AdviceKind = AdviceKind.CORRECTION,
    improving: bool = False,
    variant: int = 0,
) -> str:
    """The one thing to say: a change to make, or a movement to repeat.

    `variant` 2 and above pick the other phrasing in the pair, so the third
    identical cue does not repeat the same sentence.
    """

    if kind is AdviceKind.NONE:
        return ""
    if kind is AdviceKind.REINFORCEMENT:
        lines = REINFORCEMENT.get(focus)
        if lines is None:
            return ""
        if variant >= 2:
            return lines[0 if improving else 1]
        return lines[1 if improving else 0]
    lines = SPEECH.get(focus)
    if lines is None:
        return ""
    if variant >= 2:
        return lines[1 if positive else 0]
    return lines[0] if positive else lines[1]


def legal_displays(
    focus: CoachingFocus,
    label: str | None = None,
    kind: AdviceKind = AdviceKind.CORRECTION,
) -> frozenset[str]:
    """Every display CaddieJ would accept for this focus.

    A model is allowed to choose the words on the floor only from the words
    CaddieJ would have chosen itself. That is not distrust of the phrasing: the
    templated line is already on the floor by the time a model answers, so a
    freely worded display would replace a correct statement about a measurement
    with a rewording of it, and flicker while doing so.

    Which words those are depends on what the shot deserves. A reinforcement
    may not be labelled with a fault, and a correction may not be labelled as
    good, because the display is the one part of a coaching line a player reads
    without hearing it.
    """

    if kind is AdviceKind.REINFORCEMENT:
        return frozenset(
            display for display in {display_for(focus, kind=kind)} if display
        )
    permitted = {display_for(focus, label)}
    if focus in OBSERVED_DISPLAYS:
        permitted.add(OBSERVED_DISPLAYS[focus])
    return frozenset(display for display in permitted if display)
