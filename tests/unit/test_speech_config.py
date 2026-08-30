"""That CaddieJ stays quiet, and stays private, unless it is told otherwise.

The default has to be safe, because the default is what runs on a fresh
checkout and on the machine of somebody who cloned this to look at the floor.
Two things make it safe here, and every test below is a way one of them could
stop being true by accident: nothing makes a noise in somebody's house without
being switched on, and nothing downloads a few hundred megabytes of speech model
without being switched on either.

The other half is the log. A coaching line is not a secret, but a log that quotes
every sentence CaddieJ says is a transcript of somebody's practice session, and
nothing needs one.
"""

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from caddiej.__main__ import _JsonFormatter
from caddiej.config import Settings
from caddiej.speech.factory import build_sink, build_synthesizer
from caddiej.speech.sink import NullSink

_CONTEXT_FIELDS = _JsonFormatter._CONTEXT_FIELDS


def settings(**overrides: Any) -> Settings:
    return Settings(**overrides)


# --- the default is silent -------------------------------------------------


def test_caddiej_says_nothing_until_it_is_told_to() -> None:
    # The single most important default in the subsystem: this one makes noise
    # in somebody's living room.
    assert settings().speech_enabled is False


def test_nothing_downloads_a_model_by_default() -> None:
    # Selecting an engine is not the same as running one. With speech off, no
    # synthesizer is built at all, so nothing reaches Hugging Face.
    config = settings()

    assert config.speech_enabled is False
    assert build_synthesizer("none", voice=config.speech_voice) is None


def test_the_default_voice_is_the_one_caddiej_ships_with() -> None:
    assert settings().speech_voice == "bm_george"


def test_the_default_engine_is_the_local_one() -> None:
    # Local matters: coaching text is about somebody's golf swing in their own
    # home, and synthesising it on the machine means it never leaves.
    assert settings().speech_engine == "kokoro"


def test_an_unknown_engine_leaves_the_room_quiet_rather_than_guessing() -> None:
    assert build_synthesizer("festival", voice="bm_george") is None


def test_an_unknown_output_leaves_the_room_quiet_rather_than_guessing() -> None:
    assert build_sink("esound") is None


def test_the_fake_engine_needs_no_model_and_no_network() -> None:
    # What makes the whole path reviewable on a machine with nothing installed.
    engine = build_synthesizer("fake", voice="bm_george")

    assert engine is not None
    assert engine.is_configured


def test_a_headless_machine_can_render_without_a_speaker() -> None:
    # Distinct from having no sink at all: every line is rendered and every
    # event published, and nothing is played.
    output = build_sink("none")

    assert isinstance(output, NullSink)
    assert output.is_configured


# --- the log ---------------------------------------------------------------


def test_no_log_field_carries_what_was_said() -> None:
    assert "speech_text" not in _CONTEXT_FIELDS
    assert not any("sentence" in field for field in _CONTEXT_FIELDS)


def test_a_spoken_line_passed_as_a_log_extra_is_dropped() -> None:
    formatter = _JsonFormatter()
    record = logging.LogRecord(
        name="caddiej.speech",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="speech_started",
        args=(),
        exc_info=None,
    )
    record.speech_text = "Square the face a little earlier."  # type: ignore[attr-defined]

    assert "Square the face" not in formatter.format(record)


def test_the_engine_the_voice_and_the_duration_are_loggable() -> None:
    # None of these is private, and without them a quiet room has no
    # explanation anywhere.
    for field in ("speech_engine", "speech_output", "speech_voice", "speech_seconds"):
        assert field in _CONTEXT_FIELDS


# --- the rest of the configuration ----------------------------------------


def test_the_voice_can_be_changed_without_touching_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPEECH_VOICE", "bf_emma")

    assert settings().speech_voice == "bf_emma"


def test_a_speaker_can_be_named_and_blank_means_the_system_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert settings().speech_device == ""

    monkeypatch.setenv("SPEECH_DEVICE", "Studio Speakers")
    assert settings().speech_device == "Studio Speakers"


def test_the_queue_is_bounded() -> None:
    config = settings()

    assert config.speech_queue_size >= 1
    # Short, like coaching's: a line about three swings ago is worse than
    # silence, so this is a queue of things still worth saying, not a backlog.
    assert config.speech_queue_size <= 8


def test_one_sentence_cannot_occupy_the_room_indefinitely() -> None:
    config = settings()

    assert config.speech_max_characters > 0
    assert 0 < config.speech_render_timeout <= 60


def test_the_cache_is_bounded_in_both_memory_and_files() -> None:
    config = settings()

    assert config.speech_cache_bytes > 0
    assert config.speech_cache_files >= 1


def test_the_cache_lives_under_the_media_root() -> None:
    # So that "what does CaddieJ put on my disk" still has one answer, and
    # deleting that directory is still enough.
    assert str(settings().speech_cache_root).startswith("media")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SPEECH_ENGINE", "elevenlabs"),
        ("SPEECH_OUTPUT", "pulseaudio"),
        ("SPEECH_VOICE", ""),
        ("SPEECH_SPEED", "0"),
        ("SPEECH_SPEED", "9"),
        ("SPEECH_QUEUE_SIZE", "0"),
        ("SPEECH_MAX_CHARACTERS", "0"),
        ("SPEECH_RENDER_TIMEOUT", "0"),
        ("SPEECH_CACHE_BYTES", "0"),
        ("SPEECH_CACHE_FILES", "0"),
    ],
)
def test_a_setting_that_would_break_speech_is_refused_at_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    # Better than starting and quietly never saying anything.
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        settings()
