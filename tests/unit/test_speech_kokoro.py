"""The Kokoro adapter, with a stub in place of the model.

Nothing here installs a speech engine, downloads weights, or imports a machine
learning framework. The pipeline is a few lines of Python that yields whatever a
test tells it to, which is what makes the interesting cases arrangeable: a model
that will not load, a voice from the wrong language, floats outside the legal
range, and the two shapes of result Kokoro has returned across its versions.

The adapter is also the only place in CaddieJ that knows any of this, so these
are the tests that would catch an upstream change.
"""

from typing import Any

import pytest

from caddiej.adapters.kokoro import synthesizer as adapter
from caddiej.adapters.kokoro.synthesizer import (
    SAMPLE_RATE,
    KokoroSynthesizer,
    language_of,
)
from caddiej.speech.audio import from_wav, to_wav
from caddiej.speech.synth import SynthesisFailedError, SynthesisUnavailableError


class StubPipeline:
    """Stands in for `KPipeline`: records the call, yields what it was told."""

    def __init__(
        self,
        chunks: tuple[Any, ...] = ((0.0, 0.5, -0.5),),
        results: tuple[Any, ...] | None = None,
        error: BaseException | None = None,
        lang_code: str | None = None,
    ) -> None:
        self.chunks = chunks
        self.results = results
        """Yielded exactly as given, for the shapes that are not the usual
        `(graphemes, phonemes, audio)` triple."""

        self.error = error
        self.lang_code = lang_code
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, text: str, voice: str, speed: float = 1.0) -> Any:
        self.calls.append((text, voice, speed))
        if self.error is not None:
            raise self.error
        if self.results is not None:
            return iter(self.results)
        # A generator, like the real one: the audio does not exist until it is
        # iterated, so a test that never iterates proves nothing was rendered.
        return (("graphemes", "phonemes", chunk) for chunk in self.chunks)


class StubModule:
    """Stands in for the `kokoro` package."""

    def __init__(
        self,
        pipeline: StubPipeline | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.pipeline = pipeline or StubPipeline()
        self.error = error
        self.built: list[str] = []

    def KPipeline(self, lang_code: str) -> StubPipeline:  # noqa: N802 - mirrors the SDK
        self.built.append(lang_code)
        if self.error is not None:
            raise self.error
        self.pipeline.lang_code = lang_code
        return self.pipeline


class Tensor:
    """Stands in for a torch tensor that still wants a gradient and a device."""

    def __init__(self, values: list[float]) -> None:
        self.values = values
        self.detached = False
        self.moved = False

    def detach(self) -> "Tensor":
        self.detached = True
        return self

    def cpu(self) -> "Tensor":
        self.moved = True
        return self

    def tolist(self) -> list[float]:
        if not (self.detached and self.moved):
            raise RuntimeError("read from a tensor that was still on a device")
        return self.values


class TestLanguages:
    @pytest.mark.parametrize(
        ("voice", "code"),
        [
            ("bm_george", "b"),
            ("bf_emma", "b"),
            ("af_heart", "a"),
            ("jm_kumo", "j"),
            ("zf_xiaoni", "z"),
        ],
    )
    def test_the_language_is_the_first_letter_of_the_voice(
        self, voice: str, code: str
    ) -> None:
        """Kokoro's own convention. A mismatched pipeline does not fail, it
        mispronounces, so this is worth pinning down."""

        assert language_of(voice) == code

    @pytest.mark.parametrize("voice", ["", "qm_nobody", "george", "1m_x"])
    def test_a_voice_from_no_known_language_is_recognised_as_such(
        self, voice: str
    ) -> None:
        assert language_of(voice) is None


class TestConfiguration:
    def test_an_installed_package_and_a_known_voice_is_configured(self) -> None:
        assert KokoroSynthesizer(module=StubModule()).is_configured is True

    def test_a_missing_package_is_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The supported state of a machine that never wanted a voice."""

        monkeypatch.setattr(adapter, "sdk", lambda: None)
        assert KokoroSynthesizer().is_configured is False

    def test_a_voice_nobody_recognises_is_not_configured(self) -> None:
        """Caught at start-up rather than mispronounced all evening."""

        engine = KokoroSynthesizer(voice="qm_nobody", module=StubModule())
        assert engine.is_configured is False

    def test_the_engine_states_its_sample_rate_before_it_has_run(self) -> None:
        """So a sink can be set up while nothing is waiting."""

        assert KokoroSynthesizer(module=StubModule()).sample_rate == SAMPLE_RATE

    def test_the_default_voice_is_the_one_caddiej_ships_with(self) -> None:
        assert KokoroSynthesizer(module=StubModule()).voice == "bm_george"


class TestLoading:
    async def test_the_pipeline_is_built_for_the_voices_own_language(self) -> None:
        module = StubModule()
        await KokoroSynthesizer(voice="bm_george", module=module).open()

        assert module.built == ["b"]

    async def test_an_american_voice_builds_an_american_pipeline(self) -> None:
        module = StubModule()
        await KokoroSynthesizer(voice="af_heart", module=module).open()

        assert module.built == ["a"]

    async def test_a_model_that_will_not_load_is_reported_as_unavailable(self) -> None:
        """No network on the first run, no weights, a broken framework
        underneath. All the same answer: no voice tonight."""

        module = StubModule(error=OSError("could not reach the model host"))
        engine = KokoroSynthesizer(module=module)

        with pytest.raises(SynthesisUnavailableError, match="would not load"):
            await engine.open()

    async def test_a_missing_package_cannot_be_opened(self) -> None:
        engine = KokoroSynthesizer(module=None)
        engine._module = None  # noqa: SLF001 - the machine with nothing installed

        with pytest.raises(SynthesisUnavailableError, match="no Kokoro pipeline"):
            await engine.open()

    async def test_loading_twice_builds_one_pipeline(self) -> None:
        module = StubModule()
        engine = KokoroSynthesizer(module=module)
        await engine.open()
        await engine.open()

        assert module.built == ["b"]


class TestRendering:
    async def test_the_text_the_voice_and_the_speed_all_reach_the_model(self) -> None:
        pipeline = StubPipeline()
        engine = KokoroSynthesizer(speed=1.25, module=StubModule(pipeline))
        await engine.open()
        await engine.render("Square the face.")

        assert pipeline.calls == [("Square the face.", "bm_george", 1.25)]

    async def test_an_utterance_may_name_its_own_voice(self) -> None:
        pipeline = StubPipeline()
        engine = KokoroSynthesizer(module=StubModule(pipeline))
        await engine.open()
        await engine.render("Anything.", voice="bf_emma")

        assert pipeline.calls[0][1] == "bf_emma"

    async def test_floats_become_signed_sixteen_bit_samples(self) -> None:
        pipeline = StubPipeline(chunks=((0.0, 1.0, -1.0, 0.5),))
        engine = KokoroSynthesizer(module=StubModule(pipeline))
        await engine.open()
        audio = await engine.render("Anything.")

        assert audio.sample_rate == SAMPLE_RATE
        assert audio.sample_width == 2
        assert audio.frame_count == 4
        assert audio.data == b"\x00\x00\xff\x7f\x01\x80\x00@"

    async def test_samples_outside_the_legal_range_are_clipped_not_wrapped(
        self,
    ) -> None:
        """Wrapping would turn a loud peak into the loudest possible click in
        the opposite direction, which is the worst noise a speaker can make."""

        pipeline = StubPipeline(chunks=((4.0, -4.0),))
        engine = KokoroSynthesizer(module=StubModule(pipeline))
        await engine.open()
        audio = await engine.render("Anything.")

        assert audio.data == b"\xff\x7f\x00\x80"

    async def test_every_chunk_of_a_long_sentence_ends_up_in_the_audio(self) -> None:
        """Kokoro splits on sentence boundaries and yields each piece, so
        keeping only the first would silently truncate anything long."""

        pipeline = StubPipeline(chunks=((0.5,) * 10, (0.25,) * 10, (0.0,) * 10))
        engine = KokoroSynthesizer(module=StubModule(pipeline))
        await engine.open()
        audio = await engine.render("Three sentences. Like this. And this.")

        assert audio.frame_count == 30

    async def test_a_tensor_is_detached_and_brought_back_before_it_is_read(
        self,
    ) -> None:
        tensor = Tensor([0.0, 0.5])
        pipeline = StubPipeline(chunks=(tensor,))
        engine = KokoroSynthesizer(module=StubModule(pipeline))
        await engine.open()
        audio = await engine.render("Anything.")

        assert tensor.detached and tensor.moved
        assert audio.frame_count == 2

    async def test_a_named_result_object_works_as_well_as_a_tuple(self) -> None:
        """Both shapes are in the wild across Kokoro's versions, so the adapter
        asks for the attribute and falls back to the position."""

        class Result:
            audio = (0.0, 0.5, 1.0)

        engine = KokoroSynthesizer(module=StubModule(StubPipeline(results=(Result(),))))
        await engine.open()
        audio = await engine.render("Anything.")

        assert audio.frame_count == 3

    async def test_a_result_holding_no_audio_is_a_failure_rather_than_a_crash(
        self,
    ) -> None:
        engine = KokoroSynthesizer(
            module=StubModule(StubPipeline(results=("not a result at all",)))
        )
        await engine.open()

        with pytest.raises(SynthesisFailedError):
            await engine.render("Anything.")

    async def test_a_model_that_throws_becomes_a_neutral_failure(self) -> None:
        """The service reacts to `SynthesisError`, not to whatever a machine
        learning framework decided to raise."""

        pipeline = StubPipeline(error=RuntimeError("CUDA ran out of memory"))
        engine = KokoroSynthesizer(module=StubModule(pipeline))
        await engine.open()

        with pytest.raises(SynthesisFailedError, match="CUDA ran out of memory"):
            await engine.render("Anything.")

    async def test_rendering_before_the_model_is_loaded_says_so(self) -> None:
        engine = KokoroSynthesizer(module=StubModule())

        with pytest.raises(SynthesisUnavailableError, match="before the model"):
            await engine.render("Anything.")

    async def test_what_comes_out_can_be_written_as_a_wav_and_read_back(self) -> None:
        """The whole point of the conversion: a rendered line is a file."""

        pipeline = StubPipeline(chunks=((0.1, 0.2, 0.3, 0.4),))
        engine = KokoroSynthesizer(module=StubModule(pipeline))
        await engine.open()
        audio = await engine.render("Anything.")

        assert from_wav(to_wav(audio)) == audio


class TestClosing:
    async def test_closing_lets_go_of_the_model(self) -> None:
        engine = KokoroSynthesizer(module=StubModule())
        await engine.open()
        await engine.aclose()

        with pytest.raises(SynthesisUnavailableError):
            await engine.render("Anything.")
