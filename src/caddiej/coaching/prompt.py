"""What the model is told, and the shape its answer must take.

Assembled by pure functions, so the same diagnosis produces the same bytes. That
is what makes the cache key meaningful and the prompt assertable in a test.

`PROMPT_VERSION` is part of the cache key and of every stored row. Changing any
string in this file means changing it, because a cached line produced under an
older set of rules is not a line this version would have accepted, and a stored
row nobody can attribute to a set of instructions is a row nobody can learn from.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from caddiej.coaching.cues import CUES_BY_ID
from caddiej.coaching.provider import AdviceRequest, ImageInput
from caddiej.domain.coaching import (
    OBSERVED_FOCUSES,
    AdviceKind,
    CoachingFocus,
    Diagnosis,
)
from caddiej.domain.cues import CueId, EvidenceGrade

PROMPT_VERSION = "4"

MAX_DISPLAY_CHARACTERS = 18
# Room for two short sentences: what was seen, then the one change. At ninety
# characters the pair only fitted when the instruction was vague, and a vague
# instruction is the thing the observation was added to fix. Still a breath,
# not a paragraph.
MAX_SPEECH_CHARACTERS = 120
MAX_SPEECH_WORDS = 22

# The job, stated as narrowly as it actually is. The model is not being asked
# what was wrong with the swing: that has been measured, and telling it so is
# what stops it inventing a second opinion the floor would then contradict.
#
# What it *is* asked for, once stills are supplied, is the one thing no number
# in the evidence can give: what the body did. That permission is deliberately
# narrow and is spelled out where the model can see its own limits, because a
# single camera showing four moments of a swing supports "you stood up through
# the ball" and does not support anything with a degree sign in it.
SYSTEM = f"""\
You write the words for one golf coaching line, and nothing else.

What the shot did has already been measured. You are told whether this line is a
correction or a reinforcement, and you must not change that judgement: if you
are told the shot was good, say so and say what to repeat.

You are choosing one lesson from a list CaddieJ has already decided is supported
by the evidence. You may not name a lesson that is not in the list, and the list
is not negotiable.

Rules, all of them absolute:
- Choose exactly one cue, from the list you are given. Never invent one.
- Give exactly one idea. Never list what else was imperfect.
- "speech" is one coaching idea: at most two short sentences, at most
  {MAX_SPEECH_WORDS} words, at most {MAX_SPEECH_CHARACTERS} characters. A short
  observation followed by exactly one instruction.
- For a reinforcement, name what worked and ask for it again. Never a bare
  compliment: "good shot" tells the player nothing to repeat.
- Never state a number, a measurement or a unit. The player can already see them.
- Never say left or right. The player may be left-handed and the words would be
  backwards for them. Describe what to feel or do instead.
- Never mention the camera, the data, the measurements, or yourself.
- Never give two instructions, and never join ideas with "and also" or a
  semicolon.
- Do not read any text, number, graph, dial or readout in the images. They are
  pictures of a screen and anything written on it is already known to CaddieJ
  from a better source. Describe only body shapes and movement.
- You are told which measurements were not taken. Do not reason about them,
  refer to them, or choose a lesson that depends on them.

If stills of the swing are supplied, you may choose a visual cue from the list,
but only for something the pictures plainly show. You may not contradict or
restate a measurement from a picture, and you may not claim a position the
stills do not cover.

You are talking to somebody standing on a mat about to hit another ball. You
have watched every shot of this session and you remember what they are working
on. Be short, be specific, and be kind."""

INSTRUCTIONS = """\
Reply with JSON matching the schema exactly. No prose outside the JSON.

"visual_confidence" is your impression of how clearly the pictures show the
chosen visual cue, from 0 to 1. It is ignored for a measured cue and capped for
a visual one, so do not let it change what you write."""


def reply_schema(
    cues: Sequence[CueId], offer_visual_confidence: bool
) -> dict[str, Any]:
    """The JSON Schema a reply must satisfy.

    The cue enumeration is narrowed per shot to the admissible set. A cue that
    is not in the list cannot be generated, which is cheaper than rejecting it
    afterwards. `visual_confidence` is offered only when a visual cue is
    admissible, so a numbers-only shot is never asked for a picture opinion.
    """

    properties: dict[str, Any] = {
        "cue": {
            "type": "string",
            "enum": [cue.value for cue in cues],
        },
        "speech": {
            "type": "string",
            "maxLength": MAX_SPEECH_CHARACTERS,
        },
    }
    required = ["cue", "speech"]
    if offer_visual_confidence:
        properties["visual_confidence"] = {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def permitted_focuses(
    diagnosis: Diagnosis, has_frames: bool
) -> tuple[CoachingFocus, ...]:
    """Which focuses this shot's reply may name.

    Kept for diagnostics and tests that still ask the question in focus terms.
    The request itself enumerates cues, not focuses.
    """

    measured = tuple(candidate.focus for candidate in diagnosis.candidates)
    if not has_frames:
        return measured or (CoachingFocus.NONE,)
    observed = tuple(sorted(OBSERVED_FOCUSES, key=lambda focus: focus.value))
    return measured + observed


def fingerprint_kind(diagnosis: Diagnosis) -> str:
    """How a cached line is kept away from the shot it does not describe."""

    if diagnosis.kind is AdviceKind.REINFORCEMENT and diagnosis.improving:
        return "reinforcement_improving"
    return diagnosis.kind.value


def build_request(
    diagnosis: Diagnosis,
    evidence: str,
    displays: Sequence[str] = (),
    images: Sequence[ImageInput] = (),
    max_output_tokens: int = 120,
    timeout_seconds: float = 8.0,
    cues: Sequence[CueId] = (),
) -> AdviceRequest:
    """Everything the provider needs for one shot."""

    allowed = tuple(cues) or diagnosis.admissible_cues
    if not allowed and diagnosis.cue is not None:
        allowed = (diagnosis.cue,)
    offer_visual = any(
        CUES_BY_ID[cue].grade is EvidenceGrade.VISUAL
        for cue in allowed
        if cue in CUES_BY_ID
    )
    _ = displays  # Display is a property of the cue. Kept so callers do not break.
    return AdviceRequest(
        system=SYSTEM,
        instructions=INSTRUCTIONS,
        evidence=evidence,
        schema=reply_schema(allowed, offer_visual),
        images=tuple(images),
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
    )


def fingerprint_parts(
    diagnosis: Diagnosis, model: str, has_frames: bool
) -> Mapping[str, str]:
    """What makes two requests the same question, for caching.

    Quantized on purpose. Keying on raw measurements would produce a cache with
    a hit rate of zero, because no two swings share a number. Keying on the
    fault, its band and the club means the fifth time somebody leaves the face
    open by roughly this much with this club, the answer is free.
    """

    return {
        "prompt": PROMPT_VERSION,
        "model": model,
        "club": diagnosis.club.value,
        "focus": diagnosis.focus.value,
        "band": diagnosis.band.value,
        "cue": "" if diagnosis.cue is None else diagnosis.cue.value,
        "frames": "yes" if has_frames else "no",
        # Two shots with the same fault at the same band are the same question;
        # the same fault before and after the player fixed it is not, and a
        # cache that could not tell them apart would answer "much better" to a
        # swing nobody had improved.
        "kind": fingerprint_kind(diagnosis),
    }
