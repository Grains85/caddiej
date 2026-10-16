"""Where each body replay starts, on the three diagnostic swings of 2026-10-08.

The fixtures are the encoded sizes and request offsets every screenshot had,
copied from those shots' `diagnostic_timeline`, and the first full replay
still read by eye off the full-resolution originals. The pictures themselves
are not needed: the selector reads nothing else.

Shots 2 and 3 are the failure. ProTee fades its replay in under the spinner,
so the down-the-line still before the cut is a crossfade, and on that dark
picture it compressed to more than three quarters of a replay still. A level
measured against the whole window therefore began on the crossfade, found no
step there, and the camera was spread evenly across a window that began on
the spinner. The face-on spinner is a quarter of a replay still and never came
close, and on every shot both cameras cut on the same capture tick.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import pytest
from tests.unit.test_swing_capture_evidence import (
    BEHIND,
    ReplayObs,
    Rig,
    a_picture,
)

from caddiej.domain.cues import PanelView
from caddiej.domain.swing_phase import SwingPhase
from caddiej.swing_capture.phases import phase_plan
from caddiej.swing_capture.select import (
    CapturedFrame,
    active_interval,
    package_quota,
    replay_start,
    select,
)
from caddiej.swing_capture.store import METADATA_NAME

SHOT_1_OFFSETS = (
    0.5, 0.609, 0.718, 0.828, 0.937, 1.046, 1.156, 1.281, 1.39, 1.5, 1.609,
    1.718, 1.828, 1.937, 2.062, 2.171, 2.281, 2.39, 2.5, 2.609, 2.718, 2.828,
    2.937, 3.046, 3.156, 3.265, 3.375, 3.484, 3.593, 3.703, 3.812, 3.921,
    4.031, 4.14, 4.25, 4.359, 4.468,
)  # fmt: skip
SHOT_1_BEHIND = (
    40820, 37109, 36240, 35768, 36532, 37392, 38232, 38787, 40001, 40217,
    39979, 39235, 38005, 41483, 57785, 56738, 57466, 58341, 58453, 58475,
    58589, 57625, 57102, 57713, 58305, 58228, 57542, 58287, 58364, 57688,
    58205, 57918, 57704, 55712, 56115, 56910, 56695,
)  # fmt: skip
SHOT_1_SIDE = (
    87949, 43557, 42607, 42096, 42882, 43725, 44519, 45135, 46256, 46496,
    46285, 45548, 44354, 43371, 174672, 171510, 171848, 175730, 174091,
    169150, 168653, 168910, 169371, 168632, 170895, 170907, 170724, 174640,
    174481, 174739, 171338, 170800, 168863, 169515, 169787, 169284, 168768,
)  # fmt: skip

SHOT_2_OFFSETS = (
    0.5, 0.609, 0.718, 0.828, 0.937, 1.047, 1.156, 1.265, 1.375, 1.484, 1.593,
    1.703, 1.812, 1.922, 2.031, 2.14, 2.25, 2.359, 2.468, 2.578, 2.687, 2.797,
    2.906, 3.015, 3.125, 3.234, 3.343, 3.453, 3.562, 3.672, 3.781, 3.89, 4.0,
    4.109, 4.218, 4.328, 4.437,
)  # fmt: skip
SHOT_2_BEHIND = (
    42486, 40993, 40126, 39163, 38381, 38576, 39407, 40336, 41216, 42021,
    42746, 42727, 43405, 56931, 56870, 57129, 56361, 57452, 57647, 57663,
    58197, 57450, 57178, 58819, 59076, 58215, 58548, 57641, 58262, 57919,
    57966, 57848, 54949, 57067, 57578, 58147, 58276,
)  # fmt: skip
SHOT_2_SIDE = (
    90690, 88909, 87957, 87124, 86866, 86178, 87191, 88078, 89045, 89788,
    90553, 90829, 91054, 171971, 173337, 172011, 171903, 171555, 171069,
    169908, 169919, 169642, 169355, 173088, 172252, 172467, 172492, 170937,
    171270, 171396, 171720, 172610, 169011, 169846, 170301, 172614, 171262,
)  # fmt: skip

SHOT_3_OFFSETS = (
    0.516, 0.625, 0.734, 0.844, 0.953, 1.063, 1.172, 1.281, 1.391, 1.5, 1.609,
    1.719, 1.828, 1.938, 2.047, 2.156, 2.266, 2.375, 2.484, 2.594, 2.703,
    2.813, 2.922, 3.031, 3.141, 3.25, 3.359, 3.469, 3.578, 3.688, 3.797,
    3.906, 4.016, 4.125, 4.234, 4.344, 4.453,
)  # fmt: skip
SHOT_3_BEHIND = (
    43234, 39655, 38439, 37605, 36911, 36169, 35741, 36656, 37661, 38543,
    39041, 39930, 44442, 56721, 56778, 56802, 56647, 57396, 57605, 57852,
    57134, 57781, 57633, 57869, 58315, 58452, 57697, 57967, 57206, 57076,
    57663, 55639, 55143, 57083, 57787, 57895, 57692,
)  # fmt: skip
SHOT_3_SIDE = (
    89945, 45808, 44648, 43724, 43097, 42187, 41790, 42728, 43693, 44515,
    45119, 46060, 46287, 171071, 171265, 171057, 171954, 171621, 172630,
    170000, 170904, 169456, 172762, 172961, 171848, 171127, 171509, 171267,
    171804, 172351, 171203, 172274, 168628, 169611, 169933, 170364, 169815,
)  # fmt: skip


@dataclass(frozen=True)
class Timeline:
    name: str
    offsets: tuple[float, ...]
    sizes: tuple[int, ...]
    first_replay_frame: int
    """1-based, as the diagnostic files are numbered."""

    @property
    def index(self) -> int:
        return self.first_replay_frame - 1

    def frames(self, tag: str = "") -> list[CapturedFrame]:
        return [
            CapturedFrame(offset, a_payload(size, f"{tag}{self.name}{position}"))
            for position, (offset, size) in enumerate(
                zip(self.offsets, self.sizes, strict=True)
            )
        ]


def a_payload(length: int, marker: str) -> bytes:
    head = b"\xff\xd8" + marker.encode()
    return head + b"\x00" * max(0, length - len(head))


SEQUENCES = (
    Timeline("shot1_behind", SHOT_1_OFFSETS, SHOT_1_BEHIND, 15),
    Timeline("shot1_side", SHOT_1_OFFSETS, SHOT_1_SIDE, 15),
    Timeline("shot2_behind", SHOT_2_OFFSETS, SHOT_2_BEHIND, 14),
    Timeline("shot2_side", SHOT_2_OFFSETS, SHOT_2_SIDE, 14),
    Timeline("shot3_behind", SHOT_3_OFFSETS, SHOT_3_BEHIND, 14),
    Timeline("shot3_side", SHOT_3_OFFSETS, SHOT_3_SIDE, 14),
)
BY_NAME = {Timeline.name: Timeline for Timeline in SEQUENCES}
SHOTS = (1, 2, 3)
VIEWS = {"swing_behind": PanelView.BEHIND, "swing_side": PanelView.SIDE}
FULL_BUDGET = {"swing_behind": 8, "swing_side": 7}


def shot(number: int) -> tuple[Timeline, Timeline]:
    return BY_NAME[f"shot{number}_behind"], BY_NAME[f"shot{number}_side"]


def without_a_cut(Timeline: Timeline) -> list[CapturedFrame]:
    """The same window with the loading stills as big as the replay ones, so
    its start is not findable from this camera alone."""

    level = int(median(Timeline.sizes[Timeline.index :]))
    return [
        CapturedFrame(
            frame.offset_seconds,
            frame.data
            if position >= Timeline.index
            else a_payload(level + 7 * position, f"flat{Timeline.name}{position}"),
        )
        for position, frame in enumerate(Timeline.frames())
    ]


# --- the detector on all six real sequences ---------------------------------


class TestTheSixRealSequences:
    @pytest.mark.parametrize("Timeline", SEQUENCES, ids=lambda s: s.name)
    def test_the_first_full_replay_still_is_found(self, Timeline: Timeline) -> None:
        frames = Timeline.frames()

        interval = active_interval(frames)

        assert replay_start(frames) == Timeline.index
        assert interval.detected is True
        assert interval.start_seconds == pytest.approx(Timeline.offsets[Timeline.index])
        assert interval.pre_replay_frames == Timeline.index
        assert interval.borrowed_from is None

    @pytest.mark.parametrize(
        "name", ["shot2_behind", "shot3_behind"], ids=["shot2", "shot3"]
    )
    def test_the_crossfade_before_the_cut_cleared_the_old_window_level(
        self, name: str
    ) -> None:
        """Why the old rule failed here: the still before the cut was above
        three quarters of the window median, so a window-wide level began on
        it, and the step into it was not a step at all."""

        Timeline = BY_NAME[name]
        crossfade = Timeline.sizes[Timeline.index - 1]

        assert crossfade >= median(Timeline.sizes) * 0.75
        assert crossfade / Timeline.sizes[Timeline.index - 2] < 1.2
        assert replay_start(Timeline.frames()) == Timeline.index

    @pytest.mark.parametrize("number", SHOTS)
    def test_both_cameras_cut_on_the_same_capture_tick(self, number: int) -> None:
        behind, side = shot(number)

        assert replay_start(behind.frames()) == replay_start(side.frames())


# --- the detector, generically ---------------------------------------------


class TestTheRuleNotTheNumbers:
    def test_the_largest_held_step_wins_over_an_earlier_smaller_one(self) -> None:
        sizes = [40_000] * 8 + [42_000, 44_000] + [57_000, 57_500, 56_800] * 5
        frames = [
            CapturedFrame(index * 0.1, a_payload(size, str(index)))
            for index, size in enumerate(sizes)
        ]

        assert replay_start(frames) == 10

    def test_a_step_into_a_level_that_does_not_hold_is_refused(self) -> None:
        sizes = [40_000] * 8 + [57_000] * 10 + [45_000, 57_000, 57_000]
        frames = [
            CapturedFrame(index * 0.1, a_payload(size, str(index)))
            for index, size in enumerate(sizes)
        ]

        assert replay_start(frames) == 0

    def test_a_crossfade_that_is_most_of_the_way_there_is_not_the_start(
        self,
    ) -> None:
        """The shot 2 shape with every size scaled: the rule is ratios."""

        Timeline = BY_NAME["shot2_behind"]
        scaled = [
            CapturedFrame(offset, a_payload(size * 3, f"x{index}"))
            for index, (offset, size) in enumerate(
                zip(Timeline.offsets, Timeline.sizes, strict=True)
            )
        ]

        assert replay_start(scaled) == Timeline.index


# --- what is selected from them --------------------------------------------


class TestNothingBeforeTheStartIsKept:
    @pytest.mark.parametrize("number", SHOTS)
    def test_every_kept_still_is_at_or_after_its_camera_s_replay_start(
        self, number: int
    ) -> None:
        behind, side = shot(number)
        tracks = {"swing_behind": behind.frames(), "swing_side": side.frames()}

        chosen, intervals, _combined, aimed = select(tracks, FULL_BUDGET, VIEWS)

        for name, Timeline in (("swing_behind", behind), ("swing_side", side)):
            assert chosen[name], name
            assert min(chosen[name]) == Timeline.index, name
            assert aimed[name][0] is SwingPhase.SETUP, name
            assert intervals[name].start_seconds == pytest.approx(
                Timeline.offsets[Timeline.index]
            )

    @pytest.mark.parametrize("number", SHOTS)
    def test_the_full_budget_buys_the_backswing_on_both_cameras(
        self, number: int
    ) -> None:
        behind, side = shot(number)
        tracks = {"swing_behind": behind.frames(), "swing_side": side.frames()}

        _chosen, _intervals, _combined, aimed = select(tracks, FULL_BUDGET, VIEWS)

        assert SwingPhase.TAKEAWAY in aimed["swing_behind"]
        assert SwingPhase.BACKSWING in aimed["swing_behind"]
        assert SwingPhase.BACKSWING in aimed["swing_side"]
        assert SwingPhase.TAKEAWAY not in aimed["swing_side"]


class TestTheFrameBudget:
    PANELS = (
        ("swing_behind", False, 8),
        ("swing_side", False, 7),
        ("impact", True, 1),
    )

    def test_sixteen_stills_aim_every_calibrated_moment(self) -> None:
        quota = package_quota(16, self.PANELS)

        assert quota == {"swing_behind": 8, "swing_side": 7, "impact": 1}
        behind = [p for p, _s in phase_plan(PanelView.BEHIND, quota["swing_behind"])]
        side = [p for p, _s in phase_plan(PanelView.SIDE, quota["swing_side"])]
        assert SwingPhase.TAKEAWAY in behind
        assert SwingPhase.BACKSWING in behind
        assert SwingPhase.BACKSWING in side

    def test_thirteen_left_no_room_for_the_backswing(self) -> None:
        """The local override the diagnostic swings ran on."""

        quota = package_quota(13, self.PANELS)

        assert quota == {"swing_behind": 6, "swing_side": 6, "impact": 1}
        for view in (PanelView.BEHIND, PanelView.SIDE):
            phases = [p for p, _s in phase_plan(view, 6)]
            assert SwingPhase.BACKSWING not in phases, view
            assert SwingPhase.TAKEAWAY not in phases, view


# --- one body camera lends its start to the other ---------------------------


class TestTheOtherCameraLendsItsStart:
    @pytest.mark.parametrize("number", SHOTS)
    def test_behind_without_a_cut_is_timed_from_side(self, number: int) -> None:
        behind, side = shot(number)
        tracks = {"swing_behind": without_a_cut(behind), "swing_side": side.frames()}
        assert replay_start(tracks["swing_behind"]) == 0

        chosen, intervals, _combined, aimed = select(tracks, FULL_BUDGET, VIEWS)

        lent = intervals["swing_behind"]
        assert lent.borrowed_from == "swing_side"
        assert lent.detected is True
        assert lent.start_seconds == pytest.approx(side.offsets[side.index])
        assert lent.pre_replay_frames == side.index
        assert min(chosen["swing_behind"]) == behind.index
        assert aimed["swing_behind"][0] is SwingPhase.SETUP
        own = {"swing_behind": behind.frames(), "swing_side": side.frames()}
        _own_chosen, _own_intervals, _own_combined, own_aimed = select(
            own, FULL_BUDGET, VIEWS
        )
        assert aimed["swing_behind"] == own_aimed["swing_behind"]
        assert intervals["swing_side"].borrowed_from is None

    def test_side_without_a_cut_is_timed_from_behind(self) -> None:
        behind, side = shot(2)
        tracks = {"swing_behind": behind.frames(), "swing_side": without_a_cut(side)}

        chosen, intervals, _combined, aimed = select(tracks, FULL_BUDGET, VIEWS)

        assert intervals["swing_side"].borrowed_from == "swing_behind"
        assert min(chosen["swing_side"]) == side.index
        assert aimed["swing_side"][0] is SwingPhase.SETUP

    def test_two_found_starts_are_each_their_own(self) -> None:
        behind, side = shot(1)
        tracks = {"swing_behind": behind.frames(), "swing_side": side.frames()}

        _chosen, intervals, _combined, _aimed = select(tracks, FULL_BUDGET, VIEWS)

        assert intervals["swing_behind"].borrowed_from is None
        assert intervals["swing_side"].borrowed_from is None

    def test_a_panel_that_is_not_a_body_camera_lends_nothing(self) -> None:
        """Impact's clock is not a replay's: it is never borrowed."""

        behind, side = shot(2)
        tracks = {
            "swing_behind": without_a_cut(behind),
            "impact": side.frames("impact"),
        }

        chosen, intervals, _combined, aimed = select(
            tracks,
            {"swing_behind": 8, "impact": 1},
            {"swing_behind": PanelView.BEHIND, "impact": PanelView.NONE},
        )

        assert intervals["swing_behind"].borrowed_from is None
        assert chosen["swing_behind"] == ()
        assert aimed["swing_behind"] == ()


# --- neither found: nothing invented ----------------------------------------


class TestNeitherCameraFoundItsStart:
    @pytest.mark.parametrize("number", SHOTS)
    def test_neither_body_camera_keeps_a_still(self, number: int) -> None:
        behind, side = shot(number)
        tracks = {
            "swing_behind": without_a_cut(behind),
            "swing_side": without_a_cut(side),
        }

        chosen, intervals, combined, aimed = select(tracks, FULL_BUDGET, VIEWS)

        for name in tracks:
            assert chosen[name] == (), name
            assert aimed[name] == (), name
            assert intervals[name].detected is False, name
            assert intervals[name].borrowed_from is None, name
        assert combined.detected is False


# --- and in the capture service ---------------------------------------------


class BehindNeverCuts(ReplayObs):
    """The behind panel opens straight on a moving replay, as if its loading
    screen had been the size of the replay: no start of its own to find."""

    async def source_screenshot(
        self, source_name: str, *, width: int | None = None
    ) -> bytes:
        if source_name != BEHIND:
            return await super().source_screenshot(source_name, width=width)
        count = self.taken.get(source_name, 0)
        self.taken[source_name] = count + 1
        data = a_picture(source_name, count, 1_200)
        self.returned.setdefault(source_name, []).append(data)
        return data


def metadata(capture_directory: Path) -> dict:
    return json.loads((capture_directory / METADATA_NAME).read_text(encoding="utf-8"))


class TestTheServiceSaysWhatItDid:
    async def test_a_borrowed_start_is_logged_and_written_down(
        self, tmp_path, caplog
    ) -> None:
        rig = Rig(tmp_path, BehindNeverCuts())

        with caplog.at_level(logging.WARNING, logger="caddiej.swing_capture"):
            capture = await rig.shot("41")

        assert capture is not None
        borrowed = [
            record
            for record in caplog.records
            if record.getMessage() == "swing_capture_replay_start_borrowed"
        ]
        assert len(borrowed) == 1
        assert borrowed[0].swing_capture_camera == "swing_behind"  # type: ignore[attr-defined]
        assert "swing_side" in borrowed[0].swing_capture_detail  # type: ignore[attr-defined]
        payload = metadata(capture.directory)
        cameras = {camera["camera_id"]: camera for camera in payload["cameras"]}
        behind = cameras["swing_behind"]
        assert behind["replay_interval"]["start_borrowed_from"] == "swing_side"
        assert cameras["swing_side"]["replay_interval"]["start_borrowed_from"] is None
        start = behind["replay_interval"]["start_seconds"]
        assert start == cameras["swing_side"]["replay_interval"]["start_seconds"]
        assert behind["frames"]
        assert all(frame["offset_seconds"] >= start for frame in behind["frames"])
        assert behind["frames"][0]["phase"] == SwingPhase.SETUP.value

    async def test_with_no_start_anywhere_the_body_is_missing_not_invented(
        self, tmp_path, caplog
    ) -> None:
        rig = Rig(tmp_path, ReplayObs(loading=0))

        with caplog.at_level(logging.WARNING, logger="caddiej.swing_capture"):
            capture = await rig.shot("42")

        assert capture is not None, "the impact still is still published"
        body = [c for c in capture.cameras if c.camera_id != "impact"]
        assert all(not camera.frames for camera in body)
        assert all(not camera.ok for camera in body)
        assert all("replay start not found" in (c.detail or "") for c in body)
        impact = next(c for c in capture.cameras if c.camera_id == "impact")
        assert impact.ok and len(impact.frames) == 1
        assert capture.metrics is not None and capture.metrics.ok
        messages = [record.getMessage() for record in caplog.records]
        assert messages.count("swing_capture_replay_start_not_found") == 2
        assert "swing_capture_replay_start_borrowed" not in messages
