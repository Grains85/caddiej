"""Prepare OpenCV, MediaPipe, and the pose model before the first swing.

MediaPipe imports Matplotlib. The first import builds a font cache when that
cache is empty, which takes several seconds and used to land on the first
shot. The live application starts this work on a background thread at
startup. The thread closes the pose landmarker it creates. Each detection
still creates its own, because a MediaPipe video-mode landmarker is not
safe to share and its timestamps cannot restart.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_DONE = threading.Event()
_THREAD: threading.Thread | None = None


@dataclass(frozen=True, slots=True)
class EvidenceReadiness:
    """What the evidence pipeline can do in this process."""

    status: str
    detail: str
    elapsed_seconds: float | None = None


_STATE = EvidenceReadiness(
    status="idle",
    detail="evidence warmup has not started",
)


def configure_matplotlib() -> None:
    """Point Matplotlib at a writable cache before MediaPipe imports it.

    The backend is Agg so the import does not open a GUI. The cache directory
    is created once. A later import reuses the font list instead of scanning
    the machine again.
    """

    if not os.environ.get("MPLBACKEND"):
        os.environ["MPLBACKEND"] = "Agg"
    if os.environ.get("MPLCONFIGDIR"):
        return
    cache = Path.home() / ".cache" / "caddiej" / "matplotlib"
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    os.environ["MPLCONFIGDIR"] = str(cache)


def readiness() -> EvidenceReadiness:
    """The latest warmup result. Safe to call from any thread."""

    with _LOCK:
        return _STATE


def start_evidence_warmup() -> None:
    """Start preparation if this process has not already finished or started it.

    Returns immediately. A failure is recorded on `readiness()` and logged.
    It is not raised here, so ProTee and GSPro keep running.
    """

    global _THREAD
    with _LOCK:
        if _THREAD is not None or _STATE.status in {"ready", "degraded", "unavailable"}:
            return
        _replace(
            EvidenceReadiness(
                status="starting",
                detail="evidence warmup is running",
            )
        )
        _DONE.clear()
        _THREAD = threading.Thread(
            target=_run,
            name="evidence-warmup",
            daemon=True,
        )
        _THREAD.start()


def ensure_evidence_ready(timeout: float = 60.0) -> EvidenceReadiness:
    """Wait until warmup has finished, starting it if nobody else has.

    The wait is for a worker that is about to detect a pose, not for the
    asyncio loop. After `timeout` seconds the current status is returned
    even if warmup is still running.
    """

    start_evidence_warmup()
    _DONE.wait(timeout)
    return readiness()


def _replace(state: EvidenceReadiness) -> None:
    global _STATE
    _STATE = state


def _finish(status: str, detail: str, started: float) -> None:
    elapsed = time.perf_counter() - started
    state = EvidenceReadiness(
        status=status,
        detail=detail,
        elapsed_seconds=elapsed,
    )
    with _LOCK:
        _replace(state)
    logger.info(
        "evidence_warmup",
        extra={
            "evidence_status": status,
            "evidence_detail": detail,
            "evidence_warmup_seconds": round(elapsed, 3),
        },
    )


def _run() -> None:
    started = time.perf_counter()
    try:
        _prepare()
    except Exception as exc:
        _finish("unavailable", f"{type(exc).__name__}: {exc}", started)
    finally:
        _DONE.set()


def _prepare() -> None:
    started = time.perf_counter()
    configure_matplotlib()
    try:
        from caddiej.swing_evidence.images import require_opencv4

        require_opencv4()
    except ImportError as exc:
        _finish(
            "unavailable",
            "OpenCV is not installed "
            f"({exc}). Install with: python -m pip install -e "
            '".[vision]" -c constraints/vision.txt',
            started,
        )
        return
    except RuntimeError as exc:
        _finish("unavailable", str(exc), started)
        return

    try:
        import mediapipe  # noqa: F401
    except ImportError as exc:
        _finish(
            "degraded",
            "MediaPipe is not installed "
            f"({exc}). Motion analysis can run. Swing stages stay unresolved. "
            "Install with: python -m pip install -e "
            '".[vision]" -c constraints/vision.txt',
            started,
        )
        return

    _build_font_cache()
    from caddiej.swing_evidence.pose_model import ensure_pose_model
    from caddiej.swing_evidence.stages import open_pose_landmarker

    try:
        model = ensure_pose_model()
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        _finish(
            "degraded",
            f"{exc} Motion analysis can run. Swing stages stay unresolved.",
            started,
        )
        return
    landmarker = open_pose_landmarker(model)
    landmarker.close()
    _finish(
        "ready",
        "OpenCV, MediaPipe, the Matplotlib font cache, and the pose model are loaded",
        started,
    )


def _build_font_cache() -> None:
    """Import Matplotlib and build its font list once."""

    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import font_manager

    font_manager.fontManager
