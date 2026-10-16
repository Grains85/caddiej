"""Coaching from ProTee VX alone: one request that reads, rates and coaches.

Without GSPro there is no structured shot. What there is, after every strike, is
ProTee's own window: the swing replay panels that swing capture already
photographs, and the All Values grid with every number VX measured. Both go to
the model in one request. It reads the grid into a typed `metrics` object,
rates four parts of the visible swing into a typed `swing` object, and writes
one `coach` line. CaddieJ then checks all three.

Still one request. The swing rating could have been a second call with the same
pictures, and it is not, because the model is already looking at them and a
second round trip buys nothing but latency and another thing to fail.

Three rules, in the order they bite.

**A number is only a number if the model could read it.** Every metric is
individually nullable, a reading has to carry a unit the registry allows, a
direction the registry allows, and a magnitude a golf shot can have. Anything
else is nulled and named in `rejected`. Nothing is inferred from swing images.

**The model reads; CaddieJ judges.** The readings become a `Shot` in CaddieJ's
own sign convention (below), and the same `diagnose()` that ranks a GSPro shot
ranks this one. The model's cue is then checked against what that diagnosis
admits. It may choose among supported cues; it may not decide that a face was
open.

**A look at the body has to say which picture it is a look at.** Every entry in
`swing` carries the id of the frame it came from, and that id has to be one of
the stills this request actually sent and has to be a body camera — never the
All Values grid, which is a photograph of numbers and shows nobody's posture.
An entry without a usable frame is dropped rather than believed, because a
rating with no provenance is an opinion, and an opinion must not move a score.

**Direction is ProTee's word until exactly here.** `domain/vx.py` stores
"5.2 deg closed" as it was written. `shot_from_metrics` is the one place that
turns a word into a sign, and the convention it targets is the one `faults.py`
already owns: angles in the absolute target frame, positive meaning right of
target, so that `oriented()` mirrors them for a left-hander. A directional
metric read without its word is kept in the record and never ranked, because
the sign ProTee would have meant is not something CaddieJ knows.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Any, TypeGuard
from uuid import UUID, uuid4

from caddiej.coaching.checkpoints import CHECKPOINTS_BY_ID, SCOREABLE, Checkpoint
from caddiej.coaching.cues import CUES, CUES_BY_ID, cap_visual_confidence
from caddiej.coaching.faults import FUNCTIONAL_PATH
from caddiej.coaching.prompt import MAX_SPEECH_CHARACTERS, MAX_SPEECH_WORDS
from caddiej.coaching.provider import AdviceRequest, ImageInput
from caddiej.domain.coaching import AdviceKind, CoachingFocus
from caddiej.domain.cues import CueId, EvidenceGrade, PanelView
from caddiej.domain.shot import (
    BallLaunch,
    Club,
    ClubDelivery,
    ClubKind,
    Handedness,
    Shot,
    ShotSource,
)
from caddiej.domain.swing_checkpoints import (
    CHECKPOINT_VIEW_WORDS,
    CheckpointAssessment,
    CheckpointId,
    Deviation,
)
from caddiej.domain.swing_phase import PHASE_ORDER, SwingPhase
from caddiej.domain.swing_score import (
    VISUAL_ORDER,
    ScoreComponentId,
    VisualObservation,
    VisualRating,
)
from caddiej.domain.vx import (
    VX_METRICS,
    VX_METRICS_BY_NAME,
    MetricDirection,
    MetricSpec,
    MetricUnit,
    VxMetric,
    VxMetrics,
)
from caddiej.swing_capture.store import METRICS_FRAME_ID

PROTEE_PROMPT_VERSION = "vx13"
PHASE_VIEW_ORDER: tuple[PanelView, ...] = (PanelView.BEHIND, PanelView.SIDE)
SCHEMA_NAME = "protee_vx_coach"
METRICS_LABEL = "ALL_VALUES"

UNCLEAR_PHASE = "unclear"
"""What the model answers when a picture is not plainly one phase.

A real answer rather than a refusal. A frame between the top and the start of
the downswing is genuinely between them, and a frame with the hands cropped
out of the top of the shot cannot be placed at all."""

# Focuses a VX All Values grid can rank. Putting pace needs a target distance
# that only a simulator gives, so it is not among them.
VX_FOCUSES = frozenset(
    {
        CoachingFocus.FACE_CONTROL,
        CoachingFocus.PATH,
        CoachingFocus.STRIKE,
        CoachingFocus.LOW_POINT,
        CoachingFocus.START_LINE,
        CoachingFocus.CURVE,
        CoachingFocus.SPEED,
        CoachingFocus.LAUNCH,
        CoachingFocus.SPIN,
        CoachingFocus.CONSISTENCY,
    }
)

_MM_PER_INCH = 25.4
_MPH = {
    MetricUnit.MPH: 1.0,
    MetricUnit.KMH: 0.621371,
    MetricUnit.MPS: 2.236936,
}
_YARDS = {MetricUnit.YD: 1.0, MetricUnit.M: 1.093613}


SYSTEM = f"""\
You are CaddieJ's golf coach for one shot hit on a ProTee VX launch monitor.
You do three jobs in one reply, and they are kept apart.

Every list in the reply has exactly the length and the order given below, and
a position you cannot fill is null, never skipped.

JOB 1 - "metrics": read the numbers. Only from the image labelled
{METRICS_LABEL}, which is ProTee's All Values grid. One entry per metric, in
the numbered order of "Metrics" below, each [value, unit, direction]:
- Fill it only if its label and its value are both clearly readable.
- value: the number as written. Never compute, convert or estimate one.
- unit: the code of the unit shown beside it, from that metric's list.
- direction: the code of the qualifier word ProTee prints with the value
  (Open, Closed, Left, Right, In-to-Out, Out-to-In, Up, Down, Toe, Heel, High,
  Low, Upright, Flat), from that metric's list. 0 when no word is shown, or
  the metric has none; the number then keeps its sign. A metric marked
  "signed as printed" shows a sign and no word: write the number with its
  sign, and direction 0.
- If a label is missing, cut off, blurred, overlapped or ambiguous, the entry
  is null. A null is always better than a guess.
- Never read a number from any other image. Swing pictures contain no metrics.
If there is no {METRICS_LABEL} image, every entry is null.

JOB 1b - "frames": say what each camera cell shows, before rating anything.
One key per camera cell, its printed ID, every DTL_ and FO_ cell on its own:
look at that cell alone and write the phase you can see in it. setup (P1
address), takeaway (P2), backswing (P3 lead arm parallel), top (P4),
transition (P5), downswing (P6 delivery, shaft near parallel coming down),
impact (P7), release (P8 just after the ball, arms extending), finish (P9), or
unclear.
Judge the picture, not its label. The P-number and ~moment printed on a cell
are CaddieJ's aim from the clock and are often wrong: it has aimed a camera
using a stopwatch and has not looked at the result. If a cell labelled
~impact is plainly the finish, say finish. If it is between two phases, or the
hands and club are cropped out of the shot, say unclear. Unclear costs the
player nothing and is the right answer more often than it feels like it
should be.

JOB 2 - "checkpoints": score the swing checkpoints. Only from the swing
pictures, never from the {METRICS_LABEL} grid and never from the numbers you
just read. The checkpoints, the views and the moments each may be read from,
and what to look for, are listed under "Swing checkpoints" below, one
numbered list per phase and camera. Walk the swing in order, address to finish, and look
at concrete relationships: where the hands are against the trail shoulder,
whether the pelvis kept its depth from the ball, which way the trail shoulder
moves as the arms come down.

A list named like top_dtl is read only from DTL_ cells, top_fo only from FO_
cells, top_any from either. Each list has one entry per numbered checkpoint,
in that order: null, or [score, cell, confidence, direction]:
- score 0..100, a whole number: how far this checkpoint is from the range tour
  players functionally occupy. 90-100 exemplary, tour quality. 75-89 strong.
  60-74 functional but improvable. 40-59 a meaningful problem. 20-39 a major
  problem. 0-19 a severe deviation. It is NOT a confidence.
- cell: the printed ID of the ONE camera cell it was seen in, like "DTL_P5",
  from that list's camera. Never IMPACT, never ALL_VALUES.
- confidence 0..100, a whole number: how clearly the cell shows it.
- direction: the word for which way it misses, from that checkpoint's dir
  list, or null when it has none or scores 60 or more.
No sentences: CaddieJ knows each cell's camera, and takes its moment from your
"frames" answer.

Tour players differ. A flatter or more upright backswing, a stronger or
weaker turn, a wider or narrower stance are all used on tour: score against
the functional range, not against one model swing, and never mark a
checkpoint down only because it looks different from a textbook.

A checkpoint the pictures do not plainly show is NULL. Null is always better
than a guess: it costs the player nothing and is scored as "not established",
while a guess changes their score and may change what they are told to work
on. Club and shaft checkpoints need the whole club visible in the picture;
when the club is cropped, blurred or hidden by a graphic, they are null. If
there are no swing pictures, every checkpoint is null.

Observe first. Do not look for a fault because one is expected. A good swing
is a real answer and a common one. Score what the body and club did, not what
the ball did.

Return only what the schema asks. CaddieJ computes every score, fault and
lesson from yours.

A checkpoint may only be scored from a picture showing one of its own
moments. "At the finish: balanced and held" cannot be read from a picture of
the top of the backswing, however clear. This is checked against what YOU said
each picture shows in JOB 1b: a checkpoint cited from a picture you called
unclear, from the wrong moment or from the wrong view is thrown away.

Each swing picture's label ends with a moment and a time, like
"~transition @2.9s". The moment is CaddieJ's guess from the clock, not a fact:
it knows when the shutter went and has not looked at the picture.

The two swing views are separate recordings and are NOT synchronised. The same
time in SWING_BEHIND and in SWING_SIDE is a different moment of the swing.
Never put a down-the-line picture beside a face-on one and treat them as the
same instant, and never read one view's picture to fill in what the other view
does not show. Down-the-line and face-on are NOT interchangeable: each
checkpoint names the view it may be read from. Drift away from the target is
a face-on observation, and the trail shoulder moving out towards the ball is a
down-the-line one.

JOB 3 - one coaching line from the numbers: what you read, then one change.
- The cues in the schema are measured ones. Choose the one the numbers you
  read most clearly support, or kind "none" with cue, observation,
  instruction, reinforcement and evidence_frame_id null when none clearly
  applies.
- "kind" is "correction" for something to change, "reinforcement" for
  something worth repeating.
- One cue means one focus and ONE thing to do. The observation and the
  instruction are both about the chosen cue's focus. Never add a second
  correction (weight shift, shoulders, hips, posture, path, tempo, strike,
  face) unless it is that cue's own focus.
- "observation" is ONE short sentence stating what was seen for that cue. It
  starts with "Your" or "The" and only describes; it never tells the player to
  do anything.
- "instruction" is ONE short sentence that starts with a verb and names the
  single physical change the cue asks for: which part of the body, doing what.
  For a reinforcement, it names what to repeat.
- "reinforcement" is null for a correction. For a reinforcement it may stand in
  for the instruction as ONE short sentence confirming what to repeat. Prefer
  null.
  Valid: "Your swing path moved across the ball." then "Turn your trail
  shoulder down as the downswing starts."
  Invalid: "Shift your weight forward." as the observation (it is an
  instruction, so the line now holds two).
  Invalid: "The face was open at impact." then "Shift more weight onto your
  lead side." (the instruction is a different focus from the observation).
- "evidence_frame_id" is the id of the ONE picture that best shows what the
  observation describes, copied exactly from the image ids listed with the
  evidence, or null when no single picture shows it. For a cue read from the
  numbers, that is the {METRICS_LABEL} picture's id. Never invent an id.
- Together at most {MAX_SPEECH_WORDS} words and {MAX_SPEECH_CHARACTERS}
  characters. No numbers, no units. Never mention the camera, the data, the
  images or yourself. Be short, specific and kind.

NEVER SAY "LEFT" OR "RIGHT". Say "lead" or "trail".
This is the rule most often broken and a broken line is thrown away whole.
ProTee's grid prints Left and Right because it describes the ball's flight in
the room. A body is not the room. "Your trail shoulder" is true for both
players; "your right shoulder" is wrong for half of them, and CaddieJ cannot
check which because it does not know what you meant. So a line containing
either word is refused and the player hears a stock sentence instead of
yours. Lead = the side nearer the target. Trail = the side further away."""

INSTRUCTIONS = """\
Reply with JSON matching the schema exactly. No prose outside the JSON.
Return compact JSON with no unnecessary whitespace."""


def possible_cues(
    club: ClubKind,
    *,
    has_metrics: bool,
    visual: Sequence[CueId] = (),
) -> tuple[CueId, ...]:
    """Every cue a reply may name before the numbers are known.

    Wider than what will be admitted afterwards, deliberately: the model reads
    the grid in the same request, so the set is every measured cue VX could
    support for this club, plus whichever visual cues the pictures unlock.
    The narrowing happens after the reply, against the numbers it read.
    """

    chosen: list[CueId] = []
    if has_metrics:
        for cue in CUES:
            if club not in cue.clubs:
                continue
            if cue.grade is not EvidenceGrade.MEASURED:
                continue
            if cue.focus not in VX_FOCUSES:
                continue
            chosen.append(cue.cue_id)
    chosen.extend(cue for cue in visual if cue not in chosen)
    return tuple(chosen)


# --- the wire: checkpoints by position, cells and directions by name ---------

WIRE_CHECKPOINTS: Mapping[SwingPhase, tuple[Checkpoint, ...]] = {
    phase: tuple(item for item in SCOREABLE if item.phase is phase)
    for phase in PHASE_ORDER
}
"""vx12's lists: each phase's scoreable checkpoints, catalogue order. Still
read, so a vx12 reply decodes; vx13 splits each of them by camera."""

WIRE_CAMERAS: Mapping[str, frozenset[PanelView]] = {
    "dtl": frozenset({PanelView.BEHIND}),
    "fo": frozenset({PanelView.SIDE}),
    "any": frozenset({PanelView.BEHIND, PanelView.SIDE}),
}
"""The camera a vx13 list may cite, by the suffix of its key."""


def wire_camera(spec: Checkpoint) -> str:
    """Which camera's list a checkpoint sits in: its views, exactly."""

    return next(camera for camera, views in WIRE_CAMERAS.items() if views == spec.views)


WIRE_LISTS: Mapping[str, tuple[Checkpoint, ...]] = {
    f"{phase.value}_{camera}": items
    for phase in PHASE_ORDER
    for camera in WIRE_CAMERAS
    if (
        items := tuple(
            spec for spec in WIRE_CHECKPOINTS[phase] if wire_camera(spec) == camera
        )
    )
}
"""Each list in a vx13 reply: one phase's checkpoints that one camera may
show, catalogue order. A position IS a checkpoint, so no name is written,
and a list of the wrong length is refused whole rather than read shifted.
Splitting by camera is what lets the schema offer a `_dtl` list only DTL
cells: a checkpoint read from the wrong camera cannot be written at all."""

WIRE_DIRECTIONS: tuple[str, ...] = tuple(
    dict.fromkeys(item.value for spec in SCOREABLE for item in spec.deviations)
)
"""Every direction a scoreable checkpoint has. The schema offers the words;
whether one belongs to the checkpoint at that position the parser checks."""

WIRE_METRICS = VX_METRICS
"""The metrics list in the reply: one position per registry metric."""

METRIC_ENTRY = 3
CHECKPOINT_ENTRY = 4
CONFIDENCE_SCALE = 100
"""Confidence travels as a whole percentage. Divided by this it is the 0..1
the model used to write, and it is then quantised to 0.05 and ceilinged
exactly as before: no reading the old form could express is lost."""

UNCLEAR_CODE = 0
_CAMERA_CODES = {"DTL": 10, "FO": 20}
_OTHER_CODES = 31
_CODED_CELL = re.compile(r"(DTL|FO)_P([1-9])")
REFUSED = "refused:"
"""Prefix of what a misshapen wire entry becomes, so the parser that reads
the decoded reply refuses it under the reason that follows."""


def cell_codes(body_cells: Sequence[str]) -> dict[str, int]:
    """The number each body cell is cited by. Deterministic from the ids.

    `DTL_Pn` is 10+n and `FO_Pn` is 20+n, so the code says the camera and the
    aimed position the way the printed ID does. Any other cell -- IMPACT, an
    unaimed `DTL_03` -- is numbered from 31 in sheet order. Every code is
    listed in the prompt beside its ID, so none has to be worked out.
    """

    codes: dict[str, int] = {}
    used: set[int] = set()
    for cell in dict.fromkeys(body_cells):
        match = _CODED_CELL.fullmatch(cell)
        if match is not None:
            code = _CAMERA_CODES[match.group(1)] + int(match.group(2))
            if code not in used:
                codes[cell] = code
                used.add(code)
    following = _OTHER_CODES
    for cell in dict.fromkeys(body_cells):
        if cell in codes:
            continue
        while following in used:
            following += 1
        codes[cell] = following
        used.add(following)
    return codes


def _fixed(items: dict[str, Any], length: int) -> dict[str, Any]:
    return {
        "type": "array",
        "items": items,
        "minItems": length,
        "maxItems": length,
    }


def _codes_list(values: Sequence[str]) -> str:
    return " ".join(f"{code}={value}" for code, value in enumerate(values, start=1))


MAX_OBSERVATION_CHARACTERS = 80
"""How much of a vx10 checkpoint note is kept when a reply carries one.

The schema no longer asks for a note: up to sixty rode in a reply, they were
the largest single part of its output, and none was ever shown to a player."""

_OBSERVATION_ALLOWED = re.compile(r"[^A-Za-z0-9 ,.'\-/]")

CHECKPOINT_CONFIDENCE_STEP = 0.05
CHECKPOINT_CONFIDENCE_CEILING = 0.90
"""A model's confidence in a picture, quantised and capped below certainty.

Higher than the 0.60 a promoted visual *cue* may carry, because this is the
confidence in one observation of one still, not in a lesson, and CaddieJ's
fault inference applies its own thresholds on top."""

_VIEW_BY_WORD: Mapping[str, PanelView] = {
    word: view for view, word in CHECKPOINT_VIEW_WORDS.items()
}


def _swing_part_schema(frame_ids: Sequence[str]) -> dict[str, Any]:
    """One rated part of the visible swing, or null.

    `frame_id` is required rather than optional, and is an enum of the body
    pictures only. A rating the model cannot point at is a rating CaddieJ
    cannot check, and the cheapest place to refuse one is in the schema.
    """

    return {
        "anyOf": [
            {"type": "null"},
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["rating", "confidence", "frame_id"],
                "properties": {
                    "rating": {
                        "type": "string",
                        "enum": [item.value for item in VisualRating],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "frame_id": _choice(frame_ids),
                },
            },
        ]
    }


COMPONENT_PHASES: Mapping[ScoreComponentId, frozenset[SwingPhase]] = {
    ScoreComponentId.SWING_PLANE: frozenset(
        {SwingPhase.TOP, SwingPhase.TRANSITION, SwingPhase.DOWNSWING}
    ),
    ScoreComponentId.TRANSITION: frozenset({SwingPhase.TOP, SwingPhase.TRANSITION}),
    ScoreComponentId.POSTURE: frozenset(
        {SwingPhase.DOWNSWING, SwingPhase.IMPACT, SwingPhase.SETUP}
    ),
    ScoreComponentId.BALANCE: frozenset({SwingPhase.FINISH}),
}
"""Which moments of a swing each rating may be read from.

A rating is about a moment, and a photograph of a different moment is not
weaker evidence for it -- it is no evidence for it. "Whether the finish was
held" cannot be read from the top of the backswing, however good the picture
is and however confident the reader.

That is not hypothetical. The first session with phase-aimed frames rated
`balance` good, at the confidence ceiling, citing a frame CaddieJ had labelled
`~impact` and which was in fact the top of the backswing; and `posture` --
whether the angles set at address survived to impact -- from a frame that was
the finish, with the ball already gone. Both became part of a visual score of
100.

The setup is admissible for posture because posture is the one thing an
address frame genuinely establishes: angles that were never set cannot survive.
It is not admissible for the other three, which are all about motion."""


def _an(word: str) -> str:
    """The word with the article English wants in front of it.

    Small, and worth it. "A iron" in a prompt is a detail the model will
    forgive and the next person to read the request will not.
    """

    return f"{'An' if word[:1].lower() in 'aeiou' else 'A'} {word}"


def _frames_schema(body_frame_ids: Sequence[str]) -> dict[str, Any]:
    """What each swing picture actually shows, as the model reads it.

    The most important thing added to this contract, and it exists because
    CaddieJ is demonstrably bad at knowing. Frames are aimed by timing -- a
    calibrated fraction of each camera's replay -- and the first session with
    that aiming in place sent a photograph of the top of the backswing
    labelled `~impact`, and a post-impact finish frame labelled `~impact` from
    the other camera. The model rated "whether the finish was held" from the
    first and "whether the angles survived to impact" from the second, and
    answered `good` at the confidence ceiling for both.

    Nothing about that is the model being careless. It was asked about a
    phase, shown a picture, and given no way to say "that is not what this
    is". So now it says what it sees, before it rates anything, and CaddieJ
    keys the assessment on the answer rather than on its own guess. A
    calibration that drifts becomes a visible disagreement instead of a
    confident rating of the wrong moment.

    `unclear` is a first-class answer. A picture mid-way between two phases,
    or one with the hands cropped out of frame, is honestly unclear, and
    forcing a choice would manufacture the certainty this is meant to remove.

    Keyed by the printed cell ID, every one required: the model writes the ID
    it is looking at beside the phase it sees, so neither has to be held by
    position. vx12 asked for one phase code per picture in listed order, and
    on the bay the answer came back as the phases counted 1 to 9, which
    placed one face-on picture wrongly and the rest not at all.
    """

    ids = list(dict.fromkeys(body_frame_ids))
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ids,
        "properties": {frame_id: {"$ref": "#/$defs/phase"} for frame_id in ids},
    }


def camera_cells(
    body_frame_ids: Sequence[str], cell_views: Mapping[str, PanelView] | None
) -> dict[str, frozenset[PanelView]]:
    """The cells a checkpoint may cite, and the cameras each counts as.

    With `cell_views` -- what an evidence sheet knows about every cell -- only
    a cell from a body camera is one, so the IMPACT graphic is not. Without it
    a picture's camera is not known here, every body picture is offered to
    every list, and the parser's view check is the whole of the restriction.
    """

    body = list(dict.fromkeys(body_frame_ids))
    if cell_views is None:
        return {cell: frozenset(PHASE_VIEW_ORDER) for cell in body}
    return {
        cell: frozenset({cell_views[cell]})
        for cell in body
        if cell_views.get(cell) in PHASE_VIEW_ORDER
    }


def _sentence() -> dict[str, Any]:
    return {
        "anyOf": [
            {"type": "null"},
            {"type": "string", "maxLength": MAX_SPEECH_CHARACTERS},
        ]
    }


def _choice(values: Sequence[str]) -> dict[str, Any]:
    """Null, or one of `values`. Only null when there is nothing to choose."""

    unique = list(dict.fromkeys(values))
    if not unique:
        return {"type": "null"}
    return {"anyOf": [{"type": "null"}, {"type": "string", "enum": unique}]}


def reply_schema(
    cues: Sequence[CueId],
    frame_ids: Sequence[str] = (),
    body_frame_ids: Sequence[str] = (),
    cell_views: Mapping[str, PanelView] | None = None,
) -> dict[str, Any]:
    """Strict JSON Schema: every key required, absence spelled as null.

    `frame_ids` are the ids of the images this request carries, and they are
    the whole of what `evidence_frame_id` may say. A model asked to cite a
    picture by name will otherwise produce a plausible one; enumerating them
    turns that into a choice it cannot get wrong in the one way that matters.

    `body_frame_ids` are the subset that show a golfer rather than a grid, and
    are the whole of what a swing rating may cite. Separating the two lists is
    what stops "the posture was lost, see the All Values image" from being a
    sentence the schema permits.

    The vx13 wire: `frames` maps each camera cell's printed ID to the phase
    seen in it; `metrics` is one coded entry per registry metric; and
    `checkpoints` is one fixed-length list per phase and camera, where a
    position is a checkpoint and an entry is `[score, cell, confidence,
    direction]` with the cell's printed ID and the direction's word. A
    `_dtl` list can only cite DTL cells, a `_fo` list FO cells, an `_any`
    list either, and none can cite IMPACT or ALL_VALUES. Which direction
    belongs to which checkpoint the schema does not say; the parser checks.
    With no camera cell for a list, its entries can only be null.
    """

    cue_schema = _choice([cue.value for cue in cues])
    cameras = camera_cells(body_frame_ids, cell_views)
    defs: dict[str, Any] = {
        "phase": {
            "type": "string",
            "enum": [phase.value for phase in PHASE_ORDER] + [UNCLEAR_PHASE],
        }
    }
    for camera, views in WIRE_CAMERAS.items():
        cited = [cell for cell, seen in cameras.items() if seen & views]
        defs[camera] = (
            {
                "anyOf": [
                    {"type": "null"},
                    _fixed(
                        {
                            "anyOf": [
                                {"type": "integer", "minimum": 0, "maximum": 100},
                                {"type": "string", "enum": cited},
                                {"$ref": "#/$defs/dir"},
                                {"type": "null"},
                            ]
                        },
                        CHECKPOINT_ENTRY,
                    ),
                ]
            }
            if cited
            else {"type": "null"}
        )
    if any(defs[camera] != {"type": "null"} for camera in WIRE_CAMERAS):
        defs["dir"] = {"type": "string", "enum": list(WIRE_DIRECTIONS)}
    reading = {"anyOf": [{"type": "null"}, _fixed({"type": "number"}, METRIC_ENTRY)]}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["frames", "metrics", "checkpoints", "coach"],
        "$defs": defs,
        "properties": {
            "frames": _frames_schema(list(cameras)),
            "metrics": _fixed(reading, len(WIRE_METRICS)),
            "checkpoints": {
                "type": "object",
                "additionalProperties": False,
                "required": list(WIRE_LISTS),
                "properties": {
                    key: _fixed(
                        {"$ref": f"#/$defs/{key.rsplit('_', 1)[1]}"}, len(checkpoints)
                    )
                    for key, checkpoints in WIRE_LISTS.items()
                },
            },
            "coach": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "kind",
                    "cue",
                    "observation",
                    "instruction",
                    "reinforcement",
                    "evidence_frame_id",
                ],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            AdviceKind.CORRECTION.value,
                            AdviceKind.REINFORCEMENT.value,
                            AdviceKind.NONE.value,
                        ],
                    },
                    "cue": cue_schema,
                    "observation": _sentence(),
                    "instruction": _sentence(),
                    "reinforcement": _sentence(),
                    "evidence_frame_id": _choice(frame_ids),
                },
            },
        },
    }


def render_evidence(
    *,
    club: ClubKind,
    handedness: Handedness,
    has_metrics: bool,
    swing_images: Sequence[str],
    cues: Sequence[CueId],
    session_focus: CoachingFocus | None,
    focus_shots: int,
    recent_lines: Sequence[str] = (),
    frames: Sequence[tuple[str, str]] = (),
    sheet: bool = False,
    cell_views: Mapping[str, PanelView] | None = None,
) -> str:
    """What the model is told about this shot, beyond the pictures.

    `frames` is `(frame_id, label)` for every image, in request order. Listed
    here as well as announced beside each picture, so the ids the schema
    offers arrive with what each one shows before the model has to choose.

    `sheet` says the pictures arrive as cells of one Swing Evidence Sheet,
    each under its printed ID, rather than as separate images. `cell_views`
    is each cell's camera, as for `reply_schema`.
    """

    low, high = FUNCTIONAL_PATH.get(club, (0.0, 0.0))
    lines = [
        f"Club: {club.value}.",
        f"Player handedness: {handedness.value}.",
    ]
    if sheet:
        lines.append(
            "All pictures arrive as ONE image, the Swing Evidence Sheet. Each "
            "cell is one picture, its ID printed in the coloured bar above it "
            "with CaddieJ's ~moment and that camera's time. A picture IS a cell "
            "and its id IS the printed cell ID: cite exactly that. DTL_ cells "
            "(blue) are SWING_BEHIND, down_the_line. FO_ cells (green) are "
            "SWING_SIDE, face_on. IMPACT is ProTee's impact graphic. "
            f"{METRICS_LABEL} (brown, along the bottom) is the All Values grid."
        )
    lines.append(
        f"The image labelled {METRICS_LABEL} is ProTee's All Values grid."
        if has_metrics
        else f"No {METRICS_LABEL} image was captured; every metric is null."
    )
    if not swing_images:
        lines.append("No swing pictures were captured.")
    elif not frames:
        lines.append("Swing pictures: " + ", ".join(swing_images) + ".")
    if low != high:
        # Said here rather than left for the model to infer, because it cannot
        # infer it: it reads the path off the grid in this same request, and
        # nothing in the pictures says what a healthy path is for a seven
        # iron. Without this it proposed a path cue on two of four shots whose
        # paths were fine, and the replies were refused -- so the player heard
        # a stock line instead. Zero is not the target and never was.
        lines.append(
            f"{_an(club.value)} club path between {abs(low):g} degrees out-to-in "
            f"and {high:g} degrees in-to-out is functional and is NOT a fault. "
            "Do not name a path cue for a path inside that range, and do not "
            "describe it as a fault. A path has a sign because a swing travels "
            "on an arc."
        )
    body = [
        (frame_id, label)
        for frame_id, label in frames
        if frame_id not in (METRICS_FRAME_ID, METRICS_LABEL)
    ]
    cameras = camera_cells([frame_id for frame_id, _ in body], cell_views)
    if cameras:
        lines.append(
            "Camera cells, each a key of frames and the only cells a checkpoint "
            "may cite, as ID ~aimed moment: "
            + ", ".join(
                frame_id + ("" if label == frame_id else f" {_moment(label)}")
                for frame_id, label in body
                if frame_id in cameras
            )
            + "."
        )
        others = [frame_id for frame_id, _ in frames if frame_id not in cameras]
        if others:
            lines.append(
                ", ".join(others)
                + " are not camera cells: never a key of frames and never cited "
                "by a checkpoint."
            )
    else:
        lines.append(
            "No swing picture was sent; frames is empty and every checkpoint is null."
        )
    if frames:
        lines.append(
            "Image ids for evidence_frame_id: "
            + ", ".join(frame_id for frame_id, _ in frames)
            + "."
        )
    else:
        lines.append("No image has an id; evidence_frame_id is null.")
    lines.append("Metrics, in order, as N label: unit codes; direction codes.")
    lines.extend(_metric_lines())
    lines.append(
        "Swing checkpoints, one numbered list per phase and camera: a _dtl list "
        "is read only from DTL_ cells, a _fo list only from FO_ cells, an _any "
        "list from either. Each line is: N key (moments it may be read from) - "
        "what to look for and the functional range; its direction words."
    )
    lines.extend(_checkpoint_lines())
    if session_focus is not None:
        lines.append(
            f"This session is working on {session_focus.value.replace('_', ' ')} "
            f"({focus_shots} shots so far). Prefer it when it is nearly as "
            "important as anything else."
        )
    if recent_lines:
        lines.append("Recently said, do not repeat: " + " | ".join(recent_lines) + ".")
    lines.append(
        "Cues you may choose. Choose only from this list; it is the whole of "
        "what you may say:"
    )
    for cue_id in cues:
        cue = CUES_BY_ID[cue_id]
        lines.append(
            f"- {cue_id.value} ({cue.grade.value} {cue.kind.value}, focus "
            f"{cue.focus.value.replace('_', ' ')}, floor shows {cue.observed} "
            f"then {cue.display}): {cue.explanation}"
        )
    return "\n".join(lines)


def _moment(label: str) -> str:
    """A picture's label without its camera, which its id already names:
    "SWING_BEHIND / DOWN_THE_LINE ~top @3.7s" is "~top @3.7s" beside DTL_P4."""

    head, tilde, rest = label.partition("~")
    return tilde + rest if tilde else head


_VIEW_NAMES: Mapping[PanelView, str] = {
    PanelView.BEHIND: "SWING_BEHIND",
    PanelView.SIDE: "SWING_SIDE",
}


def _checkpoint_lines() -> tuple[str, ...]:
    """The catalogue as the model reads it, generated from the catalogue itself.

    Never written out twice: a checkpoint whose prompt text and whose
    admissibility rules were maintained separately would drift apart, and the
    model would be asked about one thing and judged on another. Checkpoints
    this bay cannot assess are not listed and are not in the schema. Numbered
    within their list, because the number is the position in the reply.
    """

    lines: list[str] = []
    for key, items in WIRE_LISTS.items():
        lines.append(f"{key} ({len(items)}):")
        for number, item in enumerate(items, start=1):
            moments = ", ".join(
                seen.value for seen in PHASE_ORDER if seen in item.phases
            )
            club = " Null unless the whole club is visible." if item.club else ""
            directions = (
                " dir " + "|".join(d.value for d in item.deviations)
                if item.deviations
                else " dir null"
            )
            lines.append(
                f"{number} {item.checkpoint_id.value} ({moments}) - "
                f"{item.observable}.{club}{directions}"
            )
    return tuple(lines)


def _metric_lines() -> tuple[str, ...]:
    lines: list[str] = []
    for number, spec in enumerate(WIRE_METRICS, start=1):
        directions = (
            "; signed as printed, dir 0"
            if spec.signed
            else (
                f"; dir {_codes_list([d.value for d in spec.directions])}"
                if spec.directions
                else ""
            )
        )
        lines.append(
            f"{number} {spec.label}: unit "
            f"{_codes_list([u.value for u in spec.units])}{directions}"
        )
    return tuple(lines)


def sent_frame_ids(images: Sequence[ImageInput]) -> tuple[str, ...]:
    """The ids a reply may cite, in request order, each once."""

    return tuple(dict.fromkeys(image.frame_id for image in images if image.frame_id))


def body_frame_ids(images: Sequence[ImageInput]) -> tuple[str, ...]:
    """The ids that show a golfer, which is the only evidence a body claim has.

    Everything sent except the All Values grid. A still of ProTee's numbers is
    perfectly good evidence for a measured cue and is no evidence at all about
    a posture, so it is not offered where a posture is being rated.
    """

    return tuple(
        frame_id for frame_id in sent_frame_ids(images) if frame_id != METRICS_FRAME_ID
    )


_body_ids = body_frame_ids


def build_request(
    *,
    evidence: str,
    images: Sequence[ImageInput],
    cues: Sequence[CueId],
    max_output_tokens: int,
    timeout_seconds: float,
    reasoning_effort: str | None = None,
    frame_ids: Sequence[str] | None = None,
    body_frame_ids: Sequence[str] | None = None,
    cell_views: Mapping[str, PanelView] | None = None,
) -> AdviceRequest:
    """One request. The citable frame ids are read off the images themselves,
    so the schema can only ever offer ids this request really carries.

    An evidence sheet is one image carrying many pictures, so it names them
    itself: `frame_ids` and `body_frame_ids` are then its printed cell IDs,
    and `cell_views` the camera of each body cell."""

    citable = sent_frame_ids(images) if frame_ids is None else tuple(frame_ids)
    body = _body_ids(images) if body_frame_ids is None else tuple(body_frame_ids)
    return AdviceRequest(
        system=SYSTEM,
        instructions=INSTRUCTIONS,
        evidence=evidence,
        schema=reply_schema(cues, citable, body, cell_views),
        images=tuple(images),
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        schema_name=SCHEMA_NAME,
        reasoning_effort=reasoning_effort,
    )


# --- reading the reply ---------------------------------------------------

_CITATION_KEYS = frozenset({"frame_id", "evidence_frame_id", "cell"})
_KEYED_BY_FRAME = frozenset({"frames"})


def canonical_reply(payload: Mapping[str, Any], cells: Mapping[str, str]) -> Any:
    """The reply with every printed cell ID replaced by its original frame id.

    Done once, before anything reads the reply, so every check after it --
    which frames were sent, which camera each came from, which phase the model
    said each shows, that All Values is never a body -- runs on the canonical
    ids it always ran on, and everything stored names the original frame
    rather than a place on a sheet. A value that is not a printed ID is left
    exactly as it was, and is then judged as the stranger it is. `frames` is
    keyed by picture, so there it is the keys that are translated.
    """

    def walk(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: (
                    cells.get(item, item)
                    if key in _CITATION_KEYS and isinstance(item, str)
                    else (
                        {cells.get(str(k), k): walk(v) for k, v in item.items()}
                        if key in _KEYED_BY_FRAME and isinstance(item, Mapping)
                        else walk(item)
                    )
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(payload)


def decode_wire(payload: Mapping[str, Any], body_cells: Sequence[str]) -> Any:
    """A vx13 or vx12 positional reply as the named reply the parsers read.

    Pure. `body_cells` are the body cell ids in the order the request listed
    them, which is what vx12's cell and frame codes were built from; vx13
    names cells, and its `frames` is already the named form. Each part is
    decoded only if it is in a positional form, so a vx10 or vx11 reply
    passes through untouched.

    Nothing is repaired here and nothing is judged that the parsers judge.
    A position becomes a name, a code becomes the word or the cell it stands
    for, and anything that does not decode becomes a value the parser will
    refuse for the same reason it always would: an unknown cell code is an
    unknown cell (`frame`), a confidence over 100 is over 1 (`confidence`),
    a direction code the checkpoint does not have is an unknown direction
    (`deviation`). Only what cannot be said in the named form gets its own
    reason: a list of the wrong length (`length`) -- every position in it is
    refused, because once one is missing none of them can be trusted to be
    the checkpoint it sits at -- and an entry that is not four whole numbers
    (`shape`).
    """

    decoded = dict(payload)
    cells = list(dict.fromkeys(body_cells))
    frames = payload.get("frames")
    if _positional(frames):
        decoded["frames"] = _decode_frames(frames, cells)
    metrics = payload.get("metrics")
    if isinstance(metrics, list):
        decoded["metrics"] = _decode_metrics(metrics)
    checkpoints = payload.get("checkpoints")
    if isinstance(checkpoints, Mapping) and any(
        key in WIRE_LISTS for key in checkpoints
    ):
        decoded["checkpoints"] = _decode_lists(checkpoints)
    elif isinstance(checkpoints, Mapping) and any(
        isinstance(item, list) for item in checkpoints.values()
    ):
        by_code = {code: cell for cell, code in cell_codes(cells).items()}
        decoded["checkpoints"] = _decode_checkpoints(checkpoints, by_code)
    return decoded


def _decode_lists(raw: Mapping[str, Any]) -> dict[str, Any]:
    """vx13: one list per phase and camera, entries naming cell and direction.

    Which camera a list was for is not carried over: the checkpoint at that
    position already says which views it may be read from, and the parser
    holds the cited cell to it, so a list that somehow cited the wrong
    camera is refused as `view` exactly as a named entry would be.
    """

    decoded: dict[str, Any] = {
        str(key): item for key, item in raw.items() if key not in WIRE_LISTS
    }
    for key, specs in WIRE_LISTS.items():
        entries = raw.get(key)
        if not isinstance(entries, list) or len(entries) != len(specs):
            for spec in specs:
                decoded[spec.checkpoint_id.value] = REFUSED + "length"
            continue
        for spec, entry in zip(specs, entries, strict=True):
            decoded[spec.checkpoint_id.value] = _decode_named(entry)
    return decoded


def _decode_named(entry: object) -> object:
    """`[score, cell, confidence, direction]`: two integers around a cell ID,
    then a direction word or null. Anything else is `shape`."""

    if entry is None:
        return None
    if not isinstance(entry, list) or len(entry) != CHECKPOINT_ENTRY:
        return REFUSED + "shape"
    score, cell, confidence, direction = entry
    whole_score, whole_confidence = _whole(score), _whole(confidence)
    if (
        whole_score is None
        or whole_confidence is None
        or not isinstance(cell, str)
        or not (direction is None or isinstance(direction, str))
    ):
        return REFUSED + "shape"
    return {
        "score": whole_score,
        "conf": whole_confidence / CONFIDENCE_SCALE,
        "cell": cell,
        "dir": direction,
    }


def _positional(raw: object) -> TypeGuard[list[Any]]:
    """A `frames` list that is not the vx10 list of `{frame_id, shows}`
    objects. `metrics` was never a list before vx12, so any list is one."""

    return isinstance(raw, list) and not any(isinstance(item, Mapping) for item in raw)


def _whole(raw: object) -> int | None:
    """A JSON integer. `80.0` is not one: the schema asks for integers, and a
    reply that writes otherwise has not followed it."""

    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _decode_frames(raw: list[Any], cells: Sequence[str]) -> dict[str, str]:
    """`{cell: phase}`. A list of the wrong length places no picture at all:
    which picture a code was meant for is exactly what is then unknown."""

    if len(raw) != len(cells):
        return {}
    seen: dict[str, str] = {}
    for cell, item in zip(cells, raw, strict=True):
        code = _whole(item)
        if code is not None and 1 <= code <= len(PHASE_ORDER):
            seen[cell] = PHASE_ORDER[code - 1].value
        else:
            seen[cell] = UNCLEAR_PHASE
    return seen


def _decode_metrics(raw: list[Any]) -> dict[str, Any]:
    if len(raw) != len(WIRE_METRICS):
        return {spec.name: REFUSED + "length" for spec in WIRE_METRICS}
    decoded: dict[str, Any] = {}
    for spec, item in zip(WIRE_METRICS, raw, strict=True):
        if item is None:
            decoded[spec.name] = None
            continue
        if not isinstance(item, list) or len(item) != METRIC_ENTRY:
            decoded[spec.name] = REFUSED + "shape"
            continue
        value, unit_code, direction_code = item
        unit = _coded(unit_code, [unit.value for unit in spec.units])
        direction = _coded(direction_code, [d.value for d in spec.directions])
        decoded[spec.name] = {
            "value": value,
            "unit": unit if unit is not None else REFUSED + "unit",
            "direction": direction if direction != "" else REFUSED + "direction",
        }
    return decoded


def _coded(raw: object, values: Sequence[str]) -> str | None:
    """1-based code to its value; 0 to None; anything else to "". A unit has
    no 0, so a 0 unit comes back None and is refused by the caller."""

    code = _whole(raw)
    if code == 0:
        return None
    if code is None or not 1 <= code <= len(values):
        return ""
    return values[code - 1]


def _decode_checkpoints(
    raw: Mapping[str, Any], by_code: Mapping[int, str]
) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    phases = {phase.value for phase in WIRE_CHECKPOINTS}
    for key, item in raw.items():
        if str(key) not in phases:
            decoded[str(key)] = item
    for phase, specs in WIRE_CHECKPOINTS.items():
        entries = raw.get(phase.value)
        if not isinstance(entries, list) or len(entries) != len(specs):
            for spec in specs:
                decoded[spec.checkpoint_id.value] = REFUSED + "length"
            continue
        for spec, entry in zip(specs, entries, strict=True):
            decoded[spec.checkpoint_id.value] = _decode_entry(spec, entry, by_code)
    return decoded


def _decode_entry(
    spec: Checkpoint, entry: object, by_code: Mapping[int, str]
) -> object:
    if entry is None:
        return None
    if not isinstance(entry, list) or len(entry) != CHECKPOINT_ENTRY:
        return REFUSED + "shape"
    codes = [code for code in (_whole(item) for item in entry) if code is not None]
    if len(codes) != CHECKPOINT_ENTRY:
        return REFUSED + "shape"
    score, cell_code, confidence, direction_code = codes
    direction = _coded(direction_code, [d.value for d in spec.deviations])
    decoded: dict[str, Any] = {
        "score": score,
        "conf": confidence / CONFIDENCE_SCALE,
        "cell": by_code.get(cell_code, f"code:{cell_code}"),
        "dir": direction if direction != "" else REFUSED + "deviation",
    }
    return decoded


def parse_metrics(
    raw: object,
    *,
    image: str | None = None,
    captured_at: datetime | None = None,
) -> VxMetrics:
    """Typed metrics, each validated alone. Never raises.

    A field that is present but wrong — an unknown unit, a direction this
    metric cannot have, a magnitude no golf shot has — is nulled and its name
    kept in `rejected`, so a misread is visible without being believed.
    """

    values: dict[str, VxMetric | None] = {spec.name: None for spec in VX_METRICS}
    rejected: list[str] = []
    if not isinstance(raw, Mapping):
        return VxMetrics(values=values, image=image, captured_at=captured_at)
    for spec in VX_METRICS:
        item = raw.get(spec.name)
        if item is None:
            continue
        reading = _reading(spec.name, item)
        if reading is None:
            rejected.append(spec.name)
            continue
        values[spec.name] = reading
    return VxMetrics(
        values=values,
        image=image,
        captured_at=captured_at,
        rejected=tuple(rejected),
    )


def parse_frames(raw: object) -> dict[str, SwingPhase]:
    """Which phase the model says each picture shows, by frame id.

    Omits anything it could not place. `unclear` is a real answer and is
    recorded as absence here, because the two have the same consequence: a
    rating may not be read from a picture whose moment is unknown.

    Never raises. A reply with no `frames` block at all -- an older model, a
    truncated answer -- yields an empty mapping, and the caller then has no
    identification to check against rather than a wrong one.

    Read from `{frame_id: phase}`, which is what the schema asks for, or from
    the vx10 list of `{frame_id, shows}`.
    """

    if isinstance(raw, Mapping):
        pairs: list[tuple[object, object]] = list(raw.items())
    elif isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        pairs = [
            (item.get("frame_id"), item.get("shows"))
            for item in raw
            if isinstance(item, Mapping)
        ]
    else:
        return {}
    seen: dict[str, SwingPhase] = {}
    for frame_id, shows in pairs:
        if not isinstance(frame_id, str) or not isinstance(shows, str):
            continue
        try:
            seen[frame_id] = SwingPhase(shows)
        except ValueError:
            # `unclear`, or a phase this build does not know. Both mean the
            # moment is not established, which is what leaving it out says.
            continue
    return seen


def parse_checkpoints(
    raw: object,
    *,
    body_frames: Collection[str],
    frame_phases: Mapping[str, SwingPhase] | None = None,
    frame_views: Mapping[str, PanelView] | None = None,
) -> tuple[tuple[CheckpointAssessment, ...], tuple[tuple[str, str], ...]]:
    """The typed checkpoint scores, and `(key, reason)` for whatever was refused.

    Never raises, and refuses rather than repairs. A refused checkpoint is
    absent, never zero. The reasons:

    * `unknown` -- a key that is not a checkpoint in the catalogue;
    * `not_scoreable` -- a checkpoint this bay cannot assess;
    * `shape`, `score` -- not an object, or a score that is not 0..100;
    * `confidence` -- not a number in 0..1;
    * `low_confidence` -- below the checkpoint's own minimum once quantised;
    * `frame` -- a frame id this request never sent;
    * `metrics_frame` -- the All Values grid, which shows nobody's swing;
    * `view_unknown` -- a frame from a camera CaddieJ has no view for;
    * `view_mismatch` -- a view the entry states is not the camera the frame
      came from;
    * `view` -- the checkpoint may not be read from that camera;
    * `phase_conflict` -- a phase the entry states disagrees with the reply's
      own `frames` identification of the cited picture;
    * `phase_unknown` -- no phase at all: the reply called the cited picture
      unclear, or never placed it, and the entry states none;
    * `phase` -- the checkpoint may not be read from that moment;
    * `deviation` -- a direction this checkpoint does not have;
    * `length`, from a vx12 reply -- its phase's list had the wrong number of
      positions, so no position in it is known to be this checkpoint.

    An entry is `{score, conf, cell, dir}`. Its view is the cell's camera and
    its phase is what `frames` says the cell shows, which is why neither is
    asked for. The vx10 names -- `confidence`, `frame_id`, `deviation`, and a
    stated `phase`, `view` and `observation` -- are read too, and a stated
    phase or view is still checked rather than trusted.

    Keys a reply adds outside `checkpoints` -- a Swing Score, phase scores, a
    list of faults -- are never read by anything.
    """

    assessments: list[CheckpointAssessment] = []
    rejected: list[tuple[str, str]] = []
    if not isinstance(raw, Mapping):
        return (), ()
    for key, item in raw.items():
        if item is None:
            continue
        name = str(key)[:40]
        try:
            checkpoint = CheckpointId(name)
        except ValueError:
            rejected.append((name, "unknown"))
            continue
        spec = CHECKPOINTS_BY_ID[checkpoint]
        if not spec.scoreable:
            rejected.append((name, "not_scoreable"))
            continue
        reading = _checkpoint_reading(
            spec, item, body_frames, frame_phases or {}, frame_views or {}
        )
        if isinstance(reading, str):
            rejected.append((name, reading))
            continue
        assessments.append(reading)
    order = {item.checkpoint_id: index for index, item in enumerate(SCOREABLE)}
    assessments.sort(key=lambda item: order[item.checkpoint])
    return tuple(assessments), tuple(rejected)


def _checkpoint_reading(
    spec: Checkpoint,
    item: object,
    body_frames: Collection[str],
    frame_phases: Mapping[str, SwingPhase],
    frame_views: Mapping[str, PanelView],
) -> CheckpointAssessment | str:
    if isinstance(item, str) and item.startswith(REFUSED):
        return item.removeprefix(REFUSED)
    if not isinstance(item, Mapping):
        return "shape"
    raw_score = item.get("score")
    if isinstance(raw_score, bool) or not isinstance(raw_score, int | float):
        return "score"
    if not math.isfinite(raw_score) or not 0 <= raw_score <= 100:
        return "score"
    confidence = _checkpoint_confidence(_field(item, "conf", "confidence"))
    if confidence is None:
        return "confidence"
    if confidence < spec.min_confidence:
        return "low_confidence"
    frame_id = _field(item, "cell", "frame_id")
    if frame_id == METRICS_FRAME_ID:
        return "metrics_frame"
    if not isinstance(frame_id, str) or frame_id not in body_frames:
        return "frame"
    view = frame_views.get(frame_id, PanelView.NONE)
    if view is PanelView.NONE:
        return "view_unknown"
    if "view" in item and _VIEW_BY_WORD.get(str(item.get("view"))) is not view:
        return "view_mismatch"
    if view not in spec.views:
        return "view"
    seen = frame_phases.get(frame_id)
    phase = seen
    if "phase" in item:
        try:
            phase = SwingPhase(str(item.get("phase")))
        except ValueError:
            return "phase"
        if seen is not None and seen is not phase:
            return "phase_conflict"
    if phase is None:
        return "phase_unknown"
    if phase not in spec.phases:
        return "phase"
    deviation: Deviation | None = None
    raw_deviation = _field(item, "dir", "deviation")
    if raw_deviation is not None:
        try:
            deviation = Deviation(str(raw_deviation))
        except ValueError:
            return "deviation"
        if deviation not in spec.deviations:
            return "deviation"
    return CheckpointAssessment(
        checkpoint=spec.checkpoint_id,
        score=round(raw_score),
        confidence=confidence,
        frame_id=frame_id,
        phase=phase,
        view=view,
        observation=_observation_text(item.get("observation")),
        deviation=deviation,
    )


def _field(item: Mapping[str, Any], name: str, vx10: str) -> object:
    return item[name] if name in item else item.get(vx10)


def _checkpoint_confidence(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = float(raw)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    steps = round(value / CHECKPOINT_CONFIDENCE_STEP)
    return round(
        min(CHECKPOINT_CONFIDENCE_CEILING, steps * CHECKPOINT_CONFIDENCE_STEP), 2
    )


def _confidence_of(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = float(raw)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    return cap_visual_confidence(value)


def _observation_text(raw: object) -> str:
    """The model's note, reduced to something safe to store and to re-read.

    Length capped and cut down to an explicit character set, like every other
    piece of model prose, even though this one never reaches a browser.
    """

    if not isinstance(raw, str):
        return ""
    text = " ".join(_OBSERVATION_ALLOWED.sub(" ", raw).split())
    return text[:MAX_OBSERVATION_CHARACTERS].strip()


MAX_LOGGED_REJECTIONS = 12


def checkpoints_summary(
    assessments: Sequence[CheckpointAssessment],
    rejected: Sequence[tuple[str, str]],
) -> dict[str, object]:
    """What may be logged about an assessment: keys, scores and reasons, never
    prose, bounded so a reply that refuses everything stays one line."""

    reasons: dict[str, int] = {}
    for _, reason in rejected:
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "coach_checkpoints_scored": len(assessments),
        "coach_checkpoints_scores": ",".join(
            f"{item.checkpoint.value}={item.score}" for item in assessments
        )
        or "none",
        "coach_checkpoints_rejected": len(rejected),
        "coach_checkpoints_rejections": ",".join(
            f"{name}:{reason}" for name, reason in rejected[:MAX_LOGGED_REJECTIONS]
        )
        or "none",
        "coach_checkpoints_reasons": ",".join(
            f"{reason}={count}" for reason, count in sorted(reasons.items())
        )
        or "none",
    }


def parse_visual(
    raw: object,
    *,
    body_frames: Collection[str],
    frame_phases: Mapping[str, SwingPhase] | None = None,
) -> tuple[tuple[VisualObservation, ...], tuple[str, ...]]:
    """The typed swing rating, and the names of whatever was thrown away.

    Never raises, and refuses rather than repairs. Four things can disqualify
    an entry, and all four are reasons to have no rating rather than a weak
    one:

    * a rating outside the three the vocabulary has;
    * a confidence that is not a number, is outside 0..1, or is below
      `VISUAL_CONFIDENCE_FLOOR` once capped — a model saying it is not sure is
      a model that should not be scored;
    * a frame id that was never sent, which is a picture it did not see;
    * a frame id that is the All Values grid, which is not a picture of a
      golfer whatever it is a picture of;
    * a cited frame the model itself says shows a moment this rating cannot
      be read from. See `COMPONENT_PHASES`. Its own identification is used
      rather than CaddieJ's timing guess, because CaddieJ's guess is the thing
      that was wrong: it is aiming a camera with a stopwatch.

    A reply with no frame identifications is checked on the other four rules
    only. That is the old behaviour, and it is the right answer for a reply
    that never claimed to know what it was looking at.

    The confidence is quantized and ceilinged on the way through, by the same
    `cap_visual_confidence` every other model impression goes through, so a
    look at a photograph can never out-rank a measurement anywhere.
    """

    observations: list[VisualObservation] = []
    rejected: list[str] = []
    if not isinstance(raw, Mapping):
        return (), ()
    for component in VISUAL_ORDER:
        item = raw.get(component.value)
        if item is None:
            continue
        observation = _visual_reading(component, item, body_frames)
        if observation is None:
            rejected.append(component.value)
            continue
        if not _phase_supports(component, observation.frame_id, frame_phases):
            rejected.append(component.value)
            continue
        observations.append(observation)
    return tuple(observations), tuple(rejected)


def _phase_supports(
    component: ScoreComponentId,
    frame_id: str | None,
    frame_phases: Mapping[str, SwingPhase] | None,
) -> bool:
    """Whether the cited picture shows a moment this rating can come from.

    True when there is nothing to check against -- no identifications, or none
    for this frame. Silence is not disagreement, and a rating is not thrown
    away because the model declined to place the picture it cited; that would
    punish the honesty this field exists to collect.
    """

    if not frame_phases or frame_id is None:
        return True
    seen = frame_phases.get(frame_id)
    if seen is None:
        return True
    allowed = COMPONENT_PHASES.get(component)
    return allowed is None or seen in allowed


def _visual_reading(
    component: ScoreComponentId, item: object, body_frames: Collection[str]
) -> VisualObservation | None:
    if not isinstance(item, Mapping):
        return None
    try:
        rating = VisualRating(str(item.get("rating")))
    except ValueError:
        return None
    raw_confidence = item.get("confidence")
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, int | float):
        return None
    confidence = float(raw_confidence)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    capped = cap_visual_confidence(confidence)
    if capped is None:
        return None
    frame_id = item.get("frame_id")
    if not isinstance(frame_id, str) or frame_id not in body_frames:
        return None
    if frame_id == METRICS_FRAME_ID:
        return None
    return VisualObservation(
        component=component,
        rating=rating,
        confidence=capped,
        frame_id=frame_id,
    )


def visual_summary(
    observations: Sequence[VisualObservation], rejected: Sequence[str]
) -> dict[str, object]:
    """What may be logged about a swing rating: names and grades, never prose."""

    return {
        "coach_visual_rated": len(observations),
        "coach_visual_components": ",".join(
            f"{item.component.value}={item.rating.value}" for item in observations
        )
        or "none",
        "coach_visual_frames": ",".join(item.frame_id for item in observations)
        or "none",
        "coach_visual_rejected": ",".join(rejected) or "none",
    }


def _reading(name: str, item: object) -> VxMetric | None:
    spec = VX_METRICS_BY_NAME[name]
    if not isinstance(item, Mapping):
        return None
    value = item.get("value")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or abs(number) > spec.limit:
        return None
    raw_unit = item.get("unit")
    if raw_unit is None and len(spec.units) == 1:
        raw_unit = spec.units[0].value
    try:
        unit = MetricUnit(str(raw_unit))
    except ValueError:
        return None
    if unit not in spec.units:
        return None
    raw_direction = item.get("direction")
    direction: MetricDirection | None = None
    if raw_direction is not None:
        try:
            direction = MetricDirection(str(raw_direction))
        except ValueError:
            return None
        if direction not in spec.directions:
            return None
    if spec.signed:
        return _signed_reading(spec, number, unit, direction)
    if direction is not None and number < 0:
        # ProTee prints "3.1° Closed", never a sign beside a word, so
        # "-3.1 Closed" says the same thing twice and could mean either.
        return None
    return VxMetric(value=number, unit=unit, direction=direction)


def _signed_reading(
    spec: MetricSpec, number: float, unit: MetricUnit, direction: MetricDirection | None
) -> VxMetric | None:
    """A metric ProTee prints as a signed number, as that signed number.

    The sign is the reading; a word beside it is redundant, and is only
    believed when it agrees. "-6.3 down" and "6.3 down" are both -6.3, which
    is what the grid shows. "-6.3 up" is two answers that cannot both be
    true, and is refused rather than settled either way.
    """

    _, negative = spec.directions
    if direction is negative:
        return VxMetric(value=-abs(number) if number else 0.0, unit=unit)
    if direction is not None and number < 0:
        return None
    return VxMetric(value=number, unit=unit)


# --- CaddieJ's sign convention ---------------------------------------------

# For each directional metric: which word means positive in the target frame
# `faults.py` reads (positive is right of target, toe, up), and whether the word
# is relative to the player — open and in-to-out are, left and right are not —
# and so has to be unmirrored here for `oriented()` to mirror it back.
_POSITIVE: dict[str, tuple[MetricDirection, bool]] = {
    "face_angle": (MetricDirection.OPEN, True),
    "swing_path": (MetricDirection.IN_TO_OUT, True),
    "attack_angle": (MetricDirection.UP, False),
    "launch_direction": (MetricDirection.RIGHT, False),
    "spin_axis": (MetricDirection.RIGHT, False),
    "impact_width": (MetricDirection.TOE, False),
}


def signed(metrics: VxMetrics, name: str, handedness: Handedness) -> float | None:
    """A reading in CaddieJ's convention, or None if it cannot be ranked.

    Undirected readings of a directional metric are None unless they are zero:
    a bare "-2.1" carries ProTee's sign, and which way that is for this player
    is not something to guess. Attack angle is the one exception, because up
    and down are not handed and every launch monitor writes down as negative.
    """

    reading = metrics.get(name)
    if reading is None:
        return None
    value = reading.value
    rule = _POSITIVE.get(name)
    if rule is None:
        return value
    positive, player_relative = rule
    if reading.direction is None:
        if value == 0.0 or name == "attack_angle":
            return value
        return None
    magnitude = value if reading.direction is positive else -value
    if player_relative and handedness is Handedness.LEFT:
        return -magnitude
    return magnitude


def _speed_mph(metrics: VxMetrics, name: str) -> float | None:
    reading = metrics.get(name)
    if reading is None or reading.unit not in _MPH:
        return None
    return reading.value * _MPH[reading.unit]


def _yards(metrics: VxMetrics, name: str) -> float | None:
    reading = metrics.get(name)
    if reading is None or reading.unit not in _YARDS:
        return None
    return reading.value * _YARDS[reading.unit]


def _millimetres(value: float | None, metrics: VxMetrics, name: str) -> float | None:
    reading = metrics.get(name)
    if value is None or reading is None:
        return None
    return value * _MM_PER_INCH if reading.unit is MetricUnit.IN else value


def shot_from_metrics(
    metrics: VxMetrics,
    *,
    shot_id: UUID,
    club: Club | None,
    handedness: Handedness,
    observed_at: datetime,
    observed_monotonic: float,
) -> Shot | None:
    """The readings as a CaddieJ `Shot`, for diagnosis only. None if empty.

    Never persisted as a shot: it is not one GSPro or a device API reported,
    and the shots table means exactly that. It exists so that one ranking of
    faults serves both paths.
    """

    ball_speed = _speed_mph(metrics, "ball_speed")
    ball = None
    if ball_speed is not None:
        ball = BallLaunch(
            speed_mph=ball_speed,
            launch_angle_degrees=signed(metrics, "launch_angle", handedness),
            launch_direction_degrees=signed(metrics, "launch_direction", handedness),
            total_spin_rpm=signed(metrics, "total_spin", handedness),
            spin_axis_degrees=signed(metrics, "spin_axis", handedness),
            back_spin_rpm=signed(metrics, "back_spin", handedness),
            carry_yards=_yards(metrics, "carry"),
        )
    delivery = ClubDelivery(
        speed_mph=_speed_mph(metrics, "club_speed"),
        angle_of_attack_degrees=signed(metrics, "attack_angle", handedness),
        path_degrees=signed(metrics, "swing_path", handedness),
        face_to_target_degrees=signed(metrics, "face_angle", handedness),
        loft_degrees=signed(metrics, "dynamic_loft", handedness),
        impact_horizontal_mm=_millimetres(
            signed(metrics, "impact_width", handedness), metrics, "impact_width"
        ),
    )
    if ball is None and delivery.is_empty:
        return None
    return Shot(
        shot_id=shot_id,
        source=ShotSource(
            kind="synthetic",
            device_session_id=uuid4(),
            observed_at=observed_at,
            observed_monotonic=observed_monotonic,
            device_id="protee_vx_all_values",
        ),
        ball=ball,
        club_delivery=None if delivery.is_empty else delivery,
        handedness=handedness,
        club=club,
    )


def coach_payload(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The `coach` half of a reply, or None when it is missing or malformed.

    The parts are joined into the one `speech` the validator judges as a
    whole, so splitting them in the schema changes what the model is asked for
    and nothing about what is accepted: still at most two sentences, still
    exactly one instruction.

    With an observation, the line is the observation and then the one thing
    to do — the instruction for a correction, and for a reinforcement the
    praise, or nothing. A reinforcement does not borrow the instruction: this
    read `instruction or reinforcement`, and the first hardware session said
    "Your clubface pointed right at impact. Turn the trail forearm over
    through impact." under the heading GOOD FACE, on the best strike of four. A reinforcement after a
    correction's instruction would be a third sentence, so it is not joined.
    Without one, the instruction and its optional restatement are joined as
    they always were. `observation` stays in the result on its own as well,
    because the validator has a rule about it that it cannot apply to a
    sentence it would first have to find.
    """

    coach = payload.get("coach")
    if not isinstance(coach, Mapping):
        return None
    observation = _text(coach.get("observation"))
    instruction = _text(coach.get("instruction"))
    reinforcement = _text(coach.get("reinforcement"))
    joined = dict(coach)
    joined["observation"] = observation
    if observation is not None:
        action = instruction
        if coach.get("kind") == AdviceKind.REINFORCEMENT.value:
            # A reinforcement says what worked. It does not then ask for a
            # change, and it may not borrow the instruction to do it: this read
            # `instruction or reinforcement`, and the first session shipped
            # "Your clubface pointed right at impact. Turn the trail forearm
            # over through impact." under the display GOOD FACE, on the best
            # strike of the four. Told it was good and told to fix it.
            action = reinforcement
        parts = [observation] if action is None else [observation, action]
    else:
        parts = [part for part in (instruction, reinforcement) if part is not None]
        if instruction is None:
            parts = []
    joined["speech"] = " ".join(parts) if parts else None
    return joined


def _text(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def metrics_summary(metrics: VxMetrics) -> dict[str, object]:
    """What may be logged about the readings: names and counts, never values."""

    return {
        "coach_metrics_provenance": metrics.provenance,
        "coach_metrics_read": len(metrics.available),
        "coach_metrics_available": ",".join(metrics.available) or "none",
        "coach_metrics_unavailable": ",".join(metrics.unavailable) or "none",
        "coach_metrics_rejected": ",".join(metrics.rejected) or "none",
    }
