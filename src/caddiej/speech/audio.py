"""Rendered speech, as bytes anything can play.

One representation crosses both seams in this subsystem — the one where a
synthesizer hands audio back, and the one where a sink is given some — so it is
deliberately the least interesting format there is: signed 16-bit little-endian
PCM, the thing every sound card, every WAV file and every audio library accepts
without conversion.

Not float samples, and not a NumPy array. Engines produce floats and would
rather hand one over, but making that the currency would put NumPy on the
interface, and then the queue, the cache, the fakes and the tests would all need
it to talk about a sentence. Converting once, inside the engine's own adapter,
keeps the rest of CaddieJ arithmetic-free.

WAV lives here too, because a cached utterance on disk should be a file somebody
can double-click. The `wave` module in the standard library does the whole job.
"""

import io
import sys
import wave
from array import array
from dataclasses import dataclass

# What Kokoro produces, and a sensible default for anything else.
DEFAULT_SAMPLE_RATE = 24_000

_PEAK = 32767
_FLOOR = -32768

# A gain beyond this is not a volume control, it is a distortion effect. The
# ceiling is here rather than in configuration so that no deployment can reach
# a setting where every sentence is a square wave.
MAX_GAIN = 4.0


@dataclass(frozen=True, slots=True)
class Audio:
    """A rendered utterance: raw PCM plus what is needed to play it."""

    data: bytes
    sample_rate: int = DEFAULT_SAMPLE_RATE
    channels: int = 1
    sample_width: int = 2
    """Bytes per sample. Two, everywhere, and validated rather than assumed so
    that a future engine handing back 24-bit audio fails here instead of
    producing noise on the speakers."""

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if self.channels < 1:
            raise ValueError("channels must be at least one")
        if self.sample_width != 2:
            raise ValueError("only 16-bit audio is supported")
        if len(self.data) % (self.channels * self.sample_width):
            raise ValueError("data does not hold a whole number of frames")

    @property
    def frame_count(self) -> int:
        return len(self.data) // (self.channels * self.sample_width)

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.sample_rate

    @property
    def is_empty(self) -> bool:
        return not self.data


def silence(seconds: float, sample_rate: int = DEFAULT_SAMPLE_RATE) -> Audio:
    """A run of quiet. What a fake engine returns instead of a voice."""

    frames = max(0, round(seconds * sample_rate))
    return Audio(data=b"\x00\x00" * frames, sample_rate=sample_rate)


def to_wav(audio: Audio) -> bytes:
    """The audio as a RIFF file, ready to write down."""

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(audio.channels)
        handle.setsampwidth(audio.sample_width)
        handle.setframerate(audio.sample_rate)
        handle.writeframes(audio.data)
    return buffer.getvalue()


def from_wav(raw: bytes) -> Audio:
    """Read a WAV back, or raise `ValueError` if it is not one CaddieJ wrote.

    Strict about the format rather than converting: this only ever reads files
    this module produced, so anything else is a corrupted cache entry, and
    resynthesising a sentence costs a second while playing a misread one is a
    burst of noise in somebody's living room.
    """

    try:
        with wave.open(io.BytesIO(raw), "rb") as handle:
            width = handle.getsampwidth()
            if width != 2:
                raise ValueError(f"expected 16-bit audio, found {width * 8}-bit")
            return Audio(
                data=handle.readframes(handle.getnframes()),
                sample_rate=handle.getframerate(),
                channels=handle.getnchannels(),
                sample_width=width,
            )
    except wave.Error as error:
        raise ValueError(f"not a readable WAV: {error}") from error


def amplify(audio: Audio, gain: float) -> tuple[Audio, int]:
    """The same sentence, louder, and how many samples hit the rail doing it.

    A synthesizer is quiet because speech is quiet: Kokoro's own output peaks
    around a third of full scale and sits two dozen decibels below it on
    average, so a room with the Windows mixer already at maximum still gets a
    voice nobody can hear over a ball striker. The missing volume is headroom
    that was never used, and this is where it gets used.

    One constant gain for every utterance, never a per-sentence normalisation.
    Normalising would make a whisper and a shout arrive at the same level,
    which is the thing that makes an automated voice sound wrong, and it would
    make the same line play back at a different volume depending on what else
    was in it. A fixed multiplier changes nothing about the shape of the words.

    Clipped rather than limited, and the count of clipped samples is returned
    rather than swallowed. A soft limiter would hide a gain that is too high by
    quietly reshaping every peak; a number the caller can log says so out loud,
    which is what lets the setting be chosen from evidence instead of taste.
    """

    if not 0.0 < gain <= MAX_GAIN:
        raise ValueError(f"gain must be above zero and at most {MAX_GAIN}")
    if gain == 1.0 or audio.is_empty:
        return audio, 0

    samples = array("h")
    samples.frombytes(audio.data)
    # The data is little-endian and `array` reads it as the machine's own, so
    # both ends are spelled out for the machine nobody tests on.
    if sys.byteorder != "little":
        samples.byteswap()
    clipped = 0
    for index, sample in enumerate(samples):
        scaled = round(sample * gain)
        if scaled > _PEAK:
            scaled = _PEAK
            clipped += 1
        elif scaled < _FLOOR:
            scaled = _FLOOR
            clipped += 1
        samples[index] = scaled
    if sys.byteorder != "little":
        samples.byteswap()
    return (
        Audio(
            data=samples.tobytes(),
            sample_rate=audio.sample_rate,
            channels=audio.channels,
            sample_width=audio.sample_width,
        ),
        clipped,
    )


def chunks(audio: Audio, seconds: float) -> tuple[bytes, ...]:
    """Split the samples into playable pieces of roughly that length.

    A sink writes these one at a time so that stopping mid-sentence is possible:
    the gaps between them are the only places an interruption can land, so the
    piece length is also the worst-case delay before a voice goes quiet.
    """

    if seconds <= 0:
        raise ValueError("chunk length must be positive")
    frame = audio.channels * audio.sample_width
    stride = max(1, round(seconds * audio.sample_rate)) * frame
    return tuple(
        audio.data[start : start + stride]
        for start in range(0, len(audio.data), stride)
    )
