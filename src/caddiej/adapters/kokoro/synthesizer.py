"""The only file in CaddieJ that knows what Kokoro looks like.

Everything engine-shaped is here: that a pipeline is built per language, that
the language is encoded in the first letter of the voice name, that generation
yields chunks rather than a buffer, that a chunk may be a tensor on some device,
and that the result is float samples at twenty-four kilohertz. Swapping engines,
or adding a second, is a new file beside this one and a line in the factory.

Three decisions worth stating.

**The model is loaded at start-up, in a thread.** It is hundreds of megabytes
and the first use downloads it. Loading it lazily would mean the first coaching
line of an evening arrives somewhere after the second shot, and loading it on
the event loop would stop the floor, the launch monitor and the HUD for as long
as it took.

**Generation also runs in a thread, and it is not a small one.** This is a
neural network on the CPU of a machine that is simultaneously running a golf
simulator. Every call crosses into a worker thread so that the loop stays free;
nothing in here ever runs inline.

**Float samples become 16-bit PCM here, without NumPy.** The conversion belongs
to the engine that produced the floats rather than to the queue, the cache or
the sound card. Doing it in the standard library is a deliberate choice: the
arithmetic is a multiply and a clamp over a few tens of thousands of samples,
which costs a millisecond or two beside the hundreds this model spends
generating them, and in exchange the adapter can be tested with a stub on a
machine that has no machine learning stack installed at all.
"""

import asyncio
import logging
import sys
from array import array
from collections.abc import Iterable
from typing import Any

from caddiej.speech.audio import Audio
from caddiej.speech.synth import SynthesisFailedError, SynthesisUnavailableError

logger = logging.getLogger(__name__)

ENGINE_NAME = "kokoro"
# What Kokoro produces, always. Stated rather than read back from the model so
# that a sink can be configured before a single sentence has been rendered.
SAMPLE_RATE = 24_000
DEFAULT_VOICE = "bm_george"

# The first letter of a voice name is its language, which is Kokoro's own
# convention: `bm_george` is British, male, George. Spelled out because a
# mismatched pipeline and voice does not fail — it mispronounces.
LANGUAGES: dict[str, str] = {
    "a": "American English",
    "b": "British English",
    "e": "Spanish",
    "f": "French",
    "h": "Hindi",
    "i": "Italian",
    "j": "Japanese",
    "p": "Brazilian Portuguese",
    "z": "Mandarin Chinese",
}


def sdk() -> Any | None:
    """The Kokoro module, or None if it was never installed.

    Optional on purpose: CaddieJ runs, coaches and films swings with no speech
    dependency present at all, so its absence is a supported state rather than
    an import error at start-up.
    """

    try:
        import kokoro
    except ModuleNotFoundError:
        return None
    return kokoro


def language_of(voice: str) -> str | None:
    """The language code for a voice, or None if it names no known one."""

    return voice[:1].lower() if voice[:1].lower() in LANGUAGES else None


class KokoroSynthesizer:
    """Speaks one line with a local Kokoro model."""

    def __init__(
        self,
        voice: str = DEFAULT_VOICE,
        speed: float = 1.0,
        module: Any | None = None,
        pipeline: Any | None = None,
    ) -> None:
        self._voice = voice or DEFAULT_VOICE
        self._speed = speed
        self._module = module if module is not None else sdk()
        self._pipeline = pipeline
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return ENGINE_NAME

    @property
    def voice(self) -> str:
        return self._voice

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE

    @property
    def is_configured(self) -> bool:
        """Whether this could work: a package, and a voice it recognises."""

        if self._module is None and self._pipeline is None:
            logger.warning("speech_kokoro_missing")
            return False
        if language_of(self._voice) is None:
            logger.warning("speech_voice_unknown", extra={"speech_voice": self._voice})
            return False
        return True

    async def open(self) -> None:
        """Build the pipeline, downloading the weights if this is the first run."""

        if self._pipeline is not None:
            return

        code = language_of(self._voice)
        if self._module is None or code is None:
            raise SynthesisUnavailableError(
                f"no Kokoro pipeline can be built for voice {self._voice!r}"
            )

        logger.info(
            "speech_kokoro_loading",
            extra={"speech_voice": self._voice, "speech_language": LANGUAGES[code]},
        )
        try:
            self._pipeline = await asyncio.to_thread(
                self._module.KPipeline, lang_code=code
            )
        except Exception as error:
            # A missing model, no network on the first run, a broken install of
            # the framework underneath. All the same answer: no voice tonight.
            raise SynthesisUnavailableError(
                f"the Kokoro pipeline would not load: {error}"
            ) from error
        logger.info("speech_kokoro_loaded", extra={"speech_voice": self._voice})

    async def render(self, text: str, voice: str | None = None) -> Audio:
        """Speak the text, off the event loop, one sentence at a time."""

        if self._pipeline is None:
            raise SynthesisUnavailableError("render called before the model was loaded")

        chosen = voice or self._voice
        # One at a time: the pipeline holds model state, and the service only
        # ever asks for one line anyway. The lock is here so that stays true if
        # a second caller is ever added.
        async with self._lock:
            try:
                return await asyncio.to_thread(self._generate, text, chosen)
            except Exception as error:
                raise SynthesisFailedError(
                    f"{type(error).__name__}: {error}"
                ) from error

    def _generate(self, text: str, voice: str) -> Audio:
        """Run the model. Blocking, and always called in a worker thread."""

        assert self._pipeline is not None
        parts = [
            _pcm(_samples(result))
            for result in self._pipeline(text, voice=voice, speed=self._speed)
        ]
        return Audio(data=b"".join(parts), sample_rate=SAMPLE_RATE)

    async def aclose(self) -> None:
        """Drop the model so its memory goes back."""

        self._pipeline = None


def _samples(result: Any) -> Any:
    """The audio out of one generated chunk.

    Kokoro has returned a plain `(graphemes, phonemes, audio)` tuple and a
    named result object across its versions, and both are still in the wild.
    Asking for the attribute first and falling back to the position costs one
    `getattr` and removes a reason for this to break on an upgrade.
    """

    audio = getattr(result, "audio", None)
    if audio is not None:
        return audio
    if isinstance(result, tuple | list) and len(result) >= 3:
        return result[2]
    raise SynthesisFailedError(
        f"the engine produced a {type(result).__name__}, which holds no audio"
    )


def _pcm(samples: Any) -> bytes:
    """Float samples in -1..1, as signed 16-bit little-endian bytes.

    Clipped rather than normalised. Normalising would make a quiet sentence and
    a loud one arrive at the same volume, which for a voice in a room is worse
    than the rare clipped peak it would avoid.
    """

    scaled = array("h", (_sample(value) for value in _floats(samples)))
    if sys.byteorder != "little":
        # WAV and every sink here are little-endian. Spelled out rather than
        # assumed, because the machine where this is wrong is the one nobody
        # tests on.
        scaled.byteswap()
    return scaled.tobytes()


def _sample(value: float) -> int:
    return max(-32768, min(32767, round(value * 32767.0)))


def _floats(samples: Any) -> Iterable[float]:
    """One flat run of Python floats, whatever the engine handed back.

    A torch tensor is detached and brought back from whatever device it was on
    first, because asking a tensor that still wants a gradient for its values
    raises rather than answering.
    """

    detach = getattr(samples, "detach", None)
    if detach is not None:
        samples = detach().cpu()

    listed = getattr(samples, "tolist", None)
    values = listed() if listed is not None else samples

    if isinstance(values, int | float):
        return (float(values),)
    return _flatten(values)


def _flatten(values: Any) -> list[float]:
    """Accept a shape of (n,) or (1, n), which engines use interchangeably."""

    flat: list[float] = []
    for value in values:
        if isinstance(value, list | tuple):
            flat.extend(_flatten(value))
        else:
            flat.append(float(value))
    return flat
