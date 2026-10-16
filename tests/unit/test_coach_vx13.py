"""The vx13 reply: vx12's positions, with the words that make them obvious.

vx13 keeps what made vx12 cheap -- no checkpoint or metric is named, every
list is one position per catalogue item -- and puts back what the model
needs to read a picture right:

* `frames` maps each camera cell, by its printed ID, to the phase the model
  sees in it, and IMPACT is not a key;
* checkpoints are one list per phase and camera (`top_dtl`, `top_fo`,
  `top_any`), each entry `[score, "CELL_ID", confidence, "direction"]`, and a
  list's schema offers only its own camera's cells -- never IMPACT, never
  ALL_VALUES;
* a metric ProTee prints signed is written signed, and kept that way.

These hold what must be true of that:

* vx11, vx12 and vx13 replies stating the same facts coach the same shot the
  same way, down to every refusal and its reason;
* whatever does not decode is refused, never guessed;
* the printed P is only an aim: the phase the model says it sees decides.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from tests.unit.schema_support import (
    checkpoint_slot,
    citable_cells,
    fixed_length,
    listed_cells,
    resolved,
    wire_line,
    wire_list,
)
from tests.unit.test_coach_compact import (
    BODY,
    CELL_PHASES,
    COACH,
    SCENARIOS,
    VIEWS,
    Fact,
    coach_shots,
    compact,
    facts,
    home_cell,
    outcome,
)
from tests.unit.test_coach_protee import (
    DRIVER,
    Coach,
    a_reading,
    an_open_face_grid,
    every_metric_null,
)
from tests.unit.test_coach_v4 import the_94_shot
from tests.unit.test_coach_wire import (
    EXTRA_SCENARIOS,
    assessed,
    named,
    positional,
    wire_metrics,
)
from tests.unit.test_evidence_sheet import EXPECTED_CELLS, a_real_capture, coach_real

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
from caddiej.domain.shot import ClubKind, Handedness
from caddiej.domain.swing_checkpoints import CheckpointId, Deviation
from caddiej.domain.swing_checkpoints import CheckpointId as C
from caddiej.domain.swing_checkpoints import SwingFaultId as F
from caddiej.domain.swing_phase import PHASE_ORDER, SwingPhase
from caddiej.domain.vx import VX_METRICS, VX_METRICS_BY_NAME
from caddiej.swing_capture.sheet import render_evidence_sheet

P = SwingPhase
CAMERAS = tuple(CELL_PHASES)
DTL = tuple(cell for cell in CAMERAS if cell.startswith("DTL_"))
FO = tuple(cell for cell in CAMERAS if cell.startswith("FO_"))
SPECS: dict[CheckpointId, Checkpoint] = {spec.checkpoint_id: spec for spec in SCOREABLE}
WHERE: dict[CheckpointId, tuple[str, int]] = {
    spec.checkpoint_id: (key, position)
    for key, specs in vx.WIRE_LISTS.items()
    for position, spec in enumerate(specs)
}


def wire_entry(spec: Checkpoint, fact: Fact, cell: str) -> list[Any]:
    score, confidence, deviation = fact
    direction = None if deviation is None else deviation.value
    return [score, cell, round(confidence * 100), direction]


def lists(
    seen: dict[CheckpointId, Fact],
    metrics: dict[str, Any],
    coach: dict[str, Any],
    cells: dict[CheckpointId, str] | None = None,
) -> dict[str, Any]:
    """The vx13 reply stating exactly the facts `compact` states."""

    cited = cells or {}
    checkpoints: dict[str, list[Any]] = {}
    for key, specs in vx.WIRE_LISTS.items():
        entries: list[Any] = []
        for spec in specs:
            fact = seen.get(spec.checkpoint_id)
            cell = cited.get(spec.checkpoint_id, home_cell(spec))
            entries.append(
                None if fact is None or cell is None else wire_entry(spec, fact, cell)
            )
        checkpoints[key] = entries
    return {
        "frames": {cell: phase.value for cell, phase in CELL_PHASES.items()},
        "metrics": wire_metrics(metrics),
        "checkpoints": checkpoints,
        "coach": dict(coach),
    }


def read(reply: dict[str, Any]) -> tuple[Any, ...]:
    """Frames, checkpoints and metrics exactly as the service reads them."""

    payload = vx.decode_wire(reply, BODY)
    frames = vx.parse_frames(payload["frames"])
    items, rejected = vx.parse_checkpoints(
        payload["checkpoints"],
        body_frames=BODY,
        frame_phases=frames,
        frame_views=VIEWS,
    )
    metrics = vx.parse_metrics(payload["metrics"])
    return frames, items, rejected, metrics.as_dict(), metrics.rejected


def checkpoints_of(reply: dict[str, Any]) -> tuple[Any, ...]:
    return read(reply)[1:3]


def only(checkpoint: CheckpointId, entry: Any) -> dict[str, Any]:
    """A reply whose only assessment is `entry`, at `checkpoint`'s position."""

    reply = lists({}, {}, COACH)
    key, position = WHERE[checkpoint]
    reply["checkpoints"][key][position] = entry
    return reply


def refusal(reply: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    items, rejected = checkpoints_of(reply)
    assert items == ()
    return rejected


SETUP = SPECS[C.SETUP_SPINE_POSTURE]
SETUP_CELL = str(home_cell(SETUP))
SHOULDER = SPECS[C.TRANSITION_TRAIL_SHOULDER]


# --- the wire itself --------------------------------------------------------------


class TestTheLists:
    def test_every_scoreable_checkpoint_has_exactly_one_position(self) -> None:
        listed = [s.checkpoint_id for specs in vx.WIRE_LISTS.values() for s in specs]
        assert sorted(listed) == sorted(s.checkpoint_id for s in SCOREABLE)
        assert len(listed) == len(set(listed))

    def test_a_list_is_one_phase_and_one_camera_in_catalogue_order(self) -> None:
        for key, specs in vx.WIRE_LISTS.items():
            phase, camera = key.rsplit("_", 1)
            assert all(spec.phase is P(phase) for spec in specs), key
            assert all(spec.views == vx.WIRE_CAMERAS[camera] for spec in specs), key
            in_catalogue = [
                spec
                for spec in vx.WIRE_CHECKPOINTS[P(phase)]
                if vx.wire_camera(spec) == camera
            ]
            assert list(specs) == in_catalogue, key

    def test_the_lists_run_in_phase_order_then_dtl_fo_any(self) -> None:
        order = [
            (PHASE_ORDER.index(P(key.rsplit("_", 1)[0])), key.rsplit("_", 1)[1])
            for key in vx.WIRE_LISTS
        ]
        rank = {"dtl": 0, "fo": 1, "any": 2}
        assert order == sorted(order, key=lambda item: (item[0], rank[item[1]]))

    def test_each_position_is_its_own_checkpoint(self) -> None:
        read_here = 0
        for key, specs in vx.WIRE_LISTS.items():
            for spec in specs:
                cell = home_cell(spec)
                if cell is None:
                    # No cell of this sheet shows it (there is no FO_P2).
                    continue
                read_here += 1
                (item,), rejected = checkpoints_of(
                    only(spec.checkpoint_id, [60, cell, 80, None])
                )
                assert rejected == (), (key, spec.checkpoint_id)
                assert item.checkpoint is spec.checkpoint_id
        assert read_here == len(facts()) == 55

    def test_the_directions_are_every_word_a_checkpoint_has(self) -> None:
        assert set(vx.WIRE_DIRECTIONS) == {
            item.value for spec in SCOREABLE for item in spec.deviations
        }
        assert len(vx.WIRE_DIRECTIONS) == len(set(vx.WIRE_DIRECTIONS))


# --- what the real request offers ----------------------------------------------


class TestTheRealRequest:
    async def test_frames_is_one_key_per_camera_cell_and_never_impact(
        self, tmp_path: Path
    ) -> None:
        _, _, provider = await coach_real(tmp_path, (lists({}, {}, COACH),))
        schema = provider.requests[0].schema
        frames = schema["properties"]["frames"]
        cameras = EXPECTED_CELLS[:-2]
        assert tuple(frames["required"]) == cameras
        assert frames["additionalProperties"] is False
        assert "IMPACT" not in frames["properties"]
        assert "ALL_VALUES" not in frames["properties"]
        assert len([c for c in cameras if c.startswith("DTL_")]) == 8
        assert len([c for c in cameras if c.startswith("FO_")]) == 7
        for cell in cameras:
            phase = resolved(schema, frames["properties"][cell])
            assert phase["enum"] == [p.value for p in PHASE_ORDER] + ["unclear"]

    async def test_a_list_offers_only_its_own_cameras_cells(
        self, tmp_path: Path
    ) -> None:
        _, _, provider = await coach_real(tmp_path, (lists({}, {}, COACH),))
        schema = provider.requests[0].schema
        cameras = list(EXPECTED_CELLS[:-2])
        expected = {
            "dtl": [c for c in cameras if c.startswith("DTL_")],
            "fo": [c for c in cameras if c.startswith("FO_")],
            "any": cameras,
        }
        for key, specs in vx.WIRE_LISTS.items():
            assert fixed_length(wire_list(schema, key)) == len(specs), key
            cited = citable_cells(schema, key)
            assert cited == expected[key.rsplit("_", 1)[1]], key
            assert "IMPACT" not in cited and "ALL_VALUES" not in cited, key

    async def test_the_prompt_names_each_cell_and_each_list(
        self, tmp_path: Path
    ) -> None:
        _, _, provider = await coach_real(tmp_path, (lists({}, {}, COACH),))
        evidence = provider.requests[0].evidence
        assert listed_cells(evidence) == list(EXPECTED_CELLS[:-2])
        assert "IMPACT, ALL_VALUES are not camera cells" in evidence
        lines = evidence.splitlines()
        headers = [f"{key} ({len(specs)}):" for key, specs in vx.WIRE_LISTS.items()]
        assert [line for line in lines if line in headers] == headers
        assert "11 DTL_P1" not in evidence, "no code table"
        attack = next(line for line in lines if " Attack Angle: " in line)
        assert attack.endswith("; signed as printed, dir 0")
        assert "Never IMPACT, never ALL_VALUES" in vx.SYSTEM
        assert "Return compact JSON with no unnecessary whitespace." in vx.INSTRUCTIONS

    def test_one_camera_only_leaves_the_other_cameras_lists_null(self) -> None:
        schema = vx.reply_schema(
            (CueId.FACE_OPEN,),
            frame_ids=DTL,
            body_frame_ids=DTL,
            cell_views=dict.fromkeys(DTL, PanelView.BEHIND),
        )
        assert checkpoint_slot(schema, "impact_fo") == {"type": "null"}
        assert citable_cells(schema, "impact_dtl") == list(DTL)
        assert citable_cells(schema, "impact_any") == list(DTL)
        assert schema["properties"]["frames"]["required"] == list(DTL)

    def test_the_direction_words_are_offered_and_the_lines_say_which(self) -> None:
        evidence = vx.render_evidence(
            club=ClubKind.DRIVER,
            handedness=Handedness.RIGHT,
            has_metrics=True,
            swing_images=BODY,
            cues=(CueId.FACE_OPEN,),
            session_focus=None,
            focus_shots=0,
        )
        for key, specs in vx.WIRE_LISTS.items():
            for spec in specs:
                line = wire_line(evidence, key, spec.checkpoint_id.value)
                words = "|".join(item.value for item in spec.deviations) or "null"
                assert line.endswith(f" dir {words}"), spec.checkpoint_id


# --- parity: the same facts in every shape --------------------------------------


class TestTheSameFactsReadTheSame:
    @pytest.mark.parametrize("name", list(SCENARIOS))
    def test_vx11_vx12_and_vx13_parse_identically(self, name: str) -> None:
        for shot in SCENARIOS[name]:
            old = read(compact(*shot))
            assert read(positional(*shot)) == old
            assert read(lists(*shot)) == old

    @pytest.mark.parametrize("name", list(EXTRA_SCENARIOS))
    def test_refusals_keep_their_reasons(self, name: str) -> None:
        shot = EXTRA_SCENARIOS[name]
        old = read(named(*shot))
        assert read(positional(*shot)) == old
        assert read(lists(*shot)) == old

    def test_the_refusal_scenario_really_refuses(self) -> None:
        _, rejected = checkpoints_of(lists(*EXTRA_SCENARIOS["refusals of every kind"]))
        reasons = dict(rejected)
        assert reasons["setup_hip_hinge"] == "score"
        assert reasons["setup_spine_posture"] == "low_confidence"
        assert reasons["transition_trail_shoulder"] in {"view", "view_mismatch"}
        assert reasons["delivery_hip_depth"] in {"phase", "phase_conflict"}


class TestTheSameFactsCoachTheSame:
    @pytest.mark.parametrize("name", list(SCENARIOS))
    async def test_vx11_vx12_and_vx13_agree_downstream(
        self, tmp_path: Path, name: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        shots = SCENARIOS[name]
        caplog.set_level(logging.INFO, logger="caddiej.coaching.service")
        runs = []
        for shape, encode in (("vx11", compact), ("vx12", positional), ("vx13", lists)):
            caplog.clear()
            coach = await coach_shots(
                tmp_path / shape, tuple(encode(*s) for s in shots)
            )
            runs.append((coach, assessed(caplog)))

        (old, old_logged), *newer = runs
        for new, logged in newer:
            assert outcome(new) == outcome(old)
            assert logged == old_logged
            assert [r.status for r in new.results] == ["ok"] * len(shots)
            for before, after in zip(old.results, new.results, strict=True):
                assert after.metrics is not None and before.metrics is not None
                assert after.metrics.provenance == before.metrics.provenance
                assert after.metrics.rejected == before.metrics.rejected
                assert after.prompt_version == vx.PROTEE_PROMPT_VERSION == "vx13"

    async def test_refusals_agree_through_the_service(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        shot = EXTRA_SCENARIOS["refusals of every kind"]
        caplog.set_level(logging.INFO, logger="caddiej.coaching.service")
        old = await coach_shots(tmp_path / "a", (named(*shot),))
        old_logged = assessed(caplog)
        caplog.clear()
        new = await coach_shots(tmp_path / "b", (lists(*shot),))

        assert outcome(new) == outcome(old)
        assert assessed(caplog) == old_logged
        assert old_logged[0]["coach_checkpoints_rejected"] >= 4

    async def test_the_mechanics_still_find_the_root_and_the_progress(
        self, tmp_path: Path
    ) -> None:
        shoulder = await coach_shots(
            tmp_path / "a",
            tuple(
                lists(*s) for s in SCENARIOS["a trail shoulder out is the root cause"]
            ),
        )
        result = shoulder.results[0]
        assert result.review is not None and result.review.root is not None
        assert result.review.root.fault is F.TRAIL_SHOULDER_OUT
        assert result.swing_score is not None and result.swing_score.value is not None

        wing = await coach_shots(
            tmp_path / "b",
            tuple(
                lists(*s) for s in SCENARIOS["a chicken wing, then a better release"]
            ),
        )
        first, second = (event.advice for event in wing.bus.of(AnalysisCompleted))
        assert first.cue is second.cue
        assert second.review is not None and second.review.progress is not None
        assert second.review.progress.delta > 0


class TestProvenance:
    async def test_a_cited_cell_resolves_to_its_original_frame(
        self, tmp_path: Path
    ) -> None:
        reply = lists(
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

    def test_frames_keys_and_cited_cells_become_the_original_frames(self) -> None:
        reply = only(SETUP.checkpoint_id, [80, SETUP_CELL, 80, None])
        cell_map = {cell: f"swing_{index:02}" for index, cell in enumerate(CAMERAS)}
        canonical = vx.canonical_reply(vx.decode_wire(reply, BODY), cell_map)
        assert set(canonical["frames"]) == set(cell_map.values())
        cited = canonical["checkpoints"][SETUP.checkpoint_id.value]["cell"]
        assert cited == cell_map[SETUP_CELL]


# --- each cell on its own, and the phase the model sees ---------------------------


class TestEachCellIsItsOwnAnswer:
    @pytest.mark.parametrize("cell", CAMERAS)
    def test_a_cell_is_placed_without_reference_to_any_other(self, cell: str) -> None:
        reply = lists({}, {}, COACH)
        reply["frames"] = dict.fromkeys(CAMERAS, "unclear") | {cell: "finish"}
        frames, *_ = read(reply)
        assert frames == {cell: P.FINISH}

    def test_all_eight_dtl_and_all_seven_fo_cells_are_keys(self) -> None:
        assert len(DTL) == 8 and len(FO) == 7
        assert set(lists({}, {}, COACH)["frames"]) == set(CAMERAS)

    def test_a_cell_said_to_be_unclear_gives_phase_unknown(self) -> None:
        reply = only(SETUP.checkpoint_id, [80, SETUP_CELL, 80, None])
        reply["frames"][SETUP_CELL] = "unclear"
        assert refusal(reply) == ((SETUP.checkpoint_id.value, "phase_unknown"),)

    def test_a_cell_left_out_of_frames_gives_phase_unknown(self) -> None:
        reply = only(SETUP.checkpoint_id, [80, SETUP_CELL, 80, None])
        del reply["frames"][SETUP_CELL]
        assert refusal(reply) == ((SETUP.checkpoint_id.value, "phase_unknown"),)

    @pytest.mark.parametrize("word", ["P1", "SETUP", "1", "address"])
    def test_a_phase_word_that_is_not_one_is_unclear(self, word: str) -> None:
        reply = only(SETUP.checkpoint_id, [80, SETUP_CELL, 80, None])
        reply["frames"][SETUP_CELL] = word
        assert refusal(reply) == ((SETUP.checkpoint_id.value, "phase_unknown"),)

    def test_the_phase_the_model_sees_beats_the_printed_p(self) -> None:
        """DTL_P1 is aimed at setup; said to show finish, setup is not read."""

        reply = only(SETUP.checkpoint_id, [80, SETUP_CELL, 80, None])
        reply["frames"][SETUP_CELL] = "finish"
        (reason,) = [r for _, r in refusal(reply)]
        assert reason in {"phase", "phase_conflict"}

    def test_a_cell_aimed_elsewhere_that_shows_the_phase_is_read(self) -> None:
        """DTL_P4 is aimed at the top; said to show transition, it may carry a
        transition checkpoint."""

        aimed_elsewhere = next(
            cell
            for cell in DTL
            if CELL_PHASES[cell] is not P.TRANSITION
            and CELL_PHASES[cell] is not SHOULDER.phase
        )
        reply = only(SHOULDER.checkpoint_id, [35, aimed_elsewhere, 80, "out"])
        reply["frames"][aimed_elsewhere] = P.TRANSITION.value
        reply["frames"][str(home_cell(SHOULDER))] = "unclear"
        (item,), rejected = checkpoints_of(reply)
        assert rejected == ()
        assert item.checkpoint is C.TRANSITION_TRAIL_SHOULDER
        assert item.phase is P.TRANSITION


# --- strictness: what does not decode is refused ---------------------------------


class TestAListMustBeWhole:
    @pytest.mark.parametrize("change", [-1, +1])
    def test_a_short_or_long_list_refuses_every_position_in_it(
        self, change: int
    ) -> None:
        reply = lists(facts(), an_open_face_grid(), COACH)
        key = "setup_dtl"
        entries = reply["checkpoints"][key]
        reply["checkpoints"][key] = (
            entries[:-1] if change < 0 else [*entries, [80, SETUP_CELL, 80, None]]
        )

        items, rejected = checkpoints_of(reply)

        refused = {k for k, reason in rejected if reason == "length"}
        assert refused == {spec.checkpoint_id.value for spec in vx.WIRE_LISTS[key]}
        assert not any(item.checkpoint.value in refused for item in items)
        assert any(item.phase is P.SETUP for item in items), "setup_any unharmed"

    def test_a_missing_list_is_refused_not_skipped(self) -> None:
        reply = lists(facts(), an_open_face_grid(), COACH)
        del reply["checkpoints"]["finish_any"]

        _, rejected = checkpoints_of(reply)

        assert {k for k, r in rejected if r == "length"} == {
            spec.checkpoint_id.value for spec in vx.WIRE_LISTS["finish_any"]
        }

    def test_a_list_that_is_not_a_list_is_refused(self) -> None:
        reply = lists(facts(), an_open_face_grid(), COACH)
        reply["checkpoints"]["finish_any"] = {"finish_balance": [80, "FO_P8", 80]}

        _, rejected = checkpoints_of(reply)

        assert ("finish_balance", "length") in rejected

    def test_an_unknown_list_key_is_refused_as_unknown(self) -> None:
        reply = lists(facts(), an_open_face_grid(), COACH)
        reply["checkpoints"]["swing_score"] = [99]

        _, rejected = checkpoints_of(reply)

        assert ("swing_score", "unknown") in rejected


class TestAnEntryMustBeExactlyItsFourFields:
    @pytest.mark.parametrize(
        ("entry", "why"),
        [
            ([80, SETUP_CELL, 80], "too short"),
            ([80, SETUP_CELL, 80, None, None], "too long"),
            ([], "empty"),
            ([80.0, SETUP_CELL, 80, None], "a float score"),
            ([80, 11, 80, None], "a vx12 cell code, not an ID"),
            ([80, None, 80, None], "no cell"),
            ([80, SETUP_CELL, 0.8, None], "a fraction, not a percent"),
            ([80, SETUP_CELL, 80, 1], "a vx12 direction code, not a word"),
            ([80, SETUP_CELL, 80, 0], "a zero, not null"),
            ([True, SETUP_CELL, 80, None], "a boolean"),
            ([SETUP_CELL, 80, 80, None], "the cell first"),
            ({"score": 80}, "an object"),
            (f"80,{SETUP_CELL},80,null", "a string"),
        ],
    )
    def test_a_misshapen_entry_is_refused_as_shape(self, entry: Any, why: str) -> None:
        assert refusal(only(SETUP.checkpoint_id, entry)) == (
            (SETUP.checkpoint_id.value, "shape"),
        ), why

    @pytest.mark.parametrize(
        ("entry", "reason"),
        [
            ([101, SETUP_CELL, 80, None], "score"),
            ([-1, SETUP_CELL, 80, None], "score"),
            ([80, SETUP_CELL, 101, None], "confidence"),
            ([80, SETUP_CELL, 10, None], "low_confidence"),
            ([80, "DTL_P9", 80, None], "frame"),
            ([80, "dtl_p1", 80, None], "frame"),
            ([80, "DTL P1", 80, None], "frame"),
            ([80, "", 80, None], "frame"),
            ([80, "IMPACT", 80, None], "view_unknown"),
        ],
    )
    def test_a_value_out_of_range_is_refused_for_its_reason(
        self, entry: list[Any], reason: str
    ) -> None:
        assert refusal(only(SETUP.checkpoint_id, entry)) == (
            (SETUP.checkpoint_id.value, reason),
        )

    def test_the_metrics_grid_is_never_a_body_cell(self) -> None:
        (reason,) = [
            r
            for _, r in refusal(only(SETUP.checkpoint_id, [80, "ALL_VALUES", 80, None]))
        ]
        assert reason in {"frame", "metrics_frame"}

    def test_a_cell_from_the_wrong_camera_is_refused_as_view(self) -> None:
        for spec in SCOREABLE:
            if len(spec.views) != 1:
                continue
            home = home_cell(spec)
            if home is None:
                continue
            others = FO if home.startswith("DTL_") else DTL
            other = next(
                (cell for cell in others if CELL_PHASES[cell] is CELL_PHASES[home]),
                others[0],
            )
            direction = spec.deviations[0].value if spec.deviations else None
            assert refusal(only(spec.checkpoint_id, [50, other, 80, direction])) == (
                (spec.checkpoint_id.value, "view"),
            ), spec.checkpoint_id

    @pytest.mark.parametrize("word", ["OUT", "sideways", "Out", " out", "1"])
    def test_a_direction_word_the_checkpoint_does_not_have_is_refused(
        self, word: str
    ) -> None:
        reply = only(SHOULDER.checkpoint_id, [35, str(home_cell(SHOULDER)), 80, word])
        assert refusal(reply) == ((SHOULDER.checkpoint_id.value, "deviation"),)

    def test_a_direction_on_a_checkpoint_with_none_is_refused(self) -> None:
        spec = next(s for s in SCOREABLE if not s.deviations)
        reply = only(spec.checkpoint_id, [70, str(home_cell(spec)), 80, "out"])
        assert refusal(reply) == ((spec.checkpoint_id.value, "deviation"),)

    def test_each_direction_word_names_its_own_direction(self) -> None:
        cell = str(home_cell(SHOULDER))
        for deviation in SHOULDER.deviations:
            (item,), rejected = checkpoints_of(
                only(SHOULDER.checkpoint_id, [35, cell, 80, deviation.value])
            )
            assert rejected == ()
            assert item.deviation is deviation

    def test_confidence_is_a_percent_capped_like_any_other(self) -> None:
        (item,), _ = checkpoints_of(
            only(SETUP.checkpoint_id, [80, SETUP_CELL, 83, None])
        )
        assert item.confidence == 0.85
        (item,), _ = checkpoints_of(
            only(SETUP.checkpoint_id, [80, SETUP_CELL, 100, None])
        )
        assert item.confidence == 0.9

    def test_the_score_is_not_quantised(self) -> None:
        (item,), _ = checkpoints_of(
            only(SETUP.checkpoint_id, [73, SETUP_CELL, 80, None])
        )
        assert item.score == 73

    def test_a_null_position_is_simply_not_assessed(self) -> None:
        assert checkpoints_of(lists({}, {}, COACH)) == ((), ())


class TestOlderRepliesStillRead:
    def test_a_vx12_coded_reply_still_decodes(self) -> None:
        shot = SCENARIOS["a trail shoulder out is the root cause"][0]
        assert read(positional(*shot)) == read(lists(*shot))

    def test_a_vx11_reply_passes_through_the_decoder_untouched(self) -> None:
        reply = compact(facts(), an_open_face_grid(), COACH)
        assert vx.decode_wire(reply, BODY) == reply


# --- newest shot wins ----------------------------------------------------------


class HeldLists(ScriptedProvider):
    """Holds the first answer until released, so a newer strike can land."""

    def __init__(self) -> None:
        reply = lists(facts(), an_open_face_grid(), COACH)
        super().__init__(replies=(reply, reply))
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: AdviceRequest) -> ProviderReply:
        if not self.started.is_set():
            self.started.set()
            await self.release.wait()
        return await super().complete(request)


class TestNewestShotWins:
    async def test_a_late_vx13_reply_for_an_older_shot_is_never_coached(
        self, tmp_path: Path
    ) -> None:
        provider = HeldLists()
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


# --- a signed metric is the number ProTee prints ---------------------------------


def grid(**readings: Any) -> Any:
    metrics = every_metric_null()
    metrics.update(readings)
    return vx.parse_metrics(metrics)


def on_the_wire(name: str, value: float, direction: int) -> Any:
    spec = VX_METRICS_BY_NAME[name]
    wire: list[Any] = [None] * len(VX_METRICS)
    wire[VX_METRICS.index(spec)] = [value, 1, direction]
    return vx.parse_metrics(vx.decode_wire({"metrics": wire}, BODY)["metrics"])


class TestSignedMetrics:
    def test_only_the_two_metrics_protee_prints_signed_are_signed(self) -> None:
        assert {spec.name for spec in VX_METRICS if spec.signed} == {
            "attack_angle",
            "impact_height",
        }

    @pytest.mark.parametrize(
        ("name", "unit", "value", "word", "kept"),
        [
            ("attack_angle", "deg", -6.3, None, -6.3),
            ("attack_angle", "deg", 6.3, None, 6.3),
            ("attack_angle", "deg", -6.3, "down", -6.3),
            ("attack_angle", "deg", 6.3, "down", -6.3),
            ("attack_angle", "deg", 6.3, "up", 6.3),
            ("attack_angle", "deg", 0.0, "up", 0.0),
            ("attack_angle", "deg", 0.0, "down", 0.0),
            ("impact_height", "mm", -3.7, None, -3.7),
            ("impact_height", "mm", 3.7, None, 3.7),
            ("impact_height", "mm", -3.7, "low", -3.7),
            ("impact_height", "mm", 3.7, "low", -3.7),
            ("impact_height", "mm", 3.7, "high", 3.7),
        ],
    )
    def test_a_sign_and_an_agreeing_word_are_one_signed_number(
        self, name: str, unit: str, value: float, word: str | None, kept: float
    ) -> None:
        metrics = grid(**{name: a_reading(value, unit, word)})
        reading = metrics.get(name)
        assert reading is not None and metrics.rejected == ()
        assert (reading.value, reading.direction) == (kept, None)

    @pytest.mark.parametrize(
        ("name", "unit", "value", "word"),
        [
            ("attack_angle", "deg", -6.3, "up"),
            ("impact_height", "mm", -3.7, "high"),
        ],
    )
    def test_a_sign_and_a_word_that_disagree_are_refused(
        self, name: str, unit: str, value: float, word: str
    ) -> None:
        metrics = grid(**{name: a_reading(value, unit, word)})
        assert metrics.get(name) is None
        assert metrics.rejected == (name,)

    @pytest.mark.parametrize(
        ("name", "value", "code", "kept"),
        [
            ("attack_angle", -6.3, 0, -6.3),
            ("attack_angle", 6.3, 2, -6.3),
            ("attack_angle", 6.3, 1, 6.3),
            ("attack_angle", -6.3, 1, None),
            ("impact_height", -3.7, 0, -3.7),
            ("impact_height", 3.7, 2, -3.7),
            ("impact_height", -3.7, 1, None),
        ],
    )
    def test_the_wire_form_is_read_the_same_way(
        self, name: str, value: float, code: int, kept: float | None
    ) -> None:
        metrics = on_the_wire(name, value, code)
        reading = metrics.get(name)
        assert (None if reading is None else reading.value) == kept
        assert metrics.rejected == (() if kept is not None else (name,))

    def test_a_worded_metric_still_has_no_sign(self) -> None:
        metrics = grid(face_angle=a_reading(-4.5, "deg", "closed"))
        assert metrics.get("face_angle") is None
        assert metrics.rejected == ("face_angle",)

    @pytest.mark.parametrize(
        ("value", "word", "expected"),
        [(-6.3, None, -6.3), (6.3, None, 6.3), (6.3, "down", -6.3), (6.3, "up", 6.3)],
    )
    def test_caddiejs_sign_for_attack_angle_is_unchanged(
        self, value: float, word: str | None, expected: float
    ) -> None:
        metrics = grid(attack_angle=a_reading(value, "deg", word))
        for handedness in Handedness:
            assert vx.signed(metrics, "attack_angle", handedness) == expected
