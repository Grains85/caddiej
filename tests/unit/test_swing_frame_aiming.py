"""Which moments of a swing reach the model, and which cannot.

The first hardware session coached four shots and never once saw a transition,
a downswing or an impact. Not because the model was bad at looking at them:
because no picture of any of them was in the request. Three down-the-line
stills reached it -- the address, the top, and the player walking away from a
mat with no ball on it -- and it was asked to rate "where the club and the
trail shoulder go as the downswing starts" anyway. It answered `good`, at the
confidence ceiling, on all four.

Everything here is about the evidence rather than the coaching. A swing coach
that cannot be shown the swing is not going to be fixed by a better taxonomy,
so this is the layer that had to move first.

The facts these tests are built on were measured off a real capture, not
chosen: seconds after each camera's own replay start, read by eye off the
2026-10-07 shots (`phases` has the table). Two things follow and both are
tested below. The two replays do not start together, so each camera has to be
aimed on its own clock. And the window's end is wherever the capture stopped,
not a moment of the swing, so nothing is aimed by a fraction of it.
"""

from __future__ import annotations

import itertools

import pytest

from caddiej.domain.cues import PanelView
from caddiej.domain.swing_phase import PHASE_ORDER, SwingPhase
from caddiej.swing_capture.phases import (
    HIGH_DETAIL_PHASES,
    PHASE_SECONDS,
    aim_capacity,
    frame_detail,
    phase_plan,
)
from caddiej.swing_capture.select import (
    ActiveInterval,
    CapturedFrame,
    likely_blank,
    nearest,
    phase_targets,
    select,
    unlike_replay,
)

# A replay shaped like the ones the bay produces: a loading screen, then
# twenty-five frames of a swing at about nine per second.
REPLAY_BYTES = 200_000
LOADING_BYTES = 47_867
"""The size of the still that was actually sent. Forty times `likely_blank`'s
threshold, and a picture of an empty replay player."""


def a_still(index: int, size: int = REPLAY_BYTES) -> bytes:
    """A JPEG-shaped payload of a given size, distinct from its neighbours."""

    body = f"{index:06d}".encode()
    return b"\xff\xd8" + body + b"\x00" * max(0, size - len(body) - 4) + b"\xff\xd9"


def a_replay(
    start: float = 1.8, count: int = 25, step: float = 0.1095, loading: int = 16
) -> list[CapturedFrame]:
    """`loading` frames of a spinner, then a replay that changes every frame."""

    frames = [
        CapturedFrame(round(start - (loading - index) * step, 4), a_still(0, 1_000))
        for index in range(loading)
    ]
    frames.extend(
        CapturedFrame(round(start + index * step, 4), a_still(index + 1))
        for index in range(count)
    )
    return frames


# --- the calibration itself -------------------------------------------------


class TestTheCalibrationIsCoherent:
    def test_every_calibrated_view_is_in_swing_order_from_its_own_start(
        self,
    ) -> None:
        for view, table in PHASE_SECONDS.items():
            phases = [phase for phase, _seconds in table]
            order = [PHASE_ORDER.index(phase) for phase in phases]
            assert order == sorted(order), view
            seconds = [moment for _phase, moment in table]
            assert seconds == sorted(seconds), view
            assert len(set(seconds)) == len(seconds), view
            assert seconds[0] == 0.0, view

    def test_down_the_line_is_aimed_at_every_phase_from_setup_to_release(
        self,
    ) -> None:
        phases = [phase for phase, _seconds in PHASE_SECONDS[PanelView.BEHIND]]

        assert phases == [p for p in PHASE_ORDER if p is not SwingPhase.FINISH]

    def test_face_on_skips_only_the_takeaway(self) -> None:
        """Its only face-on-only checkpoint there is the hip movement, and the
        down-the-line takeaway shows the rest."""

        phases = [phase for phase, _seconds in PHASE_SECONDS[PanelView.SIDE]]

        assert phases == [
            p for p in PHASE_ORDER if p not in (SwingPhase.TAKEAWAY, SwingPhase.FINISH)
        ]

    def test_the_release_is_deliberately_aimed_at_in_both_views(self) -> None:
        for view, table in PHASE_SECONDS.items():
            aimed = dict(table)
            assert SwingPhase.RELEASE in aimed, view
            assert aimed[SwingPhase.RELEASE] > aimed[SwingPhase.IMPACT], view

    def test_face_on_is_aimed_at_the_measured_moments(self) -> None:
        """Read off the full-resolution face-on originals, seconds after the
        face-on replay's own start: setup and top from the 2026-10-07 shots,
        the backswing from the first three 2026-10-08 diagnostic swings, and
        P5 to P7 from the three 20 fps swings after them."""

        assert PHASE_SECONDS[PanelView.SIDE] == (
            (SwingPhase.SETUP, 0.00),
            (SwingPhase.BACKSWING, 0.88),
            (SwingPhase.TOP, 1.53),
            (SwingPhase.TRANSITION, 1.90),
            (SwingPhase.DOWNSWING, 1.95),
            (SwingPhase.IMPACT, 2.00),
            (SwingPhase.RELEASE, 2.08),
        )

    def test_down_the_line_is_aimed_at_the_measured_moments(self) -> None:
        assert PHASE_SECONDS[PanelView.BEHIND] == (
            (SwingPhase.SETUP, 0.00),
            (SwingPhase.TAKEAWAY, 0.60),
            (SwingPhase.BACKSWING, 0.88),
            (SwingPhase.TOP, 1.64),
            (SwingPhase.TRANSITION, 1.90),
            (SwingPhase.DOWNSWING, 1.95),
            (SwingPhase.IMPACT, 2.00),
            (SwingPhase.RELEASE, 2.08),
        )

    def test_the_downswing_aims_are_a_still_apart_at_twenty_a_second(
        self,
    ) -> None:
        """P5, P6 and P7 are 0.05 s apart, one 20 fps still each, and the
        release is past the last of them."""

        for view, table in PHASE_SECONDS.items():
            aimed = dict(table)
            downswing = [
                aimed[SwingPhase.TRANSITION],
                aimed[SwingPhase.DOWNSWING],
                aimed[SwingPhase.IMPACT],
            ]
            gaps = [later - earlier for earlier, later in itertools.pairwise(downswing)]
            assert gaps == [pytest.approx(0.05), pytest.approx(0.05)], view
            assert aimed[SwingPhase.RELEASE] - downswing[-1] >= 0.05, view

    def test_from_the_change_of_direction_both_views_share_one_timeline(
        self,
    ) -> None:
        """Measured from each replay's own start, both cameras showed the same
        moment of the downswing at the same time. The face-on downswing is no
        longer aimed at the down-the-line top."""

        behind = dict(PHASE_SECONDS[PanelView.BEHIND])
        side = dict(PHASE_SECONDS[PanelView.SIDE])

        for phase in (
            SwingPhase.TRANSITION,
            SwingPhase.DOWNSWING,
            SwingPhase.IMPACT,
            SwingPhase.RELEASE,
        ):
            assert side[phase] == pytest.approx(behind[phase]), phase
        assert side[SwingPhase.DOWNSWING] != pytest.approx(behind[SwingPhase.TOP])
        assert side[SwingPhase.TOP] < behind[SwingPhase.TOP]

    def test_on_a_twenty_fps_downswing_face_on_takes_one_still_per_position(
        self,
    ) -> None:
        """A face-on replay starting 1.912 s into the window, ten stills a
        second to 3.3 s and twenty a second after it: each downswing aim takes
        its own still, and none of them the top's."""

        start = 1.912
        offsets = [round(start + 0.1 * i, 3) for i in range(14)]
        offsets += [round(3.3 + 0.05 * i, 3) for i in range(23)]
        frames = [
            CapturedFrame(offset_seconds=offset, data=bytes([i]))
            for i, offset in enumerate(offsets)
        ]
        interval = ActiveInterval(start, frames[-1].offset_seconds, detected=True)

        aims = phase_targets(interval, PanelView.SIDE, 7)
        chosen = nearest(frames, [moment for _phase, moment in aims])
        taken = {
            phase: frames[index].offset_seconds
            for (phase, _moment), index in zip(aims, chosen, strict=True)
        }

        assert taken[SwingPhase.TOP] == pytest.approx(3.45, abs=0.002)
        assert taken[SwingPhase.TRANSITION] == pytest.approx(3.8, abs=0.002)
        assert taken[SwingPhase.DOWNSWING] == pytest.approx(3.85, abs=0.002)
        assert taken[SwingPhase.IMPACT] == pytest.approx(3.9, abs=0.002)
        assert taken[SwingPhase.RELEASE] == pytest.approx(4.0, abs=0.002)
        assert len(set(chosen)) == len(chosen)

    def test_a_camera_can_use_one_still_per_aimed_moment(self) -> None:
        assert aim_capacity(PanelView.BEHIND) == 8
        assert aim_capacity(PanelView.SIDE) == 7
        assert aim_capacity(PanelView.NONE) is None


class TestWhatASmallBudgetBuys:
    def test_a_budget_of_four_buys_the_phases_that_were_never_captured(self) -> None:
        plan = [phase for phase, _fraction in phase_plan(PanelView.BEHIND, 4)]

        assert SwingPhase.TRANSITION in plan
        assert SwingPhase.IMPACT in plan
        assert plan == [
            SwingPhase.SETUP,
            SwingPhase.TOP,
            SwingPhase.TRANSITION,
            SwingPhase.IMPACT,
        ]

    def test_six_buys_the_whole_downswing_and_the_release(self) -> None:
        plan = [phase for phase, _seconds in phase_plan(PanelView.SIDE, 6)]

        assert plan == [
            SwingPhase.SETUP,
            SwingPhase.TOP,
            SwingPhase.TRANSITION,
            SwingPhase.DOWNSWING,
            SwingPhase.IMPACT,
            SwingPhase.RELEASE,
        ]

    def test_the_full_budget_buys_every_calibrated_moment(self) -> None:
        for view, table in PHASE_SECONDS.items():
            assert phase_plan(view, 16) == table, view

    def test_a_plan_is_always_in_time_order(self) -> None:
        for view, table in PHASE_SECONDS.items():
            for count in range(1, 10):
                plan = [phase for phase, _seconds in phase_plan(view, count)]
                order = [PHASE_ORDER.index(phase) for phase in plan]
                assert order == sorted(order), (view, count)
                assert len(plan) == min(count, len(table))

    def test_a_panel_with_no_view_is_aimed_at_nothing(self) -> None:
        """And the caller spreads it evenly, which is right for a graphic.

        The impact panel is ProTee's crosshair diagram. It has no phases
        because it has no swing in it, and inventing some would make the label
        on a picture of a diagram say "transition"."""

        assert phase_plan(PanelView.NONE, 6) == ()


# --- aiming one camera ------------------------------------------------------


class TestAimingIsPerCamera:
    def test_each_phase_lands_its_time_after_this_camera_s_own_replay_start(
        self,
    ) -> None:
        interval = ActiveInterval(2.0, 4.5, detected=True)

        aimed = dict(phase_targets(interval, PanelView.BEHIND, 8))

        assert aimed[SwingPhase.SETUP] == pytest.approx(2.0)
        assert aimed[SwingPhase.TOP] == pytest.approx(3.64)
        assert aimed[SwingPhase.IMPACT] == pytest.approx(4.00)
        assert aimed[SwingPhase.RELEASE] == pytest.approx(4.08)

    def test_the_same_interval_is_aimed_differently_for_the_other_view(self) -> None:
        interval = ActiveInterval(2.0, 4.5, detected=True)

        behind = dict(phase_targets(interval, PanelView.BEHIND, 8))
        side = dict(phase_targets(interval, PanelView.SIDE, 8))

        assert behind[SwingPhase.TOP] != pytest.approx(side[SwingPhase.TOP])
        assert SwingPhase.TAKEAWAY in behind
        assert SwingPhase.TAKEAWAY not in side
        assert behind[SwingPhase.RELEASE] == pytest.approx(side[SwingPhase.RELEASE])

    def test_how_long_the_window_runs_does_not_move_a_moment(self) -> None:
        """What the fractions got wrong: the window's end is not the swing's.

        Both replays of the measured capture were still moving when the window
        closed, so a fraction of the interval was a fraction of a setting."""

        short = dict(
            phase_targets(ActiveInterval(2.0, 4.5, detected=True), PanelView.SIDE, 7)
        )
        long = dict(
            phase_targets(ActiveInterval(2.0, 5.5, detected=True), PanelView.SIDE, 7)
        )

        assert short == pytest.approx(long)

    def test_a_moment_the_window_closed_before_is_dropped_not_moved(self) -> None:
        """A release the replay had not reached is not replaced by the impact
        still, or by anything else: a picture of the wrong moment is worse than
        none."""

        interval = ActiveInterval(2.0, 4.0, detected=True)

        aimed = dict(phase_targets(interval, PanelView.BEHIND, 8))

        assert SwingPhase.RELEASE not in aimed
        assert SwingPhase.IMPACT in aimed

    def test_an_interval_with_no_span_is_aimed_at_nothing(self) -> None:
        interval = ActiveInterval(2.0, 2.0, detected=False)

        assert phase_targets(interval, PanelView.BEHIND, 6) == ()


class TestTheBlankFrameTheSessionSent:
    def test_a_loading_screen_is_far_too_large_for_the_absolute_floor(self) -> None:
        """Which is why it was sent. `likely_blank` could not see it."""

        assert likely_blank(a_still(0, LOADING_BYTES)) is False

    def test_it_is_caught_by_comparison_with_the_rest_of_the_window(self) -> None:
        frames = a_replay(loading=0)
        frames.insert(0, CapturedFrame(1.6, a_still(99, LOADING_BYTES)))

        assert unlike_replay(frames, 0) is True
        assert unlike_replay(frames, 8) is False

    def test_a_window_that_is_all_loading_screens_is_not_all_rejected(self) -> None:
        """A live view, or a panel that never drew a replay.

        There is no replay for these to be unlike, so the size test has nothing
        to say, and refusing the whole camera over it would lose a capture the
        old code handled.
        """

        frames = [
            CapturedFrame(index * 0.1, a_still(index, LOADING_BYTES))
            for index in range(20)
        ]

        chosen, _intervals, _combined, _aimed = select({"a": frames}, 4)

        assert chosen["a"] != ()

    def test_the_other_camera_s_earlier_start_cannot_pull_it_back_in(self) -> None:
        """The precise mechanism that sent 47 KB of grey to the model.

        The face-on camera detected its own replay starting after its loading
        frame, correctly. The selector then asked every camera for the same
        instants, taken from the *earliest* start across cameras -- the
        down-the-line one -- and the nearest face-on still to that instant was
        the loading frame it had just excluded. Each camera's own interval was
        computed and thrown away.
        """

        behind = a_replay(start=1.813)
        side = a_replay(start=1.938)
        blank_at = 1.813
        side.insert(0, CapturedFrame(blank_at, a_still(99, LOADING_BYTES)))

        chosen, intervals, combined, _aimed = select(
            {"behind": behind, "side": side},
            6,
            {"behind": PanelView.BEHIND, "side": PanelView.SIDE},
        )

        # The combined interval still reaches back to the earlier camera, and
        # is still reported, because the metadata records it.
        assert combined.start_seconds == pytest.approx(1.813)
        assert intervals["side"].start_seconds > combined.start_seconds
        kept = [side[index].offset_seconds for index in chosen["side"]]
        assert blank_at not in kept
        assert min(kept) >= intervals["side"].start_seconds


class TestNothingIsAimedAtThePlayerWalkingAway:
    def test_the_last_still_is_inside_the_replay_not_at_the_end_of_it(self) -> None:
        frames = a_replay()

        chosen, intervals, _combined, _aimed = select(
            {"behind": frames}, 6, {"behind": PanelView.BEHIND}
        )

        kept = [frames[index].offset_seconds for index in chosen["behind"]]
        interval = intervals["behind"]
        release = dict(PHASE_SECONDS[PanelView.BEHIND])[SwingPhase.RELEASE]
        assert max(kept) < interval.end_seconds
        assert max(kept) <= interval.start_seconds + release + 0.06


class TestEachStillKnowsWhatItWasAimedAt:
    def test_the_phases_come_back_beside_the_frames_in_the_same_order(self) -> None:
        frames = a_replay()

        chosen, _intervals, _combined, aimed = select(
            {"behind": frames}, 6, {"behind": PanelView.BEHIND}
        )

        assert len(aimed["behind"]) == len(chosen["behind"])
        order = [PHASE_ORDER.index(phase) for phase in aimed["behind"]]
        assert order == sorted(order)
        assert aimed["behind"][0] is SwingPhase.SETUP
        assert aimed["behind"][-1] is SwingPhase.RELEASE

    def test_a_dropped_moment_does_not_shift_every_label_after_it(self) -> None:
        """`nearest` keeps one still per picture, not one per moment.

        A camera with fewer distinct stills than moments returns fewer frames,
        and pairing the two lists by position would then announce the top as
        the transition for the rest of the swing. A still labelled with the
        wrong phase is worse than one labelled with nothing, because the model
        is asked to rate the phase it is told about.
        """

        frames = a_replay(count=3, step=0.5, loading=2)

        chosen, _intervals, _combined, aimed = select(
            {"behind": frames}, 8, {"behind": PanelView.BEHIND}
        )

        assert len(aimed["behind"]) == len(chosen["behind"]) < 8
        for index, phase in zip(chosen["behind"], aimed["behind"], strict=True):
            offset = frames[index].offset_seconds
            plan = dict(phase_targets(_intervals["behind"], PanelView.BEHIND, 8))
            nearest_phase = min(plan, key=lambda p: abs(plan[p] - offset))
            assert phase is nearest_phase

    def test_a_body_camera_whose_replay_start_was_not_found_keeps_nothing(
        self,
    ) -> None:
        """No start, no clock: seconds after it would be counted from the
        capture's first request, and an even spread across the window is how
        a spinner reached the coach. Nothing is better than that."""

        frames = a_replay(loading=0)

        chosen, intervals, _combined, aimed = select(
            {"behind": frames}, 6, {"behind": PanelView.BEHIND}
        )

        assert intervals["behind"].detected is False
        assert chosen["behind"] == ()
        assert aimed["behind"] == ()

    def test_a_panel_that_is_not_a_body_camera_is_still_spread_evenly(
        self,
    ) -> None:
        frames = a_replay(loading=0)

        chosen, _intervals, _combined, aimed = select({"other": frames}, 6)

        assert len(chosen["other"]) == 6
        assert aimed["other"] == ()

    def test_a_panel_with_no_calibration_reports_no_phases(self) -> None:
        frames = a_replay()

        _chosen, _intervals, _combined, aimed = select({"impact": frames}, 4)

        assert aimed["impact"] == ()


# --- what it costs ----------------------------------------------------------


class TestTheMomentsThatNeedPixelsGetThem:
    def test_the_downswing_moments_are_promoted_to_full_detail(self) -> None:
        """Swing stills go at `low`, which is about 512 square.

        The second half of why a swing plane could not be read, and the half no
        amount of re-aiming a camera would have fixed: a 1920-wide panel
        squeezed into 512 pixels leaves a club shaft two or three pixels across.
        """

        for phase in HIGH_DETAIL_PHASES:
            assert frame_detail(phase, "low") == "high"

    def test_the_phases_that_survive_a_thumbnail_are_left_alone(self) -> None:
        for phase in (SwingPhase.SETUP, SwingPhase.TAKEAWAY, SwingPhase.FINISH):
            assert frame_detail(phase, "low") == "low"

    def test_an_unaimed_still_takes_the_request_s_own_setting(self) -> None:
        assert frame_detail(None, "low") == "low"
        assert frame_detail(None, "auto") == "auto"

    def test_a_request_already_asking_for_more_is_not_talked_down(self) -> None:
        assert frame_detail(SwingPhase.SETUP, "high") == "high"
