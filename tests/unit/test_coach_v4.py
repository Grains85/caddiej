"""Swing Score V4: checkpoints, phases, faults, one root cause, one lesson.

The model scores what it can see, checkpoint by checkpoint, 0 to 100, and
every number after that is CaddieJ's: the phase scores, the Swing Score, the
faults those scores amount to, which of them caused the rest, and whether the
player fixed it on the next ball. Each section below holds one of those
steps to the rule that justifies it.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from tests.unit.schema_support import (
    checkpoint_entry,
    checkpoint_slot,
    citable_cells,
    fixed_length,
    resolved,
    wire_line,
    wire_list,
)
from tests.unit.test_coach_protee import (
    BEHIND_FRAME,
    DRIVER,
    SIDE_FRAME,
    Coach,
    a_checkpoint,
    a_reading,
    a_reply,
    a_swing,
    an_open_face_grid,
    every_metric_null,
    no_swing,
)

from caddiej.adapters.hud.protocol import AdvicePayload
from caddiej.coaching import vx
from caddiej.coaching.checkpoints import (
    CHECKPOINTS,
    CHECKPOINTS_BY_ID,
    NON_TOUR_CEILING,
    SCOREABLE,
)
from caddiej.coaching.cues import CUE_SPEECH, CUES_BY_ID, cue_speech
from caddiej.coaching.fakes import ScriptedProvider
from caddiej.coaching.lesson import LessonStep, LessonTracker
from caddiej.coaching.mechanics import CLUB_MOTION, club_motion, score_mechanics
from caddiej.coaching.prompt import MAX_DISPLAY_CHARACTERS
from caddiej.coaching.score import score_swing
from caddiej.coaching.swing_faults import (
    SWING_FAULTS,
    SWING_FAULTS_BY_ID,
    corroborate,
    infer_faults,
    protee_signs,
    rank_root_cause,
)
from caddiej.coaching.vx import (
    METRICS_FRAME_ID,
    SYSTEM,
    parse_checkpoints,
    parse_metrics,
    reply_schema,
)
from caddiej.domain.coaching import AdviceKind
from caddiej.domain.cues import CueId, PanelView
from caddiej.domain.events import AnalysisCompleted, ClubSelected
from caddiej.domain.ports import CoachingResult, LessonLine, PhaseLine
from caddiej.domain.shot import ClubKind, Handedness
from caddiej.domain.swing_checkpoints import (
    CheckpointAssessment,
    CheckpointId,
    CoverageLevel,
    Deviation,
    LessonMode,
    LessonVerdict,
    SwingFaultId,
)
from caddiej.domain.swing_phase import PHASE_ORDER, SwingPhase, phase_label
from caddiej.domain.swing_review import (
    LessonSource,
    ProteeSign,
    RootCause,
    SwingReview,
)
from caddiej.domain.swing_score import SWING_SCORE_RUBRIC_V4, MechanicalScore
from caddiej.floor.state import FloorAdvice, FloorLesson, FloorPhase
from caddiej.persistence.records import (
    review_document,
    review_of,
    swing_score_columns,
    swing_score_of,
)

C = CheckpointId
D = Deviation
F = SwingFaultId

FRAMES = (BEHIND_FRAME, SIDE_FRAME)
VIEWS = {BEHIND_FRAME: PanelView.BEHIND, SIDE_FRAME: PanelView.SIDE}


# --- building swings ----------------------------------------------------------


def seen(
    checkpoint: CheckpointId,
    score: int,
    *,
    confidence: float = 0.8,
    deviation: Deviation | None = None,
) -> CheckpointAssessment:
    """One checkpoint scored from a camera and a moment it admits."""

    spec = CHECKPOINTS_BY_ID[checkpoint]
    view = PanelView.BEHIND if PanelView.BEHIND in spec.views else PanelView.SIDE
    return CheckpointAssessment(
        checkpoint=checkpoint,
        score=score,
        confidence=confidence,
        frame_id=BEHIND_FRAME if view is PanelView.BEHIND else SIDE_FRAME,
        phase=spec.phase,
        view=view,
        deviation=deviation,
    )


Change = int | tuple[int, Deviation] | CheckpointAssessment | None


def swing(base: int | None = 85, **changes: Change) -> MechanicalScore:
    """Every scoreable checkpoint at `base`, then the named ones changed.

    A tuple is a score and the direction it missed in; None takes the
    checkpoint out, which is what a picture that did not show it does."""

    items: dict[CheckpointId, CheckpointAssessment] = (
        {}
        if base is None
        else {spec.checkpoint_id: seen(spec.checkpoint_id, base) for spec in SCOREABLE}
    )
    for name, value in changes.items():
        checkpoint = C(name)
        if value is None:
            items.pop(checkpoint, None)
        elif isinstance(value, CheckpointAssessment):
            items[checkpoint] = value
        elif isinstance(value, tuple):
            items[checkpoint] = seen(checkpoint, value[0], deviation=value[1])
        else:
            items[checkpoint] = seen(checkpoint, value)
    return score_mechanics(tuple(items.values()))


def only(phases: set[SwingPhase], score: int) -> dict[str, Change]:
    """Every scoreable checkpoint of these phases at `score`."""

    return {
        spec.checkpoint_id.value: score for spec in SCOREABLE if spec.phase in phases
    }


def root_of(
    mechanics: MechanicalScore,
    *,
    session_fault: SwingFaultId | None = None,
    signs: tuple[ProteeSign, ...] = (),
) -> RootCause:
    faults = corroborate(infer_faults(mechanics), signs)
    return rank_root_cause(faults, session_fault=session_fault)


def faults_in(mechanics: MechanicalScore) -> set[SwingFaultId]:
    return {item.fault for item in infer_faults(mechanics)}


def coach(tracker: LessonTracker, mechanics: MechanicalScore) -> LessonStep:
    """One shot through the deterministic loop, as the service runs it."""

    faults = infer_faults(mechanics)
    root = rank_root_cause(faults, session_fault=tracker.fault)
    step = tracker.step(mechanics, faults, root)
    tracker.remember_swing(mechanics, mechanics.value)
    return step


def parsed(raw: dict[str, Any], **phases: SwingPhase):
    return parse_checkpoints(
        raw, body_frames=FRAMES, frame_phases=phases or None, frame_views=VIEWS
    )


def a_chicken_wing(score: int, *, bent: bool = True) -> dict[str, Change]:
    deviation = D.BENT if bent else None
    if deviation is None:
        return {
            C.RELEASE_LEAD_ARM_STRUCTURE.value: score,
            C.IMPACT_ARM_STRUCTURE.value: score,
        }
    return {
        C.RELEASE_LEAD_ARM_STRUCTURE.value: (score, deviation),
        C.IMPACT_ARM_STRUCTURE.value: (score, deviation),
    }


# --- the catalogue ------------------------------------------------------------


class TestTheCatalogue:
    def test_every_position_from_p1_to_p9_has_checkpoints_this_bay_can_score(
        self,
    ) -> None:
        assert len(PHASE_ORDER) == 9
        for phase in PHASE_ORDER:
            assert sum(spec.phase is phase for spec in SCOREABLE) >= 2, phase

    def test_each_checkpoint_declares_where_and_when_it_may_be_read(self) -> None:
        for spec in CHECKPOINTS:
            assert spec.phase in spec.phases, spec.checkpoint_id
            assert spec.views, spec.checkpoint_id
            assert spec.views <= {PanelView.BEHIND, PanelView.SIDE}
            assert spec.weight > 0
            assert 0.5 <= spec.min_confidence <= 1.0

    def test_down_the_line_and_face_on_are_not_interchangeable(self) -> None:
        assert CHECKPOINTS_BY_ID[C.TRANSITION_TRAIL_SHOULDER].views == {
            PanelView.BEHIND
        }
        assert CHECKPOINTS_BY_ID[C.DELIVERY_HIP_DEPTH].views == {PanelView.BEHIND}
        assert CHECKPOINTS_BY_ID[C.TOP_CENTRE].views == {PanelView.SIDE}
        assert CHECKPOINTS_BY_ID[C.IMPACT_HAND_POSITION].views == {PanelView.SIDE}

    def test_what_the_bay_cannot_see_is_never_asked_for(self) -> None:
        unseen = {spec.checkpoint_id for spec in CHECKPOINTS if not spec.scoreable}
        assert {C.TOP_CLUBFACE, C.TAKEAWAY_CLUBFACE, C.IMPACT_SHAFT_LEAN} <= unseen
        schema = reply_schema((CueId.FACE_OPEN,), frame_ids=FRAMES)
        offered = {
            spec.checkpoint_id for specs in vx.WIRE_LISTS.values() for spec in specs
        }
        assert offered == {spec.checkpoint_id for spec in CHECKPOINTS if spec.scoreable}
        assert not unseen & offered
        for key, specs in vx.WIRE_LISTS.items():
            assert fixed_length(wire_list(schema, key)) == len(specs)

    def test_the_reverse_index_is_the_fault_library(self) -> None:
        for spec in CHECKPOINTS:
            named = {
                fault.fault_id
                for fault in SWING_FAULTS
                if spec.checkpoint_id in fault.checkpoints
                or spec.checkpoint_id in fault.supporting
            }
            assert set(spec.faults) == named, spec.checkpoint_id

    def test_a_trigger_only_asks_for_directions_its_checkpoint_has(self) -> None:
        for fault in SWING_FAULTS:
            for trigger in fault.triggers:
                spec = CHECKPOINTS_BY_ID[trigger.checkpoint]
                assert spec.scoreable, (fault.fault_id, trigger.checkpoint)
                assert trigger.deviations <= set(spec.deviations), (
                    fault.fault_id,
                    trigger.checkpoint,
                )

    def test_the_cause_graph_has_no_cycles(self) -> None:
        for fault in SWING_FAULTS:
            pending = list(fault.causes)
            visited: set[SwingFaultId] = set()
            while pending:
                item = pending.pop()
                assert item is not fault.fault_id, f"{fault.fault_id} causes itself"
                if item in visited:
                    continue
                visited.add(item)
                pending.extend(SWING_FAULTS_BY_ID[item].causes)


# --- 1: a checkpoint score is checked, never trusted --------------------------


class TestACheckpointScoreIsChecked:
    def test_each_score_carries_everything_needed_to_check_it(self) -> None:
        items, rejected = parsed(
            {"transition_trail_shoulder": a_checkpoint(C.TRANSITION_TRAIL_SHOULDER, 42)}
        )

        assert rejected == ()
        (item,) = items
        assert item.checkpoint is C.TRANSITION_TRAIL_SHOULDER
        assert item.score == 42
        assert item.confidence == 0.7
        assert item.frame_id == BEHIND_FRAME
        assert item.phase is SwingPhase.TRANSITION
        assert item.view is PanelView.BEHIND
        assert item.observation

    def test_a_null_checkpoint_is_absent_and_never_zero(self) -> None:
        items, rejected = parsed({"setup_hip_hinge": None})
        mechanics = score_mechanics(items)

        assert items == () and rejected == ()
        assert mechanics.score_of(C.SETUP_HIP_HINGE) is None
        assert mechanics.value is None

    def test_the_wrong_camera_is_refused(self) -> None:
        face_on = a_checkpoint(C.TRANSITION_TRAIL_SHOULDER, 40) | {
            "frame_id": SIDE_FRAME,
            "view": "face_on",
        }

        items, rejected = parsed({"transition_trail_shoulder": face_on})

        assert items == ()
        assert rejected == (("transition_trail_shoulder", "view"),)

    def test_a_view_that_is_not_the_camera_the_frame_came_from_is_refused(
        self,
    ) -> None:
        lying = a_checkpoint(C.TRANSITION_TRAIL_SHOULDER, 40) | {"view": "face_on"}

        _, rejected = parsed({"transition_trail_shoulder": lying})

        assert rejected == (("transition_trail_shoulder", "view_mismatch"),)

    def test_the_wrong_moment_is_refused(self) -> None:
        at_the_top = a_checkpoint(C.FINISH_BALANCE, 80, phase="top")

        _, rejected = parsed({"finish_balance": at_the_top})

        assert rejected == (("finish_balance", "phase"),)

    def test_a_moment_its_own_frames_block_contradicts_is_refused(self) -> None:
        _, rejected = parsed(
            {"finish_balance": a_checkpoint(C.FINISH_BALANCE, 80)},
            **{BEHIND_FRAME: SwingPhase.TOP},
        )

        assert rejected == (("finish_balance", "phase_conflict"),)

    def test_low_confidence_is_refused_against_the_checkpoints_own_minimum(
        self,
    ) -> None:
        unsure = a_checkpoint(C.TOP_HAND_DEPTH, 40, confidence=0.5)

        items, rejected = parsed({"top_hand_depth": unsure})

        assert CHECKPOINTS_BY_ID[C.TOP_HAND_DEPTH].min_confidence == 0.6
        assert items == ()
        assert rejected == (("top_hand_depth", "low_confidence"),)

    def test_all_values_is_never_evidence_about_a_swing(self) -> None:
        grid = a_checkpoint(C.SETUP_HIP_HINGE, 90, frame_id=METRICS_FRAME_ID)

        _, rejected = parse_checkpoints(
            {"setup_hip_hinge": grid},
            body_frames=(*FRAMES, METRICS_FRAME_ID),
            frame_views=VIEWS,
        )

        assert rejected == (("setup_hip_hinge", "metrics_frame"),)

    def test_a_score_outside_the_scale_is_refused(self) -> None:
        for bad in (101, -1, True, "90"):
            _, rejected = parsed(
                {"setup_hip_hinge": a_checkpoint(C.SETUP_HIP_HINGE) | {"score": bad}}
            )
            assert rejected == (("setup_hip_hinge", "score"),), bad

    def test_a_direction_the_checkpoint_does_not_have_is_refused(self) -> None:
        sideways = a_checkpoint(C.TRANSITION_TRAIL_SHOULDER, 40, deviation="inside")

        _, rejected = parsed({"transition_trail_shoulder": sideways})

        assert rejected == (("transition_trail_shoulder", "deviation"),)

    def test_a_checkpoint_the_bay_cannot_assess_is_refused(self) -> None:
        _, rejected = parsed({"top_clubface": a_checkpoint(C.SETUP_HIP_HINGE)})

        assert rejected == (("top_clubface", "not_scoreable"),)


# --- 2: the model cannot compute anything -------------------------------------


class TestTheModelOnlyScoresCheckpoints:
    def test_the_reply_has_nowhere_to_put_a_score_a_fault_or_a_lesson(self) -> None:
        schema = reply_schema((CueId.FACE_OPEN,), frame_ids=FRAMES)

        assert schema["required"] == ["frames", "metrics", "checkpoints", "coach"]
        assert schema["additionalProperties"] is False
        text = json.dumps(schema)
        assert "swing_score" not in text
        for fault in SwingFaultId:
            assert fault.value not in text, fault

    def test_a_checkpoint_offers_its_own_fields_and_nothing_else(self) -> None:
        schema = reply_schema(
            (CueId.FACE_OPEN,), frame_ids=FRAMES, body_frame_ids=FRAMES
        )
        assert schema["properties"]["checkpoints"]["additionalProperties"] is False
        offered = checkpoint_entry(schema, "transition_dtl")
        assert fixed_length(offered) == vx.CHECKPOINT_ENTRY
        assert offered["items"]["anyOf"][0] == {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        }
        assert citable_cells(schema, "transition_dtl") == list(FRAMES)
        assert set(resolved(schema, {"$ref": "#/$defs/dir"})["enum"]) == {
            item.value for spec in SCOREABLE for item in spec.deviations
        }
        evidence = vx.render_evidence(
            club=ClubKind.DRIVER,
            handedness=Handedness.RIGHT,
            has_metrics=True,
            swing_images=FRAMES,
            cues=(CueId.FACE_OPEN,),
            session_focus=None,
            focus_shots=0,
        )
        shoulder = wire_line(evidence, "transition_dtl", "transition_trail_shoulder")
        words = "|".join(
            item.value
            for item in CHECKPOINTS_BY_ID[C.TRANSITION_TRAIL_SHOULDER].deviations
        )
        assert shoulder.endswith(f" dir {words}")
        assert wire_line(evidence, "finish_any", "finish_balance").endswith(" dir null")

    def test_with_no_body_picture_every_checkpoint_can_only_be_null(self) -> None:
        schema = reply_schema((CueId.FACE_OPEN,), frame_ids=(METRICS_FRAME_ID,))
        for key in vx.WIRE_LISTS:
            assert checkpoint_slot(schema, key) == {"type": "null"}
        assert schema["properties"]["frames"]["required"] == []

    def test_a_score_or_a_fault_smuggled_into_the_reply_is_never_read(self) -> None:
        items, rejected = parsed(
            {
                "swing_score": 99,
                "faults": ["KEEP_YOUR_HEAD_DOWN"],
                "setup_hip_hinge": a_checkpoint(C.SETUP_HIP_HINGE, 80),
            }
        )

        assert [item.checkpoint for item in items] == [C.SETUP_HIP_HINGE]
        assert ("swing_score", "unknown") in rejected
        assert ("faults", "unknown") in rejected

    def test_the_prompt_says_caddiej_computes_everything_else(self) -> None:
        system = " ".join(SYSTEM.split())

        assert "Return only what the schema asks." in system
        assert "CaddieJ computes every score, fault and lesson from yours." in system
        assert "CaddieJ decides the final lesson" not in system, "Python-only"


# --- 3: what the numbers mean -------------------------------------------------


class TestWhatTheNumbersMean:
    def test_tour_quality_is_reachable_where_the_cameras_can_show_it(self) -> None:
        mechanics = swing(97)

        assert mechanics.score_of(C.SETUP_HIP_HINGE) == 97
        setup = mechanics.phase_for(SwingPhase.SETUP)
        assert setup is not None and setup.value is not None and setup.value >= 90

    def test_and_is_clamped_where_they_cannot(self) -> None:
        mechanics = swing(97)
        hands = mechanics.checkpoint(C.TOP_HAND_DEPTH)

        assert hands is not None
        assert hands.score == NON_TOUR_CEILING
        assert hands.capped_from == 97
        assert C.TOP_HAND_DEPTH in mechanics.capped

    def test_tour_variation_is_not_a_fault(self) -> None:
        # Two different strong swings: one flatter and deeper, one more
        # upright. Neither is a fault, and the prompt says why.
        flatter = swing(
            85,
            top_hand_depth=(76, D.DEEP),
            backswing_shaft_pitch=(78, D.FLAT),
        )
        upright = swing(85, top_hand_height=(77, D.TOO_MUCH))

        assert infer_faults(flatter) == () and infer_faults(upright) == ()
        system = " ".join(SYSTEM.split())
        assert "Tour players differ" in system
        assert "never mark a checkpoint down only because it looks different" in system

    def test_a_phase_is_its_weighted_mean_pulled_towards_its_worst(self) -> None:
        mechanics = swing(85, transition_trail_shoulder=(30, D.OUT))
        transition = mechanics.phase_for(SwingPhase.TRANSITION)

        # 17 points of transition weight; the shoulder carries 3 of them.
        # mean = (14 x 85 + 3 x 30) / 17 = 75.3; 0.65 x 75.3 + 0.35 x 30 = 59.4
        assert transition is not None
        assert transition.value == 59
        assert transition.worst is C.TRANSITION_TRAIL_SHOULDER

    def test_a_missing_checkpoint_leaves_the_denominator(self) -> None:
        two = swing(
            None,
            transition_arms_lowering=80,
            transition_trail_shoulder=80,
        ).phase_for(SwingPhase.TRANSITION)
        three = swing(
            None,
            transition_arms_lowering=80,
            transition_trail_shoulder=80,
            transition_pelvis_initiation=80,
        ).phase_for(SwingPhase.TRANSITION)

        assert two is not None and two.value is None  # 6 of 17 is not a phase
        assert two.level is CoverageLevel.INSUFFICIENT
        assert three is not None and three.value == 80  # not dragged towards 0
        assert three.coverage == pytest.approx(9 / 17, abs=0.001)
        assert three.confidence < 0.8  # less of the phase seen, less sure

    def test_a_transition_fault_materially_lowers_the_score(self) -> None:
        sound = swing(85)
        over = swing(85, transition_trail_shoulder=(30, D.OUT))

        assert sound.value == 85
        assert over.value is not None and over.value <= 75
        assert over.ceiling_reasons == ("checkpoint:transition_trail_shoulder",)

    def test_a_good_setup_cannot_hide_a_bad_transition(self) -> None:
        mechanics = swing(
            85,
            **only({SwingPhase.SETUP, SwingPhase.FINISH}, 97),
            **only({SwingPhase.TRANSITION}, 40),
        )

        transition = mechanics.phase_for(SwingPhase.TRANSITION)
        assert transition is not None and transition.value == 40
        assert mechanics.value is not None
        assert mechanics.value <= transition.value + 25

    def test_a_finish_cannot_dominate(self) -> None:
        bad_finish = swing(85, **only({SwingPhase.FINISH}, 10))
        bad_transition = swing(85, **only({SwingPhase.TRANSITION}, 10))

        assert bad_finish.value is not None and bad_finish.value >= 80
        assert bad_transition.value is not None
        assert bad_finish.value - bad_transition.value >= 30

    def test_limited_coverage_caps_the_score(self) -> None:
        decisive = {SwingPhase.TRANSITION, SwingPhase.DOWNSWING, SwingPhase.IMPACT}
        mechanics = swing(None, **only(decisive, 95))

        assert mechanics.coverage == pytest.approx(0.48)
        assert mechanics.coverage_level is CoverageLevel.LIMITED
        assert mechanics.value == 85
        assert mechanics.ceiling_reasons == ("coverage",)

    def test_without_the_decisive_phases_there_is_no_score(self) -> None:
        mechanics = swing(
            None, **only({SwingPhase.SETUP, SwingPhase.TAKEAWAY, SwingPhase.FINISH}, 90)
        )

        assert mechanics.value is None
        assert mechanics.coverage_level is CoverageLevel.INSUFFICIENT

    def test_nothing_seen_is_no_score_rather_than_a_low_one(self) -> None:
        assert score_mechanics(()).value is None

    def test_the_same_scores_always_give_the_same_number(self) -> None:
        one = swing(80, transition_trail_shoulder=(45, D.OUT))
        two = swing(80, transition_trail_shoulder=(45, D.OUT))

        assert one == two


class TestProTeeDeliveryIsSeparate:
    def metrics(self):
        return parse_metrics(
            {
                **every_metric_null(),
                "ball_speed": a_reading(148.9, "mph"),
                "club_speed": a_reading(99.9, "mph"),
                "smash_factor": a_reading(1.49, "ratio"),
                "face_angle": a_reading(0.7, "deg", "open"),
                "swing_path": a_reading(1.7, "deg", "out_to_in"),
                "face_to_path": a_reading(2.4, "deg", "open"),
                "impact_width": a_reading(8.75, "mm", "toe"),
            }
        )

    def test_the_swing_score_is_the_mechanics_and_delivery_is_its_own_number(
        self,
    ) -> None:
        checkpoints = tuple(seen(spec.checkpoint_id, 85) for spec in SCOREABLE) + (
            seen(C.TRANSITION_TRAIL_SHOULDER, 30, deviation=D.OUT),
        )
        score = score_swing(
            self.metrics(),
            club=ClubKind.DRIVER,
            handedness=Handedness.RIGHT,
            checkpoints=checkpoints[:-1],
        )
        faulted = score_swing(
            self.metrics(),
            club=ClubKind.DRIVER,
            handedness=Handedness.RIGHT,
            checkpoints=tuple(
                item
                for item in checkpoints
                if item.checkpoint is not C.TRANSITION_TRAIL_SHOULDER
                or item.score == 30
            ),
        )

        assert score.rubric_version == SWING_SCORE_RUBRIC_V4
        assert score.mechanics is not None and score.value == score.mechanics.value
        assert score.delivery_value is not None and score.delivery_value >= 90
        # An excellent strike cannot carry a transition fault past it.
        assert faulted.delivery_value == score.delivery_value
        assert faulted.value is not None and faulted.value <= 75

    def test_no_pictures_is_no_swing_score_however_good_the_ball(self) -> None:
        score = score_swing(
            self.metrics(), club=ClubKind.DRIVER, handedness=Handedness.RIGHT
        )

        assert score.value is None
        assert score.delivery_value is not None


# --- 4: steep, shallow, steep -------------------------------------------------


class TestSteepShallowSteep:
    def test_each_needs_the_club_in_the_picture(self) -> None:
        assert len(CLUB_MOTION) == 5
        for checkpoint in CLUB_MOTION:
            spec = CHECKPOINTS_BY_ID[checkpoint]
            assert spec.club, checkpoint
            assert spec.scoreable, checkpoint
            assert spec.views == {PanelView.BEHIND}, checkpoint

    def test_an_unseen_club_claims_nothing_about_its_plane(self) -> None:
        mechanics = swing(85, **dict.fromkeys((c.value for c in CLUB_MOTION), None))

        assert set(club_motion(mechanics).values()) == {"null"}
        assert F.POOR_SHALLOWING not in faults_in(mechanics)

    def test_a_club_that_steepens_coming_down_is_poor_shallowing(self) -> None:
        mechanics = swing(
            85,
            transition_shallowing=(40, D.STEEP),
            delivery_shaft_plane=(45, D.STEEP),
        )

        assert club_motion(mechanics)[C.TRANSITION_SHALLOWING.value] == "40:steep"
        assert F.POOR_SHALLOWING in faults_in(mechanics)


# --- 5: the finite fault library ----------------------------------------------


class TestFaults:
    def test_chicken_wing(self) -> None:
        mechanics = swing(85, release_lead_arm_structure=(35, D.BENT))

        root = root_of(mechanics)

        assert faults_in(mechanics) == {F.CHICKEN_WING}
        assert root.fault is F.CHICKEN_WING
        assert root.cue is CueId.CHICKEN_WING

    def test_early_extension(self) -> None:
        mechanics = swing(
            85,
            delivery_hip_depth=(35, D.TOWARD_BALL),
            impact_hip_depth=(40, D.TOWARD_BALL),
        )

        root = root_of(mechanics)

        assert F.EARLY_EXTENSION in faults_in(mechanics)
        assert root.fault is F.EARLY_EXTENSION
        assert CUES_BY_ID[CueId.EARLY_EXTENSION].display == "KEEP HIPS BACK"

    def test_over_the_top_needs_two_places_to_agree(self) -> None:
        one = swing(85, delivery_hand_path=(40, D.OUTSIDE))
        two = swing(
            85,
            delivery_hand_path=(40, D.OUTSIDE),
            delivery_shaft_plane=(40, D.STEEP),
        )

        assert F.OVER_THE_TOP not in faults_in(one)
        assert F.OVER_THE_TOP in faults_in(two)

    def test_a_functional_score_is_not_a_fault(self) -> None:
        assert infer_faults(swing(85, transition_trail_shoulder=(65, D.OUT))) == ()

    def test_a_half_seen_picture_does_not_make_a_fault(self) -> None:
        unsure = seen(C.TRANSITION_TRAIL_SHOULDER, 30, confidence=0.5, deviation=D.OUT)

        assert infer_faults(swing(85, transition_trail_shoulder=unsure)) == ()

    def test_a_low_score_with_one_possible_direction_reads_as_that_one(self) -> None:
        shoulder = swing(85, transition_trail_shoulder=35)
        hands = swing(85, top_hand_depth=35)

        assert F.TRAIL_SHOULDER_OUT in faults_in(shoulder)
        # Too shallow and too deep are opposite lessons: no direction, no fault.
        assert not faults_in(hands) & {
            F.INSUFFICIENT_HAND_DEPTH,
            F.EXCESSIVE_HAND_DEPTH,
        }

    def test_a_sound_swing_has_no_fault(self) -> None:
        mechanics = swing(85)

        assert infer_faults(mechanics) == ()
        assert root_of(mechanics).fault is None


# --- 6: root cause ------------------------------------------------------------


class TestTheRootCause:
    def test_the_trail_shoulder_is_coached_rather_than_the_path_it_made(self) -> None:
        mechanics = swing(
            85,
            transition_trail_shoulder=(35, D.OUT),
            transition_hand_direction=(40, D.OUT),
            delivery_hand_path=(40, D.OUTSIDE),
        )

        root = root_of(mechanics)

        assert {F.TRAIL_SHOULDER_OUT, F.STEEP_TRANSITION, F.OVER_THE_TOP} <= (
            faults_in(mechanics)
        )
        assert root.fault is F.TRAIL_SHOULDER_OUT
        assert set(root.explains) >= {F.STEEP_TRANSITION, F.OVER_THE_TOP}
        assert CUES_BY_ID[root.cue].display == "DIP TRAIL SHOULDER"  # type: ignore[index]

    def test_a_credible_earlier_cause_wins(self) -> None:
        mechanics = swing(
            85,
            backswing_body_turn=(35, D.TOO_LITTLE),
            top_shoulder_turn=(30, D.TOO_LITTLE),
            transition_trail_shoulder=(45, D.OUT),
        )

        root = root_of(mechanics)

        assert root.fault is F.INSUFFICIENT_TURN
        assert F.TRAIL_SHOULDER_OUT in root.explains

    def test_a_tiny_early_imperfection_does_not_beat_a_severe_later_fault(
        self,
    ) -> None:
        mechanics = swing(
            85,
            top_shoulder_turn=seen(
                C.TOP_SHOULDER_TURN, 58, confidence=0.6, deviation=D.TOO_LITTLE
            ),
            transition_trail_shoulder=seen(
                C.TRANSITION_TRAIL_SHOULDER, 20, confidence=0.85, deviation=D.OUT
            ),
        )

        root = root_of(mechanics)

        assert F.INSUFFICIENT_TURN in faults_in(mechanics)
        assert root.fault is F.TRAIL_SHOULDER_OUT
        assert F.INSUFFICIENT_TURN not in root.explains

    def test_the_answer_does_not_depend_on_the_order(self) -> None:
        mechanics = swing(
            85,
            transition_trail_shoulder=(35, D.OUT),
            delivery_hip_depth=(40, D.TOWARD_BALL),
            release_lead_arm_structure=(45, D.BENT),
        )
        faults = infer_faults(mechanics)

        forwards = rank_root_cause(faults)
        backwards = rank_root_cause(tuple(reversed(faults)))

        assert forwards.fault is backwards.fault

    def test_the_session_lesson_is_kept_against_a_near_equal(self) -> None:
        mechanics = swing(
            85,
            transition_trail_shoulder=(40, D.OUT),
            delivery_hip_depth=(38, D.TOWARD_BALL),
        )
        best = root_of(mechanics).fault
        other = F.TRAIL_SHOULDER_OUT if best is F.EARLY_EXTENSION else F.EARLY_EXTENSION

        kept = root_of(mechanics, session_fault=other)

        assert kept.fault is other and kept.reason == "sticky"


# --- 7: ProTee ----------------------------------------------------------------


class TestProTee:
    def test_a_healthy_iron_draw_is_no_sign_of_anything(self) -> None:
        assert protee_signs(club=ClubKind.IRON, path=3.7, face=2.0, attack=-4.0) == ()

    def test_a_path_outside_its_window_is_a_sign(self) -> None:
        assert protee_signs(
            club=ClubKind.DRIVER, path=-4.0, face=None, attack=None
        ) == (ProteeSign.PATH_OUT_TO_IN,)

    def test_a_club_without_a_window_has_no_path_sign(self) -> None:
        assert protee_signs(club=None, path=-9.0, face=None, attack=None) == ()

    def test_protee_cannot_invent_a_fault(self) -> None:
        signs = (ProteeSign.PATH_OUT_TO_IN, ProteeSign.ATTACK_STEEP)

        assert corroborate((), signs) == ()
        assert root_of(swing(85), signs=signs).fault is None

    def test_it_only_raises_confidence_in_what_was_seen(self) -> None:
        mechanics = swing(
            85,
            transition_trail_shoulder=seen(
                C.TRANSITION_TRAIL_SHOULDER, 40, confidence=0.6, deviation=D.OUT
            ),
        )
        (plain,) = infer_faults(mechanics)
        (raised,) = corroborate((plain,), (ProteeSign.PATH_OUT_TO_IN,))

        assert raised.confidence == pytest.approx(plain.confidence + 0.05)
        assert raised.severity == plain.severity
        assert raised.seen_confidence == plain.confidence
        assert raised.corroborated_by == (ProteeSign.PATH_OUT_TO_IN,)

    def test_a_pattern_the_fault_does_not_produce_corroborates_nothing(self) -> None:
        (plain,) = infer_faults(swing(85, transition_trail_shoulder=(40, D.OUT)))

        (same,) = corroborate((plain,), (ProteeSign.PATH_IN_TO_OUT,))

        assert same == plain


# --- 8: the session loop ------------------------------------------------------


class TestTheSessionLesson:
    def test_a_big_move_into_the_good_range_is_much_better_and_reinforced(
        self,
    ) -> None:
        tracker = LessonTracker()
        first = coach(tracker, swing(85, **a_chicken_wing(43)))
        second = coach(tracker, swing(85, **a_chicken_wing(81, bent=False)))

        assert first.progress is not None and first.progress.mode is LessonMode.NEW
        assert first.cue is CueId.CHICKEN_WING
        progress = second.progress
        assert progress is not None
        assert progress.verdict is LessonVerdict.MUCH_BETTER
        assert progress.mode is LessonMode.REINFORCE
        assert (progress.score, progress.delta) == (81, 38)
        assert progress.headline == "MUCH BETTER RELEASE"
        assert second.reinforce and second.cue is CueId.CHICKEN_WING

    def test_38_to_79_is_meaningful(self) -> None:
        tracker = LessonTracker()
        coach(tracker, swing(85, **a_chicken_wing(38)))
        step = coach(tracker, swing(85, **a_chicken_wing(79, bent=False)))

        assert step.progress is not None
        assert step.progress.verdict is LessonVerdict.MUCH_BETTER
        assert step.progress.delta == 41

    def test_61_to_64_is_the_same_swing(self) -> None:
        tracker = LessonTracker()
        coach(tracker, swing(85, **a_chicken_wing(58)))
        coach(tracker, swing(85, **a_chicken_wing(61, bent=False)))
        step = coach(tracker, swing(85, **a_chicken_wing(64, bent=False)))

        assert step.progress is not None
        assert step.progress.verdict is LessonVerdict.SAME
        assert step.progress.mode is LessonMode.CONTINUE
        assert step.progress.headline == ""
        assert not step.reinforce

    def test_worse_is_worse(self) -> None:
        tracker = LessonTracker()
        coach(tracker, swing(85, **a_chicken_wing(55)))
        step = coach(tracker, swing(85, **a_chicken_wing(40)))

        assert step.progress is not None
        assert step.progress.verdict is LessonVerdict.WORSE

    def test_an_unseen_checkpoint_is_never_an_improvement(self) -> None:
        tracker = LessonTracker()
        coach(tracker, swing(85, **a_chicken_wing(38)))
        unseen = coach(
            tracker,
            swing(
                85,
                release_lead_arm_structure=None,
                impact_arm_structure=None,
            ),
        )
        later = coach(tracker, swing(85, **a_chicken_wing(44)))

        assert unseen.progress is not None
        assert unseen.progress.verdict is LessonVerdict.UNASSESSED
        assert unseen.progress.improved_shots == 0
        assert unseen.progress.headline == ""
        assert unseen.cue is None, "an unseen lesson is not said as if seen"
        assert tracker.fault is F.CHICKEN_WING
        # The next seen shot is compared with the last time it was seen.
        assert later.progress is not None and later.progress.previous == 38

    def test_solved_twice_retires_the_lesson(self) -> None:
        tracker = LessonTracker()
        coach(tracker, swing(85, **a_chicken_wing(40)))
        coach(tracker, swing(85, **a_chicken_wing(81, bent=False)))
        step = coach(tracker, swing(85, **a_chicken_wing(82, bent=False)))

        assert step.progress is not None
        assert step.progress.verdict is LessonVerdict.HELD
        assert step.progress.mode is LessonMode.RETIRED
        assert tracker.fault is None

    def test_a_fixed_lesson_advances_to_the_next_root(self) -> None:
        tracker = LessonTracker()
        coach(tracker, swing(85, **a_chicken_wing(40)))
        step = coach(
            tracker,
            swing(
                85,
                **a_chicken_wing(81, bent=False),
                delivery_hip_depth=(40, D.TOWARD_BALL),
                impact_hip_depth=(42, D.TOWARD_BALL),
            ),
        )

        assert step.progress is not None
        assert step.progress.fault is F.CHICKEN_WING
        assert step.progress.mode is LessonMode.ADVANCE
        assert step.progress.headline == "MUCH BETTER RELEASE"
        assert step.progress.next_fault is F.EARLY_EXTENSION
        assert step.fault is F.EARLY_EXTENSION
        assert step.cue is CueId.EARLY_EXTENSION
        assert tracker.fault is F.EARLY_EXTENSION

    def test_forgetting_the_session_forgets_the_lesson(self) -> None:
        tracker = LessonTracker()
        coach(tracker, swing(85, **a_chicken_wing(40)))

        tracker.forget()

        assert tracker.fault is None and tracker.previous_score is None


# --- 9: what the player is told -----------------------------------------------


class TestThePlayerIsToldOneReviewedThing:
    @pytest.mark.parametrize("fault", list(SWING_FAULTS), ids=lambda f: f.fault_id)
    def test_every_fault_has_a_whole_reviewed_lesson(self, fault) -> None:
        spec = CUES_BY_ID[fault.cue]

        assert spec.display == spec.display.upper()
        assert len(spec.display) <= MAX_DISPLAY_CHARACTERS
        assert spec.observed and spec.display != spec.observed
        assert cue_speech(fault.cue), fault.cue

    def test_lessons_say_lead_and_trail_never_left_and_right(self) -> None:
        sides = re.compile(r"\b(left|right)\b", re.IGNORECASE)
        for fault in SWING_FAULTS:
            spec = CUES_BY_ID[fault.cue]
            for text in (spec.display, spec.observed, CUE_SPEECH[fault.cue]):
                assert not sides.search(text), (fault.fault_id, text)

    def test_a_progress_headline_names_the_phase_the_way_the_floor_does(
        self,
    ) -> None:
        assert phase_label(SwingPhase.DOWNSWING) == "DELIVERY"
        assert phase_label(SwingPhase.RELEASE) == "RELEASE"


# --- 10: the floor's data -----------------------------------------------------


class TestTheFloorContract:
    def test_phases_and_progress_cross_to_the_overlay_as_data(self) -> None:
        advice = FloorAdvice(
            text="KEEP ARMS TOGETHER",
            swing_score=81,
            phases=(FloorPhase("TRANSITION", 78), FloorPhase("RELEASE", 81)),
            lesson=FloorLesson(
                label="RELEASE",
                value=81,
                delta=38,
                headline="MUCH BETTER RELEASE",
                verdict="much_better",
                mode="reinforce",
            ),
        )

        payload = AdvicePayload.from_advice(advice).model_dump()

        assert payload["phases"] == [
            {"label": "TRANSITION", "value": 78},
            {"label": "RELEASE", "value": 81},
        ]
        assert payload["lesson"]["headline"] == "MUCH BETTER RELEASE"
        assert payload["lesson"]["delta"] == 38

    def test_a_coaching_result_with_only_phases_is_not_empty(self) -> None:
        assert CoachingResult().is_empty
        assert not CoachingResult(phases=(PhaseLine("IMPACT", 70),)).is_empty
        assert not CoachingResult(progress=LessonLine("RELEASE")).is_empty

    def test_a_phase_out_of_range_is_refused(self) -> None:
        with pytest.raises(ValueError):
            FloorPhase("IMPACT", 101)


# --- 11: persistence ----------------------------------------------------------


class TestPersistence:
    def test_a_v4_score_and_its_review_survive_a_round_trip(self) -> None:
        mechanics = swing(85, transition_trail_shoulder=(35, D.OUT))
        score = score_swing(
            parse_metrics(an_open_face_grid()),
            club=ClubKind.DRIVER,
            handedness=Handedness.RIGHT,
            checkpoints=tuple(
                seen(
                    row.checkpoint,
                    row.score,
                    deviation=row.deviation,
                )
                for row in mechanics.checkpoints
                if row.score is not None
            ),
        )
        faults = infer_faults(score.mechanics)
        root = rank_root_cause(faults)
        tracker = LessonTracker()
        step = tracker.step(score.mechanics, faults, root)
        review = SwingReview(
            faults=faults,
            root=root,
            progress=step.progress,
            source=LessonSource.MECHANICAL,
        )

        columns = swing_score_columns(
            score, review=review, final_cue="TRAIL_SHOULDER_OUT"
        )
        text = columns["swing_score_json"]
        assert isinstance(text, str)
        back = swing_score_of(text)
        stored = review_of(text)

        assert back.value == score.value
        assert back.mechanics is not None and score.mechanics is not None
        assert back.mechanics.score_of(C.TRANSITION_TRAIL_SHOULDER) == 35
        assert back.mechanics.phases == score.mechanics.phases
        assert stored == review_document(review, "TRAIL_SHOULDER_OUT")
        assert stored is not None
        assert stored["root"]["fault"] == "TRAIL_SHOULDER_OUT"


# --- 12: through the service, one request per shot ----------------------------


def the_94_shot() -> dict[str, Any]:
    """Five readings inside their bands; a trail shoulder that moved out."""

    return {
        **every_metric_null(),
        "ball_speed": a_reading(148.9, "mph"),
        "club_speed": a_reading(99.9, "mph"),
        "smash_factor": a_reading(1.49, "ratio"),
        "carry": a_reading(152.1, "yd"),
        "face_angle": a_reading(0.7, "deg", "open"),
        "swing_path": a_reading(1.7, "deg", "out_to_in"),
        "face_to_path": a_reading(2.4, "deg", "open"),
        "impact_width": a_reading(8.75, "mm", "toe"),
    }


def shoulder_out(score: int = 35) -> dict[str, Any]:
    return a_swing(
        transition_trail_shoulder=a_checkpoint(
            C.TRANSITION_TRAIL_SHOULDER, score, deviation="out"
        ),
        transition_hand_direction=a_checkpoint(
            C.TRANSITION_HAND_DIRECTION, 40, deviation="out"
        ),
    )


def chicken_wing_reply(score: int, *, bent: bool = True) -> dict[str, Any]:
    deviation = "bent" if bent else None
    return a_reply(
        the_94_shot(),
        kind="reinforcement",
        cue="STRIKE_GOOD",
        speech="Keep that smooth tempo on the next one.",
        checkpoints=a_swing(
            release_lead_arm_structure=a_checkpoint(
                C.RELEASE_LEAD_ARM_STRUCTURE, score, deviation=deviation
            ),
            impact_arm_structure=a_checkpoint(
                C.IMPACT_ARM_STRUCTURE, score, deviation=deviation
            ),
        ),
    )


async def coach_shots(tmp_path: Path, replies: tuple[dict[str, Any], ...]) -> Coach:
    coach = Coach(ScriptedProvider(replies=replies))
    await coach.service.open()
    await coach.service.on_club_selected(ClubSelected(club=DRIVER))
    for index in range(len(replies)):
        await coach.strike(
            tmp_path / str(index), trigger_id=str(30 + index), supports=True
        )
        async with asyncio.timeout(2.0):
            while len(coach.results) <= index:
                await asyncio.sleep(0.01)
    await coach.service.aclose()
    return coach


class TestThroughTheService:
    async def test_one_request_scores_the_swing_and_coaches_the_cause(
        self, tmp_path
    ) -> None:
        provider = ScriptedProvider(
            replies=(a_reply(the_94_shot(), checkpoints=shoulder_out()),)
        )
        coach = Coach(provider)

        await coach.run(tmp_path, supports=True)

        assert provider.calls == 1
        advice = coach.bus.of(AnalysisCompleted)[0].advice
        assert advice.cue is CueId.TRAIL_SHOULDER_OUT
        assert advice.display == "DIP TRAIL SHOULDER"
        result = coach.results[0]
        assert result.review is not None
        assert result.review.root.fault is F.TRAIL_SHOULDER_OUT
        assert result.review.source is LessonSource.MECHANICAL
        assert result.swing_score is not None
        assert result.swing_score.value is not None and result.swing_score.value <= 75
        assert result.swing_score.delivery_value is not None

    async def test_the_models_own_sentence_does_not_survive_the_lesson(
        self, tmp_path
    ) -> None:
        provider = ScriptedProvider(
            replies=(
                a_reply(
                    the_94_shot(),
                    cue="FACE_OPEN",
                    speech="Square the face a little earlier.",
                    checkpoints=shoulder_out(),
                ),
            )
        )
        coach = Coach(provider)

        await coach.run(tmp_path, supports=True)

        advice = coach.bus.of(AnalysisCompleted)[0].advice
        assert advice.cue is CueId.TRAIL_SHOULDER_OUT
        assert "face" not in advice.speech.lower()
        assert advice.speech == CUE_SPEECH[CueId.TRAIL_SHOULDER_OUT]

    async def test_a_score_in_the_reply_is_not_the_swing_score(self, tmp_path) -> None:
        reply = a_reply(the_94_shot()) | {"swing_score": 99, "faults": ["MADE_UP"]}
        coach = Coach(ScriptedProvider(replies=(reply,)))

        await coach.run(tmp_path, supports=True)

        score = coach.results[0].swing_score
        assert score is not None and score.value == 85

    async def test_with_no_mechanical_cause_protee_decides_the_lesson(
        self, tmp_path
    ) -> None:
        coach = Coach(ScriptedProvider(replies=(a_reply(),)))

        await coach.run(tmp_path, supports=True)

        advice = coach.bus.of(AnalysisCompleted)[0].advice
        assert advice.cue is CueId.FACE_OPEN
        review = coach.results[0].review
        assert review is not None and review.root.fault is None
        assert review.source is LessonSource.MEASURED

    async def test_no_pictures_is_no_swing_score_and_no_mechanical_lesson(
        self, tmp_path
    ) -> None:
        coach = Coach(ScriptedProvider(replies=(a_reply(checkpoints=no_swing()),)))

        await coach.run(tmp_path, supports=True)

        result = coach.results[0]
        assert result.swing_score is not None and result.swing_score.value is None
        assert result.cue_id == CueId.FACE_OPEN.value

    async def test_the_next_shot_says_how_the_lesson_moved(self, tmp_path) -> None:
        coach = await coach_shots(
            tmp_path, (chicken_wing_reply(43), chicken_wing_reply(81, bent=False))
        )

        first, second = (event.advice for event in coach.bus.of(AnalysisCompleted))
        assert first.cue is CueId.CHICKEN_WING
        assert first.kind is AdviceKind.CORRECTION
        assert second.cue is CueId.CHICKEN_WING
        assert second.kind is AdviceKind.REINFORCEMENT
        assert second.observation == "MUCH BETTER RELEASE"
        assert second.review is not None
        progress = second.review.progress
        assert progress is not None
        assert (progress.score, progress.delta) == (81, 38)
        assert progress.mode is LessonMode.REINFORCE
