"""ProTee VX metrics, as read off the All Values panel by a vision model.

These numbers are pictures of numbers. ProTee draws its post-shot grid in its
own window; CaddieJ photographs that window through OBS and a model reads the
grid in the same request that writes the coaching line. Nothing here came from
a structured ProTee API, and the provenance constant says so on every record.

Two rules hold for every field.

**Absent is a value.** Each metric is individually optional. A label the model
could not read clearly is `None`, never a guess, and a `None` is stored as
`None` so a session review can tell "not shown" from "zero".

**Direction is kept as ProTee wrote it.** "5.2° Closed" is `value=5.2,
direction=closed`, not `-5.2`. A metric ProTee prints signed ("-6.3°" attack
angle) is kept as that signed number with no word. CaddieJ owns exactly one
place that turns a direction into a coaching label — `coaching/vx.py` — and
it is tested there.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from caddiej.domain.swing_review import SwingReview
from caddiej.domain.swing_score import SwingScore

VX_METRICS_PROVENANCE = "protee_vx_all_values_visual"
"""How every metric on this path was obtained: visually, from ProTee VX's All
Values panel. Never `measured` in the sense of a device API."""


class MetricDirection(StrEnum):
    """The qualifier words ProTee VX writes beside a value."""

    OPEN = "open"
    CLOSED = "closed"
    LEFT = "left"
    RIGHT = "right"
    IN_TO_OUT = "in_to_out"
    OUT_TO_IN = "out_to_in"
    UP = "up"
    DOWN = "down"
    TOE = "toe"
    HEEL = "heel"
    HIGH = "high"
    LOW = "low"
    UPRIGHT = "upright"
    FLAT = "flat"


class MetricUnit(StrEnum):
    MPH = "mph"
    KMH = "km/h"
    MPS = "m/s"
    DEG = "deg"
    RPM = "rpm"
    YD = "yd"
    M = "m"
    FT = "ft"
    S = "s"
    MM = "mm"
    IN = "in"
    DEG_PER_S = "deg/s"
    RATIO = "ratio"


_SPEED = (MetricUnit.MPH, MetricUnit.KMH, MetricUnit.MPS)
_DISTANCE = (MetricUnit.YD, MetricUnit.M)
_LATERAL = (MetricDirection.LEFT, MetricDirection.RIGHT)
_FACE = (MetricDirection.OPEN, MetricDirection.CLOSED)


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """One cell of the All Values grid, and what a reading of it may contain."""

    name: str
    label: str
    """The label as ProTee VX prints it."""

    units: tuple[MetricUnit, ...]
    directions: tuple[MetricDirection, ...] = ()
    limit: float = 1e6
    """The largest magnitude that is a plausible reading rather than a misread."""

    high_value: bool = False

    signed: bool = False
    """ProTee prints this one as a signed number with no word ("-6.3°"). Its
    first direction is what a positive number means, its second a negative
    one, so a word can be checked against the sign and turned into it."""


VX_METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("ball_speed", "Ball Speed", _SPEED, limit=400.0, high_value=True),
    MetricSpec("club_speed", "Club Speed", _SPEED, limit=300.0, high_value=True),
    MetricSpec(
        "smash_factor", "Smash Factor", (MetricUnit.RATIO,), limit=2.0, high_value=True
    ),
    MetricSpec(
        "face_angle", "Face Angle", (MetricUnit.DEG,), _FACE, 45.0, high_value=True
    ),
    MetricSpec(
        "swing_path",
        "Swing Path",
        (MetricUnit.DEG,),
        (MetricDirection.IN_TO_OUT, MetricDirection.OUT_TO_IN),
        45.0,
        high_value=True,
    ),
    MetricSpec(
        "face_to_path", "Face To Path", (MetricUnit.DEG,), _FACE, 45.0, high_value=True
    ),
    MetricSpec(
        "attack_angle",
        "Attack Angle",
        (MetricUnit.DEG,),
        (MetricDirection.UP, MetricDirection.DOWN),
        45.0,
        high_value=True,
        signed=True,
    ),
    MetricSpec(
        "launch_angle", "Launch Angle", (MetricUnit.DEG,), limit=90.0, high_value=True
    ),
    MetricSpec(
        "launch_direction",
        "Launch Direction",
        (MetricUnit.DEG,),
        _LATERAL,
        90.0,
        high_value=True,
    ),
    MetricSpec(
        "total_spin", "Total Spin", (MetricUnit.RPM,), limit=20000.0, high_value=True
    ),
    MetricSpec(
        "spin_axis", "Spin Axis", (MetricUnit.DEG,), _LATERAL, 90.0, high_value=True
    ),
    MetricSpec("carry", "Carry", _DISTANCE, limit=500.0, high_value=True),
    MetricSpec(
        "dynamic_loft", "Dynamic Loft", (MetricUnit.DEG,), limit=90.0, high_value=True
    ),
    MetricSpec(
        "impact_width",
        "Impact Width",
        (MetricUnit.MM, MetricUnit.IN),
        (MetricDirection.TOE, MetricDirection.HEEL),
        80.0,
        high_value=True,
    ),
    MetricSpec(
        "impact_height",
        "Impact Height",
        (MetricUnit.MM, MetricUnit.IN),
        (MetricDirection.HIGH, MetricDirection.LOW),
        80.0,
        high_value=True,
        signed=True,
    ),
    MetricSpec("back_spin", "Back Spin", (MetricUnit.RPM,), limit=20000.0),
    MetricSpec("side_spin", "Side Spin", (MetricUnit.RPM,), _LATERAL, 20000.0),
    MetricSpec("apex", "Apex", (*_DISTANCE, MetricUnit.FT), limit=600.0),
    MetricSpec("off_line", "Off Line", _DISTANCE, _LATERAL, 300.0),
    MetricSpec("apex_time", "Apex Time", (MetricUnit.S,), limit=30.0),
    MetricSpec("air_time", "Air Time", (MetricUnit.S,), limit=30.0),
    MetricSpec("total_distance", "Total Distance", _DISTANCE, limit=600.0),
    MetricSpec("bounce_roll", "Bounce/Roll", _DISTANCE, limit=300.0),
    MetricSpec("descent_angle", "Descent Angle", (MetricUnit.DEG,), limit=90.0),
    MetricSpec(
        "lie",
        "Lie",
        (MetricUnit.DEG,),
        (MetricDirection.UPRIGHT, MetricDirection.FLAT),
        45.0,
    ),
    MetricSpec("spin_loft", "Spin Loft", (MetricUnit.DEG,), limit=90.0),
    MetricSpec("rifle_spin", "Rifle Spin", (MetricUnit.RPM,), limit=20000.0),
    MetricSpec("closure_rate", "Closure Rate", (MetricUnit.DEG_PER_S,), limit=20000.0),
    MetricSpec("curve", "Curve", _DISTANCE, _LATERAL, 300.0),
)

VX_METRICS_BY_NAME: dict[str, MetricSpec] = {spec.name: spec for spec in VX_METRICS}


@dataclass(frozen=True, slots=True)
class VxMetric:
    """One clearly-read cell: the number, its unit, and ProTee's qualifier."""

    value: float
    unit: MetricUnit
    direction: MetricDirection | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "unit": self.unit.value,
            "direction": None if self.direction is None else self.direction.value,
        }


@dataclass(frozen=True, slots=True)
class VxMetrics:
    """Every supported metric for one shot, each present or explicitly absent."""

    values: Mapping[str, VxMetric | None] = field(default_factory=dict)
    provenance: str = VX_METRICS_PROVENANCE
    image: str | None = None
    """The metrics still the values were read from, relative to the capture."""

    captured_at: datetime | None = None
    rejected: tuple[str, ...] = ()
    """Fields the model returned that failed validation and were nulled."""

    def get(self, name: str) -> VxMetric | None:
        return self.values.get(name)

    @property
    def available(self) -> tuple[str, ...]:
        return tuple(
            spec.name for spec in VX_METRICS if self.values.get(spec.name) is not None
        )

    @property
    def unavailable(self) -> tuple[str, ...]:
        return tuple(
            spec.name for spec in VX_METRICS if self.values.get(spec.name) is None
        )

    def as_dict(self) -> dict[str, object]:
        """Every field, nulls included, in registry order."""

        metrics: dict[str, object] = {}
        for spec in VX_METRICS:
            reading = self.values.get(spec.name)
            metrics[spec.name] = None if reading is None else reading.as_dict()
        return {
            "provenance": self.provenance,
            "image": self.image,
            "captured_at": (
                None if self.captured_at is None else self.captured_at.isoformat()
            ),
            "read": len(self.available),
            "rejected": list(self.rejected),
            "metrics": metrics,
        }


@dataclass(frozen=True, slots=True)
class CoachResult:
    """One strike's coaching outcome on the ProTee-only path, for history.

    Keyed on the strike rather than on a shot, because there is no shot row
    without GSPro. Every outcome is recorded, including "no coaching", with the
    reason, so a session review never has to guess whether a line was withheld
    or lost.
    """

    strike_id: UUID
    trigger_id: str | None
    capture_id: UUID | None
    created_at: datetime
    status: str
    """`ok` (model line shown and spoken), `template` (CaddieJ's own line,
    floor only), `none` (nothing was shown) or `unavailable` (a failure)."""

    kind: str
    focus: str
    cue_id: str | None = None
    display: str | None = None
    speech: str | None = None
    severity: float | None = None
    confidence: float | None = None
    confidence_source: str | None = None
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    latency_ms: int | None = None
    input_images: int = 0
    swing_images: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    metrics_source: str | None = None
    metrics_status: str = "not_configured"
    """`read`, `unreadable`, `unavailable` or `not_configured`."""

    metrics: VxMetrics | None = None
    metrics_image: str | None = None
    metrics_captured_at: datetime | None = None
    panels_used: str | None = None
    evidence_grades: str | None = None
    failure_reason: str | None = None
    detail: str | None = None
    swing_score: SwingScore | None = None
    """The score made from `metrics`, kept even when it is None-valued so a
    row says whether a strike was too thinly read to score or never read."""

    review: SwingReview | None = None
    """Faults, root cause and session lesson, stored inside the score's JSON
    document so no column or migration is needed."""
