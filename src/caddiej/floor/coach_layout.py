"""Where the four parts of a coached result go, and what they may not cover.

The coached result is not one element. It is a score, a still of the player's
own swing, two lines of words and four readings, and until now it was one
centred column hung off a single anchor point, with the browser nudging it
upwards by however much it overflowed. On the default 1 m bay that happened to
look fine. On a real one — a 0.55 × 0.315 m strip near the middle of the
projection — the column was anchored above the mat and then grew down across
it, so the score sat on the turf the player was standing on and the hitting
strip read through the words.

An anchor and a lift are not a layout. This module is the layout: four named
regions with a position and a size, and a fifth that is not for drawing in at
all.

Four *stacked* regions was the first answer, and the first hardware test showed
why it was not enough. Measured on the projector, at its own 1024 × 768: the
mat's chamfer begins 38 per cent of the way down the image, which leaves 265
pixels for everything. Divided four ways that gave the score 69 pixels to draw
78 in, the words 64 to draw 93, and the readings 37 to draw 45 — three of the
four regions clipped, and the fourth a full-width letterbox with the picture
marooned in the middle of it. The arithmetic was sound and the result was
unusable, which is the difference between dividing a space and composing one.

So the room is used in two dimensions instead of one:

    SWING SCORE 65                        ┌──────────────┐
    OUT-TO-IN PATH                        │   the still  │
    DIP TRAIL SHOULDER                    └──────────────┘
    CARRY        FACE        PATH        CLUB
    128.3 m      7.5 OPEN    0.4 O-I     131.3 km/h
                  ── the mat, and Ball Status ──

A **text column** down the left and an **evidence panel** beside it, with the
**readings** across the full width underneath. The still stops competing for
vertical room with the words, which is where all three overflows came from, and
it stops spanning the image behind them, which is what made it read as a band
across the middle rather than as a picture of a golf swing.

Regions:

1. **score** — the number, top left, read first and from furthest back;
2. **observation** — what was seen, then the one thing to do about it;
3. **evidence** — the still, in its own panel to the right of both, sized to
   the full height of the text beside it and never underneath any of it;
4. **metrics** — carry, face, path and club speed, across the bottom, nearest
   the strip because they are what a player glances down at while setting up.

5. **protected** — the hitting strip and the band behind it where the ball
   status chamfer is drawn. Nothing above is allowed to touch it. That is
   checked here rather than hoped for in a stylesheet, because "does the score
   overlap the mat" is a question with an arithmetic answer.

Everything is in calibrated surface coordinates, computed from the strip's own
projected corners. That is deliberate: the regions then follow the mat through
any calibration, including a homography that turns the rectangle into a
quadrilateral, and a bay that re-measures its strip moves the coaching with it
without touching anything here. It also means this module never needs to know
the bay's size in metres, which it has no honest way of learning.

The division of what room there is, rather than fixed heights, is the other
half of the same argument. A projector pointed at a mat near the back of its
image has very little space above it and a projector pointed at one near the
front has a lot; fixed metre heights would overflow the first and strand the
second. Proportions of the available band always fit, and they keep the score
the biggest thing on the floor whatever the bay.

What proportions alone could not fix is type. The stylesheet sized every line
in viewport *width* while these boxes are fractions of viewport *height*, so a
box and the words in it were free to disagree — and on a 4:3 projector they
did, by nine, twenty-nine and eight pixels. The region heights therefore cross
to the overlay as numbers and the type is sized from them, in `floor.css`. A
bay with less room above its mat now gets smaller words rather than cut ones.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations

from caddiej.floor.geometry import FloorPoint

STRIP_CLEARANCE = 0.04
"""How much clear surface to leave between the coaching and the mat.

A fraction of the projection's depth, not metres: it is a visual separation
rather than a physical one, and what makes two things read as separate is the
gap on the projector. Four per cent of a 1080-line image is forty-odd pixels,
which is a visible band at the distance a golfer stands."""

MARGIN = 0.02
"""Surface left clear at the left and right edges of the coaching.

Small, and not nothing. A projector's edges are the least reliable part of its
image — keystone correction and lens falloff both land there — and a score
whose first digit begins in the outer two per cent of the frame is the one that
goes missing when a bay is nudged."""

METRICS_SHARE = 0.26
"""How much of the room above the mat the four readings take.

More than the old 0.14, which gave them thirty-seven pixels to draw forty-five
in and left their labels at fifteen pixels: legible on a monitor during
development and not from standing height on a floor. They are a full-width
line of four short things, so height spent here buys type size directly."""

SCORE_SHARE = 0.42
"""How much of the upper row the score takes, the rest going to the words.

The score is one line and the words are two, which would argue for less. It
gets more because of how it is read: the number is what somebody glances at
from the far end of the bay between swings, and the words are read standing
over the ball. Size is the only thing that makes the first of those possible."""

TEXT_WIDTH = 0.62
"""How much of the width the score and the words get, before the still.

Enough for `STAND FARTHER AWAY` set large and left-aligned, which is as long as
an action in the registry gets at the largest type the rest of the budget
allows."""

EVIDENCE_WIDTH = 0.33
"""How much of the width the still's panel gets.

The panel, not the picture: the still is drawn `contain`ed inside it, so a
portrait frame from a side camera and a landscape one from behind both keep
their own shape and simply use different amounts of the box. A third of the
width is roughly a four-by-three frame at the height of the words beside it,
which is the size at which a swing is recognisable rather than decorative."""

MINIMUM_BAND = 0.12
"""The least room worth laying out in, as a fraction of the surface.

Below this the regions would each be a few pixels tall and the result would be
illegible however it was arranged. A bay whose mat is that close to the back
edge of its projection has a calibration problem, and the honest answer is to
say there is no coaching layout and let the overlay fall back to the anchor it
has always used, rather than to draw four slivers."""


@dataclass(frozen=True, slots=True)
class FloorRect:
    """An axis-aligned box in calibrated surface coordinates.

    `x` and `y` are the top-left corner — the back-left on this floor — and
    not a centre, because every one of these is laid out by stacking and the
    browser positions by edge.
    """

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "width", "height"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f"FloorRect.{name} must be a finite number")
        if self.width <= 0.0 or self.height <= 0.0:
            raise ValueError("FloorRect width and height must be positive")

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def right(self) -> float:
        return self.x + self.width

    def overlaps(self, other: FloorRect) -> bool:
        """Whether the two boxes share any area at all.

        Touching edges do not overlap. A region whose bottom is exactly the
        protected band's top is adjacent to it, which is what a clearance of
        zero would mean and is still not covering it.
        """

        return (
            self.x < other.right
            and other.x < self.right
            and self.y < other.bottom
            and other.y < self.bottom
        )


@dataclass(frozen=True, slots=True)
class CoachRegions:
    """The four places a coached result is drawn, and the one it may not be."""

    score: FloorRect
    evidence: FloorRect
    observation: FloorRect
    metrics: FloorRect
    protected: FloorRect

    @property
    def drawn(self) -> tuple[FloorRect, ...]:
        return (self.score, self.evidence, self.observation, self.metrics)

    def clears_protected(self) -> bool:
        """True when nothing drawn touches the mat. Asserted by a test."""

        return not any(rect.overlaps(self.protected) for rect in self.drawn)

    def are_disjoint(self) -> bool:
        """True when no two drawn regions share any area.

        Worth asking separately now that the composition is two-dimensional.
        While the regions were a stack this was true by construction, and a
        column of boxes laid end to end cannot overlap. Once the still sits
        beside the words rather than above them, a width that does not match
        the gutter puts a picture through a sentence, and that is an arithmetic
        mistake with a visible result.
        """

        return not any(
            one.overlaps(other) for one, other in combinations(self.drawn, 2)
        )


def coach_regions(
    strip: tuple[FloorPoint, ...],
    *,
    chamfer_depth: float = 0.0,
    clearance: float = STRIP_CLEARANCE,
) -> CoachRegions | None:
    """Lay the coaching out above the projected strip. None if there is no room.

    `strip` is the mat's corners as they land on the surface, which is what
    `project_rect` produces and is already through whatever calibration the
    bay has. Its bounding box is used rather than its outline: a keystoned mat
    is a quadrilateral, and keeping coaching off the quadrilateral's bounding
    box keeps it off the mat with room to spare, which is the right direction
    to be wrong in.

    `chamfer_depth` extends the protected band towards the screen so the ball
    status lettering, which is drawn stepped out behind the mat's rear edge,
    is protected along with it. Ball Status owns that band and is not being
    redesigned; the coaching simply does not go there.

    The band is used from its very top. There is no margin above the score,
    because the top of the image is the far end of the bay and the furthest
    thing from the player: it is the one edge where nothing is lost to being
    near it, and the room is too tight to spend on a gap nobody stands near.
    """

    if not strip:
        return None
    top = min(point.y for point in strip)
    bottom = max(point.y for point in strip)
    left = min(point.x for point in strip)
    right = max(point.x for point in strip)
    protected_top = max(0.0, top - max(chamfer_depth, 0.0))
    protected = FloorRect(
        x=max(0.0, left),
        y=protected_top,
        width=max(min(right, 1.0) - max(left, 0.0), 1e-6),
        height=max(min(bottom, 1.0) - protected_top, 1e-6),
    )
    band = protected_top - clearance
    if band < MINIMUM_BAND:
        return None

    metrics_height = band * METRICS_SHARE
    row_height = band - metrics_height
    score_height = row_height * SCORE_SHARE
    score = FloorRect(x=MARGIN, y=0.0, width=TEXT_WIDTH, height=score_height)
    observation = FloorRect(
        x=MARGIN,
        y=score.bottom,
        width=TEXT_WIDTH,
        height=row_height - score_height,
    )
    # `observation.bottom` rather than `row_height`, for both of the regions
    # that have to meet it. The two are the same number in arithmetic and not
    # always in floating point -- `a + (b - a)` can land a few bits above `b`
    # -- and a few bits is enough for `are_disjoint` to report, correctly, that
    # the words and the readings share a sliver. Deriving the boundary from the
    # edge it has to match makes them equal by construction instead.
    return CoachRegions(
        score=score,
        observation=observation,
        # Beside the words rather than above them, and the full height of both,
        # so a landscape frame is as large as the column allows and a portrait
        # one is as tall. `contain` in the stylesheet does the rest.
        evidence=FloorRect(
            x=1.0 - MARGIN - EVIDENCE_WIDTH,
            y=0.0,
            width=EVIDENCE_WIDTH,
            height=observation.bottom,
        ),
        metrics=FloorRect(
            x=MARGIN,
            y=observation.bottom,
            width=1.0 - 2 * MARGIN,
            height=metrics_height,
        ),
        protected=protected,
    )
