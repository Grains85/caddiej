"""Coach V3: coach the cause, show it where it can be read, leave it up.

The shot that prompted all of this scored 94 and was told GOOD STRIKE. Every
reading was inside its band and the swing behind the ball was coming over the
top, which is a thing a coach would have stopped the player on and a launch
monitor has no way to see. Five changes follow from that, and each has its own
section below.

**A seen fault outranks a good reading.** Not "is considered alongside": a
measurement at the ball and a movement in the backswing are facts about
different things, and the one that can be changed wins.

**One focus, one instruction, still.** Nothing here widens what may be said.
The cue vocabulary is closed, the sentence is reviewed, and exactly one of
each reaches the player.

**A result stays up until the next ball is struck.** A ball going down on the
mat is not a swing.

**The coaching has somewhere to be.** Four measured regions above the hitting
strip, and nothing of CaddieJ's drawn on it.

**The player never sees the All Values grid.** It still goes to the model.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.schema_support import checkpoint_slot, citable_cells, listed_cells
from tests.unit.sheet_support import sheet_cells
from tests.unit.test_coach_protee import (
    NOW,
    Coach,
    a_checkpoint,
    a_reading,
    a_swing,
    every_metric_null,
)

from caddiej.adapters.hud.protocol import AdvicePayload, AnchorsPayload
from caddiej.coaching import vx
from caddiej.coaching.baseline import Baseline
from caddiej.coaching.cues import CUE_SPEECH, CUES_BY_ID
from caddiej.coaching.diagnose import diagnose
from caddiej.coaching.fakes import ScriptedProvider
from caddiej.coaching.metrics_strip import STRIP_METRICS, strip_metrics
from caddiej.coaching.prompt import MAX_DISPLAY_CHARACTERS
from caddiej.coaching.validate import AdviceRejected, validate
from caddiej.coaching.vx import (
    METRICS_FRAME_ID,
    parse_metrics,
    parse_visual,
    shot_from_metrics,
)
from caddiej.domain.coaching import AdviceKind, CoachMetric
from caddiej.domain.cues import CueId
from caddiej.domain.events import AnalysisCompleted
from caddiej.domain.shot import Handedness
from caddiej.domain.swing_checkpoints import CheckpointId
from caddiej.floor.chamfer import chamfer_depth
from caddiej.floor.coach_layout import MINIMUM_BAND, FloorRect, coach_regions
from caddiej.floor.geometry import (
    DEFAULT_CALIBRATION,
    PhysicalFloor,
    PhysicalRect,
    RectangularCalibration,
    project_rect,
)
from caddiej.floor.layout import FloorLayout
from caddiej.floor.state import FloorAdvice, FloorAnchors, FloorMetric, FloorPoint

# --- the shot that started it ----------------------------------------------

# Capture ea475a87-f9c3-4a1c-857d-a42471ecfc04, trigger 55. Face 0.7° open,
# path 1.7° out-to-in, face-to-path 2.4° open, strike 8.75 mm toe, smash 1.49:
# five readings, every one of them inside its band.
THE_94_SHOT: dict[str, Any] = {
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


def a_diagnosis_of(grid: dict[str, Any]):
    shot = shot_from_metrics(
        parse_metrics(grid),
        shot_id=uuid4(),
        club=None,
        handedness=Handedness.RIGHT,
        observed_at=NOW,
        observed_monotonic=0.0,
    )
    assert shot is not None
    return diagnose(shot, Baseline().for_club(None))


# --- B: coach the cause, not the result -------------------------------------
# Which cause, and how it outranks a good reading, is Swing Score V4's job and
# is tested in test_coach_v4.py. What stays here is the premise.


class TestTheShotThatStartedIt:
    def test_the_94_shot_measured_nothing_worth_correcting(self) -> None:
        # Every band is clear, so the deterministic verdict is a
        # reinforcement and the measured line has nothing to correct.
        diagnosis = a_diagnosis_of(THE_94_SHOT)

        assert diagnosis.kind is not AdviceKind.CORRECTION

    async def test_the_seen_cause_reaches_the_floor_as_the_coaching(
        self, tmp_path
    ) -> None:
        provider = ScriptedProvider(replies=(a_v3_reply(),))
        coach = Coach(provider)

        await coach.run(tmp_path, supports=True)

        assert provider.calls == 1
        advice = coach.bus.of(AnalysisCompleted)[0].advice
        assert advice.cue is CueId.TRAIL_SHOULDER_OUT
        assert advice.observation == "UPPER BODY OUT"
        assert advice.display == "DIP TRAIL SHOULDER"


class TestTheInstructionIsStillOneSentence:
    def reviewed(self, instruction: str):
        diagnosis = a_diagnosis_of(THE_94_SHOT)
        diagnosis = replace(
            diagnosis,
            cue=CueId.TRAIL_SHOULDER_OUT,
            admissible_cues=(CueId.TRAIL_SHOULDER_OUT,),
        )
        return validate(
            {
                "kind": "correction",
                "cue": CueId.TRAIL_SHOULDER_OUT.value,
                "speech": instruction,
                "observation": None,
                "reinforcement": None,
                "visual_confidence": 0.45,
            },
            diagnosis,
            NOW,
            frames_sent=True,
        )

    def test_the_reviewed_line_passes_the_speech_contract(self) -> None:
        cue = CUES_BY_ID[CueId.TRAIL_SHOULDER_OUT]

        advice = self.reviewed(CUE_SPEECH[CueId.TRAIL_SHOULDER_OUT])

        assert advice.cue is CueId.TRAIL_SHOULDER_OUT
        assert advice.display == cue.display == cue.display.upper()
        assert len(cue.display) <= MAX_DISPLAY_CHARACTERS
        assert advice.observation == cue.observed

    def test_a_line_that_only_restates_is_refused(self) -> None:
        with pytest.raises(AdviceRejected):
            self.reviewed("Your trail shoulder moved out towards the ball.")


def a_v3_reply(**overrides: Any) -> dict[str, Any]:
    """A reply that reads the grid and scores the swing's checkpoints.

    The trail shoulder moved out, which is the fault the 94 shot had: a path
    of 1.7 degrees out-to-in is inside every band CaddieJ has, and the move
    that produced it is still the thing to change."""

    reply: dict[str, Any] = {
        "metrics": THE_94_SHOT,
        "checkpoints": a_swing(
            transition_trail_shoulder=a_checkpoint(
                CheckpointId.TRANSITION_TRAIL_SHOULDER, 35, deviation="out"
            ),
        ),
        "coach": {
            "kind": "reinforcement",
            "cue": CueId.STRIKE_GOOD.value,
            "instruction": None,
            "observation": None,
            "reinforcement": "Your strike was centred and the numbers were tidy.",
            "visual_confidence": None,
        },
    }
    reply.update(overrides)
    return reply


# --- A: a rating has to have been seen somewhere ----------------------------


class TestARatingMustCiteAFrameThatWasSent:
    def test_a_rating_on_a_frame_nobody_sent_is_refused(self) -> None:
        seen, rejected = parse_visual(
            {
                "swing_plane": {
                    "rating": "fault",
                    "confidence": 0.5,
                    "frame_id": "swing_behind_09",
                }
            },
            body_frames=("swing_behind_01",),
        )

        assert seen == ()
        assert "swing_plane" in rejected

    def test_a_rating_on_the_metrics_grid_is_refused(self) -> None:
        # The grid is a picture of numbers. Nobody's plane is visible in it.
        seen, rejected = parse_visual(
            {
                "posture": {
                    "rating": "fault",
                    "confidence": 0.5,
                    "frame_id": METRICS_FRAME_ID,
                }
            },
            body_frames=("swing_behind_01", METRICS_FRAME_ID),
        )

        assert seen == ()
        assert "posture" in rejected

    def test_a_null_rating_is_absent_rather_than_rejected(self) -> None:
        seen, rejected = parse_visual(
            {"swing_plane": None, "transition": None},
            body_frames=("swing_behind_01",),
        )

        assert seen == ()
        assert rejected == ()

    async def test_the_metrics_grid_is_not_among_the_frames_a_rating_may_cite(
        self, tmp_path
    ) -> None:
        provider = ScriptedProvider(replies=(a_v3_reply(),))
        coach = Coach(provider)

        await coach.run(tmp_path, supports=True)

        request = provider.requests[0]
        schema = request.schema
        offered = listed_cells(request.evidence)
        assert offered
        assert "ALL_VALUES" not in offered
        assert METRICS_FRAME_ID not in offered
        assert schema["properties"]["frames"]["required"] == offered
        citing = [
            key
            for key in vx.WIRE_LISTS
            if checkpoint_slot(schema, key) != {"type": "null"}
        ]
        assert citing
        for key in citing:
            cited = citable_cells(schema, key)
            assert cited and set(cited) <= set(offered), key
            assert not {"ALL_VALUES", METRICS_FRAME_ID} & set(cited), key


# --- E: the four readings ---------------------------------------------------


class TestTheMetricsStrip:
    def test_the_four_are_carry_face_path_and_club_speed(self) -> None:
        assert [label for label, _ in STRIP_METRICS] == [
            "CARRY",
            "FACE",
            "PATH",
            "CLUB",
        ]

    def test_each_keeps_protees_unit_and_protees_word(self) -> None:
        shown = strip_metrics(parse_metrics(THE_94_SHOT))

        assert shown == (
            CoachMetric(label="CARRY", value="152.1 yd"),
            CoachMetric(label="FACE", value="0.7\N{DEGREE SIGN} OPEN"),
            CoachMetric(label="PATH", value="1.7\N{DEGREE SIGN} OUT-TO-IN"),
            CoachMetric(label="CLUB", value="99.9 mph"),
        )

    def test_a_missing_reading_is_a_missing_cell_and_not_a_dash(self) -> None:
        grid = dict(THE_94_SHOT, carry=None)

        shown = strip_metrics(parse_metrics(grid))

        assert [item.label for item in shown] == ["FACE", "PATH", "CLUB"]
        assert all(item.value for item in shown)

    def test_nothing_readable_is_an_empty_strip(self) -> None:
        assert strip_metrics(parse_metrics(every_metric_null())) == ()
        assert strip_metrics(None) == ()

    async def test_the_readings_reach_the_coaching_result(self, tmp_path) -> None:
        coach = Coach(ScriptedProvider(replies=(a_v3_reply(),)))

        await coach.run(tmp_path, supports=True)

        advice = coach.bus.of(AnalysisCompleted)[0].advice
        assert [item.label for item in advice.metrics] == [
            "CARRY",
            "FACE",
            "PATH",
            "CLUB",
        ]

    def test_and_cross_the_wire_as_strings(self) -> None:
        payload = AdvicePayload.from_advice(
            FloorAdvice(
                text="DIP TRAIL SHOULDER",
                metrics=(FloorMetric(label="CARRY", value="152.1 yd"),),
            )
        )

        assert payload.metrics[0].label == "CARRY"
        assert payload.metrics[0].value == "152.1 yd"


# --- F: the grid is evidence, not a picture ---------------------------------


class TestTheAllValuesGridIsNeverShownToThePlayer:
    async def test_it_still_goes_to_the_model(self, tmp_path) -> None:
        provider = ScriptedProvider(replies=(a_v3_reply(),))
        coach = Coach(provider)

        await coach.run(tmp_path, supports=True)

        (sheet,) = provider.requests[0].images
        assert "ALL_VALUES" in sheet_cells(sheet.data)

    async def test_and_never_comes_back_as_the_floors_picture(self, tmp_path) -> None:
        reply = a_v3_reply()
        reply["coach"]["evidence_frame_id"] = METRICS_FRAME_ID
        coach = Coach(ScriptedProvider(replies=(reply,)))

        await coach.run(tmp_path, supports=True)

        evidence = coach.bus.of(AnalysisCompleted)[0].evidence
        assert evidence is None or evidence.frame_id != METRICS_FRAME_ID

    async def test_what_is_shown_instead_is_a_frame_of_the_player(
        self, tmp_path
    ) -> None:
        reply = a_v3_reply()
        reply["coach"]["evidence_frame_id"] = METRICS_FRAME_ID
        coach = Coach(ScriptedProvider(replies=(reply,)))

        await coach.run(tmp_path, supports=True)

        evidence = coach.bus.of(AnalysisCompleted)[0].evidence
        assert evidence is not None
        assert evidence.frame_id.startswith(("swing_", "impact_"))

    async def test_a_cited_swing_frame_is_still_shown_as_itself(self, tmp_path) -> None:
        # The lesson is the trail shoulder, so the frame shown is the one its
        # checkpoint was scored from.
        reply = a_v3_reply()
        reply["checkpoints"]["transition_trail_shoulder"]["frame_id"] = (
            "swing_behind_02"
        )
        coach = Coach(ScriptedProvider(replies=(reply,)))

        await coach.run(tmp_path, supports=True)

        evidence = coach.bus.of(AnalysisCompleted)[0].evidence
        assert evidence is not None
        assert evidence.frame_id == "swing_behind_02"

    async def test_with_no_impact_frame_there_is_simply_no_picture(
        self, tmp_path
    ) -> None:
        reply = a_v3_reply()
        reply["coach"]["evidence_frame_id"] = METRICS_FRAME_ID
        coach = Coach(ScriptedProvider(replies=(reply,)))

        await coach.run(tmp_path, impact=0, supports=True)

        evidence = coach.bus.of(AnalysisCompleted)[0].evidence
        assert evidence is None or evidence.frame_id != METRICS_FRAME_ID


# --- D: the coaching has somewhere to be ------------------------------------


def a_real_bay() -> PhysicalRect:
    """The strip as it is actually measured in the bay this came from."""

    return PhysicalRect(x_m=0.52, y_m=0.65, width_m=0.55, depth_m=0.315)


def regions_for(strip: PhysicalRect, calibration=DEFAULT_CALIBRATION):
    return coach_regions(
        project_rect(strip, calibration),
        chamfer_depth=chamfer_depth(strip, calibration, scale=4.0),
    )


class TestTheCoachingNeverLandsOnTheHittingStrip:
    def test_the_real_bay_gets_four_regions_and_a_protected_band(self) -> None:
        regions = regions_for(a_real_bay())

        assert regions is not None
        assert len(regions.drawn) == 4
        assert regions.clears_protected()

    def test_the_score_is_the_furthest_from_the_player(self) -> None:
        # Two columns rather than one stack since the first hardware test, so
        # the still is no longer *below* the score -- it is beside both lines
        # of words, and the readings are below all three.
        regions = regions_for(a_real_bay())

        assert regions is not None
        assert regions.score.bottom <= regions.observation.y
        assert regions.observation.bottom <= regions.metrics.y
        assert regions.score.right <= regions.evidence.x
        assert regions.observation.right <= regions.evidence.x
        assert regions.evidence.bottom <= regions.metrics.y
        assert regions.metrics.bottom <= regions.protected.y

    def test_the_score_sits_above_the_analysis_text(self) -> None:
        regions = regions_for(a_real_bay())

        assert regions is not None
        assert regions.score.bottom <= regions.observation.y

    def test_the_protected_band_reaches_behind_the_mat_for_ball_status(
        self,
    ) -> None:
        strip = a_real_bay()
        regions = regions_for(strip)
        rear = project_rect(strip, DEFAULT_CALIBRATION)

        assert regions is not None
        assert regions.protected.y < min(point.y for point in rear)

    @pytest.mark.parametrize(
        "strip",
        [
            PhysicalRect(x_m=0.52, y_m=0.65, width_m=0.55, depth_m=0.315),
            PhysicalRect(x_m=0.5, y_m=0.5, width_m=1.0, depth_m=0.4),
            PhysicalRect(x_m=0.3, y_m=0.8, width_m=0.4, depth_m=0.2),
            PhysicalRect(x_m=0.8, y_m=0.72, width_m=0.6, depth_m=0.25),
        ],
    )
    def test_no_bay_puts_coaching_on_its_own_mat(self, strip: PhysicalRect) -> None:
        regions = regions_for(strip)

        assert regions is not None
        assert regions.clears_protected()

    def test_a_mat_with_no_room_above_it_gets_no_regions_rather_than_slivers(
        self,
    ) -> None:
        crowded = PhysicalRect(x_m=0.5, y_m=0.12, width_m=0.5, depth_m=0.2)

        assert regions_for(crowded) is None

    def test_the_regions_follow_the_calibration(self) -> None:
        strip = a_real_bay()
        squeezed = RectangularCalibration(PhysicalFloor(width_m=2.0, depth_m=2.0))

        plain = regions_for(strip)
        moved = regions_for(strip, squeezed)

        assert plain is not None and moved is not None
        assert plain.protected != moved.protected
        assert moved.clears_protected()

    def test_a_band_below_the_minimum_is_refused(self) -> None:
        assert MINIMUM_BAND > 0.0
        shallow = project_rect(
            PhysicalRect(x_m=0.5, y_m=0.1, width_m=0.5, depth_m=0.1),
            DEFAULT_CALIBRATION,
        )

        assert coach_regions(shallow, chamfer_depth=0.0) is None

    def test_touching_edges_do_not_count_as_overlapping(self) -> None:
        above = FloorRect(x=0.0, y=0.0, width=1.0, height=0.5)
        below = FloorRect(x=0.0, y=0.5, width=1.0, height=0.5)

        assert not above.overlaps(below)
        assert above.overlaps(FloorRect(x=0.0, y=0.49, width=1.0, height=0.5))

    def test_the_regions_reach_the_overlay(self) -> None:
        regions = regions_for(a_real_bay())
        assert regions is not None

        payload = AnchorsPayload.from_anchors(
            FloorAnchors(
                information=FloorPoint(x=0.5, y=0.2),
                advice=FloorPoint(x=0.5, y=0.4),
                coach=regions,
            )
        )

        assert payload.coach is not None
        assert payload.coach.score.height > 0.0
        assert payload.coach.protected.y > payload.coach.metrics.y

    def test_a_floor_without_regions_says_so_rather_than_inventing_them(
        self,
    ) -> None:
        payload = AnchorsPayload.from_anchors(
            FloorAnchors(
                information=FloorPoint(x=0.5, y=0.2),
                advice=FloorPoint(x=0.5, y=0.4),
            )
        )

        assert payload.coach is None

    def test_the_layouts_own_strip_is_what_is_protected(self) -> None:
        layout = FloorLayout(strip=a_real_bay())

        regions = regions_for(layout.strip)

        assert regions is not None
        assert regions.clears_protected()
