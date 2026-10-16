"""A scripted walk through every floor state, for visual checking without GSPro.

This is a development tool, not a simulation. It exists so the overlay can be
inspected on the real projector before any launch monitor is integrated, and so
that "does the putter marker read at 3 metres?" is answerable today.

It is a writer like any other: it calls `FloorService` and owns no state.
"""

import asyncio
import logging
from collections.abc import Callable, Iterator, Sequence

from caddiej.floor.chamfer import ball_status
from caddiej.floor.geometry import (
    DEFAULT_CALIBRATION,
    FloorCalibration,
    FloorPoint,
    PhysicalRect,
    project_rect,
)
from caddiej.floor.service import FloorService
from caddiej.floor.state import (
    ClubPath,
    FloorFlourish,
    HittingStrip,
    LockAcknowledgement,
    MarkerVariant,
    ShotMetrics,
)

logger = logging.getLogger(__name__)

Step = tuple[str, Callable[[FloorService], object]]


def demo_steps(
    calibration: FloorCalibration | None = None,
    *,
    scale: float = 6.0,
) -> Sequence[Step]:
    """Every state the floor can currently render, in a reviewable order.

    Each step is named so the logs say what should be on the floor, which is how
    a mismatch between intent and pixels gets noticed. Club paths use the same
    calibration the presenter does, so the demo on a 4 m × 3 m floor is the
    same geometry a real shot would produce.
    """

    mapping = DEFAULT_CALIBRATION if calibration is None else calibration
    tee = FloorPoint(x=0.5, y=0.72)
    left = FloorPoint(x=0.22, y=0.6)
    right = FloorPoint(x=0.78, y=0.6)
    green = FloorPoint(x=0.5, y=0.35)
    strip_rect = PhysicalRect(x_m=0.5, y_m=0.72, width_m=0.30, depth_m=0.45)
    strip = HittingStrip(
        corners=project_rect(strip_rect, mapping),
        visible=True,
    )

    return (
        # The startup banner is full-surface, so it goes before floor elements
        # are shown. What text exists is the server's decision, not the CSS's.
        ("status_cleared", lambda s: s.set_status_text("")),
        ("hitting_strip_on", lambda s: s.set_hitting_strip(strip)),
        (
            "ball_status_place",
            lambda s: s.set_ball_status(
                ball_status(strip_rect, "", mapping, scale=scale)
            ),
        ),
        (
            "ball_status_ready",
            lambda s: s.set_ball_status(
                ball_status(strip_rect, "READY", mapping, scale=scale)
            ),
        ),
        ("marker_normal_centre", lambda s: s.show_marker(tee, MarkerVariant.NORMAL)),
        ("marker_driver", lambda s: s.show_marker(tee, MarkerVariant.DRIVER)),
        (
            "marker_putter_on_the_green",
            lambda s: s.show_marker(green, MarkerVariant.PUTTER),
        ),
        ("marker_far_left", lambda s: s.show_marker(left, MarkerVariant.NORMAL)),
        ("marker_far_right", lambda s: s.show_marker(right, MarkerVariant.NORMAL)),
        ("marker_corners", lambda s: s.show_marker(FloorPoint(x=0.0, y=0.0))),
        ("marker_opposite_corner", lambda s: s.show_marker(FloorPoint(x=1.0, y=1.0))),
        ("marker_back_to_the_tee", lambda s: s.show_marker(tee, MarkerVariant.DRIVER)),
        ("marker_hidden_as_if_ball_detected", lambda s: s.hide_marker()),
        (
            "ball_lock_acknowledged",
            lambda s: s.set_lock_acknowledgement(LockAcknowledgement()),
        ),
        ("ball_lock_cleared", lambda s: s.clear_lock_acknowledgement()),
        ("hitting_strip_off", lambda s: s.hide_hitting_strip()),
        # A shot: straight, then the two misses, then the numbers.
        (
            "club_path_straight",
            lambda s: s.set_club_path(
                ClubPath(
                    start=tee,
                    angle_degrees=0.0,
                    length=0.4,
                    calibration=mapping,
                )
            ),
        ),
        (
            "club_path_out_to_in",
            lambda s: s.set_club_path(
                ClubPath(
                    start=tee,
                    angle_degrees=25.0,
                    length=0.4,
                    calibration=mapping,
                )
            ),
        ),
        (
            "club_path_in_to_out",
            lambda s: s.set_club_path(
                ClubPath(
                    start=tee,
                    angle_degrees=-25.0,
                    length=0.4,
                    calibration=mapping,
                )
            ),
        ),
        (
            "club_path_long_enough_to_leave_the_surface",
            lambda s: s.set_club_path(
                ClubPath(
                    start=tee,
                    angle_degrees=-8.0,
                    length=1.2,
                    calibration=mapping,
                )
            ),
        ),
        (
            "metrics_shown",
            lambda s: s.set_metrics(
                ShotMetrics(
                    ball_speed_mph=148.2,
                    launch_angle_degrees=12.6,
                    carry_yards=268.4,
                )
            ),
        ),
        (
            "metrics_updated_for_a_second_shot",
            lambda s: s.set_metrics(
                ShotMetrics(
                    ball_speed_mph=61.7,
                    launch_angle_degrees=28.1,
                    carry_yards=74.9,
                )
            ),
        ),
        ("club_path_cleared", lambda s: s.clear_club_path()),
        ("metrics_cleared", lambda s: s.clear_metrics()),
        ("marker_shown_again_for_the_next_shot", lambda s: s.show_marker(tee)),
        # Every celebration, in order, so the escalation can be judged on the
        # actual projector — which is the only place a judgement about "is a
        # birdie too much?" is worth anything.
        #
        # The values are written out rather than read from the effects
        # catalogue, because the effects package depends on this one and the
        # dependency may not run both ways. A test asserts that these still
        # match, so the two cannot drift apart quietly.
        *(
            (
                f"flourish_{cue}",
                _show_flourish(cue, label, intensity, seconds),
            )
            for cue, label, intensity, seconds in DEMO_FLOURISHES
        ),
        ("flourish_cleared", lambda s: s.clear_flourish()),
    )


# Mirrors `caddiej.effects.catalogue`; see the note in `demo_steps`.
DEMO_FLOURISHES: tuple[tuple[str, str, float, float], ...] = (
    ("par", "PAR", 0.15, 1.6),
    ("birdie", "BIRDIE", 0.4, 2.4),
    ("eagle", "EAGLE", 0.65, 3.2),
    ("albatross", "ALBATROSS", 0.85, 4.0),
    ("hole_in_one", "HOLE IN ONE", 1.0, 5.0),
)


def _show_flourish(
    cue: str, label: str, intensity: float, seconds: float
) -> Callable[[FloorService], object]:
    """A step that shows one celebration.

    A factory rather than a lambda in the comprehension above, because a lambda
    closing over a loop variable captures the variable and not its value — so
    every step would show the last one.
    """

    flourish = FloorFlourish(cue=cue, label=label, intensity=intensity, seconds=seconds)
    return lambda service: service.show_flourish(flourish)


def cycle_steps(steps: Sequence[Step]) -> Iterator[Step]:
    while True:
        yield from steps


async def run_demo(
    service: FloorService,
    step_seconds: float,
    steps: Sequence[Step] | None = None,
    calibration: FloorCalibration | None = None,
    *,
    scale: float = 6.0,
) -> None:
    """Drive the floor through every state on a loop until cancelled.

    Looping matters: it means the surface can be watched for a while without
    anyone touching a keyboard, which is the only practical way to review a
    projection you have to stand next to.
    """

    resolved = demo_steps(calibration, scale=scale) if steps is None else steps
    logger.info("floor_demo_started", extra={"floor_demo_steps": len(resolved)})
    try:
        for name, apply in cycle_steps(resolved):
            logger.info("floor_demo_step", extra={"floor_demo_step": name})
            apply(service)
            await asyncio.sleep(step_seconds)
    except asyncio.CancelledError:
        logger.info("floor_demo_stopped")
        raise
