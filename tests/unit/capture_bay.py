"""A swing-capture stand-in used by tests that are not about golf advice.

The OBS stub shows a small changing file, then a larger one. That size step is
what the capture selector treats as the replay starting.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from tests.unit.test_swing_capture import Clock, a_jpeg

from caddiej.domain.cues import CapturePanel, PanelKind, PanelView, VisualClaim
from caddiej.domain.events import ShotDetected, SwingFramesReady
from caddiej.domain.media import SwingCapture
from caddiej.domain.swing_phase import SwingPhase
from caddiej.swing_capture import LatestSwingStore, SwingCaptureService
from caddiej.swing_capture.store import METADATA_NAME

ALL_VALUES = "ProTee All Values"
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


class ConcurrentClock(Clock):
    """The fake clock, except the All Values task's waits do not move it."""

    async def sleep(self, seconds: float) -> None:
        task = asyncio.current_task()
        if task is not None and task.get_name() == "coach-metrics-capture":
            self.slept.append(seconds)
            await asyncio.sleep(0)
            return
        await super().sleep(seconds)


def a_picture(source: str, moment: int, size: int) -> bytes:
    tag = f"{source}#{moment}".encode()
    payload = a_jpeg(640, 360) + tag
    return payload + b"\x00" * max(0, size - len(payload))


class BayObs:
    """OBS showing a loading spinner, then a replay that never holds still."""

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
    """The swing-capture service at its defaults unless told otherwise."""

    def __init__(self, tmp_path: Path, **overrides: Any) -> None:
        from caddiej.config import Settings

        defaults = Settings.model_construct()
        self.obs = BayObs()
        self.clock = ConcurrentClock()
        self.published: list[object] = []
        self.latest = tmp_path / "media" / "latest_swing"
        options: dict[str, Any] = {
            "initial_delay_seconds": defaults.swing_capture_initial_delay_seconds,
            "duration_seconds": defaults.swing_capture_duration_seconds,
            "fps": defaults.swing_capture_fps,
            "downswing_fps": defaults.swing_capture_downswing_fps,
            "downswing_delay_seconds": defaults.swing_capture_downswing_delay_seconds,
            "downswing_duration_seconds": (
                defaults.swing_capture_downswing_duration_seconds
            ),
            "selected_frames": defaults.swing_capture_selected_frames,
            "static_delay_seconds": defaults.swing_capture_static_delay_seconds,
            "static_duration_seconds": defaults.swing_capture_static_duration_seconds,
            "static_fps": defaults.swing_capture_static_fps,
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


def replay_start(capture: SwingCapture, camera_id: str) -> float:
    payload = json.loads((capture.directory / METADATA_NAME).read_text("utf-8"))
    entry = next(c for c in payload["cameras"] if c["camera_id"] == camera_id)
    return float(entry["replay_interval"]["start_seconds"])
