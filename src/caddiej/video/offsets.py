"""Seek positions for a recorded clip diagnostic.

These offsets are relative to the impact time stored in a clip sidecar. They
prove that FFmpeg can extract stills from a recording. They are not the frames
sent to the swing assessment. That selection is the evidence pipeline.
"""

from collections.abc import Sequence

from caddiej.domain.media import ClipRef, TimeQuality

# Four positions across a full swing, used only by the clip diagnostic.
DEFAULT_OFFSETS: tuple[float, ...] = (-1.30, -0.45, 0.0, 0.35)

# Wider spacing when the sidecar's impact time is an estimate.
ESTIMATED_SPREAD = 1.25

# A seek to the first or last instant of a stream-copied clip often lands nowhere.
_EDGE_MARGIN = 0.05


def offsets_for(
    clip: ClipRef,
    requested: Sequence[float] = DEFAULT_OFFSETS,
    spread: float | None = None,
) -> tuple[float, ...]:
    """Turn offsets relative to impact into positions inside the clip.

    Widened when the impact time is an estimate, and clamped inside the clip so
    a short recording yields fewer frames rather than failed seeks. Duplicates
    are dropped.
    """

    scale = (
        spread
        if spread is not None
        else (ESTIMATED_SPREAD if clip.time_quality is TimeQuality.ESTIMATED else 1.0)
    )
    first = _EDGE_MARGIN
    last = max(first, clip.duration_seconds - _EDGE_MARGIN)

    chosen: list[float] = []
    for offset in requested:
        position = clip.impact_offset_seconds + offset * scale
        clamped = round(min(max(position, first), last), 3)
        if clamped not in chosen:
            chosen.append(clamped)
    return tuple(chosen)
