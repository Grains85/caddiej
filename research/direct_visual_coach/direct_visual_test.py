"""Direct visual fault classifier. Research only. Nothing is sent unless --send is given.

Full-resolution synchronized DTL and FO frames are oriented through the swing,
then classified against the CaddieJ Visual Fault Catalog. The model does not
write the lesson. CaddieJ selects one proven catalog fault and looks up the
coaching package locally.

Reads a preserved fresh_v2 shot's diagnostic timeline (swing_behind and
swing_side). The default Catalog V1 request sends about eight chronological
moments, both views, as separate full-resolution images. A camera-budget
experiment can ask for DTL, FO, or both, and for another moment count. The
default arguments are unchanged. Moments stay spread across the replay.
Motion chooses the frame inside each time band, and one neighboring still
may be kept when that does not open a large gap. It does not build the T36S
contact sheet, does not read canonical labels or a V2 reply, and does not
reconcile the answer with production coaching.

The previous free-form root-cause results remain in results/<shot-id>/.
This iteration writes to results/catalog_v1/<shot-id>/.

Model and reasoning effort match frozen V2 (gpt-5-mini, minimal). The output
ceiling and timeout are this probe's own.

    .venv\\Scripts\\python.exe research\\direct_visual_coach\\direct_visual_test.py <shot-id>
    .venv\\Scripts\\python.exe research\\direct_visual_coach\\direct_visual_test.py <shot-id> --send

Without --send the request is built, printed and saved. It is never sent.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from fault_catalog import (
    CATALOG,
    FAULTS_BY_ID,
    TEMPORAL_STATES,
    catalog_exposure,
    catalog_problems,
    classifier_catalog_json,
    coaching_package,
)
from primary_selection import PrimarySelection, select_primary

from caddiej.adapters.openai.pricing import estimate_cost
from caddiej.coaching.benchmark import request_breakdown
from caddiej.coaching.provider import AdviceRequest, ImageInput
from caddiej.video.jpeg_size import jpeg_size

# Frozen V2 settings (research/temporal_evidence/fresh_v2/config.json). Hard-coded
# so a live .env cannot move this probe off the comparison, and so this file
# does not import the frozen experiment.
MODEL = "gpt-5-mini"
REASONING_EFFORT = "minimal"
MAX_OUTPUT_TOKENS = 6000
TIMEOUT_SECONDS = 120.0
MOMENT_COUNT = 8
# Equal slices of normalized replay time. Coverage is checked against these,
# not against golf positions.
COVERAGE_BANDS = 5
MOTION_SCALE = 8
# Playback controls sit along the bottom of these frames. They are not the swing.
CONTROLS_FRACTION = 0.06
BLANK_BYTES = 800
DETAIL = "high"
SCHEMA_NAME = "visual_fault_classifier"

CAMERAS = (
    ("swing_behind", "DTL", "Down-the-Line"),
    ("swing_side", "FO", "Face-On"),
)
VIEWS = frozenset(view for _camera, view, _name in CAMERAS)

SHOTS = Path(__file__).resolve().parents[1] / "temporal_evidence" / "fresh_v2" / "shots"
RESULTS = Path(__file__).resolve().parent / "results" / "catalog_v1"

# Forward-only rank. "uncertain" is omitted and never forces a neighbor.
_STATE_RANK = {
    state: index for index, state in enumerate(TEMPORAL_STATES) if state != "uncertain"
}
_CANDIDATE_FIELDS = frozenset(
    {
        "fault_id",
        "confidence",
        "evidence",
        "why_required_evidence_is_met",
        "alternatives_considered",
    }
)
_EVIDENCE_FIELDS = frozenset({"capture_tick", "view", "observation"})
_ALTERNATIVE_FIELDS = frozenset({"fault_id", "reason_rejected"})

SYSTEM = """\
You are a visual classifier for one golf swing. You are not the coach.

Study the chronological Down-the-Line and Face-On images.

First, orient every supplied moment. Orientation answers only which general direction is visible. It is not a catalogue of named positions, and it is not club-path classification.

Then classify visible mechanical faults from the CaddieJ Visual Fault Catalog supplied with the frames. Return zero faults when the images do not prove one. status no_proven_fault is a complete answer. Do not force the swing into a catalog fault.

A fault is proven only when every one of its required_evidence items is visible, in a preferred view, on frames whose temporal state is allowed for that fault. Items listed as insufficient_evidence do not prove the fault, even if they are visible.

Also judge handedness from the address and earliest frames. handedness.side is right when the golfer plays right-handed, left when the golfer plays left-handed, and uncertain when those frames do not show which side is closer to the target.

Trail means the side farther from the target at address. Lead means the side closer to the target. Do not treat camera-left as trail. A fault with requires_handedness true may be selected only when side is right or left. When side is uncertain, omit every fault that depends on lead or trail.

Do not infer a cause from a later pattern. A downstream fault does not prove the movement that might have caused it. Select a fault only when that fault's own required evidence is visible.

Do not write a lesson, a drill, a feel, or a floor cue. Describe only what is visible.

No launch-monitor numbers are attached. A measurement is not visual evidence. Do not invent one.

Confidence is how sure you are that the required evidence is visible. It is not how common the fault is.
"""

INSTRUCTIONS = """\
Fill the JSON object and nothing else.

Capture ticks increase through the swing. A higher capture_tick is later. A lower capture_tick is earlier.
DTL and FO with the same capture_tick are the same moment. The two cameras were requested together.
Use both views when that makes the direction clearer.

TEMPORAL ORIENTATION

Do this before classification.

temporal_orientation has exactly one object for every capture_tick you were shown, in increasing tick order, and no others.

state is exactly one of:
- setup: the golfer is in place and the swing has not started
- moving_away: the hands or club are moving away from the ball
- direction_change_region: the motion is changing from away to toward the ball, and that change is visible from these frames
- moving_toward_ball: the hands or club are moving toward the ball
- after_strike: the motion has continued past the ball
- uncertain: these frames do not show the direction

Read the states in that order. A later tick may stay in the same state or move to a later state. It must not go back to an earlier state. uncertain may sit on any tick and does not force the ticks around it.
Include direction_change_region only when the change of direction is visible. Leave it out when the frames do not show the change.
visual_basis is a short description of what is visible that establishes the direction. confidence is from 0 to 1.

HANDEDNESS

Do this from the address and earliest frames, before classification.

handedness.side is exactly one of right, left, or uncertain.
right means a right-handed golfer: the lead side is the golfer's left, closer to the target.
left means a left-handed golfer: the lead side is the golfer's right, closer to the target.
uncertain means those frames do not show which side is closer to the target.
visual_basis is what is visible that establishes the side. confidence is from 0 to 1.

CLASSIFICATION

Do this after temporal_orientation. Use only fault ids from the catalog in the evidence text.

status is "classified" when one or more catalog faults are visually proven, and "no_proven_fault" when none are.
fault_candidates is empty when status is no_proven_fault. Do not include a fault in order to be helpful.

When status is classified, every candidate has:
- fault_id copied from the catalog
- confidence from 0 to 1, based only on the visual evidence
- evidence items, each with a capture_tick you were shown, view DTL or FO, and an observation of what is visible in that image
- at least min_fault_observations evidence items whose temporal state is in allowed_temporal_states
- when requires_baseline is true, at least one evidence item whose state is in baseline_temporal_states and whose tick is earlier than a fault observation
- when must_include_states is not empty, at least one fault observation in each of those states
- at least one allowed-state evidence item in a preferred view
- when requires_handedness is true, handedness.side must be right or left. If side is uncertain, omit the fault
- why_required_evidence_is_met: one non-empty string for each required_evidence item, in the same order. If you cannot fill every item from the images, omit the fault
- alternatives_considered: catalog faults you nearly selected, each with a reason_rejected. It may be empty

An insufficient_evidence observation does not prove the fault.
A frame whose state is not allowed for the fault cannot prove it. uncertain never proves a fault.
Do not add a lesson, a drill, a feel, a floor cue, or a free-form fault name.
"""


def _moment_views(moments: Sequence[Moment]) -> tuple[str, ...]:
    found: list[str] = []
    for moment in moments:
        for frame in moment.frames:
            if frame.view not in found:
                found.append(frame.view)
    return tuple(found)


def system_text(views: Sequence[str]) -> str:
    """Catalog V1 system text. Single-camera runs name only the attached view."""

    if tuple(views) == ("DTL", "FO"):
        return SYSTEM
    if tuple(views) == ("DTL",):
        attached, missing = "Down-the-Line", "Face-On"
    elif tuple(views) == ("FO",):
        attached, missing = "Face-On", "Down-the-Line"
    else:
        raise ValueError(f"unsupported views: {views}")
    return SYSTEM.replace(
        "Study the chronological Down-the-Line and Face-On images.\n",
        (
            f"Study the chronological {attached} images. "
            f"No {missing} image is attached. Do not assume a {missing} view.\n"
        ),
    )


def instructions_text(views: Sequence[str]) -> str:
    """Catalog V1 instructions. Single-camera runs do not claim the other view."""

    if tuple(views) == ("DTL", "FO"):
        return INSTRUCTIONS
    if tuple(views) == ("DTL",):
        attached, missing = "Down-the-Line", "Face-On"
    elif tuple(views) == ("FO",):
        attached, missing = "Face-On", "Down-the-Line"
    else:
        raise ValueError(f"unsupported views: {views}")
    text = INSTRUCTIONS.replace(
        "DTL and FO with the same capture_tick are the same moment. "
        "The two cameras were requested together.\n"
        "Use both views when that makes the direction clearer.\n",
        (
            f"Only {attached} frames are attached. No {missing} frame is attached. "
            f"Do not treat a missing {missing} view as available.\n"
        ),
    )
    return (
        text
        + "\nOnly an attached view can prove a fault. "
        + f"Do not assume a {missing} image. "
        + "If every preferred view of a fault was not attached, omit that fault.\n"
    )


_METRIC = re.compile(
    r"\d+(?:\.\d+)?\s*(?:degrees?|deg\b|°|mph|rpm|yards?|yds|m/s|inches|inch|"
    r"cm|mm|metres?|meters?|%)"
    r"|(?:degrees?|°)\s*(?:of\s+)?\d"
    r"|club\s*path[^.\n]{0,48}\d"
    r"|face\s*(?:angle|to\s*path)[^.\n]{0,48}\d",
    re.IGNORECASE,
)
_ALL_VALUES = re.compile(r"\bALL_VALUES\b", re.IGNORECASE)
_RECONCILE = re.compile(
    r"\b(?:lesson cue|production (?:lesson|cue|coaching)|"
    r"overrid(?:e|ing) the (?:lesson|cue))\b",
    re.IGNORECASE,
)

SELECTION_RULE = (
    "About eight synchronized diagnostic moments, or every moment when fewer remain. "
    "A tick is kept only when both swing cameras have a still at or after replay start. "
    "Blanks under 800 bytes and a repeated JPEG hash are dropped. "
    "Motion is the mean absolute greyscale change of the original frame after a fixed "
    "box reduction, per second, averaged across the two views. The bottom playback-control "
    "strip is left out of that score. The first and last stills are always kept. "
    "Interior stills are placed across equal bands of replay time, and the strongest "
    "change inside a band wins that band. A gap wider than one and a half even spacings "
    "is filled before any extra still is added. One neighboring still may then sit beside "
    "the strongest remaining change. No phase labels and no model output."
)


@dataclass(frozen=True, slots=True)
class Frame:
    view: str
    camera_id: str
    capture_tick: int
    file: str
    width: int
    height: int
    metadata_sha256: str
    nbytes: int
    offset_seconds: float
    replay_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "view": self.view,
            "camera_id": self.camera_id,
            "capture_tick": self.capture_tick,
            "file": self.file,
            "width": self.width,
            "height": self.height,
            "metadata_sha256": self.metadata_sha256,
            "bytes": self.nbytes,
            "offset_seconds": self.offset_seconds,
            "replay_seconds": self.replay_seconds,
        }


@dataclass(frozen=True, slots=True)
class Moment:
    capture_tick: int
    replay_seconds: float
    frames: tuple[Frame, ...]
    motion_rate: float = 0.0

    def as_dict(self, ordinal: int) -> dict[str, Any]:
        return {
            "ordinal": ordinal,
            "capture_tick": self.capture_tick,
            "replay_seconds": round(self.replay_seconds, 4),
            "motion_rate": round(self.motion_rate, 4),
            "frames": [frame.as_dict() for frame in self.frames],
        }


@dataclass(frozen=True, slots=True)
class MotionScore:
    capture_tick: int
    replay_seconds: float
    motion_rate: float
    selected: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "capture_tick": self.capture_tick,
            "replay_seconds": round(self.replay_seconds, 4),
            "motion_rate": round(self.motion_rate, 4),
            "selected": self.selected,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    moments: list[Moment]
    motion: tuple[MotionScore, ...]


@dataclass(frozen=True, slots=True)
class LoadedFrame:
    frame: Frame
    data: bytes
    width: int
    height: int
    sha256: str


def select_moments(
    meta: Mapping[str, Any],
    *,
    count: int = MOMENT_COUNT,
    shot: Path | None = None,
    motion: Mapping[int, float] | None = None,
    views: Sequence[str] | None = None,
) -> list[Moment]:
    """About `count` synchronized moments spread across the replay.

    `motion` is a capture tick -> greyscale change per second, for tests.
    Otherwise the score is measured from the original diagnostic JPEGs in `shot`.
    A phase tag, a sealed label, or a model reply cannot change the pick.
    `views` limits motion and the frames kept on each moment. The default keeps
    both cameras, which is the Catalog V1 request.
    """

    return plan_selection(
        meta, count=count, shot=shot, motion=motion, views=views
    ).moments


def plan_selection(
    meta: Mapping[str, Any],
    *,
    count: int = MOMENT_COUNT,
    shot: Path | None = None,
    motion: Mapping[int, float] | None = None,
    views: Sequence[str] | None = None,
) -> Plan:
    candidates = _candidates(meta)
    if motion is not None:
        rates = {
            moment.capture_tick: max(0.0, float(motion.get(moment.capture_tick, 0.0)))
            for moment in candidates
        }
    elif shot is not None:
        rates = measure_motion(shot, candidates, views=views)
    elif len(candidates) <= count:
        rates = {moment.capture_tick: 0.0 for moment in candidates}
    else:
        raise ValueError(
            "motion scores or the shot directory are required to choose moments"
        )
    picked = _pick(candidates, rates, count)
    if views is not None:
        picked = [_limit_views(moment, views) for moment in picked]
    chosen = {moment.capture_tick for moment in picked}
    scores = tuple(
        MotionScore(
            moment.capture_tick,
            moment.replay_seconds,
            rates[moment.capture_tick],
            moment.capture_tick in chosen,
        )
        for moment in candidates
    )
    return Plan(picked, scores)


def _candidates(meta: Mapping[str, Any]) -> list[Moment]:
    """Synchronized replay stills. Image bytes are not read here."""

    timeline = meta.get("diagnostic_timeline")
    if not isinstance(timeline, dict):
        raise ValueError("shot has no diagnostic timeline")
    cameras = {
        camera.get("camera_id"): camera
        for camera in timeline.get("cameras", [])
        if isinstance(camera, dict)
    }
    pools: dict[str, dict[int, Frame]] = {}
    for camera_id, view, _name in CAMERAS:
        camera = cameras.get(camera_id)
        frames = camera.get("frames") if isinstance(camera, dict) else None
        if not isinstance(frames, list) or not frames:
            raise ValueError(f"diagnostic timeline has no {camera_id} frames")
        pools[camera_id] = _usable(camera_id, view, frames)

    behind = pools["swing_behind"]
    side = pools["swing_side"]
    seen: dict[str, set[str]] = {
        camera_id: set() for camera_id, _view, _name in CAMERAS
    }
    candidates: list[Moment] = []
    for tick in sorted(set(behind) & set(side)):
        pair = (behind[tick], side[tick])
        if any(frame.replay_seconds < -1e-6 for frame in pair):
            continue
        if any(frame.nbytes < BLANK_BYTES for frame in pair):
            continue
        if any(frame.metadata_sha256 in seen[frame.camera_id] for frame in pair):
            continue
        for frame in pair:
            if frame.metadata_sha256:
                seen[frame.camera_id].add(frame.metadata_sha256)
        candidates.append(Moment(tick, behind[tick].replay_seconds, pair))
    if not candidates:
        raise ValueError("no synchronized diagnostic frames cover the swing")
    return candidates


def _usable(camera_id: str, view: str, frames: list[Any]) -> dict[int, Frame]:
    prefix = f"diagnostic/{camera_id}/"
    pool: dict[int, Frame] = {}
    for raw in frames:
        if not isinstance(raw, dict):
            continue
        file = str(raw.get("file") or "").replace("\\", "/")
        if not file.startswith(prefix) or ".." in Path(file).parts:
            continue
        tick = raw.get("capture_index")
        replay = raw.get("offset_from_replay_start_seconds")
        if (
            not isinstance(tick, int)
            or isinstance(replay, bool)
            or not isinstance(replay, int | float)
        ):
            continue
        nbytes = raw.get("bytes")
        size = nbytes if isinstance(nbytes, int) and not isinstance(nbytes, bool) else 0
        offset = raw.get("offset_seconds")
        pool[tick] = Frame(
            view=view,
            camera_id=camera_id,
            capture_tick=tick,
            file=file,
            width=int(raw.get("width") or 0),
            height=int(raw.get("height") or 0),
            metadata_sha256=str(raw.get("sha256") or ""),
            nbytes=size,
            offset_seconds=float(offset) if isinstance(offset, int | float) else 0.0,
            replay_seconds=float(replay),
        )
    return pool


def measure_motion(
    shot: Path,
    candidates: Sequence[Moment],
    *,
    views: Sequence[str] | None = None,
) -> dict[int, float]:
    """Greyscale change per second at each tick.

    With no view filter the two cameras are averaged, which is Catalog V1.
    DTL or FO alone uses only that camera. The first still has no predecessor,
    so its rate is zero. The score uses the original JPEG, reduced by a fixed
    box filter. It does not name a position.
    """

    if not candidates:
        return {}

    def active(moment: Moment) -> tuple[Frame, ...]:
        if views is None:
            return moment.frames
        return tuple(frame for frame in moment.frames if frame.view in views)

    if len(candidates) == 1:
        return {candidates[0].capture_tick: 0.0}
    opening = active(candidates[0])
    if not opening:
        raise ValueError("motion view is missing from the synchronized frames")
    previous = {frame.camera_id: _luma(shot, frame) for frame in opening}
    previous_t = candidates[0].replay_seconds
    rates = {candidates[0].capture_tick: 0.0}
    for moment in candidates[1:]:
        dt = max(1e-3, moment.replay_seconds - previous_t)
        view_rates: list[float] = []
        current: dict[str, bytes] = {}
        frames = active(moment)
        if not frames:
            raise ValueError("motion view is missing from the synchronized frames")
        for frame in frames:
            grey = _luma(shot, frame)
            view_rates.append(_mean_abs(previous[frame.camera_id], grey) / dt)
            current[frame.camera_id] = grey
        rates[moment.capture_tick] = sum(view_rates) / len(view_rates)
        previous = current
        previous_t = moment.replay_seconds
    return rates


def _limit_views(moment: Moment, views: Sequence[str]) -> Moment:
    frames = tuple(frame for frame in moment.frames if frame.view in views)
    if not frames:
        raise ValueError(
            f"capture_tick {moment.capture_tick} has no frame in {', '.join(views)}"
        )
    return replace(moment, frames=frames)


def _luma(shot: Path, frame: Frame) -> bytes:
    from PIL import Image

    path = _diagnostic_file(shot, frame.file)
    with Image.open(path) as image:
        small = image.convert("L").reduce(MOTION_SCALE)
        width, height = small.size
        usable = max(1, int(round(height * (1 - CONTROLS_FRACTION))))
        if usable < height:
            small = small.crop((0, 0, width, usable))
        return small.tobytes()


def _mean_abs(earlier: bytes, later: bytes) -> float:
    if len(earlier) != len(later) or not earlier:
        raise ValueError("consecutive diagnostic frames do not share a motion grid")
    return sum(
        abs(left - right) for left, right in zip(earlier, later, strict=True)
    ) / len(earlier)


def _pick(
    candidates: Sequence[Moment], rates: Mapping[int, float], count: int
) -> list[Moment]:
    if count <= 0:
        return []
    if len(candidates) <= count:
        indexes = range(len(candidates))
    else:
        indexes = _select_indexes(candidates, rates, count)
    return [
        replace(
            candidates[index],
            motion_rate=rates.get(candidates[index].capture_tick, 0.0),
        )
        for index in indexes
    ]


def max_gap_fraction(count: int) -> float:
    """Largest accepted replay gap, as a fraction of the full replay duration.

    `count` moments placed evenly leave gaps of 1/(count-1). Anything wider than
    1.5 times that spacing is filled before a local pair is added.
    """

    if count < 2:
        return 1.0
    return 1.5 / (count - 1)


def _select_indexes(
    candidates: Sequence[Moment], rates: Mapping[int, float], count: int
) -> list[int]:
    """Spread moments across replay time, then spend a spare slot on one neighbor.

    Motion ranks the frames inside a time window. It does not decide to skip a
    whole stretch of the replay. The first and last candidates are fixed.
    """

    times = [moment.replay_seconds for moment in candidates]
    series = [
        max(0.0, float(rates.get(moment.capture_tick, 0.0))) for moment in candidates
    ]
    duration = max(times[-1] - times[0], 1e-9)
    norms = [(time - times[0]) / duration for time in times]
    limit = max_gap_fraction(count)
    skeleton = count - 1 if count >= 7 else count
    chosen = _even_skeleton(norms, series, skeleton, limit)
    _ensure_bands(chosen, norms, series, count)
    _repair_gaps(chosen, norms, series, limit, count)
    if len(chosen) < count:
        neighbor = _neighbor_index(chosen, norms, series)
        if neighbor is not None:
            chosen.add(neighbor)
    while len(chosen) < count:
        extra = _best_split(*_largest_gap(chosen, norms), chosen, norms, series)
        if extra is None:
            break
        chosen.add(extra)
    return sorted(chosen)


def _even_skeleton(
    norms: Sequence[float], rates: Sequence[float], skeleton: int, limit: float
) -> set[int]:
    """`skeleton` moments on even replay targets, nudged toward motion within slack."""

    total = len(norms)
    chosen = {0, total - 1}
    if skeleton <= 2 or total < 3:
        return chosen
    spacing = 1.0 / (skeleton - 1)
    slack = max(0.0, (limit - spacing) / 2.0)
    for slot in range(1, skeleton - 1):
        target = slot * spacing
        window = [
            index
            for index in range(1, total - 1)
            if index not in chosen and abs(norms[index] - target) <= slack + 1e-12
        ]
        pool = window or [index for index in range(1, total - 1) if index not in chosen]
        if not pool:
            break
        if window:
            pool.sort(
                key=lambda index: (-rates[index], abs(norms[index] - target), index)
            )
        else:
            pool.sort(
                key=lambda index: (abs(norms[index] - target), -rates[index], index)
            )
        chosen.add(pool[0])
    return chosen


def _ensure_bands(
    chosen: set[int], norms: Sequence[float], rates: Sequence[float], count: int
) -> None:
    """Each equal replay band keeps a moment when a frame exists in that band."""

    for _ in range(COVERAGE_BANDS):
        empty = next(
            (
                band
                for band in range(COVERAGE_BANDS)
                if not _band_members(chosen, norms, band)
            ),
            None,
        )
        if empty is None:
            return
        center = (empty + 0.5) / COVERAGE_BANDS
        pool = [
            index
            for index in range(len(norms))
            if index not in chosen and _band(norms[index]) == empty
        ]
        if not pool:
            return
        pool.sort(key=lambda index: (-rates[index], abs(norms[index] - center), index))
        pick = pool[0]
        if len(chosen) < count:
            chosen.add(pick)
            continue
        victim = _most_redundant(chosen, norms, rates)
        if victim is None:
            return
        chosen.remove(victim)
        chosen.add(pick)


def _repair_gaps(
    chosen: set[int],
    norms: Sequence[float],
    rates: Sequence[float],
    limit: float,
    count: int,
) -> None:
    """Fill a replay gap that is wide compared with even spacing. Endpoints stay."""

    for _ in range(len(norms)):
        width, left, right = _worst_gap(chosen, norms)
        if width <= limit + 1e-12:
            return
        pick = _best_split(left, right, chosen, norms, rates)
        if pick is None:
            return
        if len(chosen) < count:
            chosen.add(pick)
            continue
        victim = _most_redundant(chosen, norms, rates)
        if victim is None:
            return
        trial = set(chosen)
        trial.remove(victim)
        trial.add(pick)
        if _worst_gap(trial, norms)[0] >= width - 1e-12:
            return
        if _missing_band(trial, norms):
            return
        chosen.clear()
        chosen.update(trial)


def _neighbor_index(
    chosen: set[int], norms: Sequence[float], rates: Sequence[float]
) -> int | None:
    """The earlier frame of one short pair beside the strongest nearby change."""

    gaps = [norms[index] - norms[index - 1] for index in range(1, len(norms))]
    typical = sorted(gaps)[len(gaps) // 2] if gaps else 1.0
    best: int | None = None
    best_key: tuple[float, float, int] | None = None
    for index in sorted(chosen):
        previous = index - 1
        if previous < 0 or previous in chosen:
            continue
        if norms[index] - norms[previous] > max(typical * 2.0, 1e-9):
            continue
        key = (rates[index] + rates[previous], rates[index], -index)
        if best_key is None or key > best_key:
            best_key = key
            best = previous
    return best


def _best_split(
    left: int,
    right: int,
    chosen: set[int],
    norms: Sequence[float],
    rates: Sequence[float],
) -> int | None:
    """Highest-motion frame near the middle of an open replay gap."""

    pool = [index for index in range(left + 1, right) if index not in chosen]
    if not pool:
        return None
    mid = (norms[left] + norms[right]) / 2.0
    half = (norms[right] - norms[left]) / 4.0
    central = [index for index in pool if abs(norms[index] - mid) <= half + 1e-12]
    use = central or pool
    use.sort(key=lambda index: (-rates[index], abs(norms[index] - mid), index))
    return use[0]


def _largest_gap(chosen: set[int], norms: Sequence[float]) -> tuple[int, int]:
    _width, left, right = _worst_gap(chosen, norms)
    return left, right


def _worst_gap(chosen: set[int], norms: Sequence[float]) -> tuple[float, int, int]:
    ordered = sorted(chosen)
    worst = (-1.0, ordered[0], ordered[-1])
    for left, right in zip(ordered, ordered[1:]):
        width = norms[right] - norms[left]
        if width > worst[0] or (width == worst[0] and left < worst[1]):
            worst = (width, left, right)
    return worst


def _most_redundant(
    chosen: set[int], norms: Sequence[float], rates: Sequence[float]
) -> int | None:
    """Interior moment whose band still has another moment and whose neighbor is closest."""

    ordered = sorted(chosen)
    best: int | None = None
    best_key: tuple[float, float, int] | None = None
    for pos, index in enumerate(ordered):
        if pos == 0 or pos == len(ordered) - 1:
            continue
        band = _band(norms[index])
        if len(_band_members(chosen, norms, band)) < 2:
            continue
        gap = min(
            norms[index] - norms[ordered[pos - 1]],
            norms[ordered[pos + 1]] - norms[index],
        )
        key = (gap, rates[index], index)
        if best_key is None or key < best_key:
            best_key = key
            best = index
    return best


def _missing_band(chosen: set[int], norms: Sequence[float]) -> bool:
    return any(not _band_members(chosen, norms, band) for band in range(COVERAGE_BANDS))


def _band_members(chosen: set[int], norms: Sequence[float], band: int) -> list[int]:
    return [index for index in chosen if _band(norms[index]) == band]


def _band(norm: float) -> int:
    if norm >= 1.0:
        return COVERAGE_BANDS - 1
    return min(COVERAGE_BANDS - 1, int(norm * COVERAGE_BANDS))


def schema() -> dict[str, Any]:
    fault_ids = [fault.id for fault in CATALOG]
    orientation_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "capture_tick": {"type": "integer"},
            "state": {"type": "string", "enum": list(TEMPORAL_STATES)},
            "confidence": {"type": "number"},
            "visual_basis": {"type": "string"},
        },
        "required": ["capture_tick", "state", "confidence", "visual_basis"],
    }
    evidence_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "view": {"type": "string", "enum": ["DTL", "FO"]},
            "capture_tick": {"type": "integer"},
            "observation": {"type": "string"},
        },
        "required": ["view", "capture_tick", "observation"],
    }
    alternative_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "fault_id": {"type": "string", "enum": fault_ids},
            "reason_rejected": {"type": "string"},
        },
        "required": ["fault_id", "reason_rejected"],
    }
    candidate = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "fault_id": {"type": "string", "enum": fault_ids},
            "confidence": {"type": "number"},
            "evidence": {"type": "array", "items": evidence_item},
            "why_required_evidence_is_met": {
                "type": "array",
                "items": {"type": "string"},
            },
            "alternatives_considered": {"type": "array", "items": alternative_item},
        },
        "required": [
            "fault_id",
            "confidence",
            "evidence",
            "why_required_evidence_is_met",
            "alternatives_considered",
        ],
    }
    handedness = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "side": {"type": "string", "enum": ["right", "left", "uncertain"]},
            "confidence": {"type": "number"},
            "visual_basis": {"type": "string"},
        },
        "required": ["side", "confidence", "visual_basis"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["classified", "no_proven_fault"]},
            "handedness": handedness,
            "temporal_orientation": {"type": "array", "items": orientation_item},
            "fault_candidates": {"type": "array", "items": candidate},
        },
        "required": [
            "status",
            "handedness",
            "temporal_orientation",
            "fault_candidates",
        ],
    }


def build_request(
    moments: Sequence[Moment], loaded: Sequence[LoadedFrame]
) -> AdviceRequest:
    """The classifier request. Images are the original diagnostic bytes.

    Coaching packages and cause links stay local. Only compact fault
    definitions are attached.
    """

    invalid = catalog_problems()
    if invalid:
        listed = "; ".join(invalid)
        raise RuntimeError(f"visual fault catalog is invalid: {listed}")
    views = _moment_views(moments)
    total = len(moments)
    image_count = sum(len(moment.frames) for moment in moments)
    lines = [
        "No launch-monitor image and no launch-monitor numbers are attached. "
        "A number cannot prove a catalog fault.",
        "",
        _evidence_intro(views, image_count),
        "temporal_orientation must contain each of these capture ticks once.",
        "",
    ]
    for ordinal, moment in enumerate(moments, start=1):
        lines.append(
            f"Moment {ordinal} of {total}: capture_tick {moment.capture_tick}, "
            f"{moment.replay_seconds:.3f}s from replay start."
        )
    lines.extend(
        [
            "",
            "VISUAL FAULT CATALOG",
            "Classify only these faults. Each object is one fault. "
            "Coaching text and cause links are not included. "
            "insufficient_evidence does not prove a fault.",
            classifier_catalog_json(),
        ]
    )
    images = []
    for ordinal, item in _indexed(moments, loaded):
        images.append(
            ImageInput(
                data=item.data,
                label=(
                    f"{item.frame.view} capture_tick {item.frame.capture_tick}, "
                    f"{_view_name(item.frame.view)}, moment {ordinal} of {total}"
                ),
                detail=DETAIL,
            )
        )
    return AdviceRequest(
        system=system_text(views),
        instructions=instructions_text(views),
        evidence="\n".join(lines),
        schema=schema(),
        images=tuple(images),
        max_output_tokens=MAX_OUTPUT_TOKENS,
        timeout_seconds=TIMEOUT_SECONDS,
        schema_name=SCHEMA_NAME,
        reasoning_effort=REASONING_EFFORT,
    )


def _evidence_intro(views: tuple[str, ...], image_count: int) -> str:
    shared = (
        "They are listed from earliest to latest. "
        "capture_tick increases through the swing: a larger tick is later, "
        "a smaller tick is earlier. "
    )
    if views == ("DTL", "FO"):
        return (
            "The images are separate full-resolution frames. "
            + shared
            + "DTL is Swing Behind (down the line). FO is Swing Side (face on). "
            "DTL and FO with the same capture_tick are one moment."
        )
    if views == ("DTL",):
        return (
            f"The images are {image_count} separate full-resolution Down-the-Line frames. "
            "No Face-On frame is attached. "
            + shared
            + "DTL is Swing Behind (down the line). "
            "Do not assume a Face-On view exists."
        )
    if views == ("FO",):
        return (
            f"The images are {image_count} separate full-resolution Face-On frames. "
            "No Down-the-Line frame is attached. "
            + shared
            + "FO is Swing Side (face on). "
            "Do not assume a Down-the-Line view exists."
        )
    raise ValueError(f"unsupported views: {views}")


def _indexed(
    moments: Sequence[Moment], loaded: Sequence[LoadedFrame]
) -> list[tuple[int, LoadedFrame]]:
    expected = [
        (frame.view, frame.capture_tick)
        for moment in moments
        for frame in moment.frames
    ]
    got = [(item.frame.view, item.frame.capture_tick) for item in loaded]
    if got != expected:
        raise ValueError("loaded frames are not the selected frames in order")
    out: list[tuple[int, LoadedFrame]] = []
    cursor = 0
    for ordinal, moment in enumerate(moments, start=1):
        for _frame in moment.frames:
            out.append((ordinal, loaded[cursor]))
            cursor += 1
    return out


def _view_name(view: str) -> str:
    return next(name for _cid, code, name in CAMERAS if code == view)


def sent_images(moments: Sequence[Moment]) -> set[tuple[str, int]]:
    return {
        (frame.view, frame.capture_tick)
        for moment in moments
        for frame in moment.frames
    }


def validate(payload: Mapping[str, Any], sent: set[tuple[str, int]]) -> list[str]:
    """Problems with a classifier reply. Empty means the reply may be kept as-is.

    The payload is not rewritten. Coaching text in the reply is rejected.
    The lesson is looked up from the catalog only after this passes.
    """

    problems: list[str] = []
    extra = set(payload) - {
        "status",
        "handedness",
        "temporal_orientation",
        "fault_candidates",
    }
    if extra:
        problems.append(f"unexpected fields: {', '.join(sorted(extra))}")
    status = payload.get("status")
    if status not in {"classified", "no_proven_fault"}:
        problems.append("status must be classified or no_proven_fault")
    sent_ticks = {tick for _view, tick in sent}
    side = _handedness(payload, problems)
    states = _temporal(payload, sent_ticks, problems)
    candidates = payload.get("fault_candidates")
    if not isinstance(candidates, list):
        problems.append("fault_candidates must be a list")
        candidates = []
    if status == "no_proven_fault" and candidates:
        problems.append("no_proven_fault must not name a fault")
    elif status == "classified" and not candidates:
        problems.append("classified reply needs at least one fault candidate")
    seen: set[str] = set()
    for index, candidate in enumerate(candidates):
        _candidate(index, candidate, sent, states, side, seen, problems)
    prose = _prose(payload)
    if _ALL_VALUES.search(prose):
        problems.append("ALL_VALUES is not visual evidence")
    if _METRIC.search(prose):
        problems.append("unsupported numerical claim")
    if _RECONCILE.search(prose):
        problems.append("production reconciliation")
    return problems


def interpret(
    payload: Mapping[str, Any], sent: set[tuple[str, int]]
) -> tuple[list[str], PrimarySelection | None, dict[str, str] | None]:
    """Validate, then select one catalog lesson. The payload is not rewritten."""

    problems = validate(payload, sent)
    if problems:
        return problems, None, None
    selection = select_primary(_candidate_pairs(payload))
    coaching = (
        None if selection.fault_id is None else coaching_package(selection.fault_id)
    )
    return problems, selection, coaching


def _handedness(payload: Mapping[str, Any], problems: list[str]) -> str:
    """right, left, or uncertain. A missing or invalid side is uncertain."""

    raw = payload.get("handedness")
    if not isinstance(raw, dict):
        problems.append("handedness must be right, left, or uncertain")
        return "uncertain"
    extra = set(raw) - {"side", "confidence", "visual_basis"}
    if extra:
        listed = ", ".join(sorted(str(field) for field in extra))
        problems.append(f"handedness has unexpected fields: {listed}")
    side = raw.get("side")
    if side not in {"right", "left", "uncertain"}:
        problems.append("handedness.side must be right, left, or uncertain")
        side = "uncertain"
    _confidence(raw.get("confidence"), "handedness.confidence", problems)
    basis = raw.get("visual_basis")
    if not isinstance(basis, str) or not basis.strip():
        problems.append("handedness visual_basis is empty")
    return side


def _confidence(value: Any, label: str, problems: list[str]) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        problems.append(f"{label} must be a number from 0 to 1")
        return
    if not 0 <= float(value) <= 1:
        problems.append(f"{label} must be a number from 0 to 1")


def _temporal(
    payload: Mapping[str, Any], sent_ticks: set[int], problems: list[str]
) -> dict[int, str]:
    """Map each sent tick to its state. Problems are appended; the map may be partial."""

    raw = payload.get("temporal_orientation")
    if not isinstance(raw, list):
        problems.append("temporal_orientation must list every sent tick once")
        return {}
    seen: set[int] = set()
    states: dict[int, str] = {}
    for index, item in enumerate(raw):
        where = f"temporal_orientation[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where} must be an object")
            continue
        tick = item.get("capture_tick")
        state = item.get("state")
        if isinstance(tick, bool) or not isinstance(tick, int):
            problems.append(f"{where} capture_tick must be a sent tick")
            continue
        if tick in seen:
            problems.append(f"temporal_orientation capture_tick {tick} is duplicated")
            continue
        seen.add(tick)
        if tick not in sent_ticks:
            problems.append(f"temporal_orientation capture_tick {tick} was not sent")
            continue
        if state not in _STATE_RANK and state != "uncertain":
            problems.append(f"{where} state is not allowed")
        else:
            states[tick] = state
        _confidence(item.get("confidence"), f"{where}.confidence", problems)
        basis = item.get("visual_basis")
        if not isinstance(basis, str) or not basis.strip():
            problems.append(f"{where} visual_basis is empty")
    missing = sorted(sent_ticks - seen)
    if missing:
        listed = ", ".join(str(tick) for tick in missing)
        problems.append(f"temporal_orientation is missing sent ticks: {listed}")
    previous: int | None = None
    for tick in sorted(states):
        state = states[tick]
        if state == "uncertain":
            continue
        rank = _STATE_RANK[state]
        if previous is not None and rank < previous:
            problems.append("temporal_orientation regresses chronologically")
            break
        previous = rank
    return states


def _candidate(
    index: int,
    candidate: Any,
    sent: set[tuple[str, int]],
    states: Mapping[int, str],
    handedness: str,
    seen: set[str],
    problems: list[str],
) -> None:
    where = f"fault_candidates[{index}]"
    if not isinstance(candidate, dict):
        problems.append(f"{where} must be an object")
        return
    extra = set(candidate) - _CANDIDATE_FIELDS
    if extra:
        listed = ", ".join(sorted(str(field) for field in extra))
        problems.append(f"{where} has unexpected fields: {listed}")
    fault_id = candidate.get("fault_id")
    if not isinstance(fault_id, str) or fault_id not in FAULTS_BY_ID:
        problems.append(f"{where} fault_id is not in the catalog")
        return
    if fault_id in seen:
        problems.append(f"fault {fault_id} is duplicated")
    seen.add(fault_id)
    _confidence(candidate.get("confidence"), f"{where}.confidence", problems)
    evidence = candidate.get("evidence")
    if not isinstance(evidence, list):
        problems.append(f"{where} evidence must be a list")
        evidence = []
    observations: list[tuple[str, int]] = []
    for evidence_index, item in enumerate(evidence):
        parsed = _observation(where, evidence_index, item, sent, problems)
        if parsed is not None:
            observations.append(parsed)
    fault = FAULTS_BY_ID[fault_id]
    supplied = {view for view, _tick in sent}
    if not set(fault.preferred_views).intersection(supplied):
        problems.append(f"{where} depends on a view that was not supplied")
    if fault.requires_handedness and handedness == "uncertain":
        problems.append(f"{where} depends on lead/trail and handedness is uncertain")
    _fault_evidence(where, fault, observations, states, problems)
    reasons = candidate.get("why_required_evidence_is_met")
    needed = len(fault.required_evidence)
    if not isinstance(reasons, list) or len(reasons) != needed:
        problems.append(
            f"{where} why_required_evidence_is_met must cover every required item"
        )
    elif any(not isinstance(item, str) or not item.strip() for item in reasons):
        problems.append(f"{where} why_required_evidence_is_met has an empty item")
    _alternatives(where, candidate.get("alternatives_considered"), fault_id, problems)


def _observation(
    where: str,
    index: int,
    item: Any,
    sent: set[tuple[str, int]],
    problems: list[str],
) -> tuple[str, int] | None:
    label = f"{where}.evidence[{index}]"
    if not isinstance(item, dict):
        problems.append(f"{label} must be an object")
        return None
    extra = set(item) - _EVIDENCE_FIELDS
    if extra:
        listed = ", ".join(sorted(str(field) for field in extra))
        problems.append(f"{label} has unexpected fields: {listed}")
    view = item.get("view")
    tick = item.get("capture_tick")
    observation = item.get("observation")
    usable = True
    if not isinstance(view, str) or view not in VIEWS:
        problems.append(f"{label} view must be DTL or FO")
        usable = False
    if isinstance(tick, bool) or not isinstance(tick, int):
        problems.append(f"{label} capture_tick must be an integer that was sent")
        usable = False
    elif isinstance(view, str) and view in VIEWS and (view, tick) not in sent:
        problems.append(f"{label} capture_tick {tick} ({view}) was not sent")
        usable = False
    if not isinstance(observation, str) or not observation.strip():
        problems.append(f"{label} observation is empty")
        usable = False
    if not usable or not isinstance(view, str) or not isinstance(tick, int):
        return None
    return view, tick


def _fault_evidence(
    where: str,
    fault: Any,
    observations: Sequence[tuple[str, int]],
    states: Mapping[int, str],
    problems: list[str],
) -> None:
    fault_ticks: list[int] = []
    fault_views: list[str] = []
    baseline_ticks: list[int] = []
    for view, tick in observations:
        state = states.get(tick)
        if state is None:
            problems.append(
                f"{where} evidence capture_tick {tick} has no temporal state"
            )
            continue
        if state in fault.allowed_temporal_states:
            fault_ticks.append(tick)
            fault_views.append(view)
        elif state in fault.baseline_temporal_states:
            baseline_ticks.append(tick)
        else:
            problems.append(
                f"{where} evidence capture_tick {tick} temporal state "
                f"{state} is not allowed for {fault.id}"
            )
    if len(fault_ticks) < fault.min_fault_observations:
        problems.append(
            f"{where} needs {fault.min_fault_observations} observations "
            "in an allowed temporal state"
        )
    seen_states = {states.get(tick) for tick in fault_ticks}
    for required in fault.must_include_states:
        if required not in seen_states:
            problems.append(f"{where} must include temporal state {required}")
    if not any(view in fault.preferred_views for view in fault_views):
        problems.append(f"{where} fault evidence is missing a preferred view")
    if not fault.requires_baseline:
        return
    earlier = any(
        base < observed for base in baseline_ticks for observed in fault_ticks
    )
    if not earlier:
        problems.append(f"{where} needs an earlier baseline frame")


def _alternatives(where: str, raw: Any, fault_id: str, problems: list[str]) -> None:
    label = f"{where}.alternatives_considered"
    if not isinstance(raw, list):
        problems.append(f"{label} must be a list")
        return
    for index, item in enumerate(raw):
        item_where = f"{label}[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{item_where} must be an object")
            continue
        extra = set(item) - _ALTERNATIVE_FIELDS
        if extra:
            listed = ", ".join(sorted(str(field) for field in extra))
            problems.append(f"{item_where} has unexpected fields: {listed}")
        other = item.get("fault_id")
        reason = item.get("reason_rejected")
        if not isinstance(other, str) or other not in FAULTS_BY_ID:
            problems.append(f"{item_where} fault_id is not in the catalog")
        elif other == fault_id:
            problems.append(f"{item_where} rejects the same fault")
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"{item_where} reason_rejected is empty")


def _candidate_pairs(payload: Mapping[str, Any]) -> list[tuple[str, float]]:
    raw = payload.get("fault_candidates")
    if not isinstance(raw, list):
        return []
    pairs: list[tuple[str, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fault_id = item.get("fault_id")
        confidence = item.get("confidence")
        if (
            isinstance(fault_id, str)
            and not isinstance(confidence, bool)
            and isinstance(confidence, int | float)
        ):
            pairs.append((fault_id, float(confidence)))
    return pairs


def _prose(value: Any) -> str:
    parts: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            for inner in item.values():
                walk(inner)
        elif isinstance(item, list):
            for inner in item:
                walk(inner)

    walk(value)
    return "\n".join(parts)


def load_frames(shot: Path, moments: Sequence[Moment]) -> list[LoadedFrame]:
    loaded: list[LoadedFrame] = []
    for moment in moments:
        for frame in moment.frames:
            path = _diagnostic_file(shot, frame.file)
            data = path.read_bytes()
            size = jpeg_size(data)
            if size is None:
                raise ValueError(f"{frame.file} is not a JPEG")
            loaded.append(
                LoadedFrame(
                    frame=frame,
                    data=data,
                    width=size[0],
                    height=size[1],
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )
    return loaded


def _diagnostic_file(shot: Path, relative: str) -> Path:
    rel = Path(relative.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"diagnostic path escapes the shot: {relative}")
    path = (shot / rel).resolve()
    root = (shot / "diagnostic").resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"diagnostic path escapes the shot: {relative}")
    if not path.is_file():
        raise ValueError(f"missing diagnostic frame: {relative}")
    return path


def execute(
    shot_id: str,
    *,
    send: bool = False,
    shots_root: Path = SHOTS,
    results_root: Path = RESULTS,
    complete: Callable[[AdviceRequest], Any] | None = None,
) -> dict[str, Any]:
    """Build the request and, only when send is true, ask the model.

    `complete` is a test seam. The CLI leaves it unset, so a real call happens
    only after --send.
    """

    shot = find_shot(shot_id, shots_root)
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    plan = plan_selection(meta, shot=shot)
    moments = plan.moments
    loaded = load_frames(shot, moments)
    request = build_request(moments, loaded)
    breakdown = request_breakdown(request, model=MODEL, default_detail=DETAIL)
    out = results_root / shot.name
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "selected_frames.json", _selection_document(shot.name, plan))
    _write(
        out / "request.json",
        _request_document(shot.name, moments, loaded, request, breakdown),
    )
    if not send:
        report = _report(
            shot.name,
            moments,
            loaded,
            breakdown_tokens=breakdown.total_tokens,
            sent=False,
        )
        _write(out / "report.json", report)
        return report

    reply = (
        complete(request) if complete is not None else asyncio.run(send_openai(request))
    )
    payload = dict(reply.payload)
    problems, selection, coaching = interpret(payload, sent_images(moments))
    usage = {
        "input_tokens": reply.input_tokens,
        "output_tokens": reply.output_tokens,
        "reasoning_tokens": reply.reasoning_tokens,
    }
    _write(
        out / "reply.json",
        {
            "model": reply.model,
            "reasoning_effort": REASONING_EFFORT,
            "latency_seconds": reply.latency_seconds,
            "usage": usage,
            "payload": payload,
        },
    )
    report = _report(
        shot.name,
        moments,
        loaded,
        breakdown_tokens=breakdown.total_tokens,
        sent=True,
        payload=payload,
        problems=problems,
        selection=selection,
        coaching=coaching,
        model=reply.model,
        latency_seconds=reply.latency_seconds,
        usage=usage,
    )
    _write(out / "report.json", report)
    return report


def find_shot(shot_id: str, root: Path) -> Path:
    if not root.is_dir():
        raise SystemExit(f"preserved shots directory does not exist: {root}")
    exact = root / shot_id
    if exact.is_dir() and (exact / "metadata.json").is_file():
        return exact
    matches = [
        path
        for path in sorted(root.iterdir())
        if path.is_dir() and shot_id in path.name and (path / "metadata.json").is_file()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"no preserved fresh_v2 shot matches {shot_id!r} under {root}")
    names = ", ".join(path.name for path in matches)
    raise SystemExit(f"shot id {shot_id!r} matches more than one shot: {names}")


def _selection_document(shot: str, plan: Plan) -> dict[str, Any]:
    return {
        "shot": shot,
        "rule": SELECTION_RULE,
        "moment_count": len(plan.moments),
        "max_gap_fraction": max_gap_fraction(MOMENT_COUNT),
        "motion_scale": MOTION_SCALE,
        "selected_capture_ticks": [moment.capture_tick for moment in plan.moments],
        "motion": [score.as_dict() for score in plan.motion],
        "moments": [
            moment.as_dict(i) for i, moment in enumerate(plan.moments, start=1)
        ],
    }


def _request_document(
    shot: str,
    moments: Sequence[Moment],
    loaded: Sequence[LoadedFrame],
    request: AdviceRequest,
    breakdown: Any,
) -> dict[str, Any]:
    """Safe diagnostic: prompts, schema and image identity. No image bytes."""

    return {
        "shot": shot,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "detail": DETAIL,
        "schema_name": SCHEMA_NAME,
        "architecture": "visual_fault_catalog_v1",
        "catalog_exposure": catalog_exposure(),
        "image_inputs": len(request.images),
        "estimated_input_tokens": breakdown.total_tokens,
        "system": request.system,
        "instructions": request.instructions,
        "evidence": request.evidence,
        "schema": dict(request.schema),
        "images": [
            {
                "view": item.frame.view,
                "capture_tick": item.frame.capture_tick,
                "file": item.frame.file,
                "width": item.width,
                "height": item.height,
                "bytes": len(item.data),
                "sha256": item.sha256,
                "detail": DETAIL,
                "label": image.label,
            }
            for item, image in zip(loaded, request.images, strict=True)
        ],
    }


def _report(
    shot: str,
    moments: Sequence[Moment],
    loaded: Sequence[LoadedFrame],
    *,
    breakdown_tokens: int,
    sent: bool,
    payload: Mapping[str, Any] | None = None,
    problems: list[str] | None = None,
    selection: PrimarySelection | None = None,
    coaching: Mapping[str, str] | None = None,
    model: str | None = None,
    latency_seconds: float | None = None,
    usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    input_tokens = None if usage is None else usage.get("input_tokens")
    output_tokens = None if usage is None else usage.get("output_tokens")
    if sent:
        cost = estimate_cost(model or MODEL, input_tokens, output_tokens)
        cost_basis = "measured_usage"
    else:
        cost = estimate_cost(MODEL, breakdown_tokens, 0)
        cost_basis = "estimated_input_tokens_only"
    classification = None if payload is None else _model_classification(payload)
    return {
        "shot": shot,
        "experiment": "direct_visual_coach",
        "architecture": "visual_fault_catalog_v1",
        "catalog_exposure": catalog_exposure(),
        "sent": sent,
        "model": MODEL,
        "answered_model": model,
        "reasoning_effort": REASONING_EFFORT,
        "selected_capture_ticks": [moment.capture_tick for moment in moments],
        "motion_rates": [
            {
                "capture_tick": moment.capture_tick,
                "replay_seconds": round(moment.replay_seconds, 4),
                "motion_rate": round(moment.motion_rate, 4),
            }
            for moment in moments
        ],
        "image_inputs": len(loaded),
        "estimated_input_tokens": breakdown_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": None if usage is None else usage.get("reasoning_tokens"),
        "openai_latency_s": None
        if latency_seconds is None
        else round(latency_seconds, 3),
        "estimated_cost_usd": cost,
        "cost_basis": cost_basis,
        "model_classification": classification,
        "primary_selection": None if selection is None else selection.as_dict(),
        "caddiej_coaching": None if coaching is None else dict(coaching),
        "validation": None
        if problems is None
        else {"ok": not problems, "problems": problems},
        "production_reconciliation": False,
    }


def _model_classification(payload: Mapping[str, Any]) -> dict[str, Any]:
    orientation = payload.get("temporal_orientation")
    candidates = payload.get("fault_candidates")
    return {
        "status": payload.get("status"),
        "handedness": payload.get("handedness"),
        "temporal_orientation": orientation if isinstance(orientation, list) else None,
        "fault_candidates": candidates if isinstance(candidates, list) else None,
    }


def _motion_line(rates: Any) -> str:
    if not isinstance(rates, list) or not rates:
        return "motion_rate: -"
    bits = [
        f"{row['capture_tick']}:{row['motion_rate']:.1f}"
        for row in rates
        if isinstance(row, dict)
    ]
    return "motion_rate: " + " ".join(bits)


def _classification_lines(report: Mapping[str, Any]) -> list[str]:
    lines = ["MODEL CLASSIFICATION"]
    classification = report.get("model_classification")
    if not isinstance(classification, dict):
        lines.append("status: -")
        return lines
    lines.append(f"status: {classification.get('status')}")
    handedness = classification.get("handedness")
    if isinstance(handedness, dict):
        lines.append(
            f"handedness: {handedness.get('side')} "
            f"confidence={handedness.get('confidence')} "
            f"{handedness.get('visual_basis')}"
        )
    else:
        lines.append("handedness: -")
    lines.append("temporal_orientation:")
    orientation = classification.get("temporal_orientation")
    if isinstance(orientation, list) and orientation:
        for item in orientation:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"  {item.get('capture_tick')} {item.get('state')} "
                f"confidence={item.get('confidence')} {item.get('visual_basis')}"
            )
    else:
        lines.append("  -")
    lines.append("fault_candidates:")
    candidates = classification.get("fault_candidates")
    if isinstance(candidates, list) and candidates:
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            lines.append(
                f"  {candidate.get('fault_id')} confidence={candidate.get('confidence')}"
            )
            evidence = candidate.get("evidence")
            if isinstance(evidence, list):
                for item in evidence:
                    if isinstance(item, dict):
                        lines.append(
                            f"    {item.get('view')} {item.get('capture_tick')} "
                            f"{item.get('observation')}"
                        )
            alternatives = candidate.get("alternatives_considered")
            if isinstance(alternatives, list):
                for item in alternatives:
                    if isinstance(item, dict):
                        lines.append(
                            f"    rejected {item.get('fault_id')}: "
                            f"{item.get('reason_rejected')}"
                        )
    else:
        lines.append("  -")
    lines.append("PRIMARY SELECTION")
    selection = report.get("primary_selection")
    if isinstance(selection, dict):
        lines.append(f"fault_id: {selection.get('fault_id')}")
        lines.append(f"reason: {selection.get('reason')}")
    else:
        lines.append("fault_id: -")
        lines.append("reason: -")
    lines.append("CADDIEJ COACHING OUTPUT")
    coaching = report.get("caddiej_coaching")
    if isinstance(coaching, dict):
        lines.append(f"voice: {coaching.get('voice')}")
        lines.append(f"floor: {coaching.get('floor')}")
        lines.append(f"feel: {coaching.get('feel')}")
        lines.append(f"drill: {coaching.get('drill')}")
    else:
        lines.append("none")
    return lines


def format_summary(report: Mapping[str, Any]) -> str:
    def shown(value: Any) -> str:
        return "-" if value is None else str(value)

    lines = [
        f"shot: {report['shot']}",
        f"sent: {'yes' if report['sent'] else 'no'}",
        f"model: {report['model']}  reasoning_effort: {report['reasoning_effort']}",
        f"capture_ticks: {' '.join(str(t) for t in report['selected_capture_ticks'])}",
        _motion_line(report.get("motion_rates")),
        f"image_inputs: {report['image_inputs']}",
        f"estimated_input_tokens: {report['estimated_input_tokens']}",
        f"input_tokens: {shown(report['input_tokens'])}",
        f"output_tokens: {shown(report['output_tokens'])}",
        f"openai_latency_s: {shown(report['openai_latency_s'])}",
        f"estimated_cost_usd: {shown(report['estimated_cost_usd'])} ({report['cost_basis']})",
    ]
    if isinstance(report.get("model_classification"), dict):
        lines.extend(_classification_lines(report))
    validation = report.get("validation")
    if isinstance(validation, dict) and validation.get("problems"):
        lines.append("validation: " + "; ".join(validation["problems"]))
    return "\n".join(lines)


async def send_openai(request: AdviceRequest) -> Any:
    """The one network call. Reached only from execute when send is true."""

    from caddiej.adapters.openai.provider import OpenAIAdviceProvider
    from caddiej.config import Settings

    settings = Settings()
    key = settings.coaching_api_key.get_secret_value().strip()
    if not key:
        raise SystemExit("COACHING_API_KEY is empty; the request was not sent")
    provider = OpenAIAdviceProvider(
        api_key=key,
        model=MODEL,
        base_url=settings.coaching_api_base or None,
        detail=DETAIL,
    )
    try:
        return await provider.complete(request)
    finally:
        await provider.aclose()


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Direct visual coaching probe. Sends only with --send."
    )
    parser.add_argument("shot", help="preserved fresh_v2 shot id")
    parser.add_argument(
        "--send",
        action="store_true",
        help="send the request to OpenAI; without this flag nothing is sent",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = execute(args.shot, send=args.send, shots_root=SHOTS, results_root=RESULTS)
    print(format_summary(report))
    print(f"wrote: {RESULTS / report['shot']}")
    validation = report.get("validation")
    if isinstance(validation, dict) and not validation.get("ok", True):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
