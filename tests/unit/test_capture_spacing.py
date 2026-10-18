"""No two requests for one camera inside one replay frame.

On the sixth 20 fps verification swing of 2026-10-08 the deadline at 3.85 s
woke a timer tick late, at 3.8656 s, the two body screenshots took 18 ms, and
the 3.90 s deadline was then inside the early-fire allowance, so it went out
at 3.8858 s: 20 ms after the last, inside one frame of the replay. OBS
returned the same picture twice on both cameras, the delivery aim took it,
and the impact aim, finding only that picture near it, was rightly dropped.
The selector was right. The schedule now keeps each camera's requests at
least `MIN_SPACING` of a period apart, without moving any later deadline and
without catching up.
"""

from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
from typing import Any

import pytest
from tests.unit.capture_bay import BEHIND, SIDE
from tests.unit.test_capture_cadence import (
    BODY,
    DOWNSWING,
    TICK,
    Burst,
    WindowsClock,
    camera_metadata,
    gaps,
    offsets,
)
from tests.unit.test_downswing_phase_regression import Sequence, kept

from caddiej.domain.cues import PanelView
from caddiej.domain.swing_phase import SwingPhase
from caddiej.swing_capture.cadence import (
    EARLY_SECONDS,
    MIN_SPACING,
    sample_times,
    spacing_floor,
)
from caddiej.swing_capture.select import replay_start

START = 1000.0
FAST_SPACING = 0.035
"""The least gap between one camera's requests at 20 a second."""
MS = 0.0006
"""Offsets in `metadata.json` are written to the millisecond."""


LATE_DEADLINE = 3.85
LATE_INDEX = sample_times(0.5, 4.0, 10.0, DOWNSWING).index(LATE_DEADLINE)


class LateWakeClock(WindowsClock):
    """Sleeps end exactly when asked, except the wake for the down-the-line
    camera's `LATE_INDEX` deadline, which ends one tick late: the shot-6 wake."""

    def __init__(self) -> None:
        super().__init__()
        self.taken: dict[str, int] = {}
        self.woke_late = False

    async def sleep(self, seconds: float) -> None:
        task = asyncio.current_task()
        if task is not None and task.get_name() == "coach-metrics-capture":
            await asyncio.sleep(0)
            return
        self.now += max(0.0, seconds)
        if not self.woke_late and self.taken.get(BEHIND, 0) == LATE_INDEX:
            self.now += TICK
            self.woke_late = True
        await asyncio.sleep(0)


def a_burst(tmp_path: Path, clock: WindowsClock, **options: Any) -> Burst:
    burst = Burst(tmp_path, **options)
    burst.clock = clock
    burst.obs.clock = clock
    burst.service._monotonic = clock.monotonic  # noqa: SLF001
    burst.service._sleep = clock.sleep  # noqa: SLF001
    return burst


def screenshot_ms(milliseconds: float) -> Any:
    """Each body screenshot takes this long; the two of a tick, twice it."""

    return lambda source, _nth: milliseconds / 1000.0 if source in (BEHIND, SIDE) else 0


def deadlines() -> tuple[float, ...]:
    return sample_times(0.5, 4.0, 10.0, DOWNSWING)


# --- the rule ----------------------------------------------------------------


class TestTheSpacingRule:
    def test_it_is_seventy_percent_of_the_plan_s_own_period(self) -> None:
        plan = deadlines()
        fast = plan.index(3.85)
        slow = plan.index(1.0)

        assert MIN_SPACING == 0.7
        assert spacing_floor(plan, fast, 3.8656) == pytest.approx(3.8656 + 0.035)
        assert spacing_floor(plan, slow, 0.9) == pytest.approx(0.9 + 0.07)

    def test_a_first_request_has_no_floor(self) -> None:
        plan = deadlines()

        assert spacing_floor(plan, 0, None) == -math.inf
        assert spacing_floor(plan, 5, None) == -math.inf


# --- the shot-6 pattern --------------------------------------------------------


async def shot_six(tmp_path: Path) -> tuple[Burst, Any]:
    """The 3.85 s deadline wakes 15.6 ms late and each tick's two body
    screenshots take 19 ms, so the 3.90 s deadline is inside the early-fire
    allowance when the tick before it is answered."""

    clock = LateWakeClock()
    burst = a_burst(tmp_path, clock, latency=screenshot_ms(9.5))
    clock.taken = burst.obs.taken
    capture = await burst.shot()
    assert clock.woke_late
    return burst, capture


class TestTheShotSixPattern:
    async def test_the_pattern_is_the_one_measured(self, tmp_path) -> None:
        burst, capture = await shot_six(tmp_path)

        times = offsets(capture, "swing_behind")
        late = LATE_INDEX
        assert times[late] == pytest.approx(LATE_DEADLINE + TICK, abs=MS)
        answered = times[late] + 0.019
        assert 3.90 - answered <= EARLY_SECONDS, "the old rule would fire at once"
        assert times[late + 1] - times[late] >= FAST_SPACING - 1e-9

    async def test_no_two_requests_for_a_camera_are_closer_than_the_spacing(
        self, tmp_path
    ) -> None:
        burst, capture = await shot_six(tmp_path)

        for camera_id in BODY:
            times = offsets(capture, camera_id)
            assert min(gaps(times)) >= FAST_SPACING - 1e-9, camera_id
        for source in (BEHIND, SIDE):
            starts = [t for name, t in burst.obs.log if name == source]
            assert min(gaps(starts)) >= FAST_SPACING - 1e-9, source

    async def test_the_held_request_does_not_move_any_later_deadline(
        self, tmp_path
    ) -> None:
        _burst, capture = await shot_six(tmp_path)

        plan = deadlines()
        for camera_id in BODY:
            times = offsets(capture, camera_id)
            assert len(times) == len(plan) == 51
            late = LATE_INDEX
            held = late + 1
            assert times[held] == pytest.approx(times[late] + FAST_SPACING, abs=MS)
            for index in range(held + 1, len(plan)):
                assert times[index] == pytest.approx(plan[index], abs=MS), index
            for index in range(late):
                assert times[index] == pytest.approx(plan[index], abs=MS), index

    async def test_it_is_counted_once_and_costs_no_still(
        self, tmp_path, caplog
    ) -> None:
        with caplog.at_level(logging.INFO, logger="caddiej.swing_capture"):
            _burst, capture = await shot_six(tmp_path)

        for camera_id in BODY:
            cadence = camera_metadata(capture, camera_id)["cadence"]
            assert cadence["spacing_holds"] == 1
            assert cadence["missed_deadlines"] == 0
            assert cadence["requested"] == cadence["planned"] == 51
            assert cadence["interval_min_ms"] >= 35.0 - 0.05
        lines = [r for r in caplog.records if r.getMessage() == "swing_capture_cadence"]
        assert len(lines) == 2
        assert all(r.swing_capture_spacing_holds == 1 for r in lines)  # type: ignore[attr-defined]
        assert not [
            r for r in caplog.records if r.getMessage() == "swing_capture_frame"
        ]


# --- no catching up -------------------------------------------------------------


class TestNoCatchUp:
    async def test_a_stall_in_the_downswing_skips_deadlines_and_never_bursts(
        self, tmp_path
    ) -> None:
        """One request takes 120 ms in the 20 fps stretch. The deadlines it
        covered are skipped, the latest is taken, and nothing is asked for
        sooner than the spacing to make up for it."""

        def stall(source: str, nth: int) -> float:
            return 0.12 if source == BEHIND and nth == 35 else 0.0

        clock = WindowsClock()
        burst = a_burst(tmp_path, clock, latency=stall)
        capture = await burst.shot()

        for camera_id in BODY:
            times = offsets(capture, camera_id)
            assert min(gaps(times)) >= FAST_SPACING - 1e-9, camera_id
            cadence = camera_metadata(capture, camera_id)["cadence"]
            assert cadence["missed_deadlines"] >= 1
            assert cadence["requested"] + cadence["missed_deadlines"] == 51
        starts = [t for name, t in burst.obs.log if name == BEHIND]
        assert min(gaps(starts)) >= FAST_SPACING - 1e-9

    async def test_slow_screenshots_still_never_queue(self, tmp_path) -> None:
        clock = WindowsClock()
        burst = a_burst(tmp_path, clock, latency=screenshot_ms(30))
        capture = await burst.shot()

        for camera_id in BODY:
            times = offsets(capture, camera_id)
            assert times == sorted(times)
            assert min(gaps(times)) >= FAST_SPACING - 1e-9
        last_tick = 4.5 + 2 * 0.03
        assert max(t for _name, t in burst.obs.log) - START <= last_tick + 1e-6


# --- nothing else moves -----------------------------------------------------------


def on_ticks(plan: tuple[float, ...]) -> list[float]:
    """Where a sleep to each deadline ends on the Windows clock."""

    return [math.ceil((START + time) / TICK - 1e-9) * TICK - START for time in plan]


class TestNothingElseMoves:
    @pytest.mark.parametrize("downswing_fps", [0.0, 20.0], ids=["10fps", "20fps"])
    async def test_with_prompt_screenshots_every_request_is_where_it_was(
        self, tmp_path, downswing_fps: float
    ) -> None:
        """No spacing hold on the plain Windows clock: each request starts on
        the tick its deadline sleeps to, as before the rule."""

        capture = await Burst(tmp_path, downswing_fps=downswing_fps).shot()

        segment = DOWNSWING if downswing_fps else None
        plan = sample_times(0.5, 4.0, 10.0, segment)
        for camera_id in BODY:
            assert offsets(capture, camera_id) == pytest.approx(on_ticks(plan), abs=MS)
            assert camera_metadata(capture, camera_id)["cadence"]["spacing_holds"] == 0

    async def test_ten_fps_spacing_is_seventy_ms_and_holds_nothing_on_time(
        self, tmp_path
    ) -> None:
        capture = await Burst(tmp_path, downswing_fps=0.0).shot()

        for camera_id in BODY:
            assert min(gaps(offsets(capture, camera_id))) >= 0.07


SHOT_6_OFFSETS = (
    0.5229, 0.6015, 0.7122, 0.8084, 0.903, 1.012, 1.1057, 1.2158, 1.312,
    1.4081, 1.5197, 1.5998, 1.7115, 1.8067, 1.9018, 2.0115, 2.1066, 2.2027,
    2.3149, 2.411, 2.5066, 2.6027, 2.7144, 2.8105, 2.9066, 3.0008, 3.1099,
    3.203, 3.3121, 3.3602, 3.4078, 3.4548, 3.502, 3.5641, 3.6107, 3.6587,
    3.7063, 3.7549, 3.8025, 3.8656, 3.8858, 3.9594, 4.0063, 4.0528, 4.115,
    4.136, 4.2081, 4.2561, 4.3026, 4.3642, 4.3841,
)  # fmt: skip
SHOT_6_BEHIND = (
    37371, 36823, 36188, 35779, 36527, 37335, 38049, 38552, 39351, 40241,
    40190, 39754, 38812, 43689, 66055, 65819, 66163, 66003, 66643, 66726,
    66288, 66702, 66075, 66366, 66008, 67359, 66928, 67119, 66944, 67052,
    66061, 66870, 65972, 66039, 66292, 65854, 65887, 65702, 66191, 63396,
    63396, 62962, 63827, 64213, 64561, 64785, 65607, 66012, 64926, 65577,
    65577,
)  # fmt: skip
SHOT_6_SIDE = (
    43478, 42947, 42210, 41811, 42655, 43411, 44032, 44671, 45329, 46275,
    46299, 45816, 44866, 98336, 188588, 189281, 188755, 188883, 189058,
    188989, 187010, 186118, 185858, 186722, 188035, 187910, 188031, 187832,
    187424, 187213, 187597, 187979, 187688, 190899, 188574, 190916, 187984,
    187835, 186416, 190101, 190101, 186594, 186492, 186480, 187812, 187354,
    188087, 191103, 188753, 191938, 191938,
)  # fmt: skip
"""Shot 6 as captured, before the rule: stills 40 and 41, requested 20 ms
apart, are one picture on both cameras, and so are 50 and 51."""


@pytest.mark.parametrize(
    "sequence",
    [
        Sequence("shot6_behind", SHOT_6_OFFSETS, SHOT_6_BEHIND, PanelView.BEHIND),
        Sequence("shot6_side", SHOT_6_OFFSETS, SHOT_6_SIDE, PanelView.SIDE),
    ],
    ids=lambda s: s.name,
)
class TestShotSixSelectionIsUnchanged:
    def test_the_impact_with_no_picture_of_its_own_stays_unavailable(
        self, sequence: Sequence
    ) -> None:
        """The selector still refuses the repeated picture and has no other
        still within its 25 ms reach of the impact aim."""

        frames = sequence.frames()
        assert replay_start(frames) == 14
        assert frames[39].data == frames[40].data

        stills = kept(sequence)

        assert SwingPhase.IMPACT not in stills
        assert stills[SwingPhase.TRANSITION] == 39
        assert stills[SwingPhase.DOWNSWING] == 40
        assert stills[SwingPhase.RELEASE] == 42
        assert 41 not in stills.values()
        assert len(set(stills.values())) == len(stills)
