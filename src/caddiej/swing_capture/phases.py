"""Which moment of a swing a still is likely to be, and how to go looking for it.

Frame selection used to spread its chosen moments evenly across the part of the
replay where the picture was moving. That is the right thing to do when nothing
is known about what the replay contains, and the first hardware session showed
what it costs: of three down-the-line stills reaching the model, one was the
address, one was the top of the backswing, and one was the player walking away
from a mat with no ball on it. No transition. No downswing. No delivery. No
impact. The model was nevertheless asked to rate "where the club and the trail
shoulder go as the downswing starts", and answered `good`, at the confidence
ceiling, on all four shots.

So the moments are aimed now rather than spread, and this module is the aiming.

**The phases are a guess from timing, and they are labelled as a guess.**
Nothing here looks at a picture. What it has is the moment each camera's
replay replaced its loading screen, and a calibration of how long after that
each phase appeared. That is enough to stop sampling the player walking away
and start sampling the downswing, and it is not enough to promise that
`swing_behind_05` *is* the transition. The label travels with the image saying
which moment CaddieJ was aiming at, the prompt says it is CaddieJ's guess from
the clock, and the model is free to say it is looking at something else. A
calibration that drifts therefore shows up as the model disagreeing, rather
than as a confident rating of the wrong moment.

**Seconds after the replay starts, not fractions of the window.** The first
calibration was fractions of the active interval, and the interval's end is
not a swing event: on the 2026-10-07 capture both replays were still moving
when the capture window closed, so the "end" was wherever
`SWING_CAPTURE_DYNAMIC_DURATION_SECONDS` happened to put it, and every fraction
moved with that setting. The start is an event -- ProTee's loading screen gives
way to the replay, which `select.replay_start` finds by a step in encoded size
-- and ProTee cuts each replay at a fixed time before the strike, so the swing
sits at a fixed time after it however long the window runs. A body camera
whose own start was not found borrows the other body camera's; when neither
was found there is no clock to aim on, and neither camera is spread across
the window instead.

**Each camera is timed from its own replay start.** The two replays do not
start together -- on the 2026-10-07 captures the down-the-line one began about
0.1 s after the face-on one -- but measured from each one's own start they show
the same moment of the swing at the same time. Read off the full-resolution
originals of the 2026-10-07 shots, the first with the whole golfer and the
whole club in the down-the-line picture, and checked against the ProTee
player's progress knob, which moves at a constant rate (no slow motion) and
sits within 0.02 s of the bar's start at each detected replay start:

    swing_behind   address 0.00  early takeaway 0.55  P3 0.88  top 1.64
                   change of direction 1.75  between P5 and P6 1.86
                   just after impact 1.97  about P9 2.08
    swing_side     address 0.00  P3 0.88  near top 1.42  top 1.53
                   change of direction 1.64-1.75  P5 1.86
                   just before impact 1.92 (the other shot)

An earlier table had the face-on replay reaching its top 0.22 s before the
down-the-line one and aimed every face-on moment after the top that much
earlier. It was never measured past the top, and the measurement refutes it:
its face-on impact still showed the start of the downswing with the hands
high, and its release the hands at shoulder height. The face-on moments after
the top now share the down-the-line times.

The face-on P3 was first read at 0.99 s. Three full-resolution diagnostic
swings of 2026-10-08 put lead-arm-parallel at 0.74-0.88 s on both cameras,
the same frame in each, so it is aimed where the down-the-line P3 is. The
other moments held on those swings and are not moved on three of them.

**The downswing is aimed at its own positions.** At ten stills a second P5 to
P8 held two stills, so the downswing aims above were compromises: the
transition took the change of direction, the delivery the still between P5
and P6, and the impact the still nearest the strike. With the downswing
sampled at 20 a second, three more full-resolution swings of 2026-10-08 showed
each position on its own still, on the same still in both views, at these
times after each camera's own replay start:

    lead arm parallel (P5)   1.893  1.891  1.909
    shaft parallel (P6)      1.939  1.953  1.958
    impact (P7)              2.002  2.001  2.006
    shaft parallel (P8)      2.049  2.097  2.100

so P5, P6 and P7 are aimed at 1.90, 1.95 and 2.00 in both views. The release
stays at 2.08: it took the P8 still on two of the three swings and the one
just after it on the third. The top is a slow drift of the hands from about
1.2 s to 1.75 s, and the change of direction comes after it.

**Every phase but the finish is aimed at, in each view that has evidence for
it.** The release (P8) is aimed in both, because both carry the chicken-wing
checkpoints and the face-on view alone shows the arms' connection. The face-on
takeaway is not: it adds only the hip movement there, and the down-the-line
takeaway shows the rest. The finish is not aimed in either: it is the least
weighted phase and a consequence of the others, and a still the model places
there is still scored.

What is deliberately *not* here: any claim about what a phase means for
coaching, any rating, and any fault. This module says "this still is probably
the transition". What may be concluded from a picture of a transition belongs
with the cues.
"""

from __future__ import annotations

from caddiej.domain.cues import PanelView
from caddiej.domain.swing_phase import SwingPhase

# How long after each camera's own replay start each phase appeared, measured
# as in the module docstring. From P5 to P8 the stills are 0.05 s apart, so
# the transition, delivery, impact and release targets each take the still
# nearest their own position. They are here rather than in configuration
# because a default nobody can read is a default nobody will fix; the plan a
# capture actually used is written into its metadata.
_BEHIND: tuple[tuple[SwingPhase, float], ...] = (
    (SwingPhase.SETUP, 0.00),
    (SwingPhase.TAKEAWAY, 0.60),
    (SwingPhase.BACKSWING, 0.88),
    (SwingPhase.TOP, 1.64),
    (SwingPhase.TRANSITION, 1.90),
    (SwingPhase.DOWNSWING, 1.95),
    (SwingPhase.IMPACT, 2.00),
    (SwingPhase.RELEASE, 2.08),
)
"""Down-the-line. Eight moments: every checkpoint phase this view carries
evidence for except the finish. The release is the shaft-parallel position
after impact, before the club rises into the finish."""

_SIDE: tuple[tuple[SwingPhase, float], ...] = (
    (SwingPhase.SETUP, 0.00),
    (SwingPhase.BACKSWING, 0.88),
    (SwingPhase.TOP, 1.53),
    (SwingPhase.TRANSITION, 1.90),
    (SwingPhase.DOWNSWING, 1.95),
    (SwingPhase.IMPACT, 2.00),
    (SwingPhase.RELEASE, 2.08),
)
"""Face-on. Seven moments: no takeaway. The address is kept although few
face-on checkpoints are scored there, because it is the reference every
face-on centre and sway checkpoint is judged against. The top is the face-on
still where the hands are highest, 0.11 s before the down-the-line one; from
the change of direction on, the times are the down-the-line ones, which the
20 fps swings showed on the same still in both views."""

PHASE_SECONDS: dict[PanelView, tuple[tuple[SwingPhase, float], ...]] = {
    PanelView.BEHIND: _BEHIND,
    PanelView.SIDE: _SIDE,
}


def aim_capacity(view: PanelView) -> int | None:
    """How many stills a calibrated camera can use, or None for any other.

    One per aimed moment. A budget larger than that would be spent on a second
    still of a moment already covered, which is a near-duplicate and not
    evidence, so the surplus is left unspent rather than spread."""

    table = PHASE_SECONDS.get(view)
    return None if table is None else len(table)


def phase_plan(view: PanelView, count: int) -> tuple[tuple[SwingPhase, float], ...]:
    """The `count` moments worth asking this camera for, earliest first.

    Which phases are dropped when there is not room for all of them is a
    coaching decision rather than an arithmetic one, so it is a priority list
    rather than an even thinning of the table. Even thinning is what produced
    "address, top, player walking away" and it will always produce something
    like it, because it has no way to know that one of those three is worth
    three of the others.

    A view with no calibration -- the impact graphic, a panel somebody has
    configured that this does not know about -- gets nothing, and the caller
    falls back to spreading moments evenly. That is the old behaviour, which is
    correct for a panel whose contents are unknown.
    """

    table = PHASE_SECONDS.get(view)
    if table is None or count <= 0:
        return ()
    calibrated = {phase for phase, _seconds in table}
    keep: set[SwingPhase] = set()
    for phase in _PRIORITY:
        if len(keep) >= count:
            break
        if phase in calibrated:
            keep.add(phase)
    return tuple((phase, seconds) for phase, seconds in table if phase in keep)


_PRIORITY: tuple[SwingPhase, ...] = (
    SwingPhase.TRANSITION,
    SwingPhase.IMPACT,
    SwingPhase.TOP,
    SwingPhase.SETUP,
    SwingPhase.DOWNSWING,
    SwingPhase.RELEASE,
    SwingPhase.BACKSWING,
    SwingPhase.TAKEAWAY,
)
"""Which moments to buy first when the image budget is smaller than the table.

Transition first because it is where the causes are and it is the one phase
the old selector never once captured. Impact second because it is what the
delivery numbers describe. The top and the setup next: both are cheap to read
and the setup is the reference the later positions are judged against. The
delivery before the release because it carries twice the weight, and the
release before the two backswing moments because it is the only picture of a
chicken wing. The mid-backswing and the takeaway are last: neighbouring
frames say the most about them and they say the least on their own."""


HIGH_DETAIL_PHASES: frozenset[SwingPhase] = frozenset(
    {
        SwingPhase.BACKSWING,
        SwingPhase.TOP,
        SwingPhase.TRANSITION,
        SwingPhase.DOWNSWING,
        SwingPhase.IMPACT,
        SwingPhase.RELEASE,
    }
)
"""Which moments are worth sending at full detail.

Swing stills go to the API at `detail="low"`, which on a tile-priced model is
a flat token price and downsamples the image to roughly 512 square. That is
the second half of why the first session could not read a swing plane, and the
half that no amount of re-aiming a camera would have fixed: a 1920-wide
down-the-line panel squeezed into 512 pixels leaves a club shaft two or three
pixels across and a pair of hands about twenty. Asking "where does the shaft
point" of that picture is asking it of something that is not in it.

Full detail costs real tokens there, so it is not bought for every still.
These are the moments where the answer depends on a few pixels: the shaft's
pitch in the backswing, where it points at the top, which way the club moved
as the direction changed, whether the hands led, whether the lead elbow folded
after the ball and which way the club exited. The setup and takeaway are
judged from whole-body shapes that survive the thumbnail. A patch-priced model
(`gpt-5-mini`) is charged by size whatever this says.

Not a detail setting on its own: the request's own default still applies to
everything not named here, so turning the model's image budget down turns this
down with it."""


def frame_detail(phase: SwingPhase | None, default: str) -> str:
    """Which image detail to send a still of this moment at.

    `default` is the request's own setting, and is what an unaimed still and
    every context phase gets. Only the moments in `HIGH_DETAIL_PHASES` are
    promoted, and only upwards -- a caller that has already chosen `high` for
    everything is not talked down to `low` here.
    """

    if phase is None or phase not in HIGH_DETAIL_PHASES:
        return default
    return "high"
