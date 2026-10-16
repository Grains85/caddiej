"""Rubric v2 of the Swing Score, and the promises every rubric has to keep.

Rubric v2 is history now -- v3 scores a swing from its phases and is tested in
`test_swing_score_v3.py` -- and this file still runs against it, through
`score_swing_v2`, for the reason v1 is still callable: a stored row says which
rubric made it, and a row CaddieJ can no longer recompute is a number nobody
can check. Everything below was written for the fifty-five / forty-five split
and means what it meant.

The one exception is `TestThroughTheService`, which goes through the live
coach and therefore scores under v3. It is here because what it checks -- that
a refused reply still settles a line carrying whatever score was made -- is a
promise about the service and not about a rubric.

Four promises, each checked from the boundary that would break it.

**Deterministic.** The same readings, club and hand give the same score, and
the coaching words beside it change nothing.

**Absent is not zero.** An unreadable cell leaves the total rather than
dragging it down, and too little evidence is no score rather than a low one.

**Every point is traceable.** Each component says what it read, how it was
graded and what it earned, and history gives all of that back.

**It survives the words.** A refused or failed coaching reply still settles a
line carrying the score, because the score never depended on the reply.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from tests.unit.test_coach_protee import (
    BEHIND_FRAME,
    SIDE_FRAME,
    Coach,
    a_reading,
    a_reply,
    an_open_face_grid,
    every_metric_null,
)

from caddiej.coaching.checkpoints import CHECKPOINTS, SCOREABLE
from caddiej.coaching.fakes import ScriptedProvider
from caddiej.coaching.score import (
    DELIVERY_WEIGHT,
    DELIVERY_WEIGHTS,
    DELIVERY_WEIGHTS_V3,
    MINIMUM_DELIVERY_WEIGHT,
    MINIMUM_SCORED_WEIGHT,
    RUBRIC,
    SMASH_TARGETS,
    VISUAL_WEIGHT,
    VISUAL_WEIGHTS,
    score_delivery_v1,
)
from caddiej.coaching.score import (
    score_swing as score_swing_v4,
)
from caddiej.coaching.score import (
    score_swing_v2 as score_swing,
)
from caddiej.coaching.vx import parse_metrics
from caddiej.domain.cues import EvidenceGrade, PanelView
from caddiej.domain.events import AnalysisCompleted
from caddiej.domain.shot import ClubKind, Handedness
from caddiej.domain.swing_checkpoints import CheckpointAssessment, CoverageLevel
from caddiej.domain.swing_score import (
    SWING_SCORE_RUBRIC_V1,
    SWING_SCORE_RUBRIC_V2,
    SWING_SCORE_RUBRIC_V3,
    SWING_SCORE_RUBRIC_V4,
    SWING_SCORE_RUBRIC_VERSION,
    ScoreComponent,
    ScoreComponentId,
    SwingScore,
    VisualObservation,
    VisualRating,
)
from caddiej.persistence.database import Database
from caddiej.persistence.records import swing_score_columns, swing_score_of
from caddiej.persistence.service import HistoryService


def a_grid(**readings: Any) -> dict[str, Any]:
    metrics = every_metric_null()
    metrics.update(readings)
    return metrics


def a_full_grid(**overrides: Any) -> dict[str, Any]:
    """Every scored cell readable: the worked example in docs/swing-score.md."""

    readings: dict[str, Any] = {
        "face_angle": a_reading(2.0, "deg", "open"),
        "swing_path": a_reading(3.0, "deg", "out_to_in"),
        "face_to_path": a_reading(5.0, "deg", "open"),
        "impact_width": a_reading(10.0, "mm", "toe"),
        "smash_factor": a_reading(1.45, "ratio"),
    }
    readings.update(overrides)
    return a_grid(**readings)


def scored(
    grid: dict[str, Any],
    club: ClubKind | None = ClubKind.DRIVER,
    handedness: Handedness = Handedness.RIGHT,
) -> SwingScore:
    return score_swing(parse_metrics(grid), club=club, handedness=handedness)


def component(score: SwingScore, name: ScoreComponentId) -> ScoreComponent:
    return next(item for item in score.components if item.component is name)


def a_look(
    name: ScoreComponentId,
    rating: VisualRating,
    confidence: float = 0.45,
    frame_id: str = "swing_behind_001",
) -> VisualObservation:
    return VisualObservation(
        component=name, rating=rating, confidence=confidence, frame_id=frame_id
    )


def watched(
    grid: dict[str, Any],
    *visual: VisualObservation,
    club: ClubKind | None = ClubKind.DRIVER,
) -> SwingScore:
    return score_swing(
        parse_metrics(grid),
        club=club,
        handedness=Handedness.RIGHT,
        visual=visual,
    )


def records(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == event]


def sound_mechanics() -> tuple[CheckpointAssessment, ...]:
    """What `a_reply`'s default checkpoint scores parse to.

    Written out rather than run through `parse_checkpoints`, so a change that
    silently stopped the fixture's citations from being admissible shows up as
    a failing comparison here rather than as two matching Nones."""

    return tuple(
        CheckpointAssessment(
            checkpoint=item.checkpoint_id,
            score=85,
            confidence=0.7,
            frame_id=BEHIND_FRAME if PanelView.BEHIND in item.views else SIDE_FRAME,
            phase=item.phase,
            view=(
                PanelView.BEHIND if PanelView.BEHIND in item.views else PanelView.SIDE
            ),
            observation="The body looks settled here.",
        )
        for item in SCOREABLE
    )


def as_the_service_scores(
    grid: dict[str, Any], club: ClubKind | None = ClubKind.DRIVER
) -> SwingScore:
    """The v4 score the live coach makes for `a_reply(grid)`.

    The service tests below run through the real coach, so they score under
    the live rubric rather than under v2 like the rest of this file."""

    return score_swing_v4(
        parse_metrics(grid),
        club=club,
        handedness=Handedness.RIGHT,
        checkpoints=sound_mechanics(),
    )


# --- the rubric -------------------------------------------------------------


class TestTheRubric:
    def test_the_weights_add_up_to_a_hundred(self) -> None:
        assert sum(ramp.weight for ramp in RUBRIC.values()) == 100
        assert [ramp.weight for ramp in RUBRIC.values()] == [25, 25, 20, 15, 15]

    def test_no_single_component_clears_the_minimum_alone(self) -> None:
        assert max(ramp.weight for ramp in RUBRIC.values()) < MINIMUM_SCORED_WEIGHT
        assert MINIMUM_SCORED_WEIGHT == 40
        assert max(DELIVERY_WEIGHTS.values()) < MINIMUM_DELIVERY_WEIGHT
        assert MINIMUM_DELIVERY_WEIGHT == 22

    def test_the_two_halves_divide_one_hundred(self) -> None:
        assert sum(DELIVERY_WEIGHTS.values()) == DELIVERY_WEIGHT == 55
        assert sum(VISUAL_WEIGHTS.values()) == VISUAL_WEIGHT == 45
        assert DELIVERY_WEIGHT + VISUAL_WEIGHT == 100

    def test_delivery_keeps_v1s_proportions(self) -> None:
        # Scaled, not re-argued: a face still matters more than a strike by
        # the same ratio it always did, to within the rounding to integers.
        for name, ramp in RUBRIC.items():
            assert DELIVERY_WEIGHTS[name] == pytest.approx(
                ramp.weight * DELIVERY_WEIGHT / 100, abs=0.6
            )

    def test_the_worked_example(self) -> None:
        score = scored(a_full_grid())

        assert score.value == 72
        assert score.available_weight == 55
        assert score.rubric_version == SWING_SCORE_RUBRIC_V2
        assert [item.sub_score for item in score.delivery] == pytest.approx(
            [0.8, 2.0 / 5.5, 5.0 / 6.0, 0.8, 1.0]
        )

    def test_a_grid_nobody_watched_is_a_delivery_score_and_says_so(self) -> None:
        score = scored(a_full_grid())

        assert score.delivery_value == score.value == 72
        assert score.visual_value is None
        assert score.ceiling is None
        assert all(item.is_absent for item in score.visual)
        assert score.available_weight == DELIVERY_WEIGHT

    def test_the_worked_example_without_a_path(self) -> None:
        score = scored(a_full_grid(swing_path=None, face_to_path=None))

        assert score.available_weight == 30
        assert score.value == 85

    @pytest.mark.parametrize(
        ("name", "metric", "unit", "direction", "full", "middle", "zero"),
        [
            (ScoreComponentId.FACE_CONTROL, "face_angle", "deg", "open", 1.0, 3.5, 6.0),
            (
                ScoreComponentId.FACE_TO_PATH,
                "face_to_path",
                "deg",
                "closed",
                1.5,
                4.25,
                7.0,
            ),
            (ScoreComponentId.PATH, "swing_path", "deg", "in_to_out", 2.0, 5.0, 8.0),
            (ScoreComponentId.STRIKE, "impact_width", "mm", "heel", 5.0, 17.5, 30.0),
        ],
    )
    def test_each_angle_and_strike_ramp(
        self,
        name: ScoreComponentId,
        metric: str,
        unit: str,
        direction: str,
        full: float,
        middle: float,
        zero: float,
    ) -> None:
        def rating(value: float) -> float | None:
            grid = a_grid(**{metric: a_reading(value, unit, direction)})
            return component(scored(grid), name).sub_score

        assert rating(0.0) == 1.0
        assert rating(full) == 1.0
        assert rating(middle) == pytest.approx(0.5)
        assert rating(zero) == 0.0
        assert rating(zero + 5.0) == 0.0

    @pytest.mark.parametrize(
        ("smash", "expected"),
        [(1.52, 1.0), (1.45, 1.0), (1.365, 0.5), (1.28, 0.0), (1.10, 0.0)],
    )
    def test_the_efficiency_ramp_on_the_drivers_target(
        self, smash: float, expected: float
    ) -> None:
        grid = a_grid(smash_factor=a_reading(smash, "ratio"))

        rating = component(scored(grid), ScoreComponentId.EFFICIENCY).sub_score

        assert SMASH_TARGETS[ClubKind.DRIVER] == 1.48
        assert rating == pytest.approx(expected)

    def test_a_strike_in_inches_is_rated_in_millimetres(self) -> None:
        grid = a_grid(impact_width=a_reading(0.5, "in", "toe"))

        strike = component(scored(grid), ScoreComponentId.STRIKE)

        assert strike.value == pytest.approx(12.7)
        assert strike.sub_score == pytest.approx((30.0 - 12.7) / 25.0)


# --- deterministic ----------------------------------------------------------


class TestDeterministic:
    def test_the_same_readings_always_give_the_same_score(self) -> None:
        scores = {scored(a_full_grid()) for _ in range(5)}

        assert len(scores) == 1

    def test_the_hand_does_not_move_the_number(self) -> None:
        right = scored(a_full_grid(), handedness=Handedness.RIGHT)
        left = scored(a_full_grid(), handedness=Handedness.LEFT)

        assert left.value == right.value
        assert [c.sub_score for c in left.components] == [
            c.sub_score for c in right.components
        ]

    def test_the_stored_text_is_the_same_every_time(self) -> None:
        first = swing_score_columns(scored(a_full_grid()))
        second = swing_score_columns(scored(a_full_grid()))

        assert first == second

    async def test_the_coaching_words_do_not_move_the_score(
        self, tmp_path: Path
    ) -> None:
        grid = an_open_face_grid(smash_factor=a_reading(1.44, "ratio"))
        scores = []
        for index, speech in enumerate(
            ("Square the face a little earlier.", "Close the face a touch sooner.")
        ):
            coach = Coach(ScriptedProvider(replies=(a_reply(grid, speech=speech),)))
            await coach.run(tmp_path / str(index))
            scores.append(coach.results[0].swing_score)

        assert scores[0] is not None
        assert scores[0] == scores[1]
        assert scores[0] == as_the_service_scores(grid)


# --- absent is not zero -----------------------------------------------------


class TestAbsentIsNotZero:
    def test_a_perfect_face_with_an_unreadable_path_scores_high(self) -> None:
        grid = a_grid(
            face_angle=a_reading(0.5, "deg", "open"),
            swing_path=None,
            impact_width=a_reading(3.0, "mm", "toe"),
            smash_factor=a_reading(1.47, "ratio"),
        )

        score = scored(grid)

        assert score.value == 100
        path = component(score, ScoreComponentId.PATH)
        assert path.grade is EvidenceGrade.ABSENT
        assert path.sub_score is None
        assert path.metrics == ("swing_path",)
        seen = {item.component for item in score.visual}
        assert {item.component for item in score.absent} - seen == {
            ScoreComponentId.PATH,
            ScoreComponentId.FACE_TO_PATH,
        }

    def test_a_rejected_reading_is_absent_too(self) -> None:
        grid = a_full_grid(swing_path=a_reading(3.0, "deg", "toe"))

        metrics = parse_metrics(grid)
        score = score_swing(metrics, club=ClubKind.DRIVER, handedness=Handedness.RIGHT)

        assert "swing_path" in metrics.rejected
        assert component(score, ScoreComponentId.PATH).is_absent
        assert score.available_weight == DELIVERY_WEIGHT - 11

    def test_a_direction_that_was_not_read_is_not_guessed(self) -> None:
        grid = a_full_grid(face_angle=a_reading(2.0, "deg"))

        face = component(scored(grid), ScoreComponentId.FACE_CONTROL)

        assert face.is_absent

    def test_the_mean_is_over_the_components_that_were_read(self) -> None:
        grid = a_grid(
            face_angle=a_reading(3.5, "deg", "closed"),
            impact_width=a_reading(5.0, "mm", "heel"),
        )

        score = scored(grid)

        assert score.available_weight == 22
        assert score.value == round(100 * (14 * 0.5 + 8 * 1.0) / 22)
        assert score.value == 68
        assert len(score.scored) == 2
        assert len(score.absent) == 7

    def test_below_the_minimum_weight_there_is_no_score_not_a_low_one(
        self,
    ) -> None:
        grid = a_grid(face_angle=a_reading(12.0, "deg", "open"))

        score = scored(grid)

        assert score.available_weight == 14 < MINIMUM_DELIVERY_WEIGHT
        assert score.value is None
        assert component(score, ScoreComponentId.FACE_CONTROL).sub_score == 0.0

    def test_a_watched_swing_cannot_rescue_an_unreadable_grid(self) -> None:
        # The visual half is worth forty-five points and still does not make a
        # score on its own: without a measurement of what the club did, a
        # number would be an impression with a decimal point on it.
        score = score_swing(
            parse_metrics(every_metric_null()),
            club=ClubKind.DRIVER,
            handedness=Handedness.RIGHT,
            visual=(
                a_look(ScoreComponentId.SWING_PLANE, VisualRating.GOOD),
                a_look(ScoreComponentId.POSTURE, VisualRating.GOOD),
            ),
        )

        assert score.value is None
        assert score.delivery_value is None
        assert score.visual_value == 100, "what was seen is still recorded"

    def test_nothing_read_is_no_score(self) -> None:
        score = scored(every_metric_null())

        assert score.value is None
        assert score.available_weight == 0
        assert len(score.absent) == len(DELIVERY_WEIGHTS) + len(VISUAL_WEIGHTS)

    @pytest.mark.parametrize("club", [None, ClubKind.UNKNOWN, ClubKind.PUTTER])
    def test_efficiency_needs_a_club_with_a_target(self, club: ClubKind | None) -> None:
        score = scored(a_full_grid(), club=club)

        efficiency = component(score, ScoreComponentId.EFFICIENCY)
        assert efficiency.is_absent
        assert efficiency.value == 1.45, "what was read is kept, unjudged"
        assert score.available_weight == DELIVERY_WEIGHT - 8


# --- provenance -------------------------------------------------------------


class TestProvenance:
    def test_a_face_to_path_read_from_the_grid_is_measured(self) -> None:
        face_to_path = component(scored(a_full_grid()), ScoreComponentId.FACE_TO_PATH)

        assert face_to_path.grade is EvidenceGrade.MEASURED
        assert face_to_path.metrics == ("face_to_path",)
        assert face_to_path.value == 5.0

    def test_a_face_to_path_worked_out_from_face_and_path_is_derived(self) -> None:
        grid = a_full_grid(face_to_path=None)

        face_to_path = component(scored(grid), ScoreComponentId.FACE_TO_PATH)

        assert face_to_path.grade is EvidenceGrade.DERIVED
        assert face_to_path.metrics == ("face_angle", "swing_path")
        assert face_to_path.value == pytest.approx(5.0)
        assert face_to_path.sub_score == pytest.approx(2.0 / 5.5)

    def test_every_component_says_what_it_read(self) -> None:
        score = scored(a_full_grid())

        assert [(c.component, c.weight, c.metrics) for c in score.delivery] == [
            (ScoreComponentId.FACE_CONTROL, 14, ("face_angle",)),
            (ScoreComponentId.FACE_TO_PATH, 14, ("face_to_path",)),
            (ScoreComponentId.PATH, 11, ("swing_path",)),
            (ScoreComponentId.STRIKE, 8, ("impact_width",)),
            (ScoreComponentId.EFFICIENCY, 8, ("smash_factor",)),
        ]
        assert [c.value for c in score.delivery] == [2.0, 5.0, -3.0, 10.0, 1.45]
        assert all(c.grade is EvidenceGrade.MEASURED for c in score.delivery)

    def test_a_visual_component_carries_the_frame_it_came_from(self) -> None:
        score = watched(
            a_full_grid(),
            a_look(
                ScoreComponentId.SWING_PLANE,
                VisualRating.FAULT,
                confidence=0.6,
                frame_id="swing_behind_002",
            ),
        )

        plane = component(score, ScoreComponentId.SWING_PLANE)
        assert plane.is_visual and not plane.is_absent
        assert plane.grade is EvidenceGrade.VISUAL
        assert plane.frame_id == "swing_behind_002"
        assert plane.confidence == 0.6
        assert plane.rating is VisualRating.FAULT
        assert plane.metrics == (), "a visual rating read no cell"

    def test_an_unwatched_component_is_absent_and_not_a_zero(self) -> None:
        score = watched(
            a_full_grid(), a_look(ScoreComponentId.SWING_PLANE, VisualRating.GOOD)
        )

        transition = component(score, ScoreComponentId.TRANSITION)
        assert transition.is_absent
        assert transition.sub_score is None
        assert transition.rating is None
        assert transition.frame_id is None
        assert transition.weight == VISUAL_WEIGHTS[ScoreComponentId.TRANSITION]
        assert (
            transition.weight not in (item.weight for item in score.scored)
            or transition not in score.scored
        )

    def test_a_visual_component_without_a_frame_cannot_exist(self) -> None:
        with pytest.raises(ValueError):
            VisualObservation(
                component=ScoreComponentId.POSTURE,
                rating=VisualRating.FAULT,
                confidence=0.45,
                frame_id="",
            )

    def test_a_delivery_component_cannot_be_rated_by_eye(self) -> None:
        with pytest.raises(ValueError):
            a_look(ScoreComponentId.STRIKE, VisualRating.FAULT)


# --- delivery is not the swing ----------------------------------------------


class TestExcellentDeliveryIsNotAnExcellentSwing:
    """The shot that started this: ninety-four for an over-the-top swing.

    Real readings, from capture `ea475a87-f9c3-4a1c-857d-a42471ecfc04`. Every
    delivery number is inside its band; the swing behind the ball is not.
    """

    def real_grid(self) -> dict[str, Any]:
        return a_grid(
            face_angle=a_reading(0.7, "deg", "open"),
            swing_path=a_reading(1.7, "deg", "out_to_in"),
            face_to_path=a_reading(2.4, "deg", "open"),
            impact_width=a_reading(8.75, "mm", "toe"),
            smash_factor=a_reading(1.49, "ratio"),
        )

    def test_v1_scored_it_in_the_nineties(self) -> None:
        v1 = score_delivery_v1(
            parse_metrics(self.real_grid()),
            club=ClubKind.DRIVER,
            handedness=Handedness.RIGHT,
        )

        assert v1.rubric_version == SWING_SCORE_RUBRIC_V1
        assert v1.value == 94

    def test_a_seen_fault_cannot_leave_it_near_perfect(self) -> None:
        score = watched(
            self.real_grid(),
            a_look(ScoreComponentId.SWING_PLANE, VisualRating.FAULT),
        )

        assert score.delivery_value == 94, "the delivery was excellent and still is"
        assert score.value is not None and score.value < 85
        assert score.ceiling == 84
        assert score.seen_faults

    def test_the_lightest_visual_fault_is_still_capped(self) -> None:
        # Balance is worth eight points of forty-five. Without the ceiling a
        # fault in it would move the total by a point or two and the player
        # would read ninety-something beside a fault they were just told about.
        score = watched(
            self.real_grid(), a_look(ScoreComponentId.BALANCE, VisualRating.FAULT)
        )

        assert score.value is not None and score.value <= 84

    def test_a_swing_nobody_faulted_keeps_its_delivery_score(self) -> None:
        score = watched(
            self.real_grid(),
            a_look(ScoreComponentId.SWING_PLANE, VisualRating.GOOD),
            a_look(ScoreComponentId.POSTURE, VisualRating.GOOD),
        )

        assert score.ceiling is None
        assert score.value is not None and score.value >= 94

    def test_a_minor_is_visible_and_not_a_fault(self) -> None:
        score = watched(
            self.real_grid(), a_look(ScoreComponentId.POSTURE, VisualRating.MINOR)
        )

        assert score.ceiling == 93
        assert score.value == 87, "the weights already took it below the cap"
        assert not score.seen_faults

    def test_v1_and_v2_are_different_rubrics_and_both_remain(self) -> None:
        metrics = parse_metrics(self.real_grid())

        v1 = score_delivery_v1(
            metrics, club=ClubKind.DRIVER, handedness=Handedness.RIGHT
        )
        v2 = score_swing(metrics, club=ClubKind.DRIVER, handedness=Handedness.RIGHT)

        assert v1.rubric_version == SWING_SCORE_RUBRIC_V1 == "swing-score-v1"
        assert v2.rubric_version == SWING_SCORE_RUBRIC_V2 == "swing-score-v2"
        assert SWING_SCORE_RUBRIC_V3 == "swing-score-v3" != SWING_SCORE_RUBRIC_V2
        assert SWING_SCORE_RUBRIC_V4 == "swing-score-v4" != SWING_SCORE_RUBRIC_V3
        # And v4 is what production writes, so none of the above is
        # comparable with a row written tonight.
        assert SWING_SCORE_RUBRIC_VERSION == SWING_SCORE_RUBRIC_V4


# --- through the service and into history -----------------------------------


class TestThroughTheService:
    async def test_the_score_is_logged_with_its_provenance(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO")
        coach = Coach(ScriptedProvider(replies=(a_reply(),)))

        await coach.run(tmp_path)

        record = records(caplog, "coaching_swing_score")[0]
        # A 6.5 degree open face on an otherwise sound swing: the Swing Score
        # is the swing, and the face is ProTee's delivery, scored apart; the
        # strike and the smash factor were not on this grid at all.
        assert record.swing_score == 85
        assert record.swing_score_rubric == SWING_SCORE_RUBRIC_V4
        assert record.swing_score_weight == 70
        assert record.swing_score_club == "driver"
        assert record.swing_score_components == (
            "face_control=measured:0.00,face_to_path=derived:0.00,"
            "path=measured:1.00,strike=absent,efficiency=absent"
        )
        assert record.swing_score_absent == "strike,efficiency"

    async def test_a_coached_line_carries_the_score_to_the_floor(
        self, tmp_path: Path
    ) -> None:
        coach = Coach(ScriptedProvider(replies=(a_reply(),)))

        await coach.run(tmp_path)

        advice = coach.bus.of(AnalysisCompleted)[0].advice
        assert advice.swing_score == as_the_service_scores(an_open_face_grid())
        assert coach.results[0].status == "ok"

    @pytest.mark.parametrize(
        "reply",
        [
            a_reply(kind="none", cue=None, speech=None),
            a_reply(cue="PATH_IN_TO_OUT"),
            a_reply(speech="Your face was open, so shift your weight and turn."),
        ],
        ids=["declined", "unsupported_cue", "refused_words"],
    )
    async def test_a_refused_reply_keeps_the_score(
        self, tmp_path: Path, reply: dict[str, Any]
    ) -> None:
        coach = Coach(ScriptedProvider(replies=(reply,)))

        await coach.run(tmp_path)

        result = coach.results[0]
        assert result.cue_id == "FACE_OPEN"
        assert result.swing_score == as_the_service_scores(an_open_face_grid())

    async def test_no_all_values_is_a_swing_score_without_a_delivery(
        self, tmp_path: Path
    ) -> None:
        coach = Coach(ScriptedProvider(replies=(a_reply(),)))

        await coach.run(tmp_path, metrics=None)

        score = coach.results[0].swing_score
        assert score is not None and score.value == 85
        assert score.delivery_value is None


class TestHistory:
    def test_a_score_survives_its_json(self) -> None:
        score = scored(a_full_grid(face_to_path=None), club=ClubKind.IRON)

        text = swing_score_columns(score)["swing_score_json"]

        assert isinstance(text, str)
        assert swing_score_of(text) == score

    async def test_the_score_is_stored_and_read_back_as_a_trend(
        self, tmp_path: Path
    ) -> None:
        history = HistoryService(database=Database(tmp_path / "history.db"))
        await history.open()
        grids = [
            an_open_face_grid(face_angle=a_reading(angle, "deg", "open"))
            for angle in (4.0, 3.0, 2.0)
        ]
        for index, grid in enumerate(grids):
            coach = Coach(ScriptedProvider(replies=(a_reply(grid),)))
            coach.service._record_coach = history.record_coach_result  # noqa: SLF001
            await coach.run(tmp_path / str(index), trigger_id=str(20 + index))

        trend = await history.swing_scores(ClubKind.DRIVER)
        other_club = await history.swing_scores(ClubKind.IRON)
        rows = await history.coach_results()
        await history.aclose()

        assert [item.score for item in trend] == [
            as_the_service_scores(g) for g in grids
        ]
        # The swing was the same each time, so the Swing Score is; ProTee's
        # delivery is its own number and improved with the face.
        assert {item.score.value for item in trend} == {85}
        delivery = [item.score.delivery_value for item in trend]
        assert delivery == sorted(delivery) and len(set(delivery)) == 3
        assert other_club == []
        assert rows[-1]["swing_score_rubric"] == SWING_SCORE_RUBRIC_V4
        assert rows[-1]["swing_score_club"] == "driver"
        assert rows[-1]["swing_score"] == trend[-1].score.value
        component_names = [c.component for c in trend[-1].score.components]
        assert component_names == list(DELIVERY_WEIGHTS_V3)
        # And the swing half survives whole: every checkpoint, nine phases
        # and the coverage that decided whether a number was allowed at all.
        mechanics = trend[-1].score.mechanics
        assert mechanics is not None
        assert len(mechanics.checkpoints) == len(CHECKPOINTS)
        assert len(mechanics.phases) == 9
        assert mechanics.coverage_level is CoverageLevel.FULL

    def test_a_v1_document_reads_back_as_v1(self) -> None:
        # History is not rescored. A row written before CaddieJ could look at
        # a swing is a five-component delivery score and stays one, with the
        # v2 fields absent rather than filled in with zeros.
        v1 = score_delivery_v1(
            parse_metrics(a_full_grid()),
            club=ClubKind.DRIVER,
            handedness=Handedness.RIGHT,
        )

        text = swing_score_columns(v1)["swing_score_json"]
        assert isinstance(text, str)
        read = swing_score_of(text)

        assert read == v1
        assert read.rubric_version == SWING_SCORE_RUBRIC_V1
        assert read.value == 73
        assert len(read.components) == len(RUBRIC)
        assert read.visual_value is None and read.ceiling is None

    def test_a_watched_score_survives_its_json_whole(self) -> None:
        score = watched(
            a_full_grid(),
            a_look(
                ScoreComponentId.SWING_PLANE,
                VisualRating.FAULT,
                confidence=0.6,
                frame_id="swing_behind_003",
            ),
        )

        text = swing_score_columns(score)["swing_score_json"]
        assert isinstance(text, str)
        read = swing_score_of(text)

        assert read == score
        plane = component(read, ScoreComponentId.SWING_PLANE)
        assert plane.frame_id == "swing_behind_003"
        assert plane.rating is VisualRating.FAULT
        assert plane.confidence == 0.6
        assert read.ceiling == 84 and read.delivery_value == 72
