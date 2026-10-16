"""What can be wrong with a golf shot, and how to measure how wrong.

This is the only file in the subsystem that holds an opinion about golf, and the
numbers in it are written down rather than fitted so that they can be argued
with. A curve nobody can explain would be worse than a table somebody disagrees
with.

Two ideas run through it.

**Signs are normalized once.** A left-handed player's open face is a right-handed
player's closed one, and every device reports the raw angle in an absolute frame.
So a fault says whether its sign means anything to the player, and mirroring
happens here. Nothing downstream — and no language model — ever has to work out
which way is open for whom.

**A fault is measured two ways and the worse one wins.** Against the player's own
mean, which catches an unusual miss, and against an absolute band, which catches
the player who leaves the face open on every single swing and therefore has a
mean that says nothing is unusual. Taking the maximum is what makes coaching
useful to both of them.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from caddiej.domain.coaching import CoachingFocus, Observation
from caddiej.domain.shot import ClubKind, Handedness, Shot
from caddiej.domain.statistics import MetricSummary

FULL_SWING_CLUBS: frozenset[ClubKind] = frozenset(
    {
        ClubKind.DRIVER,
        ClubKind.WOOD,
        ClubKind.HYBRID,
        ClubKind.IRON,
        ClubKind.WEDGE,
        ClubKind.UNKNOWN,
    }
)
PUTTING_CLUBS: frozenset[ClubKind] = frozenset({ClubKind.PUTTER})

Measure = Callable[[Shot], float | None]

# Where the club should be travelling at impact, per club. A driver is struck on
# the way up off a tee, an iron on the way down into the turf, and a fairway wood
# somewhere near level. Getting this wrong by club would tell a good iron player
# to stop hitting down, so the ideal is per club or the fault is not ranked.
IDEAL_ATTACK: Mapping[ClubKind, float] = {
    ClubKind.DRIVER: 3.0,
    ClubKind.WOOD: 0.0,
    ClubKind.HYBRID: -1.0,
    ClubKind.IRON: -4.0,
    ClubKind.WEDGE: -5.0,
}

# Where a club path is doing its job, per club, in degrees as the player
# experienced them: positive is in-to-out, negative is out-to-in.
#
# Not a tolerance around zero. Zero is not the target and never was, and
# treating it as one is how a 3.7 degree in-to-out iron -- a deliberate,
# well-struck draw -- was told to SWING MORE ACROSS. A club path has a sign
# because a golf swing travels on an arc, and the sign is a shot shape rather
# than a defect.
#
# Asymmetric on purpose, and skewed towards in-to-out, because that is how the
# clubs are used. A driver is struck after the low point and wants the path
# moving out a little; an iron is struck before it, and a touch of in-to-out is
# a draw rather than a fault. The out-to-in side is narrower than the in-to-out
# side for the long clubs, because out-to-in past a few degrees there is the
# motion that produces the common amateur miss, where in-to-out past a few
# degrees mostly produces a draw. The wedge is the exception and skews the
# other way: a short club is delivered steeper and a little across, and
# telling a decent wedge player to stop doing that would be wrong.
#
# These are *functional* windows and not claims about elite play. Inside one,
# CaddieJ has nothing to say about path -- including when the number is unusual
# for this player, which is the override that mattered.
FUNCTIONAL_PATH: Mapping[ClubKind, tuple[float, float]] = {
    ClubKind.DRIVER: (-2.0, 6.0),
    ClubKind.WOOD: (-2.0, 5.0),
    ClubKind.HYBRID: (-2.5, 4.5),
    ClubKind.IRON: (-3.0, 4.0),
    ClubKind.WEDGE: (-4.5, 3.5),
}

# How far past one standard deviation a shot has to be before it is unusual, and
# where "as bad as it gets" sits. Inside one deviation is simply how this player
# swings; three deviations out is the worst thing they did all evening.
_DEVIATIONS_FINE = 1.0
_DEVIATIONS_SPAN = 2.0


def clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _ball(get: Callable[..., float | None]) -> Measure:
    return lambda shot: None if shot.ball is None else get(shot.ball)


def _club(get: Callable[..., float | None]) -> Measure:
    return lambda shot: None if shot.club_delivery is None else get(shot.club_delivery)


@dataclass(frozen=True, slots=True)
class Fault:
    """One thing that can be wrong, and everything needed to rank it.

    `tolerance` and `span` are magnitudes away from the ideal: below the first
    nothing is worth saying, at the second the fault is as bad as this scale
    goes. They are only used when `absolute` is true.

    `absolute` is false for the faults that have no meaningful fixed band. A
    100 mph swing is excellent with a driver and alarming with a nine iron, so
    club speed can only be judged against the player's own, and a fault that
    cannot be judged is left unranked rather than guessed at.
    """

    focus: CoachingFocus
    metric: str | None
    unit: str
    labels: tuple[str, str]
    """What to call the fault when the deviation is positive, then negative."""

    measure: Measure
    tolerance: float = 0.0
    span: float = 1.0
    mirrored: bool = False
    """Whether the sign means left or right to the player, and so must be
    flipped for a left-hander. False for anything measured against the club
    face rather than against the target."""

    absolute: bool = True
    ideals: Mapping[ClubKind, float] | None = None
    """Where this measurement should sit, per club. None centres it on zero.
    A club absent from the mapping means the fault is not ranked at all: a
    putter has no business being told about its angle of attack."""

    clubs: frozenset[ClubKind] = FULL_SWING_CLUBS
    """Which clubs this fault may be ranked on. Full-swing by default, so a
    putt cannot inherit a slice diagnosis from a few degrees of spin axis."""

    windows: Mapping[ClubKind, tuple[float, float]] | None = None
    """Where this measurement is doing its job, per club, as a signed range.

    Different from `tolerance` in two ways that both matter. It is asymmetric,
    so a measurement whose two directions are not equally wrong can say so.
    And it is authoritative: inside the window the fault is not ranked at all,
    where `tolerance` only zeroes the fixed band and leaves the player's own
    scatter free to rank the shot anyway.

    That override is the whole reason this exists. Severity is the larger of
    the fixed band and the distance from this player's own average, which is
    right for a measurement with no healthy range -- an unusually slow swing
    is worth noticing -- and wrong for one that has. A club path of 3.7
    degrees in-to-out is a draw; it was also two standard deviations from the
    player's recent average, so it scored 0.82 and produced SWING MORE ACROSS
    on a shot the player had hit deliberately and hit well. Being unusual is
    not being wrong."""

    def applies_to(self, club: ClubKind) -> bool:
        return club in self.clubs

    def is_functional(self, club: ClubKind, value: float) -> bool:
        """Whether this measurement is inside its healthy range for this club.

        False when there is no window, which is most faults: a fault without a
        reviewed healthy range is ranked as it always was.
        """

        if self.windows is None:
            return False
        window = self.windows.get(club)
        if window is None:
            return False
        low, high = window
        return low <= value <= high

    def ideal_for(self, club: ClubKind) -> float | None:
        if self.ideals is None:
            return 0.0
        return self.ideals.get(club)

    def label_for(self, deviation: float) -> str:
        positive, negative = self.labels
        return positive if deviation >= 0 else negative


FAULTS: tuple[Fault, ...] = (
    # Face to target rather than face to path, deliberately. Face to path is the
    # number that explains curvature and it is in the evidence a model is given,
    # but it needs a club path to exist, and "the face was open" is what a player
    # recognises and what the floor can say in two words.
    Fault(
        focus=CoachingFocus.FACE_CONTROL,
        metric="face_to_target",
        unit="deg",
        labels=("face open", "face closed"),
        measure=_club(lambda club: club.face_to_target_degrees),
        tolerance=2.0,
        span=8.0,
        mirrored=True,
    ),
    # `tolerance` and `span` still rank a path that has left its window: the
    # severity of an extreme path is how far past the edge it went. What the
    # window changes is whether there is anything to rank at all.
    Fault(
        focus=CoachingFocus.PATH,
        metric="club_path",
        unit="deg",
        labels=("in to out", "out to in"),
        measure=_club(lambda club: club.path_degrees),
        tolerance=3.0,
        span=10.0,
        mirrored=True,
        windows=FUNCTIONAL_PATH,
    ),
    # Not mirrored: the toe and the heel are parts of the club, so they mean the
    # same thing whichever side of the ball the player stands on.
    Fault(
        focus=CoachingFocus.STRIKE,
        metric=None,
        unit="mm",
        labels=("toe strike", "heel strike"),
        measure=_club(lambda club: club.impact_horizontal_mm),
        tolerance=8.0,
        span=30.0,
    ),
    Fault(
        focus=CoachingFocus.LOW_POINT,
        metric="angle_of_attack",
        unit="deg",
        labels=("hitting up", "hitting down"),
        measure=_club(lambda club: club.angle_of_attack_degrees),
        tolerance=3.0,
        span=10.0,
        ideals=IDEAL_ATTACK,
    ),
    # The two faults a passive observer can still rank. A launch monitor that
    # only ever reaches CaddieJ through the simulator reports where the ball
    # went and not how the club got there, so these are the whole of the
    # measured evidence on that path. They are ball facts and are named as ball
    # facts: "started pushed" is where it set off, not a claim about the face.
    Fault(
        focus=CoachingFocus.START_LINE,
        metric="launch_direction",
        unit="deg",
        labels=("start pushed", "start pulled"),
        measure=_ball(lambda ball: ball.launch_direction_degrees),
        tolerance=2.0,
        span=8.0,
        mirrored=True,
    ),
    # Spin axis, so it is curvature rather than the line the ball finished on.
    # Slice and hook are already the player's own terms — they mean curving
    # away from and towards their own side — so the mirroring below leaves the
    # word correct for a left-hander rather than reversing it.
    Fault(
        focus=CoachingFocus.CURVE,
        metric="spin_axis",
        unit="deg",
        labels=("slice spin", "hook spin"),
        measure=_ball(lambda ball: ball.spin_axis_degrees),
        tolerance=4.0,
        span=16.0,
        mirrored=True,
    ),
    Fault(
        focus=CoachingFocus.SPEED,
        metric="club_speed",
        unit="mph",
        labels=("club faster", "club slower"),
        measure=_club(lambda club: club.speed_mph),
        absolute=False,
    ),
    Fault(
        focus=CoachingFocus.LAUNCH,
        metric="launch_angle",
        unit="deg",
        labels=("launch high", "launch low"),
        measure=_ball(lambda ball: ball.launch_angle_degrees),
        absolute=False,
    ),
    Fault(
        focus=CoachingFocus.SPIN,
        metric="total_spin",
        unit="rpm",
        labels=("spin high", "spin low"),
        measure=_ball(lambda ball: ball.total_spin_rpm),
        absolute=False,
    ),
    # Putting. Tighter bands: a degree of face on a putt is the start line,
    # and a few millimetres of strike is a miss they can feel.
    Fault(
        focus=CoachingFocus.FACE_CONTROL,
        metric="face_to_target",
        unit="deg",
        labels=("face open", "face closed"),
        measure=_club(lambda club: club.face_to_target_degrees),
        tolerance=0.75,
        span=3.0,
        mirrored=True,
        clubs=PUTTING_CLUBS,
    ),
    Fault(
        focus=CoachingFocus.STRIKE,
        metric=None,
        unit="mm",
        labels=("toe strike", "heel strike"),
        measure=_club(lambda club: club.impact_horizontal_mm),
        tolerance=4.0,
        span=15.0,
        clubs=PUTTING_CLUBS,
    ),
)


FAULTS_BY_FOCUS: dict[CoachingFocus, Fault] = {}
for _fault in FAULTS:
    # First writer wins, so the full-swing row keeps the labels `is_positive`
    # looks up. Putting rows share those labels and must not overwrite them.
    FAULTS_BY_FOCUS.setdefault(_fault.focus, _fault)


def is_positive(focus: CoachingFocus, label: str) -> bool:
    """Which of a fault's two directions a label names.

    The label is the record of direction, rather than a boolean carried
    alongside it, because a label is meaningful to whoever reads a stored row
    and a boolean is not.
    """

    fault = FAULTS_BY_FOCUS.get(focus)
    if fault is None:
        return True
    return label == fault.labels[0]


def oriented(value: float, handedness: Handedness) -> float:
    """The measurement as the player experienced it.

    Devices report an angle in an absolute frame: positive is right of target
    whoever is swinging. A left-handed player's open face points left, so the
    sign flips. Unknown handedness is treated as right-handed, which is the
    common case and the only defensible guess.
    """

    return -value if handedness is Handedness.LEFT else value


def absolute_severity(fault: Fault, deviation: float) -> float | None:
    """How bad this is against a fixed band, or None if there is not one."""

    if not fault.absolute:
        return None
    magnitude = abs(deviation)
    if magnitude <= fault.tolerance:
        return 0.0
    return clamp((magnitude - fault.tolerance) / (fault.span - fault.tolerance))


def relative_severity(deviations: float | None) -> float | None:
    """How bad this is against the player's own normal, or None if unknown."""

    if deviations is None:
        return None
    return clamp((abs(deviations) - _DEVIATIONS_FINE) / _DEVIATIONS_SPAN)


def deviations_from(
    mean: float | None, standard_deviation: float | None, value: float
) -> float | None:
    """How many of the player's own standard deviations from their own mean.

    None when there is no usable baseline, which includes the case of a spread
    of zero: two identical measurements do not mean the third one is infinitely
    unusual, they mean nothing is known about the spread yet.
    """

    if mean is None or standard_deviation is None or standard_deviation <= 0.0:
        return None
    return (value - mean) / standard_deviation


def observe(
    fault: Fault,
    shot: Shot,
    club: ClubKind,
    baseline: Mapping[str, MetricSummary],
) -> tuple[Observation, float] | None:
    """Measure one fault on one shot, or None if it does not apply.

    Returns the observation as the player would describe it, and how bad it was
    on 0..1. Not applicable covers three cases: the device did not measure it,
    the club has no ideal for it, and nothing could rank it because the fault
    has no absolute band and the player has no baseline yet.
    """

    if not fault.applies_to(club):
        return None

    raw = fault.measure(shot)
    if raw is None:
        return None

    ideal = fault.ideal_for(club)
    if ideal is None:
        return None

    mirror = fault.mirrored and shot.handedness is Handedness.LEFT
    value = oriented(raw, shot.handedness) if fault.mirrored else raw
    deviation = value - ideal

    summary = None if fault.metric is None else baseline.get(fault.metric)
    # The mean is mirrored with the measurement. Comparing an open face against
    # an unmirrored average would describe a left-hander's usual two degrees
    # closed as two degrees open, which is the one mistake that turns coaching
    # into the opposite of coaching.
    mean = None if summary is None else (-summary.mean if mirror else summary.mean)
    deviations = deviations_from(
        mean, None if summary is None else summary.standard_deviation, value
    )

    # A measurement inside its reviewed functional range is healthy, full
    # stop: neither the fixed band nor the player's own scatter gets a vote.
    #
    # Both had to go. The fixed band is a tolerance around zero, so it scored
    # the 3.7 degree draw 0.10 for having a sign at all. The statistics then
    # scored it 0.82 for being unusual, and severity is the larger of the two,
    # which is how a deliberate, well-struck shot came back as the worst thing
    # in the session and produced SWING MORE ACROSS. Zero was never the
    # target, and being unusual is not being wrong.
    #
    # The observation is still produced and still carries the reading, so the
    # evidence records the path and a reinforcement can name it. Only the
    # severity is zero.
    functional = fault.is_functional(club, value)
    scores = (
        [0.0]
        if functional
        else [
            score
            for score in (
                absolute_severity(fault, deviation),
                relative_severity(deviations),
            )
            if score is not None
        ]
    )
    if not scores:
        return None

    # A fault with a fixed band is named against that band: open means open,
    # whatever this player usually does. A fault without one can only be named
    # against their own average, because "ninety-five miles an hour" is fast or
    # slow depending on whose swing and which club it is.
    direction = deviation if fault.absolute or mean is None else value - mean
    observation = Observation(
        focus=fault.focus,
        label=fault.label_for(direction),
        value=round(value, 2),
        unit=fault.unit,
        baseline_mean=None if mean is None else round(mean, 2),
        deviations=None if deviations is None else round(deviations, 2),
    )
    return observation, max(scores)
