"""The vx11 compact reply: the model returns facts, CaddieJ derives the rest.

The vx10 reply restated, for every checkpoint, the phase and the view of the
picture it cited and wrote a sentence about it: about two thirds of five
thousand output tokens, none of it read by anything a player sees. vx11 asks
for `{score, conf, cell, dir}` and takes the view from the cell's camera and
the phase from the reply's own `frames` answer.

These hold the two things that must be true of that:

* every refusal still happens, for the same reason, through the smaller shape;
* an old verbose reply and a compact one stating the same visual facts lead
  to exactly the same scores, faults, root cause, lesson and cue.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from tests.unit.test_coach_protee import DRIVER, Coach, a_reading, an_open_face_grid
from tests.unit.test_coach_v4 import the_94_shot
from tests.unit.test_evidence_sheet import DTL_PHASES, FO_PHASES, a_real_capture

from caddiej.coaching import vx
from caddiej.coaching.checkpoints import SCOREABLE, Checkpoint
from caddiej.coaching.fakes import ScriptedProvider
from caddiej.coaching.provider import AdviceRequest, ProviderReply
from caddiej.domain.coaching import AdviceFailure
from caddiej.domain.cues import CueId, PanelView
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisFailed,
    ClubSelected,
    ShotDetected,
    SwingFramesReady,
)
from caddiej.domain.swing_checkpoints import (
    CHECKPOINT_VIEW_WORDS,
    CheckpointId,
    Deviation,
)
from caddiej.domain.swing_checkpoints import CheckpointId as C
from caddiej.domain.swing_checkpoints import SwingFaultId as F
from caddiej.domain.swing_phase import SwingPhase
from caddiej.domain.vx import VX_METRICS, VX_METRICS_BY_NAME
from caddiej.persistence.records import coach_result_row
from caddiej.swing_capture.sheet import render_evidence_sheet
from caddiej.swing_capture.store import METRICS_FRAME_ID

P = SwingPhase

# The real capture's cells and the moment each was aimed at, which in these
# fixtures is also what the model says each shows.
CELL_PHASES: dict[str, SwingPhase] = {
    **{f"DTL_P{index}": phase for index, phase in enumerate(DTL_PHASES, start=1)},
    **{f"FO_P{DTL_PHASES.index(phase) + 1}": phase for phase in FO_PHASES},
}
CELL_VIEWS = {
    cell: PanelView.BEHIND if cell.startswith("DTL_") else PanelView.SIDE
    for cell in CELL_PHASES
}
BODY = (*CELL_PHASES, "IMPACT")


def home_cell(spec: Checkpoint) -> str | None:
    """The cell a careful reader would cite: the checkpoint's own moment from
    the first camera it allows, else any moment it allows."""

    for view in (PanelView.BEHIND, PanelView.SIDE):
        if view not in spec.views:
            continue
        cells = [cell for cell, seen in CELL_VIEWS.items() if seen is view]
        for cell in cells:
            if CELL_PHASES[cell] is spec.phase:
                return cell
        for cell in cells:
            if CELL_PHASES[cell] in spec.phases:
                return cell
    return None


Fact = tuple[int, float, Deviation | None]


def facts(base: int = 85, **changes: Fact) -> dict[CheckpointId, Fact]:
    """What the pictures show: every checkpoint a cell can show, at `base`."""

    seen: dict[CheckpointId, Fact] = {
        spec.checkpoint_id: (base, 0.7, None)
        for spec in SCOREABLE
        if home_cell(spec) is not None
    }
    seen.update({CheckpointId(key): fact for key, fact in changes.items()})
    return seen


COACH = {
    "kind": "correction",
    "cue": "FACE_OPEN",
    "observation": None,
    "instruction": "Square the face a little earlier.",
    "reinforcement": None,
    "evidence_frame_id": "ALL_VALUES",
}


def verbose(
    seen: dict[CheckpointId, Fact], metrics: dict[str, Any], coach: dict[str, Any]
) -> dict[str, Any]:
    """The vx10 reply: every checkpoint restates phase and view, with a note."""

    checkpoints: dict[str, Any] = {}
    for spec in SCOREABLE:
        fact = seen.get(spec.checkpoint_id)
        cell = home_cell(spec)
        if fact is None or cell is None:
            checkpoints[spec.checkpoint_id.value] = None
            continue
        score, confidence, deviation = fact
        entry: dict[str, Any] = {
            "score": score,
            "confidence": confidence,
            "frame_id": cell,
            "phase": CELL_PHASES[cell].value,
            "view": CHECKPOINT_VIEW_WORDS[CELL_VIEWS[cell]],
            "observation": "The trail elbow stays close to the body.",
        }
        if spec.deviations:
            entry["deviation"] = None if deviation is None else deviation.value
        checkpoints[spec.checkpoint_id.value] = entry
    return {
        "frames": [
            {"frame_id": cell, "shows": phase.value}
            for cell, phase in CELL_PHASES.items()
        ]
        + [{"frame_id": "IMPACT", "shows": "unclear"}],
        "metrics": metrics,
        "checkpoints": checkpoints,
        "coach": coach | {"visual_confidence": None},
    }


def compact_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """The same readings with the keys vx11 does not ask for left out."""

    out: dict[str, Any] = {}
    for name, item in metrics.items():
        if item is None:
            out[name] = None
            continue
        spec = VX_METRICS_BY_NAME[name]
        reading = dict(item)
        if len(spec.units) == 1:
            reading.pop("unit")
        if not spec.directions:
            reading.pop("direction", None)
        out[name] = reading
    return out


def compact(
    seen: dict[CheckpointId, Fact], metrics: dict[str, Any], coach: dict[str, Any]
) -> dict[str, Any]:
    """The vx11 reply stating exactly the same facts."""

    checkpoints: dict[str, Any] = {}
    for spec in SCOREABLE:
        fact = seen.get(spec.checkpoint_id)
        cell = home_cell(spec)
        if fact is None or cell is None:
            checkpoints[spec.checkpoint_id.value] = None
            continue
        score, confidence, deviation = fact
        entry: dict[str, Any] = {"score": score, "conf": confidence, "cell": cell}
        if spec.deviations:
            entry["dir"] = None if deviation is None else deviation.value
        checkpoints[spec.checkpoint_id.value] = entry
    return {
        "frames": {cell: phase.value for cell, phase in CELL_PHASES.items()}
        | {"IMPACT": "unclear"},
        "metrics": compact_metrics(metrics),
        "checkpoints": checkpoints,
        "coach": dict(coach),
    }


# --- the parser, directly -------------------------------------------------------

VIEWS = {cell: CELL_VIEWS.get(cell, PanelView.NONE) for cell in BODY}
SEEN = {cell: phase for cell, phase in CELL_PHASES.items()}


def read(
    entries: dict[str, Any], phases: dict[str, SwingPhase] | None = None
) -> tuple[tuple[Any, ...], tuple[tuple[str, str], ...]]:
    return vx.parse_checkpoints(
        entries,
        body_frames=BODY,
        frame_phases=SEEN if phases is None else phases,
        frame_views=VIEWS,
    )


def entry(score: int = 40, conf: Any = 0.8, cell: Any = "DTL_P5", **extra: Any):
    return {"score": score, "conf": conf, "cell": cell} | extra


class TestACompactEntry:
    def test_takes_its_view_from_the_cell_and_its_phase_from_frames(self) -> None:
        (item,), rejected = read(
            {"transition_trail_shoulder": entry(35, 0.83, dir="out")}
        )

        assert rejected == ()
        assert item.checkpoint is C.TRANSITION_TRAIL_SHOULDER
        assert (item.score, item.confidence) == (35, 0.85)
        assert item.frame_id == "DTL_P5"
        assert item.phase is P.TRANSITION
        assert item.view is PanelView.BEHIND
        assert item.deviation is Deviation.OUT
        assert item.observation == "", "no prose is asked for or invented"

    def test_a_null_checkpoint_is_simply_not_assessed(self) -> None:
        items, rejected = read({spec.checkpoint_id.value: None for spec in SCOREABLE})

        assert items == ()
        assert rejected == (), "not assessed is not a refusal"

    @pytest.mark.parametrize(
        ("item", "reason"),
        [
            ("not an object", "shape"),
            (entry(score=101), "score"),
            (entry(score=True), "score"),
            (entry(conf=1.2), "confidence"),
            (entry(conf="high"), "confidence"),
            (entry(conf=0.2), "low_confidence"),
            (entry(cell="ALL_VALUES"), "frame"),
            (entry(cell=METRICS_FRAME_ID), "metrics_frame"),
            (entry(cell="DTL_P9"), "frame"),
            (entry(cell="IMPACT"), "view_unknown"),
            (entry(cell="FO_P5"), "view"),
            (entry(cell="DTL_P1"), "phase"),
            (entry(dir="sideways"), "deviation"),
            (entry(dir="in"), "deviation"),
        ],
    )
    def test_every_refusal_still_has_its_own_reason(
        self, item: object, reason: str
    ) -> None:
        items, rejected = read({"transition_trail_shoulder": item})

        assert items == ()
        assert rejected == (("transition_trail_shoulder", reason),)

    def test_a_cell_the_model_called_unclear_supports_nothing(self) -> None:
        phases = dict(SEEN)
        del phases["DTL_P5"]

        items, rejected = read({"transition_trail_shoulder": entry()}, phases)

        assert items == ()
        assert rejected == (("transition_trail_shoulder", "phase_unknown"),)

    def test_a_label_is_not_a_phase(self) -> None:
        """DTL_P8 was aimed at the release; the model says it shows the finish,
        so a release checkpoint cited from it is refused."""

        phases = dict(SEEN) | {"DTL_P8": P.FINISH}

        items, rejected = read(
            {"release_lead_arm_structure": entry(30, cell="DTL_P8", dir="bent")},
            phases,
        )

        assert items == ()
        assert rejected == (("release_lead_arm_structure", "phase"),)

    def test_a_stated_phase_or_view_is_still_checked_not_trusted(self) -> None:
        _, rejected = read(
            {
                "transition_trail_shoulder": entry(phase="downswing"),
                "transition_shallowing": entry(view="face_on"),
            }
        )

        assert dict(rejected) == {
            "transition_trail_shoulder": "phase_conflict",
            "transition_shallowing": "view_mismatch",
        }

    def test_the_schema_offers_the_compact_fields_and_no_prose(self) -> None:
        schema = vx.reply_schema((CueId.FACE_OPEN,), (*BODY, METRICS_FRAME_ID), BODY)
        text = json.dumps(schema)

        for word in (
            '"observation": {"type": "string", "maxLength"',
            '"view"',
            '"visual_confidence"',
            '"frame_id"',
        ):
            assert word not in text, word
        # `phase` is the shared enum frames' values point at, never a field.
        assert '"phase"' not in json.dumps(schema["properties"])
        assert not any(
            "phase" in definition.get("properties", {})
            for definition in schema["$defs"].values()
        )
        assert set(schema["properties"]["frames"]["required"]) == set(BODY)


class TestTheCellResolvesToItsOriginal:
    def test_frames_keys_and_cells_are_translated_values_are_not(self) -> None:
        cells = {"DTL_P5": "swing_behind_05", "ALL_VALUES": METRICS_FRAME_ID}
        reply = {
            "frames": {"DTL_P5": "transition", "FO_P9": "finish"},
            "checkpoints": {"transition_trail_shoulder": entry(cell="DTL_P5")},
            "coach": {"evidence_frame_id": "ALL_VALUES", "observation": "DTL_P5"},
        }

        canonical = vx.canonical_reply(reply, cells)

        assert canonical["frames"] == {
            "swing_behind_05": "transition",
            "FO_P9": "finish",
        }
        assert canonical["checkpoints"]["transition_trail_shoulder"]["cell"] == (
            "swing_behind_05"
        )
        assert canonical["coach"]["evidence_frame_id"] == METRICS_FRAME_ID
        assert canonical["coach"]["observation"] == "DTL_P5", "prose is not an id"
        assert vx.parse_frames(canonical["frames"])["swing_behind_05"] is (P.TRANSITION)

    async def test_through_the_service_a_compact_fault_points_at_the_original(
        self, tmp_path
    ) -> None:
        reply = compact(
            facts(
                release_lead_arm_structure=(30, 0.8, Deviation.BENT),
                impact_arm_structure=(35, 0.8, Deviation.BENT),
            ),
            the_94_shot(),
            COACH,
        )
        coach = await coach_shots(tmp_path, (reply,))

        result = coach.results[0]
        assert result.review is not None
        wing = next(f for f in result.review.faults if f.fault is F.CHICKEN_WING)
        assert wing.frame_id is not None and wing.frame_id.startswith("swing_")
        mechanics = result.swing_score.mechanics  # type: ignore[union-attr]
        cited = [item.frame_id for item in mechanics.checkpoints if item.frame_id]
        assert cited
        assert all(frame.startswith("swing_") for frame in cited), "never a cell id"


class TestAllValuesThroughTheCompactShape:
    def test_a_single_unit_metric_is_read_without_a_unit(self) -> None:
        single = next(spec for spec in VX_METRICS if len(spec.units) == 1)

        value = single.limit / 2

        metrics = vx.parse_metrics({single.name: {"value": value}})

        reading = metrics.get(single.name)
        assert reading is not None
        assert (reading.value, reading.unit) == (value, single.units[0])

    def test_a_metric_with_a_choice_of_units_still_needs_one(self) -> None:
        several = next(spec for spec in VX_METRICS if len(spec.units) > 1)

        metrics = vx.parse_metrics({several.name: {"value": 140.0}})

        assert metrics.get(several.name) is None
        assert several.name in metrics.rejected

    def test_a_wrong_unit_or_word_is_still_refused(self) -> None:
        metrics = vx.parse_metrics(
            {
                "face_angle": {"value": 2.0, "unit": "mph", "direction": "open"},
                "swing_path": {"value": 2.0, "direction": "left"},
            }
        )

        assert metrics.get("face_angle") is None
        assert metrics.get("swing_path") is None

    def test_both_shapes_of_the_same_grid_read_the_same(self) -> None:
        grid = the_94_shot()

        old = vx.parse_metrics(grid)
        new = vx.parse_metrics(compact_metrics(grid))

        assert new.as_dict() == old.as_dict()
        assert new.available == old.available


# --- parity: the same facts, either shape, the same coaching --------------------


async def coach_shots(tmp_path: Path, replies: tuple[dict[str, Any], ...]) -> Coach:
    coach = Coach(
        ScriptedProvider(replies=replies),
        max_images=16,
        evidence_sheet=render_evidence_sheet,
    )
    await coach.service.open()
    await coach.service.on_club_selected(ClubSelected(club=DRIVER))
    for index in range(len(replies)):
        detected = ShotDetected(trigger_id=str(60 + index))
        await coach.service.on_shot_detected(detected)
        capture = a_real_capture(
            tmp_path / str(index),
            strike_id=detected.event_id,
            trigger_id=str(60 + index),
        )
        await coach.service.on_swing_frames(SwingFramesReady(capture=capture))
        async with asyncio.timeout(5.0):
            while len(coach.results) <= index:
                await asyncio.sleep(0.01)
    await coach.service.aclose()
    return coach


SESSION = UUID(int=7)
COMPARED = (
    "status",
    "kind",
    "focus",
    "cue_id",
    "display",
    "speech",
    "severity",
    "confidence",
    "confidence_source",
    "metrics_read",
    "metrics_json",
    "swing_score",
    "swing_score_rubric",
)


def outcome(coach: Coach) -> list[dict[str, Any]]:
    """Everything stored and shown for each shot, minus the vx10 note."""

    shots = []
    for result, event in zip(
        coach.results, coach.bus.of(AnalysisCompleted), strict=True
    ):
        row = coach_result_row(SESSION, result)
        document = json.loads(str(row["swing_score_json"]))
        for item in document.get("mechanics", {}).get("checkpoints", []):
            item.pop("observation")
        advice = event.advice
        shots.append(
            {key: row[key] for key in COMPARED}
            | {
                "swing_score_json": document,
                "advice": (
                    advice.cue,
                    advice.kind,
                    advice.focus,
                    advice.display,
                    advice.observation,
                    advice.speech,
                    advice.source,
                ),
            }
        )
    return shots


SCENARIOS: dict[
    str, tuple[tuple[dict[CheckpointId, Fact], dict[str, Any], dict], ...]
] = {
    "a sound swing, the numbers decide": ((facts(), an_open_face_grid(), COACH),),
    "a trail shoulder out is the root cause": (
        (
            facts(
                transition_trail_shoulder=(35, 0.8, Deviation.OUT),
                transition_hand_direction=(40, 0.8, Deviation.OUT),
            ),
            the_94_shot(),
            COACH,
        ),
    ),
    "a chicken wing, then a better release": (
        (
            facts(
                release_lead_arm_structure=(43, 0.8, Deviation.BENT),
                impact_arm_structure=(43, 0.8, Deviation.BENT),
            ),
            the_94_shot(),
            COACH | {"kind": "reinforcement", "cue": "STRIKE_GOOD"},
        ),
        (
            facts(release_lead_arm_structure=(81, 0.8, None)),
            the_94_shot(),
            COACH | {"kind": "reinforcement", "cue": "STRIKE_GOOD"},
        ),
    ),
    "a slice the model reads off the grid": (
        (
            facts(70),
            an_open_face_grid(
                face_angle=a_reading(1.0, "deg", "open"),
                swing_path=a_reading(0.5, "deg", "out_to_in"),
                spin_axis=a_reading(14.0, "deg", "right"),
            ),
            COACH | {"cue": "CURVE_SLICE"},
        ),
    ),
}


class TestTheSameFactsCoachTheSame:
    @pytest.mark.parametrize("name", list(SCENARIOS))
    async def test_verbose_and_compact_replies_agree_downstream(
        self, tmp_path, name: str
    ) -> None:
        shots = SCENARIOS[name]
        old = await coach_shots(
            tmp_path / "vx10", tuple(verbose(*shot) for shot in shots)
        )
        new = await coach_shots(
            tmp_path / "vx11", tuple(compact(*shot) for shot in shots)
        )

        assert outcome(new) == outcome(old)
        assert [r.status for r in new.results] == ["ok"] * len(shots)

    async def test_the_scenarios_really_exercise_the_mechanics(self, tmp_path) -> None:
        """Parity of two empty results would prove nothing."""

        shoulder = await coach_shots(
            tmp_path / "a",
            tuple(
                compact(*shot)
                for shot in SCENARIOS["a trail shoulder out is the root cause"]
            ),
        )
        review = shoulder.results[0].review
        assert review is not None and review.root is not None
        assert review.root.fault is F.TRAIL_SHOULDER_OUT
        score = shoulder.results[0].swing_score
        assert score is not None and score.value is not None

        wing = await coach_shots(
            tmp_path / "b",
            tuple(
                compact(*shot)
                for shot in SCENARIOS["a chicken wing, then a better release"]
            ),
        )
        first, second = (event.advice for event in wing.bus.of(AnalysisCompleted))
        assert first.cue is second.cue is CueId.CHICKEN_WING
        assert second.review is not None and second.review.progress is not None
        assert second.review.progress.delta > 0
        assert second.observation == "MUCH BETTER RELEASE"


# --- newest shot wins, with compact replies --------------------------------------


class HeldCompact(ScriptedProvider):
    """Holds the first answer until released, so a newer strike can land."""

    def __init__(self) -> None:
        reply = compact(facts(), an_open_face_grid(), COACH)
        super().__init__(replies=(reply, reply))
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: AdviceRequest) -> ProviderReply:
        if not self.started.is_set():
            self.started.set()
            await self.release.wait()
        return await super().complete(request)


class TestNewestShotWins:
    async def test_a_late_compact_reply_for_an_older_shot_is_never_coached(
        self, tmp_path
    ) -> None:
        provider = HeldCompact()
        coach = Coach(provider, max_images=16, evidence_sheet=render_evidence_sheet)
        await coach.service.open()
        await coach.service.on_club_selected(ClubSelected(club=DRIVER))
        old = ShotDetected(trigger_id="16")
        await coach.service.on_shot_detected(old)
        first = a_real_capture(tmp_path / "a", strike_id=old.event_id, trigger_id="16")
        task = asyncio.create_task(
            coach.service.on_swing_frames(SwingFramesReady(capture=first))
        )
        await asyncio.wait_for(provider.started.wait(), 2.0)

        new = ShotDetected(trigger_id="17")
        await coach.service.on_shot_detected(new)
        provider.release.set()
        await task
        second = a_real_capture(tmp_path / "b", strike_id=new.event_id, trigger_id="17")
        await coach.service.on_swing_frames(SwingFramesReady(capture=second))
        await coach.service.aclose()

        completed = coach.bus.of(AnalysisCompleted)
        assert [event.shot_id for event in completed] == [new.event_id]
        failed = coach.bus.of(AnalysisFailed)
        assert [(e.shot_id, e.reason) for e in failed] == [
            (old.event_id, AdviceFailure.SUPERSEDED)
        ]
        score = coach.results[-1].swing_score
        assert score is not None and score.value is not None
