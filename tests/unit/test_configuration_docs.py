"""Configuration that nobody documented is configuration nobody can use.

Every setting has three homes: the model that validates it, `.env.example` where
the reasoning for its default lives, and the reference table somebody reads when
they are looking for it. Adding a setting to one of the three and forgetting the
others is the easiest mistake in the project to make and the hardest to notice,
because everything still works — on the machine of the person who added it.

The same reasoning as the logging allowlist test: a rule stated in a document is
a rule until somebody is in a hurry.
"""

import re
from pathlib import Path

import pytest

from caddiej.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"
REFERENCE = ROOT / "docs" / "configuration.md"


def settings_aliases() -> set[str]:
    aliases = set()
    for field in Settings.model_fields.values():
        alias = field.validation_alias
        assert isinstance(alias, str), "every setting is read from one named variable"
        aliases.add(alias)
    return aliases


def assigned_in_env_example() -> set[str]:
    """Variables `.env.example` actually sets, ignoring commented-out examples."""

    pattern = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)
    return set(pattern.findall(ENV_EXAMPLE.read_text()))


def documented_in_reference() -> set[str]:
    """Variables the reference names in a table row or a heading."""

    pattern = re.compile(r"`([A-Z][A-Z0-9_]{2,})`")
    return set(pattern.findall(REFERENCE.read_text()))


class TestEverySettingIsDocumented:
    def test_env_example_sets_every_setting(self) -> None:
        missing = sorted(settings_aliases() - assigned_in_env_example())

        assert not missing, f"settings with no line in .env.example: {missing}"

    def test_the_reference_names_every_setting(self) -> None:
        missing = sorted(settings_aliases() - documented_in_reference())

        assert not missing, f"settings missing from docs/configuration.md: {missing}"

    def test_the_documents_invent_nothing(self) -> None:
        # The other direction: a variable that was renamed or removed leaves a
        # line behind that sends somebody to set something CaddieJ ignores.
        invented = sorted(assigned_in_env_example() - settings_aliases())

        assert not invented, f".env.example sets variables CaddieJ ignores: {invented}"


class TestTheSampleHoldsNoCredentials:
    @pytest.mark.parametrize(
        "variable",
        [
            "OBS_PASSWORD",
            "COACHING_API_KEY",
            "LIGHTING_APPLICATION_KEY",
            "ASSISTANT_API_TOKEN",
        ],
    )
    def test_every_credential_is_blank(self, variable: str) -> None:
        # `.env.example` is committed. A value here is a value in the history of
        # every clone, for ever.
        line = next(
            row
            for row in ENV_EXAMPLE.read_text().splitlines()
            if row.startswith(f"{variable}=")
        )

        assert line == f"{variable}=", f"{variable} must ship blank"

    def test_the_sample_loads_and_starts_nothing(self, tmp_path) -> None:
        # It must also be usable as-is: copying it to .env and running CaddieJ
        # is the documented first step, and it should validate and touch nothing.
        settings = Settings(_env_file=ENV_EXAMPLE)

        assert settings.gspro_mode == "off"
        assert settings.video_enabled is False
        assert settings.speech_enabled is False
        assert settings.lighting_enabled is False
        assert settings.assistant_api_enabled is False
        assert settings.coaching_provider == "none"
