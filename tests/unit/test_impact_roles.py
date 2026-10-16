"""Impact has two scene items and two jobs, and neither may do the other's.

Swing capture screenshots the Impact view from the private capture scene,
where its item stays enabled and nobody watches. The floor reveal shows Impact
on the projector, from a hidden item in the floor scene, after each strike.
Same OBS source, two scene items. These run the real `ObsClient` against a
model of the bay's OBS that records every visibility change, so "capture
never touches the floor" is a count of requests rather than a promise.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from tests.unit.test_capture_scene import _VersionedBay, a_burst
from tests.unit.test_obs_client import CAPTURE, FLOOR, SOURCES, _client
from tests.unit.test_obs_reveal import Clock, StubClient, an_impact

from caddiej.adapters.hud import ConnectionRegistry, create_app
from caddiej.adapters.obs import ObsResourceNotFoundError
from caddiej.config import Settings
from caddiej.floor import FloorService
from caddiej.obs_reveal import RevealFeature, TimedObsSource, probe_obs_source

IMPACT = "Impact"
TOP_IMPACT = "Top Impact"
FLOOR_IMPACT_ITEM = 10 + SOURCES.index(IMPACT)
CAPTURE_IMPACT_ITEM = 20 + SOURCES.index(IMPACT)


class _RolesBay(_VersionedBay):
    """The bay, answering the scene item lookups a reveal and the probe make."""

    async def get_scene_item_id(
        self, scene_name: str, source_name: str, timeout: float
    ) -> int:
        item = self.scenes.get(scene_name, {}).get(source_name)
        if item is None:
            raise ObsResourceNotFoundError(
                f"OBS has no source named '{source_name}' in scene '{scene_name}'"
            )
        return int(item["sceneItemId"])


def a_reveal(
    bay: _RolesBay,
    clock: Clock,
    *,
    scene: str = FLOOR,
    source: str = IMPACT,
    **options: Any,
) -> TimedObsSource:
    return TimedObsSource(
        lambda: _client(bay),
        scene,
        source,
        feature=RevealFeature.TOP_IMPACT,
        enabled=True,
        delay_seconds=1.5,
        duration_seconds=12.0,
        command_timeout=1.0,
        shutdown_timeout=1.0,
        sleep=clock.sleep,
        **options,
    )


class TestCoachCaptureNeverTouchesTheFloor:
    async def test_a_capture_burst_makes_no_visibility_calls(self, tmp_path) -> None:
        bay = _RolesBay()

        await a_burst(tmp_path, bay)

        assert bay.toggles == []
        assert bay.program_switches == []
        for name in ("Swing Behind", "Swing Side", "All Values", IMPACT):
            assert not bay.on_floor(name), name

    async def test_capture_and_the_reveal_together_touch_only_the_floor_impact(
        self, tmp_path
    ) -> None:
        bay = _RolesBay()
        clock = Clock()
        reveal = a_reveal(bay, clock, capture_scene=CAPTURE)
        startup = reveal.start()
        assert startup is not None
        await startup

        await reveal.on_shot_detected(an_impact("16"))
        await clock.release_one()
        await a_burst(tmp_path, bay)
        assert bay.on_floor(IMPACT), "the intentional reveal is showing"
        await clock.release_one()
        await reveal.aclose()

        assert {(scene, item) for scene, item, _ in bay.toggles} == {
            (FLOOR, FLOOR_IMPACT_ITEM)
        }
        assert bay.scenes[CAPTURE][IMPACT]["sceneItemEnabled"] is True
        for name in ("Swing Behind", "Swing Side", "All Values"):
            assert not bay.on_floor(name), name


class TestTheIntentionalImpactReveal:
    async def test_it_shows_and_hides_on_its_own_timing(self) -> None:
        bay = _RolesBay()
        clock = Clock()
        reveal = a_reveal(bay, clock, capture_scene=CAPTURE)
        startup = reveal.start()
        assert startup is not None
        await startup

        await reveal.on_shot_detected(an_impact("16"))
        await clock.wait_pending()
        assert not bay.on_floor(IMPACT), "nothing before the delay"
        await clock.release_one()
        assert bay.on_floor(IMPACT)
        await clock.release_one()

        assert not bay.on_floor(IMPACT)
        assert clock.slept == [1.5, 12.0]
        assert [visible for *_, visible in bay.toggles] == [False, False, True, False]
        assert reveal.health().state == "resolved"
        assert reveal.health().reveals_shown == 1
        await reveal.aclose()

    async def test_a_newer_strike_still_wins(self) -> None:
        bay = _RolesBay()
        clock = Clock()
        reveal = a_reveal(bay, clock)
        await reveal.on_shot_detected(an_impact("16"))
        await clock.release_one()

        await reveal.on_shot_detected(an_impact("17"))
        await clock.wait_slept(3)

        assert not bay.on_floor(IMPACT), "the older reveal is taken down first"
        await reveal.aclose()


class TestTheCaptureSceneIsNeverAFloor:
    async def test_a_reveal_pointed_at_the_capture_scene_refuses(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bay = _RolesBay()
        reveal = a_reveal(bay, Clock(), scene=CAPTURE, capture_scene=CAPTURE)

        with caplog.at_level(logging.ERROR):
            assert reveal.start() is None
        await reveal.on_shot_detected(an_impact("16"))
        await reveal.aclose()

        assert not reveal.enabled
        assert bay.toggles == []
        assert "top_impact_refused_capture_scene" in caplog.text
        health = reveal.health()
        assert health.state == "refused"
        assert "OBS_FLOOR_SCENE" in health.detail

    def test_settings_refuse_a_floor_scene_that_is_the_capture_scene(self) -> None:
        with pytest.raises(ValidationError, match="swing capture scene"):
            Settings(
                OBS_FLOOR_SCENE=CAPTURE,
                SWING_CAPTURE_SCENE=CAPTURE,
                SWING_CAPTURE_ENABLED=True,
                FLOOR_TOP_IMPACT_ENABLED=True,
                _env_file=None,
            )

    def test_settings_allow_the_two_scenes_to_differ(self) -> None:
        settings = Settings(
            OBS_FLOOR_SCENE=FLOOR,
            SWING_CAPTURE_SCENE=CAPTURE,
            SWING_CAPTURE_ENABLED=True,
            FLOOR_TOP_IMPACT_ENABLED=True,
            _env_file=None,
        )

        assert settings.obs_floor_scene != settings.swing_capture_scene


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestAMissingRevealSourceKeepsSayingSo:
    async def test_the_warning_repeats_on_an_interval_with_a_count(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        clock = Clock()
        monotonic = FakeMonotonic()
        missing = StubClient(command_error=ObsResourceNotFoundError("600"))
        reveal = TimedObsSource(
            lambda: missing,
            FLOOR,
            TOP_IMPACT,
            feature=RevealFeature.TOP_IMPACT,
            enabled=True,
            delay_seconds=0.0,
            duration_seconds=1.0,
            command_timeout=0.5,
            shutdown_timeout=0.5,
            sleep=clock.sleep,
            monotonic=monotonic,
            missing_warning_interval=300.0,
        )

        with caplog.at_level(logging.WARNING):
            startup = reveal.start()
            assert startup is not None
            await startup
            monotonic.now += 10.0
            await reveal.on_shot_detected(an_impact("16"))
            await clock.wait_pending()
            monotonic.now += 301.0
            await reveal.on_shot_detected(an_impact("17"))
            await clock.wait_slept(2)
            await reveal.aclose()

        warnings = [
            record
            for record in caplog.records
            if record.getMessage() == "top_impact_source_missing"
        ]
        assert len(warnings) == 2
        counts = [getattr(record, "obs_reveal_missing_count") for record in warnings]
        assert counts[0] == 1 and counts[1] > counts[0]
        health = reveal.health()
        assert health.state == "missing"
        assert "Add Existing" in health.detail and TOP_IMPACT in health.detail

    def test_healthz_shows_the_missing_source(self) -> None:
        reveal = TimedObsSource(
            lambda: StubClient(),
            FLOOR,
            TOP_IMPACT,
            feature=RevealFeature.TOP_IMPACT,
            enabled=True,
        )
        app = create_app(
            FloorService(),
            ConnectionRegistry(),
            {"top_impact": lambda: reveal.health().as_dict()},
        )

        body = TestClient(app).get("/healthz").json()

        assert body["top_impact"]["state"] == "unchecked"
        assert body["top_impact"]["scene"] == FLOOR
        assert body["top_impact"]["source"] == TOP_IMPACT


class TestTheProbeReportsBothRoles:
    async def probe(self, bay: _RolesBay, source: str = IMPACT, scene: str = FLOOR):
        return await probe_obs_source(
            feature=RevealFeature.TOP_IMPACT,
            host="127.0.0.1",
            port=4455,
            password="not-printed",
            connect_timeout=1.0,
            scene=scene,
            source=source,
            hold_seconds=0.0,
            client_factory=lambda: _client(bay),
            capture_scene=CAPTURE,
            capture_source=IMPACT,
        )

    async def test_one_source_two_scene_items(self) -> None:
        bay = _RolesBay()

        probe = await self.probe(bay)

        assert probe.ok, probe.report
        assert "Floor presentation" in probe.report
        assert "Coach evidence" in probe.report
        assert f"scene item id {FLOOR_IMPACT_ITEM}" in probe.report
        assert f"scene item id {CAPTURE_IMPACT_ITEM}, visibility" in probe.report
        assert "used in both roles" in probe.report
        assert bay.toggles == [
            (FLOOR, FLOOR_IMPACT_ITEM, True),
            (FLOOR, FLOOR_IMPACT_ITEM, False),
        ]
        assert bay.scenes[CAPTURE][IMPACT]["sceneItemEnabled"] is True
        assert "not-printed" not in probe.report

    async def test_a_floor_without_the_item_says_how_to_add_it(self) -> None:
        bay = _RolesBay()

        probe = await self.probe(bay, source=TOP_IMPACT)

        assert not probe.ok
        assert "FAILED at GetSceneItemId" in probe.report
        assert "Add Existing" in probe.report
        assert f"scene item id {CAPTURE_IMPACT_ITEM}, visibility" in probe.report
        assert bay.toggles == []

    async def test_a_capture_scene_without_impact_is_reported(self) -> None:
        bay = _RolesBay()
        del bay.scenes[CAPTURE][IMPACT]

        probe = await self.probe(bay)

        assert not probe.ok
        assert "FAILED at GetSceneItemId (capture)" in probe.report
        assert "leave it enabled" in probe.report

    async def test_the_capture_scene_is_refused_as_a_floor(self) -> None:
        bay = _RolesBay()

        probe = await self.probe(bay, scene=CAPTURE)

        assert not probe.ok
        assert "REFUSED" in probe.report
        assert bay.toggles == [] and not bay.connected


async def test_gspro_is_not_needed_for_any_of_it(tmp_path) -> None:
    bay = _RolesBay()
    clock = Clock()
    reveal = a_reveal(bay, clock, capture_scene=CAPTURE)

    await reveal.on_shot_detected(an_impact("16"))
    await clock.release_one()
    published, _ = await a_burst(tmp_path, bay)
    await clock.release_one()
    await reveal.aclose()
    await asyncio.sleep(0)

    assert bay.on_floor(IMPACT) is False
    assert published, "capture ran from the strike alone"
