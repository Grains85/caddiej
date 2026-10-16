"""Which still each downswing aim takes, on the three 20 fps swings of 2026-10-08.

The fixtures are the request offsets and encoded sizes every screenshot had,
copied from those shots' `diagnostic_timeline`. Where ProTee handed back the
same picture twice the size repeats, and so does the payload here. What each
still showed was read by eye off the full-resolution originals, the same
still in both views:

    still 39  lead arm parallel (P5)
    still 40  shaft parallel (P6)
    still 41  impact (P7)
    still 43  shaft parallel after impact (P8) on shots 2 and 3, and just past
              it on shot 1, whose P8 is still 42

The aims they replaced, 1.75, 1.86 and 1.97 s, took stills 36, 38 and 40 on
every one of them: the top again, the hands at the shoulder, and the delivery
with the ball still on the tee, labelled transition, downswing and impact.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from caddiej.domain.cues import PanelView
from caddiej.domain.swing_phase import SwingPhase
from caddiej.swing_capture.phases import PHASE_SECONDS
from caddiej.swing_capture.select import CapturedFrame, nearest, replay_start, select

SHOT_1_OFFSETS = (
    0.5, 0.611, 0.69, 0.801, 0.91, 1.004, 1.116, 1.211, 1.307, 1.403, 1.499,
    1.611, 1.707, 1.803, 1.912, 2.008, 2.102, 2.213, 2.306, 2.402, 2.514,
    2.608, 2.704, 2.815, 2.894, 3.006, 3.114, 3.209, 3.303, 3.349, 3.413,
    3.435, 3.508, 3.556, 3.602, 3.65, 3.712, 3.758, 3.805, 3.852, 3.915,
    3.962, 4.009, 4.055, 4.103, 4.15, 4.214, 4.262, 4.31, 4.335, 4.405,
)  # fmt: skip
SHOT_1_BEHIND = (
    39466, 38614, 37907, 37072, 36231, 35534, 36763, 37389, 38156, 38866,
    39600, 39778, 44298, 43705, 58459, 58430, 58785, 58747, 58777, 58799,
    59932, 59406, 59765, 59831, 59542, 59881, 60080, 59869, 60130, 60611,
    60476, 60476, 60262, 60516, 60397, 59833, 60179, 60212, 61084, 59545,
    57601, 57255, 57439, 58293, 59280, 59772, 59445, 59208, 59792, 59792,
    59472,
)  # fmt: skip
SHOT_1_SIDE = (
    45713, 44478, 44077, 43162, 41683, 41632, 42875, 43490, 44191, 44995,
    45707, 46421, 46591, 45760, 184642, 186493, 186780, 184088, 183909,
    183834, 184552, 185943, 182192, 182266, 184121, 186380, 183419, 185671,
    183039, 183224, 184293, 184293, 186037, 183828, 186093, 183707, 182911,
    182493, 183207, 184305, 183711, 181319, 182880, 181358, 182541, 181221,
    184737, 180844, 184532, 184532, 182052,
)  # fmt: skip

SHOT_2_OFFSETS = (
    0.509, 0.604, 0.713, 0.807, 0.902, 1.013, 1.108, 1.201, 1.31, 1.405, 1.5,
    1.61, 1.704, 1.815, 1.911, 2.006, 2.115, 2.21, 2.305, 2.414, 2.507,
    2.601, 2.712, 2.808, 2.903, 2.996, 3.107, 3.201, 3.328, 3.351, 3.408,
    3.456, 3.504, 3.551, 3.614, 3.661, 3.708, 3.755, 3.802, 3.864, 3.912,
    3.961, 4.008, 4.055, 4.103, 4.151, 4.215, 4.235, 4.309, 4.355, 4.403,
)  # fmt: skip
SHOT_2_BEHIND = (
    40529, 36104, 37076, 37841, 38358, 38956, 39840, 40206, 40077, 39221,
    38510, 37552, 36742, 40085, 59811, 58110, 58322, 58460, 58496, 59186,
    58821, 59471, 59925, 59135, 59837, 60379, 59950, 60090, 60234, 60234,
    59903, 59983, 59899, 59915, 59848, 60192, 60043, 59783, 60568, 59830,
    59696, 57690, 57009, 57765, 59318, 58975, 59594, 59102, 59922, 59420,
    59543,
)  # fmt: skip
SHOT_2_SIDE = (
    91980, 42174, 43083, 43934, 44404, 44975, 45930, 46239, 46144, 45244,
    44486, 43635, 42659, 41807, 186320, 183654, 183639, 183878, 183985,
    185449, 184492, 182519, 182961, 182112, 183418, 183123, 183641, 183357,
    183418, 183418, 183130, 183357, 183654, 183176, 183937, 184474, 184213,
    183831, 183170, 182793, 183622, 182349, 181323, 183374, 182647, 181952,
    182884, 183961, 182684, 182552, 182528,
)  # fmt: skip

SHOT_3_OFFSETS = (
    0.522, 0.601, 0.713, 0.807, 0.902, 1.015, 1.11, 1.205, 1.301, 1.411,
    1.49, 1.602, 1.713, 1.81, 1.905, 2.001, 2.113, 2.209, 2.305, 2.401,
    2.498, 2.609, 2.705, 2.801, 2.897, 3.007, 3.102, 3.213, 3.307, 3.355,
    3.403, 3.451, 3.498, 3.56, 3.607, 3.655, 3.704, 3.751, 3.814, 3.863,
    3.911, 3.959, 4.005, 4.035, 4.1, 4.162, 4.184, 4.257, 4.305, 4.353, 4.401,
)  # fmt: skip
SHOT_3_BEHIND = (
    42434, 39017, 39784, 40263, 40246, 39379, 38632, 38035, 37114, 36117,
    35540, 36180, 37169, 42494, 59411, 58109, 58357, 58369, 58878, 59253,
    58570, 60099, 58993, 59642, 59608, 59454, 59712, 59931, 60514, 60869,
    60197, 60334, 60283, 60463, 59733, 60385, 59530, 59530, 60202, 60982,
    59180, 58610, 56939, 56939, 57695, 59091, 59469, 59571, 59729, 59992,
    59459,
)  # fmt: skip
SHOT_3_SIDE = (
    92072, 44998, 45998, 46321, 46333, 45533, 44742, 44151, 43204, 42162,
    41618, 42372, 43361, 44403, 188046, 184783, 188373, 188516, 189510,
    186028, 183550, 182294, 185432, 183644, 184685, 187145, 183703, 183450,
    183465, 182808, 182556, 183884, 183751, 182722, 183094, 182990, 183914,
    183914, 186090, 182457, 186341, 182880, 183958, 183958, 180979, 181551,
    182892, 182593, 185804, 182629, 182178,
)  # fmt: skip

FIRST_REPLAY_FRAME = 15
"""1-based, as the diagnostic files are numbered: 1.912, 1.911 and 1.905 s."""


@dataclass(frozen=True)
class Sequence:
    name: str
    offsets: tuple[float, ...]
    sizes: tuple[int, ...]
    view: PanelView

    def frames(self) -> list[CapturedFrame]:
        """A payload per still, the same one again where ProTee repeated it."""

        frames: list[CapturedFrame] = []
        marker = 0
        for position, (offset, size) in enumerate(
            zip(self.offsets, self.sizes, strict=True)
        ):
            if position == 0 or size != self.sizes[position - 1]:
                marker = position
            head = b"\xff\xd8" + f"{self.name}{marker}".encode()
            frames.append(
                CapturedFrame(offset, head + b"\x00" * max(0, size - len(head)))
            )
        return frames


SEQUENCES = (
    Sequence("shot1_behind", SHOT_1_OFFSETS, SHOT_1_BEHIND, PanelView.BEHIND),
    Sequence("shot1_side", SHOT_1_OFFSETS, SHOT_1_SIDE, PanelView.SIDE),
    Sequence("shot2_behind", SHOT_2_OFFSETS, SHOT_2_BEHIND, PanelView.BEHIND),
    Sequence("shot2_side", SHOT_2_OFFSETS, SHOT_2_SIDE, PanelView.SIDE),
    Sequence("shot3_behind", SHOT_3_OFFSETS, SHOT_3_BEHIND, PanelView.BEHIND),
    Sequence("shot3_side", SHOT_3_OFFSETS, SHOT_3_SIDE, PanelView.SIDE),
)
BUDGET = {PanelView.BEHIND: 8, PanelView.SIDE: 7}
PANEL = {PanelView.BEHIND: "swing_behind", PanelView.SIDE: "swing_side"}

DOWNSWING_STILLS = {
    SwingPhase.TRANSITION: 39,
    SwingPhase.DOWNSWING: 40,
    SwingPhase.IMPACT: 41,
    SwingPhase.RELEASE: 43,
}
"""1-based diagnostic still each downswing aim takes, on every sequence."""


def kept(sequence: Sequence) -> dict[SwingPhase, int]:
    """Each aimed phase and the 1-based still that went out labelled with it."""

    name = PANEL[sequence.view]
    chosen, _intervals, _combined, aimed = select(
        {name: sequence.frames()},
        {name: BUDGET[sequence.view]},
        {name: sequence.view},
    )
    assert len(chosen[name]) == len(aimed[name])
    return {
        phase: index + 1 for phase, index in zip(aimed[name], chosen[name], strict=True)
    }


@pytest.mark.parametrize("sequence", SEQUENCES, ids=lambda s: s.name)
class TestTheTwentyFpsSwings:
    def test_the_replay_starts_on_the_still_read_by_eye(
        self, sequence: Sequence
    ) -> None:
        assert replay_start(sequence.frames()) == FIRST_REPLAY_FRAME - 1

    def test_each_downswing_aim_takes_its_own_position(
        self, sequence: Sequence
    ) -> None:
        stills = kept(sequence)

        for phase, still in DOWNSWING_STILLS.items():
            assert stills[phase] == still, phase

    def test_the_moments_before_the_downswing_are_where_they_were(
        self, sequence: Sequence
    ) -> None:
        stills = kept(sequence)

        assert stills[SwingPhase.SETUP] == FIRST_REPLAY_FRAME
        assert stills[SwingPhase.BACKSWING] == 24
        if sequence.view is PanelView.BEHIND:
            assert stills[SwingPhase.TAKEAWAY] == 21
            assert stills[SwingPhase.TOP] == 34
        else:
            assert SwingPhase.TAKEAWAY not in stills
            assert stills[SwingPhase.TOP] == 32

    def test_the_whole_budget_is_spent_one_still_per_aim(
        self, sequence: Sequence
    ) -> None:
        stills = kept(sequence)

        assert len(stills) == BUDGET[sequence.view]
        assert len(set(stills.values())) == len(stills)

    def test_the_old_ten_fps_aims_took_the_top_and_the_delivery(
        self, sequence: Sequence
    ) -> None:
        """Why the aims moved: 1.75, 1.86 and 1.97 s took stills 36, 38 and 40,
        one to two positions early for what their labels promised."""

        frames = sequence.frames()
        start = frames[FIRST_REPLAY_FRAME - 1].offset_seconds
        old = nearest(
            frames, [start + 1.75, start + 1.86, start + 1.97], not_before=start
        )

        assert [index + 1 for index in old] == [36, 38, 40]


def test_both_views_aim_the_downswing_at_the_same_times() -> None:
    behind = dict(PHASE_SECONDS[PanelView.BEHIND])
    side = dict(PHASE_SECONDS[PanelView.SIDE])

    for phase, seconds in (
        (SwingPhase.TRANSITION, 1.90),
        (SwingPhase.DOWNSWING, 1.95),
        (SwingPhase.IMPACT, 2.00),
        (SwingPhase.RELEASE, 2.08),
    ):
        assert behind[phase] == pytest.approx(seconds), phase
        assert side[phase] == pytest.approx(seconds), phase
