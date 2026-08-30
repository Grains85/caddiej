"""Turning whatever came back into an `Advice`, or refusing it.

A provider is asked for a strict schema, and this validates the reply anyway.
Not out of distrust of structured outputs, but because the rules that matter
here cannot be expressed as a schema: how many ideas a sentence contains, whether
a fault was one CaddieJ actually measured, whether the words contradict the
numbers already on the floor.

**Nothing is repaired.** A reply that breaks a rule is refused and the templated
line stands, which is a correct statement about a measurement. Truncating a
coaching sentence produces "Square the face a little" — grammatical, confident,
and no longer advice. One exception: a display outside the permitted set is
replaced by the template's, because the display is CaddieJ's word for the fault
and the model was only ever choosing from a list.

**Model output is untrusted input.** It ends up in a browser, so it is length
capped and reduced to an explicit character set here, at the boundary, rather
than anywhere closer to the DOM.
"""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from caddiej.coaching.prompt import (
    MAX_DISPLAY_CHARACTERS,
    MAX_SPEECH_CHARACTERS,
    MAX_SPEECH_WORDS,
)
from caddiej.coaching.templates import legal_displays
from caddiej.domain.coaching import (
    OBSERVED_FOCUSES,
    Advice,
    AdviceSource,
    CoachingFocus,
    Diagnosis,
    SeveritySource,
)

MIN_DISPLAY_WORDS = 2
MAX_DISPLAY_WORDS = 4
MAX_SPEECH_SENTENCES = 2

# A coarse test for "this sentence tells the player to do something". Not
# linguistics: it catches the realistic shapes a model produces, and the cost of
# a false refusal is the templated line, which was good enough anyway.
_INSTRUCTION_VERBS = frozenset(
    {
        "add",
        "allow",
        "avoid",
        "bring",
        "catch",
        "check",
        "close",
        "control",
        "ease",
        "feel",
        "finish",
        "focus",
        "get",
        "give",
        "hold",
        "keep",
        "let",
        "look",
        "make",
        "match",
        "move",
        "open",
        "put",
        "reduce",
        "release",
        "rotate",
        "settle",
        "shift",
        "slow",
        "square",
        "stand",
        "start",
        "stay",
        "step",
        "strike",
        "swing",
        "sweep",
        "take",
        "think",
        "throw",
        "trust",
        "try",
        "turn",
        "use",
        "watch",
        "work",
    }
)

_BANNED_IN_SPEECH = ("and also", ";", "•", " - ", "\n", "\t")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_DISPLAY_ALLOWED = re.compile(r"^[A-Z][A-Z \-]*$")
_SPEECH_ALLOWED = re.compile(r"^[A-Za-z ,.'!?\-]+$")
# Coarse severity for a fault no measurement ranks, so that a stored row cannot
# imply a precision that was never there.
_OBSERVED_STEPS = (0.25, 0.5, 0.75)


class AdviceRejected(ValueError):
    """The reply was not something CaddieJ is willing to show or say."""


def validate(
    payload: Mapping[str, Any],
    diagnosis: Diagnosis,
    created_at: datetime,
    frames_sent: bool = False,
) -> Advice:
    """Build an `Advice` from a provider's reply, or raise `AdviceRejected`."""

    focus = _focus(payload, diagnosis, frames_sent)
    speech = _speech(payload)
    severity, severity_source = _severity(payload, diagnosis, focus)
    display = _display(payload, diagnosis, focus)

    return Advice(
        shot_id=diagnosis.shot_id,
        focus=focus,
        display=display,
        speech=speech,
        severity=severity,
        severity_source=severity_source,
        source=AdviceSource.MODEL,
        created_at=created_at,
    )


def from_diagnosis(diagnosis: Diagnosis, created_at: datetime) -> Advice:
    """The line CaddieJ would give with no model at all.

    Both the starting point and the fallback, which is the same thing said
    twice: the deterministic line is what the player has been reading since the
    ball was struck, so a refused reply changes nothing on the floor.
    """

    return Advice(
        shot_id=diagnosis.shot_id,
        focus=diagnosis.focus,
        display=diagnosis.display,
        speech=diagnosis.speech,
        severity=diagnosis.severity,
        severity_source=SeveritySource.MEASURED,
        source=AdviceSource.TEMPLATE,
        created_at=created_at,
    )


def _focus(
    payload: Mapping[str, Any], diagnosis: Diagnosis, frames_sent: bool
) -> CoachingFocus:
    raw = payload.get("focus")
    if not isinstance(raw, str):
        raise AdviceRejected("focus was missing or not a string")
    try:
        focus = CoachingFocus(raw)
    except ValueError:
        raise AdviceRejected(f"focus {raw!r} is not a coaching focus") from None

    if focus in OBSERVED_FOCUSES:
        if not frames_sent:
            # Naming a fault only a video reveals, having been given no video,
            # is a guess. On the floor a guess is indistinguishable from a
            # measurement, so it is refused.
            raise AdviceRejected(f"focus {focus.value} needs frames to have been sent")
        return focus

    if focus is CoachingFocus.NONE:
        raise AdviceRejected("a fault was diagnosed, so 'none' is not an answer")

    if diagnosis.severity_of(focus) is None:
        raise AdviceRejected(f"focus {focus.value} was not measured on this shot")
    return focus


def _display(
    payload: Mapping[str, Any], diagnosis: Diagnosis, focus: CoachingFocus
) -> str:
    label = next(
        (
            observation.label
            for observation in diagnosis.observations
            if observation.focus is focus
        ),
        None,
    )
    permitted = legal_displays(focus, label)
    fallback = diagnosis.display or next(iter(sorted(permitted)), "")

    raw = payload.get("display")
    if not isinstance(raw, str):
        return fallback

    candidate = _collapse(raw).upper()
    if not _DISPLAY_ALLOWED.match(candidate):
        return fallback
    if len(candidate) > MAX_DISPLAY_CHARACTERS:
        return fallback
    if not MIN_DISPLAY_WORDS <= len(candidate.split()) <= MAX_DISPLAY_WORDS:
        return fallback
    if candidate not in permitted:
        return fallback
    return candidate


def _speech(payload: Mapping[str, Any]) -> str:
    raw = payload.get("speech")
    if not isinstance(raw, str):
        raise AdviceRejected("speech was missing or not a string")

    text = _collapse(unicodedata.normalize("NFKC", raw))
    if not text:
        raise AdviceRejected("speech was empty")
    if len(text) > MAX_SPEECH_CHARACTERS:
        raise AdviceRejected(f"speech was {len(text)} characters")
    # Before the character set, which would also catch a digit or a semicolon
    # but would say only that something unrenderable was in there. These two
    # are worth their own answer, because they are the two a model actually
    # does and the reason each is refused is not "the floor cannot draw it".
    if any(char.isdigit() for char in text):
        # A model writing "about three degrees" beside a floor reading 6.5 has
        # produced a contradiction, and the player will believe the sentence.
        raise AdviceRejected("speech stated a number")
    for banned in _BANNED_IN_SPEECH:
        if banned in text:
            raise AdviceRejected(f"speech contained {banned!r}")
    if not _SPEECH_ALLOWED.match(text):
        raise AdviceRejected("speech held characters the floor will not render")

    words = text.split()
    if len(words) > MAX_SPEECH_WORDS:
        raise AdviceRejected(f"speech was {len(words)} words")

    sentences = [part for part in _SENTENCE_BREAK.split(text) if part]
    if len(sentences) > MAX_SPEECH_SENTENCES:
        raise AdviceRejected(f"speech was {len(sentences)} sentences")

    instructions = sum(1 for sentence in sentences if _is_instruction(sentence))
    if instructions != 1:
        raise AdviceRejected(f"speech held {instructions} instructions, not one")
    return text


def _severity(
    payload: Mapping[str, Any], diagnosis: Diagnosis, focus: CoachingFocus
) -> tuple[float, SeveritySource]:
    """CaddieJ's number, unless nothing could compute one.

    Two severities for one shot is one too many. Where a measurement ranked the
    fault, that value is authoritative and the model's is discarded, so a stored
    row and the colour on the floor can never disagree with the instrument.
    """

    measured = diagnosis.severity_of(focus)
    if measured is not None:
        return measured, SeveritySource.MEASURED

    raw = payload.get("severity")
    if not isinstance(raw, int | float) or isinstance(raw, bool):
        raise AdviceRejected("severity was missing for a fault nothing measured")
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise AdviceRejected(f"severity {value} is outside 0..1")
    nearest = min(_OBSERVED_STEPS, key=lambda step: abs(step - value))
    return nearest, SeveritySource.OBSERVED


def _is_instruction(sentence: str) -> bool:
    words = sentence.split()
    if not words:
        return False
    first = words[0].strip(",.'!?-").lower()
    return first in _INSTRUCTION_VERBS


def _collapse(text: str) -> str:
    return " ".join(text.split())


def recent_lines(advice: Sequence[Advice], limit: int = 3) -> tuple[str, ...]:
    """The last few things the player was told, newest last.

    Given to the model so a session does not become one sentence repeated
    fifteen times, which is the failure mode of a coach with one idea.
    """

    spoken = [item.speech for item in advice if item.speech]
    return tuple(spoken[-limit:])
