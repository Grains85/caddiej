"""Deterministic primary-fault selection. No model and no images."""

from __future__ import annotations

import fault_catalog as catalog
import primary_selection as selection


def test_direct_upstream_relationship_can_select_the_cause() -> None:
    result = selection.select_primary(
        [
            ("OUT_TO_IN_DELIVERY", 0.84),
            ("HAND_PATH_OUT", 0.88),
        ]
    )
    assert result.fault_id == "HAND_PATH_OUT"
    assert result.reason == "upstream_cause"
    explained = {
        row.fault_id: row.explained_by
        for row in result.ranked
        if row.disposition == "explained"
    }
    assert explained["OUT_TO_IN_DELIVERY"] == "HAND_PATH_OUT"
    confidences = {row.fault_id: row.confidence for row in result.ranked}
    assert confidences == {"HAND_PATH_OUT": 0.88, "OUT_TO_IN_DELIVERY": 0.84}


def test_unreturned_intermediate_fault_is_not_a_bridge() -> None:
    """Trail shoulder is upstream of hand path, which is upstream of out-to-in.

    The hand-path fault was not returned, so the shoulder must not explain
    the delivery.
    """

    result = selection.select_primary(
        [
            ("TRAIL_SHOULDER_OUT_EARLY", 0.80),
            ("OUT_TO_IN_DELIVERY", 0.84),
        ]
    )
    by_id = {row.fault_id: row for row in result.ranked}
    assert by_id["OUT_TO_IN_DELIVERY"].explained_by is None
    assert by_id["TRAIL_SHOULDER_OUT_EARLY"].explains == ()
    assert result.reason == "tie_break"
    assert result.fault_id == "OUT_TO_IN_DELIVERY"


def test_every_two_step_chain_ignores_the_unreturned_middle() -> None:
    checked = 0
    faults = catalog.FAULTS_BY_ID
    for middle in faults.values():
        for parent in middle.upstream_candidates:
            children = [
                fault.id
                for fault in catalog.CATALOG
                if middle.id in fault.upstream_candidates
            ]
            for child in children:
                if parent == child or parent in faults[child].upstream_candidates:
                    continue
                result = selection.select_primary([(parent, 0.90), (child, 0.84)])
                by_id = {row.fault_id: row for row in result.ranked}
                assert by_id[child].explained_by is None
                assert by_id[parent].explains == ()
                assert result.reason == "highest_confidence"
                assert result.fault_id == parent
                checked += 1
    assert checked > 0


def test_weak_proven_upstream_does_not_suppress_strong_downstream() -> None:
    result = selection.select_primary(
        [
            ("HAND_PATH_OUT", 0.70),
            ("OUT_TO_IN_DELIVERY", 0.90),
        ]
    )
    assert result.fault_id == "OUT_TO_IN_DELIVERY"
    assert result.reason == "highest_confidence"
    assert all(row.explained_by is None for row in result.ranked)
    assert {row.fault_id: row.confidence for row in result.ranked} == {
        "HAND_PATH_OUT": 0.70,
        "OUT_TO_IN_DELIVERY": 0.90,
    }


def test_weak_earliest_fault_does_not_take_the_lesson() -> None:
    result = selection.select_primary(
        [
            ("BACKSWING_ARMS_LIFT_WITHOUT_CHEST", 0.61),
            ("TRAIL_SHOULDER_OUT_EARLY", 0.86),
            ("HAND_PATH_OUT", 0.84),
        ]
    )
    assert result.fault_id == "TRAIL_SHOULDER_OUT_EARLY"
    assert result.reason == "upstream_cause"


def test_tie_is_deterministic_and_ignores_input_order() -> None:
    forward = [("FINISH_BALANCE_LOST", 0.80), ("CHICKEN_WING", 0.80)]
    backward = list(reversed(forward))
    first = selection.select_primary(forward)
    second = selection.select_primary(backward)
    assert first.fault_id == second.fault_id == "CHICKEN_WING"
    assert first.reason == "tie_break"
    assert first.as_dict() == second.as_dict()


def test_small_confidence_gap_does_not_switch_an_unrelated_lesson() -> None:
    pair = [
        ("SETUP_ARMS_NOT_HANGING", 0.80),
        ("SETUP_WEIGHT_TOWARD_TOES", 0.84),
    ]
    forward = selection.select_primary(pair)
    backward = selection.select_primary(list(reversed(pair)))
    assert forward.fault_id == backward.fault_id == "SETUP_ARMS_NOT_HANGING"
    assert forward.reason == "tie_break"
    assert forward.as_dict() == backward.as_dict()


def test_confidence_outside_the_stable_band_wins() -> None:
    result = selection.select_primary(
        [
            ("SETUP_ARMS_NOT_HANGING", 0.80),
            ("SETUP_WEIGHT_TOWARD_TOES", 0.90),
        ]
    )
    assert result.fault_id == "SETUP_WEIGHT_TOWARD_TOES"
    assert result.reason == "highest_confidence"


def test_exclusive_near_tie_drops_both() -> None:
    result = selection.select_primary(
        [
            ("TAKEAWAY_CLUB_INSIDE", 0.80),
            ("TAKEAWAY_CLUB_OUTSIDE", 0.83),
        ]
    )
    assert result.fault_id is None
    assert result.reason == "no_proven_fault"
    assert {row.disposition for row in result.ranked} == {"contradicted"}


def test_exclusive_gap_keeps_the_stronger_fault() -> None:
    result = selection.select_primary(
        [
            ("TAKEAWAY_CLUB_INSIDE", 0.70),
            ("TAKEAWAY_CLUB_OUTSIDE", 0.90),
        ]
    )
    assert result.fault_id == "TAKEAWAY_CLUB_OUTSIDE"
    assert result.reason == "highest_confidence"


def test_below_threshold_and_empty_input_select_nothing() -> None:
    below = selection.select_primary([("SETUP_WEIGHT_TOWARD_TOES", 0.59)])
    empty = selection.select_primary([])
    assert below.fault_id is None
    assert below.reason == "no_proven_fault"
    assert empty.fault_id is None
    assert empty.reason == "no_proven_fault"


def test_exactly_one_catalog_package_is_selected() -> None:
    result = selection.select_primary(
        [
            ("SETUP_WEIGHT_TOWARD_TOES", 0.91),
            ("SETUP_ARMS_NOT_HANGING", 0.77),
            ("CHICKEN_WING", 0.40),
        ]
    )
    assert result.fault_id == "SETUP_WEIGHT_TOWARD_TOES"
    package = catalog.coaching_package(result.fault_id or "")
    assert package["voice"] == (
        catalog.FAULTS_BY_ID["SETUP_WEIGHT_TOWARD_TOES"].coaching.voice
    )
    assert package["source"] == "fault_catalog"
