"""Command line for one saved SwingEvidencePackage.

The default does not call a model. `--reference-call-model` is the explicit
approval for one paid vision request. `--reference-mock` replays a saved reply,
or the word `insufficient` for a reply that scores nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from caddiej.config import Settings
from caddiej.swing_reference.model import load_reference
from caddiej.swing_reference.pipeline import AnalysisResult, analyze_saved_swing
from caddiej.swing_reference.request import PlayerContext, PreviousNote
from caddiej.swing_reference.session import read_session, write_session
from caddiej.swing_reference.validate import insufficient_assessment


def run_reference_analysis(options: argparse.Namespace, settings: Settings) -> int:
    package = Path(options.reference_package)
    shot_dir = Path(options.reference_shot_dir)
    if not package.exists():
        print(f"evidence package not found: {package}")
        return 2
    if not shot_dir.is_dir():
        print(f"shot directory not found: {shot_dir}")
        return 2
    reference = load_reference()
    context = PlayerContext(
        club_type=options.reference_club,
        shot_type=options.reference_shot_type,
        distance_yards=options.reference_distance,
        handedness=options.reference_handedness,
        target_line=options.reference_target,
        evidence_context=options.reference_evidence_context,
    )
    previous = _previous(options.reference_previous)
    state = (
        read_session(Path(options.reference_state)) if options.reference_state else None
    )
    prepared_error = _exclusive(options)
    if prepared_error:
        print(prepared_error)
        return 2
    response: dict[str, Any] | str | None = None
    response_error: str | None = None
    called_model = False
    if options.reference_mock:
        response = _mock_response(options.reference_mock, reference, options, context)
    elif options.reference_call_model:
        called_model = True
        response, response_error = asyncio.run(
            _call_model(
                options, settings, package, shot_dir, reference, context, previous
            )
        )
    else:
        _write_prepared(options, package, shot_dir, reference, context, previous)
        return 0
    result = analyze_saved_swing(
        package,
        shot_dir,
        response=response,
        response_error=response_error,
        state=state,
        sequence=options.reference_sequence,
        context=context,
        previous=previous,
        reference=reference,
        confirm_delivery=bool(options.reference_delivered),
    )
    _write(options, result, called_model=called_model)
    print(_report(result, called_model=called_model, prepared_only=False))
    return 0


def _exclusive(options: argparse.Namespace) -> str:
    if options.reference_mock and options.reference_call_model:
        return "Pass either --reference-mock or --reference-call-model, not both."
    return ""


def _mock_response(
    value: str,
    reference: Any,
    options: argparse.Namespace,
    context: PlayerContext,
) -> dict[str, Any]:
    if value == "insufficient":
        shot_id = options.reference_shot_id or _shot_id_from_package(options)
        return insufficient_assessment(reference, shot_id, context.as_dict())
    payload = json.loads(Path(value).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("mock assessment must be an object")
    return payload


def _shot_id_from_package(options: argparse.Namespace) -> str:
    path = Path(options.reference_package)
    file_path = path / "evidence_package.json" if path.is_dir() else path
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    return str(payload["shot_id"])


async def _call_model(
    options: argparse.Namespace,
    settings: Settings,
    package: Path,
    shot_dir: Path,
    reference: Any,
    context: PlayerContext,
    previous: PreviousNote | None,
) -> tuple[dict[str, Any] | None, str | None]:
    from caddiej.adapters.openai.provider import OpenAIAdviceProvider
    from caddiej.swing_reference.package import load_evidence_package
    from caddiej.swing_reference.request import build_request

    provider = OpenAIAdviceProvider(
        api_key=settings.coaching_api_key.get_secret_value(),
        model=settings.coaching_model,
        base_url=settings.coaching_api_base or None,
    )
    if not provider.is_configured:
        return None, "model provider is not configured"
    prepared = build_request(
        load_evidence_package(package),
        shot_dir,
        reference,
        context=context,
        previous=previous,
    )
    try:
        reply = await provider.complete(
            prepared.advice_request(
                timeout_seconds=settings.coach_openai_timeout,
                image_detail=settings.coaching_image_detail,
            )
        )
    except Exception as error:
        return None, f"model request failed: {type(error).__name__}"
    finally:
        await provider.aclose()
    return dict(reply.payload), None


def _previous(path: str | None) -> PreviousNote | None:
    if not path:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return PreviousNote(
        active_focus=str(payload["active_focus"]),
        previous_cue=payload.get("previous_cue"),
        previous_observation=payload.get("previous_observation"),
        delivery_status=str(payload.get("delivery_status") or "unknown"),
        schema_version=str(payload.get("schema_version") or "1"),
    )


def _write_prepared(
    options: argparse.Namespace,
    package: Path,
    shot_dir: Path,
    reference: Any,
    context: PlayerContext,
    previous: PreviousNote | None,
) -> None:
    from caddiej.swing_reference.package import load_evidence_package
    from caddiej.swing_reference.request import build_request

    prepared = build_request(
        load_evidence_package(package),
        shot_dir,
        reference,
        context=context,
        previous=previous,
    )
    print(
        "\n".join(
            (
                f"shot {prepared.shot_id}",
                f"images {len(prepared.images)} "
                f"({sum(len(image.data) for image in prepared.images)} bytes)",
                "model_called false",
                "prepared the image request; no model was called",
            )
        )
    )
    if not options.reference_output:
        return
    output = Path(options.reference_output)
    output.mkdir(parents=True, exist_ok=True)
    manifest = dict(prepared.manifest)
    embedded = manifest.get("reference")
    version = embedded.get("version") if isinstance(embedded, dict) else None
    manifest["reference"] = {
        "version": version,
        "embedded_in_model_request": False,
        "placement": "stable_prefix",
        "stored_as": "config/ideal_swing.json",
    }
    _json(output / "request.json", manifest)
    _json(output / "images.json", _image_payload(prepared.images, called_model=False))


def _write(
    options: argparse.Namespace, result: AnalysisResult, *, called_model: bool
) -> None:
    if not options.reference_output:
        return
    output = Path(options.reference_output)
    output.mkdir(parents=True, exist_ok=True)
    manifest = dict(result.prepared.manifest)
    embedded = manifest.get("reference")
    version = embedded.get("version") if isinstance(embedded, dict) else None
    manifest["reference"] = {
        "version": version,
        "embedded_in_model_request": False,
        "placement": "stable_prefix",
        "stored_as": "config/ideal_swing.json",
    }
    _json(output / "request.json", manifest)
    _json(
        output / "images.json",
        _image_payload(result.prepared.images, called_model=called_model),
    )
    if result.assessment is not None:
        _json(output / "assessment.json", result.assessment)
    _json(
        output / "decision.json",
        {
            "presented": result.presented,
            "hud_text": result.hud_text,
            "speech_text": result.speech_text,
            "feedback": result.feedback,
            "decision_type": result.decision_type,
            "decision_status": result.decision_status,
            "active_focus": result.active_focus,
            "rule_id": result.rule_id,
            "ranking_status": result.ranking_status,
            "primary_checkpoint_id": result.primary_checkpoint_id,
            "tied_checkpoint_ids": list(result.tied_checkpoint_ids),
            "deterministic_ranking": result.deterministic_ranking,
            "reason": result.reason,
            "correction_note": result.correction_note,
            "errors": list(result.errors),
            "recovery": list(result.recovery),
            "warnings": list(result.warnings),
            "called_model": called_model,
            "next_context": None
            if result.next_context is None
            else result.next_context.as_dict(),
        },
    )
    write_session(result.state, output / "session_state.json")


def _image_payload(images: Any, *, called_model: bool) -> dict[str, Any]:
    return {
        "count": len(images),
        "bytes": sum(len(image.data) for image in images),
        "called_model": called_model,
        "images": [
            {
                "image_id": image.image_id,
                "camera_id": image.camera_id,
                "relative_file": image.relative_file,
                "capture_tick": image.capture_tick,
                "timestamp_seconds": image.timestamp_seconds,
                "stage": image.stage,
                "sha256": image.sha256,
                "byte_length": len(image.data),
                "media_type": image.media_type,
            }
            for image in images
        ],
    }


def _json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _report(result: AnalysisResult, *, called_model: bool, prepared_only: bool) -> str:
    lines = [
        f"shot {result.shot_id}",
        f"images {result.image_count} ({result.image_bytes} bytes)",
        f"model_called {str(called_model).lower()}",
    ]
    if prepared_only:
        lines.append("prepared the image request; no model was called")
        return "\n".join(lines)
    lines.append(f"decision {result.decision_status} {result.decision_type}")
    lines.append(f"ranking {result.ranking_status or 'rejected'}")
    if result.tied_checkpoint_ids:
        lines.append("tied " + ", ".join(result.tied_checkpoint_ids))
    lines.append(f"focus {result.active_focus or 'none'}")
    lines.append(f"presented {str(result.presented).lower()}")
    if result.speech_text:
        lines.append(result.speech_text)
    if result.errors:
        lines.append("errors " + "; ".join(result.errors[:4]))
    return "\n".join(lines)
