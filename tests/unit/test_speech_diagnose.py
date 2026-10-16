"""`caddiej --check-speech`, with the voice and speaker replaced by fakes.

The diagnostic exists to prove the production path works on a real machine. The
tests exist to prove it names the step that failed, without a model download or
a sound card. Every synthesizer and sink here is a fake; the factories the
diagnostic calls in production are the same ones a normal run uses, and that
is asserted separately rather than exercised against Kokoro.
"""

from __future__ import annotations

import logging
from base64 import b64encode

import pytest

from caddiej.speech.audio import Audio, silence
from caddiej.speech.diagnose import CHECK_PHRASE, probe_speech
from caddiej.speech.fakes import FakeSynthesizer, RecordingSink
from caddiej.speech.sink import PlaybackError, PlaybackUnavailableError
from caddiej.speech.synth import SynthesisFailedError, SynthesisUnavailableError

pytestmark = pytest.mark.asyncio


def a_probe(**options: object):
    defaults: dict[str, object] = {
        "enabled": True,
        "engine": "kokoro",
        "output": "sounddevice",
        "voice": "bm_george",
        "synthesizer_factory": lambda: FakeSynthesizer(voice_name="bm_george"),
        "sink_factory": lambda: RecordingSink(),
    }
    defaults.update(options)
    return probe_speech(**defaults)  # type: ignore[arg-type]


async def test_it_speaks_the_check_line_through_the_configured_voice() -> None:
    engine = FakeSynthesizer(voice_name="bm_george")
    sink = RecordingSink()

    probe = await a_probe(synthesizer_factory=lambda: engine, sink_factory=lambda: sink)

    assert probe.ok
    assert engine.opened == 1
    assert engine.closed == 1
    assert engine.calls == [(CHECK_PHRASE, "bm_george")]
    assert len(sink.played) == 1
    assert sink.closed == 1
    assert "engine ready" in probe.report
    assert "played" in probe.report
    assert "CaddieJ can speak" in probe.report


async def test_speech_that_is_switched_off_is_refused() -> None:
    engine = FakeSynthesizer()
    sink = RecordingSink()

    probe = await a_probe(
        enabled=False,
        synthesizer_factory=lambda: engine,
        sink_factory=lambda: sink,
    )

    assert not probe.ok
    assert "FAILED at enabled" in probe.report
    assert "SPEECH_ENABLED" in probe.report
    assert engine.opened == 0
    assert sink.played == []


async def test_an_engine_set_to_none_is_refused() -> None:
    probe = await a_probe(engine="none")

    assert not probe.ok
    assert "FAILED at engine" in probe.report
    assert "SPEECH_ENGINE is none" in probe.report


async def test_an_output_that_plays_nothing_is_refused() -> None:
    """`SPEECH_OUTPUT=none` is a supported way to run CaddieJ, and the opposite
    of what this check is for."""

    probe = await a_probe(output="none")

    assert not probe.ok
    assert "FAILED at output" in probe.report
    assert "SPEECH_OUTPUT is none" in probe.report


async def test_an_unknown_engine_is_named() -> None:
    probe = await a_probe(engine="mystery", synthesizer_factory=lambda: None)

    assert not probe.ok
    assert "FAILED at engine" in probe.report
    assert "mystery" in probe.report


async def test_a_missing_engine_package_is_named() -> None:
    probe = await a_probe(synthesizer_factory=lambda: FakeSynthesizer(configured=False))

    assert not probe.ok
    assert "FAILED at engine" in probe.report
    assert "[tts]" in probe.report


async def test_a_missing_output_package_is_named() -> None:
    engine = FakeSynthesizer()

    probe = await a_probe(
        synthesizer_factory=lambda: engine,
        sink_factory=lambda: RecordingSink(configured=False),
    )

    assert not probe.ok
    assert "FAILED at output" in probe.report
    assert engine.closed == 1


async def test_a_voice_that_will_not_load_is_named() -> None:
    probe = await a_probe(
        synthesizer_factory=lambda: FakeSynthesizer(
            open_error=SynthesisUnavailableError("no weights")
        )
    )

    assert not probe.ok
    assert "FAILED at engine open" in probe.report
    assert "no weights" in probe.report
    assert "[tts]" in probe.report


async def test_a_voice_that_never_finishes_loading_is_cut_off() -> None:
    probe = await a_probe(
        open_timeout_seconds=0.02,
        synthesizer_factory=lambda: FakeSynthesizer(open_delay=1.0),
    )

    assert not probe.ok
    assert "FAILED at engine open" in probe.report
    assert "did not load" in probe.report


async def test_a_speaker_that_will_not_open_is_named() -> None:
    probe = await a_probe(
        sink_factory=lambda: RecordingSink(
            open_error=PlaybackUnavailableError("no usable audio output")
        )
    )

    assert not probe.ok
    assert "FAILED at output open" in probe.report
    assert "no usable audio output" in probe.report


async def test_a_render_that_fails_leaves_the_speaker_untouched() -> None:
    sink = RecordingSink()

    probe = await a_probe(
        synthesizer_factory=lambda: FakeSynthesizer(
            error=SynthesisFailedError("the voice broke")
        ),
        sink_factory=lambda: sink,
    )

    assert not probe.ok
    assert "FAILED at synthesis" in probe.report
    assert sink.played == []
    assert sink.started == []


async def test_a_render_that_never_returns_is_cut_off() -> None:
    probe = await a_probe(
        render_timeout_seconds=0.02,
        synthesizer_factory=lambda: FakeSynthesizer(delay=1.0),
    )

    assert not probe.ok
    assert "FAILED at synthesis" in probe.report


async def test_silence_from_the_engine_is_not_a_working_voice() -> None:
    class EmptyVoice(FakeSynthesizer):
        async def render(self, text: str, voice: str | None = None) -> Audio:
            await super().render(text, voice)
            return silence(0)

    sink = RecordingSink()
    probe = await a_probe(
        synthesizer_factory=lambda: EmptyVoice(), sink_factory=lambda: sink
    )

    assert not probe.ok
    assert "FAILED at synthesis" in probe.report
    assert sink.played == []


async def test_a_speaker_that_fails_mid_sentence_is_named() -> None:
    probe = await a_probe(
        sink_factory=lambda: RecordingSink(error=PlaybackError("the speaker went away"))
    )

    assert not probe.ok
    assert "FAILED at playback" in probe.report
    assert "the speaker went away" in probe.report


async def test_it_closes_the_voice_and_the_speaker_after_a_failure() -> None:
    engine = FakeSynthesizer(error=SynthesisFailedError("broke"))
    sink = RecordingSink()

    await a_probe(synthesizer_factory=lambda: engine, sink_factory=lambda: sink)

    assert engine.closed == 1
    assert sink.closed == 1


async def test_the_production_factory_is_what_builds_the_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an injected factory this is the same construction a normal run
    uses. That is the whole point of the check."""

    built: list[tuple[str, str, float]] = []
    engine = FakeSynthesizer()
    sink = RecordingSink()

    def synthesizer(kind: str, voice: str, speed: float = 1.0) -> FakeSynthesizer:
        built.append((kind, voice, speed))
        return engine

    def output(kind: str, device: str = "") -> RecordingSink:
        built.append((kind, device, 0.0))
        return sink

    monkeypatch.setattr("caddiej.speech.diagnose.build_synthesizer", synthesizer)
    monkeypatch.setattr("caddiej.speech.diagnose.build_sink", output)

    probe = await probe_speech(
        enabled=True,
        engine="kokoro",
        output="sounddevice",
        voice="bm_george",
        speed=1.1,
        device="",
    )

    assert probe.ok
    assert built[0] == ("kokoro", "bm_george", 1.1)
    assert built[1][0] == "sounddevice"
    assert engine.calls == [(CHECK_PHRASE, "bm_george")]


async def test_audio_bytes_never_reach_the_report_or_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = b"PCM-not-really-but-recognisable"
    engine = FakeSynthesizer()

    async def render(text: str, voice: str | None = None) -> Audio:
        engine.calls.append((text, voice or engine.voice_name))
        return Audio(data=marker * 8)

    engine.render = render  # type: ignore[method-assign]
    sink = RecordingSink()

    with caplog.at_level(logging.DEBUG):
        probe = await a_probe(
            synthesizer_factory=lambda: engine, sink_factory=lambda: sink
        )

    assert probe.ok
    logged = "\n".join(record.getMessage() for record in caplog.records)
    logged += repr([record.__dict__ for record in caplog.records])
    assert "PCM-not-really" not in probe.report
    assert "PCM-not-really" not in logged
    assert b64encode(marker).decode()[:12] not in logged
    assert b64encode(marker).decode()[:12] not in probe.report
