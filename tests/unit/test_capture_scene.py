"""Coach sources stay live through a private capture scene, never on the floor.

The real swing capture service and the real OBS client, against a model of the
bay's OBS in which a window capture refreshes only while something renders it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tests.unit.test_obs_client import CAPTURE, FLOOR, SOURCES, _Bay, _client
from tests.unit.test_swing_capture import Clock, an_impact

from caddiej.adapters.obs import ObsClient
from caddiej.domain.events import Event, SwingFramesReady
from caddiej.swing_capture import LatestSwingStore, SwingCaptureService
from caddiej.swing_capture.diagnose import probe_swing_capture

PANELS = SOURCES[:3]


class _VersionedBay(_Bay):
    async def get_version(self, timeout: float) -> dict[str, Any]:
        return {
            "obsVersion": "32.0.0",
            "obsWebSocketVersion": "5.6.0",
            "rpcVersion": 1,
            "supportedImageFormats": ["jpg", "png"],
        }


async def a_burst(tmp_path, bay: _Bay) -> tuple[list[Event], list[ObsClient]]:
    clients: list[ObsClient] = []
    published: list[Event] = []

    def make() -> ObsClient:
        client = _client(bay)
        clients.append(client)
        return client

    async def publish(event: Event) -> None:
        published.append(event)

    clock = Clock()
    service = SwingCaptureService(
        make,
        PANELS,
        LatestSwingStore(tmp_path / "media" / "latest_swing"),
        publish,
        enabled=True,
        initial_delay_seconds=0.5,
        duration_seconds=1.0,
        fps=10.0,
        selected_frames=4,
        metrics_source="All Values",
        metrics_delay_seconds=0.5,
        capture_scene=CAPTURE,
        command_timeout=1.0,
        shutdown_timeout=0.5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    await service.on_shot_detected(an_impact("16"))
    task = service._task  # noqa: SLF001 — the test owns the lifecycle
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)
    await service.aclose()
    return published, clients


class TestABurstThroughTheCaptureScene:
    async def test_every_panel_is_live_and_nothing_reaches_the_floor(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        bay = _Bay()

        published, _ = await a_burst(tmp_path, bay)

        ready = [event for event in published if isinstance(event, SwingFramesReady)]
        assert len(ready) == 1
        root = tmp_path / "media" / "latest_swing"
        stills = [
            (root / frame.path).read_bytes()
            for camera in ready[0].capture.cameras
            for frame in camera.frames
        ]
        assert len(stills) >= 3
        assert len(set(stills)) == len(stills), "a panel froze"
        assert all(bay.ticks[name] > 1 for name in PANELS), "every panel refreshed"
        assert ready[0].capture.metrics is not None, "All Values is still captured"

        assert bay.toggles == []
        assert bay.program_switches == []
        assert bay.program == FLOOR
        assert all(bay.ticks[name] > 0 for name in SOURCES)
        messages = [record.getMessage() for record in caplog.records]
        assert messages.count("obs_capture_source_state") == len(SOURCES)
        assert "obs_capture_floor_unchanged" in messages
        assert "obs_capture_floor_changed" not in messages

    async def test_without_the_capture_scene_the_floor_is_still_never_used(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        bay = _Bay(capture_scene=False)

        await a_burst(tmp_path, bay)

        assert bay.toggles == []
        assert bay.program_switches == []
        assert "obs_capture_source_not_rendering" in caplog.text


class TestTheCheckCommand:
    async def check(self, tmp_path, bay: _Bay):
        return await probe_swing_capture(
            host="127.0.0.1",
            port=4455,
            password="not-printed",
            connect_timeout=1.0,
            sources=PANELS,
            metrics_source="All Values",
            capture_scene=CAPTURE,
            output_root=tmp_path / "out",
            client_factory=lambda: _client(bay),
        )

    async def test_it_reports_rendered_and_a_hidden_floor_item(self, tmp_path) -> None:
        bay = _VersionedBay()

        probe = await self.check(tmp_path, bay)

        assert probe.ok, probe.report
        assert f"Capture scene        {CAPTURE!r} (never Program)" in probe.report
        assert "'Swing Behind'" in probe.report
        assert "rendered=true" in probe.report
        assert "floor item: hidden" in probe.report
        assert "not-printed" not in probe.report
        assert bay.toggles == [] and bay.program_switches == []

    async def test_it_fails_with_the_setup_when_nothing_renders(self, tmp_path) -> None:
        bay = _VersionedBay(nested=False)

        probe = await self.check(tmp_path, bay)

        assert not probe.ok
        assert "FAILED at capture scene" in probe.report
        assert "Color Correction" in probe.report
        assert "Opacity 0" in probe.report
        assert bay.toggles == [] and bay.program_switches == []
