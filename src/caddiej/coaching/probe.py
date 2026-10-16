"""Exercise the production coaching pipeline without a golf shot.

This is a diagnostic, not a second implementation. It builds a representative
shot, then calls the same `diagnose`, `admissible_cues`, `render`,
`build_request`, `validate` and `AnalysisService` path a live ball uses.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from caddiej.coaching.baseline import ClubBaseline
from caddiej.coaching.cues import CUES, admissible_cues, preferred_cue
from caddiej.coaching.diagnose import diagnose
from caddiej.coaching.evidence import render
from caddiej.coaching.prompt import PROMPT_VERSION, build_request
from caddiej.coaching.service import AnalysisService, SheetRenderer
from caddiej.coaching.session import CoachingSession
from caddiej.coaching.validate import from_diagnosis
from caddiej.domain.coaching import Advice, Diagnosis
from caddiej.domain.cues import CueId
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisStarted,
    ShotCompleted,
    SwingFramesReady,
)
from caddiej.domain.media import SwingCapture
from caddiej.domain.shot import (
    BallLaunch,
    Club,
    ClubDelivery,
    ClubKind,
    Handedness,
    Shot,
    ShotSource,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class CoachProbe:
    ok: bool
    report: str
    diagnosis: Diagnosis | None = None
    advice: Advice | None = None


def fixture_shot(
    *,
    club: ClubKind = ClubKind.IRON,
    handedness: Handedness = Handedness.RIGHT,
    face_to_target: float = 4.8,
    path: float = 1.1,
    attack: float = -3.8,
    impact_horizontal: float = 2.0,
) -> Shot:
    """A measured iron with an open face. Enough evidence to coach honestly."""

    return Shot(
        shot_id=uuid4(),
        source=ShotSource(
            kind="synthetic",
            device_session_id=uuid4(),
            observed_at=NOW,
            observed_monotonic=0.0,
        ),
        club=Club(kind=club, vendor_code="7I"),
        handedness=handedness,
        ball=BallLaunch(
            speed_mph=118.0,
            launch_angle_degrees=16.0,
            launch_direction_degrees=2.4,
            total_spin_rpm=6200.0,
            spin_axis_degrees=4.0,
            carry_yards=155.0,
        ),
        club_delivery=ClubDelivery(
            speed_mph=86.0,
            angle_of_attack_degrees=attack,
            path_degrees=path,
            face_to_target_degrees=face_to_target,
            impact_horizontal_mm=impact_horizontal,
        ),
    )


def fixture_putt(
    *,
    face_to_target: float = 1.4,
    carry_yards: float = 5.5,
) -> Shot:
    return Shot(
        shot_id=uuid4(),
        source=ShotSource(
            kind="synthetic",
            device_session_id=uuid4(),
            observed_at=NOW,
            observed_monotonic=0.0,
        ),
        club=Club(kind=ClubKind.PUTTER, vendor_code="PT"),
        handedness=Handedness.RIGHT,
        ball=BallLaunch(speed_mph=4.2, carry_yards=carry_yards),
        club_delivery=ClubDelivery(
            speed_mph=3.8,
            face_to_target_degrees=face_to_target,
            impact_horizontal_mm=1.0,
        ),
    )


def cue_table_report() -> str:
    """The closed cue set, for `--check-coach-cues`."""

    lines = [
        "Coach V1 cue registry",
        f"  cues                 {len(CUES)}",
        "",
    ]
    for cue in CUES:
        asset = cue.asset or "(placeholder, no artwork)"
        lines.append(
            f"  {cue.cue_id.value:<22} {cue.display:<18} "
            f"{cue.grade.value:<9} {cue.kind.value:<14} {asset}"
        )
    return "\n".join(lines)


def admissibility_report(diagnosis: Diagnosis, *, has_frames: bool) -> str:
    allowed = diagnosis.admissible_cues or admissible_cues(
        diagnosis, has_frames=has_frames
    )
    chosen = preferred_cue(diagnosis, allowed)
    lines = [
        f"  club                 {diagnosis.club.value}",
        f"  kind                 {diagnosis.kind.value}",
        f"  focus                {diagnosis.focus.value}",
        f"  templated cue        {'' if diagnosis.cue is None else diagnosis.cue.value}",
        f"  preferred            {'' if chosen is None else chosen.value}",
        f"  admissible           {', '.join(cue.value for cue in allowed) or '(none)'}",
        f"  unmeasured           {', '.join(diagnosis.unmeasured) or '(none)'}",
    ]
    excluded = [cue.cue_id for cue in CUES if cue.cue_id not in allowed]
    lines.append(f"  excluded             {len(excluded)} cues")
    return "\n".join(lines)


async def probe_coach(
    *,
    provider_name: str,
    provider: object | None,
    send_frames: bool = False,
    visual_cues: bool = True,
    max_images: int = 6,
    capture: SwingCapture | None = None,
    speak: Callable[[str], object] | None = None,
) -> CoachProbe:
    """Run one synthetic shot through the production analysis worker."""

    shot = fixture_shot()
    session = CoachingSession()
    draft = diagnose(
        shot, ClubBaseline(club=ClubKind.IRON, shots=0), progress=session.progress
    )
    lines = [
        "Coach V1 production check — no golf shot required",
        f"  prompt               {PROMPT_VERSION}",
        f"  provider             {provider_name}",
        f"  send_frames          {send_frames}",
        f"  visual_cues          {visual_cues}",
        "",
        "Diagnosis (production diagnose())",
        admissibility_report(draft, has_frames=send_frames and capture is not None),
        "",
    ]

    published: list[object] = []

    async def publish(event: object) -> None:
        published.append(event)

    service = AnalysisService(
        publish=publish,
        provider=provider if provider is not None else None,  # type: ignore[arg-type]
        enabled=True,
        send_frames=send_frames,
        visual_cues=visual_cues,
        max_images=max_images,
        clip_wait_seconds=0.05 if capture is None else 0.2,
    )
    await service.open()
    try:
        if capture is not None:
            await service.on_swing_frames(SwingFramesReady(capture=capture))
        await service.on_shot_completed(ShotCompleted(shot=shot, trigger_id="check-1"))
        await service.aclose()
    except Exception as error:
        lines.append(f"  FAILED at AnalysisService: {error}")
        return CoachProbe(ok=False, report="\n".join(lines), diagnosis=draft)

    advice = _advice_from(published)
    called = any(isinstance(event, AnalysisStarted) for event in published)
    if advice is None:
        advice = from_diagnosis(draft, datetime.now(UTC))
        lines.append("  result               templated (no model reply)")
        lines.append("  provider call        not made")
        lines.append(f"  floor display        {advice.display!r}")
    else:
        lines.append(
            f"  result               {advice.source.value} "
            f"{'' if advice.cue is None else advice.cue.value} "
            f"{advice.display!r}"
        )
        lines.append(f"  speech               {advice.speech!r}")
        lines.append(
            f"  confidence           {advice.confidence} "
            f"({'' if advice.confidence_source is None else advice.confidence_source.value})"
        )
        lines.append(
            "  provider call        made"
            if called
            else "  provider call        not made (cached or templated line)"
        )
        lines.append(f"  floor display        {advice.display!r}")
        lines.append(
            f"  floor cue            {'' if advice.cue is None else advice.cue.value}"
        )

    evidence = render(draft, capture=capture if send_frames else None)
    request = build_request(draft, evidence, cues=draft.admissible_cues)
    lines.append("")
    lines.append(
        f"  schema cues          {request.schema['properties']['cue']['enum']}"
    )
    lines.append(f"  images in request    {len(request.images)}")

    if speak is not None and advice is not None and advice.speech:
        try:
            speak(advice.speech)
            lines.append("  speech path          invoked")
        except Exception as error:
            lines.append(f"  speech path          failed ({error})")

    lines.extend(
        [
            "",
            "This used diagnose(), admissible_cues(), render(), build_request(),",
            "validate()/from_diagnosis(), and AnalysisService.on_shot_completed.",
            "A live ball takes the same path with a real ShotCompleted.",
            "",
            "Limits of this check:",
            "  The advice object above is what PlayFlow would hand the floor.",
            "  It is not pushed to the live HUD or the projector.",
            "  Speech is not played. Hear the configured voice with",
            "  caddiej --check-speech (Kokoro, SPEECH_VOICE, real output).",
        ]
    )
    return CoachProbe(ok=True, report="\n".join(lines), diagnosis=draft, advice=advice)


async def probe_protee_coach(
    *,
    provider_name: str,
    provider: object | None,
    capture: SwingCapture | None,
    send_frames: bool = True,
    visual_cues: bool = True,
    max_images: int = 8,
    timeout_seconds: float = 30.0,
    budget_seconds: float = 45.0,
    max_output_tokens: int = 10000,
    reasoning_effort: str | None = None,
    evidence_sheet: SheetRenderer | None = None,
) -> CoachProbe:
    """The ProTee-only path, end to end, against a real capture on disk.

    The same `AnalysisService` the bay runs, in `protee` mode, with the real
    provider and the real stills, so a pass here is a pass of the request a
    live strike makes. Nothing is pushed to the floor or spoken. Pointed at an
    older shot's directory it is the real-shot benchmark: the sheet is drawn
    from that shot's originals and one normal request is made, once.
    """

    from caddiej.swing_capture.stills import read_labeled_stills

    lines = ["ProTee-only Coach check — last real swing capture, real provider"]
    if capture is None:
        lines.extend(
            [
                "  capture              none on disk",
                "",
                "  Hit one ball with CaddieJ running and swing capture on, then run",
                "  this again. It reuses media/latest_swing rather than faking one.",
            ]
        )
        return CoachProbe(ok=False, report="\n".join(lines))
    frames = sum(len(camera.frames) for camera in capture.cameras)
    still = capture.metrics
    lines.extend(
        [
            f"  capture              {capture.directory}",
            f"  strike_id            {capture.strike_id}",
            f"  swing frames         {frames}",
            "  All Values still     "
            + (
                "not configured"
                if still is None
                else (
                    f"{still.path} {still.width}x{still.height}"
                    if still.ok
                    else f"unavailable ({still.detail})"
                )
            ),
            "",
        ]
    )
    published: list[object] = []
    results: list[object] = []

    async def publish(event: object) -> None:
        published.append(event)

    async def record(result: object) -> None:
        results.append(result)

    options: dict[str, object] = {}
    if evidence_sheet is not None:
        options["evidence_sheet"] = evidence_sheet
    service = AnalysisService(
        publish=publish,
        provider=provider,  # type: ignore[arg-type]
        capture_stills=read_labeled_stills,
        enabled=True,
        send_frames=send_frames,
        visual_cues=visual_cues,
        max_images=max_images,
        min_interval_seconds=0.0,
        trigger="protee",
        protee_max_output_tokens=max_output_tokens,
        protee_timeout_seconds=timeout_seconds,
        protee_budget_seconds=budget_seconds,
        reasoning_effort=reasoning_effort,
        record_coach=record,
        drain_timeout_seconds=budget_seconds + 5.0,
        **options,  # type: ignore[arg-type]
    )
    heard = _Heard()
    watched = logging.getLogger("caddiej")
    level = watched.level
    if watched.getEffectiveLevel() > logging.INFO:
        watched.setLevel(logging.INFO)
    watched.addHandler(heard)
    started = time.monotonic()
    await service.open()
    try:
        await service.on_swing_frames(SwingFramesReady(capture=capture))
        await service.aclose()
    except Exception as error:
        lines.append(f"  FAILED at AnalysisService: {error}")
        return CoachProbe(ok=False, report="\n".join(lines))
    finally:
        watched.removeHandler(heard)
        watched.setLevel(level)
    wall = time.monotonic() - started

    called = any(isinstance(event, AnalysisStarted) for event in published)
    advice = _advice_from(published)
    result = results[-1] if results else None
    lines.append(f"  provider             {provider_name}")
    lines.append(f"  provider call        {'made' if called else 'not made'}")
    if result is not None:
        metrics = getattr(result, "metrics", None)
        lines.append(f"  outcome              {getattr(result, 'status', '?')}")
        lines.append(f"  images sent          {getattr(result, 'input_images', 0)}")
        lines.append(f"  metrics status       {getattr(result, 'metrics_status', '')}")
        if metrics is not None:
            lines.append(f"  metrics read         {len(metrics.available)}")
            for name in metrics.available:
                reading = metrics.get(name)
                assert reading is not None
                direction = "" if reading.direction is None else reading.direction
                lines.append(
                    f"    {name:<20} {reading.value} {reading.unit} {direction}"
                )
            lines.append(
                f"  metrics null         {', '.join(metrics.unavailable) or '(none)'}"
            )
        lines.append(f"  detail               {getattr(result, 'detail', None)}")
    if advice is not None:
        lines.append(
            f"  coach                {advice.kind.value} "
            f"{'' if advice.cue is None else advice.cue.value} {advice.display!r}"
        )
        lines.append(f"  speech               {advice.speech!r}")
    if result is not None:
        lines.extend(_transport_lines(capture, result, heard, wall))
    lines.extend(
        [
            "",
            "GSPro was not used. Speech is not played here; hear the voice with",
            "caddiej --check-speech.",
        ]
    )
    return CoachProbe(ok=called, report="\n".join(lines), advice=advice)


class _Heard(logging.Handler):
    """Keeps the few structured lines the benchmark report is built from."""

    WANTED = frozenset(
        {
            "coach_evidence_sheet",
            "coach_checkpoints_assessed",
            "coaching_request_started",
        }
    )

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: dict[str, logging.LogRecord] = {}

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message in self.WANTED:
            self.lines[message] = record

    def field(self, message: str, name: str) -> Any:
        record = self.lines.get(message)
        return None if record is None else getattr(record, name, None)


def _picture_size(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as picture:
            return picture.size
    except Exception:
        return None


def _transport_lines(
    capture: SwingCapture, result: Any, heard: _Heard, wall: float
) -> list[str]:
    """The request as it travelled, what it cost, and what came back."""

    from caddiej.coaching.benchmark import estimate_image_tokens
    from caddiej.swing_capture.sheet import stored_cells
    from caddiej.swing_capture.store import frame_files, frame_path

    sheet = "coach_evidence_sheet"
    width = heard.field(sheet, "coach_sheet_width")
    height = heard.field(sheet, "coach_sheet_height")
    size = heard.field(sheet, "coach_sheet_bytes")
    model = str(
        getattr(result, "model", None)
        or heard.field("coaching_request_started", "coaching_model")
        or ""
    )
    lines = [
        "",
        "Transport (one Swing Evidence Sheet)",
        f"  sheet                {heard.field(sheet, 'coach_sheet_status')}",
    ]
    if width and height:
        lines.append(
            f"  sheet size           {width} x {height} px, "
            f"{(size or 0) / 1024:.0f} KB, "
            f"{heard.field(sheet, 'coach_sheet_cells')} cells, "
            f"drawn in {heard.field(sheet, 'coach_sheet_render_ms')} ms"
        )
        lines.append(
            f"  dropped cells        {heard.field(sheet, 'coach_sheet_dropped')}"
        )
    sent = heard.field("coaching_request_started", "coaching_images")
    lines.append(
        f"  API images           {'no request made' if sent is None else sent}"
    )
    lines.append(f"  wall time            {wall:.1f} s (whole service, one strike)")
    latency = getattr(result, "latency_ms", None)
    lines.append(
        "  OpenAI latency       "
        + ("not measured" if latency is None else f"{latency / 1000:.1f} s")
    )
    tokens_in = getattr(result, "input_tokens", None)
    tokens_out = getattr(result, "output_tokens", None)
    total = None if tokens_in is None or tokens_out is None else tokens_in + tokens_out
    lines.append(
        f"  tokens               input {tokens_in}, output {tokens_out}, total {total}"
    )
    cells = stored_cells(capture.directory)
    if cells and model:
        separate = 0
        for cell in cells.values():
            original = frame_path(capture, str(cell.get("frame_id")))
            dims = None if original is None else _picture_size(original)
            if dims is not None:
                separate += estimate_image_tokens(
                    dims[0], dims[1], detail="high", model=model
                ).tokens
        on_sheet = (
            estimate_image_tokens(int(width), int(height), detail="high", model=model)
            if width and height
            else None
        )
        lines.append(
            f"  image tokens         sheet ~{on_sheet.tokens if on_sheet else '?'}; "
            f"the same {len(cells)} originals sent separately ~{separate} (estimate)"
        )
    scored = heard.field("coach_checkpoints_assessed", "coach_checkpoints_scored")
    rejected = heard.field("coach_checkpoints_assessed", "coach_checkpoints_rejected")
    lines.extend(
        [
            "",
            "Swing review",
            "  checkpoints          "
            + (
                "not assessed"
                if scored is None
                else f"returned {scored + (rejected or 0)}, accepted {scored}, "
                f"rejected {rejected}"
            ),
            "  rejection reasons    "
            + str(
                heard.field("coach_checkpoints_assessed", "coach_checkpoints_reasons")
            ),
        ]
    )
    score = getattr(result, "swing_score", None)
    mechanics = None if score is None else score.mechanics
    lines.append(f"  Swing Score          {None if score is None else score.value}")
    if mechanics is not None:
        lines.append(
            f"  coverage             {mechanics.coverage:.2f} "
            f"({mechanics.coverage_level.value})"
        )
        for phase in mechanics.phases:
            lines.append(
                f"    {phase.phase.value:<12} {phase.assessed}/{phase.scoreable} "
                f"assessed, score {phase.value}"
            )
    review = getattr(result, "review", None)
    files = frame_files(capture)
    if review is not None:
        for fault in review.faults:
            original = files.get(fault.frame_id or "")
            lines.append(
                f"  fault                {fault.fault.value} "
                f"<- {','.join(item.value for item in fault.triggers)} "
                f"<- {fault.frame_id} "
                f"({'no file' if original is None else original.as_posix()})"
            )
        root = review.root
        lines.append(
            "  root cause           "
            + (
                "none"
                if root is None or root.fault is None
                else f"{root.fault.value} ({root.reason})"
            )
        )
    lines.append(
        f"  final cue            {getattr(result, 'cue_id', None)} "
        f"{getattr(result, 'display', None)!r}"
    )
    return lines


def _advice_from(published: list[object]) -> Advice | None:
    for event in reversed(published):
        if isinstance(event, AnalysisCompleted):
            return event.advice
    return None


@dataclass
class CueProbe:
    ok: bool
    report: str
    diagnoses: list[Diagnosis] = field(default_factory=list)


def probe_coach_cues() -> CueProbe:
    """Offline: the cue table and why a synthetic shot admits what it admits."""

    empty = ClubBaseline(club=ClubKind.IRON, shots=0)
    iron = diagnose(fixture_shot(), empty)
    putt = diagnose(fixture_putt(), empty, target_distance_m=4.0)
    left = diagnose(
        fixture_shot(handedness=Handedness.LEFT, face_to_target=-4.8),
        empty,
    )
    observe = diagnose(
        Shot(
            shot_id=uuid4(),
            source=ShotSource(
                kind="synthetic",
                device_session_id=uuid4(),
                observed_at=NOW,
                observed_monotonic=0.0,
            ),
            club=Club(kind=ClubKind.IRON),
            ball=BallLaunch(speed_mph=118.0, carry_yards=155.0),
        ),
        empty,
    )
    lines = [
        cue_table_report(),
        "",
        "Synthetic iron (open face)",
        admissibility_report(iron, has_frames=False),
        "",
        "Synthetic putt (open face, long of target)",
        admissibility_report(putt, has_frames=False),
        "",
        "Synthetic left-handed iron (mirrored open face)",
        admissibility_report(left, has_frames=False),
        "",
        "Observe-mode iron (ball only, no club delivery)",
        admissibility_report(observe, has_frames=False),
    ]
    if CueId.FACE_OPEN not in iron.admissible_cues:
        lines.append("")
        lines.append("  FAILED: open-face iron did not admit FACE_OPEN")
        return CueProbe(ok=False, report="\n".join(lines), diagnoses=[iron, putt, left])
    if any(cue.value.startswith("PUTT_") for cue in iron.admissible_cues):
        lines.append("")
        lines.append("  FAILED: full-swing iron admitted a putting cue")
        return CueProbe(ok=False, report="\n".join(lines), diagnoses=[iron, putt, left])
    return CueProbe(
        ok=True, report="\n".join(lines), diagnoses=[iron, putt, left, observe]
    )
