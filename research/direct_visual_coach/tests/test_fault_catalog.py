"""Structural checks for the research visual fault catalog."""

from __future__ import annotations

import fault_catalog as catalog


def test_catalog_structure_is_valid() -> None:
    assert catalog.catalog_problems() == []


def test_fault_ids_are_unique() -> None:
    ids = [fault.id for fault in catalog.CATALOG]
    assert len(ids) == len(set(ids))
    assert len(ids) == len(catalog.FAULTS_BY_ID)


def test_every_definition_excludes_the_normal_motion() -> None:
    for fault in catalog.CATALOG:
        assert "not this fault" in fault.visual_definition


def test_every_fault_is_defined_as_an_abnormality() -> None:
    for fault in catalog.CATALOG:
        assert fault.visual_definition.strip()
        assert fault.required_evidence
        assert all(item.strip() for item in fault.required_evidence)
        assert fault.insufficient_evidence
        assert all(item.strip() for item in fault.insufficient_evidence)
        assert fault.allowed_temporal_states
        for state in (
            *fault.allowed_temporal_states,
            *fault.baseline_temporal_states,
            *fault.must_include_states,
        ):
            assert state in catalog.TEMPORAL_STATES
            assert state != "uncertain"


def test_coaching_packages_are_complete_and_local() -> None:
    sent = catalog.classifier_catalog_json()
    for fault in catalog.CATALOG:
        package = catalog.coaching_package(fault.id)
        assert package["source"] == "fault_catalog"
        assert package["voice"] == fault.coaching.voice
        assert package["floor"] == fault.coaching.floor
        assert package["feel"] == fault.coaching.feel
        assert package["drill"] == fault.coaching.drill
        assert package["voice"] not in sent
        assert package["drill"] not in sent
        assert "upstream_candidates" not in fault.classifier_dict()


def test_fault_references_exist() -> None:
    for fault in catalog.CATALOG:
        for ref in (
            *fault.confusable_with,
            *fault.mutually_exclusive_with,
            *fault.upstream_candidates,
        ):
            assert ref in catalog.FAULTS_BY_ID
            assert ref != fault.id
