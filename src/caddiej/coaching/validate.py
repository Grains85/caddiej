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
than anywhere closer to the DOM. The same goes for the frame a reply cites: it
is accepted only as one of the ids the request carried, and it is later turned
into a file only by an exact lookup, never by building a path from it.
"""

import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from caddiej.coaching.cues import (
    CUES_BY_ID,
    CoachCue,
    cap_visual_confidence,
    contradicting_words,
    cue_speech,
    derived_confidence,
    display_for_cue,
    observed_for_cue,
    plain_words,
)
from caddiej.coaching.prompt import (
    MAX_DISPLAY_CHARACTERS,
    MAX_SPEECH_CHARACTERS,
    MAX_SPEECH_WORDS,
)
from caddiej.coaching.templates import legal_displays
from caddiej.domain.coaching import (
    OBSERVED_FOCUSES,
    SEVERITY_FINE,
    Advice,
    AdviceKind,
    AdviceSource,
    CoachingFocus,
    Diagnosis,
    SeveritySource,
)
from caddiej.domain.cues import ConfidenceSource, CueId, EvidenceGrade

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
        "drop",
        "ease",
        "feel",
        "finish",
        "focus",
        "get",
        "give",
        "hold",
        "keep",
        "let",
        "lift",
        "look",
        "maintain",
        "make",
        "match",
        "move",
        "open",
        "point",
        "press",
        "push",
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
        "tuck",
        "turn",
        "use",
        "watch",
        "work",
    }
)

# A sentence that only points back at the instruction before it ("Keep that
# same feeling on the next swing.") restates one instruction rather than adding
# a second. Recognised only when every word after the verb is from this list,
# so "Keep that lead arm straight" is still a new instruction.
_RESTATING_VERBS = frozenset(
    {"do", "feel", "hold", "keep", "repeat", "strike", "trust"}
)
_RESTATING_WORDS = frozenset(
    {
        "a",
        "again",
        "ball",
        "balls",
        "each",
        "every",
        "exactly",
        "feel",
        "feeling",
        "few",
        "for",
        "it",
        "just",
        "like",
        "more",
        "motion",
        "move",
        "next",
        "on",
        "one",
        "same",
        "shot",
        "shots",
        "swing",
        "swings",
        "that",
        "the",
        "this",
        "through",
        "time",
    }
)
_POINTING_BACK = frozenset({"again", "it", "same", "that", "this"})

# Words that belong to one family of corrections. Speech for a cue may only use
# the families its focus owns, so a face cue cannot smuggle in a weight shift.
_FAMILIES: Mapping[str, frozenset[str]] = {
    "face": frozenset(
        {"face", "clubface", "release", "rotate", "rotation", "square", "toe pass"}
    ),
    "path": frozenset(
        {
            "path",
            "inside",
            "outside",
            "across",
            "in to out",
            "out to in",
            "over the top",
        }
    ),
    "strike": frozenset({"toe", "heel", "closer", "room", "centre", "center"}),
    "attack": frozenset(
        {"down into", "on the way up", "steep", "shallow", "divot", "turf", "low point"}
    ),
    "tempo": frozenset(
        {"tempo", "rhythm", "pace", "smoother", "slower", "faster", "rush", "hurry"}
    ),
    "weight": frozenset(
        {"weight", "pressure", "shift", "lead side", "trail side", "knees", "legs"}
    ),
    "body": frozenset(
        {
            "posture",
            "spine",
            "extension",
            "angles",
            "stand up",
            "shoulder",
            "shoulders",
            "hip",
            "hips",
        }
    ),
}
_FAMILIES_BY_FOCUS: Mapping[CoachingFocus, frozenset[str]] = {
    CoachingFocus.FACE_CONTROL: frozenset({"face"}),
    CoachingFocus.PATH: frozenset({"path", "body"}),
    CoachingFocus.STRIKE: frozenset({"strike"}),
    CoachingFocus.LOW_POINT: frozenset({"attack", "weight"}),
    CoachingFocus.SPEED: frozenset({"tempo"}),
    CoachingFocus.START_LINE: frozenset({"face", "path"}),
    CoachingFocus.CURVE: frozenset({"face", "path"}),
    CoachingFocus.LAUNCH: frozenset({"attack", "strike"}),
    CoachingFocus.SPIN: frozenset({"attack", "strike"}),
    CoachingFocus.CONSISTENCY: frozenset({"tempo"}),
    CoachingFocus.TEMPO: frozenset({"tempo"}),
    CoachingFocus.POSTURE: frozenset({"body", "weight"}),
    CoachingFocus.EARLY_EXTENSION: frozenset({"body", "weight"}),
    CoachingFocus.SWAY: frozenset({"body", "weight"}),
}

_BANNED_IN_SPEECH = ("and also", ";", "•", " - ", "\n", "\t")
_HANDED_WORDS = frozenset({"left", "right", "lefts", "rights", "righty", "lefty"})
"""Words that name a side of the body, which CaddieJ does not say.

The prompt asks for lead and trail instead, and the first hardware session
showed that asking is not enough: a reinforcement on a face that was 1.5
degrees *closed* said "Your clubface pointed right at impact." Wrong for a
right-hander, and wrong in the other direction for a left-hander, from one
sentence.

"Lead" and "trail" are true for both players, which is the whole reason they
are the house vocabulary. A word that is only true for one of them cannot be
checked for correctness here -- nothing in this function knows which way the
face was pointing -- so it is refused rather than inspected. The cue's own
reviewed line is said instead, and that line is handedness-neutral by
construction.

Whole words only. "Rightly" is not a side and "upright" is a shaft lie, which
is why this is a set of words and not a substring scan."""
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_DISPLAY_ALLOWED = re.compile(r"^[A-Z][A-Z \-]*$")
_SPEECH_ALLOWED = re.compile(r"^[A-Za-z ,.'!?\-]+$")
# Coarse severity for a fault no measurement ranks, so that a stored row cannot
# imply a precision that was never there.
_OBSERVED_STEPS = (0.25, 0.5, 0.75)


class AdviceRejected(ValueError):
    """The reply was not something CaddieJ is willing to show or say."""


class SpeechRejected(AdviceRejected):
    """The cue was sound and only the words were refused."""


def validate(
    payload: Mapping[str, Any],
    diagnosis: Diagnosis,
    created_at: datetime,
    frames_sent: bool = False,
    fine: float = SEVERITY_FINE,
    frame_ids: Collection[str] = (),
) -> Advice:
    """Build an `Advice` from a provider's reply, or raise `AdviceRejected`.

    `frame_ids` are the ids of the images the request actually carried. A
    reply may cite one of them as its evidence, or none; it may not cite
    anything else, because a frame that was never sent is a picture the model
    described without having seen.
    """

    cue = _cue(payload, diagnosis, frames_sent)
    spec = CUES_BY_ID[cue]
    observation = _observation(payload)
    speech, instructions = _speech(payload)
    if spec.kind is AdviceKind.CORRECTION and instructions == 0:
        raise SpeechRejected(
            "speech for a correction only restated, it never instructed"
        )
    _stays_on_focus(speech, spec.focus)
    _agrees_with_cue(observation, speech, cue)
    evidence_frame_id = _evidence_frame(payload, frame_ids)
    confidence, confidence_source = _confidence(payload, diagnosis, spec)
    if confidence is None:
        raise AdviceRejected("visual confidence was below the floor")
    if spec.grade is EvidenceGrade.VISUAL:
        # Kind is the cue's. A visual lesson is never a measured reinforcement
        # in disguise, and CaddieJ does not let a picture overwrite a band.
        kind = spec.kind
        severity = min(_OBSERVED_STEPS, key=lambda step: abs(step - confidence))
        severity_source = SeveritySource.OBSERVED
    else:
        if spec.kind is not diagnosis.kind and spec.kind is not AdviceKind.NONE:
            raise AdviceRejected(
                f"cue {cue.value} is a {spec.kind.value}, this shot is a "
                f"{diagnosis.kind.value}"
            )
        kind = diagnosis.kind
        severity, severity_source = _severity(payload, diagnosis, spec.focus)
    _ = fine

    return Advice(
        shot_id=diagnosis.shot_id,
        focus=spec.focus,
        display=display_for_cue(cue),
        speech=speech,
        severity=severity,
        severity_source=severity_source,
        source=AdviceSource.MODEL,
        created_at=created_at,
        kind=kind,
        cue=cue,
        confidence=confidence,
        confidence_source=confidence_source,
        observation=observed_for_cue(cue),
        spoken_observation=observation,
        evidence_frame_id=evidence_frame_id,
    )


def from_diagnosis(diagnosis: Diagnosis, created_at: datetime) -> Advice:
    """The line CaddieJ would give with no model at all.

    Both the starting point and the fallback, which is the same thing said
    twice: the deterministic line is what the player has been reading since the
    ball was struck, so a refused reply changes nothing on the floor.
    """

    confidence = None
    source = None
    if diagnosis.cue is not None:
        spec = CUES_BY_ID[diagnosis.cue]
        confidence = derived_confidence(diagnosis, spec)
        source = ConfidenceSource.DERIVED
    return Advice(
        shot_id=diagnosis.shot_id,
        focus=diagnosis.focus,
        display=diagnosis.display,
        speech=diagnosis.speech,
        severity=diagnosis.severity,
        severity_source=SeveritySource.MEASURED,
        source=AdviceSource.TEMPLATE,
        created_at=created_at,
        kind=diagnosis.kind,
        cue=diagnosis.cue,
        confidence=confidence,
        confidence_source=source,
        observation="" if diagnosis.cue is None else observed_for_cue(diagnosis.cue),
    )


def from_cue(
    diagnosis: Diagnosis,
    created_at: datetime,
    frames_sent: bool = False,
    fine: float = SEVERITY_FINE,
    frame_ids: Collection[str] = (),
    evidence_frame_id: str | None = None,
    visual_confidence: float | None = None,
) -> Advice | None:
    """The diagnosis's own cue, said in that cue's reviewed words, or None.

    What a player is given when CaddieJ knows the lesson and the model's words
    for it could not be used. `from_diagnosis` is the same lesson as two words
    on the floor; this is the whole of it — what was seen, the one thing to do,
    and the sentence George says — and every part of it was written and
    reviewed with the cue, so nothing here is composed for the occasion.

    It goes through `validate` like any reply would. The reviewed sentence is
    trusted because it was reviewed, and checked anyway, so that a registry
    edit which gave a cue two instructions, or a sentence about another focus,
    is refused here rather than spoken. None means there is no such sentence,
    or the cue is not one this shot admits, and the floor-only line stands.

    `visual_confidence` is the rating's, for a visual cue CaddieJ promoted. It
    is required rather than assumed: this is the path a player actually hears
    when the model's wording could not be reused, and a visual lesson with no
    confidence behind it is one CaddieJ has no business giving.
    """

    cue = diagnosis.cue
    if cue is None or cue not in diagnosis.admissible_cues:
        return None
    line = cue_speech(cue)
    if line is None:
        return None
    try:
        advice = validate(
            {
                "cue": cue.value,
                "speech": line,
                "observation": None,
                "evidence_frame_id": evidence_frame_id,
                "visual_confidence": visual_confidence,
            },
            diagnosis,
            created_at,
            frames_sent=frames_sent,
            fine=fine,
            frame_ids=frame_ids,
        )
    except AdviceRejected:
        return None
    return replace(
        advice,
        source=AdviceSource.TEMPLATE,
        spoken_observation=CUES_BY_ID[cue].explanation,
    )


def _cue(payload: Mapping[str, Any], diagnosis: Diagnosis, frames_sent: bool) -> CueId:
    raw = payload.get("cue")
    if not isinstance(raw, str):
        raise AdviceRejected("cue was missing or not a string")
    try:
        cue = CueId(raw)
    except ValueError:
        raise AdviceRejected(f"cue {raw!r} is not a coaching cue") from None
    if cue not in CUES_BY_ID:
        raise AdviceRejected(f"cue {raw!r} is not in the registry")
    spec = CUES_BY_ID[cue]
    allowed = diagnosis.admissible_cues
    if allowed and cue not in allowed:
        raise AdviceRejected(f"cue {cue.value} was not admissible on this shot")
    if spec.grade is EvidenceGrade.VISUAL and not frames_sent:
        raise AdviceRejected(f"cue {cue.value} needs frames to have been sent")
    if diagnosis.club not in spec.clubs:
        club = "an unknown club" if diagnosis.club is None else diagnosis.club.value
        raise AdviceRejected(f"cue {cue.value} does not apply to {club}")
    return cue


def _confidence(
    payload: Mapping[str, Any], diagnosis: Diagnosis, spec: CoachCue
) -> tuple[float | None, ConfidenceSource]:
    if spec.grade is not EvidenceGrade.VISUAL:
        return derived_confidence(diagnosis, spec), ConfidenceSource.DERIVED
    raw = payload.get("visual_confidence")
    if not isinstance(raw, int | float) or isinstance(raw, bool):
        raise AdviceRejected("visual_confidence was missing for a visual cue")
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise AdviceRejected(f"visual_confidence {value} is outside 0..1")
    capped = cap_visual_confidence(value)
    if capped is None:
        return None, ConfidenceSource.MODEL_CAPPED
    return capped, ConfidenceSource.MODEL_CAPPED


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
        # Silence is CaddieJ's answer to having no evidence, not a wording a
        # model may choose. It was asked because there was something to say.
        raise AdviceRejected("this shot was worth a line, so 'none' is not an answer")

    if diagnosis.severity_of(focus) is None:
        raise AdviceRejected(f"focus {focus.value} was not measured on this shot")
    return focus


def _display(
    payload: Mapping[str, Any],
    diagnosis: Diagnosis,
    focus: CoachingFocus,
    kind: AdviceKind,
) -> str:
    label = next(
        (
            observation.label
            for observation in diagnosis.observations
            if observation.focus is focus
        ),
        None,
    )
    permitted = legal_displays(focus, label, kind)
    # CaddieJ's own words for this focus, which are the diagnosis's only when
    # the reply stayed on the focus the measurements named. A refused display
    # for a fault only the video showed must not fall back to the line for a
    # different fault entirely.
    own = (
        diagnosis.display if focus is diagnosis.focus and kind is diagnosis.kind else ""
    )
    fallback = own or next(iter(sorted(permitted)), "")

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


def _speech(payload: Mapping[str, Any]) -> tuple[str, int]:
    raw = payload.get("speech")
    if not isinstance(raw, str):
        raise SpeechRejected("speech was missing or not a string")

    text = _collapse(unicodedata.normalize("NFKC", raw))
    if not text:
        raise SpeechRejected("speech was empty")
    if len(text) > MAX_SPEECH_CHARACTERS:
        raise SpeechRejected(f"speech was {len(text)} characters")
    # Before the character set, which would also catch a digit or a semicolon
    # but would say only that something unrenderable was in there. These two
    # are worth their own answer, because they are the two a model actually
    # does and the reason each is refused is not "the floor cannot draw it".
    if any(char.isdigit() for char in text):
        # A model writing "about three degrees" beside a floor reading 6.5 has
        # produced a contradiction, and the player will believe the sentence.
        raise SpeechRejected("speech stated a number")
    for banned in _BANNED_IN_SPEECH:
        if banned in text:
            raise SpeechRejected(f"speech contained {banned!r}")
    if not _SPEECH_ALLOWED.match(text):
        raise SpeechRejected("speech held characters the floor will not render")

    words = text.split()
    if len(words) > MAX_SPEECH_WORDS:
        raise SpeechRejected(f"speech was {len(words)} words")
    handed = sorted(
        {
            word
            for word in (w.strip(".,!?'-").casefold() for w in words)
            if word in _HANDED_WORDS
        }
    )
    if handed:
        raise SpeechRejected(f"speech said {handed[0]!r} instead of lead or trail")

    sentences = [part for part in _SENTENCE_BREAK.split(text) if part]
    if len(sentences) > MAX_SPEECH_SENTENCES:
        raise SpeechRejected(f"speech was {len(sentences)} sentences")

    restating = [_restates(sentence) for sentence in sentences]
    instructions = sum(
        1
        for sentence, restates in zip(sentences, restating, strict=True)
        if not restates and _is_instruction(sentence)
    )
    if instructions > 1:
        raise SpeechRejected(f"speech held {instructions} instructions, not one")
    if any(restating[:-1]):
        # Pointing back only works at the end: a restatement first has nothing
        # to point at, and two of them say nothing.
        raise SpeechRejected("speech restated before it had an instruction")
    if instructions == 0 and not restating[-1]:
        raise SpeechRejected("speech held 0 instructions, not one")
    return text, instructions


def _observation(payload: Mapping[str, Any]) -> str:
    """The reply's observation sentence, or "" when it offered none.

    Judged on its own before the line is judged as a whole. Joined to an
    instruction, an observation that is itself an instruction — "Shift your
    weight forward." — reads as a second correction, and the player is back
    to holding two ideas. The whole-line count would usually catch it too, but
    not when the second sentence happens to be a restatement, and "it was an
    instruction posing as an observation" is the reason worth logging.
    """

    raw = payload.get("observation")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise SpeechRejected("observation was not a string")
    text = _collapse(unicodedata.normalize("NFKC", raw))
    if not text:
        return ""
    sentences = [part for part in _SENTENCE_BREAK.split(text) if part]
    if len(sentences) != 1:
        raise SpeechRejected(f"observation was {len(sentences)} sentences")
    if _is_instruction(text) or _restates(text):
        raise SpeechRejected("observation was itself an instruction")
    return text


def _evidence_frame(
    payload: Mapping[str, Any], frame_ids: Collection[str]
) -> str | None:
    """The cited frame, if it is one that was sent. Absence is not a fault."""

    raw = payload.get("evidence_frame_id")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise AdviceRejected("evidence_frame_id was not a string")
    if raw not in frame_ids:
        raise AdviceRejected(f"evidence_frame_id {raw[:40]!r} was not a frame sent")
    return raw


def _stays_on_focus(speech: str, focus: CoachingFocus) -> None:
    """Refuse a sentence that reaches into another focus's corrections."""

    text = plain_words(speech)
    owned = _FAMILIES_BY_FOCUS.get(focus, frozenset())
    for family in owned:
        for phrase in _FAMILIES[family]:
            text = text.replace(f" {phrase} ", " ")
    for family, phrases in _FAMILIES.items():
        if family in owned:
            continue
        for phrase in phrases:
            if f" {phrase} " in text:
                raise SpeechRejected(
                    f"speech mentioned {family} ({phrase!r}) on a {focus.value} cue"
                )


def _agrees_with_cue(observation: str, speech: str, cue: CueId) -> None:
    """Refuse words that describe the opposite of the fault the cue names.

    The cue is authoritative and the prose is checked against it, never the
    other way round: a FACE_OPEN reply that says the face was closed is not a
    model that knew better, it is a line that would tell the player two
    things at once.

    Only what the model says it *saw* is held to this. An instruction is
    exempt, because a correction is the opposite direction by definition —
    the fix for out-to-in is to swing from the inside, the fix for an open
    face may be to close it — and checking instructions would refuse every
    sound correction there is. So the observation is checked, and of the
    speech only the sentences that are not instructions.
    """

    contradicting = contradicting_words(cue)
    if not contradicting:
        return
    said = [observation] if observation else []
    said += [
        sentence
        for sentence in _SENTENCE_BREAK.split(speech)
        if sentence and not _is_instruction(sentence)
    ]
    for sentence in said:
        text = plain_words(sentence)
        for phrase in sorted(contradicting):
            if f" {phrase} " in text:
                raise SpeechRejected(
                    f"the words said {phrase!r}, the opposite of cue {cue.value}"
                )


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


def _restates(sentence: str) -> bool:
    """True for a sentence that only repeats the instruction it follows."""

    words = [word.strip(",.'!?-").lower() for word in sentence.split()]
    words = [word for word in words if word]
    if not words:
        return False
    if words[0] in _RESTATING_VERBS:
        rest = words[1:]
    elif words[0] == "same":
        rest = words
    else:
        return False
    return (
        bool(rest)
        and all(word in _RESTATING_WORDS for word in rest)
        and any(word in _POINTING_BACK for word in rest)
    )


def _collapse(text: str) -> str:
    return " ".join(text.split())


def recent_lines(advice: Sequence[Advice], limit: int = 3) -> tuple[str, ...]:
    """The last few things the player was told, newest last.

    Given to the model so a session does not become one sentence repeated
    fifteen times, which is the failure mode of a coach with one idea.
    """

    spoken = [item.speech for item in advice if item.speech]
    return tuple(spoken[-limit:])
