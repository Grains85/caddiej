"""The part somebody will want to argue about: how strong the effect is.

All pure arithmetic, so these are the cheapest tests in the subsystem and the
ones that pin down the actual feel of it.
"""

from itertools import pairwise

import pytest
from tests.unit.test_lighting_envelope import joined, tone

from caddiej.lighting.modulation import (
    Flourish,
    Modulation,
    pulse_curve,
    speaking_curve,
)
from caddiej.speech.audio import silence
from caddiej.speech.envelope import envelope_of


class TestTheModulationRange:
    def test_silence_sits_at_the_minimum_and_a_peak_at_the_maximum(self) -> None:
        modulation = Modulation(minimum=0.5, maximum=1.0)

        assert modulation.brightness(0.0) == 0.5
        assert modulation.brightness(1.0) == 1.0
        assert modulation.brightness(0.5) == 0.75

    def test_the_default_is_a_dip_rather_than_a_lift(self) -> None:
        """Deliberate: dipping has room to work at any baseline, lifting has none
        at all when the lights are already full."""

        modulation = Modulation()

        assert modulation.maximum == 1.0
        assert 0.7 < modulation.minimum < 0.9

    def test_a_level_outside_the_unit_range_is_clamped_not_extrapolated(self) -> None:
        modulation = Modulation(minimum=0.5, maximum=1.0)

        assert modulation.brightness(-5.0) == 0.5
        assert modulation.brightness(5.0) == 1.0

    def test_an_equal_minimum_and_maximum_is_how_the_effect_is_switched_off(
        self,
    ) -> None:
        assert Modulation(minimum=1.0, maximum=1.0).is_flat
        assert not Modulation().is_flat

    @pytest.mark.parametrize(
        ("minimum", "maximum"),
        [
            (0.0, 1.0),  # zero brightness is off, and off is not a brightness
            (-0.5, 1.0),
            (1.0, 0.5),  # inverted
            (1.0, 3.0),  # a typo that would treble somebody's lights
        ],
    )
    def test_an_impossible_range_is_refused_where_it_can_still_be_traced(
        self, minimum: float, maximum: float
    ) -> None:
        with pytest.raises(ValueError):
            Modulation(minimum=minimum, maximum=maximum)


class TestFollowingAVoice:
    def test_the_lights_are_brighter_on_a_word_than_in_a_gap(self) -> None:
        envelope = envelope_of(joined(tone(0.5), silence(0.5)), release_seconds=0.05)
        curve = speaking_curve(envelope, Modulation(minimum=0.5, maximum=1.0))

        assert curve(0.4) > curve(0.9)

    def test_it_never_leaves_the_configured_range(self) -> None:
        envelope = envelope_of(joined(tone(0.4), silence(0.2), tone(0.4)))
        modulation = Modulation(minimum=0.6, maximum=0.95)
        curve = speaking_curve(envelope, modulation)

        for step in range(0, 120):
            assert 0.6 <= curve(step / 100) <= 0.95

    def test_past_the_end_of_the_line_it_sits_at_the_minimum(self) -> None:
        """Because the envelope is silent there, and silence is the minimum. The
        service stops driving at that point anyway; this is what makes running a
        fraction long harmless rather than a jump."""

        envelope = envelope_of(tone(0.5))
        curve = speaking_curve(envelope, Modulation(minimum=0.7, maximum=1.0))

        assert curve(99.0) == 0.7


class TestTheCueShape:
    def test_it_leaves_baseline_reaches_the_target_and_comes_back(self) -> None:
        curve = pulse_curve(target=0.5, seconds=1.0)

        assert curve(0.0) == pytest.approx(1.0)
        assert curve(0.5) == pytest.approx(0.5)
        assert curve(1.0) == pytest.approx(1.0, abs=1e-9)

    def test_outside_its_window_the_room_is_at_baseline(self) -> None:
        curve = pulse_curve(target=0.5, seconds=1.0)

        assert curve(-0.1) == 1.0
        assert curve(1.1) == 1.0

    def test_it_has_no_corners_for_a_bridge_to_tick_across(self) -> None:
        curve = pulse_curve(target=0.4, seconds=1.0)
        steps = [curve(index / 200) for index in range(201)]
        deltas = [after - before for before, after in pairwise(steps)]

        # A shape with a corner in it has a jump in the deltas. Half a sine does
        # not, so consecutive differences stay close together.
        for before, after in pairwise(deltas):
            assert abs(after - before) < 0.001

    def test_a_target_above_baseline_lifts_instead_of_dipping(self) -> None:
        """Supported, though the table does not use it: a room that is not
        already at full brightness has headroom to celebrate into."""

        curve = pulse_curve(target=1.3, seconds=1.0)

        assert curve(0.5) == pytest.approx(1.3)

    def test_a_pulse_with_no_duration_is_refused(self) -> None:
        with pytest.raises(ValueError):
            pulse_curve(target=0.5, seconds=0.0)


class TestAGesture:
    def test_a_gesture_reaches_its_target_at_the_middle(self) -> None:
        flourish = Flourish(target=0.25, seconds=2.0)

        assert flourish.curve(1.0) == pytest.approx(0.25)

    @pytest.mark.parametrize(
        ("target", "seconds"),
        [(0.0, 1.0), (-1.0, 1.0), (3.0, 1.0), (0.5, 0.0), (0.5, -1.0)],
    )
    def test_an_impossible_gesture_is_refused(
        self, target: float, seconds: float
    ) -> None:
        with pytest.raises(ValueError):
            Flourish(target=target, seconds=seconds)
