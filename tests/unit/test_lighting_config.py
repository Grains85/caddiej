"""That the defaults are safe, and that a bridge key cannot reach a log.

Lighting is the one feature that changes something outside CaddieJ which
somebody else set deliberately, so "off unless asked" is a property worth
asserting rather than assuming.
"""

import logging

import pytest
from pydantic import ValidationError

from caddiej.__main__ import _JsonFormatter
from caddiej.config import Settings
from caddiej.lighting.factory import build_lights
from caddiej.lighting.port import NullLights


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


class TestTheDefaultsAreSafe:
    def test_lighting_is_off_until_somebody_asks_for_it(self) -> None:
        assert settings().lighting_enabled is False

    def test_no_bridge_and_no_key_are_configured_out_of_the_box(self) -> None:
        resolved = settings()

        assert resolved.lighting_bridge_host == ""
        assert resolved.lighting_application_key.get_secret_value() == ""

    def test_the_default_reaction_is_a_dip_of_less_than_a_fifth(self) -> None:
        resolved = settings()

        assert resolved.lighting_maximum == 1.0
        assert 0.8 <= resolved.lighting_minimum < 0.9

    def test_the_default_command_budget_is_philips_own_guidance(self) -> None:
        assert settings().lighting_max_commands_per_second == 10.0

    def test_a_key_is_a_secret_and_does_not_appear_when_printed(self) -> None:
        resolved = settings(LIGHTING_APPLICATION_KEY="the-real-key")

        assert "the-real-key" not in repr(resolved)
        assert "the-real-key" not in str(resolved.lighting_application_key)
        assert resolved.lighting_application_key.get_secret_value() == "the-real-key"


class TestRefusingNonsense:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("LIGHTING_OUTPUT", "lifx"),
            ("LIGHTING_MINIMUM", 0.0),
            ("LIGHTING_MINIMUM", -1.0),
            ("LIGHTING_MAXIMUM", 5.0),
            ("LIGHTING_MAX_COMMANDS_PER_SECOND", 0),
            ("LIGHTING_MAX_COMMANDS_PER_SECOND", 500),
            ("LIGHTING_TRANSITION_SCALE", 0.0),
            ("LIGHTING_FAILURE_LIMIT", 0),
            ("LIGHTING_COMMAND_TIMEOUT", 0),
        ],
    )
    def test_an_impossible_value_is_refused_at_startup(
        self, field: str, value: object
    ) -> None:
        with pytest.raises(ValidationError):
            settings(**{field: value})

    def test_both_supported_outputs_are_accepted(self) -> None:
        for output in ("none", "hue"):
            assert settings(LIGHTING_OUTPUT=output).lighting_output == output


class TestTheFactory:
    def test_none_gives_lights_that_change_nothing(self) -> None:
        assert isinstance(build_lights("none"), NullLights)

    def test_an_unknown_output_is_refused_rather_than_guessed_at(self) -> None:
        assert build_lights("lifx") is None

    def test_hue_is_built_from_an_address_a_key_and_a_list_of_lamps(self) -> None:
        lights = build_lights(
            "hue",
            bridge_host="10.0.0.5",
            application_key="k",
            lights="Sitting room, Kitchen",
        )

        assert lights is not None
        assert lights.name == "hue"

    def test_a_blank_light_list_means_every_light(self) -> None:
        from caddiej.lighting.factory import _named

        assert _named("") == ()
        assert _named("  ,  ") == ()
        assert _named("Kitchen, Sitting room") == ("Kitchen", "Sitting room")


class TestNothingCanLogABridgeKey:
    def test_the_allowlist_has_no_field_that_could_carry_one(self) -> None:
        fields = _JsonFormatter._CONTEXT_FIELDS

        assert not [
            field
            for field in fields
            if "key" in field or "secret" in field or "password" in field
        ]

    def test_the_lighting_fields_a_log_may_carry_are_labels_and_numbers(self) -> None:
        expected = {
            "lighting_output",
            "lighting_bridge",
            "lighting_lights",
            "lighting_named",
            "lighting_missing",
            "lighting_cue",
            "lighting_minimum",
            "lighting_maximum",
            "lighting_cooldown_seconds",
        }

        assert expected <= set(_JsonFormatter._CONTEXT_FIELDS)

    def test_a_key_passed_through_extra_is_dropped_by_the_formatter(self) -> None:
        """The formatter fails closed, so a future caller cannot leak a
        credential into the logs by inventing a field name."""

        record = logging.LogRecord(
            name="caddiej.lighting",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="lighting_ready",
            args=(),
            exc_info=None,
        )
        record.lighting_application_key = "the-real-key"  # type: ignore[attr-defined]
        record.lighting_output = "hue"  # type: ignore[attr-defined]

        rendered = _JsonFormatter().format(record)

        assert "the-real-key" not in rendered
        assert '"lighting_output": "hue"' in rendered
