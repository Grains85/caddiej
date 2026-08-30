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

from caddiej.domain.coaching import CoachingFocus

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
        "Give yourself a little more room.",
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


def display_for(focus: CoachingFocus, label: str | None = None) -> str:
    """The two to four words the floor shows.

    A measured fault names itself: the label is already the player's own
    language, so upper-casing it is the whole of the presentation. A fault only
    a model can see has no label, so it falls back to the table above.
    """

    if focus is CoachingFocus.NONE:
        return ""
    if label:
        return label.upper()
    return OBSERVED_DISPLAYS.get(focus, focus.value.replace("_", " ").upper())


def speech_for(focus: CoachingFocus, positive: bool = True) -> str:
    """The one instruction to give, or nothing when there is nothing to say."""

    lines = SPEECH.get(focus)
    if lines is None:
        return ""
    return lines[0] if positive else lines[1]


def legal_displays(focus: CoachingFocus, label: str | None = None) -> frozenset[str]:
    """Every display CaddieJ would accept for this focus.

    A model is allowed to choose the words on the floor only from the words
    CaddieJ would have chosen itself. That is not distrust of the phrasing: the
    templated line is already on the floor by the time a model answers, so a
    freely worded display would replace a correct statement about a measurement
    with a rewording of it, and flicker while doing so.
    """

    permitted = {display_for(focus, label)}
    if focus in OBSERVED_DISPLAYS:
        permitted.add(OBSERVED_DISPLAYS[focus])
    return frozenset(display for display in permitted if display)
