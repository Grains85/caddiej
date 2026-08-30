"""The audio sink, with a stub in place of PortAudio.

Nothing here opens a sound card. The stream is a handful of Python that records
what it was told to do, which is the only way to assert the thing that actually
matters: that cutting a sentence short throws the buffered audio away rather
than playing it out, and that `play` does not return until the device has
genuinely gone quiet.

That last point is the whole reason this adapter exists in the shape it does.
The service starts the next sentence the moment `play` returns, so a tail still
draining would be two voices at once.
"""

import asyncio
import threading
from typing import Any

import pytest

from caddiej.adapters.audio import sink as adapter
from caddiej.adapters.audio.sink import SoundDeviceSink
from caddiej.speech.audio import Audio, silence
from caddiej.speech.sink import PlaybackError, PlaybackUnavailableError

# Long enough to be several chunks, so "some of it played" is a real state.
A_SENTENCE = silence(0.5)


class StubStream:
    """Stands in for `RawOutputStream`, and remembers everything."""

    def __init__(self, **options: Any) -> None:
        self.options = options
        self.writes: list[bytes] = []
        self.started = 0
        self.stopped = 0
        self.aborted = 0
        self.closed = 0
        self.error: BaseException | None = None
        self.progress = threading.Event()
        """Set once a chunk has reached the device, so a test can wait for
        playback to be genuinely underway rather than sleeping and hoping."""

        self.gate: threading.Event | None = None
        """When set, each write blocks after handing its chunk over. This is how
        a test holds a sentence exactly partway through."""

    def start(self) -> None:
        self.started += 1

    def write(self, data: Any) -> None:
        if self.error is not None:
            raise self.error
        self.writes.append(bytes(data))
        self.progress.set()
        if self.gate is not None:
            # Bounded, so a test that forgets to release it fails rather than
            # leaving a thread behind for the rest of the run.
            self.gate.wait(timeout=2.0)

    def stop(self) -> None:
        self.stopped += 1

    def abort(self) -> None:
        self.aborted += 1

    def close(self) -> None:
        self.closed += 1


class StubModule:
    """Stands in for the `sounddevice` package."""

    def __init__(
        self,
        stream: StubStream | None = None,
        check_error: BaseException | None = None,
    ) -> None:
        self.stream = stream or StubStream()
        self.check_error = check_error
        self.checks: list[dict[str, Any]] = []

    def check_output_settings(self, **options: Any) -> None:
        self.checks.append(options)
        if self.check_error is not None:
            raise self.check_error

    def RawOutputStream(self, **options: Any) -> StubStream:  # noqa: N802 - mirrors the SDK
        self.stream.options = options
        return self.stream


async def until(flag: threading.Event, timeout: float = 2.0) -> None:
    """Wait for something a worker thread will set."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if flag.is_set():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("the worker thread never got there")


class TestConfiguration:
    def test_a_missing_package_is_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter, "sdk", lambda: None)
        assert SoundDeviceSink().is_configured is False

    def test_an_installed_package_is_configured(self) -> None:
        assert SoundDeviceSink(module=StubModule()).is_configured is True

    @pytest.mark.parametrize(
        ("given", "expected"),
        [("", None), ("3", 3), ("Studio Speakers", "Studio Speakers")],
    )
    def test_a_device_may_be_named_or_numbered_or_left_to_the_system(
        self, given: str, expected: int | str | None
    ) -> None:
        """PortAudio takes either, and blank is what almost everybody wants:
        it is the only setting that survives replugging a pair of speakers."""

        module = StubModule()
        sink = SoundDeviceSink(device=given, module=module)
        assert sink._device == expected  # noqa: SLF001 - the translation is the point


class TestOpening:
    async def test_the_device_is_asked_whether_it_takes_what_we_will_send(
        self,
    ) -> None:
        """Once, at start-up, rather than in the second after a shot."""

        module = StubModule()
        await SoundDeviceSink(module=module).open()

        assert module.checks == [{"device": None, "channels": 1, "dtype": "int16"}]

    async def test_a_machine_with_no_usable_output_says_so_and_stops(self) -> None:
        module = StubModule(check_error=RuntimeError("no default output device"))

        with pytest.raises(PlaybackUnavailableError, match="no usable audio output"):
            await SoundDeviceSink(module=module).open()

    async def test_a_missing_package_cannot_be_opened(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter, "sdk", lambda: None)

        with pytest.raises(PlaybackUnavailableError, match="not installed"):
            await SoundDeviceSink().open()


class TestPlaying:
    async def test_the_whole_sentence_reaches_the_device(self) -> None:
        module = StubModule()
        sink = SoundDeviceSink(module=module)
        await sink.open()
        await sink.play(A_SENTENCE)

        assert b"".join(module.stream.writes) == A_SENTENCE.data

    async def test_it_is_handed_over_in_pieces_rather_than_all_at_once(self) -> None:
        """The gaps between pieces are the only places an interruption can
        land, so there have to be some."""

        module = StubModule()
        sink = SoundDeviceSink(chunk_seconds=0.05, module=module)
        await sink.open()
        await sink.play(A_SENTENCE)

        assert len(module.stream.writes) == 10

    async def test_the_stream_is_told_the_format_of_the_audio_it_is_given(
        self,
    ) -> None:
        module = StubModule()
        sink = SoundDeviceSink(module=module)
        await sink.open()
        await sink.play(Audio(data=b"\x00\x00" * 100, sample_rate=16_000))

        assert module.stream.options["samplerate"] == 16_000
        assert module.stream.options["channels"] == 1
        assert module.stream.options["dtype"] == "int16"

    async def test_finishing_waits_for_the_buffered_tail(self) -> None:
        """`stop` plays out what PortAudio still holds. Returning without it
        would make `SpeechCompleted` a fact about a buffer, not about a room."""

        module = StubModule()
        sink = SoundDeviceSink(module=module)
        await sink.open()
        await sink.play(A_SENTENCE)

        assert module.stream.stopped == 1
        assert module.stream.aborted == 0
        assert module.stream.closed == 1

    async def test_an_empty_utterance_never_opens_a_stream(self) -> None:
        module = StubModule()
        sink = SoundDeviceSink(module=module)
        await sink.open()
        await sink.play(Audio(data=b""))

        assert module.stream.started == 0

    async def test_a_device_that_fails_mid_word_becomes_a_neutral_error(self) -> None:
        """The service reacts to `PlaybackError`, not to whatever PortAudio
        decided to raise."""

        stream = StubStream()
        stream.error = RuntimeError("the output stream was disconnected")
        sink = SoundDeviceSink(module=StubModule(stream))
        await sink.open()

        with pytest.raises(PlaybackError, match="disconnected"):
            await sink.play(A_SENTENCE)

    async def test_the_stream_is_closed_even_when_the_device_fails(self) -> None:
        stream = StubStream()
        stream.error = RuntimeError("gone")
        sink = SoundDeviceSink(module=StubModule(stream))
        await sink.open()

        with pytest.raises(PlaybackError):
            await sink.play(A_SENTENCE)
        assert stream.closed == 1


class TestInterruption:
    async def test_cutting_a_sentence_short_throws_the_buffer_away(self) -> None:
        """`abort` rather than `stop`: playing out what is buffered is the
        opposite of being interrupted."""

        stream = StubStream()
        stream.gate = threading.Event()
        sink = SoundDeviceSink(chunk_seconds=0.05, module=StubModule(stream))
        await sink.open()

        playing = asyncio.create_task(sink.play(A_SENTENCE))
        await until(stream.progress)
        playing.cancel()
        # Let `play` reach the point where it has asked the device to stop, then
        # release the write it is blocked in so the thread can act on it.
        await asyncio.sleep(0.01)
        stream.gate.set()

        with pytest.raises(asyncio.CancelledError):
            await playing

        assert stream.aborted == 1
        assert stream.stopped == 0
        assert stream.closed == 1

    async def test_the_rest_of_the_sentence_never_reaches_the_device(self) -> None:
        stream = StubStream()
        stream.gate = threading.Event()
        sink = SoundDeviceSink(chunk_seconds=0.05, module=StubModule(stream))
        await sink.open()

        playing = asyncio.create_task(sink.play(A_SENTENCE))
        await until(stream.progress)
        playing.cancel()
        await asyncio.sleep(0.01)
        stream.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await playing

        # One chunk of ten. The other nine were abandoned rather than played.
        assert len(stream.writes) == 1

    async def test_playing_does_not_return_until_the_device_is_quiet(self) -> None:
        """The service starts the next sentence the moment this returns, so
        anything still draining here would be two voices at once."""

        stream = StubStream()
        stream.gate = threading.Event()
        sink = SoundDeviceSink(chunk_seconds=0.05, module=StubModule(stream))
        await sink.open()

        playing = asyncio.create_task(sink.play(A_SENTENCE))
        await until(stream.progress)
        playing.cancel()
        await asyncio.sleep(0.02)

        # Still held inside the blocked write, so nothing has been aborted yet
        # and `play` has not finished either.
        assert stream.aborted == 0
        assert not playing.done()

        stream.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await playing
        assert stream.aborted == 1

    async def test_a_wedged_device_does_not_hold_up_the_next_sentence_forever(
        self,
    ) -> None:
        """Only reachable when PortAudio has stopped responding. Bounded,
        because the alternative is a service that can never speak again."""

        stream = StubStream()
        stream.gate = threading.Event()
        sink = SoundDeviceSink(
            chunk_seconds=0.05, abort_timeout_seconds=0.02, module=StubModule(stream)
        )
        await sink.open()

        playing = asyncio.create_task(sink.play(A_SENTENCE))
        await until(stream.progress)
        playing.cancel()

        with pytest.raises(asyncio.CancelledError):
            await playing
        assert stream.aborted == 0

        # Released so the worker thread finishes rather than outliving the test.
        stream.gate.set()
        await asyncio.sleep(0.05)
