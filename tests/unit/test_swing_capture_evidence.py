"""What the first ProTee-only hardware shot got wrong, kept from coming back.

Two failures from one shot, trigger 40. Of 77 screenshots, 74 repeated the
one before, because the window captures were hidden scene items and OBS kept
rendering their cached texture. Then `latest_swing` could not be renamed
(`WinError 5`) because something held a file inside it open, and that one
rename was allowed to throw away a complete capture before Coach saw it.

The capture timing here is the production timing on a fake clock: body panels
from 0.5 s for 4 s at 10 fps, the impact panel from 3.5 s for 1.5 s at 2 fps,
eight stills in total, All Values at 3 s outside that budget.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path

import pytest
from tests.unit.sheet_support import sheet_cells
from tests.unit.test_coach_protee import (
    ALL_VALUES,
    DRIVER,
    Coach,
    ConcurrentClock,
    a_reply,
)
from tests.unit.test_swing_capture import a_jpeg

from caddiej.coaching.fakes import ScriptedProvider
from caddiej.domain.cues import CapturePanel, PanelKind, PanelView, VisualClaim
from caddiej.domain.events import (
    AnalysisCompleted,
    ClubSelected,
    ShotDetected,
    SwingFramesReady,
)
from caddiej.domain.media import SwingCapture
from caddiej.domain.swing_phase import SwingPhase
from caddiej.swing_capture import LatestSwingStore, SwingCaptureService
from caddiej.swing_capture import store as store_module
from caddiej.swing_capture.phases import PHASE_SECONDS
from caddiej.swing_capture.select import CapturedFrame, distinct_count, select
from caddiej.swing_capture.stills import read_labeled_stills
from caddiej.swing_capture.store import (
    EVIDENCE_KEEP,
    METADATA_NAME,
    STALE_NAME,
    newest_capture,
    retention_limit,
)

BEHIND = "Swing Behind"
SIDE = "Swing Side"
IMPACT = "Impact"
BODY = frozenset({VisualClaim.WHOLE_BODY, VisualClaim.SEQUENCE})
PANELS = (
    CapturePanel(
        "swing_behind",
        BEHIND,
        PanelKind.DYNAMIC,
        BODY,
        PanelView.BEHIND,
        "SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE",
    ),
    CapturePanel(
        "swing_side",
        SIDE,
        PanelKind.DYNAMIC,
        BODY,
        PanelView.SIDE,
        "SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE",
    ),
    CapturePanel(
        "impact",
        IMPACT,
        PanelKind.STATIC,
        frozenset(),
        PanelView.NONE,
        "SWING_CAPTURE_PANEL_IMPACT_SOURCE",
    ),
)


def a_picture(source: str, moment: int, size: int = 900) -> bytes:
    """A distinct still per source and moment, past the blank-frame size."""

    tag = f"{source}#{moment}".encode()
    payload = a_jpeg(640, 360) + tag
    return payload + b"\x00" * max(0, size - len(payload))


LOADING_STILLS = 5
"""ProTee's spinner before each body replay: distinct, and smaller than it."""

BURST_STILLS = 40
"""Body stills per shot on the `Rig` timing: 0.5 s for 4 s at 10 fps."""


class ReplayObs:
    """OBS showing ProTee's replay.

    Each body panel opens every shot's burst on `LOADING_STILLS` spinner
    stills, which is how the replay start is found. `frozen` sources return one picture for the whole
    burst, which is what a hidden window capture did. `held` sources then sit
    on their first replay picture for that many screenshots before the swing
    starts moving.
    """

    def __init__(
        self,
        *,
        frozen: frozenset[str] = frozenset(),
        held: dict[str, int] | None = None,
        loading: int = LOADING_STILLS,
    ) -> None:
        self.frozen = frozen
        self.held = held or {}
        self.loading = loading
        self.taken: dict[str, int] = {}
        self.returned: dict[str, list[bytes]] = {}

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def source_screenshot(
        self, source_name: str, *, width: int | None = None
    ) -> bytes:
        count = self.taken.get(source_name, 0)
        self.taken[source_name] = count + 1
        if source_name == ALL_VALUES:
            return a_picture(source_name, count)
        if source_name == IMPACT or source_name in self.frozen:
            data = a_picture(source_name, 0)
        elif count % BURST_STILLS < self.loading:
            data = a_picture(f"{source_name}-loading", count)
        else:
            into = count % BURST_STILLS - self.loading - self.held.get(source_name, 0)
            moment = count - count % BURST_STILLS + max(0, into)
            data = a_picture(source_name, moment, 1_200)
        self.returned.setdefault(source_name, []).append(data)
        return data


class Rig:
    """One swing-capture service on the production timing and a fake clock."""

    def __init__(self, tmp_path: Path, obs: ReplayObs) -> None:
        self.obs = obs
        self.clock = ConcurrentClock()
        self.published: list[object] = []
        self.latest = tmp_path / "media" / "latest_swing"
        self.store = LatestSwingStore(self.latest)
        self.service = SwingCaptureService(
            lambda: obs,
            (),
            self.store,
            self._publish,
            enabled=True,
            panels=PANELS,
            initial_delay_seconds=0.5,
            duration_seconds=4.0,
            fps=10.0,
            selected_frames=8,
            static_delay_seconds=3.5,
            static_duration_seconds=1.5,
            static_fps=2.0,
            metrics_source=ALL_VALUES,
            metrics_delay_seconds=3.0,
            command_timeout=1.0,
            shutdown_timeout=0.5,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )

    async def _publish(self, event: object) -> None:
        self.published.append(event)

    async def shot(self, trigger_id: str) -> SwingCapture | None:
        detected = ShotDetected(trigger_id=trigger_id, source="protee_log")
        return await self.strike(detected)

    async def strike(self, detected: ShotDetected) -> SwingCapture | None:
        before = len(self.captures())
        await self.service.on_shot_detected(detected)
        task = self.service._task  # noqa: SLF001
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        captures = self.captures()
        return captures[-1] if len(captures) > before else None

    def captures(self) -> list[SwingCapture]:
        return [
            event.capture
            for event in self.published
            if isinstance(event, SwingFramesReady)
        ]

    def latest_metadata(self) -> dict:
        return json.loads((self.latest / METADATA_NAME).read_text(encoding="utf-8"))


def kept(capture: SwingCapture, camera_id: str) -> list[bytes]:
    camera = next(c for c in capture.cameras if c.camera_id == camera_id)
    return [(capture.directory / frame.path).read_bytes() for frame in camera.frames]


def offsets(capture: SwingCapture, camera_id: str) -> list[float]:
    camera = next(c for c in capture.cameras if c.camera_id == camera_id)
    return [frame.offset_seconds for frame in camera.frames]


@pytest.fixture
def locked_latest(monkeypatch):
    """`latest_swing` cannot be renamed, as on the hardware PC at trigger 40.

    Only renames of `latest_swing` itself are refused. Creating a file beside
    the open ones still works, exactly as Windows allows.
    """

    real = store_module._rename  # noqa: SLF001
    state = {"locked": True}

    def refusing(source: Path, destination: Path, attempts: int = 1) -> None:
        if state["locked"] and source.name == "latest_swing":
            raise PermissionError(
                13, "Access is denied", str(source), None, str(destination)
            )
        real(source, destination, attempts)

    monkeypatch.setattr(store_module, "_rename", refusing)
    return state


# 1, 2, 3 ------------------------------------------------------------------


class TestDistinctMomentsStayDistinct:
    async def test_distinct_obs_content_reaches_disk_unchanged(self, tmp_path) -> None:
        rig = Rig(tmp_path, ReplayObs())

        capture = await rig.shot("40")

        assert capture is not None
        returned = set(rig.obs.returned[BEHIND])
        for data in kept(capture, "swing_behind"):
            assert data in returned, "the file on disk is not what OBS returned"

    async def test_the_four_behind_stills_are_four_different_pictures(
        self, tmp_path
    ) -> None:
        rig = Rig(tmp_path, ReplayObs())

        capture = await rig.shot("40")

        assert capture is not None
        behind = kept(capture, "swing_behind")
        assert len(behind) == 4
        assert len(set(behind)) == 4

    async def test_the_side_stills_are_different_pictures(self, tmp_path) -> None:
        rig = Rig(tmp_path, ReplayObs())

        capture = await rig.shot("40")

        assert capture is not None
        side = kept(capture, "swing_side")
        assert len(side) == 3
        assert len(set(side)) == 3

    async def test_a_replay_that_starts_late_still_gives_distinct_stills(
        self, tmp_path
    ) -> None:
        """Half the burst on one held picture, then the swing."""

        rig = Rig(tmp_path, ReplayObs(held={BEHIND: 20, SIDE: 20}))

        capture = await rig.shot("40")

        assert capture is not None
        for camera_id in ("swing_behind", "swing_side"):
            stills = kept(capture, camera_id)
            assert len(set(stills)) == len(stills), camera_id


# 4 ------------------------------------------------------------------------


class TestTheStillsSpanTheSwing:
    async def test_body_stills_are_aimed_at_moments_not_spread_evenly(
        self, tmp_path
    ) -> None:
        """What replaced the even spread, and why the spread had to go.

        This used to assert a minimum gap between stills, which is the right
        thing to ask for when nothing is known about what the replay contains.
        The first hardware session showed what it buys: four down-the-line
        stills, beautifully spaced, of the address, the takeaway, the top, and
        the player walking away from a mat with no ball on it. No transition.
        No downswing. No impact. The model was asked to rate the downswing
        anyway, and did.

        So the stills are now bunched where the swing is, and bunching is the
        evidence that the aiming happened: the transition and impact are a
        fraction of a second apart and both are worth more than an even gap.

        The replay holds its first picture for a second, as ProTee's does
        before it starts: that is the start the aiming counts from.
        """

        rig = Rig(tmp_path, ReplayObs(held={BEHIND: 10, SIDE: 10}))

        capture = await rig.shot("40")

        assert capture is not None
        times = offsets(capture, "swing_behind")
        assert times == sorted(times)
        behind = next(c for c in capture.cameras if c.camera_id == "swing_behind")
        aimed = [frame.phase for frame in behind.frames]
        # The four moments a four-image budget buys, cheapest last: the
        # transition and impact are what the old selector never once caught.
        assert aimed == [
            SwingPhase.SETUP,
            SwingPhase.TOP,
            SwingPhase.TRANSITION,
            SwingPhase.IMPACT,
        ]
        # And nothing is aimed at the tail, which is where the player walking
        # away lives. The last still is inside the replay, not at the end of it:
        # the old selector's final moment was the interval's own last frame.
        payload = json.loads(
            (capture.directory / METADATA_NAME).read_text(encoding="utf-8")
        )
        entry = next(c for c in payload["cameras"] if c["camera_id"] == "swing_behind")
        window = entry["replay_interval"]
        last_moment = max(
            seconds for _phase, seconds in PHASE_SECONDS[PanelView.BEHIND]
        )
        assert times[-1] <= window["start_seconds"] + last_moment + 0.06
        assert times[-1] < window["end_seconds"]

    async def test_the_metadata_has_a_timeline_of_every_screenshot(
        self, tmp_path
    ) -> None:
        rig = Rig(tmp_path, ReplayObs())

        capture = await rig.shot("40")

        assert capture is not None
        payload = json.loads(
            (capture.directory / METADATA_NAME).read_text(encoding="utf-8")
        )
        behind = next(c for c in payload["cameras"] if c["camera_id"] == "swing_behind")
        timeline = behind["timeline"]
        assert len(timeline) == behind["captured_frames"] == 40
        assert behind["distinct_frames"] == 40
        assert all(entry["changed"] for entry in timeline)
        assert sum(entry["selected"] for entry in timeline) == 4
        for entry in timeline:
            assert set(entry) >= {
                "index",
                "offset_seconds",
                "sha256",
                "bytes",
                "width",
                "height",
                "changed",
                "selected",
            }
            assert (entry["width"], entry["height"]) == (640, 360)
        for frame in behind["frames"]:
            data = (capture.directory / frame["file"]).read_bytes()
            assert hashlib.sha256(data).hexdigest().startswith(frame["sha256"][:12])
        text = json.dumps(payload)
        assert all(a_picture(BEHIND, m).hex()[:64] not in text for m in range(40))


# 5, 6, 7, 8 ---------------------------------------------------------------


class TestTheBudget:
    async def test_a_frozen_source_gives_one_still_not_four_copies(
        self, tmp_path, caplog
    ) -> None:
        rig = Rig(tmp_path, ReplayObs(frozen=frozenset({BEHIND})))

        with caplog.at_level(logging.WARNING, logger="caddiej.swing_capture"):
            capture = await rig.shot("40")

        assert capture is not None
        assert len(kept(capture, "swing_behind")) == 1
        assert "swing_capture_source_frozen" in caplog.text

    async def test_identical_stills_are_never_sent_to_the_model_twice(
        self, tmp_path
    ) -> None:
        rig = Rig(tmp_path, ReplayObs(frozen=frozenset({BEHIND})))

        capture = await rig.shot("40")

        assert capture is not None
        sent = await read_labeled_stills(capture, 8)
        images = [data for data, _label, _panel, _frame, _detail in sent]
        assert len(images) == len(set(images))

    async def test_the_frozen_panels_share_goes_to_the_panel_that_moved(
        self, tmp_path
    ) -> None:
        """Six aims go to the face-on panel. On this rig's ten stills a second
        its transition, delivery and impact aims are 0.05 s apart, so one of
        them has no still of its own and is dropped rather than sent twice."""

        moving = Rig(tmp_path / "moving", ReplayObs())
        rig = Rig(tmp_path / "frozen", ReplayObs(frozen=frozenset({BEHIND})))

        normal = await moving.shot("39")
        capture = await rig.shot("40")

        assert normal is not None and capture is not None
        assert len(kept(capture, "swing_side")) > len(kept(normal, "swing_side"))
        assert len(kept(capture, "swing_side")) == 5
        assert sum(len(c.frames) for c in capture.cameras) == 7

    async def test_impact_is_one_static_still(self, tmp_path) -> None:
        rig = Rig(tmp_path, ReplayObs())

        capture = await rig.shot("40")

        assert capture is not None
        impact = next(c for c in capture.cameras if c.camera_id == "impact")
        assert len(impact.frames) == 1
        assert impact.frames[0].offset_seconds >= 3.5

    async def test_eight_stills_in_total_across_the_three_panels(
        self, tmp_path
    ) -> None:
        rig = Rig(tmp_path, ReplayObs())

        capture = await rig.shot("40")

        assert capture is not None
        assert [len(c.frames) for c in capture.cameras] == [4, 3, 1]

    async def test_all_values_is_separate_from_the_eight(self, tmp_path) -> None:
        rig = Rig(tmp_path, ReplayObs())

        capture = await rig.shot("40")

        assert capture is not None
        assert capture.metrics is not None and capture.metrics.ok
        assert capture.metrics.path is not None
        assert (capture.directory / capture.metrics.path).is_file()
        assert rig.obs.taken[ALL_VALUES] == 1
        assert sum(len(c.frames) for c in capture.cameras) == 8

    def test_selection_never_repeats_a_picture_when_a_different_one_is_near(
        self,
    ) -> None:
        moving = [CapturedFrame(i * 0.1, a_picture("b", i // 5)) for i in range(40)]

        chosen, _intervals, _combined, _phases = select({"behind": moving}, 4)

        pictures = [moving[index].data for index in chosen["behind"]]
        assert len(pictures) == len(set(pictures)) == min(4, distinct_count(moving))


# 9 - 14 -------------------------------------------------------------------


class TestLatestSwingOnWindows:
    async def test_publishing_leaves_nothing_open(self, tmp_path) -> None:
        """Every directory written can be renamed and deleted at once.

        On Windows a single handle left open by CaddieJ fails this.
        """

        rig = Rig(tmp_path, ReplayObs())
        capture = await rig.shot("40")
        assert capture is not None
        await read_labeled_stills(capture, 8)

        for directory in (capture.directory, rig.latest):
            moved = directory.with_name(directory.name + "-moved")
            directory.rename(moved)
            moved.rename(directory)

    async def test_a_new_shot_replaces_the_old_latest_whole(self, tmp_path) -> None:
        rig = Rig(tmp_path, ReplayObs())
        await rig.shot("39")
        capture = await rig.shot("40")

        assert capture is not None
        assert capture.published == rig.latest
        assert rig.latest_metadata()["trigger_id"] == "40"
        assert not (rig.latest / STALE_NAME).exists()

    async def test_a_refused_rename_marks_the_old_latest_stale(
        self, tmp_path, locked_latest
    ) -> None:
        locked_latest["locked"] = False
        rig = Rig(tmp_path, ReplayObs())
        await rig.shot("39")
        locked_latest["locked"] = True

        capture = await rig.shot("40")

        assert capture is not None
        old = rig.latest_metadata()
        assert old["trigger_id"] == "39"
        assert old["stale"] is True
        assert old["superseded_by"]["trigger_id"] == "40"
        assert (rig.latest / STALE_NAME).is_file()
        newest = newest_capture(rig.latest)
        assert newest is not None and newest.trigger_id == "40"

    async def test_a_refused_rename_keeps_the_capture_and_says_which_failed(
        self, tmp_path, locked_latest, caplog
    ) -> None:
        locked_latest["locked"] = False
        rig = Rig(tmp_path, ReplayObs())
        await rig.shot("39")
        locked_latest["locked"] = True

        with caplog.at_level(logging.INFO, logger="caddiej.swing_capture"):
            capture = await rig.shot("40")

        assert capture is not None, "no swing_capture_completed: Coach never ran"
        assert capture.published is None
        assert capture.trigger_id == "40"
        assert len(kept(capture, "swing_behind")) == 4
        messages = [record.getMessage() for record in caplog.records]
        assert "swing_capture_latest_publish_failed" in messages
        assert "swing_capture_failed" not in messages
        assert "swing_capture_completed" in messages
        assert rig.service.captures_failed == 0
        assert rig.service.latest_failures == 1

    async def test_working_directories_do_not_pile_up(
        self, tmp_path, locked_latest
    ) -> None:
        locked_latest["locked"] = False
        rig = Rig(tmp_path, ReplayObs())
        await rig.shot("38")
        locked_latest["locked"] = True
        await rig.shot("39")
        locked_latest["locked"] = False
        for trigger in range(40, 40 + EVIDENCE_KEEP + 2):
            await rig.shot(str(trigger))

        media = rig.latest.parent
        assert not list(media.glob(".latest_swing.*"))
        assert len(list((media / "swing_shots").iterdir())) == EVIDENCE_KEEP
        assert not (rig.latest / STALE_NAME).exists()

    def test_startup_sweeps_what_a_crash_left_behind(self, tmp_path) -> None:
        latest = tmp_path / "media" / "latest_swing"
        for leftover in ("staging-1234abcd", "discard-922d8a28", "mirror-00ff00ff"):
            path = latest.parent / f".latest_swing.{leftover}"
            path.mkdir(parents=True)
            (path / "frame_001.jpg").write_bytes(b"old")

        LatestSwingStore(latest).prepare()

        assert not list(latest.parent.glob(".latest_swing.*"))

    def test_startup_prune_keeps_twenty_when_asked_and_three_by_default(
        self, tmp_path
    ) -> None:
        wide = LatestSwingStore(tmp_path / "wide" / "latest_swing", evidence_keep=20)
        narrow = LatestSwingStore(tmp_path / "narrow" / "latest_swing")
        assert retention_limit(EVIDENCE_KEEP, 20) == 20

        def fill(store: LatestSwingStore, count: int) -> None:
            root = store.evidence_root
            root.mkdir(parents=True)
            for index in range(count):
                shot = root / f"20261008T1200{index:02d}-1-abcd{index:04d}"
                shot.mkdir()
                diagnostic = shot / "diagnostic" / "swing_behind"
                diagnostic.mkdir(parents=True)
                (diagnostic / "frame_001.jpg").write_bytes(b"jpeg")

        fill(wide, 22)
        fill(narrow, 5)
        wide.prepare()
        narrow.prepare()

        wide_kept = sorted(path.name for path in wide.evidence_root.iterdir())
        assert len(wide_kept) == 20
        newest = wide.evidence_root / wide_kept[-1]
        assert (newest / "diagnostic" / "swing_behind" / "frame_001.jpg").is_file()
        assert not (wide.evidence_root / "20261008T120000-1-abcd0000").exists()
        assert len(list(narrow.evidence_root.iterdir())) == EVIDENCE_KEEP
        wide.prepare()
        assert len(list(wide.evidence_root.iterdir())) == 20

    async def test_the_newest_shot_wins_everywhere(self, tmp_path) -> None:
        rig = Rig(tmp_path, ReplayObs())
        first = await rig.shot("39")
        second = await rig.shot("40")

        assert first is not None and second is not None
        assert first.directory != second.directory
        assert rig.latest_metadata()["trigger_id"] == "40"
        newest = newest_capture(rig.latest)
        assert newest is not None and newest.trigger_id == "40"
        assert rig.store.note_shot("39", "late-shot") is False
        assert rig.latest_metadata().get("shot_id") is None

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows file locking")
    async def test_a_real_open_file_in_latest_does_not_lose_the_shot(
        self, tmp_path
    ) -> None:
        """The trigger 40 failure itself: an open handle inside latest_swing."""

        rig = Rig(tmp_path, ReplayObs())
        await rig.shot("39")
        viewer = (rig.latest / "swing_behind" / "frame_001.jpg").open("rb")
        try:
            capture = await rig.shot("40")
        finally:
            viewer.close()

        assert capture is not None
        assert capture.published is None
        assert rig.latest_metadata()["stale"] is True
        assert len(kept(capture, "swing_behind")) == 4

        after = await rig.shot("41")
        assert after is not None and after.published == rig.latest
        assert rig.latest_metadata()["trigger_id"] == "41"
        assert not list(rig.latest.parent.glob(".latest_swing.*"))


# 15 -----------------------------------------------------------------------


class TestCoachStillRuns:
    async def test_a_locked_latest_still_reaches_all_values_and_advice(
        self, tmp_path, locked_latest
    ) -> None:
        locked_latest["locked"] = False
        rig = Rig(tmp_path, ReplayObs())
        await rig.shot("39")
        locked_latest["locked"] = True

        provider = ScriptedProvider(replies=(a_reply(),))
        coach = Coach(provider)
        await coach.service.open()
        await coach.service.on_club_selected(ClubSelected(club=DRIVER))
        detected = ShotDetected(trigger_id="40", source="protee_log")
        await coach.service.on_shot_detected(detected)

        capture = await rig.strike(detected)
        assert capture is not None and capture.published is None
        await coach.service.on_swing_frames(SwingFramesReady(capture=capture))
        await coach.service.aclose()

        assert provider.calls == 1
        images = provider.requests[0].images
        assert len(images) == 1, "one evidence sheet"
        cells = sheet_cells(images[0].data)
        assert len(cells) == 9, "eight swing stills and the All Values still"
        assert cells[-1] == "ALL_VALUES"
        completed = coach.bus.of(AnalysisCompleted)
        assert len(completed) == 1
        assert completed[0].shot_id == detected.event_id
