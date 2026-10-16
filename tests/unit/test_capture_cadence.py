"""The capture cadence: ten a second asked for, ten a second delivered, and
twenty a second where the downswing is.

Every real capture measured on the bay asked for 10 stills a second and got
9.0-9.1: the burst waited one period after each still, every Windows sleep
ends on the next 15.6 ms timer tick, and each still inherited the lateness of
all the stills before it. Two stills then spanned P5 to P8, so delivery,
impact and release could not each have one nearby.

The clock here behaves like that one: every sleep ends on the next 15.6 ms
tick. Against it, deadlines counted from the start of the burst hold the
requested rate, the downswing stretch is twice as dense and only where it is
configured, and a slow OBS costs stills, never a queue of them. Downstream
nothing moves: the same replay start, the same phases, the same 16 stills, the
same one sheet and the same one request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from pathlib import Path
from statistics import mean
from typing import Any

import pytest
from tests.unit.sheet_support import sheet_cells
from tests.unit.test_coach_protee import ALL_VALUES, DRIVER, Coach, a_reply
from tests.unit.test_release_evidence import BEHIND, IMPACT, PANELS, SIDE, BayObs

from caddiej.adapters.obs import ObsResourceNotFoundError
from caddiej.coaching.fakes import ScriptedProvider
from caddiej.config import Settings
from caddiej.domain.events import ClubSelected, ShotDetected, SwingFramesReady
from caddiej.domain.media import SwingCapture
from caddiej.domain.swing_phase import SwingPhase
from caddiej.swing_capture import LatestSwingStore, SwingCaptureService
from caddiej.swing_capture.cadence import EARLY_SECONDS, Segment, sample_times
from caddiej.swing_capture.phases import PHASE_SECONDS
from caddiej.swing_capture.store import METADATA_NAME

TICK = 0.015625
"""The Windows timer tick an asyncio sleep ends on."""

DOWNSWING = Segment(3.3, 4.4, 20.0)
BODY = ("swing_behind", "swing_side")
DEFAULTS = Settings.model_construct()


class WindowsClock:
    """A fake clock whose sleeps end on the next 15.6 ms tick, never early.

    The All Values still's own wait does not move it, as on the real clock it
    runs alongside the burst rather than before it.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        task = asyncio.current_task()
        if task is not None and task.get_name() == "coach-metrics-capture":
            await asyncio.sleep(0)
            return
        self.now = math.ceil((self.now + max(0.0, seconds)) / TICK - 1e-9) * TICK
        await asyncio.sleep(0)


class TimedObs(BayObs):
    """The bay's OBS, answering after `latency(source, nth)` seconds of the
    fake clock, and keeping the order and time of every request."""

    def __init__(
        self,
        clock: WindowsClock,
        latency: Any = None,
        failing: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self.clock = clock
        self.latency = latency or (lambda _source, _nth: 0.0)
        self.failing = failing
        self.log: list[tuple[str, float]] = []

    async def source_screenshot(
        self, source_name: str, *, width: int | None = None
    ) -> bytes:
        nth = self.taken.get(source_name, 0)
        self.log.append((source_name, self.clock.now))
        self.clock.now += self.latency(source_name, nth)
        if source_name in self.failing:
            self.taken[source_name] = nth + 1
            raise ObsResourceNotFoundError(source_name)
        return await super().source_screenshot(source_name, width=width)


class Burst:
    """The swing-capture service on the Windows clock, at the production
    timing unless told otherwise."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        latency: Any = None,
        failing: frozenset[str] = frozenset(),
        **overrides: Any,
    ) -> None:
        self.clock = WindowsClock()
        self.obs = TimedObs(self.clock, latency, failing)
        self.published: list[object] = []
        options: dict[str, Any] = {
            "initial_delay_seconds": 0.5,
            "duration_seconds": 4.0,
            "fps": DEFAULTS.swing_capture_fps,
            "downswing_fps": DEFAULTS.swing_capture_downswing_fps,
            "downswing_delay_seconds": DEFAULTS.swing_capture_downswing_delay_seconds,
            "downswing_duration_seconds": (
                DEFAULTS.swing_capture_downswing_duration_seconds
            ),
            "selected_frames": DEFAULTS.swing_capture_selected_frames,
            "static_delay_seconds": DEFAULTS.swing_capture_static_delay_seconds,
            "static_duration_seconds": DEFAULTS.swing_capture_static_duration_seconds,
            "static_fps": DEFAULTS.swing_capture_static_fps,
        }
        options.update(overrides)
        self.service = SwingCaptureService(
            lambda: self.obs,
            (),
            LatestSwingStore(tmp_path / "media" / "latest_swing"),
            self._publish,
            enabled=True,
            panels=PANELS,
            metrics_source=ALL_VALUES,
            metrics_delay_seconds=3.0,
            command_timeout=1.0,
            shutdown_timeout=0.5,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
            **options,
        )

    async def _publish(self, event: object) -> None:
        self.published.append(event)

    def captures(self) -> list[SwingCapture]:
        return [
            event.capture
            for event in self.published
            if isinstance(event, SwingFramesReady)
        ]

    async def strike(self, detected: ShotDetected) -> SwingCapture | None:
        before = len(self.captures())
        await self.service.on_shot_detected(detected)
        task = self.service._task  # noqa: SLF001
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        captures = self.captures()
        return captures[-1] if len(captures) > before else None

    async def shot(self, trigger_id: str = "90") -> SwingCapture:
        capture = await self.strike(
            ShotDetected(trigger_id=trigger_id, source="protee_log")
        )
        assert capture is not None
        return capture


def camera_metadata(capture: SwingCapture, camera_id: str) -> dict[str, Any]:
    payload = json.loads((capture.directory / METADATA_NAME).read_text("utf-8"))
    return next(c for c in payload["cameras"] if c["camera_id"] == camera_id)


def offsets(capture: SwingCapture, camera_id: str) -> list[float]:
    return [
        entry["offset_seconds"]
        for entry in camera_metadata(capture, camera_id)["timeline"]
    ]


def gaps(times: list[float]) -> list[float]:
    return [later - earlier for earlier, later in zip(times, times[1:])]


# --- the schedule itself ------------------------------------------------------


class TestTheSchedule:
    def test_each_deadline_is_counted_from_the_start_not_the_last_still(
        self,
    ) -> None:
        times = sample_times(0.5, 4.0, 10.0)

        assert len(times) == 40
        for index, time in enumerate(times):
            assert time == pytest.approx(0.5 + index * 0.1, abs=1e-9)

    def test_the_downswing_stretch_is_twice_as_dense_and_only_there(self) -> None:
        times = sample_times(0.5, 4.0, 10.0, DOWNSWING)

        before = [t for t in times if t < 3.3 - 1e-9]
        inside = [t for t in times if 3.3 - 1e-9 <= t < 4.4 - 1e-9]
        after = [t for t in times if t >= 4.4 - 1e-9]
        assert len(before) == 28
        assert all(gap == pytest.approx(0.1) for gap in gaps(before))
        assert len(inside) == 22
        assert inside[0] == pytest.approx(3.3)
        assert all(gap == pytest.approx(0.05) for gap in gaps(inside))
        assert after == pytest.approx([4.4])
        assert len(times) == 51
        assert min(gaps(list(times))) == pytest.approx(0.05)

    def test_the_stretch_is_clipped_to_the_panel_s_own_window(self) -> None:
        times = sample_times(0.5, 4.0, 10.0, Segment(4.0, 6.0, 20.0))

        assert [t for t in times if t >= 4.0 - 1e-9] == pytest.approx(
            [4.0 + 0.05 * n for n in range(10)]
        )

    @pytest.mark.parametrize(
        "segment",
        [None, Segment(3.3, 4.4, 10.0), Segment(3.3, 4.4, 0.0), Segment(5, 6, 20)],
        ids=["none", "not-faster", "off", "outside"],
    )
    def test_no_faster_stretch_changes_nothing(self, segment: Segment | None) -> None:
        assert sample_times(0.5, 4.0, 10.0, segment) == sample_times(0.5, 4.0, 10.0)

    def test_production_defaults_are_the_measured_recommendation(self) -> None:
        assert DEFAULTS.swing_capture_fps == 10.0
        assert DEFAULTS.swing_capture_downswing_fps == 20.0
        assert DEFAULTS.swing_capture_downswing_delay_seconds == 3.3
        assert DEFAULTS.swing_capture_downswing_duration_seconds == 1.1
        assert DEFAULTS.swing_capture_selected_frames == 16


# --- on a clock that rounds every sleep up -------------------------------------


class TestTheBurstOnAWindowsClock:
    async def test_absolute_deadlines_do_not_accumulate_sleep_overshoot(
        self, tmp_path
    ) -> None:
        """Waiting 100 ms after each still on this clock is 109 ms a still, and
        forty of them end 0.36 s late. Counted from the start, every still is
        within one tick of its deadline and the average is 100 ms."""

        capture = await Burst(tmp_path, downswing_fps=0.0).shot()

        for camera_id in BODY:
            times = offsets(capture, camera_id)
            assert len(times) == 40
            for index, time in enumerate(times):
                lateness = time - (0.5 + index * 0.1)
                assert -EARLY_SECONDS <= lateness <= TICK + 1e-9, (camera_id, index)
            assert mean(gaps(times)) == pytest.approx(0.1, abs=0.001)
            assert times[-1] < 4.4 + TICK + 1e-9

    async def test_the_fast_stretch_is_only_in_its_window(self, tmp_path) -> None:
        capture = await Burst(tmp_path).shot()

        for camera_id in BODY:
            times = offsets(capture, camera_id)
            before = [t for t in times if t < 3.3 - EARLY_SECONDS]
            inside = [
                t for t in times if 3.3 - EARLY_SECONDS <= t < 4.4 - EARLY_SECONDS
            ]
            assert len(times) == 51
            assert len(inside) == 22
            assert mean(gaps(inside)) == pytest.approx(0.05, abs=0.002)
            assert max(gaps(inside)) <= 0.05 + TICK + 1e-9
            assert mean(gaps(before)) == pytest.approx(0.1, abs=0.002)
            assert min(gaps(before)) >= 0.1 - TICK - 1e-9

    async def test_the_impact_panel_keeps_its_own_slow_rate(self, tmp_path) -> None:
        capture = await Burst(tmp_path).shot()

        times = offsets(capture, "impact")
        assert len(times) == 3
        assert times == pytest.approx([3.5, 4.0, 4.5], abs=TICK + 1e-9)

    async def test_one_cadence_line_per_body_camera_per_shot(
        self, tmp_path, caplog
    ) -> None:
        with caplog.at_level(logging.INFO, logger="caddiej.swing_capture"):
            capture = await Burst(tmp_path).shot()

        lines = [r for r in caplog.records if r.getMessage() == "swing_capture_cadence"]
        assert sorted(r.swing_capture_camera for r in lines) == list(BODY)  # type: ignore[attr-defined]
        for line in lines:
            fields = line.__dict__
            assert fields["swing_capture_fps"] == 10.0
            assert fields["swing_capture_downswing_fps"] == 20.0
            assert fields["swing_capture_downswing_achieved_fps"] == pytest.approx(
                20.0, abs=1.0
            )
            assert fields["swing_capture_planned_frames"] == 51
            assert fields["swing_capture_frames"] == 51
            assert fields["swing_capture_missed_deadlines"] == 0
            assert fields["swing_capture_overruns"] == 0
            for name in ("mean", "median", "min", "max"):
                assert fields[f"swing_capture_interval_{name}_ms"] is not None
        assert not [
            r for r in caplog.records if r.getMessage() == "swing_capture_frame"
        ]
        cadence = camera_metadata(capture, "swing_behind")["cadence"]
        assert cadence["requested_fps"] == 10.0
        assert cadence["downswing_fps"] == 20.0
        assert cadence["missed_deadlines"] == 0
        assert cadence["achieved_fps"] is not None

    async def test_a_stalled_request_skips_the_deadlines_it_missed(
        self, tmp_path
    ) -> None:
        """One request takes a third of a second. The two deadlines that
        passed entirely are dropped, the one just passed is asked for at
        once, and the schedule is back on its grid the tick after."""

        def stall(source: str, nth: int) -> float:
            return 0.33 if source == BEHIND and nth == 9 else 0.0

        burst = Burst(tmp_path, latency=stall, downswing_fps=0.0)
        capture = await burst.shot()

        times = offsets(capture, "swing_behind")
        stalled = times[9]
        assert stalled == pytest.approx(1.4, abs=TICK + 1e-9)
        assert len(times) == 40 - 2
        assert times[10] == pytest.approx(stalled + 0.33, abs=1e-6)
        assert times[11] == pytest.approx(1.8, abs=TICK + 1e-9)
        assert offsets(capture, "swing_side") == times
        cadence = camera_metadata(capture, "swing_behind")["cadence"]
        assert cadence["missed_deadlines"] == 2
        assert cadence["overruns"] == 1
        assert cadence["planned"] - cadence["requested"] == 2

    async def test_an_obs_slower_than_the_schedule_never_builds_a_backlog(
        self, tmp_path
    ) -> None:
        """Every screenshot takes 40 ms, so a tick of two body stills takes 80
        and the 50 ms downswing deadlines cannot all be met."""

        burst = Burst(tmp_path, latency=lambda _source, _nth: 0.04)
        capture = await burst.shot()

        body = [t for source, t in burst.obs.log if source in (BEHIND, SIDE)]
        assert body, "the burst still ran"
        started = min(t for _source, t in burst.obs.log)
        assert max(body) - started < 4.5, "the burst ended with its window"
        cadence = camera_metadata(capture, "swing_behind")["cadence"]
        assert cadence["missed_deadlines"] > 0
        assert cadence["requested"] + cadence["missed_deadlines"] == cadence["planned"]
        times = offsets(capture, "swing_behind")
        assert times == sorted(times)
        assert min(gaps(times)) >= 0.08 - 1e-6, "one tick at a time, never a burst"

    async def test_the_request_order_is_deterministic(self, tmp_path) -> None:
        first = Burst(tmp_path / "a")
        second = Burst(tmp_path / "b")
        await first.shot()
        await second.shot()

        def relative(log: list[tuple[str, float]]) -> list[tuple[str, float]]:
            start = log[0][1]
            return [(source, round(t - start, 6)) for source, t in log]

        assert relative(first.obs.log) == relative(second.obs.log)
        body = [source for source, _t in first.obs.log if source in (BEHIND, SIDE)]
        assert body == [BEHIND, SIDE] * (len(body) // 2)

    async def test_a_failing_source_is_isolated(self, tmp_path) -> None:
        burst = Burst(tmp_path, failing=frozenset({SIDE}))
        capture = await burst.shot()

        side = next(c for c in capture.cameras if c.camera_id == "swing_side")
        behind = next(c for c in capture.cameras if c.camera_id == "swing_behind")
        impact = next(c for c in capture.cameras if c.camera_id == "impact")
        assert not side.ok and not side.frames
        assert behind.ok and len(behind.frames) == 8
        assert impact.ok
        assert capture.metrics is not None and capture.metrics.ok
        cadence = camera_metadata(capture, "swing_behind")["cadence"]
        assert cadence["downswing_achieved_fps"] == pytest.approx(20.0, abs=1.0)


# --- nothing downstream moves ------------------------------------------------


async def both(tmp_path: Path) -> tuple[SwingCapture, SwingCapture]:
    slow = await Burst(tmp_path / "slow", downswing_fps=0.0).shot()
    fast = await Burst(tmp_path / "fast").shot()
    return slow, fast


class TestNothingDownstreamMoves:
    async def test_the_replay_start_is_found_on_the_same_still(self, tmp_path) -> None:
        slow, fast = await both(tmp_path)

        for camera_id in BODY:
            assert (
                camera_metadata(fast, camera_id)["replay_interval"]
                == camera_metadata(slow, camera_id)["replay_interval"]
            )

    async def test_the_burst_keeps_all_sixteen_stills_and_ten_a_second_drops_one(
        self, tmp_path
    ) -> None:
        """P5, P6 and P7 are aimed 0.05 s apart. With the burst each gets a
        still of its own; at ten a second one of them shares a still with its
        neighbour and is dropped rather than sent twice."""

        slow, fast = await both(tmp_path)

        assert [len(c.frames) for c in fast.cameras] == [8, 7, 1]
        for camera_id in BODY:
            view = next(c.view for c in fast.cameras if c.camera_id == camera_id)
            camera = next(c for c in fast.cameras if c.camera_id == camera_id)
            assert [f.phase for f in camera.frames] == [
                phase for phase, _seconds in PHASE_SECONDS[view]
            ]
            dropped = next(c for c in slow.cameras if c.camera_id == camera_id)
            assert len(dropped.frames) == len(camera.frames) - 1
            assert set(f.phase for f in dropped.frames) < set(
                f.phase for f in camera.frames
            )
            offsets = [f.offset_seconds for f in dropped.frames]
            assert len(set(offsets)) == len(offsets)

    async def test_the_downswing_stills_land_nearer_their_aims(self, tmp_path) -> None:
        slow, fast = await both(tmp_path)
        downswing = (
            SwingPhase.TRANSITION,
            SwingPhase.DOWNSWING,
            SwingPhase.IMPACT,
            SwingPhase.RELEASE,
        )

        for camera_id in BODY:
            view = next(c.view for c in fast.cameras if c.camera_id == camera_id)
            aims = dict(PHASE_SECONDS[view])
            start = camera_metadata(fast, camera_id)["replay_interval"]["start_seconds"]

            def error(capture: SwingCapture, phase: SwingPhase) -> float | None:
                camera = next(c for c in capture.cameras if c.camera_id == camera_id)
                frame = next((f for f in camera.frames if f.phase is phase), None)
                if frame is None:
                    return None
                return abs(frame.offset_seconds - (start + aims[phase]))  # noqa: B023

            for phase in downswing:
                fast_error = error(fast, phase)
                slow_error = error(slow, phase)
                assert fast_error is not None, phase
                assert fast_error <= 0.025 + TICK, phase
                if slow_error is not None:
                    assert fast_error <= slow_error + 1e-9, phase

    async def test_one_sheet_one_image_and_all_values_once(self, tmp_path) -> None:
        async def coached(burst: Burst) -> ScriptedProvider:
            provider = ScriptedProvider(replies=(a_reply(),))
            coach = Coach(provider, max_images=16)
            await coach.service.open()
            await coach.service.on_club_selected(ClubSelected(club=DRIVER))
            detected = ShotDetected(trigger_id="91", source="protee_log")
            await coach.service.on_shot_detected(detected)
            capture = await burst.strike(detected)
            assert capture is not None
            await coach.service.on_swing_frames(SwingFramesReady(capture=capture))
            await coach.service.aclose()
            return provider

        slow_burst = Burst(tmp_path / "slow", downswing_fps=0.0)
        fast_burst = Burst(tmp_path / "fast")
        slow = await coached(slow_burst)
        fast = await coached(fast_burst)

        assert slow.calls == fast.calls == 1
        assert len(fast.requests[0].images) == len(slow.requests[0].images) == 1
        cells = sheet_cells(fast.requests[0].images[0].data)
        slow_cells = sheet_cells(slow.requests[0].images[0].data)
        assert len(cells) == 17, "sixteen stills and All Values"
        assert len(slow_cells) == 15, "two downswing aims dropped at ten a second"
        assert set(slow_cells) < set(cells)
        fast_schema, slow_schema = fast.requests[0].schema, slow.requests[0].schema
        assert fast_schema.keys() == slow_schema.keys()
        assert fast_schema["properties"].keys() == slow_schema["properties"].keys()
        assert fast_schema["$defs"]["phase"] == slow_schema["$defs"]["phase"]
        for burst in (slow_burst, fast_burst):
            assert burst.obs.taken[ALL_VALUES] == 1
            assert burst.obs.taken[IMPACT] == 3

    async def test_the_newest_shot_still_wins(self, tmp_path) -> None:
        burst = Burst(tmp_path)

        await burst.service.on_shot_detected(
            ShotDetected(trigger_id="92", source="protee_log")
        )
        first = burst.service._task  # noqa: SLF001
        await asyncio.sleep(0)
        await burst.service.on_shot_detected(
            ShotDetected(trigger_id="93", source="protee_log")
        )
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.gather(burst.service._task, return_exceptions=True)  # noqa: SLF001

        assert [c.trigger_id for c in burst.captures()] == ["93"]
