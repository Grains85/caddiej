"""That CaddieJ cannot spend money, or leak a key, without being told to.

The default has to be safe, because the default is what runs on a fresh
checkout, in CI, and on the machine of somebody who cloned this to look at the
floor. Every test here is a way that could stop being true by accident.

The other half is the key itself: it comes from the environment, it is a
`SecretStr` so a traceback cannot print it, and the log formatter has no field
that could carry it.
"""

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from caddiej.__main__ import _JsonFormatter
from caddiej.coaching.factory import build_provider
from caddiej.config import Settings

_CONTEXT_FIELDS = _JsonFormatter._CONTEXT_FIELDS


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


# --- the default is offline -----------------------------------------------


def test_nothing_asks_a_model_until_it_is_told_to() -> None:
    # The single most important default in the subsystem.
    assert settings().coaching_provider == "none"


def test_the_default_provider_is_no_provider() -> None:
    assert build_provider(kind="none", model="gpt-5-mini") is None


def test_coaching_itself_is_on_by_default() -> None:
    # Because the deterministic tier costs nothing and is the thing that puts a
    # fault on the floor. "Off" would make a fresh checkout look broken.
    assert settings().coaching_enabled is True


def test_there_is_no_key_in_the_repository() -> None:
    assert settings().coaching_api_key.get_secret_value() == ""


def test_a_default_configuration_cannot_reach_a_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Belt and braces for the requirement that no test spends anything: with the
    # defaults there is nothing that could make a call.
    config = settings()
    provider = build_provider(
        kind=config.coaching_provider,
        model=config.coaching_model,
        api_key=config.coaching_api_key.get_secret_value(),
        api_base=config.coaching_api_base,
    )

    assert provider is None


def test_the_demo_provider_needs_no_key_and_no_network() -> None:
    # What makes the whole path reviewable on a machine with no credentials.
    provider = build_provider(kind="scripted", model="ignored")

    assert provider is not None
    assert provider.is_configured
    assert provider.name == "demo"


def test_an_unknown_provider_coaches_from_templates_rather_than_guessing() -> None:
    assert build_provider(kind="anthropic", model="claude") is None


def test_asking_for_openai_without_a_key_produces_an_unconfigured_provider() -> None:
    # It exists, so health can say why it is not being used, and it will not be
    # asked anything.
    provider = build_provider(kind="openai", model="gpt-test", api_key="")

    assert provider is not None
    assert not provider.is_configured


# --- the key ---------------------------------------------------------------


def test_the_key_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COACHING_API_KEY", "sk-not-a-real-key")

    assert settings().coaching_api_key.get_secret_value() == "sk-not-a-real-key"


def test_the_key_does_not_appear_when_the_settings_are_printed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A `Settings` ends up in a debug log or a traceback sooner or later.
    monkeypatch.setenv("COACHING_API_KEY", "sk-not-a-real-key")

    assert "sk-not-a-real-key" not in repr(settings())
    assert "sk-not-a-real-key" not in str(settings())


def test_no_log_field_could_carry_a_key() -> None:
    # The formatter copies an allowlist rather than everything on the record,
    # so a stray `extra` cannot put a secret in the log.
    assert not any("key" in field or "secret" in field for field in _CONTEXT_FIELDS)


def test_a_secret_passed_as_a_log_extra_is_dropped() -> None:
    formatter = _JsonFormatter()
    record = logging.LogRecord(
        name="caddiej.coaching",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="coaching_ready",
        args=(),
        exc_info=None,
    )
    record.coaching_api_key = "sk-not-a-real-key"  # type: ignore[attr-defined]

    assert "sk-not-a-real-key" not in formatter.format(record)


def test_the_provider_and_model_are_loggable_because_neither_is_secret() -> None:
    assert "coaching_provider" in _CONTEXT_FIELDS
    assert "coaching_model" in _CONTEXT_FIELDS


# --- the rest of the configuration ----------------------------------------


def test_the_model_can_be_changed_without_touching_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COACHING_MODEL", "gpt-4o-mini")

    assert settings().coaching_model == "gpt-4o-mini"


def test_an_endpoint_can_be_pointed_somewhere_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A local OpenAI-compatible server, which is a real way to run this.
    monkeypatch.setenv("COACHING_API_BASE", "http://localhost:11434/v1")

    assert settings().coaching_api_base == "http://localhost:11434/v1"


def test_a_provider_nobody_has_written_is_refused_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Better than starting and quietly never coaching.
    monkeypatch.setenv("COACHING_PROVIDER", "gemini")

    with pytest.raises(ValidationError):
        settings()


def test_an_empty_model_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COACHING_MODEL", "")

    with pytest.raises(ValidationError):
        settings()


def test_the_queue_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    config = settings()

    assert config.coaching_queue_size >= 1
    # Short on purpose. Advice for three swings ago is worse than silence.
    assert config.coaching_queue_size <= 4


def test_there_is_a_timeout_and_it_is_shorter_than_a_golfer_s_patience() -> None:
    config = settings()

    assert 0 < config.coaching_timeout <= 30


def test_a_session_has_a_ceiling_on_what_it_can_spend() -> None:
    config = settings()

    assert config.coaching_call_limit > 0
    assert config.coaching_max_output_tokens > 0


def test_frames_are_not_sent_unless_asked_for() -> None:
    # Images cost more than text and are the easy way to make an evening
    # expensive without noticing.
    assert settings().coaching_send_frames is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("COACHING_QUEUE_SIZE", "0"),
        ("COACHING_TIMEOUT", "0"),
        ("COACHING_CALL_LIMIT", "-1"),
        ("COACHING_SEVERITY_FINE", "2.0"),
    ],
)
def test_a_setting_that_would_break_coaching_is_refused_at_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        settings()
