"""Where ProTee's replay begins, found from nothing but how big each still is.

The fixture here is a real shot. Its stills are long gone — only the newest
few captures are kept on disk — but its `metadata.json` recorded every
screenshot's offset and encoded size, and those two columns are all the
selector reads. So the timeline is reproduced exactly: one payload per still,
JPEG-headed, of the recorded length, and no two alike, because ProTee's loading
screen is an animated spinner and no two of its frames were alike either. That
last property is the whole bug. Equality-based trimming saw a spinner as
movement, kept the whole window, and sent the spinner to the coach.

The detector tests after it are the ways a size signal could fool itself: a
slow rise, a single bright frame, a window that is all loading, a window that
is all replay, and dropped requests in the middle.
"""

from __future__ import annotations

import pytest

from caddiej.swing_capture import (
    CapturedFrame,
    active_interval,
    replay_start,
    select,
)
from caddiej.swing_capture.select import package_quota, take_evenly

BEHIND = (
    (0.516, 83873), (0.625, 83243), (0.735, 81688), (0.844, 80745),
    (0.953, 79849), (1.063, 79107), (1.172, 77717), (1.281, 78274),
    (1.391, 79063), (1.5, 79626), (1.61, 80631), (1.719, 64759),
    (1.828, 88119), (1.938, 147119), (2.047, 148189), (2.156, 148846),
    (2.266, 148683), (2.375, 149944), (2.485, 146201), (2.594, 145922),
    (2.703, 145302), (2.828, 145287), (2.938, 144774), (3.063, 145297),
    (3.172, 146844), (3.281, 147930), (3.391, 146796), (3.5, 146501),
    (3.61, 147155), (3.735, 145169), (3.844, 145482), (3.953, 141068),
    (4.063, 141608), (4.172, 142196), (4.281, 145698), (4.406, 144704),
)  # fmt: skip

SIDE = (
    (0.516, 101953), (0.625, 101814), (0.735, 100550), (0.844, 99907),
    (0.953, 99261), (1.063, 98720), (1.172, 98980), (1.281, 99000),
    (1.391, 101210), (1.5, 101517), (1.61, 101723), (1.719, 101777),
    (1.828, 104090), (1.938, 207134), (2.047, 206741), (2.156, 205760),
    (2.266, 205579), (2.375, 203899), (2.485, 205208), (2.594, 202932),
    (2.703, 203991), (2.828, 206546), (2.938, 204140), (3.063, 203878),
    (3.172, 205572), (3.281, 204041), (3.391, 205775), (3.5, 204505),
    (3.61, 204727), (3.735, 203722), (3.844, 203574), (3.953, 201944),
    (4.063, 203476), (4.172, 204948), (4.281, 205423), (4.406, 204594),
)  # fmt: skip

REPLAY_STARTS = 1.938
LOADING_STILLS = 13
"""Stills 0.516-1.828 s inclusive: the spinner."""

SELECTED = 8
PROVIDER_IMAGES = 6


def a_payload(length: int, marker: int) -> bytes:
    """A distinct JPEG-headed payload of exactly `length` bytes."""

    head = b"\xff\xd8" + marker.to_bytes(4, "big")
    return head + b"\x00" * max(0, length - len(head))


def frames_of(timeline: tuple[tuple[float, int], ...]) -> list[CapturedFrame]:
    return [
        CapturedFrame(offset, a_payload(size, index))
        for index, (offset, size) in enumerate(timeline)
    ]


def sized(sizes: list[int], *, step: float = 0.1) -> list[CapturedFrame]:
    return [
        CapturedFrame(round(index * step, 3), a_payload(size, index))
        for index, size in enumerate(sizes)
    ]


def capture_selection() -> tuple[dict[str, tuple[float, ...]], float, float]:
    """What the capture keeps, by the same quota arithmetic the service uses."""

    tracks = {"swing_behind": frames_of(BEHIND), "swing_side": frames_of(SIDE)}
    quota = package_quota(
        SELECTED,
        (
            ("swing_behind", False, len(BEHIND)),
            ("swing_side", False, len(SIDE)),
            ("impact", True, 3),
        ),
    )
    assert quota == {"swing_behind": 4, "swing_side": 3, "impact": 1}
    chosen, _intervals, combined, _phases = select(
        tracks, max(quota[name] for name in tracks)
    )
    kept = {
        name: tuple(
            tracks[name][index].offset_seconds
            for index in take_evenly(chosen[name], quota[name])
        )
        for name in tracks
    }
    return kept, combined.start_seconds, combined.end_seconds


class TestTheRealShot:
    @pytest.mark.parametrize("timeline", [BEHIND, SIDE], ids=["behind", "side"])
    def test_each_camera_finds_the_replay_at_1_938_s(
        self, timeline: tuple[tuple[float, int], ...]
    ) -> None:
        frames = frames_of(timeline)

        interval = active_interval(frames)

        assert len({frame.data for frame in frames}) == len(frames)
        assert replay_start(frames) == LOADING_STILLS
        assert interval.detected is True
        assert interval.start_seconds == pytest.approx(REPLAY_STARTS)
        assert interval.end_seconds == pytest.approx(4.406)
        assert interval.pre_replay_frames == LOADING_STILLS

    def test_the_shared_timeline_starts_where_the_replay_does(self) -> None:
        _kept, start, end = capture_selection()

        assert start == pytest.approx(REPLAY_STARTS)
        assert end == pytest.approx(4.406)

    def test_no_kept_still_is_the_loading_screen(self) -> None:
        kept, _start, _end = capture_selection()

        assert len(kept["swing_behind"]) == 4
        assert len(kept["swing_side"]) == 3
        for offsets in kept.values():
            assert min(offsets) >= REPLAY_STARTS
            assert list(offsets) == sorted(set(offsets))
            assert offsets[0] == pytest.approx(REPLAY_STARTS)
            assert offsets[-1] == pytest.approx(4.406)
        assert kept["swing_behind"] == pytest.approx((1.938, 2.703, 3.61, 4.406))
        assert kept["swing_side"] == pytest.approx((1.938, 3.61, 4.406))

    def test_the_provider_still_sends_three_behind_and_two_side(self) -> None:
        kept, _start, _end = capture_selection()
        quota = package_quota(
            PROVIDER_IMAGES,
            (
                ("swing_behind", False, len(kept["swing_behind"])),
                ("swing_side", False, len(kept["swing_side"])),
                ("impact", True, 1),
            ),
        )

        sent = {
            name: tuple(
                kept[name][index]
                for index in take_evenly(tuple(range(len(kept[name]))), quota[name])
            )
            for name in kept
        }

        assert quota == {"swing_behind": 3, "swing_side": 2, "impact": 1}
        assert sent["swing_behind"] == pytest.approx((1.938, 3.61, 4.406))
        assert sent["swing_side"] == pytest.approx((1.938, 4.406))


class TestTheDetector:
    LOADING = [80000, 79000, 81000, 78000, 80500, 79500, 80200, 79800]
    REPLAY = [150000, 149000, 151000, 148000, 150500, 149500] * 3

    def test_a_loading_screen_then_a_replay_is_found(self) -> None:
        frames = sized(self.LOADING + self.REPLAY)

        assert replay_start(frames) == len(self.LOADING)

    def test_a_slow_rise_is_not_a_replay_starting(self) -> None:
        frames = sized([50000 + 3000 * index for index in range(36)])

        interval = active_interval(frames)

        assert replay_start(frames) == 0
        assert interval.detected is False
        assert interval.pre_replay_frames == 0

    def test_a_single_bright_still_during_loading_does_not_start_it(self) -> None:
        loading = list(self.LOADING)
        loading[3] = 160000

        assert replay_start(sized(loading + self.LOADING + self.LOADING)) == 0
        assert replay_start(sized(loading + self.REPLAY)) == len(loading)

    def test_a_replay_that_ends_below_the_level_is_left_alone(self) -> None:
        frames = sized(self.LOADING + self.REPLAY + [50000])

        interval = active_interval(frames)

        assert replay_start(frames) == 0
        assert interval.pre_replay_frames == 0

    def test_a_window_that_is_all_loading_is_not_detected(self) -> None:
        frames = sized(self.LOADING * 4)

        interval = active_interval(frames)

        assert replay_start(frames) == 0
        assert interval.detected is False
        assert interval.start_seconds == frames[0].offset_seconds
        assert interval.end_seconds == frames[-1].offset_seconds

    def test_a_window_that_is_all_replay_is_unchanged(self) -> None:
        frames = sized(self.REPLAY * 2)

        interval = active_interval(frames)

        assert replay_start(frames) == 0
        assert interval.detected is False
        assert interval.pre_replay_frames == 0

    def test_blank_stills_take_no_part(self) -> None:
        frames = sized(self.LOADING + self.REPLAY)
        blank = b"\xff\xd8" + b"\x00" * 100
        frames[2] = CapturedFrame(frames[2].offset_seconds, blank)
        frames[len(self.LOADING) + 4] = CapturedFrame(
            frames[len(self.LOADING) + 4].offset_seconds, blank + b"\x01"
        )

        assert replay_start(frames) == len(self.LOADING)
        assert active_interval(frames).pre_replay_frames == len(self.LOADING)

    def test_a_blank_before_the_step_is_stepped_over(self) -> None:
        frames = sized(self.LOADING + self.REPLAY)
        last_loading = len(self.LOADING) - 1
        frames[last_loading] = CapturedFrame(
            frames[last_loading].offset_seconds, b"\xff\xd8" + b"\x00" * 100
        )

        assert replay_start(frames) == len(self.LOADING)

    def test_a_replay_too_short_to_trust_falls_back_to_the_whole_window(
        self,
    ) -> None:
        """The replay is found, but then holds its first frame for the rest of
        the window: two moving stills are not a swing."""

        held = a_payload(150000, 9999)
        frames = sized([*self.LOADING, 80100, 79900, 151000]) + [
            CapturedFrame(1.1 + index * 0.1, held) for index in range(11)
        ]

        interval = active_interval(frames)

        assert replay_start(frames) == len(self.LOADING) + 2
        assert interval.detected is False
        assert interval.pre_replay_frames == 0
        assert interval.start_seconds == frames[0].offset_seconds
        assert interval.end_seconds == frames[-1].offset_seconds

    def test_a_held_still_before_the_replay_is_trimmed_as_before(self) -> None:
        held = a_payload(80000, 9999)
        frames = [CapturedFrame(index * 0.1, held) for index in range(10)] + [
            CapturedFrame(1.0 + index * 0.1, a_payload(80000 + index, index))
            for index in range(20)
        ]

        interval = active_interval(frames)

        assert replay_start(frames) == 0
        assert interval.detected is True
        assert interval.start_seconds == pytest.approx(0.9)
        assert interval.pre_replay_frames == 0

    def test_the_later_of_the_two_starts_wins(self) -> None:
        held = a_payload(80000, 9999)
        frames = (
            [CapturedFrame(index * 0.1, held) for index in range(4)]
            + [
                CapturedFrame(0.4 + index * 0.1, a_payload(size, index))
                for index, size in enumerate(self.LOADING)
            ]
            + [
                CapturedFrame(1.2 + index * 0.1, a_payload(size, 100 + index))
                for index, size in enumerate(self.REPLAY)
            ]
        )

        interval = active_interval(frames)

        assert interval.detected is True
        assert interval.start_seconds == pytest.approx(1.2)
        assert interval.pre_replay_frames == 4 + len(self.LOADING)
