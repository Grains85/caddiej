"""Choosing which of the captured stills are worth keeping.

A capture asks OBS for a still ten times a second for several seconds, which is
forty-odd pictures of a two-and-a-half second replay. Most of them are the same
picture: the replay has not started yet, or it has finished and the panel is
holding its last frame. Sending forty near-identical images to a model is slow,
expensive, and no more informative than sending eight.

**Nothing here decodes an image.** There is no JPEG decoder in the dependency
set and this milestone is not a reason to add one, so the only two things this
knows about a still are its encoded bytes and how big they are. That is enough
for the one question worth asking cheaply — *did the picture change at all?* —
because OBS encodes identical pixels to identical bytes. It is not enough to
find impact, and this does not pretend to: the interval it finds is "the part
of the window where something was moving", not "the swing".

**Size finds what equality cannot: the end of a loading screen.** ProTee does
not hold a still before its replay, it animates a spinner, and no two frames of
a spinner are byte-identical. Equality therefore sees movement from the first
still and trims nothing, and a uniform spread puts the first chosen moment on
the spinner. But a spinner on a plain background is a small file and a replay
of a person in a bay is a large one, and the switch from one to the other is a
step in encoded size that stays stepped: the replay never goes back to looking
like a loading screen. That step is the third trim, after the two equality
ones, and it asks for both halves — a jump, and a level the rest of the window
holds — because either alone is something a loading screen can produce. A
flash during loading jumps and falls back; a picture that slowly fills in
holds a level it reached without jumping.

The level is the replay's own, not the window's. ProTee fades the replay in
under its spinner, so the last loading still or two is a crossfade that can
compress to three quarters of a replay still on a dark down-the-line picture;
a level measured against the whole window then starts the replay on the
crossfade, finds no step there, and gives up. The cut itself is the biggest
jump in the window, and what follows it agrees with itself.

The stills before the replay are captured on purpose and never sent. They cost
no tokens, and they are the baseline the step is measured against: a capture
that began on the replay would have no step to find.

**A body camera is never spread across a timeline nobody found.** Both body
replays begin on the same capture tick, so a camera whose own start was not
found borrows the other body camera's. When neither was found nothing is kept
from either: an even spread across the window is how a spinner frame and the
previous swing's finish reached the coach. Any other panel -- one whose
contents this knows nothing about -- still gets the uniform spread, which is
the right answer when there is no replay to find.

**Both cameras are selected against one timeline.** The two panels are showing
the same swing, so the frames handed to a coach should be the same moments seen
twice. Targets are computed once, in seconds, and each camera contributes
whichever of its own stills is nearest each target. A camera that missed a few
requests therefore still lines up with the other one, rather than drifting by
however many stills it lost.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import median

from caddiej.domain.cues import PanelView
from caddiej.domain.swing_phase import SwingPhase
from caddiej.swing_capture.phases import phase_plan

# What "the picture changed" means when the only evidence is encoded bytes.
# Identical bytes mean identical pixels; anything else is movement of some
# amount that this cannot measure, so it is reported as movement.
_CHANGED = 1.0
_UNCHANGED = 0.0

# A trimmed interval this short is not a replay, it is two stills that happened
# to differ. Below it the fallback is used instead, as a fraction of everything
# captured.
_MINIMUM_ACTIVE_FRACTION = 0.15

# A single still this far below the window's median is not a replay still: a
# loading screen, an empty player, a panel that has not drawn. A per-still
# test only; where the replay begins is decided by `replay_start`.
_REPLAY_SIZE_RATIO = 0.75

# The replay is entered by a step, not a slope. The real cuts were 1.28-1.39x
# on the down-the-line panel and 1.7-4x face-on; the stills either side of a
# crossfade differed by at most 1.11x. The spinner can jump too -- 1.36x once
# -- which is why a step decides nothing until the level after it holds.
_REPLAY_STEP = 1.2

# And the level holds: every still after the step stays within this of the
# median of the stills after the step. Replay stills never fell below 0.95 of
# their own median on six real sequences; a loading still is 0.8 or less.
_REPLAY_HOLD = 0.9

# Fewer stills than this after a step are not a level that can be said to hold.
_REPLAY_MINIMUM_STILLS = 3

# Both body cameras are asked in the same tick, so the same moment has the same
# offset in both, to within scheduling noise far below one still.
_SAME_TICK_SECONDS = 0.02

_BODY_VIEWS = frozenset({PanelView.BEHIND, PanelView.SIDE})

# How far past the interval's end a calibrated moment may still be aimed: half
# a still at the default 10 a second, so the last still of a window that closed
# just after the moment can still stand for it.
_AIM_TOLERANCE = 0.05


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    """One still as it came back from OBS, before anything chose anything."""

    offset_seconds: float
    """From the start of the capture window, not from impact."""

    data: bytes


@dataclass(frozen=True, slots=True)
class ActiveInterval:
    """When a camera's picture was moving, and how that was decided."""

    start_seconds: float
    end_seconds: float
    detected: bool
    """False when the trimming was not usable and the whole window is being
    reported instead. Written into the metadata so that a capture that looks
    badly timed can be told apart from one the trimming gave up on."""

    pre_replay_frames: int = 0
    """How many leading stills were judged to be the loading screen before the
    replay, by size rather than equality. Zero when no replay start was found,
    which includes every capture of a panel that holds a still instead."""

    borrowed_from: str | None = None
    """The other body camera whose replay start this one is timed from, when
    its own could not be found. None when the start is this camera's own."""


def likely_blank(data: bytes) -> bool:
    """True when a JPEG is so small it is almost certainly a black frame.

    Only applied to real JPEGs. Test bytes and other payloads are left alone.

    An absolute floor, and so it only catches a frame that is nearly empty in
    absolute terms. A panel showing an empty replay player is not: shot 3 of
    the first hardware session sent 47 KB of uniform grey with a scrubber
    drawn on it, which is forty times this and carries no swing at all. That
    one is caught by `unlike_replay`, which has the rest of the window to
    compare against and can therefore see that it is nothing like the stills
    around it.
    """

    return data.startswith(b"\xff\xd8") and len(data) < 800


def unlike_replay(frames: Sequence[CapturedFrame], index: int) -> bool:
    """True when this still is too unlike the rest of the window to be the replay.

    The same measurement `replay_start` already trusts, asked about one frame
    instead of about a boundary: a still well below the window's median
    encoded size is a loading screen, an empty player or a panel that has not
    drawn yet, because a replay of a person in a bay does not compress to a
    third of what the replay of the same person compresses to.

    This exists because `likely_blank` could not see the frame that mattered.
    An empty ProTee replay player is a large file -- it has a scrubber, grid
    lines and a gradient -- and is still not a picture of a swing. The session
    sent one as one of two face-on images and the model rated a finish from
    it.

    Compared against the median rather than against neighbours on purpose. A
    loading screen's neighbours are other loading screens, and a run of them
    agrees with itself.
    """

    if not 0 <= index < len(frames):
        return False
    sizes = [len(frame.data) for frame in frames if not likely_blank(frame.data)]
    if len(sizes) < 3:
        return False
    return len(frames[index].data) < median(sizes) * _REPLAY_SIZE_RATIO


def difference(first: bytes, second: bytes) -> float:
    """How much two encoded stills differ, as far as bytes can say.

    Zero means the two are byte-identical, which for a single encoder given the
    same pixels means the picture did not change at all — a replay that has not
    started, or one that has ended on a held frame.

    Anything else is reported as a full change rather than a measured one. The
    honest alternative would be decoding both images, and a byte-distance
    between two JPEGs is not a distance between two pictures: a one-pixel move
    can rewrite most of the file. So this is a change detector, not a metric,
    and everything downstream treats it as one.
    """

    return _UNCHANGED if first == second else _CHANGED


def active_interval(frames: Sequence[CapturedFrame]) -> ActiveInterval:
    """The part of the window where the picture was moving.

    Leading and trailing stills that are identical to their neighbour are
    trimmed, and so is a loading screen that `replay_start` can see ending. If
    that leaves too little to be a replay — or there was nothing to trim — the
    whole window is returned with `detected` false.
    """

    if not frames:
        return ActiveInterval(0.0, 0.0, detected=False)

    whole = ActiveInterval(
        frames[0].offset_seconds, frames[-1].offset_seconds, detected=False
    )
    if len(frames) < 3:
        return whole

    first = 0
    while (
        first < len(frames) - 1
        and difference(frames[first].data, frames[first + 1].data) == _UNCHANGED
    ):
        first += 1

    last = len(frames) - 1
    while last > first and difference(frames[last].data, frames[last - 1].data) == (
        _UNCHANGED
    ):
        last -= 1

    loading = replay_start(frames)
    first = max(first, loading)
    if first == 0 and last == len(frames) - 1:
        # Nothing was trimmed, so nothing was learned. The camera is probably
        # showing a live view rather than a replay panel, which is normal.
        return whole
    if (last - first + 1) < max(2, len(frames) * _MINIMUM_ACTIVE_FRACTION):
        return whole
    return ActiveInterval(
        frames[first].offset_seconds,
        frames[last].offset_seconds,
        detected=True,
        pre_replay_frames=loading,
    )


def replay_start(frames: Sequence[CapturedFrame]) -> int:
    """The index of the first replay still after a loading screen, or zero.

    The still after the largest step up in encoded size whose stills from
    there on hold a level: their median is itself a step above the still
    before, so a lone bright still is not a level, and none of them is below
    `_REPLAY_HOLD` of that median. Largest, because the crossfade before a cut
    is a smaller step than the cut, and the first full replay still is the
    one wanted.

    Zero means no loading screen was found, not that the replay starts at the
    first still: a window with no step, a step the window does not hold, a rise
    too gradual to be a cut, and one that ends below the level it would have to
    hold are all answered the same way, and leave the interval to the equality
    trims exactly as if this did not exist. Blank
    stills are dropped OBS requests, not pictures, and take no part.
    """

    usable = [
        index for index, frame in enumerate(frames) if not likely_blank(frame.data)
    ]
    sizes = [len(frames[index].data) for index in usable]
    best = 0
    best_step = 0.0
    for position in range(1, len(sizes) - _REPLAY_MINIMUM_STILLS + 1):
        before = max(1, sizes[position - 1])
        step = sizes[position] / before
        if step < _REPLAY_STEP or step <= best_step:
            continue
        level = sizes[position:]
        middle = median(level)
        if middle < before * _REPLAY_STEP or min(level) < middle * _REPLAY_HOLD:
            continue
        best, best_step = position, step
    return usable[best] if best else 0


def targets(interval: ActiveInterval, count: int) -> tuple[float, ...]:
    """`count` moments spread evenly across the interval, ends included.

    What to ask for when nothing is known about what the replay contains. A
    camera whose phases have been calibrated is aimed by `phase_targets`
    instead, because an even spread has no way to know that one of its moments
    is the top of the backswing and another is the player walking away.
    """

    if count <= 0:
        return ()
    if count == 1:
        return ((interval.start_seconds + interval.end_seconds) / 2,)
    span = interval.end_seconds - interval.start_seconds
    if span <= 0:
        return (interval.start_seconds,)
    step = span / (count - 1)
    return tuple(interval.start_seconds + step * index for index in range(count))


def phase_targets(
    interval: ActiveInterval, view: PanelView, count: int
) -> tuple[tuple[SwingPhase, float], ...]:
    """The moments of this camera's own replay worth asking for, with their names.

    Each phase's calibrated time after *this* camera's replay start, which is
    the point: the two replays in the bay are not synchronised, and the same
    instant is a different moment of the swing in each.

    A moment after the interval's end is dropped rather than moved: the window
    closed before the replay reached it, and the nearest earlier still is a
    picture of something else.

    Empty when the view has no calibration or the interval has no span, and
    the caller spreads moments evenly instead. Deliberately not a silent
    fallback inside here -- a caller that gets nothing back should be able to
    record that it aimed at nothing in particular.
    """

    plan = phase_plan(view, count)
    span = interval.end_seconds - interval.start_seconds
    if not plan or span <= 0:
        return ()
    return tuple(
        (phase, interval.start_seconds + seconds)
        for phase, seconds in plan
        if seconds <= span + _AIM_TOLERANCE
    )


def distinct_count(frames: Sequence[CapturedFrame]) -> int:
    """How many different pictures, not counting blanks. Identical bytes are one."""

    return len({frame.data for frame in frames if not likely_blank(frame.data)})


def nearest(
    frames: Sequence[CapturedFrame],
    moments: Sequence[float],
    *,
    not_before: float | None = None,
) -> tuple[int, ...]:
    """The still closest to each moment, in order, never the same picture twice.

    When the closest still is a picture already kept — a panel holding a frame
    — the closest *different* picture within half a step of the moment is
    taken instead, so a moment is never represented by another moment's
    picture from far away. If there is none, the moment is dropped: a camera
    that lost half its stills, or returned one frozen picture, sends fewer
    frames rather than the same picture twice pretending to be two moments.

    `not_before` is a replay start: no still requested before it is a
    candidate, whatever its size, so an aimed camera can never stand a
    crossfade or a spinner in for its address.
    """

    earliest = -float("inf") if not_before is None else not_before - 1e-9
    candidates = [
        index
        for index, frame in enumerate(frames)
        if frame.offset_seconds >= earliest and not likely_blank(frame.data)
    ]
    usable = [index for index in candidates if not unlike_replay(frames, index)]
    if not usable:
        # Every still looked unlike a replay, which means the window has no
        # replay in it to be unlike -- a live view, or a panel that never
        # drew. The size test has nothing to say then, and refusing the whole
        # camera over it would lose a capture that the old code handled.
        usable = candidates
    if not usable:
        return ()
    ordered = sorted(moments)
    gaps = [later - earlier for earlier, later in zip(ordered, ordered[1:])]
    reach = (min(gaps) / 2) if gaps and min(gaps) > 0 else float("inf")
    chosen: list[int] = []
    seen: set[bytes] = set()
    for moment in moments:
        by_distance = sorted(
            usable,
            key=lambda position: (
                abs(frames[position].offset_seconds - moment),
                position,
            ),
        )
        closest = by_distance[0]
        if closest in chosen:
            continue
        for index in by_distance:
            if index != closest and (
                abs(frames[index].offset_seconds - moment) > reach + 1e-9
            ):
                break
            if index in chosen or frames[index].data in seen:
                continue
            chosen.append(index)
            seen.add(frames[index].data)
            break
    return tuple(sorted(chosen))


def static_choice(frames: Sequence[CapturedFrame]) -> tuple[int, bool] | None:
    """The settled still among frames that are not blank, or nothing."""

    usable = [
        index for index, frame in enumerate(frames) if not likely_blank(frame.data)
    ]
    if not usable:
        return None
    local, settled = settled_frame([frames[index] for index in usable])
    return usable[local], settled


def settled_frame(frames: Sequence[CapturedFrame]) -> tuple[int, bool]:
    """The still of a static panel after it stopped drawing.

    Walks backwards and returns the first still that matches its predecessor.
    That is the panel after rendering finished. If no two stills match, the
    last one is returned and marked unsettled — a first guess, not a claim
    that the graphic is complete.
    """

    if not frames:
        return 0, False
    if len(frames) == 1:
        return 0, False
    for index in range(len(frames) - 1, 0, -1):
        if difference(frames[index].data, frames[index - 1].data) == _UNCHANGED:
            return index, True
    return len(frames) - 1, False


def package_quota(
    budget: int, panels: Sequence[tuple[str, bool, int]]
) -> dict[str, int]:
    """How many kept stills each panel may contribute. The sum is at most `budget`.

    `panels` is `(panel_id, is_static, usable_frames)` in capture order. A panel
    with nothing usable gets nothing. Each static panel is offered one still
    when that still leaves every live dynamic panel at least one. What remains
    is split across the dynamic panels; earlier panels receive the remainder.
    """

    quota = {panel_id: 0 for panel_id, _static, _usable in panels}
    if budget <= 0:
        return quota
    dynamic = [
        (panel_id, usable)
        for panel_id, is_static, usable in panels
        if not is_static and usable > 0
    ]
    static = [
        panel_id for panel_id, is_static, usable in panels if is_static and usable > 0
    ]
    static_slots = 0
    if static:
        room = budget - len(dynamic) if dynamic else budget
        static_slots = max(0, min(len(static), room))
    for panel_id in static[:static_slots]:
        quota[panel_id] = 1
    remaining = budget - static_slots
    if not dynamic or remaining <= 0:
        return quota
    base, extra = divmod(remaining, len(dynamic))
    for index, (panel_id, usable) in enumerate(dynamic):
        quota[panel_id] = min(usable, base + (1 if index < extra else 0))
    unused = remaining - sum(quota[panel_id] for panel_id, _usable in dynamic)
    if unused <= 0:
        return quota
    for panel_id, usable in dynamic:
        room = usable - quota[panel_id]
        take = min(room, unused)
        quota[panel_id] += take
        unused -= take
        if unused <= 0:
            break
    return quota


def take_evenly(indices: Sequence[int], count: int) -> tuple[int, ...]:
    """`count` indices spread across `indices`, ends included, in order."""

    if count <= 0 or not indices:
        return ()
    if count >= len(indices):
        return tuple(indices)
    if count == 1:
        return (indices[len(indices) // 2],)
    step = (len(indices) - 1) / (count - 1)
    return tuple(indices[round(step * position)] for position in range(count))


def select(
    tracks: Mapping[str, Sequence[CapturedFrame]],
    count: int | Mapping[str, int],
    views: Mapping[str, PanelView] | None = None,
) -> tuple[
    dict[str, tuple[int, ...]],
    dict[str, ActiveInterval],
    ActiveInterval,
    dict[str, tuple[SwingPhase, ...]],
]:
    """Pick the most informative set of stills from every camera.

    Each camera is aimed across **its own** active interval, at the phases
    `phases.phase_plan` says are worth buying, and the phase each chosen still
    was aimed at is returned beside it.

    Both of those are changes of principle rather than of tuning, and both come
    from the same hardware session.

    Aiming per camera, because the two replays are not synchronised: on the
    same capture the same instant is a different moment of the swing in each
    (`phases` has the measurements). The old code computed one combined
    interval and asked every camera for the same instants, which guaranteed
    that at most one of the two views was looking at the moment being asked
    about.

    And aiming within the camera's own interval, because the combined one
    reached back before some cameras' replays had started. On shot 3 of the
    session the face-on panel's own interval correctly began after its loading
    frame; the combined interval began earlier, and the nearest still to that
    target was the loading frame -- 47 KB of empty grey, sent to the model as
    one of only two face-on images, and rated `balance=good`.

    The combined interval is still computed and still returned, because the
    metadata records it and a reader comparing two cameras wants it. It no
    longer decides anything.

    `count` may be one number for every camera or one per camera. Per camera
    is what the capture service passes, because each panel's quota is already
    worked out by then and asking for more and thinning afterwards is how the
    aiming was being undone: an even thinning of six aimed moments keeps three
    of them chosen for their spacing rather than for what they show.
    """

    counts = count if isinstance(count, Mapping) else dict.fromkeys(tracks, count)
    views = views or {}
    intervals = shared_body_start(
        tracks,
        {name: active_interval(frames) for name, frames in tracks.items()},
        views,
    )
    combined = _combined(tracks, intervals)
    chosen: dict[str, tuple[int, ...]] = {}
    aimed: dict[str, tuple[SwingPhase, ...]] = {}
    for name, frames in tracks.items():
        wanted = counts.get(name, 0)
        view = views.get(name, PanelView.NONE)
        found = replay_found(frames, intervals[name])
        plan = phase_targets(intervals[name], view, wanted) if found else ()
        if plan:
            indices = nearest(
                frames,
                [moment for _phase, moment in plan],
                not_before=intervals[name].start_seconds,
            )
            chosen[name] = indices
            aimed[name] = _aimed_phases(frames, indices, plan)
            continue
        aimed[name] = ()
        if view in _BODY_VIEWS and not found:
            chosen[name] = ()
            continue
        chosen[name] = nearest(frames, targets(intervals[name], wanted))
    return chosen, intervals, combined, aimed


def shared_body_start(
    tracks: Mapping[str, Sequence[CapturedFrame]],
    intervals: Mapping[str, ActiveInterval],
    views: Mapping[str, PanelView],
) -> dict[str, ActiveInterval]:
    """Lend one body camera's replay start to the other when only it has one.

    The two body replays began on the same capture tick on every real shot
    measured, so the moment one panel cut from its spinner to its replay is
    the moment the other did. Only between body cameras, and only when
    exactly one of them found its start: two found starts are each their own,
    and two missing ones have nothing to lend.
    """

    result = dict(intervals)
    body = [
        name
        for name, frames in tracks.items()
        if frames and views.get(name) in _BODY_VIEWS
    ]
    found = [name for name in body if replay_found(tracks[name], intervals[name])]
    missing = [name for name in body if name not in found]
    if len(found) != 1 or len(missing) != 1:
        return result
    donor, name = found[0], missing[0]
    borrowed = _borrowed(tracks[name], intervals[name], intervals[donor], donor)
    if borrowed is not None:
        result[name] = borrowed
    return result


def _borrowed(
    frames: Sequence[CapturedFrame],
    own: ActiveInterval,
    donor: ActiveInterval,
    donor_name: str,
) -> ActiveInterval | None:
    """This camera's interval timed from another camera's replay start."""

    first = next(
        (
            index
            for index, frame in enumerate(frames)
            if frame.offset_seconds >= donor.start_seconds - _SAME_TICK_SECONDS
            and not likely_blank(frame.data)
        ),
        None,
    )
    if first is None or first == 0:
        return None
    end = own.end_seconds
    kept = sum(
        1
        for frame in frames[first:]
        if frame.offset_seconds <= end + 1e-9 and not likely_blank(frame.data)
    )
    if kept < max(2, len(frames) * _MINIMUM_ACTIVE_FRACTION):
        return None
    return ActiveInterval(
        frames[first].offset_seconds,
        end,
        detected=True,
        pre_replay_frames=first,
        borrowed_from=donor_name,
    )


def replay_found(frames: Sequence[CapturedFrame], interval: ActiveInterval) -> bool:
    """Whether this camera's replay start was found, so its clock can be aimed on.

    Found means the interval was detected and its start trimmed something off
    the front of the window -- a loading screen or a held still. A window that
    began on a moving picture has no start in it to count from, and a
    calibration in seconds after the replay start would be counted from the
    capture's own first request instead."""

    return (
        interval.detected
        and bool(frames)
        and interval.start_seconds > frames[0].offset_seconds
    )


def _aimed_phases(
    frames: Sequence[CapturedFrame],
    chosen: Sequence[int],
    plan: Sequence[tuple[SwingPhase, float]],
) -> tuple[SwingPhase, ...]:
    """Which phase each kept still was aimed at, in the order they were kept.

    Matched by nearest moment rather than by position, because `nearest` drops
    a moment it cannot represent with a picture of its own. Pairing the two
    lists by index would then shift every label after the gap, and a still
    announced as the transition that is actually the top is worse than one
    announced as nothing at all.
    """

    labels: list[SwingPhase] = []
    for index in chosen:
        offset = frames[index].offset_seconds
        phase, _moment = min(plan, key=lambda item: abs(item[1] - offset))
        labels.append(phase)
    return tuple(labels)


def _combined(
    tracks: Mapping[str, Sequence[CapturedFrame]],
    intervals: Mapping[str, ActiveInterval],
) -> ActiveInterval:
    """One timeline for both cameras.

    The union of the detected intervals, so a camera whose replay starts a
    fraction later is not cut off at the front. If neither camera found one,
    the whole of what was captured. The loading-screen count is the one from
    whichever camera's interval starts the union, because that is the camera
    whose stills the start is measured in.
    """

    found = [
        intervals[name]
        for name, frames in tracks.items()
        if frames and intervals[name].detected
    ]
    if found:
        earliest = min(found, key=lambda interval: interval.start_seconds)
        return ActiveInterval(
            earliest.start_seconds,
            max(interval.end_seconds for interval in found),
            detected=True,
            pre_replay_frames=earliest.pre_replay_frames,
        )
    offsets = [frame.offset_seconds for frames in tracks.values() for frame in frames]
    if not offsets:
        return ActiveInterval(0.0, 0.0, detected=False)
    return ActiveInterval(min(offsets), max(offsets), detected=False)
