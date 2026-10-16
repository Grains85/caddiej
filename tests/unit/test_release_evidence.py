"""Deliberate evidence for every V4 phase, release included, from each camera.

V4 scores six release checkpoints and infers a chicken wing from them, and no
still was ever aimed at the release: the frame plan stopped at impact, the
budget of thirteen bought six moments per camera, and a release picture
reached the model only by accident. And the shaft checkpoints were written off
as a property of the bay when the down-the-line source was merely framed too
tight in OBS; it now shows the whole golfer and the whole club.

The capture here is the bay's own shape on a fake clock: ProTee's loading
spinner for about 1.4 s of the burst, then two replays that are still moving
when the window closes, captured at the production defaults.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from tests.unit.sheet_support import sheet_cells
from tests.unit.test_coach_protee import (
    ALL_VALUES,
    DRIVER,
    Coach,
    ConcurrentClock,
    a_checkpoint,
    a_reply,
)
from tests.unit.test_coach_v4 import BEHIND_FRAME, SIDE_FRAME, parsed
from tests.unit.test_swing_capture import a_jpeg

from caddiej.coaching.checkpoints import CHECKPOINTS_BY_ID, SCOREABLE
from caddiej.coaching.mechanics import CLUB_MOTION, club_motion, score_mechanics
from caddiej.coaching.swing_faults import infer_faults
from caddiej.config import Settings
from caddiej.domain.cues import CapturePanel, PanelKind, PanelView, VisualClaim
from caddiej.domain.events import ClubSelected, ShotDetected, SwingFramesReady
from caddiej.domain.media import SwingCapture
from caddiej.domain.swing_checkpoints import CheckpointId, SwingFaultId
from caddiej.domain.swing_phase import PHASE_ORDER, SwingPhase
from caddiej.swing_capture import LatestSwingStore, SwingCaptureService
from caddiej.swing_capture.phases import PHASE_SECONDS
from caddiej.swing_capture.sheet import stored_cells
from caddiej.swing_capture.store import METADATA_NAME

C = CheckpointId
F = SwingFaultId
P = SwingPhase

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
LOADING_STILLS = 14
"""Screenshots of ProTee's spinner before the replay: 0.5 s to 1.8 s of the
burst at 10 a second, as on the bay (13-14 on the 2026-10-07 capture)."""

DEFAULTS = Settings.model_construct()


def a_picture(source: str, moment: int, size: int) -> bytes:
    tag = f"{source}#{moment}".encode()
    payload = a_jpeg(640, 360) + tag
    return payload + b"\x00" * max(0, size - len(payload))


class BayObs:
    """OBS showing a loading spinner, then a replay that never holds still.

    The spinner is a small file and changes every screenshot; the replay is
    four times the size, as ProTee's is, which is the step `replay_start`
    finds."""

    def __init__(self) -> None:
        self.taken: dict[str, int] = {}

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def source_screenshot(
        self, source_name: str, *, width: int | None = None
    ) -> bytes:
        count = self.taken.get(source_name, 0)
        self.taken[source_name] = count + 1
        if source_name in (ALL_VALUES, IMPACT):
            return a_picture(source_name, 0, 4_000)
        if count < LOADING_STILLS:
            return a_picture(f"{source_name}-loading", count, 2_000)
        return a_picture(source_name, count, 8_000)


class Bay:
    """The swing-capture service at the production defaults unless told."""

    def __init__(self, tmp_path: Path, **overrides: Any) -> None:
        self.obs = BayObs()
        self.clock = ConcurrentClock()
        self.published: list[object] = []
        self.latest = tmp_path / "media" / "latest_swing"
        options: dict[str, Any] = {
            "initial_delay_seconds": DEFAULTS.swing_capture_initial_delay_seconds,
            "duration_seconds": DEFAULTS.swing_capture_duration_seconds,
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
            LatestSwingStore(self.latest),
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

    async def strike(self, detected: ShotDetected) -> SwingCapture | None:
        before = len(self.captures())
        await self.service.on_shot_detected(detected)
        task = self.service._task  # noqa: SLF001
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        captures = self.captures()
        return captures[-1] if len(captures) > before else None

    async def shot(self, trigger_id: str = "70") -> SwingCapture:
        capture = await self.strike(
            ShotDetected(trigger_id=trigger_id, source="protee_log")
        )
        assert capture is not None
        return capture

    def captures(self) -> list[SwingCapture]:
        return [
            event.capture
            for event in self.published
            if isinstance(event, SwingFramesReady)
        ]


def aimed(capture: SwingCapture, camera_id: str) -> list[SwingPhase | None]:
    camera = next(c for c in capture.cameras if c.camera_id == camera_id)
    return [frame.phase for frame in camera.frames]


def offset_of(capture: SwingCapture, camera_id: str, phase: SwingPhase) -> float:
    camera = next(c for c in capture.cameras if c.camera_id == camera_id)
    return next(f.offset_seconds for f in camera.frames if f.phase is phase)


def replay_start(capture: SwingCapture, camera_id: str) -> float:
    payload = json.loads((capture.directory / METADATA_NAME).read_text("utf-8"))
    entry = next(c for c in payload["cameras"] if c["camera_id"] == camera_id)
    return entry["replay_interval"]["start_seconds"]


def kept(capture: SwingCapture) -> int:
    return sum(len(camera.frames) for camera in capture.cameras)


# --- the release is aimed at, on each camera's own clock -----------------------


class TestTheReleaseIsDeliberatelyTargeted:
    async def test_down_the_line_is_aimed_at_every_phase_through_the_release(
        self, tmp_path
    ) -> None:
        capture = await Bay(tmp_path).shot()

        assert aimed(capture, "swing_behind") == [
            P.SETUP,
            P.TAKEAWAY,
            P.BACKSWING,
            P.TOP,
            P.TRANSITION,
            P.DOWNSWING,
            P.IMPACT,
            P.RELEASE,
        ]

    async def test_face_on_is_aimed_at_everything_but_the_takeaway(
        self, tmp_path
    ) -> None:
        capture = await Bay(tmp_path).shot()

        assert aimed(capture, "swing_side") == [
            P.SETUP,
            P.BACKSWING,
            P.TOP,
            P.TRANSITION,
            P.DOWNSWING,
            P.IMPACT,
            P.RELEASE,
        ]

    async def test_each_release_still_is_its_own_camera_s_release_moment(
        self, tmp_path
    ) -> None:
        capture = await Bay(tmp_path).shot()

        for camera_id, view in (
            ("swing_behind", PanelView.BEHIND),
            ("swing_side", PanelView.SIDE),
        ):
            moment = dict(PHASE_SECONDS[view])[P.RELEASE]
            start = replay_start(capture, camera_id)
            assert offset_of(capture, camera_id, P.RELEASE) == pytest.approx(
                start + moment, abs=0.06
            )

    async def test_the_default_window_reaches_the_release_with_room_to_spare(
        self, tmp_path
    ) -> None:
        capture = await Bay(tmp_path).shot()

        window_end = (
            DEFAULTS.swing_capture_initial_delay_seconds
            + DEFAULTS.swing_capture_duration_seconds
        )
        latest = max(
            offset_of(capture, camera_id, P.RELEASE)
            for camera_id in ("swing_behind", "swing_side")
        )
        assert window_end - latest >= 0.9
        static_end = (
            DEFAULTS.swing_capture_static_delay_seconds
            + DEFAULTS.swing_capture_static_duration_seconds
        )
        assert window_end <= static_end, "the longer window costs no wall time"

    async def test_a_window_too_short_for_the_release_aims_at_none(
        self, tmp_path
    ) -> None:
        """The old 3 s default ended before either replay reached its release.
        No earlier still is relabelled to stand in for it."""

        capture = await Bay(tmp_path, duration_seconds=3.0).shot()

        for camera_id in ("swing_behind", "swing_side"):
            assert P.RELEASE not in aimed(capture, camera_id), camera_id
            assert P.IMPACT not in aimed(capture, camera_id), camera_id


class TestTheTwoClocksAreIndependent:
    async def test_each_camera_is_aimed_from_its_own_replay_start(
        self, tmp_path
    ) -> None:
        capture = await Bay(tmp_path).shot()

        for camera_id, view in (
            ("swing_behind", PanelView.BEHIND),
            ("swing_side", PanelView.SIDE),
        ):
            start = replay_start(capture, camera_id)
            for phase in (P.TOP, P.DOWNSWING):
                moment = dict(PHASE_SECONDS[view])[phase]
                assert offset_of(capture, camera_id, phase) == pytest.approx(
                    start + moment, abs=0.06
                ), (camera_id, phase)

        assert offset_of(capture, "swing_side", P.DOWNSWING) - replay_start(
            capture, "swing_side"
        ) != pytest.approx(
            offset_of(capture, "swing_behind", P.TOP)
            - replay_start(capture, "swing_behind"),
            abs=0.06,
        )

    async def test_every_label_is_announced_as_a_guess_with_its_own_time(
        self, tmp_path
    ) -> None:
        from caddiej.swing_capture.stills import read_labeled_stills

        capture = await Bay(tmp_path).shot()

        stills = await read_labeled_stills(capture, 16)
        labels = [label for _data, label, _panel, _id, _detail in stills]
        release = [label for label in labels if "~release" in label]
        assert len(release) == 2
        assert any("SWING_BEHIND" in label for label in release)
        assert any("SWING_SIDE" in label for label in release)


# --- the image budget ----------------------------------------------------------


class TestTheBudget:
    async def test_the_default_is_the_impact_panel_and_fifteen_aimed_moments(
        self, tmp_path
    ) -> None:
        capture = await Bay(tmp_path).shot()

        assert DEFAULTS.swing_capture_selected_frames == 16
        assert DEFAULTS.coaching_max_images == 16
        assert kept(capture) == 16
        assert len(aimed(capture, "impact")) == 1

    async def test_all_values_is_outside_the_budget(self, tmp_path) -> None:
        capture = await Bay(tmp_path).shot()

        assert capture.metrics is not None and capture.metrics.ok
        assert kept(capture) == 16
        payload = json.loads((capture.directory / METADATA_NAME).read_text("utf-8"))
        assert payload["coach_metrics"]["counts_against_selected_frames"] is False

    async def test_a_smaller_budget_drops_moments_by_priority_and_is_respected(
        self, tmp_path
    ) -> None:
        capture = await Bay(tmp_path, selected_frames=13).shot()

        assert kept(capture) == 13
        assert aimed(capture, "swing_behind") == [
            P.SETUP,
            P.TOP,
            P.TRANSITION,
            P.DOWNSWING,
            P.IMPACT,
            P.RELEASE,
        ]
        assert P.RELEASE in aimed(capture, "swing_side")

    async def test_a_larger_budget_buys_no_near_duplicates(self, tmp_path) -> None:
        capture = await Bay(tmp_path, selected_frames=24).shot()

        assert kept(capture) == 16
        for camera_id in ("swing_behind", "swing_side"):
            phases = aimed(capture, camera_id)
            assert len(phases) == len(set(phases)), camera_id
            assert None not in phases, camera_id


# --- one request, newest shot wins ---------------------------------------------


class TestOneRequestStillCarriesItAll:
    async def test_one_request_with_sixteen_stills_and_the_all_values_grid(
        self, tmp_path
    ) -> None:
        from caddiej.coaching.fakes import ScriptedProvider

        bay = Bay(tmp_path)
        provider = ScriptedProvider(replies=(a_reply(),))
        coach = Coach(provider, max_images=DEFAULTS.coaching_max_images)
        await coach.service.open()
        await coach.service.on_club_selected(ClubSelected(club=DRIVER))
        detected = ShotDetected(trigger_id="71", source="protee_log")
        await coach.service.on_shot_detected(detected)
        capture = await bay.strike(detected)
        assert capture is not None
        await coach.service.on_swing_frames(SwingFramesReady(capture=capture))
        await coach.service.aclose()

        assert provider.calls == 1
        images = provider.requests[0].images
        assert len(images) == 1, "the seventeen travel as one evidence sheet"
        cells = sheet_cells(images[0].data)
        assert cells == (
            *(f"DTL_P{n}" for n in range(1, 9)),
            *(f"FO_P{n}" for n in (1, 3, 4, 5, 6, 7, 8)),
            "IMPACT",
            "ALL_VALUES",
        )
        releases = [
            (cell["cell_id"], cell["frame_id"])
            for cell in stored_cells(capture.directory).values()
            if cell["aimed_phase"] == "release"
        ]
        assert [cell_id for cell_id, _ in releases] == ["DTL_P8", "FO_P8"]
        assert releases[0][1].startswith("swing_behind_")
        assert releases[1][1].startswith("swing_side_")

    async def test_a_newer_strike_still_wins_the_capture(self, tmp_path) -> None:
        bay = Bay(tmp_path)

        await bay.service.on_shot_detected(
            ShotDetected(trigger_id="72", source="protee_log")
        )
        first = bay.service._task  # noqa: SLF001
        await asyncio.sleep(0)
        await bay.service.on_shot_detected(
            ShotDetected(trigger_id="73", source="protee_log")
        )
        await asyncio.gather(first, return_exceptions=True)
        latest = bay.service._task  # noqa: SLF001
        if latest is not None:
            await asyncio.gather(latest, return_exceptions=True)

        assert [capture.trigger_id for capture in bay.captures()] == ["73"]


# --- what a release picture can and cannot support -----------------------------


RELEASE_FRAME = BEHIND_FRAME
FACE_ON_RELEASE = SIDE_FRAME


def at_the_release(checkpoint: CheckpointId, score: int, **extra: Any) -> dict:
    return a_checkpoint(checkpoint, score, phase=P.RELEASE.value, **extra)


class TestAReleasePictureCanShowAChickenWing:
    def test_a_folded_lead_arm_at_the_release_is_a_chicken_wing(self) -> None:
        items, rejected = parsed(
            {
                "release_lead_arm_structure": at_the_release(
                    C.RELEASE_LEAD_ARM_STRUCTURE, 35, deviation="bent"
                ),
                "release_arm_extension": at_the_release(
                    C.RELEASE_ARM_EXTENSION, 55, deviation="too_little"
                ),
            },
            **{RELEASE_FRAME: P.RELEASE},
        )

        assert rejected == ()
        faults = {item.fault for item in infer_faults(score_mechanics(items))}
        assert F.CHICKEN_WING in faults

    def test_face_on_shows_the_connection_down_the_line_cannot(self) -> None:
        connection = a_checkpoint(C.RELEASE_ARM_CONNECTION, 50)
        assert connection["frame_id"] == FACE_ON_RELEASE

        items, rejected = parsed(
            {"release_arm_connection": connection},
            **{FACE_ON_RELEASE: P.RELEASE},
        )

        assert rejected == ()
        assert [item.view for item in items] == [PanelView.SIDE]


class TestWrongOrMissingReleaseEvidenceIsNull:
    def test_the_release_still_identified_as_the_finish_is_refused(self) -> None:
        """The aim was the release; the picture was the finish. The model's
        identification wins, and the chicken wing is not read from it."""

        items, rejected = parsed(
            {
                "release_lead_arm_structure": at_the_release(
                    C.RELEASE_LEAD_ARM_STRUCTURE, 35, deviation="bent"
                )
            },
            **{RELEASE_FRAME: P.FINISH},
        )

        assert items == ()
        assert rejected == (("release_lead_arm_structure", "phase_conflict"),)

    def test_a_release_checkpoint_cited_from_the_impact_is_refused(self) -> None:
        _, rejected = parsed(
            {
                "release_lead_arm_structure": a_checkpoint(
                    C.RELEASE_LEAD_ARM_STRUCTURE, 35, deviation="bent", phase="impact"
                )
            }
        )

        assert rejected == (("release_lead_arm_structure", "phase"),)

    def test_no_release_picture_is_no_release_score_and_no_chicken_wing(
        self,
    ) -> None:
        reply = {
            spec.checkpoint_id.value: a_checkpoint(spec.checkpoint_id, 85)
            for spec in SCOREABLE
            if spec.phase is not P.RELEASE
        }

        items, _ = parsed(reply)
        mechanics = score_mechanics(items)

        release = next(p for p in mechanics.phases if p.phase is P.RELEASE)
        assert release.value is None
        assert release.assessed == 0
        for spec in SCOREABLE:
            if spec.phase is P.RELEASE:
                assert mechanics.score_of(spec.checkpoint_id) is None
        faults = {item.fault for item in infer_faults(mechanics)}
        assert F.CHICKEN_WING not in faults


# --- the whole club is in the down-the-line picture ----------------------------


class TestTheWholeClubDownTheLine:
    def test_every_club_motion_checkpoint_is_accepted_from_its_own_dtl_still(
        self,
    ) -> None:
        reply = {
            checkpoint.value: a_checkpoint(checkpoint, 80) for checkpoint in CLUB_MOTION
        }

        items, rejected = parsed(reply)
        motion = club_motion(score_mechanics(items))

        assert rejected == ()
        assert {item.checkpoint for item in items} == set(CLUB_MOTION)
        assert all(value == "80" for value in motion.values())

    def test_each_one_has_a_down_the_line_still_aimed_at_its_moment(self) -> None:
        aimed_dtl = {phase for phase, _seconds in PHASE_SECONDS[PanelView.BEHIND]}
        for checkpoint in CLUB_MOTION:
            assert CHECKPOINTS_BY_ID[checkpoint].phase in aimed_dtl, checkpoint

    def test_a_shaft_checkpoint_from_the_face_on_camera_is_refused(self) -> None:
        face_on = a_checkpoint(C.TRANSITION_SHALLOWING, 80) | {
            "frame_id": SIDE_FRAME,
            "view": "face_on",
        }

        _, rejected = parsed({"transition_shallowing": face_on})

        assert rejected == (("transition_shallowing", "view"),)

    def test_a_club_the_model_did_not_see_is_still_null(self) -> None:
        items, rejected = parsed({checkpoint.value: None for checkpoint in CLUB_MOTION})

        assert items == () and rejected == ()
        assert set(club_motion(score_mechanics(items)).values()) == {"null"}

    def test_a_whole_club_does_not_make_the_clubface_readable(self) -> None:
        for checkpoint in (C.TAKEAWAY_CLUBFACE, C.TOP_CLUBFACE, C.IMPACT_SHAFT_LEAN):
            assert CHECKPOINTS_BY_ID[checkpoint].scoreable is False, checkpoint

        _, rejected = parsed({"top_clubface": a_checkpoint(C.TOP_CLUBFACE, 80)})

        assert rejected == (("top_clubface", "not_scoreable"),)


class TestTheOldCropIsNotACameraLimit:
    """The club leaving the down-the-line picture was an OBS framing problem
    and has been fixed. Nothing may describe it as a property of the bay."""

    STALE = (
        "leaves the down-the-line frame",
        "crops the hands and club",
        "nearly always null",
        "club often leaves",
        "no release picture is deliberately aimed",
        "does not aim at it",
    )

    def test_no_document_or_module_says_the_bay_crops_the_club(self) -> None:
        root = Path(__file__).resolve().parents[2]
        paths = [
            *sorted((root / "docs").glob("*.md")),
            *sorted((root / "src" / "caddiej").rglob("*.py")),
        ]
        offenders = [
            f"{path.relative_to(root)}: {phrase}"
            for path in paths
            for phrase in self.STALE
            if phrase in path.read_text(encoding="utf-8").lower()
        ]

        assert offenders == []

    def test_every_phase_with_club_checkpoints_is_aimed_at_down_the_line(
        self,
    ) -> None:
        aimed_dtl = {phase for phase, _seconds in PHASE_SECONDS[PanelView.BEHIND]}
        club_phases = {
            spec.phase
            for spec in SCOREABLE
            if spec.club and PanelView.BEHIND in spec.views
        }

        assert club_phases <= aimed_dtl
        assert [p for p in PHASE_ORDER if p in aimed_dtl][-1] is P.RELEASE
