"""Coach V1 on ProTee VX alone: strike, swing capture, All Values, one request.

GSPro is not running in any of these. The strike is a `ShotDetected`, the
evidence is the capture swing capture published for it, and the numbers come
only from the one All Values still, read by the model in the same request.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.schema_support import fixed_length, resolved
from tests.unit.sheet_support import placeholder_sheet, sheet_cells
from tests.unit.test_play_flow import Announcements, FloorSpy, an_advice, flow
from tests.unit.test_speech_service import RecordingSink, Table, a_shot
from tests.unit.test_swing_capture import Clock, StubObs, a_still

from caddiej.coaching.checkpoints import CHECKPOINTS_BY_ID, SCOREABLE
from caddiej.coaching.fakes import BrokenProvider, ScriptedProvider, SilentProvider
from caddiej.coaching.probe import probe_protee_coach
from caddiej.coaching.provider import ProviderUnavailableError
from caddiej.coaching.service import AnalysisService
from caddiej.coaching.vx import (
    PROTEE_PROMPT_VERSION,
    SCHEMA_NAME,
    WIRE_LISTS,
    parse_metrics,
    possible_cues,
    reply_schema,
    shot_from_metrics,
    signed,
)
from caddiej.domain.coaching import AdviceFailure, AdviceKind, CoachingFocus
from caddiej.domain.cues import CueId, PanelKind, PanelView, VisualClaim
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisFailed,
    AnalysisStarted,
    BallDetected,
    ClubSelected,
    LaunchMonitorReadinessChanged,
    ShotCompleted,
    ShotDetected,
    ShotDiagnosed,
    SwingFramesReady,
)
from caddiej.domain.media import (
    MetricsStill,
    SwingCameraCapture,
    SwingCapture,
    SwingFrame,
)
from caddiej.domain.shot import Club, ClubKind, Handedness
from caddiej.domain.swing_checkpoints import CheckpointId
from caddiej.domain.vx import (
    VX_METRICS,
    VX_METRICS_BY_NAME,
    VX_METRICS_PROVENANCE,
    CoachResult,
    MetricDirection,
    MetricUnit,
)
from caddiej.persistence.database import Database
from caddiej.persistence.service import HistoryService
from caddiej.swing_capture import LatestSwingStore, SwingCaptureService
from caddiej.swing_capture.stills import read_labeled_stills
from caddiej.swing_capture.store import METADATA_NAME, load_capture

NOW = datetime(2026, 10, 1, 19, 0, tzinfo=UTC)
DRIVER = Club(kind=ClubKind.DRIVER, vendor_code="DR")
ALL_VALUES = "ProTee All Values"

BODY_CLAIMS = frozenset(
    {
        VisualClaim.BODY_IN_BACKSWING,
        VisualClaim.BODY_THROUGH_IMPACT,
        VisualClaim.FINISH_POSITION,
        VisualClaim.SEQUENCE,
        VisualClaim.WHOLE_BODY,
    }
)
"""What a swing_behind or swing_side panel claims to show in a real bay.

Copied from the panel configuration a hardware session writes, so a test that
turns these on is testing against the claims production actually makes."""

# --- what the model is scripted to read ------------------------------------


def a_reading(value: float, unit: str, direction: str | None = None) -> dict:
    return {"value": value, "unit": unit, "direction": direction}


def every_metric_null() -> dict[str, Any]:
    return {spec.name: None for spec in VX_METRICS}


def an_open_face_grid(**overrides: Any) -> dict[str, Any]:
    """What a clearly readable All Values grid of an open-faced drive says."""

    metrics = every_metric_null()
    metrics.update(
        ball_speed=a_reading(148.0, "mph"),
        club_speed=a_reading(112.0, "mph"),
        face_angle=a_reading(6.5, "deg", "open"),
        swing_path=a_reading(1.0, "deg", "out_to_in"),
        attack_angle=a_reading(1.0, "deg", "down"),
        launch_angle=a_reading(12.6, "deg"),
        launch_direction=a_reading(1.5, "deg", "right"),
        total_spin=a_reading(2400.0, "rpm"),
        carry=a_reading(265.0, "yd"),
    )
    metrics.update(overrides)
    return metrics


BEHIND_FRAME = "swing_behind_01"
SIDE_FRAME = "swing_side_01"


def a_checkpoint(
    checkpoint: CheckpointId | str,
    score: int = 85,
    *,
    confidence: float = 0.7,
    deviation: str | None = None,
    frame_id: str | None = None,
    phase: str | None = None,
) -> dict[str, Any]:
    """One checkpoint score, cited from a camera and a moment it admits.

    Down the line when the checkpoint allows it, face on otherwise, and its
    home phase, so a test that wants a refusal has to ask for one."""

    spec = CHECKPOINTS_BY_ID[CheckpointId(checkpoint)]
    behind = PanelView.BEHIND in spec.views
    return {
        "score": score,
        "confidence": confidence,
        "frame_id": frame_id or (BEHIND_FRAME if behind else SIDE_FRAME),
        "phase": phase or spec.phase.value,
        "view": "down_the_line" if behind else "face_on",
        "observation": "The body looks settled here.",
        "deviation": deviation,
    }


def a_swing(score: int = 85, **overrides: Any) -> dict[str, Any]:
    """Every scoreable checkpoint scored, from a camera that can show it.

    85 throughout by default: a sound swing, fully covered, with no fault to
    infer. That is the right baseline for the many tests here that are about
    the measured path and want the body out of the way."""

    checkpoints: dict[str, Any] = {
        item.checkpoint_id.value: a_checkpoint(item.checkpoint_id, score)
        for item in SCOREABLE
    }
    checkpoints.update(overrides)
    return checkpoints


def no_swing() -> dict[str, Any]:
    """A reply that scored nothing: every checkpoint null.

    What a shot with no usable replay stills produces, and under rubric v4
    that is no Swing Score rather than a low one."""

    return {item.checkpoint_id.value: None for item in SCOREABLE}


def a_reply(
    metrics: dict[str, Any] | None = None,
    *,
    kind: str = "correction",
    cue: str | None = "FACE_OPEN",
    speech: str | None = "Square the face a little earlier.",
    reinforcement: str | None = None,
    checkpoints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "metrics": an_open_face_grid() if metrics is None else metrics,
        "checkpoints": a_swing() if checkpoints is None else checkpoints,
        "coach": {
            "kind": kind,
            "cue": cue,
            "instruction": speech,
            "reinforcement": reinforcement,
            "visual_confidence": None,
        },
    }


# --- a capture on disk, as swing capture writes it -------------------------


def a_capture_on_disk(
    tmp_path: Path,
    *,
    strike_id=None,
    trigger_id: str = "16",
    behind: int = 4,
    side: int = 3,
    impact: int = 1,
    metrics: bool | None = True,
    metrics_bytes: bytes | None = None,
    supports: bool = False,
) -> SwingCapture:
    """Three panels and, unless told otherwise, one All Values still.

    `supports` declares the visual claims the two swing panels make, as a real
    bay's configuration does. Off by default: a capture that claims nothing is
    a capture no visual cue is admissible against, and most of these tests are
    about the measured path and want that out of the way.
    """

    claimed = BODY_CLAIMS if supports else frozenset()
    directory = tmp_path / "latest_swing"
    cameras = []
    for camera_id, count, view, kind, panel_claims in (
        ("swing_behind", behind, PanelView.BEHIND, PanelKind.DYNAMIC, claimed),
        ("swing_side", side, PanelView.SIDE, PanelKind.DYNAMIC, claimed),
        ("impact", impact, PanelView.NONE, PanelKind.STATIC, frozenset()),
    ):
        frames = []
        for index in range(count):
            path = Path(camera_id) / f"frame_{index + 1:03d}.jpg"
            (directory / path).parent.mkdir(parents=True, exist_ok=True)
            (directory / path).write_bytes(
                b"\xff\xd8" + camera_id.encode() + b"\xff\xd9"
            )
            frames.append(
                SwingFrame(
                    camera_id=camera_id,
                    index=index + 1,
                    offset_seconds=index * 0.1,
                    path=path,
                    width=640,
                    height=360,
                )
            )
        cameras.append(
            SwingCameraCapture(
                camera_id=camera_id,
                source=f"CaddieJ {camera_id}",
                ok=bool(frames),
                frames=tuple(frames),
                captured=count,
                failed=0,
                kind=kind,
                view=view,
                supports=panel_claims,
            )
        )
    still: MetricsStill | None = None
    if metrics is True:
        path = Path("metrics") / "all_values.jpg"
        (directory / path).parent.mkdir(parents=True, exist_ok=True)
        (directory / path).write_bytes(metrics_bytes or b"\xff\xd8grid\xff\xd9")
        still = MetricsStill(
            source=ALL_VALUES,
            ok=True,
            path=path,
            width=1920,
            height=1080,
            captured_at=NOW + timedelta(seconds=3),
            delay_seconds=3.0,
            offset_seconds=3.0,
        )
    elif metrics is False:
        still = MetricsStill(
            source=ALL_VALUES,
            ok=False,
            captured_at=NOW + timedelta(seconds=3),
            delay_seconds=3.0,
            detail="OBS has no source named 'ProTee All Values'",
        )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / METADATA_NAME).write_text(
        json.dumps({"trigger_id": trigger_id}), encoding="utf-8"
    )
    return SwingCapture(
        capture_id=uuid4(),
        trigger_id=trigger_id,
        directory=directory,
        started_at=NOW,
        detected_at=NOW,
        cameras=tuple(cameras),
        strike_id=strike_id,
        metrics=still,
    )


class Bus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def __call__(self, event: object) -> None:
        self.events.append(event)

    def of[T](self, kind: type[T]) -> list[T]:
        return [event for event in self.events if isinstance(event, kind)]


class Coach:
    """A ProTee-mode coaching service with the real still reader."""

    def __init__(self, provider: object | None, **options: Any) -> None:
        self.bus = Bus()
        self.results: list[CoachResult] = []
        defaults: dict[str, Any] = {
            "send_frames": True,
            "max_images": 8,
            "timeout_seconds": 0.5,
            "min_interval_seconds": 0.0,
            "backoff_initial_seconds": 0.001,
            "backoff_max_seconds": 0.002,
            "now": lambda: NOW,
            "evidence_sheet": placeholder_sheet,
        }
        defaults.update(options)
        self.provider = provider
        self.service = AnalysisService(
            publish=self.bus,
            provider=provider,  # type: ignore[arg-type]
            capture_stills=read_labeled_stills,
            trigger="protee",
            record_coach=self._record,
            **defaults,
        )

    async def _record(self, result: CoachResult) -> None:
        self.results.append(result)

    async def strike(self, tmp_path: Path, **capture: Any) -> ShotDetected:
        detected = ShotDetected(trigger_id=capture.pop("trigger_id", "16"))
        await self.service.on_shot_detected(detected)
        made = a_capture_on_disk(
            tmp_path,
            strike_id=detected.event_id,
            trigger_id=detected.trigger_id,
            **capture,
        )
        await self.service.on_swing_frames(SwingFramesReady(capture=made))
        return detected

    async def run(self, tmp_path: Path, **capture: Any) -> ShotDetected:
        await self.service.open()
        await self.service.on_club_selected(ClubSelected(club=DRIVER))
        detected = await self.strike(tmp_path, **capture)
        await self.service.aclose()
        return detected


# 1, 2, 3 ----------------------------------------------------------------------


class TestACoachWithoutGSPro:
    async def test_a_protee_strike_is_coached_with_no_gspro_shot_at_all(
        self, tmp_path
    ) -> None:
        provider = ScriptedProvider(replies=(a_reply(),))
        coach = Coach(provider)

        detected = await coach.run(tmp_path)

        assert not any(isinstance(e, ShotCompleted) for e in coach.bus.events)
        completed = coach.bus.of(AnalysisCompleted)
        assert len(completed) == 1
        assert completed[0].shot_id == detected.event_id
        assert completed[0].advice.cue is CueId.FACE_OPEN
        assert completed[0].advice.focus is CoachingFocus.FACE_CONTROL
        assert coach.results[0].status == "ok"
        assert coach.results[0].strike_id == detected.event_id

    async def test_one_request_carries_the_swing_frames_and_the_all_values_still(
        self, tmp_path
    ) -> None:
        provider = ScriptedProvider(replies=(a_reply(),))
        coach = Coach(provider)

        await coach.run(tmp_path)

        assert provider.calls == 1, "no separate OCR request"
        request = provider.requests[0]
        assert request.schema_name == SCHEMA_NAME
        assert len(request.images) == 1, "one sheet, never the stills beside it"
        (sheet,) = request.images
        assert sheet.label == "SWING_EVIDENCE_SHEET"
        assert sheet.detail == "high"
        cells = sheet_cells(sheet.data)
        assert cells[-1] == "ALL_VALUES"
        assert "ALL_VALUES" not in cells[:-1]
        assert (tmp_path / "latest_swing" / "swing_evidence.jpg").read_bytes() == (
            sheet.data
        )

    async def test_gspro_being_absent_is_not_logged_as_a_failure(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        coach = Coach(ScriptedProvider(replies=(a_reply(),)))

        await coach.run(tmp_path)

        assert not coach.bus.of(AnalysisFailed)
        events = [record.getMessage() for record in caplog.records]
        assert "coaching_evidence_ready" in events
        assert "coaching_request_started" in events
        assert "coaching_advice_ready" in events
        assert "coaching_shot_ignored" not in events
        assert any(
            "gspro=not required" in str(getattr(r, "coaching_detail", ""))
            for r in caplog.records
        )

    async def test_a_late_gspro_shot_does_not_coach_the_strike_twice(
        self, tmp_path
    ) -> None:
        provider = ScriptedProvider(replies=(a_reply(),))
        coach = Coach(provider)
        await coach.service.open()
        await coach.strike(tmp_path)
        await coach.service.on_shot_completed(ShotCompleted(shot=a_shot(uuid4())))
        await coach.service.aclose()

        assert provider.calls == 1
        assert len(coach.bus.of(AnalysisCompleted)) == 1


# 4, 5 -------------------------------------------------------------------------


class TestTheImageBudget:
    async def test_eight_swing_frames_plus_one_all_values_still(self, tmp_path) -> None:
        provider = ScriptedProvider(replies=(a_reply(),))
        coach = Coach(provider, max_images=8)

        await coach.run(tmp_path, behind=4, side=3, impact=1)

        images = provider.requests[0].images
        assert len(images) == 1
        cells = sheet_cells(images[0].data)
        assert len(cells) == 9
        assert cells[:8].count("ALL_VALUES") == 0
        assert coach.results[0].input_images == 1
        assert coach.results[0].swing_images == 8

    async def test_the_all_values_still_never_displaces_a_swing_frame(
        self, tmp_path
    ) -> None:
        provider = ScriptedProvider(replies=(a_reply(),))
        coach = Coach(provider, max_images=8)

        await coach.run(tmp_path, behind=8, side=8, impact=2)

        cells = sheet_cells(provider.requests[0].images[0].data)
        assert sum(1 for cell in cells if cell != "ALL_VALUES") == 8
        assert cells[-1] == "ALL_VALUES"

    async def test_the_request_is_noted_in_the_capture_metadata(self, tmp_path) -> None:
        coach = Coach(ScriptedProvider(replies=(a_reply(),)))

        await coach.run(tmp_path)

        payload = json.loads(
            (tmp_path / "latest_swing" / METADATA_NAME).read_text(encoding="utf-8")
        )
        assert payload["provider_image_count"] == 1
        assert payload["provider_swing_frame_count"] == 8
        assert payload["coach_request"]["prompt_version"] == PROTEE_PROMPT_VERSION
        assert payload["coach_request"]["metrics_image"] is True
        assert payload["coach_request"]["transport"] == "evidence_sheet"
        assert payload["provider_images"] == [
            {
                "panel_id": "evidence_sheet",
                "label": "SWING_EVIDENCE_SHEET",
                "sent": 1,
                "metrics_evidence": False,
            }
        ]
        metrics_cells = [
            cell
            for cell in payload["evidence_sheet"]["cells"]
            if cell["kind"] == "metrics"
        ]
        assert [(c["cell_id"], c["frame_id"]) for c in metrics_cells] == [
            ("ALL_VALUES", "all_values")
        ]
        assert payload["provider_frame_ids"]["all_values"] == "metrics/all_values.jpg"


# 6, 7 -------------------------------------------------------------------------


class TestAMissingOrBadAllValuesStill:
    async def test_a_failed_all_values_still_scores_the_swing_and_reads_no_numbers(
        self, tmp_path, caplog
    ) -> None:
        """No numbers, but swing pictures: the one request still goes, because
        the Swing Score is the checkpoints and needs no grid. Nothing measured
        is read or coached, and the log and the row say why."""

        caplog.set_level("INFO")
        provider = ScriptedProvider(replies=(a_reply(),))
        coach = Coach(provider)

        await coach.run(tmp_path, metrics=False)

        assert provider.calls == 1
        assert not coach.bus.of(AnalysisFailed)
        result = coach.results[0]
        assert result.metrics_status == "unavailable"
        assert result.metrics is None
        assert result.swing_score is not None and result.swing_score.value == 85
        assert result.cue_id is None
        evidence = [
            str(getattr(r, "coaching_detail", ""))
            for r in caplog.records
            if r.getMessage() == "coaching_evidence_ready"
        ]
        assert "all_values=no" in evidence[0]
        assert "no source named" in evidence[0]

    def test_without_a_grid_the_request_says_every_metric_is_null(self) -> None:
        from caddiej.coaching.vx import render_evidence

        text = render_evidence(
            club=ClubKind.DRIVER,
            handedness=Handedness.RIGHT,
            has_metrics=False,
            swing_images=("SWING_BEHIND / DOWN_THE_LINE",),
            cues=(),
            session_focus=None,
            focus_shots=0,
        )

        assert "No ALL_VALUES image was captured; every metric is null." in text

    async def test_no_metrics_and_no_pictures_explains_why_nothing_was_said(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        provider = ScriptedProvider(replies=(a_reply(),))
        coach = Coach(provider)

        await coach.run(tmp_path, metrics=None, behind=0, side=0)

        assert provider.calls == 0
        reasons = [
            getattr(record, "coaching_reason", None)
            for record in caplog.records
            if record.getMessage() == "coaching_no_advice"
        ]
        assert reasons == ["no_admissible_cue"]
        assert coach.results[0].status == "none"
        assert coach.bus.of(ShotDiagnosed), "the floor is told to drop its mark"

    async def test_a_one_pixel_metrics_still_is_refused_at_capture(
        self, tmp_path
    ) -> None:
        obs = StubObs(tiny_sources=frozenset({ALL_VALUES}))
        capture = await one_capture(tmp_path, obs)

        assert capture.metrics is not None
        assert not capture.metrics.ok
        assert capture.metrics.path is None
        assert "1×1" in (capture.metrics.detail or "")

    async def test_an_unreadable_grid_is_not_believed(self, tmp_path) -> None:
        reply = a_reply(
            every_metric_null()
            | {
                "face_angle": a_reading(6.5, "rpm", "open"),
                "ball_speed": a_reading(float("inf"), "mph"),
                "club_speed": a_reading(-112.0, "mph", "open"),
            },
            kind="none",
            cue=None,
            speech=None,
        )
        coach = Coach(ScriptedProvider(replies=(reply,)))

        await coach.run(tmp_path)

        metrics = coach.results[0].metrics
        assert metrics is not None
        assert metrics.available == ()
        assert set(metrics.rejected) == {"face_angle", "ball_speed", "club_speed"}
        assert coach.results[0].metrics_status == "unreadable"
        assert not coach.bus.of(AnalysisCompleted)


# 8, 9, 10 ---------------------------------------------------------------------


class TestReadingTheGrid:
    def test_clear_values_come_back_typed_with_unit_and_direction(self) -> None:
        metrics = parse_metrics(
            {
                "face_angle": a_reading(5.2, "deg", "closed"),
                "carry": a_reading(231, "m"),
            }
        )

        face = metrics.get("face_angle")
        assert face is not None
        assert (face.value, face.unit, face.direction) == (
            5.2,
            MetricUnit.DEG,
            MetricDirection.CLOSED,
        )
        assert metrics.get("carry").unit is MetricUnit.M  # type: ignore[union-attr]
        assert (
            metrics.provenance == VX_METRICS_PROVENANCE == "protee_vx_all_values_visual"
        )

    def test_every_metric_is_individually_nullable(self) -> None:
        metrics = parse_metrics(every_metric_null() | {"carry": a_reading(240, "yd")})

        assert metrics.available == ("carry",)
        assert len(metrics.unavailable) == len(VX_METRICS) - 1
        payload = metrics.as_dict()["metrics"]
        assert isinstance(payload, dict)
        assert payload["face_angle"] is None
        assert len(payload) == len(VX_METRICS)

    def test_the_schema_requires_every_key_and_spells_absence_as_null(self) -> None:
        schema = reply_schema((CueId.FACE_OPEN,))

        metrics = schema["properties"]["metrics"]
        assert fixed_length(metrics) == len(VX_METRICS)
        assert {"type": "null"} in resolved(schema, metrics["items"])["anyOf"]
        assert set(schema["properties"]["checkpoints"]["required"]) == set(WIRE_LISTS)
        coach = schema["properties"]["coach"]
        assert set(coach["required"]) == set(coach["properties"])

    @pytest.mark.parametrize(
        ("item", "why"),
        [
            ({"value": 5.2, "unit": "deg", "direction": "sideways"}, "unknown word"),
            ({"value": 5.2, "unit": "deg", "direction": "left"}, "not a face word"),
            ({"value": 5.2, "unit": "mph", "direction": "open"}, "wrong unit"),
            ({"value": -5.2, "unit": "deg", "direction": "closed"}, "sign and word"),
            ({"value": 80.0, "unit": "deg", "direction": "open"}, "implausible"),
            ({"value": "5.2", "unit": "deg", "direction": "open"}, "not a number"),
            ({"value": True, "unit": "deg", "direction": "open"}, "a boolean"),
        ],
    )
    def test_an_ambiguous_reading_becomes_null(self, item: dict, why: str) -> None:
        metrics = parse_metrics({"face_angle": item})

        assert metrics.get("face_angle") is None, why
        assert metrics.rejected == ("face_angle",)

    @pytest.mark.parametrize(
        ("name", "reading", "right", "left"),
        [
            ("face_angle", a_reading(5.2, "deg", "open"), 5.2, -5.2),
            ("face_angle", a_reading(5.2, "deg", "closed"), -5.2, 5.2),
            ("swing_path", a_reading(3.0, "deg", "in_to_out"), 3.0, -3.0),
            ("swing_path", a_reading(3.0, "deg", "out_to_in"), -3.0, 3.0),
            ("launch_direction", a_reading(2.0, "deg", "right"), 2.0, 2.0),
            ("launch_direction", a_reading(2.0, "deg", "left"), -2.0, -2.0),
            ("attack_angle", a_reading(4.0, "deg", "down"), -4.0, -4.0),
            ("attack_angle", a_reading(-4.0, "deg"), -4.0, -4.0),
        ],
    )
    def test_direction_words_become_caddiejs_sign_only_at_diagnosis(
        self, name: str, reading: dict, right: float, left: float
    ) -> None:
        """Open/closed and in-to-out are the player's; left/right are the
        target's. `oriented()` mirrors the target frame for a left-hander, so
        the player-relative words are unmirrored here to come out right."""

        metrics = parse_metrics({name: reading})

        kept = metrics.get(name)
        assert kept is not None
        if VX_METRICS_BY_NAME[name].signed:
            # Kept as ProTee prints it: the signed number, no word.
            assert (kept.value, kept.direction) == (right, None)
        else:
            assert kept.direction == reading["direction"]
        assert signed(metrics, name, Handedness.RIGHT) == right
        assert signed(metrics, name, Handedness.LEFT) == left

    def test_a_directional_number_with_no_word_is_kept_but_not_ranked(self) -> None:
        metrics = parse_metrics({"face_angle": a_reading(-2.1, "deg")})

        assert metrics.get("face_angle") is not None
        assert signed(metrics, "face_angle", Handedness.RIGHT) is None

    def test_a_left_handers_open_face_is_diagnosed_as_open(self) -> None:
        from caddiej.coaching.faults import oriented

        metrics = parse_metrics(
            {
                "ball_speed": a_reading(148.0, "mph"),
                "face_angle": a_reading(6.0, "deg", "open"),
            }
        )
        shot = shot_from_metrics(
            metrics,
            shot_id=uuid4(),
            club=DRIVER,
            handedness=Handedness.LEFT,
            observed_at=NOW,
            observed_monotonic=0.0,
        )

        assert shot is not None
        assert shot.source.device_id == "protee_vx_all_values"
        assert shot.club_delivery is not None
        assert shot.club_delivery.face_to_target_degrees == -6.0
        assert (
            oriented(shot.club_delivery.face_to_target_degrees, Handedness.LEFT) == 6.0
        )  # type: ignore[arg-type]


# 11 ---------------------------------------------------------------------------


class TestOnlyFiniteCues:
    async def test_a_cue_the_schema_never_offered_is_refused(self, tmp_path) -> None:
        coach = Coach(ScriptedProvider(replies=(a_reply(cue="KEEP_YOUR_HEAD_DOWN"),)))

        await coach.run(tmp_path)

        assert [e.advice.cue for e in coach.bus.of(AnalysisCompleted)] == [
            CueId.FACE_OPEN
        ]
        assert coach.results[0].cue_id == "FACE_OPEN"

    async def test_a_cue_the_numbers_do_not_support_is_refused(self, tmp_path) -> None:
        """The model may choose; it may not decide that a face was closed."""

        coach = Coach(ScriptedProvider(replies=(a_reply(cue="FACE_CLOSED"),)))

        await coach.run(tmp_path)

        completed = coach.bus.of(AnalysisCompleted)
        assert [e.advice.cue for e in completed] == [CueId.FACE_OPEN]
        assert completed[0].advice.display == "PASS THE TOE EARLY"
        result = coach.results[0]
        assert result.cue_id == "FACE_OPEN"
        assert "FACE_CLOSED" in (result.detail or "")

    def test_the_schema_offers_only_registered_cue_ids(self) -> None:
        cues = possible_cues(ClubKind.DRIVER, has_metrics=True)
        schema = reply_schema(cues)

        offered = schema["properties"]["coach"]["properties"]["cue"]["anyOf"][1]["enum"]
        assert offered and set(offered) <= {cue.value for cue in CueId}

    async def test_the_model_declining_is_no_coaching_not_a_failure(
        self, tmp_path
    ) -> None:
        coach = Coach(
            ScriptedProvider(
                replies=(
                    a_reply(every_metric_null(), kind="none", cue=None, speech=None),
                )
            )
        )

        await coach.run(tmp_path)

        assert not coach.bus.of(AnalysisCompleted)
        assert not coach.bus.of(AnalysisFailed)
        assert coach.results[0].status == "none"
        assert coach.results[0].kind == AdviceKind.NONE.value


# 12, 13 -----------------------------------------------------------------------


def advice_for(strike_id, display: str = "FACE OPEN") -> AnalysisCompleted:
    return AnalysisCompleted(
        shot_id=strike_id, advice=an_advice(strike_id, display=display)
    )


class TestTheNextBallEndsTheLesson:
    """Reversed after the first hardware test. See `_retire_coaching_for_new_ball`.

    The line used to survive until the next strike, on the reasoning that a
    player should not lose a lesson to a sensor. In a real bay the next ball
    going down is the moment the player has finished reading and wants a
    distance and a club instead, and the coaching was in the way of both.
    """

    async def test_placing_a_ball_retires_the_line(self) -> None:
        floor = FloorSpy()
        subject = flow(floor)
        strike = ShotDetected(trigger_id="16")
        await subject.on_shot_detected(strike)
        await subject.on_advice_ready(advice_for(strike.event_id))
        assert floor.showing_advice

        await subject.on_ball_detected(BallDetected())
        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

        assert not floor.showing_advice

    async def test_advice_that_arrives_after_ball_ready_is_not_shown(self) -> None:
        # The stale-answer half of the same rule. A model still working on the
        # previous swing when the next ball goes down has missed its window,
        # and putting its answer up now would cover the address information.
        floor = FloorSpy()
        subject = flow(floor)
        strike = ShotDetected(trigger_id="16")
        await subject.on_shot_detected(strike)
        await subject.on_ball_detected(BallDetected())
        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

        await subject.on_advice_ready(advice_for(strike.event_id))

        assert not floor.showing_advice

    async def test_the_next_strike_retires_the_last_line(self) -> None:
        floor = FloorSpy()
        subject = flow(floor, announcements=Announcements())
        first = ShotDetected(trigger_id="16")
        await subject.on_shot_detected(first)
        await subject.on_advice_ready(advice_for(first.event_id))

        await subject.on_shot_detected(ShotDetected(trigger_id="17"))
        assert not floor.showing_advice

        await subject.on_advice_ready(advice_for(first.event_id))
        assert not floor.showing_advice, "A's late line must not cover strike B"

    async def test_the_same_trigger_logged_twice_is_one_strike(self) -> None:
        floor = FloorSpy()
        subject = flow(floor)
        strike = ShotDetected(trigger_id="16")
        await subject.on_shot_detected(strike)
        await subject.on_advice_ready(advice_for(strike.event_id))

        await subject.on_shot_detected(ShotDetected(trigger_id="16"))

        assert floor.showing_advice

    async def test_coaching_drops_strike_a_once_strike_b_is_detected(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        provider = ScriptedProvider(replies=(a_reply(),))
        coach = Coach(provider)
        await coach.service.open()
        await coach.service.on_club_selected(ClubSelected(club=DRIVER))
        old = ShotDetected(trigger_id="16")
        await coach.service.on_shot_detected(old)
        await coach.service.on_shot_detected(ShotDetected(trigger_id="17"))
        stale = a_capture_on_disk(tmp_path, strike_id=old.event_id, trigger_id="16")
        await coach.service.on_swing_frames(SwingFramesReady(capture=stale))
        await coach.service.aclose()

        assert provider.calls == 0
        assert any(
            getattr(record, "coaching_reason", None) == "superseded"
            for record in caplog.records
        )

    async def test_speech_about_strike_a_is_dropped_by_strike_b(self) -> None:
        gate = asyncio.Event()
        first = uuid4()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.say("Playing now.", perishable=False)
            for _ in range(20):
                await asyncio.sleep(0)
            await table.service.on_shot_detected(ShotDetected(trigger_id="16"))
            await table.say("About strike A.", shot_id=first)
            await table.service.on_shot_detected(ShotDetected(trigger_id="17"))
            gate.set()
            await table.settled()

        spoken = [text for text, _ in table.engine.calls]
        assert "About strike A." not in spoken

    async def test_gspro_writing_the_same_strike_late_keeps_its_line(self) -> None:
        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.say("Playing now.", perishable=False)
            for _ in range(20):
                await asyncio.sleep(0)
            strike = ShotDetected(trigger_id="16")
            await table.service.on_shot_detected(strike)
            await table.say("About this strike.", shot_id=strike.event_id)
            await table.service.on_shot_completed(ShotCompleted(shot=a_shot(uuid4())))
            gate.set()
            await table.settled()

        spoken = [text for text, _ in table.engine.calls]
        assert "About this strike." in spoken


# 14 ---------------------------------------------------------------------------


class TestFailuresNeverReachPlay:
    async def test_an_openai_timeout_is_an_explained_failure(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        coach = Coach(SilentProvider(), timeout_seconds=0.05, total_budget_seconds=0.2)

        await coach.run(tmp_path)

        assert coach.bus.of(AnalysisFailed)
        assert not coach.bus.of(AnalysisCompleted)
        assert coach.results, "the failure is recorded, not silent"

    async def test_an_openai_outage_is_an_explained_failure(self, tmp_path) -> None:
        coach = Coach(BrokenProvider(error=ProviderUnavailableError("503")))

        await coach.run(tmp_path)

        assert coach.bus.of(AnalysisFailed)
        assert coach.results

    async def test_no_provider_is_no_coaching_with_a_reason(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        coach = Coach(None)

        await coach.run(tmp_path)

        reasons = [
            getattr(record, "coaching_reason", None)
            for record in caplog.records
            if record.getMessage() == "coaching_no_advice"
        ]
        assert reasons == ["no_provider"]

    async def test_a_failed_analysis_only_takes_down_the_pending_mark(self) -> None:
        floor = FloorSpy()
        subject = flow(floor)
        strike = ShotDetected(trigger_id="16")
        await subject.on_shot_detected(strike)
        await subject.on_analysis_started(
            AnalysisStarted(
                shot_id=strike.event_id,
                focus=CoachingFocus.NONE,
                frames=9,
                model="m",
            )
        )
        assert floor.calls[-1] == "show_advice_pending"

        await subject.on_analysis_failed(
            AnalysisFailed(shot_id=strike.event_id, reason=AdviceFailure.TIMED_OUT)
        )
        assert floor.calls[-1] == "clear_advice"

        calls = len(floor.calls)
        await subject.on_ball_detected(BallDetected())
        assert "clear_advice" not in floor.calls[calls:]
        assert floor.showing_address is False or "show_address" in floor.calls

    async def test_an_obs_failure_still_publishes_the_capture(self, tmp_path) -> None:
        obs = StubObs(failing_sources=frozenset({ALL_VALUES}))

        capture = await one_capture(tmp_path, obs)

        assert capture.cameras and all(camera.frames for camera in capture.cameras)
        assert capture.metrics is not None and not capture.metrics.ok


# 15 ---------------------------------------------------------------------------


class TestSessionHistory:
    async def test_a_coached_strike_is_a_row_with_provenance_and_nulls(
        self, tmp_path
    ) -> None:
        history = HistoryService(database=Database(tmp_path / "history.db"))
        await history.open()
        provider = ScriptedProvider(replies=(a_reply(),))
        coach = Coach(provider)
        coach.service._record_coach = history.record_coach_result  # noqa: SLF001

        detected = await coach.run(tmp_path)
        rows = await history.coach_results()
        await history.aclose()

        assert len(rows) == 1
        row = rows[0]
        assert row["strike_id"] == str(detected.event_id)
        assert row["status"] == "ok"
        assert row["cue_id"] == "FACE_OPEN"
        assert row["focus"] == CoachingFocus.FACE_CONTROL.value
        assert row["metrics_provenance"] == "protee_vx_all_values_visual"
        stored = json.loads(str(row["metrics_json"]))
        assert stored["metrics"]["face_angle"] == {
            "value": 6.5,
            "unit": "deg",
            "direction": "open",
        }
        assert stored["metrics"]["smash_factor"] is None
        assert row["input_images"] == 1, "one evidence sheet"
        assert row["prompt_version"] == PROTEE_PROMPT_VERSION


# 16 ---------------------------------------------------------------------------


STATIC = Path(__file__).parents[2] / "src" / "caddiej" / "adapters" / "hud" / "static"


class TestAdviceAndBallStatusCoexist:
    def test_the_advice_line_is_not_inside_the_floor_svg(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")

        assert html.index('id="ball-status"') < html.index("</svg>")
        assert html.index('id="advice"') > html.index("</svg>")

    def test_no_rule_hides_the_advice_line_behind_ball_status(self) -> None:
        css = (STATIC / "css" / "floor.css").read_text(encoding="utf-8")

        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            if re.search(r"\.advice(?![\w-])", selector) and "ball-status" in selector:
                assert "display: none" not in body
                assert "visibility: hidden" not in body


# 18 ---------------------------------------------------------------------------


class ConcurrentClock(Clock):
    """The fake clock, except the All Values task's waits do not move it.

    The real still is taken while the burst is running. A shared fake clock
    that jumped three seconds for it would end the burst's window early.
    """

    async def sleep(self, seconds: float) -> None:
        task = asyncio.current_task()
        if task is not None and task.get_name() == "coach-metrics-capture":
            self.slept.append(seconds)
            await asyncio.sleep(0)
            return
        await super().sleep(seconds)


async def one_capture(
    tmp_path: Path,
    obs: StubObs,
    *,
    delay: float = 3.0,
    shots: int = 1,
) -> SwingCapture:
    """Run swing capture with an All Values source on the fake clock."""

    clock = ConcurrentClock()
    published: list[object] = []
    obs.frames_total = 10

    async def publish(event: object) -> None:
        published.append(event)

    service = SwingCaptureService(
        lambda: obs,
        ("CaddieJ Swing Camera 1", "CaddieJ Swing Camera 2"),
        LatestSwingStore(tmp_path / "media" / "latest_swing"),
        publish,
        enabled=True,
        initial_delay_seconds=0.5,
        duration_seconds=1.0,
        fps=10.0,
        selected_frames=4,
        metrics_source=ALL_VALUES,
        metrics_delay_seconds=delay,
        command_timeout=1.0,
        shutdown_timeout=0.5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    for shot in range(shots):
        await service.on_shot_detected(ShotDetected(trigger_id=str(16 + shot)))
        task = service._task  # noqa: SLF001
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
    ready = [event for event in published if isinstance(event, SwingFramesReady)]
    assert ready
    one_capture.clock = clock  # type: ignore[attr-defined]
    one_capture.obs = obs  # type: ignore[attr-defined]
    return ready[-1].capture


class SameGrid(StubObs):
    """ProTee not having redrawn: the All Values still never changes."""

    async def source_screenshot(self, source_name: str, *, width=None) -> bytes:
        if source_name == ALL_VALUES:
            self.requests.append(source_name)
            self.widths.append(width)
            return a_still(77)
        return await super().source_screenshot(source_name, width=width)


class TestTheAllValuesStill:
    async def test_one_still_is_taken_at_the_delay_outside_the_frame_budget(
        self, tmp_path
    ) -> None:
        obs = StubObs()

        capture = await one_capture(tmp_path, obs, delay=3.0)

        assert obs.requests.count(ALL_VALUES) == 1
        assert sum(len(camera.frames) for camera in capture.cameras) == 4
        assert capture.metrics is not None and capture.metrics.ok
        assert capture.metrics.delay_seconds == 3.0
        assert 3.0 in one_capture.clock.slept  # type: ignore[attr-defined]
        assert (capture.directory / capture.metrics.path).is_file()  # type: ignore[operator]

    async def test_it_is_taken_at_full_resolution(self, tmp_path) -> None:
        obs = StubObs()

        await one_capture(tmp_path, obs)

        index = obs.requests.index(ALL_VALUES)
        assert obs.widths[index] is None

    async def test_metadata_says_it_is_metrics_evidence_and_not_a_frame(
        self, tmp_path
    ) -> None:
        capture = await one_capture(tmp_path, StubObs())

        payload = json.loads(
            (capture.directory / METADATA_NAME).read_text(encoding="utf-8")
        )
        metrics = payload["coach_metrics"]
        assert metrics["ok"] is True
        assert metrics["counts_against_selected_frames"] is False
        assert metrics["provenance"] == "protee_vx_all_values_visual"
        assert metrics["file"] == "metrics/all_values.jpg"
        assert payload["strike_id"] == str(capture.strike_id)

    async def test_the_capture_round_trips_from_disk(self, tmp_path) -> None:
        capture = await one_capture(tmp_path, StubObs())

        loaded = load_capture(capture.directory)

        assert loaded is not None
        assert loaded.strike_id == capture.strike_id
        assert loaded.metrics is not None and loaded.metrics.ok
        assert loaded.metrics.path == capture.metrics.path  # type: ignore[union-attr]
        assert [len(c.frames) for c in loaded.cameras] == [
            len(c.frames) for c in capture.cameras
        ]

    async def test_a_grid_identical_to_the_last_shots_is_refused(
        self, tmp_path
    ) -> None:
        obs = SameGrid()

        capture = await one_capture(tmp_path, obs, shots=2)

        assert capture.metrics is not None
        assert not capture.metrics.ok
        assert "identical" in (capture.metrics.detail or "")
        assert obs.requests.count(ALL_VALUES) == 3, "first shot once, second twice"

    async def test_a_missing_source_is_not_retried(self, tmp_path) -> None:
        obs = StubObs(failing_sources=frozenset({ALL_VALUES}))

        capture = await one_capture(tmp_path, obs)

        assert obs.requests.count(ALL_VALUES) == 1
        assert capture.metrics is not None
        assert "no source named" in (capture.metrics.detail or "")


# --check-coach ---------------------------------------------------------------


class TestTheCoachCheck:
    async def test_it_runs_the_real_protee_path_against_the_last_capture(
        self, tmp_path
    ) -> None:
        provider = ScriptedProvider(replies=(a_reply(),))
        capture = a_capture_on_disk(tmp_path, strike_id=uuid4())

        probe = await probe_protee_coach(
            provider_name="openai",
            provider=provider,
            capture=capture,
            evidence_sheet=placeholder_sheet,
        )

        assert probe.ok
        assert provider.calls == 1
        assert len(provider.requests[0].images) == 1
        assert len(sheet_cells(provider.requests[0].images[0].data)) == 9
        assert "API images           1" in probe.report
        assert "sheet                ok" in probe.report
        assert "GSPro was not used" in probe.report
        assert "face_angle" in probe.report

    async def test_the_swing_capture_check_photographs_all_values_too(
        self, tmp_path
    ) -> None:
        from caddiej.swing_capture import probe_swing_capture

        obs = StubObs()
        probe = await probe_swing_capture(
            host="127.0.0.1",
            port=4455,
            password="not-printed",
            connect_timeout=1.0,
            sources=("CaddieJ Swing Camera 1", "CaddieJ Swing Camera 2"),
            output_root=tmp_path / "out",
            client_factory=lambda: obs,
            metrics_source=ALL_VALUES,
        )

        written = {path.name for path in (tmp_path / "out").rglob("*.jpg")}
        assert probe.ok
        assert "coach_metrics.jpg" in written
        assert "All Values" in probe.report
        assert obs.widths[obs.requests.index(ALL_VALUES)] is None
        assert "not-printed" not in probe.report

    async def test_no_capture_on_disk_says_how_to_make_one(self) -> None:
        probe = await probe_protee_coach(
            provider_name="openai", provider=ScriptedProvider(), capture=None
        )

        assert not probe.ok
        assert "Hit one ball" in probe.report
