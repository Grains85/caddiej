"""The measurement the whole subsystem rests on.

Everything downstream is arithmetic on these numbers, so what matters here is
that they describe the actual waveform: loud passages read as loud, gaps read as
quiet, and nothing invents a shape where there was no sound.
"""

import math

from caddiej.domain.speech import Envelope
from caddiej.speech.audio import Audio, silence
from caddiej.speech.envelope import (
    DEFAULT_FRAME_RATE,
    envelope_of,
)


def tone(
    seconds: float,
    amplitude: float = 0.8,
    hertz: float = 220.0,
    sample_rate: int = 24_000,
) -> Audio:
    """A sine, which is the simplest thing with an honest RMS level."""

    frames = round(seconds * sample_rate)
    samples = bytearray()
    for index in range(frames):
        value = amplitude * math.sin(2 * math.pi * hertz * index / sample_rate)
        samples += round(value * 32767).to_bytes(2, "little", signed=True)
    return Audio(data=bytes(samples), sample_rate=sample_rate)


def joined(*parts: Audio) -> Audio:
    return Audio(
        data=b"".join(part.data for part in parts),
        sample_rate=parts[0].sample_rate,
    )


class TestMeasuringAVoice:
    def test_a_steady_tone_settles_at_its_own_peak(self) -> None:
        envelope = envelope_of(tone(1.0))

        # Normalised against itself, so a constant signal ends up at the top
        # whatever amplitude it was rendered at.
        assert envelope.values[-1] > 0.95
        assert envelope.peak <= 1.0

    def test_the_same_shape_is_measured_whatever_the_volume(self) -> None:
        """Normalisation is what stops a change of voice changing the lights."""

        quiet = envelope_of(joined(tone(0.4, amplitude=0.05), silence(0.4)))
        loud = envelope_of(joined(tone(0.4, amplitude=0.9), silence(0.4)))

        assert len(quiet.values) == len(loud.values)
        for left, right in zip(quiet.values, loud.values, strict=True):
            assert left == round(right, 10) or abs(left - right) < 0.02

    def test_a_gap_reads_quieter_than_the_words_around_it(self) -> None:
        envelope = envelope_of(
            joined(tone(0.5), silence(0.5), tone(0.5)), release_seconds=0.05
        )

        spoken = envelope.at(0.4)
        gap = envelope.at(0.9)
        assert gap < spoken / 2

    def test_silence_measures_as_nothing_rather_than_as_a_shape(self) -> None:
        """The distinction the fake engine depends on: rendered silence has no
        shape in it, and scaling it up would manufacture one."""

        assert envelope_of(silence(2.0)).is_empty
        assert envelope_of(Audio(data=b"")).is_empty

    def test_the_frame_count_follows_the_length_of_the_line(self) -> None:
        envelope = envelope_of(tone(2.0), frame_rate=20.0)

        assert len(envelope.values) == 40
        assert envelope.duration_seconds == 2.0

    def test_a_stereo_line_is_measured_from_one_channel(self) -> None:
        mono = tone(0.5)
        doubled = bytearray()
        for at in range(0, len(mono.data), 2):
            doubled += mono.data[at : at + 2] * 2
        stereo = Audio(data=bytes(doubled), sample_rate=mono.sample_rate, channels=2)

        assert stereo.duration_seconds == mono.duration_seconds
        assert len(envelope_of(stereo).values) == len(envelope_of(mono).values)


class TestSmoothing:
    def test_it_rises_faster_than_it_falls(self) -> None:
        """The asymmetry is the point: catch the start of a word, ride over the
        gap between two of them."""

        envelope = envelope_of(
            joined(silence(0.3), tone(0.6), silence(0.6)),
            attack_seconds=0.05,
            release_seconds=0.4,
        )

        onset = envelope.at(0.35) - envelope.at(0.3)
        release = envelope.at(0.9) - envelope.at(0.95)
        assert onset > release

    def test_no_smoothing_at_all_is_a_legitimate_request(self) -> None:
        envelope = envelope_of(
            joined(tone(0.2), silence(0.2)),
            attack_seconds=0.0,
            release_seconds=0.0,
        )

        # Straight through, so the gap reads as actual silence rather than as a
        # decay towards it.
        assert envelope.values[-1] < 0.01

    def test_a_long_release_never_leaves_the_legal_range(self) -> None:
        envelope = envelope_of(tone(1.0), attack_seconds=0.0, release_seconds=5.0)

        assert all(0.0 <= value <= 1.0 for value in envelope.values)


class TestReadingAnEnvelope:
    def test_it_interpolates_between_the_frames_around_a_moment(self) -> None:
        envelope = Envelope(values=(0.0, 1.0), frame_rate=1.0)

        assert envelope.at(0.0) == 0.0
        assert envelope.at(0.5) == 0.5
        assert envelope.at(1.0) == 1.0

    def test_outside_the_line_there_is_nothing(self) -> None:
        envelope = Envelope(values=(0.5, 0.5), frame_rate=1.0)

        assert envelope.at(-1.0) == 0.0
        assert envelope.at(99.0) == 0.0

    def test_an_empty_envelope_is_silent_everywhere(self) -> None:
        envelope = Envelope(values=(), frame_rate=DEFAULT_FRAME_RATE)

        assert envelope.is_empty
        assert envelope.at(0.0) == 0.0
        assert envelope.peak == 0.0

    def test_values_outside_the_unit_range_are_refused(self) -> None:
        for bad in ((1.5,), (-0.1,)):
            try:
                Envelope(values=bad, frame_rate=1.0)
            except ValueError:
                continue
            raise AssertionError(f"{bad} should not be a legal envelope")

    def test_a_frame_rate_of_zero_is_refused(self) -> None:
        try:
            Envelope(values=(0.5,), frame_rate=0.0)
        except ValueError:
            return
        raise AssertionError("a frame rate of zero should not be legal")
