"""The vx12 positional reply: the same facts as vx11, in a fraction of the tokens.

vx13 is what is asked for now (see `test_coach_vx13`); a vx12 reply is still
decoded, and these hold that it still reads exactly as it did.

vx12 writes no names. `frames` is one phase code per body cell, `metrics` one
`[value, unit, direction]` per registry metric, and each phase's checkpoints
one `[score, cell, confidence, direction]` per position. CaddieJ turns that
back into the named reply before anything reads it.

These hold what must be true of that:

* a vx11 reply and a vx12 reply stating the same facts coach the same shot
  the same way, down to every refusal and its reason;
* a position or a code that does not decode is refused, never guessed: the
  parser is stricter than it was, not looser.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from tests.unit.test_coach_compact import (
    BODY,
    CELL_PHASES,
    COACH,
    SCENARIOS,
    SEEN,
    VIEWS,
    Fact,
    coach_shots,
    compact,
    compact_metrics,
    facts,
    home_cell,
    outcome,
)
from tests.unit.test_coach_protee import a_reading, an_open_face_grid
from tests.unit.test_coach_v4 import the_94_shot

from caddiej.coaching import vx
from caddiej.coaching.checkpoints import SCOREABLE, Checkpoint
from caddiej.domain.cues import CueId
from caddiej.domain.events import AnalysisCompleted
from caddiej.domain.swing_checkpoints import CheckpointId, Deviation
from caddiej.domain.swing_checkpoints import CheckpointId as C
from caddiej.domain.swing_checkpoints import SwingFaultId as F
from caddiej.domain.swing_phase import PHASE_ORDER, SwingPhase
from caddiej.domain.vx import VX_METRICS, VX_METRICS_BY_NAME

P = SwingPhase
CODES = vx.cell_codes(BODY)
SPECS: dict[CheckpointId, Checkpoint] = {spec.checkpoint_id: spec for spec in SCOREABLE}


def phase_code(phase: SwingPhase | None) -> int:
    return 0 if phase is None else PHASE_ORDER.index(phase) + 1


def wire_metrics(metrics: dict[str, Any]) -> list[Any]:
    """The same readings, one positional entry per registry metric."""

    out: list[Any] = []
    for spec in VX_METRICS:
        item = metrics.get(spec.name)
        if item is None:
            out.append(None)
            continue
        unit = item.get("unit", spec.units[0].value)
        direction = item.get("direction")
        units = [u.value for u in spec.units]
        directions = [d.value for d in spec.directions]
        out.append(
            [
                item["value"],
                units.index(unit) + 1,
                0 if direction is None else directions.index(direction) + 1,
            ]
        )
    return out


def wire_entry(spec: Checkpoint, fact: Fact, cell: str) -> list[int]:
    score, confidence, deviation = fact
    direction = 0 if deviation is None else spec.deviations.index(deviation) + 1
    return [score, CODES[cell], round(confidence * 100), direction]


def positional(
    seen: dict[CheckpointId, Fact],
    metrics: dict[str, Any],
    coach: dict[str, Any],
    cells: dict[CheckpointId, str] | None = None,
) -> dict[str, Any]:
    """The vx12 reply stating exactly the facts `compact` states."""

    cited = cells or {}
    checkpoints: dict[str, list[Any]] = {}
    for phase, specs in vx.WIRE_CHECKPOINTS.items():
        entries: list[Any] = []
        for spec in specs:
            fact = seen.get(spec.checkpoint_id)
            cell = cited.get(spec.checkpoint_id, home_cell(spec))
            entries.append(
                None if fact is None or cell is None else wire_entry(spec, fact, cell)
            )
        checkpoints[phase.value] = entries
    return {
        "frames": [phase_code(CELL_PHASES.get(cell)) for cell in BODY],
        "metrics": wire_metrics(metrics),
        "checkpoints": checkpoints,
        "coach": dict(coach),
    }


def named(
    seen: dict[CheckpointId, Fact],
    metrics: dict[str, Any],
    coach: dict[str, Any],
    cells: dict[CheckpointId, str] | None = None,
) -> dict[str, Any]:
    """`compact`, with the same per-checkpoint cell overrides."""

    reply = compact(seen, metrics, coach)
    for checkpoint, cell in (cells or {}).items():
        entry = reply["checkpoints"][checkpoint.value]
        if entry is not None:
            entry["cell"] = cell
    return reply


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


def setup_entry(entry: Any, position: int = 0) -> dict[str, Any]:
    """A reply whose only assessment is `entry` at setup `position`."""

    setup: list[Any] = [None] * len(vx.WIRE_CHECKPOINTS[P.SETUP])
    setup[position] = entry
    return {
        "frames": [phase_code(CELL_PHASES.get(cell)) for cell in BODY],
        "metrics": [None] * len(VX_METRICS),
        "checkpoints": {
            phase.value: setup if phase is P.SETUP else [None] * len(specs)
            for phase, specs in vx.WIRE_CHECKPOINTS.items()
        },
        "coach": dict(COACH),
    }


SETUP_FIRST = vx.WIRE_CHECKPOINTS[P.SETUP][0]
SETUP_CELL = CODES[str(home_cell(SETUP_FIRST))]


def refusal(reply: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    items, rejected = checkpoints_of(reply)
    assert items == ()
    return rejected


# --- the wire itself --------------------------------------------------------------


class TestTheWire:
    def test_every_scoreable_checkpoint_has_exactly_one_position(self) -> None:
        listed = [s for specs in vx.WIRE_CHECKPOINTS.values() for s in specs]
        assert sorted(s.checkpoint_id for s in listed) == sorted(
            s.checkpoint_id for s in SCOREABLE
        )
        for phase, specs in vx.WIRE_CHECKPOINTS.items():
            assert all(spec.phase is phase for spec in specs)
        assert [p for p in vx.WIRE_CHECKPOINTS] == list(PHASE_ORDER)

    def test_cell_codes_name_the_camera_and_moment_and_never_collide(self) -> None:
        assert CODES["DTL_P1"] == 11
        assert CODES["DTL_P8"] == 18
        assert CODES["FO_P3"] == 23
        assert CODES["IMPACT"] == 31
        assert len(set(CODES.values())) == len(CODES)
        assert set(CODES) == set(BODY)
        assert all(0 < code <= 100 for code in CODES.values())

    def test_duplicate_and_unpatterned_cells_get_their_own_codes(self) -> None:
        codes = vx.cell_codes(("DTL_P1", "DTL_03", "IMPACT", "DTL_P1"))
        assert codes == {"DTL_P1": 11, "DTL_03": 31, "IMPACT": 32}

    def test_the_schema_is_small_and_names_nothing(self) -> None:
        schema = vx.reply_schema((CueId.FACE_OPEN,), BODY, BODY)
        text = str(schema)
        for spec in SCOREABLE:
            assert spec.checkpoint_id.value not in text
        for spec in VX_METRICS:
            assert f"'{spec.name}'" not in text


# --- parity: the same facts in either shape -------------------------------------


EXTRA_SCENARIOS: dict[str, tuple[Any, ...]] = {
    "refusals of every kind": (
        facts(
            setup_hip_hinge=(150, 0.7, None),
            setup_spine_posture=(70, 0.2, None),
            transition_trail_shoulder=(35, 0.8, Deviation.OUT),
            delivery_hip_depth=(60, 0.8, None),
        ),
        an_open_face_grid(),
        COACH,
        {
            C.TRANSITION_TRAIL_SHOULDER: "FO_P5",
            C.DELIVERY_HIP_DEPTH: "DTL_P1",
        },
    ),
    "a bad metric beside good ones": (
        facts(),
        an_open_face_grid(face_angle=a_reading(80.0, "deg", "open")),
        COACH,
        {},
    ),
}


class TestTheSameFactsReadTheSame:
    @pytest.mark.parametrize("name", list(SCENARIOS))
    def test_frames_checkpoints_and_metrics_parse_identically(self, name: str) -> None:
        for shot in SCENARIOS[name]:
            assert read(positional(*shot)) == read(compact(*shot))

    @pytest.mark.parametrize("name", list(EXTRA_SCENARIOS))
    def test_refusals_keep_their_reasons(self, name: str) -> None:
        seen, metrics, coach, cells = EXTRA_SCENARIOS[name]

        old = read(named(seen, metrics, coach, cells))
        new = read(positional(seen, metrics, coach, cells))

        assert new == old

    def test_the_refusal_scenario_really_refuses(self) -> None:
        seen, metrics, coach, cells = EXTRA_SCENARIOS["refusals of every kind"]
        _, rejected = checkpoints_of(positional(seen, metrics, coach, cells))
        reasons = dict(rejected)
        assert reasons["setup_hip_hinge"] == "score"
        assert reasons["setup_spine_posture"] == "low_confidence"
        assert reasons["transition_trail_shoulder"] in {"view", "view_mismatch"}
        assert reasons["delivery_hip_depth"] in {"phase", "phase_conflict"}

    def test_a_bad_metric_is_rejected_in_both_shapes(self) -> None:
        seen, metrics, coach, cells = EXTRA_SCENARIOS["a bad metric beside good ones"]
        *_, values, rejected = read(positional(seen, metrics, coach, cells))
        assert rejected == ("face_angle",)
        assert values["metrics"]["face_angle"] is None
        assert values["metrics"]["ball_speed"] is not None


class TestTheSameFactsCoachTheSame:
    @pytest.mark.parametrize("name", list(SCENARIOS))
    async def test_vx11_and_vx12_replies_agree_downstream(
        self, tmp_path: Path, name: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        shots = SCENARIOS[name]
        caplog.set_level(logging.INFO, logger="caddiej.coaching.service")
        old = await coach_shots(tmp_path / "vx11", tuple(compact(*s) for s in shots))
        old_logged = assessed(caplog)
        caplog.clear()
        new = await coach_shots(tmp_path / "vx12", tuple(positional(*s) for s in shots))

        assert outcome(new) == outcome(old)
        assert assessed(caplog) == old_logged
        assert [r.status for r in new.results] == ["ok"] * len(shots)
        assert [r.metrics_status for r in new.results] == [
            r.metrics_status for r in old.results
        ]
        for before, after in zip(old.results, new.results, strict=True):
            assert after.metrics is not None and before.metrics is not None
            assert after.metrics.provenance == before.metrics.provenance
            assert after.metrics.rejected == before.metrics.rejected

    async def test_refusals_agree_through_the_service(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        seen, metrics, coach, cells = EXTRA_SCENARIOS["refusals of every kind"]
        caplog.set_level(logging.INFO, logger="caddiej.coaching.service")
        old = await coach_shots(tmp_path / "a", (named(seen, metrics, coach, cells),))
        old_logged = assessed(caplog)
        caplog.clear()
        new = await coach_shots(
            tmp_path / "b", (positional(seen, metrics, coach, cells),)
        )

        assert outcome(new) == outcome(old)
        assert assessed(caplog) == old_logged
        assert old_logged[0]["coach_checkpoints_rejected"] >= 4

    async def test_the_scenarios_really_exercise_the_mechanics(
        self, tmp_path: Path
    ) -> None:
        shoulder = await coach_shots(
            tmp_path / "a",
            tuple(
                positional(*s)
                for s in SCENARIOS["a trail shoulder out is the root cause"]
            ),
        )
        result = shoulder.results[0]
        assert result.review is not None and result.review.root is not None
        assert result.review.root.fault is F.TRAIL_SHOULDER_OUT
        assert result.swing_score is not None and result.swing_score.value is not None
        assert result.prompt_version == vx.PROTEE_PROMPT_VERSION

        wing = await coach_shots(
            tmp_path / "b",
            tuple(
                positional(*s)
                for s in SCENARIOS["a chicken wing, then a better release"]
            ),
        )
        first, second = (event.advice for event in wing.bus.of(AnalysisCompleted))
        assert first.cue is second.cue
        assert second.review is not None and second.review.progress is not None
        assert second.review.progress.delta > 0

    async def test_a_cited_cell_resolves_to_its_original_frame(
        self, tmp_path: Path
    ) -> None:
        reply = positional(
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


def assessed(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    keys = (
        "coach_checkpoints_scored",
        "coach_checkpoints_scores",
        "coach_checkpoints_rejected",
        "coach_checkpoints_rejections",
        "coach_checkpoints_reasons",
    )
    return [
        {key: getattr(record, key) for key in keys}
        for record in caplog.records
        if record.getMessage() == "coach_checkpoints_assessed"
    ]


# --- strictness: what does not decode is refused ---------------------------------


class TestAPhaseListMustBeWhole:
    @pytest.mark.parametrize("change", [-1, +1])
    def test_a_short_or_long_phase_list_refuses_every_position_in_it(
        self, change: int
    ) -> None:
        reply = positional(facts(), an_open_face_grid(), COACH)
        setup = reply["checkpoints"]["setup"]
        reply["checkpoints"]["setup"] = (
            setup[:-1] if change < 0 else [*setup, [80, SETUP_CELL, 80, 0]]
        )

        items, rejected = checkpoints_of(reply)

        refused = {key for key, reason in rejected if reason == "length"}
        assert refused == {
            spec.checkpoint_id.value for spec in vx.WIRE_CHECKPOINTS[P.SETUP]
        }
        assert not any(item.phase is P.SETUP for item in items)
        assert any(item.phase is P.TRANSITION for item in items), "others unharmed"

    def test_a_missing_phase_is_refused_not_skipped(self) -> None:
        reply = positional(facts(), an_open_face_grid(), COACH)
        del reply["checkpoints"]["finish"]

        _, rejected = checkpoints_of(reply)

        assert {k for k, r in rejected if r == "length"} == {
            spec.checkpoint_id.value for spec in vx.WIRE_CHECKPOINTS[P.FINISH]
        }

    def test_a_phase_list_that_is_not_a_list_is_refused(self) -> None:
        reply = positional(facts(), an_open_face_grid(), COACH)
        reply["checkpoints"]["finish"] = {"finish_balance": [80, 11, 80, 0]}

        _, rejected = checkpoints_of(reply)

        assert ("finish_balance", "length") in rejected

    def test_an_unknown_phase_key_is_refused_as_unknown(self) -> None:
        reply = positional(facts(), an_open_face_grid(), COACH)
        reply["checkpoints"]["swing_score"] = [99]

        _, rejected = checkpoints_of(reply)

        assert ("swing_score", "unknown") in rejected


class TestAnEntryMustBeFourWholeNumbers:
    @pytest.mark.parametrize(
        ("entry", "why"),
        [
            ([80, 11, 80], "too short"),
            ([80, 11, 80, 0, 0], "too long"),
            ([], "empty"),
            ([80.0, 11, 80, 0], "a float score"),
            ([80, "DTL_P1", 80, 0], "a cell id, not a code"),
            ([80, 11, 0.8, 0], "a fraction, not a percent"),
            ([80, 11, 80, None], "a null direction"),
            ([True, 11, 80, 0], "a boolean"),
            ({"score": 80}, "an object"),
            ("80,11,80,0", "a string"),
        ],
    )
    def test_a_misshapen_entry_is_refused_as_shape(self, entry: Any, why: str) -> None:
        assert refusal(setup_entry(entry)) == (
            (SETUP_FIRST.checkpoint_id.value, "shape"),
        ), why

    def test_a_refused_sentinel_from_the_model_is_not_a_reason_it_may_choose(
        self,
    ) -> None:
        """Only the decoder writes `refused:`; a vx11 entry is a mapping."""

        reply = compact(facts(), an_open_face_grid(), COACH)
        reply["checkpoints"][SETUP_FIRST.checkpoint_id.value] = "refused:shape"
        _, rejected = vx.parse_checkpoints(
            vx.canonical_reply(reply, {})["checkpoints"],
            body_frames=BODY,
            frame_phases=SEEN,
            frame_views=VIEWS,
        )
        assert (SETUP_FIRST.checkpoint_id.value, "shape") in rejected


class TestEveryCodeIsChecked:
    @pytest.mark.parametrize(
        ("entry", "reason"),
        [
            ([101, SETUP_CELL, 80, 0], "score"),
            ([-1, SETUP_CELL, 80, 0], "score"),
            ([80, SETUP_CELL, 101, 0], "confidence"),
            ([80, SETUP_CELL, -5, 0], "confidence"),
            ([80, SETUP_CELL, 10, 0], "low_confidence"),
            ([80, 99, 80, 0], "frame"),
            ([80, 0, 80, 0], "frame"),
            ([80, 19, 80, 0], "frame"),
        ],
    )
    def test_an_out_of_range_code_is_refused_for_its_reason(
        self, entry: list[int], reason: str
    ) -> None:
        assert refusal(setup_entry(entry)) == (
            (SETUP_FIRST.checkpoint_id.value, reason),
        )

    def test_a_direction_code_the_checkpoint_does_not_have_is_refused(self) -> None:
        position, spec = next(
            (i, s) for i, s in enumerate(vx.WIRE_CHECKPOINTS[P.SETUP]) if s.deviations
        )
        cell = CODES[str(home_cell(spec))]
        too_high = [70, cell, 80, len(spec.deviations) + 1]

        assert refusal(setup_entry(too_high, position)) == (
            (spec.checkpoint_id.value, "deviation"),
        )
        assert refusal(setup_entry([70, cell, 80, -1], position)) == (
            (spec.checkpoint_id.value, "deviation"),
        )

    def test_a_direction_on_a_checkpoint_with_none_is_refused(self) -> None:
        spec = next(
            s
            for specs in vx.WIRE_CHECKPOINTS.values()
            for s in specs
            if not s.deviations
        )
        phase_specs = vx.WIRE_CHECKPOINTS[spec.phase]
        position = phase_specs.index(spec)
        reply = positional({}, {}, COACH)
        reply["checkpoints"][spec.phase.value][position] = [
            70,
            CODES[str(home_cell(spec))],
            80,
            1,
        ]

        assert refusal(reply) == ((spec.checkpoint_id.value, "deviation"),)

    def test_each_direction_code_names_its_own_direction(self) -> None:
        position, spec = next(
            (i, s) for i, s in enumerate(vx.WIRE_CHECKPOINTS[P.SETUP]) if s.deviations
        )
        cell = CODES[str(home_cell(spec))]
        for code, deviation in enumerate(spec.deviations, start=1):
            (item,), rejected = checkpoints_of(
                setup_entry([60, cell, 80, code], position)
            )
            assert rejected == ()
            assert item.deviation is deviation

    def test_confidence_is_a_percent_capped_like_any_other(self) -> None:
        (item,), _ = checkpoints_of(setup_entry([80, SETUP_CELL, 83, 0]))
        assert item.confidence == 0.85
        (item,), _ = checkpoints_of(setup_entry([80, SETUP_CELL, 100, 0]))
        assert item.confidence == 0.9

    def test_the_score_is_not_quantised(self) -> None:
        (item,), _ = checkpoints_of(setup_entry([73, SETUP_CELL, 80, 0]))
        assert item.score == 73

    def test_a_null_position_is_simply_not_assessed(self) -> None:
        items, rejected = checkpoints_of(positional({}, {}, COACH))
        assert items == ()
        assert rejected == ()


class TestTheCellsPhaseAndViewStillDecide:
    def test_a_cell_coded_unclear_gives_phase_unknown(self) -> None:
        reply = setup_entry([80, SETUP_CELL, 80, 0])
        reply["frames"][BODY.index(str(home_cell(SETUP_FIRST)))] = 0

        assert refusal(reply) == ((SETUP_FIRST.checkpoint_id.value, "phase_unknown"),)

    @pytest.mark.parametrize("code", [10, -1, 99])
    def test_a_phase_code_out_of_range_is_unclear_not_a_phase(self, code: int) -> None:
        reply = setup_entry([80, SETUP_CELL, 80, 0])
        reply["frames"][BODY.index(str(home_cell(SETUP_FIRST)))] = code

        assert refusal(reply) == ((SETUP_FIRST.checkpoint_id.value, "phase_unknown"),)

    @pytest.mark.parametrize("change", [-1, +1])
    def test_a_frames_list_of_the_wrong_length_places_no_picture(
        self, change: int
    ) -> None:
        reply = positional(facts(), an_open_face_grid(), COACH)
        frames = reply["frames"]
        reply["frames"] = frames[:-1] if change < 0 else [*frames, 1]

        frames_read, items, rejected, *_ = read(reply)

        assert frames_read == {}
        assert items == ()
        assert {reason for _, reason in rejected} == {"phase_unknown"}

    def test_the_model_saying_a_cell_shows_another_moment_wins(self) -> None:
        reply = setup_entry([80, SETUP_CELL, 80, 0])
        reply["frames"][BODY.index(str(home_cell(SETUP_FIRST)))] = phase_code(P.FINISH)

        (reason,) = [r for _, r in refusal(reply)]
        assert reason in {"phase", "phase_conflict"}

    def test_a_cell_from_the_wrong_camera_is_refused_as_view(self) -> None:
        spec = next(s for s in vx.WIRE_CHECKPOINTS[P.SETUP] if len(s.views) == 1)
        position = vx.WIRE_CHECKPOINTS[P.SETUP].index(spec)
        home = str(home_cell(spec))
        other = "FO_P1" if home.startswith("DTL_") else "DTL_P1"

        assert refusal(setup_entry([80, CODES[other], 80, 0], position)) == (
            (spec.checkpoint_id.value, "view"),
        )

    def test_each_frame_code_is_the_phase_in_order(self) -> None:
        for code, phase in enumerate(PHASE_ORDER, start=1):
            reply = setup_entry(None)
            reply["frames"] = [code] * len(BODY)
            frames, *_ = read(reply)
            assert set(frames.values()) == {phase}


class TestMetricsByPosition:
    def test_both_shapes_of_the_same_grid_read_the_same(self) -> None:
        grid = the_94_shot()
        old = vx.parse_metrics(compact_metrics(grid))
        new = vx.parse_metrics(
            vx.decode_wire({"metrics": wire_metrics(grid)}, BODY)["metrics"]
        )
        assert new.as_dict() == old.as_dict()
        assert new.available == old.available
        assert new.provenance == old.provenance

    @pytest.mark.parametrize("change", [-1, +1])
    def test_a_metrics_list_of_the_wrong_length_reads_nothing(
        self, change: int
    ) -> None:
        wire = wire_metrics(the_94_shot())
        wire = wire[:-1] if change < 0 else [*wire, None]

        metrics = vx.parse_metrics(vx.decode_wire({"metrics": wire}, BODY)["metrics"])

        assert metrics.available == ()
        assert set(metrics.rejected) == {spec.name for spec in VX_METRICS}

    def test_null_is_unreadable_and_is_not_a_rejection(self) -> None:
        metrics = vx.parse_metrics(
            vx.decode_wire({"metrics": [None] * len(VX_METRICS)}, BODY)["metrics"]
        )
        assert metrics.available == ()
        assert metrics.rejected == ()

    @pytest.mark.parametrize(
        ("entry", "why"),
        [
            ([148.0, 1], "too short"),
            ([148.0, 1, 0, 0], "too long"),
            (["148", 1, 0], "a string value"),
            ([148.0, 0, 0], "no unit"),
            ([148.0, 9, 0], "a unit it does not have"),
            ([148.0, 1.0, 0], "a float unit code"),
            ([148.0, 1, 1], "a direction ball speed does not have"),
            ([9999.0, 1, 0], "implausible"),
            ({"value": 148.0}, "an object"),
        ],
    )
    def test_a_malformed_position_is_rejected(self, entry: Any, why: str) -> None:
        wire: list[Any] = [None] * len(VX_METRICS)
        position = next(
            i for i, spec in enumerate(VX_METRICS) if spec.name == "ball_speed"
        )
        wire[position] = entry

        metrics = vx.parse_metrics(vx.decode_wire({"metrics": wire}, BODY)["metrics"])

        assert metrics.get("ball_speed") is None, why
        assert metrics.rejected == ("ball_speed",), why

    def test_a_direction_code_out_of_range_is_rejected(self) -> None:
        spec = VX_METRICS_BY_NAME["face_angle"]
        wire: list[Any] = [None] * len(VX_METRICS)
        wire[VX_METRICS.index(spec)] = [2.0, 1, len(spec.directions) + 1]

        metrics = vx.parse_metrics(vx.decode_wire({"metrics": wire}, BODY)["metrics"])

        assert metrics.rejected == ("face_angle",)

    def test_each_position_is_its_own_metric(self) -> None:
        for position, spec in enumerate(VX_METRICS):
            wire: list[Any] = [None] * len(VX_METRICS)
            value = min(spec.limit / 2, 1.0)
            wire[position] = [value, 1, 1 if spec.directions else 0]
            metrics = vx.parse_metrics(
                vx.decode_wire({"metrics": wire}, BODY)["metrics"]
            )
            assert metrics.available == (spec.name,), spec.name


class TestOlderRepliesStillRead:
    def test_a_vx11_reply_passes_through_the_decoder_untouched(self) -> None:
        reply = compact(facts(), an_open_face_grid(), COACH)
        assert vx.decode_wire(reply, BODY) == reply

    async def test_a_vx11_reply_still_coaches_through_the_service(
        self, tmp_path: Path
    ) -> None:
        shot = SCENARIOS["a trail shoulder out is the root cause"][0]
        coach = await coach_shots(tmp_path, (compact(*shot),))
        review = coach.results[0].review
        assert review is not None and review.root is not None
        assert review.root.fault is F.TRAIL_SHOULDER_OUT
