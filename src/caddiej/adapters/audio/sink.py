"""The only file in CaddieJ that knows what a sound card looks like.

PortAudio, through `sounddevice`, because it is the one option that is genuinely
cross-platform, opens a device without a subprocess, and can be told to stop
*now* rather than at the end of the buffer. The last of those is the reason:
interrupting a sentence is a requirement here, and a player process that has to
be killed makes it a guess.

Three decisions worth stating.

**Raw streams, not arrays.** `RawOutputStream` takes bytes, which is what a
rendered utterance already is, so playback needs no numerical library and no
conversion. The alternative would put NumPy between a WAV file and a speaker.

**Playback happens entirely inside one thread.** PortAudio's calls block, and
splitting them across `to_thread` boundaries would let the async side cancel
between two operations on a live device. One function owns the stream from
`start` to `close`, and the only thing crossing into it is a flag.

**Cancelling waits for the device to go quiet.** `play` does not return until
the stream has actually been aborted, because the caller's next act is to start
another sentence, and a tail still draining from the previous one is exactly the
overlap the service above promises cannot happen.
"""

import asyncio
import logging
import threading
from typing import Any

from caddiej.speech.audio import Audio, chunks
from caddiej.speech.sink import PlaybackError, PlaybackUnavailableError

logger = logging.getLogger(__name__)

OUTPUT_NAME = "sounddevice"
# How much audio is handed to the device at a time. Also the granularity of an
# interruption, so it is small enough that a cut-off sentence sounds cut off
# rather than finished.
CHUNK_SECONDS = 0.05
# How long to wait for the device to fall silent after asking it to. Generous:
# it is only ever reached when PortAudio is wedged, and the alternative is
# starting the next sentence over the top of this one.
ABORT_TIMEOUT = 2.0


def sdk() -> Any | None:
    """The `sounddevice` module, or None if it was never installed."""

    try:
        import sounddevice
    except (ModuleNotFoundError, OSError):
        # OSError as well as the obvious one: `sounddevice` raises it at import
        # time when the PortAudio shared library is missing, which is a normal
        # state on a slim Linux image and is not a reason to fail to start.
        return None
    return sounddevice


def _device(name: str | None) -> int | str | None:
    """A device as PortAudio wants it: an index, a name, or the default."""

    if not name:
        return None
    try:
        return int(name)
    except ValueError:
        return name


class SoundDeviceSink:
    """Plays one utterance at a time through the system's audio output."""

    def __init__(
        self,
        device: str | None = None,
        chunk_seconds: float = CHUNK_SECONDS,
        abort_timeout_seconds: float = ABORT_TIMEOUT,
        module: Any | None = None,
    ) -> None:
        self._device = _device(device)
        self._chunk_seconds = chunk_seconds
        self._abort_timeout = abort_timeout_seconds
        self._module = module if module is not None else sdk()

    @property
    def name(self) -> str:
        return OUTPUT_NAME

    @property
    def is_configured(self) -> bool:
        if self._module is None:
            logger.warning("speech_sounddevice_missing")
            return False
        return True

    async def open(self) -> None:
        """Check the device will take what CaddieJ is going to send it.

        Asked once, at start-up, rather than discovered on the first coaching
        line: a machine with no output at all should say so in a log line while
        nobody is waiting, not fail in the second after a shot.
        """

        module = self._module
        if module is None:
            raise PlaybackUnavailableError("the sounddevice package is not installed")

        try:
            await asyncio.to_thread(
                module.check_output_settings,
                device=self._device,
                channels=1,
                dtype="int16",
            )
        except Exception as error:
            raise PlaybackUnavailableError(
                f"no usable audio output: {error}"
            ) from error
        logger.info("speech_output_ready", extra={"speech_output": OUTPUT_NAME})

    async def play(self, audio: Audio) -> None:
        """Play it all, then return. Cancelling silences the device first."""

        if audio.is_empty:
            return
        if self._module is None:
            raise PlaybackUnavailableError("the sounddevice package is not installed")

        stop = threading.Event()
        # Shielded, because cancelling the wrapper would not stop the thread it
        # is waiting on. Stopping is done by the flag, and the wait below is
        # what makes it observable.
        playing = asyncio.ensure_future(
            asyncio.to_thread(self._play_blocking, audio, stop)
        )
        try:
            await asyncio.shield(playing)
        except asyncio.CancelledError:
            stop.set()
            done, _ = await asyncio.wait({playing}, timeout=self._abort_timeout)
            if not done:
                logger.warning("speech_output_would_not_stop")
            raise
        except Exception as error:
            raise PlaybackError(f"{type(error).__name__}: {error}") from error

    def _play_blocking(self, audio: Audio, stop: threading.Event) -> None:
        """Own the stream from start to close. Always in a worker thread."""

        assert self._module is not None
        stream = self._module.RawOutputStream(
            samplerate=audio.sample_rate,
            channels=audio.channels,
            dtype="int16",
            device=self._device,
            # Small buffers, so that abandoning the buffer abandons very little
            # of the sentence and an interruption sounds immediate.
            latency="low",
        )
        stream.start()
        try:
            for chunk in chunks(audio, self._chunk_seconds):
                if stop.is_set():
                    break
                stream.write(chunk)
            if stop.is_set():
                # Throws away whatever PortAudio still holds. `stop` would play
                # it out, which is the opposite of being interrupted.
                stream.abort()
            else:
                # Waits for the buffered tail, so returning means the room has
                # actually heard the whole line.
                stream.stop()
        finally:
            stream.close()

    async def aclose(self) -> None:
        return None
