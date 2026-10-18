"""Attached images keep camera identity, order and bytes."""

from __future__ import annotations

from dataclasses import replace

from tests.unit.reference_support import PACKAGE, SHOT_DIR, reference

from caddiej.swing_reference.package import load_evidence_package
from caddiej.swing_reference.request import PlayerContext, PreviousNote, build_request


def test_request_attaches_real_image_bytes_in_order() -> None:
    book = reference()
    package = load_evidence_package(PACKAGE)
    prepared = build_request(package, SHOT_DIR, book, context=PlayerContext())
    assert prepared.images
    assert prepared.manifest["reference_version"] == "2.9-timing-first"
    assert prepared.manifest["reference"]["version"] == "2.9-timing-first"
    times = [image.timestamp_seconds for image in prepared.images]
    assert times == sorted(times)
    seen: dict[str, list[tuple[str, float]]] = {}
    for image in prepared.images:
        assert image.data
        assert image.path.read_bytes() == image.data
        prefix = "DTL" if image.camera_id == "swing_behind" else "FO"
        assert image.image_id.startswith(prefix + "_")
        seen.setdefault(image.camera_id, []).append(
            (image.image_id, image.timestamp_seconds)
        )
        manifest = next(
            item
            for item in prepared.manifest["frames"]
            if item["image_id"] == image.image_id
        )
        assert manifest["camera_id"] == image.camera_id
        assert manifest["relative_file"] == image.relative_file
        assert manifest["capture_tick"] == image.capture_tick
    for items in seen.values():
        ids = [image_id for image_id, _stamp in items]
        assert ids == sorted(ids)
    advice = prepared.advice_request()
    assert advice.images[0].data == prepared.images[0].data
    assert advice.images[0].frame_id == prepared.images[0].image_id
    assert "base64" not in advice.evidence
    assert advice.strict_schema is False
    unresolved = [
        frame
        for frame in package.selected_frames
        if frame.stage is None and frame.reference.camera_id == "swing_behind"
    ]
    assert unresolved
    missing_stage = next(
        item
        for item in prepared.manifest["frames"]
        if item["relative_file"] == unresolved[0].reference.relative_file
        and item["camera_id"] == "swing_behind"
    )
    assert "stage" not in missing_stage


def test_missing_camera_and_missing_files_are_reported(tmp_path) -> None:
    book = reference()
    package = load_evidence_package(PACKAGE)
    behind = tuple(
        frame
        for frame in package.selected_frames
        if frame.reference.camera_id == "swing_behind"
    )
    only_behind = replace(package, selected_frames=behind)
    prepared = build_request(only_behind, SHOT_DIR, book)
    assert prepared.images
    assert all(image.camera_id == "swing_behind" for image in prepared.images)
    assert any("no swing_side" in warning for warning in prepared.warnings)
    empty = build_request(package, tmp_path, book)
    assert empty.images == ()
    assert any("missing" in warning for warning in empty.warnings)


def test_undelivered_cue_is_not_sent_to_the_model() -> None:
    book = reference()
    package = load_evidence_package(PACKAGE)
    heard = PreviousNote(
        active_focus="takeaway.clubhead_position",
        previous_cue="In the backswing, keep the clubhead over the hands.",
        previous_observation="The clubhead was inside the hands.",
        delivery_status="delivered",
    )
    sent = build_request(package, SHOT_DIR, book, previous=heard)
    assert sent.manifest["previous_context"]["previous_cue"] == heard.previous_cue
    unheard = PreviousNote(
        active_focus="takeaway.clubhead_position",
        previous_cue="In the backswing, keep the clubhead over the hands.",
        previous_observation="The clubhead was inside the hands.",
        delivery_status="not_delivered",
    )
    withheld = build_request(package, SHOT_DIR, book, previous=unheard)
    context = withheld.manifest["previous_context"]
    assert context["previous_cue"] is None
    assert context["delivery_status"] == "not_delivered"
    assert context["active_focus"] == "takeaway.clubhead_position"
    assert context["previous_observation"] == unheard.previous_observation
