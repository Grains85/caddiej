"""Configuration and wiring for the effects subsystem.

Two things worth pinning: which outputs are on by default and why, and that the
whole thing assembles from a `Settings` into something that starts and stops
cleanly without any of its four outputs existing.
"""

import asyncio

from caddiej.__main__ import _JsonFormatter
from caddiej.app import _build_effects, _build_lighting, _build_speech
from caddiej.config import Settings
from caddiej.domain.scoring import HoleResult
from caddiej.effects.factory import build_effect_director, obs_clients
from caddiej.floor.service import FloorService


def a_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


class TestTheDefaults:
    def test_effects_are_on_because_the_floor_is_caddiejs_own_projector(self) -> None:
        """Unlike the lights, which belong to the house, and OBS, which belongs
        to a scene somebody built. Marking a birdie on CaddieJ's own surface is
        the product working rather than a liberty being taken."""

        settings = a_settings()

        assert settings.effects_enabled is True
        assert settings.effects_floor is True
        assert settings.effects_speech is True
        assert settings.effects_lights is True

    def test_obs_is_off_and_unconfigured(self) -> None:
        """It shows a source in a scene somebody built, and CaddieJ creates
        neither. With nothing there to show, the honest default is not to try."""

        settings = a_settings()

        assert settings.effects_obs is False
        assert settings.effects_obs_scene == ""
        assert settings.effects_obs_source == ""
        assert settings.effects_obs_text_source == ""

    def test_there_is_a_cap_on_how_long_anything_can_hold_the_room(self) -> None:
        settings = a_settings()

        assert settings.effects_max_seconds > 0
        assert settings.effects_channel_timeout > 0

    def test_every_switch_reads_its_own_environment_variable(self) -> None:
        settings = a_settings(
            EFFECTS_ENABLED=False,
            EFFECTS_FLOOR=False,
            EFFECTS_SPEECH=False,
            EFFECTS_LIGHTS=False,
            EFFECTS_OBS=True,
            EFFECTS_OBS_SCENE="Round",
            EFFECTS_OBS_SOURCE="Stinger",
            EFFECTS_OBS_TEXT_SOURCE="Caption",
            EFFECTS_MAX_SECONDS=4.0,
            EFFECTS_CHANNEL_TIMEOUT=1.0,
        )

        assert settings.effects_enabled is False
        assert settings.effects_floor is False
        assert settings.effects_speech is False
        assert settings.effects_lights is False
        assert settings.effects_obs is True
        assert settings.effects_obs_scene == "Round"
        assert settings.effects_obs_source == "Stinger"
        assert settings.effects_obs_text_source == "Caption"
        assert settings.effects_max_seconds == 4.0
        assert settings.effects_channel_timeout == 1.0


class TestAssembly:
    def test_it_builds_from_settings_with_none_of_its_outputs_running(self) -> None:
        """The default machine: no bridge, no speaker, no OBS. Every channel is
        still constructed, because a channel that is off still gets reset."""

        settings = a_settings()
        floor = FloorService()
        director = _build_effects(
            settings,
            floor,
            _build_speech(settings, _nowhere),
            _build_lighting(settings),
        )

        # The floor is the one output that cannot fail, so it alone is enough to
        # make the subsystem worth running.
        assert director.is_available is True
        assert director.health().channels == ("floor",)

    def test_switching_the_floor_off_leaves_nothing_available(self) -> None:
        settings = a_settings(EFFECTS_FLOOR=False)
        director = _build_effects(
            settings,
            FloorService(),
            _build_speech(settings, _nowhere),
            _build_lighting(settings),
        )

        assert director.is_available is False

    def test_asking_for_obs_without_a_factory_is_reported_not_guessed(self) -> None:
        director = build_effect_director(
            floor=FloorService(),
            speech=_build_speech(a_settings(), _nowhere),
            lighting=_build_lighting(a_settings()),
            use_obs=True,
            obs_client_factory=None,
        )

        assert "obs" not in director.health().channels

    def test_a_client_factory_makes_a_fresh_client_every_time(self) -> None:
        """The channel throws a client away after a failure, so it has to be able
        to make another."""

        make = obs_clients(
            host="127.0.0.1", port=4455, password="", connect_timeout=1.0
        )

        assert make() is not make()

    async def test_it_closes_cleanly_having_never_played_anything(self) -> None:
        settings = a_settings()
        director = _build_effects(
            settings,
            FloorService(),
            _build_speech(settings, _nowhere),
            _build_lighting(settings),
        )

        await director.aclose()

        assert director.is_idle

    async def test_a_celebration_reaches_the_floor_through_the_whole_wiring(
        self,
    ) -> None:
        """The one path that proves the assembly rather than the construction."""

        settings = a_settings(EFFECTS_MAX_SECONDS=0.05)
        floor = FloorService()
        director = _build_effects(
            settings,
            floor,
            _build_speech(settings, _nowhere),
            _build_lighting(settings),
        )

        assert await director.play(HoleResult.BIRDIE) is True
        assert floor.state.flourish is None, "the task has not run yet"

        for _ in range(200):
            if director.is_idle:
                break
            await asyncio.sleep(0.01)

        await director.aclose()

        assert floor.state.flourish is None, "and it was put back"
        assert director.health().plays == 1
        assert director.health().resets == 1


class TestTheLogAllowlist:
    def test_the_effects_fields_a_log_may_carry_are_labels_and_numbers(self) -> None:
        expected = {
            "effect_cue",
            "effect_playing",
            "effect_priority",
            "effect_seconds",
            "effect_channel",
            "effect_phase",
        }

        assert expected <= set(_JsonFormatter._CONTEXT_FIELDS)

    def test_the_label_is_not_logged(self) -> None:
        """It is a restatement of the cue, so a line that says both says one
        thing twice."""

        assert "effect_label" not in _JsonFormatter._CONTEXT_FIELDS


async def _nowhere(event: object) -> None:
    """A publisher that goes nowhere. Nothing under test here reads the bus."""

    return None
