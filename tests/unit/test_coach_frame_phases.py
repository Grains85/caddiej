"""A rating about a moment may not be read from a picture of another moment.

The evidence that produced this file, from the first session with phase-aimed
frames. CaddieJ aimed its cameras with a stopwatch and announced what it
thought it had bought. It was wrong twice in the same shot:

    labelled                    actually showed
    swing_behind_04 ~impact     the finish, ball already gone
    swing_side_03   ~impact     the top of the backswing

The model then rated `balance` -- "whether the finish was held" -- from the top
of the backswing, and `posture` -- "whether the angles set at address survived
to impact" -- from a post-impact finish frame. Both came back `good` at the
confidence ceiling, and the shot scored a visual 100.

None of that is the model being careless. It was asked about a phase, handed a
picture, and given no way to say "that is not what this is". So the contract
now asks it what each picture shows before it rates anything, and a rating
whose cited frame shows an incompatible moment is refused.

The identification is the model's, not CaddieJ's, because CaddieJ's was the
thing that was wrong.
"""

from __future__ import annotations

import pytest

from caddiej.coaching import vx
from caddiej.coaching.checkpoints import CHECKPOINTS
from caddiej.domain.cues import PanelView
from caddiej.domain.swing_checkpoints import CheckpointId
from caddiej.domain.swing_phase import SwingPhase
from caddiej.domain.swing_score import ScoreComponentId

BODY = ("swing_behind_02", "swing_behind_03", "swing_behind_04", "swing_side_03")


def a_rating(frame_id: str, rating: str = "good") -> dict[str, object]:
    return {"rating": rating, "confidence": 0.8, "frame_id": frame_id}


def a_swing_block(**ratings: dict[str, object]) -> dict[str, object]:
    """A `swing` block with only the named parts rated and the rest null."""

    block: dict[str, object] = {component.value: None for component in vx.VISUAL_ORDER}
    block.update(ratings)
    return block


# --- reading the identification --------------------------------------------


class TestWhatTheModelSaysItSaw:
    def test_each_frame_is_mapped_to_the_phase_it_shows(self) -> None:
        seen = vx.parse_frames(
            [
                {"frame_id": "swing_behind_02", "shows": "top"},
                {"frame_id": "swing_side_03", "shows": "finish"},
            ]
        )

        assert seen == {
            "swing_behind_02": SwingPhase.TOP,
            "swing_side_03": SwingPhase.FINISH,
        }

    def test_unclear_is_recorded_as_not_knowing_rather_than_as_a_phase(self) -> None:
        """A real answer, and the one a blurred or blocked picture deserves.

        A model forced to name a phase for a picture it cannot read would be
        manufacturing the certainty this field exists to remove.
        """

        seen = vx.parse_frames([{"frame_id": "swing_behind_02", "shows": "unclear"}])

        assert seen == {}

    @pytest.mark.parametrize(
        "raw",
        [None, "frames", 42, [{"frame_id": "x"}], [{"shows": "top"}], [None], {}],
    )
    def test_a_reply_that_says_nothing_usable_yields_nothing(self, raw: object) -> None:
        assert vx.parse_frames(raw) == {}

    def test_a_phase_this_build_has_never_heard_of_is_not_a_phase(self) -> None:
        seen = vx.parse_frames([{"frame_id": "swing_behind_02", "shows": "waggle"}])

        assert seen == {}


# --- enforcing it ----------------------------------------------------------


class TestARatingMustCiteAPictureOfTheRightMoment:
    def test_balance_cannot_be_read_from_the_top_of_the_backswing(self) -> None:
        """The exact rating the session shipped, now refused."""

        observations, rejected = vx.parse_visual(
            a_swing_block(balance=a_rating("swing_side_03")),
            body_frames=BODY,
            frame_phases={"swing_side_03": SwingPhase.TOP},
        )

        assert observations == ()
        assert "balance" in rejected

    def test_balance_can_be_read_from_the_finish(self) -> None:
        observations, rejected = vx.parse_visual(
            a_swing_block(balance=a_rating("swing_side_03")),
            body_frames=BODY,
            frame_phases={"swing_side_03": SwingPhase.FINISH},
        )

        assert rejected == ()
        assert [item.component for item in observations] == [ScoreComponentId.BALANCE]

    def test_posture_cannot_be_read_from_a_finish_with_the_ball_gone(self) -> None:
        """The session's other one. Whether the angles survived to impact is
        not a question a photograph taken after impact answers."""

        observations, rejected = vx.parse_visual(
            a_swing_block(posture=a_rating("swing_behind_04")),
            body_frames=BODY,
            frame_phases={"swing_behind_04": SwingPhase.FINISH},
        )

        assert observations == ()
        assert "posture" in rejected

    def test_posture_may_be_read_from_the_address_as_well_as_the_strike(self) -> None:
        """The one thing an address frame genuinely establishes: angles that
        were never set cannot survive."""

        for phase in (SwingPhase.SETUP, SwingPhase.DOWNSWING, SwingPhase.IMPACT):
            observations, rejected = vx.parse_visual(
                a_swing_block(posture=a_rating("swing_behind_04")),
                body_frames=BODY,
                frame_phases={"swing_behind_04": phase},
            )

            assert rejected == (), phase
            assert len(observations) == 1, phase

    def test_a_plane_rating_needs_the_club_moving_not_the_address(self) -> None:
        refused, rejected = vx.parse_visual(
            a_swing_block(swing_plane=a_rating("swing_behind_02")),
            body_frames=BODY,
            frame_phases={"swing_behind_02": SwingPhase.SETUP},
        )
        allowed, _ = vx.parse_visual(
            a_swing_block(swing_plane=a_rating("swing_behind_02")),
            body_frames=BODY,
            frame_phases={"swing_behind_02": SwingPhase.TRANSITION},
        )

        assert refused == ()
        assert "swing_plane" in rejected
        assert len(allowed) == 1

    def test_one_bad_citation_does_not_cost_the_other_ratings(self) -> None:
        observations, rejected = vx.parse_visual(
            a_swing_block(
                transition=a_rating("swing_behind_03"),
                balance=a_rating("swing_side_03"),
            ),
            body_frames=BODY,
            frame_phases={
                "swing_behind_03": SwingPhase.TRANSITION,
                "swing_side_03": SwingPhase.TOP,
            },
        )

        assert [item.component for item in observations] == [
            ScoreComponentId.TRANSITION
        ]
        assert "balance" in rejected


class TestSilenceIsNotDisagreement:
    def test_a_reply_with_no_identifications_is_checked_as_it_always_was(self) -> None:
        """The old behaviour, and the right answer for a reply that never
        claimed to know what it was looking at."""

        observations, rejected = vx.parse_visual(
            a_swing_block(balance=a_rating("swing_side_03")),
            body_frames=BODY,
            frame_phases={},
        )

        assert rejected == ()
        assert len(observations) == 1

    def test_a_frame_the_model_declined_to_place_keeps_its_rating(self) -> None:
        """Refusing here would punish the honesty the field collects.

        A model that says `unclear` for one picture and rates from it anyway
        is telling CaddieJ something useful about its own uncertainty, and the
        confidence it gave is where that belongs -- not in a silent discard.
        """

        observations, rejected = vx.parse_visual(
            a_swing_block(balance=a_rating("swing_side_03")),
            body_frames=BODY,
            frame_phases={"swing_behind_02": SwingPhase.TOP},
        )

        assert rejected == ()
        assert len(observations) == 1


class TestTheMapIsCoherent:
    def test_every_rated_component_says_which_moments_establish_it(self) -> None:
        for component in vx.VISUAL_ORDER:
            assert component in vx.COMPONENT_PHASES, component
            assert vx.COMPONENT_PHASES[component], component

    def test_no_component_may_be_read_from_the_whole_swing(self) -> None:
        """A map that admitted everything would be decoration."""

        for component, phases in vx.COMPONENT_PHASES.items():
            assert len(phases) < len(SwingPhase), component

    def test_balance_is_the_narrowest_because_a_finish_is_one_moment(self) -> None:
        assert vx.COMPONENT_PHASES[ScoreComponentId.BALANCE] == frozenset(
            {SwingPhase.FINISH}
        )


# --- the same rule over the checkpoint catalogue ----------------------------

VIEWS = {
    "swing_behind_02": PanelView.BEHIND,
    "swing_behind_03": PanelView.BEHIND,
    "swing_behind_04": PanelView.BEHIND,
    "swing_side_03": PanelView.SIDE,
}


def a_score(frame_id: str, phase: SwingPhase, score: int = 80) -> dict[str, object]:
    return {
        "score": score,
        "confidence": 0.8,
        "frame_id": frame_id,
        "phase": phase.value,
        "view": "face_on" if frame_id.startswith("swing_side") else "down_the_line",
        "observation": "",
        "deviation": None,
    }


def checkpoints(raw: dict[str, object], frame_phases: dict[str, SwingPhase]):
    return vx.parse_checkpoints(
        raw, body_frames=BODY, frame_phases=frame_phases, frame_views=VIEWS
    )


class TestACheckpointMustCiteAPictureOfTheRightMoment:
    """Swing Score V4's checkpoints, held to the rule that produced this file.

    Narrower than any earlier rubric: a checkpoint is one relationship at one
    position, so there are more ways for a citation to be about something
    else, and each is refused.
    """

    def test_the_finish_cannot_be_judged_from_the_top_of_the_backswing(self) -> None:
        assessments, rejected = checkpoints(
            {"finish_balance": a_score("swing_side_03", SwingPhase.FINISH)},
            {"swing_side_03": SwingPhase.TOP},
        )

        assert assessments == ()
        assert rejected == (("finish_balance", "phase_conflict"),)

    def test_and_can_be_judged_from_the_finish(self) -> None:
        assessments, rejected = checkpoints(
            {"finish_balance": a_score("swing_side_03", SwingPhase.FINISH)},
            {"swing_side_03": SwingPhase.FINISH},
        )

        assert rejected == ()
        assert [item.checkpoint for item in assessments] == [
            CheckpointId.FINISH_BALANCE
        ]
        assert assessments[0].phase is SwingPhase.FINISH

    def test_the_models_identification_beats_caddiejs_stopwatch(self) -> None:
        """Which is the whole reason the `frames` block exists.

        CaddieJ aimed `swing_behind_04` at impact and asked about impact
        posture. The model says the ball had already gone. The model wins,
        and the checkpoint goes unscored rather than being answered from a
        picture of a different moment.
        """

        assessments, rejected = checkpoints(
            {"impact_posture": a_score("swing_behind_04", SwingPhase.IMPACT)},
            {"swing_behind_04": SwingPhase.FINISH},
        )

        assert assessments == ()
        assert rejected == (("impact_posture", "phase_conflict"),)

    def test_a_camera_that_cannot_see_it_cannot_score_it_either(self) -> None:
        # Drift off the ball is a face-on observation. The same score cited
        # from down the line is an equally confident answer to a question
        # that view cannot settle.
        wrong_view, rejected = checkpoints(
            {"backswing_centre": a_score("swing_behind_03", SwingPhase.BACKSWING)}, {}
        )
        right_view, kept = checkpoints(
            {"backswing_centre": a_score("swing_side_03", SwingPhase.BACKSWING)}, {}
        )

        assert wrong_view == () and rejected == (("backswing_centre", "view"),)
        assert kept == () and len(right_view) == 1

    def test_one_bad_citation_does_not_cost_the_other_checkpoints(self) -> None:
        assessments, rejected = checkpoints(
            {
                "transition_pelvis_initiation": a_score(
                    "swing_behind_03", SwingPhase.TRANSITION
                ),
                "finish_balance": a_score("swing_side_03", SwingPhase.FINISH),
            },
            {
                "swing_behind_03": SwingPhase.TRANSITION,
                "swing_side_03": SwingPhase.TOP,
            },
        )

        assert [item.checkpoint for item in assessments] == [
            CheckpointId.TRANSITION_PELVIS_INITIATION
        ]
        assert rejected == (("finish_balance", "phase_conflict"),)

    def test_a_frame_the_model_declined_to_place_keeps_its_score(self) -> None:
        # The checkpoint's own `phase` still has to be one of its moments.
        assessments, rejected = checkpoints(
            {"finish_balance": a_score("swing_side_03", SwingPhase.FINISH)}, {}
        )

        assert rejected == ()
        assert len(assessments) == 1

    def test_the_all_values_grid_is_not_a_picture_of_a_swing(self) -> None:
        assessments, rejected = checkpoints(
            {"setup_hip_hinge": a_score(vx.METRICS_FRAME_ID, SwingPhase.SETUP)}, {}
        )

        assert assessments == ()
        assert rejected == (("setup_hip_hinge", "metrics_frame"),)

    def test_every_checkpoint_names_the_moments_and_the_views_that_settle_it(
        self,
    ) -> None:
        for item in CHECKPOINTS:
            assert item.phases, item.checkpoint_id
            assert item.views, item.checkpoint_id
            assert len(item.phases) <= 2, item.checkpoint_id
