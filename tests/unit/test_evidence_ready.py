"""Warmup reports readiness and does not raise into the caller."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from caddiej.swing_evidence import ready
from caddiej.swing_evidence.ready import (
    configure_matplotlib,
    ensure_evidence_ready,
    readiness,
    start_evidence_warmup,
)
from caddiej.swing_reference.live import ReferenceLiveCoach


def _reset() -> None:
    thread = ready._THREAD
    if thread is not None and thread.is_alive():
        ready._DONE.wait(90)
    with ready._LOCK:
        ready._THREAD = None
        ready._STATE = ready.EvidenceReadiness(
            status="idle",
            detail="evidence warmup has not started",
        )
    ready._DONE.clear()


def test_configure_matplotlib_uses_a_writable_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MPLBACKEND", raising=False)
    monkeypatch.delenv("MPLCONFIGDIR", raising=False)
    monkeypatch.setattr(ready.Path, "home", lambda: tmp_path)
    configure_matplotlib()
    assert os.environ["MPLBACKEND"] == "Agg"
    cache = Path(os.environ["MPLCONFIGDIR"])
    assert cache == tmp_path / ".cache" / "caddiej" / "matplotlib"
    assert cache.is_dir()


def test_an_existing_matplotlib_cache_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MPLCONFIGDIR", "/tmp/keep-matplotlib")
    monkeypatch.setenv("MPLBACKEND", "Agg")
    configure_matplotlib()
    assert os.environ["MPLCONFIGDIR"] == "/tmp/keep-matplotlib"


def test_warmup_failure_is_recorded_and_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset()

    def boom() -> None:
        raise RuntimeError("opencv exploded")

    monkeypatch.setattr(ready, "_prepare", boom)
    start_evidence_warmup()
    assert ready._THREAD is not None
    ready._THREAD.join(5)
    state = readiness()
    assert state.status == "unavailable"
    assert "opencv exploded" in state.detail
    _reset()


def test_a_finished_warmup_is_not_started_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset()
    calls = {"n": 0}

    def once() -> None:
        calls["n"] += 1
        ready._finish("ready", "ok", time.perf_counter())

    monkeypatch.setattr(ready, "_prepare", once)
    start_evidence_warmup()
    assert ready._THREAD is not None
    ready._THREAD.join(5)
    start_evidence_warmup()
    assert calls["n"] == 1
    assert readiness().status == "ready"
    _reset()


def test_vision_calls_off_does_not_load_the_pipeline() -> None:
    coach = ReferenceLiveCoach(None, None, vision_calls=False)
    status, detail = coach._evidence_readiness()
    assert status == "not_requested"
    assert "COACHING_VISION_CALLS" in detail


def test_installed_stack_reaches_a_terminal_status() -> None:
    state = ensure_evidence_ready(timeout=90)
    assert state.status in {"ready", "degraded", "unavailable"}
    assert state.detail
    if state.status == "unavailable":
        pytest.fail(state.detail)
