"""The judgements, pinned down.

The catalogue is five lines of numbers, and numbers in a table are exactly the
thing that drifts when somebody tunes one of them at eleven at night. So these
tests are less about correctness than about intent: they say what the shape of
the escalation is for, and they fail when a change breaks the shape rather than
when it changes a value.
"""

from itertools import pairwise

import pytest

from caddiej.domain.scoring import HoleResult
from caddiej.effects.catalogue import EFFECTS, effect_for
from caddiej.effects.effect import Effect, EffectPriority


class TestEveryOutcomeIsCovered:
    def test_every_result_the_domain_names_has_an_effect(self) -> None:
        """The hook is only wired if all five names actually resolve."""

        for result in HoleResult:
            assert effect_for(result) is not None

    def test_an_unknown_result_resolves_to_nothing_rather_than_a_default(self) -> None:
        """A missing entry means somebody added an outcome and did not decide
        what the room should do. Inventing a reaction would hide that."""

        assert EFFECTS.get("triple bogey") is None  # type: ignore[arg-type]

    def test_the_cue_matches_the_result_it_is_filed_under(self) -> None:
        """The cue is the browser's styling hook and the logs' identifier. If it
        drifted from the result name, a celebration would silently lose its
        colour and nothing would say why."""

        for result, effect in EFFECTS.items():
            assert effect.cue == result.value


class TestTheEscalation:
    ORDER = (
        HoleResult.PAR,
        HoleResult.BIRDIE,
        HoleResult.EAGLE,
        HoleResult.ALBATROSS,
        HoleResult.HOLE_IN_ONE,
    )

    def test_rarer_outcomes_are_bigger_and_last_longer(self) -> None:
        effects = [EFFECTS[result] for result in self.ORDER]

        for milder, rarer in pairwise(effects):
            assert rarer.intensity > milder.intensity
            assert rarer.seconds > milder.seconds

    def test_precedence_never_decreases_as_outcomes_get_rarer(self) -> None:
        """Not strictly increasing: an eagle and an albatross share a class on
        purpose, because they are the same kind of thing and cannot overlap."""

        effects = [EFFECTS[result] for result in self.ORDER]

        for milder, rarer in pairwise(effects):
            assert rarer.priority >= milder.priority

    def test_a_par_is_almost_nothing(self) -> None:
        """The most important number in the table. If a par gets a real reaction
        then a birdie has to be bigger than a real reaction, and by the time the
        scale reaches an ace there is nowhere left to go."""

        par = EFFECTS[HoleResult.PAR]

        assert par.intensity <= 0.2
        assert par.seconds <= 2.0
        assert par.say == "", "a par happens on most holes; it is not announced"
        assert par.priority is EffectPriority.ROUTINE

    def test_only_a_hole_in_one_is_a_landmark(self) -> None:
        """The top of the precedence scale has exactly one occupant, which is
        what makes it mean anything."""

        landmarks = [
            result
            for result, effect in EFFECTS.items()
            if effect.priority is EffectPriority.LANDMARK
        ]

        assert landmarks == [HoleResult.HOLE_IN_ONE]

    def test_nothing_is_a_light_show(self) -> None:
        """Over in a few seconds, all of them. Asserted so that growing one into
        something elaborate is a decision somebody makes here rather than a
        drift."""

        for effect in EFFECTS.values():
            assert effect.seconds <= 6.0

    def test_everything_worth_naming_is_named_out_loud(self) -> None:
        """Everything except a par, which is the point of the exception."""

        for result, effect in EFFECTS.items():
            if result is HoleResult.PAR:
                continue
            assert effect.say, f"{result} should be announced"


class TestAnEffect:
    def test_it_carries_a_label_worth_reading_from_standing_height(self) -> None:
        for effect in EFFECTS.values():
            assert effect.label == effect.label.upper()
            assert len(effect.label) <= 16

    @pytest.mark.parametrize(
        ("cue", "label", "intensity", "seconds"),
        [
            ("", "BIRDIE", 0.5, 1.0),
            ("birdie", "", 0.5, 1.0),
            ("birdie", "BIRDIE", -0.1, 1.0),
            ("birdie", "BIRDIE", 1.1, 1.0),
            ("birdie", "BIRDIE", 0.5, 0.0),
            ("birdie", "BIRDIE", 0.5, -1.0),
        ],
    )
    def test_an_impossible_effect_is_refused(
        self, cue: str, label: str, intensity: float, seconds: float
    ) -> None:
        with pytest.raises(ValueError):
            Effect(
                cue=cue,
                label=label,
                priority=EffectPriority.NOTABLE,
                intensity=intensity,
                seconds=seconds,
            )


class TestPriorityIsSeparateFromSize:
    def test_precedence_can_be_high_while_the_gesture_is_small(self) -> None:
        """The reason they are two fields. Nothing in the catalogue needs this
        today, and something urgent and quiet — a warning — would."""

        quiet_alarm = Effect(
            cue="test",
            label="TEST",
            priority=EffectPriority.LANDMARK,
            intensity=0.05,
            seconds=1.0,
        )

        assert quiet_alarm.priority is EffectPriority.LANDMARK
        assert quiet_alarm.intensity < EFFECTS[HoleResult.PAR].intensity

    def test_the_priorities_are_spaced_so_one_can_be_added_between_two(self) -> None:
        values = sorted(int(priority) for priority in EffectPriority)

        for lower, higher in pairwise(values):
            assert higher - lower >= 2
