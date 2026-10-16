"""The first-hardware profile must load, stay secret-free, and stay minimal."""

import re
from pathlib import Path

from caddiej.cli import describe
from caddiej.config import Settings

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "config" / "examples" / "hardware-validation.env"


def _assigned() -> dict[str, str]:
    pattern = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$", re.MULTILINE)
    return dict(pattern.findall(PROFILE.read_text(encoding="utf-8")))


def _load(monkeypatch) -> Settings:
    for name in _assigned():
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=PROFILE)


def test_the_hardware_profile_is_valid(monkeypatch) -> None:
    settings = _load(monkeypatch)

    assert settings.gspro_mode == "interpose"
    assert settings.gspro_bind_host == "127.0.0.1"
    assert settings.gspro_bind_port == 9210
    assert settings.gspro_upstream_host == "127.0.0.1"
    assert settings.gspro_upstream_port == 921
    assert settings.gspro_idle_timeout == 180.0
    assert "GSPRO_IDLE_TIMEOUT" not in _assigned()
    assert settings.history_enabled is True
    assert settings.history_record_raw_payloads is True
    assert settings.history_raw_payload_max_bytes == 8192
    assert settings.hud_host == "127.0.0.1"
    assert settings.hud_port == 8730
    assert settings.floor_demo is False
    assert settings.floor_width_m == 1.0
    assert settings.floor_depth_m == 1.0
    assert settings.floor_tee_x_m == 0.5
    assert settings.floor_tee_y_m == 0.72
    assert settings.floor_club_path_length == 1.2
    assert settings.video_enabled is False
    assert settings.coaching_provider == "none"
    assert settings.speech_enabled is False
    assert settings.lighting_enabled is False
    assert settings.effects_enabled is False
    assert settings.assistant_api_enabled is False
    assert settings.obs_provision_floor_source is False
    assert settings.floor_shot_video_enabled is False


def test_the_hardware_profile_sets_only_known_variables() -> None:
    aliases = {
        field.validation_alias
        for field in Settings.model_fields.values()
        if isinstance(field.validation_alias, str)
    }
    invented = sorted(set(_assigned()) - aliases)

    assert not invented, f"hardware profile sets unknown variables: {invented}"


def test_the_hardware_profile_holds_no_secrets_or_player_name(monkeypatch) -> None:
    text = PROFILE.read_text(encoding="utf-8")

    assert "OBS_PASSWORD=" not in text
    assert "COACHING_API_KEY=" not in text
    assert "LIGHTING_APPLICATION_KEY=" not in text
    assert "ASSISTANT_API_TOKEN=" not in text
    assert "PLAYER_NAME=" not in text
    report = describe(_load(monkeypatch))
    assert "listening on 127.0.0.1:9210" in report
    assert "forwarding to 127.0.0.1:921" in report
    assert "off (VIDEO_ENABLED=false)" in report
    assert "off (SPEECH_ENABLED=false)" in report
    assert "off (LIGHTING_ENABLED=false)" in report
    assert "off (EFFECTS_ENABLED=false)" in report
    assert "off (ASSISTANT_API_ENABLED=false)" in report
    assert "Configuration is valid." in report
