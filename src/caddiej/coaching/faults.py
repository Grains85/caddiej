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
    Fault(
        focus=CoachingFocus.PATH,
        metric="club_path",
        unit="deg",
        labels=("in to out", "out to in"),
        measure=_club(lambda club: club.path_degrees),
        tolerance=3.0,
        span=10.0,
        mirrored=True,
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
)


FAULTS_BY_FOCUS: Mapping[CoachingFocus, Fault] = {
    fault.focus: fault for fault in FAULTS
}


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

    scores = [
        score
        for score in (
            absolute_severity(fault, deviation),
            relative_severity(deviations),
        )
        if score is not None
    ]
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
