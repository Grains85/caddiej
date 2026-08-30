"""Measuring how loud a rendered line is, moment by moment.

The room reacts to the voice by *measuring* it. Not by asking a model when to
dim a light, not by guessing from the length of the sentence, and not by
animating something plausible on a timer: the samples about to be played are
already in hand, and they say exactly where the words and the pauses are. Any
other source of that information would be an invention that occasionally
disagrees with what somebody can hear.

Three decisions make the result usable by a light rather than merely accurate.

**Root mean square, not peak.** Peak amplitude jumps on every consonant, and a
light driven by it flickers. RMS over a window is the energy in that window,
which is much closer to what a listener would call loudness.

**Normalised against its own peak.** The interesting thing is the shape of the
words, not the volume the engine happened to produce. Absolute level would make
every consumer responsible for knowing how loud a loud utterance is, and would
make the same sentence behave differently after a change of voice.

**Smoothed asymmetrically.** Rising fast and falling slow is how every compressor,
meter and VU needle ever built behaves, and for the same reason: it catches the
onset of a syllable but does not drop into the gap between two of them. A
symmetric filter has to choose between chattering and missing the words entirely.

The arithmetic is standard-library. `math.sumprod` does the sum of squares at C
speed, so a three-second sentence costs a millisecond or so — cheap enough to do
on the event loop once per line, which is where it happens.
"""

import math
from array import array

from caddiej.domain.speech import Envelope
from caddiej.speech.audio import Audio

# Twenty frames a second. Syllables arrive at three to five hertz, so this
# resolves them several times over while staying far below the rate at which a
# lamp — or an eye — can tell the difference.
DEFAULT_FRAME_RATE = 20.0

# How quickly the measurement chases a rise and a fall. The gap between them is
# the whole point: the attack is short enough to catch the start of a word, and
# the release is long enough to ride over the gap between two of them instead of
# dropping to nothing four times a second.
DEFAULT_ATTACK_SECONDS = 0.06
DEFAULT_RELEASE_SECONDS = 0.22

# Peaks below this are treated as silence rather than amplified into a full-scale
# shape. Without it, a line the engine rendered as near-silence — or as actual
# silence, which is what the fake engine produces — would be normalised into
# something that looks like shouting.
SILENCE_FLOOR = 1e-4


def envelope_of(
    audio: Audio,
    frame_rate: float = DEFAULT_FRAME_RATE,
    attack_seconds: float = DEFAULT_ATTACK_SECONDS,
    release_seconds: float = DEFAULT_RELEASE_SECONDS,
) -> Envelope:
    """The shape of an utterance: one level per frame, from nothing to its peak.

    Silence in gives an empty envelope out rather than a run of zeros, so that
    "there was nothing to measure" and "the voice was quiet here" stay different
    answers. A consumer that treats an empty envelope as nothing to do then
    behaves correctly without a special case.
    """

    if frame_rate <= 0:
        raise ValueError("frame_rate must be positive")
    if attack_seconds < 0 or release_seconds < 0:
        raise ValueError("attack and release must not be negative")
    if audio.is_empty:
        return Envelope(values=(), frame_rate=frame_rate)

    loudness = _frame_loudness(audio, frame_rate)
    peak = max(loudness, default=0.0)
    if peak <= SILENCE_FLOOR:
        # Rendered silence, which is exactly what the fake engine returns. There
        # is no shape in it, and scaling it up would manufacture one.
        return Envelope(values=(), frame_rate=frame_rate)

    return Envelope(
        values=tuple(
            _smooth(
                [value / peak for value in loudness],
                seconds_per_frame=1.0 / frame_rate,
                attack_seconds=attack_seconds,
                release_seconds=release_seconds,
            )
        ),
        frame_rate=frame_rate,
    )


def _frame_loudness(audio: Audio, frame_rate: float) -> list[float]:
    """RMS amplitude per frame, in whatever units the samples are in.

    Mixed to mono by taking the first channel rather than averaging. Everything
    CaddieJ renders is mono, and a shape taken from one channel of a stereo
    voice is the same shape as the average of both — so the cheap answer is also
    the right one.
    """

    samples = array("h")
    samples.frombytes(audio.data)
    if audio.channels > 1:
        samples = samples[:: audio.channels]

    stride = max(1, round(audio.sample_rate / frame_rate))
    levels: list[float] = []
    for start in range(0, len(samples), stride):
        frame = samples[start : start + stride]
        if not frame:
            continue
        # `sumprod` of a sequence with itself is its sum of squares, computed in
        # C. The obvious Python generator is a hundred times slower and this
        # runs on the event loop.
        levels.append(math.sqrt(math.sumprod(frame, frame) / len(frame)))
    return levels


def _smooth(
    levels: list[float],
    seconds_per_frame: float,
    attack_seconds: float,
    release_seconds: float,
) -> list[float]:
    """A one-pole filter that rises quickly and falls slowly.

    The coefficient is the standard `1 - exp(-dt/tau)`, which makes `tau` an
    honest time constant: after that long, the output has covered about
    two-thirds of a step. Expressing it in seconds rather than as a raw weight is
    what lets the frame rate change without silently retuning the feel.
    """

    rising = _coefficient(attack_seconds, seconds_per_frame)
    falling = _coefficient(release_seconds, seconds_per_frame)

    smoothed: list[float] = []
    current = 0.0
    for level in levels:
        weight = rising if level > current else falling
        current += (level - current) * weight
        # Clamped because the domain refuses anything outside the unit range, and
        # floating point can leave a value a hair above one after a rise from
        # something that was already one.
        smoothed.append(min(1.0, max(0.0, current)))
    return smoothed


def _coefficient(tau_seconds: float, seconds_per_frame: float) -> float:
    """How much of the way to a new value one frame moves.

    Zero seconds means "no smoothing at all", which is a legitimate request and
    would otherwise be a division by zero.
    """

    if tau_seconds <= 0:
        return 1.0
    return 1.0 - math.exp(-seconds_per_frame / tau_seconds)
