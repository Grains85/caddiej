"""Which stills to pull out of a swing clip.

Chosen by time relative to impact, not by looking at pixels. The sidecar written
when the clip was cut already states where impact falls, how long the clip is,
how fast it was filmed and how much the timing can be trusted, which is exactly
the information this needs and rather more than frame analysis would recover.

The seam is one method wide and returns bytes. Coaching never learns that FFmpeg
exists, and the video subsystem never learns that a language model does.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from caddiej.domain.media import ClipRef, SwingCapture, TimeQuality

# Address, the top of the backswing, impact, and just past it. Four positions
# a coach would ask to see, spaced for a full swing rather than a putt.
DEFAULT_OFFSETS: tuple[float, ...] = (-1.30, -0.45, 0.0, 0.35)

# How much wider to spread the frames when nobody has measured the camera's
# latency or the launch monitor's reporting lag. Impact could be a few hundred
# milliseconds from where the sidecar says, so asking for exactly 0.00 would
# reliably return a frame that is confidently the wrong side of the ball.
ESTIMATED_SPREAD = 1.25

# Never ask for the very first or very last frame: a seek to the boundary of a
# stream-copied clip lands on nothing often enough to matter.
_EDGE_MARGIN = 0.05


class FrameSource(Protocol):
    """Something that can turn a clip and some offsets into JPEG stills."""

    async def frames(
        self, clip: ClipRef, offsets: Sequence[float]
    ) -> tuple[bytes, ...]:
        """Stills at those offsets from the start of the clip, in order.

        Never raises: a camera or an encoder failing is not a reason to abandon
        coaching, only a reason to coach from the numbers. Fewer stills than
        offsets, including none at all, is a normal answer.
        """


CaptureStillReader = Callable[[SwingCapture, int], Awaitable[object]]
"""The other way pictures of a swing can arrive.

A function rather than a port, because there is one implementation and it
takes no state. The difference from `FrameSource` is not the shape of the call
but what comes back: a clip is footage CaddieJ recorded from a camera it
placed, with a known angle and a known frame rate, and a capture is a handful
of screenshots of the launch monitor's own replay window. Both are pictures of
the same swing; only one of them supports a claim about where impact was.

Kept apart rather than unified for exactly that reason. `evidence.py` says
which one it got, and the two say different things about what may be read
from them.
"""

# A capture holds more stills than are worth sending, and every one of them is
# billed. Matched to how many a clip contributes so a session's cost does not
# depend on which pipeline produced the pictures.
CAPTURE_STILL_LIMIT = len(DEFAULT_OFFSETS)


def offsets_for(
    clip: ClipRef,
    requested: Sequence[float] = DEFAULT_OFFSETS,
    spread: float | None = None,
) -> tuple[float, ...]:
    """Turn offsets relative to impact into offsets into the clip.

    Widened when the impact time is an estimate, and clamped inside the clip so
    a short recording yields fewer frames rather than failed seeks. Duplicates
    are dropped, because two seeks to the same clamped position would send the
    same picture twice and be charged for twice.
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
