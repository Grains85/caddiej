"""Shot reports, contact sheets, and session export.

These tests do not call a model. They check that a report says what was
observed, and that explaining a ranking does not change it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from PIL import Image

from caddiej.diagnostics.decision import explain_checkpoints
from caddiej.diagnostics.export import export_session
from caddiej.diagnostics.journal import DiagnosticJournal
from caddiej.diagnostics.store import safe_segment, scrub
from caddiej.swing_reference.live import ReferenceLiveCoach
from caddiej.swing_reference.rank import rank_findings

_NOW = datetime(2026, 10, 18, 18, 0, tzinfo=UTC)


class _Checkpoint:
    def __init__(self, mode: str) -> None:
        self.mode = mode


class _Reference:
    def __init__(self) -> None:
        self.checkpoints = {
            "a": _Checkpoint("visual"),
            "b": _Checkpoint("visual"),
            "pose": _Checkpoint("numeric"),
        }


def _findings() -> dict[str, dict[str, object]]:
    return {
        "a": {"status": "deviation", "certainty": "high", "severity": "large"},
        "b": {"status": "deviation", "certainty": "medium", "severity": "minor"},
        "pose": {"status": "deviation", "certainty": "high", "severity": "large"},
        "gone": {"status": "near_reference", "certainty": "low", "severity": "clear"},
    }


def _jpeg(path: Path, color: tuple[int, int, int]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), color).save(path, format="JPEG")
    return path.read_bytes()


def _ball() -> SimpleNamespace:
    ball = SimpleNamespace(
        speed_mph=95.0,
        launch_angle_degrees=None,
        launch_direction_degrees=None,
        total_spin_rpm=None,
        spin_axis_degrees=None,
        back_spin_rpm=None,
        side_spin_rpm=None,
        carry_yards=152.0,
    )
    return ball


def _shot(shot_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        shot_id=shot_id,
        club=SimpleNamespace(kind=SimpleNamespace(value="iron"), vendor_code="7I"),
        ball=_ball(),
        club_delivery=None,
        handedness=SimpleNamespace(value="right"),
    )


def _camera(camera_id: str, *, ok: bool, source: str = "panel") -> SimpleNamespace:
    return SimpleNamespace(
        camera_id=camera_id,
        source=source,
        ok=ok,
        captured=8 if ok else 0,
        frames=(1, 2) if ok else (),
        failed=0 if ok else 1,
        detail=None if ok else f"OBS has no source named {source!r}",
        view=SimpleNamespace(value=camera_id),
    )


def _frame(camera_id: str, name: str, tick: int, stage: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        reference=SimpleNamespace(
            camera_id=camera_id,
            relative_file=name,
            capture_tick=tick,
            timestamp_seconds=tick / 10,
            sha256="abc",
        ),
        stage=stage,
        selection_reason="motion peak",
    )


def _package(
    *frames: SimpleNamespace, status: str = "ok", errors: tuple[str, ...] = ()
) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        fallback="",
        errors=errors,
        warnings=(),
        selected_frames=frames,
        stage_estimates=(
            SimpleNamespace(
                camera_id="swing_behind",
                status="partial",
                unresolved_stages=("impact",),
                boundaries=(
                    SimpleNamespace(
                        stage="top",
                        time_interval_seconds=(0.2, 0.4),
                        confidence=0.4,
                        confidence_scale="uncalibrated_heuristic",
                    ),
                ),
            ),
        ),
        impact_estimate=SimpleNamespace(
            status="estimated",
            classification="inferred_contact_interval",
            confidence=0.3,
            confidence_scale="uncalibrated_heuristic",
            method="motion",
            camera_id="swing_behind",
            contact_interval_ticks=(2, 4),
            contact_interval_seconds=(0.2, 0.4),
            reason="the ball left the tee area",
        ),
    )


def _result(**overrides: object) -> SimpleNamespace:
    payload = {
        "assessment": {
            "findings": [
                {
                    "checkpoint_id": "a",
                    "status": "deviation",
                    "certainty": "high",
                    "severity": "large",
                    "observation": "the hips start early",
                }
            ]
        },
        "errors": (),
        "recovery": (),
        "decision_status": "ok",
        "decision_type": "START_FOCUS",
        "reason": "the largest deviation is new",
        "active_focus": "a",
        "rule_id": "rule-a",
        "hud_text": "Hips",
        "speech_text": "Turn the hips a little sooner.",
        "state": SimpleNamespace(progress="start"),
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _journal(tmp_path: Path, **kwargs: object) -> DiagnosticJournal:
    return DiagnosticJournal(tmp_path, session_id="session-1", **kwargs)  # type: ignore[arg-type]


def _read(journal: DiagnosticJournal, shot_id: str) -> tuple[dict[str, object], str]:
    journal.flush()
    directory = tmp_shot(journal, shot_id)
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    return report, (directory / "report.md").read_text(encoding="utf-8")


def tmp_shot(journal: DiagnosticJournal, shot_id: str) -> Path:
    from caddiej.diagnostics.store import shot_dir

    return shot_dir(journal._root, journal.session_id, shot_id)


def test_explaining_a_ranking_does_not_change_it() -> None:
    reference = _Reference()
    findings = _findings()
    before = rank_findings(reference, findings)  # type: ignore[arg-type]
    snapshot = json.dumps(findings, sort_keys=True)
    explained = explain_checkpoints(reference, findings)
    after = rank_findings(reference, findings)  # type: ignore[arg-type]

    assert after == before
    assert json.dumps(findings, sort_keys=True) == snapshot
    assert explained["primary_checkpoint_id"] == "a"
    by_id = {row["checkpoint_id"]: row for row in explained["checkpoints"]}
    assert by_id["b"]["eligible"] is True
    assert by_id["b"]["exclusion_reason"] is None
    assert "not visual" in by_id["pose"]["exclusion_reason"]
    assert "not in the reference" in by_id["gone"]["exclusion_reason"]


def test_a_successful_shot_reports_what_was_delivered(tmp_path: Path) -> None:
    strike = str(uuid4())
    capture_id = str(uuid4())
    simulator = str(uuid4())
    _jpeg(tmp_path / "bay" / "dtl.jpg", (20, 40, 60))
    _jpeg(tmp_path / "bay" / "fo.jpg", (60, 40, 20))
    original = (tmp_path / "bay" / "dtl.jpg").read_bytes()
    journal = _journal(tmp_path / "diagnostics")
    journal.note_strike(
        strike_id=strike,
        trigger_id="4",
        monotonic_seconds=1.0,
        occurred_at=_NOW,
    )
    journal.note_simulator_shot(
        _shot(simulator),
        "4",
        monotonic_seconds=1.2,
        occurred_at=_NOW,
    )
    journal.note_capture(
        SimpleNamespace(
            coaching_id=strike,
            capture_id=capture_id,
            trigger_id="4",
            strike_id=strike,
            directory=tmp_path / "bay",
            cameras=(
                _camera("swing_behind", ok=True, source="Swing Behind"),
                _camera("swing_side", ok=True, source="Swing Side"),
            ),
            started_at=_NOW,
            detected_at=_NOW,
        )
    )
    journal.note_evidence(
        strike,
        _package(
            _frame("swing_behind", "dtl.jpg", 3, None),
            _frame("swing_side", "fo.jpg", 4, "top"),
        ),
        tmp_path / "bay",
    )
    journal.note_evidence_timing(
        strike, {"timeline_seconds": 0.04, "pose_seconds": None}
    )
    journal.note_trace(
        SimpleNamespace(
            shot_id=strike,
            simulated=False,
            capture_id=capture_id,
            errors=(),
            marks={
                "shot_completed": 1.2,
                "swing_frames_ready": 2.0,
                "evidence_started": 2.1,
                "evidence_completed": 2.5,
                "vision_started": 2.6,
                "vision_received": 3.6,
                "assessment_validated": 3.7,
                "coaching_decision_completed": 3.8,
                "tts_requested": 3.9,
                "tts_started": 4.2,
                "tts_completed": 6.0,
                "hud_shown": 3.85,
            },
        )
    )
    journal.note_gpt(
        strike,
        prepared=SimpleNamespace(
            reference_version="2.9",
            reference_prefix="prefix",
            system="system",
            instructions="instructions",
            manifest={"shot_id": strike},
            images=(
                SimpleNamespace(
                    image_id="DTL_001",
                    camera_id="swing_behind",
                    relative_file="dtl.jpg",
                    capture_tick=3,
                    timestamp_seconds=0.3,
                    data=b"jpeg",
                    detail="high",
                ),
            ),
        ),
        reply=SimpleNamespace(
            model="gpt-5-mini",
            payload={"findings": []},
            input_tokens=10,
            output_tokens=4,
            cached_tokens=None,
            latency_seconds=1.0,
        ),
        provider="openai",
        simulated=False,
        checkpoints=["a", "b"],
        image_detail="high",
    )
    journal.note_assessment(
        strike,
        result=_result(),
        approved_correction="Turn the hips a little sooner.",
        previous_focus=None,
        previous=SimpleNamespace(
            active_focus="old",
            delivery_status="not_delivered",
            previous_cue="this was not confirmed",
        ),
        reference=_Reference(),
        observation="the hips start early",
        observation_included=True,
    )
    journal.note_hud(strike, "success", "Hips", "shown on the floor")
    journal.note_tts(strike, status="success", detail="queued", monotonic_seconds=3.9)
    journal.note_playback_started(strike)
    journal.note_playback_finished(
        strike, outcome="spoken", spoken_seconds=1.8, detail=None
    )
    report, markdown = _read(journal, strike)

    assert report["schema_version"] == "1"
    assert report["status"] == "success"
    identity = report["identity"]
    assert identity["shot_id"] == strike
    assert identity["capture_id"] == capture_id
    assert identity["trigger_id"] == "4"
    assert identity["simulator_shot_id"] == simulator
    assert report["measurements"]["present"]["ball.carry_yards"] == 152.0
    assert "ball.launch_angle_degrees" in report["measurements"]["missing"]
    assert "club_delivery" in report["measurements"]["missing"]
    assert report["decision"]["previous_cue"] is None
    assert report["decision"]["speech_text"] == "Turn the hips a little sooner."
    assert report["delivery"]["tts_playback"]["status"] == "success"
    assert report["delivery"]["hud"]["status"] == "success"
    assert report["evidence"]["impact"]["quality"] == "estimated"
    assert report["contact_sheet"]["status"] == "success"
    audio = _span(report, "shot_to_first_audio")
    assert audio["quality"] == "measured"
    assert audio["milliseconds"] == 3200
    assert "Turn the hips a little sooner." in markdown
    assert "this was not confirmed" not in markdown
    assert (tmp_path / "bay" / "dtl.jpg").read_bytes() == original
    payload = json.loads(
        (tmp_shot(journal, strike) / "gpt_request.json").read_text(encoding="utf-8")
    )
    assert payload["retained"] is True
    assert "data" not in payload["body"]["images"][0]
    assert payload["body"]["images"][0]["bytes"] == 4
    journal.close()


def test_a_missing_camera_is_a_capture_failure(tmp_path: Path) -> None:
    strike = str(uuid4())
    journal = _journal(tmp_path)
    journal.note_capture(
        SimpleNamespace(
            coaching_id=strike,
            capture_id=str(uuid4()),
            trigger_id="1",
            strike_id=strike,
            directory=tmp_path / "bay",
            cameras=(
                _camera("swing_behind", ok=True),
                _camera("swing_side", ok=False, source="Swing Side"),
            ),
            started_at=_NOW,
            detected_at=_NOW,
        )
    )
    report, markdown = _read(journal, strike)

    assert report["capture"]["status"] == "failed"
    assert "swing_side" in report["capture"]["detail"]
    assert "missing camera" in markdown
    assert report["status"] != "success"
    journal.close()


def test_unresolved_stages_are_named_and_not_invented(tmp_path: Path) -> None:
    strike = str(uuid4())
    bay = tmp_path / "bay"
    _jpeg(bay / "dtl.jpg", (1, 2, 3))
    journal = _journal(tmp_path / "diagnostics")
    journal.note_evidence(
        strike,
        _package(_frame("swing_behind", "dtl.jpg", 3, None)),
        bay,
    )
    report, markdown = _read(journal, strike)
    frame = report["evidence"]["selected_frames"][0]

    assert frame["stage"] is None
    assert report["evidence"]["stages"][0]["unresolved_stages"] == ["impact"]
    assert "stage unknown" in markdown
    assert "impact" in markdown
    journal.close()


def test_failed_evidence_is_not_a_successful_assessment(tmp_path: Path) -> None:
    strike = str(uuid4())
    journal = _journal(tmp_path)
    journal.note_evidence(
        strike,
        _package(status="failed", errors=("pose model missing",)),
        tmp_path,
    )
    journal.note_failure(strike, "failed", "pose model missing")
    report, _markdown = _read(journal, strike)

    assert report["evidence"]["status"] == "failed"
    assert report["status"] == "failed"
    assert report["gpt"]["status"] == "skipped"
    journal.close()


def test_a_provider_timeout_is_a_failed_request(tmp_path: Path) -> None:
    strike = str(uuid4())
    journal = _journal(tmp_path)
    journal.note_gpt(
        strike,
        prepared=None,
        reply=None,
        provider="openai",
        simulated=False,
        checkpoints=["a"],
        error="ProviderTimeoutError: timed out",
    )
    journal.note_failure(strike, "failed", "ProviderTimeoutError")
    report, markdown = _read(journal, strike)

    assert report["gpt"]["status"] == "failed"
    assert report["gpt"]["validation_status"] == "skipped"
    assert report["status"] == "failed"
    assert "ProviderTimeoutError" in markdown
    assert report["delivery"]["tts_playback"]["status"] != "success"
    journal.close()


def test_an_invalid_assessment_rejects_the_findings(tmp_path: Path) -> None:
    strike = str(uuid4())
    journal = _journal(tmp_path)
    journal.note_gpt(
        strike,
        prepared=None,
        reply=SimpleNamespace(
            model="gpt-5-mini",
            payload={},
            input_tokens=1,
            output_tokens=1,
            cached_tokens=None,
            latency_seconds=0.2,
        ),
        provider="openai",
        simulated=False,
        checkpoints=["a"],
        image_detail="high",
    )
    journal.note_assessment(
        strike,
        result=_result(
            errors=("ranking does not match severity",),
            decision_status="held",
            hud_text="",
            speech_text="",
            active_focus=None,
        ),
        approved_correction="",
        previous_focus=None,
        previous=None,
        reference=_Reference(),
        observation="",
        observation_included=False,
    )
    report, markdown = _read(journal, strike)

    assert report["status"] == "failed"
    assert report["gpt"]["status"] == "success"
    assert report["gpt"]["validation_status"] == "failed"
    assert report["gpt"]["accepted_findings"] == []
    assert report["gpt"]["rejected_findings"]
    assert "ranking does not match severity" in markdown
    journal.close()


def test_a_superseded_shot_is_not_marked_successful(tmp_path: Path) -> None:
    strike = str(uuid4())
    journal = _journal(tmp_path)
    journal.note_gpt(
        strike,
        prepared=None,
        reply=SimpleNamespace(
            model="gpt-5-mini",
            payload={},
            input_tokens=None,
            output_tokens=None,
            cached_tokens=None,
            latency_seconds=None,
        ),
        provider="openai",
        simulated=False,
        checkpoints=[],
    )
    journal.note_failure(strike, "superseded", "a newer shot arrived")
    report, _markdown = _read(journal, strike)

    assert report["status"] == "superseded"
    assert report["decision"]["status"] != "success"
    assert report["delivery"]["hud"]["status"] == "superseded"
    journal.close()


def test_tts_failure_is_not_spoken_audio(tmp_path: Path) -> None:
    strike = str(uuid4())
    journal = _journal(tmp_path)
    journal.note_tts(strike, status="success", detail="queued", monotonic_seconds=1.0)
    journal.note_playback_finished(
        strike, outcome="failed", spoken_seconds=0.0, detail="the engine failed"
    )
    report, markdown = _read(journal, strike)

    assert report["delivery"]["tts_request"]["status"] == "success"
    assert report["delivery"]["tts_playback"]["status"] == "failed"
    assert report["delivery"]["tts_playback"]["spoken_seconds"] is None
    assert "Audio playback was not confirmed." in markdown
    journal.close()


def test_a_request_without_playback_is_not_confirmed(tmp_path: Path) -> None:
    strike = str(uuid4())
    journal = _journal(tmp_path)
    journal.note_assessment(
        strike,
        result=_result(),
        approved_correction="Turn the hips a little sooner.",
        previous_focus=None,
        previous=None,
        reference=_Reference(),
        observation="",
        observation_included=False,
    )
    journal.note_tts(strike, status="success", detail="queued", monotonic_seconds=1.0)
    report, markdown = _read(journal, strike)

    assert report["delivery"]["tts_playback"]["status"] == "unavailable"
    assert "speech was requested; playback did not start" in markdown
    assert "Audio playback was not confirmed." in markdown
    journal.close()


def test_a_cancelled_capture_stays_cancelled(tmp_path: Path) -> None:
    strike = str(uuid4())
    journal = _journal(tmp_path)
    journal.note_capture_outcome(
        status="cancelled",
        reason="capture was cancelled",
        trigger_id="9",
        strike_id=strike,
        capture_id=str(uuid4()),
    )
    report, _markdown = _read(journal, strike)

    assert report["status"] == "cancelled"
    assert report["capture"]["status"] == "cancelled"
    journal.close()


def test_a_partial_session_does_not_treat_missing_times_as_zero(tmp_path: Path) -> None:
    finished = str(uuid4())
    pending = str(uuid4())
    journal = _journal(tmp_path)
    journal.note_trace(
        SimpleNamespace(
            shot_id=finished,
            simulated=False,
            capture_id="",
            errors=(),
            marks={"shot_detected": 0.0, "tts_started": 2.0},
        )
    )
    journal.note_assessment(
        finished,
        result=_result(),
        approved_correction="Turn the hips a little sooner.",
        previous_focus=None,
        previous=None,
        reference=_Reference(),
        observation="",
        observation_included=False,
    )
    journal.note_strike(
        strike_id=pending,
        trigger_id="2",
        monotonic_seconds=5.0,
        occurred_at=_NOW,
    )
    journal.close()
    summary = json.loads(
        (tmp_path / "session-1" / "session.json").read_text(encoding="utf-8")
    )
    text = (tmp_path / "session-1" / "summary.md").read_text(encoding="utf-8")

    assert summary["shots"] == 2
    assert summary["successfully_assessed"] == 1
    assert summary["unfinished"] + summary["skipped"] == 1
    assert summary["without_feedback"] == 2
    stats = summary["timing"]["shot_to_first_audio"]
    assert stats["sample_size"] == 1
    assert stats["median_ms"] == 2000
    assert stats["p95_ms"] == 2000
    assert "sample" in text.lower() or "| shot_to_first_audio | 1 |" in text


def test_export_uses_windows_safe_names_and_skips_secrets(tmp_path: Path) -> None:
    strike = "shot:1"
    bay = tmp_path / "bay"
    _jpeg(bay / "dtl.jpg", (8, 8, 8))
    journal = _journal(tmp_path / "diagnostics", retain_payloads=True)
    journal.note_capture(
        SimpleNamespace(
            coaching_id=strike,
            capture_id="capture-1",
            trigger_id="1",
            strike_id=strike,
            directory=bay,
            cameras=(_camera("swing_behind", ok=True, source="Swing Behind"),),
            started_at=_NOW,
            detected_at=_NOW,
        )
    )
    journal.note_evidence(
        strike,
        _package(
            _frame("swing_behind", "dtl.jpg", 1, None),
            _frame("swing_behind", "gone.jpg", 2, None),
        ),
        bay,
    )
    journal.note_gpt(
        strike,
        prepared=SimpleNamespace(
            reference_version="2.9",
            reference_prefix="prefix",
            system="Bearer sk-live-secret-value",
            instructions="instructions",
            manifest={"authorization": "secret", "shot_id": strike},
            images=(),
        ),
        reply=SimpleNamespace(
            model="gpt-5-mini",
            payload={"api_key": "sk-live-secret-value", "ok": True},
            input_tokens=3,
            output_tokens=1,
            cached_tokens=None,
            latency_seconds=0.1,
        ),
        provider="openai",
        simulated=False,
        checkpoints=["a"],
        image_detail="high",
    )
    journal.flush()
    session = tmp_path / "diagnostics" / "session-1"
    (session / ".env").write_text(
        "COACHING_API_KEY=sk-live-secret-value", encoding="utf-8"
    )
    exported = export_session(
        tmp_path / "diagnostics",
        tmp_path / "out" / "session.zip",
        include_images=True,
    )
    import zipfile

    with zipfile.ZipFile(exported) as archive:
        names = archive.namelist()
        request = json.loads(archive.read("shots/shot_1/gpt_request.json"))
        response = json.loads(archive.read("shots/shot_1/gpt_response.json"))
        image = archive.read("images/shot_1/dtl.jpg")
        missing = archive.read("missing.txt").decode("utf-8")

    assert all("\\" not in name for name in names)
    assert "shots/shot_1/report.json" in names
    assert not any(name.endswith(".env") or "/.env" in name for name in names)
    assert "sk-live" not in request["body"]["system"]
    assert request["body"]["evidence"]["authorization"] == "[redacted]"
    assert response["body"]["api_key"] == "[redacted]"
    assert "gone.jpg" in missing
    assert b"sk-live" not in image
    segment = safe_segment(r"..\shot:1")
    assert ":" not in segment
    assert "\\" not in segment
    assert not segment.startswith("..")
    journal.close()


def test_retention_drops_old_sessions_and_shots(tmp_path: Path) -> None:
    first = DiagnosticJournal(
        tmp_path, session_id="older", keep_sessions=1, keep_shots=1
    )
    first.note_strike(
        strike_id="one", trigger_id="1", monotonic_seconds=1.0, occurred_at=_NOW
    )
    first.flush()
    first.close()
    assert (tmp_path / "older" / "session.json").is_file()

    current = DiagnosticJournal(
        tmp_path, session_id="current", keep_sessions=1, keep_shots=1
    )
    current.note_strike(
        strike_id="a", trigger_id="1", monotonic_seconds=1.0, occurred_at=_NOW
    )
    current.note_strike(
        strike_id="b", trigger_id="2", monotonic_seconds=2.0, occurred_at=_NOW
    )
    current.flush()

    assert not (tmp_path / "older").exists()
    assert (tmp_path / "current" / "shots" / "b" / "report.json").is_file()
    assert not (tmp_path / "current" / "shots" / "a").exists()
    current.close()


def test_payload_retention_is_opt_in_and_scrubs_keys() -> None:
    assert scrub({"input_tokens": 3, "api_key": "sk-abcdefghijklmnopqrst"}) == {
        "input_tokens": 3,
        "api_key": "[redacted]",
    }


def test_a_raising_journal_does_not_stop_the_coach() -> None:
    class Boom:
        def note_trace(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("disk full")

    coach = ReferenceLiveCoach(None, None, vision_calls=False, diagnostics=Boom())
    shot_id = uuid4()
    coach._trace(shot_id)
    coach._log_trace(shot_id)


def test_simulated_gpt_latency_is_estimated(tmp_path: Path) -> None:
    strike = str(uuid4())
    journal = _journal(tmp_path)
    journal.note_trace(
        SimpleNamespace(
            shot_id=strike,
            simulated=True,
            capture_id="",
            errors=(),
            marks={"vision_started": 1.0, "vision_received": 1.4},
        )
    )
    report, markdown = _read(journal, strike)
    span = _span(report, "gpt_latency")

    assert span["quality"] == "estimated"
    assert span["milliseconds"] == 400
    assert "simulated" in markdown.lower()
    journal.close()


def test_check_config_shows_where_the_latest_zip_is_written(
    tmp_path: Path, monkeypatch
) -> None:
    from caddiej.cli import describe
    from caddiej.config import Settings

    monkeypatch.chdir(tmp_path)
    for name in (
        "DIAGNOSTICS_ENABLED",
        "DIAGNOSTICS_RETAIN_PAYLOADS",
        "DIAGNOSTICS_KEEP_SHOTS",
        "DIAGNOSTICS_AUTO_EXPORT_LATEST",
        "DIAGNOSTICS_INCLUDE_IMAGES",
        "DIAGNOSTICS_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    report = describe(Settings())

    assert "Enabled:               yes" in report
    assert "Retain GPT payloads:   yes" in report
    assert "Retained shots:        3" in report
    assert "Auto-export latest:    yes" in report
    assert "Include images:        yes" in report
    assert str(tmp_path / "media" / "diagnostics" / "latest_shot.zip") in report
    assert list(tmp_path.iterdir()) == []


def test_the_newest_finished_shot_owns_the_zip(tmp_path: Path) -> None:
    bay = tmp_path / "bay"
    journal = _journal(tmp_path / "diagnostics", keep_shots=3)
    _jpeg(bay / "older.jpg", (1, 2, 3))
    _jpeg(bay / "newer.jpg", (4, 5, 6))
    _file_shot(journal, "older", bay, "older.jpg")
    _file_shot(journal, "newer", bay, "newer.jpg")
    journal.note_failure("newer", "failed", "the model timed out")
    journal.flush()
    assert _manifest(tmp_path / "diagnostics")["shot_id"] == "newer"
    assert _manifest(tmp_path / "diagnostics")["status"] == "failed"

    journal.note_failure("older", "superseded", "finished late")
    journal.flush()
    manifest = _manifest(tmp_path / "diagnostics")

    assert manifest["shot_id"] == "newer"
    assert manifest["sequence"] == 2
    assert manifest["created_at"]
    journal.close()


def test_the_fourth_shot_drops_the_first_and_keeps_its_statistics(
    tmp_path: Path,
) -> None:
    root = tmp_path / "diagnostics"
    bay = tmp_path / "bay"
    journal = DiagnosticJournal(root, session_id="session-1", keep_shots=3)
    for index, shot in enumerate(("one", "two", "three", "four"), start=1):
        _jpeg(bay / f"{shot}.jpg", (index, index, index))
        _file_shot(journal, shot, bay, f"{shot}.jpg")
        if shot == "one":
            journal.note_trace(
                SimpleNamespace(
                    shot_id=shot,
                    simulated=False,
                    capture_id="",
                    errors=(),
                    marks={"shot_detected": 0.0, "tts_started": 1.5},
                )
            )
        journal.note_failure(shot, "failed", "stopped")
        journal.flush()
    journal.close()
    summary = json.loads(
        (root / "session-1" / "session.json").read_text(encoding="utf-8")
    )
    kept = sorted(path.name for path in (root / "session-1" / "shots").iterdir())

    assert kept == ["four", "three", "two"]
    assert summary["shots"] == 4
    assert summary["retained_shots"] == 3
    assert summary["timing"]["shot_to_first_audio"]["sample_size"] == 1
    assert summary["timing"]["shot_to_first_audio"]["median_ms"] == 1500
    assert _manifest(root)["shot_id"] == "four"
    with _zip(root) as archive:
        image = archive.read("images/swing_behind_1_four.jpg")
    assert image.startswith(b"\xff\xd8")


def test_a_shot_without_a_model_call_keeps_images_marked_not_sent(
    tmp_path: Path,
) -> None:
    bay = tmp_path / "bay"
    original = _jpeg(bay / "dtl.jpg", (9, 9, 9))
    journal = _journal(tmp_path / "diagnostics", retain_payloads=True)
    _file_shot(journal, "plain", bay, "dtl.jpg")
    journal.note_failure("plain", "failed", "model provider is not configured")
    report, markdown = _read(journal, "plain")

    frame = report["evidence"]["selected_frames"][0]
    assert frame["transmission"] == "not_sent"
    assert frame["requested"] is False
    assert "NOT SENT" in markdown
    assert report["gpt"]["status"] == "skipped"
    owned = tmp_shot(journal, "plain") / str(frame["diagnostic_file"])
    assert owned.read_bytes() == original
    with _zip(tmp_path / "diagnostics") as archive:
        names = archive.namelist()
        assert archive.read(str(frame["diagnostic_file"])) == original
    assert "gpt_response.json" not in names
    assert "gpt_request.json" not in names
    journal.close()


def test_payloads_are_omitted_when_retention_is_off(tmp_path: Path) -> None:
    journal = _journal(tmp_path, retain_payloads=False)
    journal.note_gpt(
        "shot",
        prepared=None,
        reply=SimpleNamespace(
            model="gpt-5-mini",
            payload={"api_key": "sk-abcdefghijklmnopqrst", "ok": True},
            input_tokens=1,
            output_tokens=1,
            cached_tokens=None,
            latency_seconds=0.1,
        ),
        provider="openai",
        simulated=False,
        checkpoints=["a"],
        image_detail="high",
    )
    journal.note_failure("shot", "failed", "invalid")
    journal.flush()
    with _zip(tmp_path) as archive:
        names = archive.namelist()
    assert "gpt_request.json" not in names
    assert "gpt_response.json" not in names
    journal.close()


def test_a_failed_export_is_retried_and_does_not_raise(
    tmp_path: Path, monkeypatch
) -> None:
    import caddiej.diagnostics.journal as journal_module

    real = journal_module.export_latest_shot
    journal = _journal(tmp_path)

    def broken(*_args: object, **_kwargs: object) -> Path:
        raise OSError("the destination is locked")

    monkeypatch.setattr(journal_module, "export_latest_shot", broken)
    journal.note_failure("shot", "failed", "timed out")
    journal.flush()
    assert not (tmp_path / "latest_shot.zip").exists()

    monkeypatch.setattr(journal_module, "export_latest_shot", real)
    journal.note_failure("shot", "failed", "timed out")
    journal.flush()
    assert _manifest(tmp_path)["shot_id"] == "shot"
    assert _manifest(tmp_path)["status"] == "failed"
    journal.close()


def test_a_locked_shot_directory_is_removed_on_a_later_pass(
    tmp_path: Path, monkeypatch
) -> None:
    import caddiej.diagnostics.journal as journal_module

    real = journal_module.remove_tree
    journal = DiagnosticJournal(tmp_path, session_id="session-1", keep_shots=1)
    journal.note_failure("old", "failed", "first")
    journal.flush()
    blocked = {"fail": True}

    def flaky(path: Path, root: Path) -> bool:
        if blocked["fail"] and path.name == "old":
            return False
        return real(path, root)

    monkeypatch.setattr(journal_module, "remove_tree", flaky)
    journal.note_failure("new", "failed", "second")
    journal.flush()
    assert (tmp_path / "session-1" / "shots" / "old").is_dir()

    blocked["fail"] = False
    journal.note_failure("new", "failed", "second")
    journal.flush()
    assert not (tmp_path / "session-1" / "shots" / "old").exists()
    assert (tmp_path / "session-1" / "shots" / "new").is_dir()
    journal.close()


def test_capture_cleanup_does_not_remove_the_diagnostic_copy(tmp_path: Path) -> None:
    bay = tmp_path / "bay"
    original = _jpeg(bay / "dtl.jpg", (3, 3, 3))
    journal = _journal(tmp_path / "diagnostics")
    _file_shot(journal, "kept", bay, "dtl.jpg")
    journal.flush()
    (bay / "dtl.jpg").unlink()
    journal.note_failure("kept", "failed", "capture directory was rotated")
    report, _markdown = _read(journal, "kept")
    frame = report["evidence"]["selected_frames"][0]

    assert frame["transmission"] == "not_sent"
    with _zip(tmp_path / "diagnostics") as archive:
        assert archive.read(str(frame["diagnostic_file"])) == original
    journal.close()


def test_replacing_the_latest_zip_is_atomic(tmp_path: Path, monkeypatch) -> None:
    import caddiej.diagnostics.export as export_module

    shot = tmp_path / "shot"
    shot.mkdir()
    (shot / "report.json").write_text("{}\n", encoding="utf-8")
    (shot / "report.md").write_text("# Shot\n", encoding="utf-8")
    export_module.export_latest_shot(
        tmp_path,
        shot,
        {"shot_id": "first", "status": "success", "sequence": 1, "session_id": "s"},
    )

    def reject(_path: Path) -> bool:
        return False

    monkeypatch.setattr(export_module, "_valid_latest", reject)
    try:
        export_module.export_latest_shot(
            tmp_path,
            shot,
            {"shot_id": "second", "status": "failed", "sequence": 2, "session_id": "s"},
        )
    except OSError:
        rejected = True
    else:
        rejected = False

    assert rejected
    assert _manifest(tmp_path)["shot_id"] == "first"
    assert list(tmp_path.glob(".latest_shot.*.tmp")) == []


def test_shutdown_drops_old_shots_and_abandoned_zip_temps(tmp_path: Path) -> None:
    abandoned = tmp_path / ".latest_shot.1.1.tmp"
    abandoned.write_bytes(b"partial")
    journal = DiagnosticJournal(tmp_path, session_id="session-1", keep_shots=1)
    journal.note_failure("old", "skipped", "old")
    journal.note_failure("new", "failed", "new")
    journal.close()

    assert not abandoned.exists()
    assert _manifest(tmp_path)["shot_id"] == "new"
    assert [path.name for path in (tmp_path / "session-1" / "shots").iterdir()] == [
        "new"
    ]


def test_an_image_is_sent_only_when_the_request_lists_it(tmp_path: Path) -> None:
    bay = tmp_path / "bay"
    _jpeg(bay / "dtl.jpg", (1, 1, 1))
    _jpeg(bay / "extra.jpg", (2, 2, 2))
    journal = _journal(tmp_path)
    journal.note_evidence(
        "shot",
        _package(
            _frame("swing_behind", "dtl.jpg", 3, None),
            _frame("swing_behind", "extra.jpg", 4, None),
        ),
        bay,
    )
    journal.note_gpt(
        "shot",
        prepared=SimpleNamespace(
            reference_version="2.9",
            reference_prefix="prefix",
            system="system",
            instructions="instructions",
            manifest={},
            images=(
                SimpleNamespace(
                    image_id="DTL_003",
                    camera_id="swing_behind",
                    relative_file="dtl.jpg",
                    capture_tick=3,
                    timestamp_seconds=0.3,
                    data=b"jpeg-bytes",
                    detail="high",
                ),
            ),
        ),
        reply=SimpleNamespace(
            model="gpt-5-mini",
            payload={"ok": True},
            input_tokens=2,
            output_tokens=1,
            cached_tokens=None,
            latency_seconds=0.2,
        ),
        provider="openai",
        simulated=False,
        checkpoints=["a"],
        image_detail="high",
    )
    journal.note_failure("shot", "failed", "assessment was rejected")
    report, markdown = _read(journal, "shot")
    by_file = {
        frame["relative_file"]: frame for frame in report["evidence"]["selected_frames"]
    }

    assert by_file["dtl.jpg"]["transmission"] == "sent"
    assert by_file["extra.jpg"]["transmission"] == "not_sent"
    assert "NOT SENT" in markdown
    request = json.loads(
        (tmp_shot(journal, "shot") / "gpt_request.json").read_text(encoding="utf-8")
    )
    encoded = json.dumps(request)
    assert "jpeg-bytes" not in encoded
    assert request["body"]["images"][0]["diagnostic_file"]
    journal.close()


def test_recording_a_shot_does_not_change_selection_or_the_line(tmp_path: Path) -> None:
    bay = tmp_path / "bay"
    _jpeg(bay / "dtl.jpg", (2, 2, 2))
    frame = _frame("swing_behind", "dtl.jpg", 3, None)
    package = _package(frame)
    findings = _findings()
    before = rank_findings(_Reference(), findings)  # type: ignore[arg-type]
    result = _result()
    spoken = result.speech_text
    journal = _journal(tmp_path)
    journal.note_evidence("shot", package, bay)
    journal.note_assessment(
        "shot",
        result=result,
        approved_correction=spoken,
        previous_focus=None,
        previous=None,
        reference=_Reference(),
        observation="",
        observation_included=False,
    )
    report, _markdown = _read(journal, "shot")

    assert frame.reference.relative_file == "dtl.jpg"
    assert frame.selection_reason == "motion peak"
    assert result.speech_text == spoken
    assert rank_findings(_Reference(), findings) == before  # type: ignore[arg-type]
    assert report["decision"]["speech_text"] == spoken
    journal.close()


def _file_shot(journal: DiagnosticJournal, shot_id: str, bay: Path, name: str) -> None:
    journal.note_evidence(shot_id, _package(_frame("swing_behind", name, 1, None)), bay)


def _manifest(root: Path) -> dict[str, object]:
    with _zip(root) as archive:
        return json.loads(archive.read("manifest.json"))


def _zip(root: Path):
    import zipfile

    return zipfile.ZipFile(root / "latest_shot.zip")


def _span(report: dict[str, object], name: str) -> dict[str, object]:
    timing = report["timing"]
    assert isinstance(timing, dict)
    for span in timing["spans"]:
        assert isinstance(span, dict)
        if span["name"] == name:
            return span
    raise AssertionError(name)
