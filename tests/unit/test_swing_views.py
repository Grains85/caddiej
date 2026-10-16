"""Two body views on the existing swing-capture path.

Behind is down-the-line. Side is face-on. Impact and delivery stay the
graphics they already were. The kept stills are one budget for the shot.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from caddiej.coaching.cues import CUES_BY_ID, admissible_cues
from caddiej.coaching.diagnose import diagnose
from caddiej.coaching.evidence import _capture_note
from caddiej.coaching.probe import fixture_shot
from caddiej.config import Settings
from caddiej.core.event_bus import AsyncEventBus
from caddiej.domain.coaching import AdviceKind
from caddiej.domain.cues import (
    CapturePanel,
    CueId,
    EvidenceGrade,
    PanelKind,
    PanelView,
    VisualClaim,
)
from caddiej.domain.events import ShotDetected
from caddiej.domain.media import SwingCameraCapture, SwingCapture, SwingFrame
from caddiej.domain.shot import ClubKind
from caddiej.swing_capture.diagnose import probe_swing_capture
from caddiej.swing_capture.select import package_quota
from caddiej.swing_capture.service import SwingCaptureService
from caddiej.swing_capture.stills import image_label
from caddiej.swing_capture.store import LatestSwingStore

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
BODY = frozenset(
    {
        VisualClaim.BODY_THROUGH_IMPACT,
        VisualClaim.BODY_IN_BACKSWING,
        VisualClaim.WHOLE_BODY,
        VisualClaim.FINISH_POSITION,
        VisualClaim.SEQUENCE,
    }
)


def _baseline():
    from caddiej.coaching.baseline import ClubBaseline

    return ClubBaseline(club=ClubKind.IRON, shots=8)


def _settings(**values: str) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _body(panel_id: str, view: PanelView) -> SwingCapture:
    frame = SwingFrame(
        camera_id=panel_id,
        index=1,
        offset_seconds=0.4,
        path=Path(panel_id) / "frame_001.jpg",
        width=32,
        height=32,
    )
    return SwingCapture(
        capture_id=uuid4(),
        trigger_id="16",
        directory=Path("media/latest_swing"),
        started_at=NOW,
        detected_at=NOW,
        cameras=(
            SwingCameraCapture(
                camera_id=panel_id,
                source=panel_id,
                ok=True,
                frames=(frame,),
                captured=4,
                failed=0,
                kind=PanelKind.DYNAMIC,
                supports=BODY,
                view=view,
            ),
        ),
    )


class _Obs:
    """Static panels return one picture; a body panel holds its first picture
    for two stills, as a replay does before it starts, and then moves."""

    BODY_SOURCES = frozenset({"Swing Behind", "Swing Side"})

    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.failing = failing
        self.requests: list[str] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def source_screenshot(
        self, source_name: str, *, width: int | None = None
    ) -> bytes:
        self.requests.append(source_name)
        if source_name in self.failing:
            from caddiej.adapters.obs import ObsResourceNotFoundError

            raise ObsResourceNotFoundError(source_name)
        frame = (
            b"\xff\xc0"
            + (17).to_bytes(2, "big")
            + b"\x08"
            + (8).to_bytes(2, "big")
            + (8).to_bytes(2, "big")
            + bytes(10)
        )
        moment = b""
        if source_name in self.BODY_SOURCES:
            taken = self.requests.count(source_name) - 1
            moment = b"#%d" % max(0, taken - 1)
        payload = b"\xff\xd8" + frame + source_name.encode() + moment + b"\xff\xd9"
        if len(payload) < 800:
            payload += b"\x00" * (800 - len(payload))
        return payload

    async def get_version(self):
        from caddiej.adapters.obs import ObsVersionInfo

        return ObsVersionInfo("30", "5", 1, ("jpg",))


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)
        await asyncio.sleep(0)


def _service(tmp_path, obs: _Obs, panels: tuple[CapturePanel, ...], selected: int = 8):
    clock = _Clock()
    published: list[object] = []

    async def publish(event: object) -> None:
        published.append(event)

    service = SwingCaptureService(
        lambda: obs,
        (),
        LatestSwingStore(tmp_path / "latest_swing"),
        publish,
        enabled=True,
        panels=panels,
        initial_delay_seconds=0.0,
        duration_seconds=0.3,
        fps=10.0,
        selected_frames=selected,
        static_delay_seconds=0.3,
        static_duration_seconds=0.2,
        static_fps=10.0,
        command_timeout=1.0,
        shutdown_timeout=0.2,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    return service, published


def _panels() -> tuple[CapturePanel, ...]:
    return (
        CapturePanel(
            "swing_behind",
            "Swing Behind",
            PanelKind.DYNAMIC,
            BODY,
            PanelView.BEHIND,
            "SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE",
        ),
        CapturePanel(
            "swing_side",
            "Swing Side",
            PanelKind.DYNAMIC,
            BODY,
            PanelView.SIDE,
            "SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE",
        ),
        CapturePanel(
            "impact",
            "Top Impact",
            PanelKind.STATIC,
            frozenset(),
            PanelView.NONE,
            "SWING_CAPTURE_PANEL_IMPACT_SOURCE",
        ),
        CapturePanel(
            "delivery",
            "Shot Video",
            PanelKind.STATIC,
            frozenset(),
            PanelView.NONE,
            "SWING_CAPTURE_PANEL_DELIVERY_SOURCE",
        ),
    )


class TestConfiguration:
    def test_behind_only(self) -> None:
        panels = _settings(
            SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE="Swing Behind"
        ).capture_panels()
        assert [panel.panel_id for panel in panels] == ["swing_behind"]
        assert panels[0].view is PanelView.BEHIND

    def test_side_only(self) -> None:
        panels = _settings(
            SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE="Swing Side"
        ).capture_panels()
        assert [panel.panel_id for panel in panels] == ["swing_side"]
        assert panels[0].view is PanelView.SIDE

    def test_both_body_views(self) -> None:
        panels = _settings(
            SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE="Swing Behind",
            SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE="Swing Side",
        ).capture_panels()
        assert [panel.panel_id for panel in panels] == ["swing_behind", "swing_side"]

    def test_all_four_roles(self) -> None:
        panels = _settings(
            SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE="Swing Behind",
            SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE="Swing Side",
            SWING_CAPTURE_PANEL_IMPACT_SOURCE="Top Impact",
            SWING_CAPTURE_PANEL_DELIVERY_SOURCE="Shot Video",
        ).capture_panels()
        assert [panel.panel_id for panel in panels] == [
            "swing_behind",
            "swing_side",
            "impact",
            "delivery",
        ]
        assert panels[2].supports == frozenset()
        assert panels[3].supports == frozenset()

    def test_three_panels_leave_delivery_and_the_floor_source_out(self) -> None:
        settings = _settings(
            SWING_CAPTURE_PANELS="swing_behind,swing_side,impact",
            SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE="Swing Behind",
            SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE="Swing Side",
            SWING_CAPTURE_PANEL_IMPACT_SOURCE="Impact",
            SWING_CAPTURE_PANEL_DELIVERY_SOURCE="Flight Path",
            OBS_SHOT_VIDEO_SOURCE="Flight Path",
        )
        panels = settings.capture_panels()
        assert [panel.panel_id for panel in panels] == [
            "swing_behind",
            "swing_side",
            "impact",
        ]
        assert [panel.source for panel in panels] == [
            "Swing Behind",
            "Swing Side",
            "Impact",
        ]
        assert settings.absent_capture_roles() == ()

    def test_the_old_swing_source_is_the_behind_view(self) -> None:
        panels = _settings(
            SWING_CAPTURE_PANELS="swing,impact,delivery",
            SWING_CAPTURE_PANEL_SWING_SOURCE="Swing Behind",
            SWING_CAPTURE_PANEL_IMPACT_SOURCE="Top Impact",
        ).capture_panels()
        assert [panel.panel_id for panel in panels] == ["swing_behind", "impact"]
        assert panels[0].source == "Swing Behind"
        assert panels[0].setting == "SWING_CAPTURE_PANEL_SWING_SOURCE"

    def test_an_explicit_behind_source_wins_and_is_not_duplicated(self) -> None:
        with pytest.warns(DeprecationWarning, match="ignored"):
            panels = _settings(
                SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE="Swing Behind",
                SWING_CAPTURE_PANEL_SWING_SOURCE="Some Other Camera",
                SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE="Swing Side",
            ).capture_panels()
        assert [panel.source for panel in panels] == ["Swing Behind", "Swing Side"]

    def test_the_same_source_is_not_captured_twice(self) -> None:
        with pytest.warns(UserWarning, match="not captured"):
            panels = _settings(
                SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE="Swing Behind",
                SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE="Swing Behind",
            ).capture_panels()
        assert [panel.panel_id for panel in panels] == ["swing_behind"]


class TestThePackage:
    def test_eight_frames_are_shared_not_multiplied(self) -> None:
        quota = package_quota(
            8,
            (
                ("swing_behind", False, 40),
                ("swing_side", False, 40),
                ("impact", True, 3),
                ("delivery", True, 3),
            ),
        )
        assert sum(quota.values()) == 8
        assert quota["impact"] == 1
        assert quota["delivery"] == 1
        assert quota["swing_behind"] == 3
        assert quota["swing_side"] == 3

    def test_three_panels_keep_one_impact_still_inside_eight(self) -> None:
        quota = package_quota(
            8,
            (
                ("swing_behind", False, 40),
                ("swing_side", False, 40),
                ("impact", True, 3),
            ),
        )
        assert sum(quota.values()) <= 8
        assert sum(quota.values()) == 8
        assert quota["impact"] == 1
        assert quota["swing_behind"] == 4
        assert quota["swing_side"] == 3
        assert "delivery" not in quota

    def test_a_missing_body_view_gives_its_share_to_the_other(self) -> None:
        quota = package_quota(
            8,
            (
                ("swing_behind", False, 40),
                ("swing_side", False, 0),
                ("impact", True, 3),
                ("delivery", True, 3),
            ),
        )
        assert quota["swing_side"] == 0
        assert quota["swing_behind"] == 6
        assert sum(quota.values()) == 8


class TestViewSemantics:
    def test_no_body_source_admits_no_body_cue(self) -> None:
        diagnosis = diagnose(fixture_shot(), _baseline())
        graphic = SwingCapture(
            capture_id=uuid4(),
            trigger_id="16",
            directory=Path("media/latest_swing"),
            started_at=NOW,
            detected_at=NOW,
            cameras=(
                SwingCameraCapture(
                    camera_id="impact",
                    source="Top Impact",
                    ok=True,
                    frames=(
                        SwingFrame(
                            camera_id="impact",
                            index=1,
                            offset_seconds=3.5,
                            path=Path("impact/frame_001.jpg"),
                        ),
                    ),
                    captured=2,
                    failed=0,
                    kind=PanelKind.STATIC,
                    view=PanelView.NONE,
                ),
            ),
        )
        allowed = admissible_cues(diagnosis, has_frames=True, capture=graphic)
        assert all(CUES_BY_ID[cue].grade is not EvidenceGrade.VISUAL for cue in allowed)

    def test_sway_needs_the_side_view(self) -> None:
        diagnosis = diagnose(fixture_shot(impact_horizontal=12.0), _baseline())
        assert diagnosis.kind is AdviceKind.CORRECTION
        behind = admissible_cues(
            diagnosis, has_frames=True, capture=_body("swing_behind", PanelView.BEHIND)
        )
        side = admissible_cues(
            diagnosis, has_frames=True, capture=_body("swing_side", PanelView.SIDE)
        )
        assert CueId.SWAY_OFF_BALL not in behind
        assert CueId.SWAY_OFF_BALL in side
        assert CueId.REVERSE_PIVOT not in behind
        assert CueId.REVERSE_PIVOT in side


class TestCapture:
    async def test_one_failing_source_keeps_the_others(self, tmp_path) -> None:
        obs = _Obs(failing=frozenset({"Swing Side"}))
        service, published = _service(tmp_path, obs, _panels())
        await service.on_shot_detected(
            ShotDetected(trigger_id="16", source="protee_log")
        )
        assert service._task is not None
        await service._task
        await service.aclose()

        assert published
        capture = published[0].capture  # type: ignore[attr-defined]
        ready = {camera.camera_id for camera in capture.cameras if camera.frames}
        assert ready == {"swing_behind", "impact", "delivery"}
        assert sum(len(camera.frames) for camera in capture.cameras) <= 8

    async def test_a_shot_captures_three_panels_and_not_delivery(
        self, tmp_path
    ) -> None:
        panels = _panels()[:3]
        panels = (
            panels[0],
            panels[1],
            CapturePanel(
                "impact",
                "Impact",
                PanelKind.STATIC,
                frozenset(),
                PanelView.NONE,
                "SWING_CAPTURE_PANEL_IMPACT_SOURCE",
            ),
        )
        obs = _Obs()
        service, published = _service(tmp_path, obs, panels)
        await service.on_shot_detected(
            ShotDetected(trigger_id="16", source="protee_log")
        )
        assert service._task is not None
        await service._task
        await service.aclose()

        assert set(obs.requests) == {"Swing Behind", "Swing Side", "Impact"}
        assert published
        capture = published[0].capture  # type: ignore[attr-defined]
        assert {camera.camera_id for camera in capture.cameras} == {
            "swing_behind",
            "swing_side",
            "impact",
        }
        assert sum(len(camera.frames) for camera in capture.cameras) <= 8
        labels = {image_label(camera) for camera in capture.cameras if camera.frames}
        assert labels == {
            "SWING_BEHIND / DOWN_THE_LINE",
            "SWING_SIDE / FACE_ON",
            "IMPACT",
        }
        note = _capture_note(
            capture, sum(len(camera.frames) for camera in capture.cameras)
        )
        assert "DELIVERY" not in note
        assert "Flight Path" not in note

    async def test_a_detected_shot_on_the_bus_starts_capture(self, tmp_path) -> None:
        obs = _Obs()
        service, _published = _service(tmp_path, obs, _panels()[:1], selected=2)
        bus = AsyncEventBus()
        service.subscribe(bus)
        await bus.start()
        await bus.publish(ShotDetected(trigger_id="41", source="protee_log"))
        assert await bus.drain(timeout=2.0)
        if service._task is not None:
            await service._task
        await service.aclose()
        await bus.stop(timeout=1.0)
        assert service.captures_started == 1
        assert "Swing Behind" in obs.requests

    async def test_the_newer_shot_replaces_the_capture(self, tmp_path) -> None:
        obs = _Obs()
        service, _published = _service(tmp_path, obs, _panels()[:1], selected=2)
        await service.on_shot_detected(
            ShotDetected(trigger_id="16", source="protee_log")
        )
        assert service._task is not None
        await service._task
        obs.requests.clear()
        await service.on_shot_detected(
            ShotDetected(trigger_id="17", source="protee_log")
        )
        assert service._task is not None
        await service._task
        await service.aclose()
        text = (tmp_path / "latest_swing" / "metadata.json").read_text(encoding="utf-8")
        assert '"trigger_id": "17"' in text
        assert '"trigger_id": "16"' not in text


class TestTheCheck:
    async def test_the_check_names_every_configured_source(self, tmp_path) -> None:
        obs = _Obs()
        probe = await probe_swing_capture(
            host="127.0.0.1",
            port=4455,
            password="not-printed",
            connect_timeout=1.0,
            panels=_panels(),
            output_root=tmp_path,
            client_factory=lambda: obs,
        )
        assert probe.ok
        for title in ("Swing Behind:", "Swing Side:", "Impact:", "Delivery:"):
            assert title in probe.report
        assert "capture: OK" in probe.report
        assert "Effective panels" in probe.report
        written = sorted(path.name for path in tmp_path.rglob("*.jpg"))
        assert written == [
            "delivery.jpg",
            "impact.jpg",
            "swing_behind.jpg",
            "swing_side.jpg",
        ]

    async def test_the_check_reports_only_the_three_configured_panels(
        self, tmp_path
    ) -> None:
        panels = (
            _panels()[0],
            _panels()[1],
            CapturePanel(
                "impact",
                "Impact",
                PanelKind.STATIC,
                frozenset(),
                PanelView.NONE,
                "SWING_CAPTURE_PANEL_IMPACT_SOURCE",
            ),
        )
        probe = await probe_swing_capture(
            host="127.0.0.1",
            port=4455,
            password="not-printed",
            connect_timeout=1.0,
            panels=panels,
            absent=(),
            output_root=tmp_path,
            client_factory=_Obs,
        )
        assert probe.ok
        assert "swing_behind='Swing Behind'" in probe.report
        assert "swing_side='Swing Side'" in probe.report
        assert "impact='Impact'" in probe.report
        assert "Delivery" not in probe.report
        assert "Flight Path" not in probe.report
        written = sorted(path.name for path in tmp_path.rglob("*.jpg"))
        assert written == ["impact.jpg", "swing_behind.jpg", "swing_side.jpg"]

    async def test_a_missing_source_names_its_setting(self, tmp_path) -> None:
        obs = _Obs(failing=frozenset({"Swing Side"}))
        probe = await probe_swing_capture(
            host="127.0.0.1",
            port=4455,
            password="not-printed",
            connect_timeout=1.0,
            panels=_panels(),
            output_root=tmp_path,
            client_factory=lambda: obs,
        )
        assert not probe.ok
        assert "SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE" in probe.report
        assert "Swing Side" in probe.report
