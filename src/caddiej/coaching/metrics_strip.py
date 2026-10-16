"""The four numbers a player wants on the floor, as ProTee wrote them.

A coaching line answers "what should I change". It does not answer "how far did
that go", and a player standing over the next ball wants both. The All Values
grid has every number there is and is a 5102 × 435 strip that cannot be read
from standing height, so four of its cells come down to the floor on their own:
carry, face, path and club speed. Those four because they are the shot (how
far), the two causes a golfer already thinks in (face and path), and the one
input they can feel changing (speed).

**Nothing is converted and nothing is re-read.** The readings are the ones the
coaching request already produced; there is no second look at the grid, no OCR
and no arithmetic beyond rounding a float to the places ProTee itself prints.
The unit stays whatever the grid said, and the direction stays ProTee's word.
That is not laziness about units: the player can see ProTee's own screen, and a
floor that says 139 m beside a screen saying 152.1 yd is a floor nobody
believes again. A sign convention invented here would be worse still, because
"1.7° L" means one thing to CaddieJ, another to a left-hander, and nothing at
all to somebody reading it next to a grid that said "Out-to-In".

**A missing cell is a missing entry.** Three values laid out cleanly is a true
statement about what was read. A dash under a label is a measurement that
failed, and a zero is a lie.
"""

from caddiej.domain.coaching import CoachMetric
from caddiej.domain.vx import MetricDirection, MetricUnit, VxMetric, VxMetrics

STRIP_METRICS: tuple[tuple[str, str], ...] = (
    ("CARRY", "carry"),
    ("FACE", "face_angle"),
    ("PATH", "swing_path"),
    ("CLUB", "club_speed"),
)
"""What the strip shows, left to right, and which cell each one is.

Fixed order, because a strip whose columns move between shots has to be read
rather than glanced at. `CLUB` rather than `CLUB SPEED` so four labels fit
across a projected floor at a size that can be read standing up."""

_DEGREE = "\N{DEGREE SIGN}"

_DIRECTION_WORDS: dict[MetricDirection, str] = {
    MetricDirection.OPEN: "OPEN",
    MetricDirection.CLOSED: "CLOSED",
    MetricDirection.LEFT: "LEFT",
    MetricDirection.RIGHT: "RIGHT",
    MetricDirection.IN_TO_OUT: "IN-TO-OUT",
    MetricDirection.OUT_TO_IN: "OUT-TO-IN",
    MetricDirection.UP: "UP",
    MetricDirection.DOWN: "DOWN",
    MetricDirection.TOE: "TOE",
    MetricDirection.HEEL: "HEEL",
    MetricDirection.HIGH: "HIGH",
    MetricDirection.LOW: "LOW",
    MetricDirection.UPRIGHT: "UPRIGHT",
    MetricDirection.FLAT: "FLAT",
}
"""ProTee's own qualifier, upper-cased and nothing else.

Not abbreviated to a letter. "O" and "C" are a key the player has to have been
taught, and the floor has room for the word."""


def strip_metrics(metrics: VxMetrics | None) -> tuple[CoachMetric, ...]:
    """The readable cells among the four, in strip order. Possibly empty."""

    if metrics is None:
        return ()
    shown: list[CoachMetric] = []
    for label, name in STRIP_METRICS:
        reading = metrics.get(name)
        if reading is None:
            continue
        shown.append(CoachMetric(label=label, value=format_reading(reading)))
    return tuple(shown)


def format_reading(reading: VxMetric) -> str:
    """One reading, in ProTee's unit and ProTee's direction word.

    An angle gets the degree sign rather than the word "deg", because that is
    how the grid prints it and because three characters of a projected floor
    are worth having. Everything else keeps its unit as the vocabulary spells
    it. The magnitude is rounded to one decimal place, which is what ProTee
    shows; a float that is already whole loses its trailing zero, so a club
    speed reads 82 rather than 82.0.
    """

    number = _number(reading.value)
    if reading.unit is MetricUnit.DEG:
        shown = f"{number}{_DEGREE}"
    elif reading.unit is MetricUnit.RATIO:
        shown = number
    else:
        shown = f"{number} {reading.unit.value}"
    word = None if reading.direction is None else _DIRECTION_WORDS[reading.direction]
    return shown if word is None else f"{shown} {word}"


def _number(value: float) -> str:
    rounded = round(value, 1)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded}"
