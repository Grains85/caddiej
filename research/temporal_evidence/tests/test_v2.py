"""Temporal Evidence V2 (v2_order, v2_request, v2_offline): deterministic tests, no call.

    .venv\\Scripts\\python.exe -m pytest research\\temporal_evidence\\tests\\test_v2.py -q

Synthetic sheets pin the strict-order rule; the five frozen fresh shots, when on
disk, pin the exact failures of the frozen validation and that synchronization,
the sheet and P7 are untouched.
"""

from __future__ import annotations

import json
import random
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import fresh_run as fr  # noqa: E402
import hybrid_replay as hy  # noqa: E402
import t36  # noqa: E402
import t36_request as req  # noqa: E402
import v2_grade as grade  # noqa: E402
import v2_offline as off  # noqa: E402
import v2_order as v2  # noqa: E402
import v2_request as rq  # noqa: E402
import vx14c_replay as c  # noqa: E402

POS = hy.POS
P = {name: i for i, name in enumerate(POS)}
FROZEN_PRIOR = (1.7277, 2.2277)
# T36S-like replay times: wide gaps before the impact window, every tick inside it.
TIMES = (
    0.0,
    0.2,
    0.4,
    0.6,
    0.9,
    1.1,
    1.3,
    1.55,
    1.75,
    1.8,
    1.85,
    1.9,
    1.95,
    2.0,
    2.05,
    2.1,
    2.3,
    2.5,
)
GOOD = {
    "P1": 1,
    "P2": 3,
    "P3": 5,
    "P4": 8,
    "P5": 11,
    "P6": 12,
    "P7": 14,
    "P8": 16,
    "P9": 18,
}

have_fresh = pytest.mark.skipif(
    not all((fr.FRESH / "runs" / n / "reply.json").exists() for n in off.SHOTS),
    reason="frozen fresh runs not on disk",
)


@pytest.fixture(autouse=True)
def frozen_prior() -> Iterator[None]:
    saved = hy.R7
    hy.R7 = FROZEN_PRIOR
    yield
    hy.R7 = saved


def _cams() -> dict:
    rows = []
    for camera_id, prefix, view in (
        ("swing_behind", "DTL", "behind"),
        ("swing_side", "FO", "side"),
    ):
        for n, t in enumerate(TIMES, start=1):
            rows.append(
                {
                    "cell_id": f"{prefix}_{n:02d}",
                    "camera_id": camera_id,
                    "view": view,
                    "frame_id": f"{camera_id}_c{n:02d}",
                    "capture_index": 14 + n,
                    "offset_from_replay_start_seconds": t,
                }
            )
    return hy.sheet(rows)


def _groups(**change: int | None) -> dict[str, list[str | None]]:
    numbers = {**GOOD, **change}
    return {
        key: [
            None if numbers[pos] is None else f"{prefix}_{numbers[pos]:02d}"
            for pos in POS
        ]
        for key, prefix in (("dtl", "DTL"), ("fo", "FO"))
    }


def _nulled(groups: dict, kept: dict) -> dict[str, set[str]]:
    return {
        key: {
            POS[p]
            for p in range(9)
            if groups[key][p] is not None and kept[key][p] is None
        }
        for key in groups
    }


def _check(change: dict[str, int | None], nulled: set[str]) -> None:
    groups = _groups(**change)
    kept, _notes = v2.validate_groups(groups, _cams())
    assert _nulled(groups, kept) == {"dtl": nulled, "fo": nulled}
    for key in groups:
        for p in range(9):
            assert kept[key][p] in (None, groups[key][p])


# --- the strict order: the exact failures -------------------------------------------------


def test_a_well_ordered_swing_with_gaps_and_neighbouring_ticks_is_kept_whole() -> None:
    _check({}, set())
    _check(
        {"P5": 12, "P6": 13, "P7": 14, "P8": 15}, set()
    )  # adjacent ticks are natural


def test_p3_and_p4_cannot_share_one_moment() -> None:
    """Shot 23 of the frozen validation: P3 and P4 both on tick 27."""

    groups = _groups(P3=6, P4=6)
    v1_kept, _ = c.validate_groups(groups, _cams())
    assert (
        v1_kept["dtl"][P["P3"]] == v1_kept["dtl"][P["P4"]] == "DTL_06"
    )  # V1 accepted it
    _check({"P3": 6, "P4": 6}, {"P3", "P4"})


def test_p3_must_precede_p4() -> None:
    _check({"P3": 9, "P4": 8}, {"P3", "P4"})


def test_p5_must_follow_p4() -> None:
    _check({"P5": 8}, {"P4", "P5"})
    _check({"P4": 9, "P5": 8}, {"P4", "P5"})


def test_p6_must_follow_p5() -> None:
    _check({"P6": 11}, {"P5", "P6"})
    _check({"P5": 12, "P6": 11}, {"P5", "P6"})


def test_p7_must_follow_p6_and_p7_is_never_the_one_nulled() -> None:
    _check({"P6": 14}, {"P6"})
    _check({"P6": 15}, {"P6"})


def test_p8_must_follow_p7_and_p7_is_never_the_one_nulled() -> None:
    _check({"P8": 14}, {"P8"})
    _check({"P8": 13}, {"P8"})


@pytest.mark.parametrize("first", range(8), ids=lambda i: f"{POS[i]}-{POS[i + 1]}")
def test_every_neighbouring_pair_sharing_a_cell_is_refused(first: int) -> None:
    a, b = POS[first], POS[first + 1]
    shared = GOOD["P7"] if "P7" in (a, b) else GOOD[b]
    _check({a: shared, b: shared}, {a, b} - {"P7"})


def test_a_position_given_zero_is_left_out_of_the_rule() -> None:
    _check({"P6": None}, set())
    _check({"P3": None, "P4": None}, set())
    _check({"P5": None, "P6": 7}, {"P4", "P6"})  # the rule still spans the gap


def test_p7_outside_the_impact_prior_is_still_nulled_and_is_no_anchor() -> None:
    groups = _groups(P7=6, P6=14)  # P7 at 1.1 s: outside the frozen prior
    kept, notes = v2.validate_groups(groups, _cams())
    assert kept["dtl"][P["P7"]] is None and kept["fo"][P["P7"]] is None
    assert any("outside impact prior" in n for n in notes)


def test_never_moves_fills_or_invents_a_cell_and_keeps_strict_order() -> None:
    rng = random.Random(20261008)
    cams = _cams()
    for _ in range(400):
        nums = {pos: rng.choice([None, *range(1, 19)]) for pos in POS}
        groups = _groups(**nums)
        ordered, _ = v2.strict_order(groups, cams)
        kept, _ = v2.validate_groups(groups, cams)
        for key in groups:
            assert all(k in (None, g) for k, g in zip(kept[key], groups[key]))
            times = [cams[key]["t"][k] for k in kept[key] if k is not None]
            assert times == sorted(set(times))
            p7 = groups[key][P["P7"]]
            if p7 is not None and hy.side_ok(P["P7"], cams[key]["t"][p7]):
                assert ordered[key][P["P7"]] == p7


# --- the request ----------------------------------------------------------------------------


def _request(builder=rq.build):
    rows = []
    for camera_id, prefix, view in (
        ("swing_behind", "DTL", "behind"),
        ("swing_side", "FO", "side"),
    ):
        for n, t in enumerate(TIMES, start=1):
            rows.append(
                {
                    "cell_id": f"{prefix}_{n:02d}",
                    "camera_id": camera_id,
                    "view": view,
                    "frame_id": f"{camera_id}_c{14 + n:02d}",
                    "capture_index": 14 + n,
                    "offset_from_replay_start_seconds": t,
                }
            )
    rows += [
        {"cell_id": "IMPACT", "frame_id": "impact_01", "file": "impact.jpg"},
        {"cell_id": "ALL_VALUES", "frame_id": "all_values", "file": "all_values.jpg"},
    ]
    return builder(
        rows,
        [],
        b"\xff\xd8sheet",
        max_output_tokens=10000,
        timeout_seconds=30.0,
        reasoning_effort="minimal",
    )


def test_p3_backswing_and_p5_downswing_lead_arm_are_disambiguated() -> None:
    r = _request()
    assert "P3 = lead-arm-parallel BACKSWING." in r.system
    assert "P5 = lead-arm-parallel DOWNSWING." in r.system
    assert (
        "P3 backswing = the LEAD ARM parallel in the BACKSWING: before P4" in r.system
    )
    assert "P5 = the LEAD ARM parallel in the DOWNSWING: after P4" in r.system
    assert 'position\'s key is "transition"' in r.system
    assert "does NOT\nmean the top or the direction-change moment" in r.system
    assert "P5 transition" not in r.system
    assert "P3 comes before P4, P5 after it." in r.system
    phases = r.schema["properties"]["phases"]["properties"]
    assert "BACKSWING" in phases["backswing"]["description"]
    assert "Legacy key name" in phases["transition"]["description"]
    assert "DOWNSWING" in phases["transition"]["description"]


def test_p5_lead_arm_and_p6_shaft_are_disambiguated() -> None:
    r = _request()
    assert "P4 = TOP / direction change." in r.system
    assert "P6 = shaft-parallel DOWNSWING / delivery." in r.system
    assert "P7 = club-ball CONTACT." in r.system
    assert "P6 = the SHAFT parallel in the DOWNSWING" in r.system
    assert "P5 is\njudged by the LEAD ARM, P6 by the SHAFT." in r.system
    assert "never the P5\ncell, never the P7 cell." in r.system
    assert 'key is "downswing"' in r.system
    assert (
        "shaft" in rq.DESCRIPTIONS["downswing"]
        and "lead arm" in rq.DESCRIPTIONS["downswing"]
    )
    assert "transition (" in r.evidence
    assert r.evidence.count(rq.LEGACY_KEYS) == 1


def test_order_is_strict_in_the_request_and_sharing_is_no_longer_allowed() -> None:
    v1, r = _request(req.build), _request()
    sharing = "One cell may\nstand for two neighbouring positions"
    assert sharing in v1.system and sharing not in r.system
    assert "P1 < P2 < P3 < P4\n< P5 < P6 < P7 < P8 < P9" in r.system
    assert "Never give one cell to two positions" in r.system


def test_only_the_targeted_passages_change() -> None:
    v1, r = _request(req.build), _request()
    back = r.system.replace(rq._P3_P6_V2, rq._P3_P6_V1).replace(
        rq._ORDER_V2, rq._ORDER_V1
    )
    assert back == v1.system
    evidence = r.evidence.replace(rq.LEGACY_KEYS, "")
    for _key, _camera, old, new in rq.P5_CHECKPOINTS:
        evidence = evidence.replace(new, old)
    assert evidence == v1.evidence
    schema = json.loads(json.dumps(r.schema))
    for key, text in rq.DESCRIPTIONS.items():
        assert (
            schema["properties"]["phases"]["properties"][key].pop("description") == text
        )
    assert schema == v1.schema
    assert fr.schema_shape(schema) == fr.schema_shape(v1.schema)
    assert (r.max_output_tokens, r.timeout_seconds, r.reasoning_effort, r.images) == (
        v1.max_output_tokens,
        v1.timeout_seconds,
        v1.reasoning_effort,
        v1.images,
    )
    assert rq.problems(r) == []


def _group(evidence: str, phase: str) -> list[str]:
    start = evidence.index(f"\n{phase} (") + 1
    lines = evidence[start:].splitlines()[1:]
    out = []
    for line in lines:
        if not line[:1].isdigit():
            break
        out.append(line)
    return out


# Words that time an observable at another position than the group's own.
_FOREIGN_TIMING = {
    "backswing": ("at the top", "coming down", "downswing", "impact", "after the ball"),
    "top": ("coming down", "downswing starts", "after the ball", "at the strike"),
    "transition": (
        "start of the downswing",
        "as the downswing starts",
        "finish going back",
        "in transition",
        "starting down",
        "at the top",
        "after the ball",
    ),
    "downswing": ("at the top", "going back", "finish going back", "after the ball"),
    "impact": ("at the top", "going back", "after the ball", "finish"),
}


def test_every_p5_checkpoint_is_read_in_the_p5_cell() -> None:
    lines = _group(_request().evidence, "transition")
    assert len(lines) == len(rq.P5_CHECKPOINTS) == 7
    for line in lines:
        assert "in the P5 cell" in line, line
        for phrase in _FOREIGN_TIMING["transition"]:
            assert phrase not in line, (phrase, line)


def test_p5_rewording_keeps_ids_cameras_order_club_flags_and_directions() -> None:
    v1 = _group(_request(req.build).evidence, "transition")
    v2_lines = _group(_request().evidence, "transition")

    def frame(line: str) -> tuple[str, str, str]:
        head, rest = line.split(" - ", 1)
        club = "Null unless the whole club is visible." in rest
        return head, str(club), rest.rsplit(" dir ", 1)[1]

    assert [frame(x) for x in v2_lines] == [frame(x) for x in v1]
    assert [k for k, *_ in rq.P5_CHECKPOINTS] == [x.split()[1] for x in v1]


@pytest.mark.parametrize("phase", ["backswing", "top", "downswing", "impact"])
def test_p3_p4_p6_p7_checkpoints_carry_no_foreign_timing_and_are_unchanged(
    phase: str,
) -> None:
    v1 = _group(_request(req.build).evidence, phase)
    lines = _group(_request().evidence, phase)
    assert lines == v1
    for line in lines:
        for phrase in _FOREIGN_TIMING[phase]:
            assert phrase not in line.split(" - ", 1)[1], (phrase, line)


def test_the_p5_override_refuses_drifted_production_wording() -> None:
    text = _request(req.build).evidence.replace(
        "as the arms finish going back", "as the arms go back"
    )
    with pytest.raises(RuntimeError, match="transition_pressure_shift"):
        rq.evidence_v2(text)


def test_p1_p2_p7_p8_p9_text_is_unchanged() -> None:
    v1, r = _request(req.build), _request()
    head = v1.system[: v1.system.index("P3 backswing")]
    assert r.system.startswith(head)
    tail = v1.system[
        v1.system.index("P7 impact:") : v1.system.index("Along one camera")
    ]
    assert tail in r.system and "Search hint only" in tail


# --- the five frozen shots -----------------------------------------------------------------------


@have_fresh
def test_frozen_shot_23_p3_p4_shared_tick_is_now_refused_and_nothing_else_changes() -> (
    None
):
    for name in off.SHOTS:
        a = off.replay(name, c.validate_groups)
        b = off.replay(name, v2.validate_groups)
        diff = {
            (key, POS[p])
            for key in ("dtl", "fo")
            for p in range(9)
            if a["kept"][key][p] != b["kept"][key][p]
        }
        if name.startswith("20261008T142305-23"):
            assert a["groups"]["dtl"][P["P3"]] == a["groups"]["dtl"][P["P4"]]
            assert diff == {(k, x) for k in ("dtl", "fo") for x in ("P3", "P4")}
            assert all(b["kept"][k][P[x]] is None for k, x in diff)
        else:
            assert diff == set()


@have_fresh
def test_offline_replay_reproduces_the_frozen_m4_and_v2_moves_it_only_slightly() -> (
    None
):
    frozen = fr._json(fr.FRESH / "evaluation.json")["metrics"][
        "M4_wrong_position_accepts"
    ]
    v1 = off.m4(off.SHOTS, c.validate_groups)
    v2m = off.m4(off.SHOTS, v2.validate_groups)
    assert f"{v1['wrong']}/{v1['accepted']}" == frozen[2] == "75/268"
    assert (v2m["wrong"], v2m["accepted"]) == (68, 254)


@have_fresh
def test_a_correct_answer_on_every_frozen_sheet_passes_v2() -> None:
    """The by-eye best cells for P3-P6 (with the frozen P1, P2, P7-P9) are never refused."""

    for name in off.SHOTS:
        base = off.replay(name, c.validate_groups)
        run = off._load(name)
        cell = {m["tick"]: m["number"] for m in run["meta"]["sheet"]["moments"]}
        rows = {r["position"]: r for r in off.diagnosis((name,))}
        groups = {k: list(v) for k, v in base["groups"].items()}
        for pos in ("P3", "P4", "P5", "P6"):
            n = cell[rows[pos]["best_on_sheet"][0]]
            for key, prefix in (("dtl", "DTL"), ("fo", "FO")):
                groups[key][P[pos]] = f"{prefix}_{n:02d}"
        cams = hy.sheet(run["rows"])
        kept, notes = v2.validate_groups(groups, cams)
        assert not [n for n in notes if "order" in n], (name, notes)
        for key in ("dtl", "fo"):
            for pos in ("P3", "P4", "P5", "P6", "P7"):
                assert kept[key][P[pos]] == groups[key][P[pos]], (name, key, pos)


@have_fresh
def test_good_p3_p6_candidates_were_on_every_frozen_sheet() -> None:
    table = off.diagnosis()
    assert len(table) == 20 and all(r["candidate_on_sheet"] for r in table)
    assert all(r["same_on_both_cameras"] for r in table)


@have_fresh
def test_synchronization_and_sheet_are_unchanged() -> None:
    config = fr._json(fr.FRESH / "config.json")
    assert fr.fingerprints() == config["fingerprints"]
    for name in off.SHOTS:
        meta = fr._json(fr.FRESH / "shots" / name / "metadata.json")
        frozen = fr._json(fr.FRESH / "runs" / name / "request_meta.json")["sheet"][
            "moments"
        ]
        moments = t36.plan(meta)
        assert [(m.number, m.tick) for m in moments] == [
            (m["number"], m["tick"]) for m in frozen
        ]
        rows = t36.provenance(meta, moments)
        assert json.loads(json.dumps(rows, default=str)) == fr._json(
            fr.FRESH / "runs" / name / "cells.json"
        )
        by = {(r["camera_id"], r["moment"]): r for r in rows if "moment" in r}
        for m in moments:
            d, f = by[("swing_behind", m.number)], by[("swing_side", m.number)]
            if d["available"] and f["available"]:
                assert d["capture_index"] == f["capture_index"] == m.tick


@have_fresh
def test_p7_behaviour_is_unchanged_on_the_frozen_replies() -> None:
    for name in off.SHOTS:
        a = off.replay(name, c.validate_groups)
        b = off.replay(name, v2.validate_groups)
        for key in ("dtl", "fo"):
            assert b["groups"][key][P["P7"]] == a["groups"][key][P["P7"]]
            assert b["kept"][key][P["P7"]] == a["kept"][key][P["P7"]] is not None
    v1_request = (fr.FRESH / "runs" / off.SHOTS[0] / "request_system.txt").read_text(
        encoding="utf-8"
    )
    p7 = v1_request[v1_request.index("P7 impact:") : v1_request.index("P8 release:")]
    assert p7 in rq.system_v2()


@have_fresh
def test_frozen_material_is_untouched() -> None:
    """Replies are still the ones in the ledger; grades and evaluation are as frozen."""

    ledger = [
        json.loads(x)
        for x in (fr.FRESH / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if x
    ]
    for name in off.SHOTS:
        (entry,) = [x for x in ledger if x["shot"] == name]
        assert entry["reply_sha256"] == fr.sha256(
            (fr.FRESH / "runs" / name / "reply.json").read_bytes()
        )
    evaluation = fr._json(fr.FRESH / "evaluation.json")
    assert evaluation["pass"] is False
    assert evaluation["metrics"]["M3_P7_within_one_tick"][0] == "10/10"
    shot_23 = fr._json(fr.FRESH / "runs" / off.SHOTS[-1] / "grades.json")
    assert shot_23["contact_tick"] == {"dtl": 44, "fo": 44}


def test_the_v1_contact_audit_note_records_the_post_hoc_finding_only() -> None:
    note = (Path(__file__).parents[1] / "V1_CONTACT_AUDIT.md").read_text(
        encoding="utf-8"
    )
    assert "20261008T142305-23-3d49f492" in note
    assert "contact_tick=44" in note and "identifies tick 42" in note
    assert "M3 result 8/10" in note and "remain unchanged" in note
    grades = fr._json(fr.FRESH / "runs" / "20261008T142305-23-3d49f492" / "grades.json")
    assert grades["contact_tick"] == {"dtl": 44, "fo": 44}


def test_contact_rule_requires_the_ball_still_on_the_mat() -> None:
    assert grade.CONTACT_RULE.startswith("contact_tick is the capture tick")
    assert "still on/at the mat" in grade.CONTACT_RULE
    assert "must not be labelled contact" in grade.CONTACT_RULE
    assert grade.is_contact(clubhead_at_ball=True, ball_still_on_mat=True)
    assert not grade.is_contact(clubhead_at_ball=True, ball_still_on_mat=False)
    assert not grade.is_contact(clubhead_at_ball=False, ball_still_on_mat=True)


def test_p6_between_frames_is_acceptable_only_when_the_shaft_supports_it() -> None:
    assert "shaft-parallel-to-ground" in grade.P6_RULE
    assert "merely because it is temporally near P6" in grade.P6_RULE
    assert (
        grade.p6_verdict(shaft="horizontal", between_p5_and_p7=True, nearest=True)
        == "correct"
    )
    assert (
        grade.p6_verdict(shaft="approaching", between_p5_and_p7=True, nearest=True)
        == "acceptable"
    )
    assert (
        grade.p6_verdict(shaft="just_passed", between_p5_and_p7=True, nearest=True)
        == "acceptable"
    )
    # The nearest frame in time, with the shaft nowhere near horizontal.
    assert (
        grade.p6_verdict(shaft="other", between_p5_and_p7=True, nearest=True) == "wrong"
    )
    # A horizontal shaft that is not between P5 and P7 is a different position.
    assert (
        grade.p6_verdict(shaft="approaching", between_p5_and_p7=False, nearest=True)
        == "wrong"
    )
    assert (
        grade.p6_verdict(shaft="just_passed", between_p5_and_p7=True, nearest=False)
        == "wrong"
    )


def test_a_model_tick_is_graded_against_pre_recorded_labels() -> None:
    assert grade.grade_pick(24, [24], [24, 25], [24]) == ("correct", None)
    assert grade.grade_pick(25, [24], [24, 25], [24, 25]) == ("acceptable", None)
    assert grade.grade_pick(28, [24], [24, 25], [24]) == ("wrong", None)
    assert grade.grade_pick(None, [24], [24], [24]) == ("unavailable", True)
    assert grade.grade_pick(None, [24], [24], [30]) == ("unavailable", False)
