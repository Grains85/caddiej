"""When each screenshot of a burst is due, and how close the burst came to it.

**Every still is due at a fixed time from the start of the burst.** The burst
used to wait one period after each still it had just taken. On Windows a
sleep ends on the next 15.6 ms timer tick, never before, so every wait came up
a little long and each still inherited every earlier one's lateness: ten a
second asked for, 9.1 a second delivered, on every real capture measured.
Counting each deadline from the start instead of from the previous still
leaves each still up to one tick late and no later, and the average rate is
the one asked for.

**Faster only where the swing is fast.** Ten stills a second puts two stills
between P5 and P8, which take about 0.2 s, so delivery, impact and release
cannot all be near one. The downswing segment samples faster inside a fixed
window of the burst, measured from the strike like the rest of it, and wide
enough to hold the top to the release on every replay start and tempo seen;
the loading screen, the address and the backswing stay at the normal rate.
The window is fixed rather than found because finding it would need the
replay start while the burst is still running, which is a second pass.

**Late is skipped, not queued.** When OBS answers more slowly than the
schedule, every deadline that passed while waiting is dropped except the
latest one, which is asked for at once. So a slow OBS costs stills, never a
backlog, and the burst still ends when its window does.

**Never two requests in one replay frame.** A deadline is taken up to
`EARLY_SECONDS` before it is due, and the one before it may have started a
timer tick late. With an 18 ms screenshot in between, two requests at 20 a
second went out 20 ms apart, inside one frame of the replay, and OBS returned
the same picture twice: on one real swing that was the impact. So a panel's
request never starts sooner than `MIN_SPACING` of its own planned period after
its previous one. A request held back for that waits for the spacing and no
longer; the deadlines after it stay where the schedule put them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import mean, median

# A deadline this close is taken now rather than slept for. An asyncio loop
# on Windows wakes on its 15.6 ms clock and may run a timer that early, so a
# tighter tolerance would sleep again for nothing.
EARLY_SECONDS = 0.016

MIN_SPACING = 0.7
"""Of a deadline's planned period: the least time after the same panel's
previous request that it may be asked for. 35 ms at 20 a second."""


def spacing_floor(plan: Sequence[float], index: int, previous: float | None) -> float:
    """The earliest deadline `index` of `plan` may be asked for, whenever it is
    due: `MIN_SPACING` of its period after `previous`, this panel's last
    request. The period is the plan's own gap before this deadline, so it is
    50 ms inside the downswing and 100 ms outside it.
    """

    if previous is None or index == 0:
        return -math.inf
    return previous + MIN_SPACING * (plan[index] - plan[index - 1])


@dataclass(frozen=True, slots=True)
class Segment:
    """A stretch of the burst sampled at its own rate. Offsets in seconds."""

    start: float
    end: float
    fps: float


def sample_times(
    delay: float,
    duration: float,
    fps: float,
    downswing: Segment | None = None,
) -> tuple[float, ...]:
    """Every deadline of one panel's window, in order, from the burst's zero.

    The normal grid is `delay + n / fps`. Inside `downswing`, clipped to the
    window, its own grid replaces the normal one; a normal deadline closer
    than half a fast period to a fast one is dropped rather than taken twice.
    A downswing no faster than the normal rate changes nothing.
    """

    end = delay + duration
    times = _grid(delay, end, fps)
    if downswing is None or downswing.fps <= fps:
        return times
    start = max(delay, downswing.start)
    stop = min(end, downswing.end)
    if stop <= start:
        return times
    fast = _grid(start, stop, downswing.fps)
    outside = [time for time in times if time < start - 1e-9 or time >= stop - 1e-9]
    spacing = 0.5 / downswing.fps
    kept: list[float] = []
    for time in sorted([*outside, *fast]):
        if kept and time - kept[-1] < spacing - 1e-9:
            continue
        kept.append(time)
    return tuple(kept)


def _grid(start: float, stop: float, fps: float) -> tuple[float, ...]:
    """`start + n / fps` for every n that lands before `stop`."""

    if fps <= 0 or stop <= start:
        return ()
    count = math.ceil((stop - start) * fps - 1e-9)
    return tuple(round(start + index / fps, 6) for index in range(count))


@dataclass(frozen=True, slots=True)
class Cadence:
    """What one panel's burst asked for and what it got. One per shot."""

    requested_fps: float
    downswing_fps: float | None
    planned: int
    requested: int
    missed: int
    """Deadlines skipped because OBS was still answering an earlier one."""
    overruns: int
    """Requests that came back after the next deadline had already passed."""
    mean_ms: float | None
    median_ms: float | None
    min_ms: float | None
    max_ms: float | None
    achieved_fps: float | None
    downswing_achieved_fps: float | None
    request_mean_ms: float | None
    request_max_ms: float | None
    spacing_holds: int = 0
    """Requests started later than their deadline allowed, to keep them
    `MIN_SPACING` after the one before."""

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "requested_fps": self.requested_fps,
            "downswing_fps": self.downswing_fps,
            "planned": self.planned,
            "requested": self.requested,
            "missed_deadlines": self.missed,
            "overruns": self.overruns,
            "interval_mean_ms": self.mean_ms,
            "interval_median_ms": self.median_ms,
            "interval_min_ms": self.min_ms,
            "interval_max_ms": self.max_ms,
            "achieved_fps": self.achieved_fps,
            "downswing_achieved_fps": self.downswing_achieved_fps,
            "request_mean_ms": self.request_mean_ms,
            "request_max_ms": self.request_max_ms,
            "spacing_holds": self.spacing_holds,
        }


def summarise(
    *,
    requested_fps: float,
    downswing: Segment | None,
    planned: int,
    offsets: Sequence[float],
    latencies: Sequence[float],
    missed: int,
    overruns: int,
    spacing_holds: int = 0,
) -> Cadence:
    """The cadence one panel achieved, from when each request was made.

    Requests rather than stills: a request OBS failed to answer was still
    made on time, and this is about the schedule, not about OBS's replies.
    """

    gaps = [later - earlier for earlier, later in zip(offsets, offsets[1:])]
    fast = (
        []
        if downswing is None
        else [
            offset
            for offset in offsets
            if downswing.start - EARLY_SECONDS <= offset < downswing.end
        ]
    )
    return Cadence(
        requested_fps=requested_fps,
        downswing_fps=None if downswing is None else downswing.fps,
        planned=planned,
        requested=len(offsets),
        missed=missed,
        overruns=overruns,
        mean_ms=_ms(mean(gaps)) if gaps else None,
        median_ms=_ms(median(gaps)) if gaps else None,
        min_ms=_ms(min(gaps)) if gaps else None,
        max_ms=_ms(max(gaps)) if gaps else None,
        achieved_fps=_rate(offsets),
        downswing_achieved_fps=_rate(fast),
        request_mean_ms=_ms(mean(latencies)) if latencies else None,
        request_max_ms=_ms(max(latencies)) if latencies else None,
        spacing_holds=spacing_holds,
    )


def _rate(offsets: Sequence[float]) -> float | None:
    if len(offsets) < 2 or offsets[-1] <= offsets[0]:
        return None
    return round((len(offsets) - 1) / (offsets[-1] - offsets[0]), 2)


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 1)
