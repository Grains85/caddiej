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

from caddiej.coaching.provider import AdviceRequest, ImageInput
from caddiej.domain.coaching import OBSERVED_FOCUSES, CoachingFocus, Diagnosis

PROMPT_VERSION = "1"

MAX_DISPLAY_CHARACTERS = 18
MAX_SPEECH_CHARACTERS = 90
MAX_SPEECH_WORDS = 14

# The job, stated as narrowly as it actually is. The model is not being asked
# what was wrong with the swing: that has been measured, and telling it so is
# what stops it inventing a second opinion the floor would then contradict.
SYSTEM = f"""\
You write the words for one golf coaching line, and nothing else.

The fault has already been identified from launch monitor measurements. Your job
is to say it well, not to decide what it is.

Rules, all of them absolute:
- Choose exactly one focus, from the list you are given. Never invent one.
- "display" is 2 to 4 words, upper case, at most {MAX_DISPLAY_CHARACTERS}
  characters, and must be one of the permitted displays you are given.
- "speech" is one coaching idea: at most two short sentences, at most
  {MAX_SPEECH_WORDS} words, at most {MAX_SPEECH_CHARACTERS} characters. An
  optional short affirmation followed by exactly one instruction.
- Never state a number, a measurement or a unit. The player can already see them.
- Never say left or right. The player may be left-handed and the words would be
  backwards for them. Describe what to feel or do instead.
- Never mention the camera, the data, the measurements, or yourself.
- Never give two instructions, and never join ideas with "and also" or a
  semicolon.
- If the focus you are given is "none", the shot was good. Say nothing new.

You are talking to somebody standing on a mat about to hit another ball. Be
short, be specific, and be kind."""

INSTRUCTIONS = """\
Reply with JSON matching the schema exactly. No prose outside the JSON.

"severity" is your impression of how bad the fault is, from 0 to 1. It is used
only for a fault that no measurement can rank; otherwise the measured value is
kept and yours is discarded, so do not let it change what you write."""


def reply_schema(
    focuses: Sequence[CoachingFocus], displays: Sequence[str]
) -> dict[str, Any]:
    """The JSON Schema a reply must satisfy.

    The enumerations are narrowed per shot rather than being the whole
    vocabulary. A structured-output schema that only permits the faults CaddieJ
    actually measured on this swing removes a whole class of invalid answer
    before it is generated, which is cheaper than rejecting it afterwards.
    """

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["focus", "display", "speech", "severity"],
        "properties": {
            "focus": {
                "type": "string",
                "enum": [focus.value for focus in focuses],
            },
            "display": {
                "type": "string",
                "enum": list(displays),
            },
            "speech": {
                "type": "string",
                "maxLength": MAX_SPEECH_CHARACTERS,
            },
            "severity": {"type": "number", "minimum": 0, "maximum": 1},
        },
    }


def permitted_focuses(
    diagnosis: Diagnosis, has_frames: bool
) -> tuple[CoachingFocus, ...]:
    """Which focuses this shot's reply may name.

    The measured candidates always, plus the ones only a video shows when a
    video was actually sent. Offering `TEMPO` to a model that received no frames
    would be inviting it to guess, and a guess is indistinguishable from a
    measurement once it is on the floor.
    """

    measured = tuple(candidate.focus for candidate in diagnosis.candidates)
    if not has_frames:
        return measured or (CoachingFocus.NONE,)
    observed = tuple(sorted(OBSERVED_FOCUSES, key=lambda focus: focus.value))
    return measured + observed


def build_request(
    diagnosis: Diagnosis,
    evidence: str,
    displays: Sequence[str],
    images: Sequence[ImageInput] = (),
    max_output_tokens: int = 60,
    timeout_seconds: float = 8.0,
) -> AdviceRequest:
    """Everything the provider needs for one shot."""

    focuses = permitted_focuses(diagnosis, has_frames=bool(images))
    return AdviceRequest(
        system=SYSTEM,
        instructions=INSTRUCTIONS,
        evidence=evidence,
        schema=reply_schema(focuses, displays),
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
        "display": diagnosis.display,
        "frames": "yes" if has_frames else "no",
    }
