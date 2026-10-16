"""Turning what the domain decided into something the floor can draw.

This is where golf becomes geometry. The domain says "show the address for a
driver"; deciding that this means a wide marker at the driver's ball position,
and that a club path is a physical ray of a certain length at a certain angle
from it, is a presentation decision and lives here.

Every position is worked out in metres on the hitting bay, and every one of
them that has anything to do with golf is worked out *from the hitting strip*
(`caddiej.floor.layout`). Moving the mat, or moving the projector and
re-measuring, is then one rectangle rather than a list of coordinates. Only the
last step maps metres onto the calibrated surface; projector pixels never enter
the calculation.
"""

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from caddiej.domain.bag import ClubRecommendation
from caddiej.domain.coaching import band_for
from caddiej.domain.events import CoachingStage
from caddiej.domain.observation import ObservedRoundShot
from caddiej.domain.ports import CoachingResult
from caddiej.domain.shot import Club, ClubKind, Shot
from caddiej.floor.chamfer import ball_status, chamfer_depth
from caddiej.floor.coach_layout import CoachRegions, coach_regions
from caddiej.floor.flight import fit_ball_flight, relative_ball_path
from caddiej.floor.format import (
    format_ball_speed_kmh,
    format_remaining_distance_m,
    format_target_distance_m,
    format_total_distance_m,
)
from caddiej.floor.geometry import (
    DEFAULT_CALIBRATION,
    FloorCalibration,
    FloorPoint,
    project_rect,
)
from caddiej.floor.layout import (
    FloorLayout,
    PlacementMode,
    placement_mode_for,
)
from caddiej.floor.putting import PuttTraceRefused, putt_trace
from caddiej.floor.service import FloorService
from caddiej.floor.state import (
    AddressView,
    ClubPath,
    FloorAdvice,
    FloorAnchors,
    FloorEvidence,
    FloorLesson,
    FloorMetric,
    FloorPhase,
    FloorProgress,
    HittingStrip,
    LockAcknowledgement,
    MarkerVariant,
    PuttingLane,
    RoundResultView,
    ShotMetrics,
)

logger = logging.getLogger(__name__)

_VARIANTS = {
    PlacementMode.DRIVER: MarkerVariant.DRIVER,
    PlacementMode.PUTTER: MarkerVariant.PUTTER,
}


def marker_variant_for(club: Club | None) -> MarkerVariant:
    """Which marker a club calls for.

    Appearance only, and derived from the same placement mode that decides
    where the ball goes, so the two cannot drift apart: a bay cannot end up
    drawing a driver halo over the iron position.
    """

    return _VARIANTS.get(placement_mode_for(club), MarkerVariant.NORMAL)


class FloorPresenter:
    """Implements the domain's `FloorPort` by writing floor state."""

    def __init__(
        self,
        floor: FloorService,
        layout: FloorLayout,
        club_path_length: float = 1.2,
        calibration: FloorCalibration | None = None,
        ball_status_scale: float = 6.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._floor = floor
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        self._club_path_length = club_path_length
        self._calibration = DEFAULT_CALIBRATION if calibration is None else calibration
        # Uniform scale of the Compact chamfer. 1 is the drawing; the default
        # is what a real projector needs before the words can be read standing
        # over the ball. It does not move the strip.
        self._ball_status_scale = ball_status_scale
        # One coordinate model, and it arrives already built. Absolute tee
        # points are converted by `layout_from_tees`, which is the one place
        # that conversion lives; this class used to accept both and carry its
        # own copy of it.
        self._layout = layout
        # Whether the floor is currently in putting mode. Presentation state,
        # not golf: the domain says which club is in play, and this remembers
        # what it did about it so a result arriving later knows whether it is
        # looking at a putt. GSPro's own `isPutt` was false on all 196 shots of
        # the real capture, so CaddieJ's answer is the more reliable of the two.
        self._putting = False
        self._off_surface_logged = False
        self._floor.set_anchors(self._anchors())

    # --- geometry ---------------------------------------------------------

    def _anchors(self) -> FloorAnchors:
        return FloorAnchors(
            information=self._calibration.to_surface(
                self._layout.information_position()
            ),
            advice=self._calibration.to_surface(self._layout.advice_position()),
            coach=self._coach_regions(),
        )

    def _coach_regions(self) -> CoachRegions | None:
        """The coaching layout, measured off the strip as it actually projects.

        The chamfer is included in what the coaching must clear. Ball Status
        is drawn on the mat's rear edge and stepped out behind it, and it is
        not being moved; the depth it occupies is derived from the same scale
        the chamfer itself is drawn at, so a bay that enlarges the lettering
        pushes the coaching back rather than having the two collide.
        """

        return coach_regions(
            project_rect(self._layout.strip, self._calibration),
            chamfer_depth=chamfer_depth(
                self._layout.strip, self._calibration, scale=self._ball_status_scale
            ),
        )

    def _ball_on_surface(self, club: Club | None = None) -> FloorPoint:
        """The browser only ever sees surface coordinates."""

        point = self._calibration.to_surface(self._layout.ball_position_for(club))
        return self._on_the_surface(point)

    def _on_the_surface(self, point: FloorPoint) -> FloorPoint:
        """Keep a marker drawable even if somebody configured it off the floor.

        A ball position is allowed to sit off the hitting strip — that is what
        the putter position is for — but a point off the *projection* cannot be
        drawn at all, and a bay is more use with a marker in the wrong place
        than with a round that stops when a putter is selected. Logged once, so
        the configuration error is visible without a line per poll.
        """

        clamped = FloorPoint(
            x=min(max(point.x, 0.0), 1.0),
            y=min(max(point.y, 0.0), 1.0),
        )
        if clamped != point and not self._off_surface_logged:
            self._off_surface_logged = True
            logger.warning(
                "floor_ball_position_off_surface",
                extra={"floor_ball_x": point.x, "floor_ball_y": point.y},
            )
        return clamped

    def _strip_on_surface(self, *, visible: bool) -> HittingStrip:
        return HittingStrip(
            corners=project_rect(self._layout.strip, self._calibration),
            visible=visible,
            filled=self._layout.strip_filled,
        )

    def _lane_on_surface(self, *, visible: bool) -> PuttingLane:
        ball = self._layout.ball_position(PlacementMode.PUTTER)
        return PuttingLane(
            corners=project_rect(self._layout.putting_lane_rect(), self._calibration),
            start=self._calibration.to_surface(ball),
            end=self._calibration.to_surface(self._layout.putting_lane_end()),
            visible=visible,
        )

    # --- the room ---------------------------------------------------------

    def light_hitting_area(self) -> None:
        """Put the turf light up and leave it there.

        Logged once, at the level an operator reads, because "is the pad lit"
        is the first question asked of a dark bay. The anchor goes in the same
        line: every golf position on the floor is derived from it, so a bay
        whose numbers are in the wrong place has one value to check.
        """

        self._floor.set_hitting_strip(self._strip_on_surface(visible=True))
        anchor = self._layout.anchor
        logger.info(
            "floor_hitting_area_lit",
            extra={
                "floor_hitting_strip_filled": self._layout.strip_filled,
                "floor_anchor_x_m": anchor.x_m,
                "floor_anchor_y_m": anchor.y_m,
            },
        )

    def darken_hitting_area(self) -> None:
        self._floor.hide_hitting_strip()
        self._floor.hide_putting_lane()
        logger.info("floor_hitting_area_darkened")

    # --- the address ------------------------------------------------------

    def show_address(self, club: Club | None) -> None:
        self._stand_down_the_banner()
        self._floor.show_marker(self._ball_on_surface(club), marker_variant_for(club))

    def hide_address(self) -> None:
        self._floor.hide_marker()

    def show_putting_guide(self, club: Club | None) -> None:
        """Put the lane up for a putter, and take it down for anything else.

        Called on every pass of the domain's decision rather than on a
        transition, so the lane follows the club in play without anything
        having to remember which club it was last time. Setting state that is
        already set is not a change, so a poll costs nothing.
        """

        putting = placement_mode_for(club) is PlacementMode.PUTTER
        if putting and not self._layout.putting_lane:
            putting = False
        if putting == self._putting:
            return
        self._putting = putting
        if putting:
            self._floor.set_putting_lane(self._lane_on_surface(visible=True))
        else:
            self._floor.hide_putting_lane()
            # The last putt's roll belongs to putting mode, not to the next
            # full swing's address.
            self._floor.clear_putt_path()
        logger.info("floor_putting_mode_changed", extra={"floor_putting": putting})

    def acknowledge_ball_lock(self) -> None:
        """Pulse the turf light without moving or extinguishing it."""

        self._floor.set_lock_acknowledgement(LockAcknowledgement())

    def clear_lock_acknowledgement(self) -> None:
        self._floor.clear_lock_acknowledgement()

    def show_address_hud(
        self,
        club: Club | None,
        distance_raw: float | None,
        recommendation: ClubRecommendation | None = None,
    ) -> None:
        self._stand_down_the_banner()
        distance_display = format_target_distance_m(distance_raw)
        view = AddressView(
            club_display=_club_display(club),
            distance_display=distance_display,
            recommended_club_display=(
                "" if recommendation is None else recommendation.club
            ),
        )
        # The in-play club alone is not worth a panel: it is drawn as a marker
        # under the ball already, and on ProTee it is the generic word the
        # detector reports rather than the club in the player's hands.
        if distance_display is None and not view.recommended_club_display:
            self._floor.clear_address()
            return
        self._floor.set_address(view)

    def hide_address_hud(self) -> None:
        self._floor.clear_address()

    def show_play_status(self, status: str, club: Club | None) -> None:
        """Keep the device's own word, and draw the strip's translation of it.

        The diagnostic (`READY`, `FINDING BALL`) still travels. What the rear
        edge says is decided here, and a failure to draw it must not stop the
        rest of the floor: the marker, the distance and the result do not
        depend on the lamp.
        """

        label = ""
        if club is not None:
            label = (
                club.kind.value.upper()
                if club.kind is not ClubKind.UNKNOWN
                else (club.vendor_code or "")
            )
        self._floor.set_play_status(status, label)
        previous = self._floor.state.ball_status
        try:
            shown = ball_status(
                self._layout.strip,
                status,
                self._calibration,
                scale=self._ball_status_scale,
            )
            self._floor.set_ball_status(shown)
        except Exception:
            logger.exception(
                "ball_status_presentation_failed",
                extra={"ball_status_source": status},
            )
            return
        if (
            previous is not None
            and previous.label == shown.label
            and previous.ready == shown.ready
        ):
            return
        logger.info(
            "ball_status_presented",
            extra={
                "ball_status_source": status,
                "ball_status_label": shown.label,
                "ball_status_ready": shown.ready,
                "ball_status_scale": self._ball_status_scale,
            },
        )

    # --- a shot -----------------------------------------------------------

    def show_shot(self, shot: Shot) -> None:
        self._stand_down_the_banner()
        self._floor.clear_address()
        self._floor.clear_round_result()
        self._show_club_path(shot)
        self._show_metrics(shot)

    def show_round_result(self, shot: ObservedRoundShot) -> None:
        self._stand_down_the_banner()
        self._floor.clear_address()
        self._floor.clear_metrics()
        self._floor.clear_club_path()
        relative = relative_ball_path(shot.ball_path, shot.ball_start)
        ball_speed_display = format_ball_speed_kmh(shot.ball_speed_raw)
        shot_distance_display = format_total_distance_m(shot.total_distance_raw)
        remaining_distance_display = format_remaining_distance_m(
            shot.distance_to_pin_raw
        )
        # Both halves of the conversion in one line, because "the overlay shows
        # a dash" has two very different causes — a value currentRound.dat did
        # not carry, and a value that did not survive formatting — and they are
        # not distinguishable from the floor.
        logger.info(
            "floor_round_result_presented",
            extra={
                "shot_id": shot.shot_id,
                "round_ball_speed_raw": shot.ball_speed_raw,
                "round_total_distance_raw": shot.total_distance_raw,
                "round_distance_to_pin_raw": shot.distance_to_pin_raw,
                "round_ball_speed_display": ball_speed_display or "",
                "round_shot_distance_display": shot_distance_display or "",
                "round_remaining_distance_display": remaining_distance_display or "",
            },
        )
        self._floor.set_round_result(
            RoundResultView(
                shot_id=shot.shot_id,
                player_name=shot.player_name or "",
                hole=shot.hole,
                hole_shot=shot.hole_shot,
                ball_speed_raw=shot.ball_speed_raw,
                total_distance_raw=shot.total_distance_raw,
                distance_to_pin_raw=shot.distance_to_pin_raw,
                ball_speed_display=ball_speed_display,
                shot_distance_display=shot_distance_display,
                remaining_distance_display=remaining_distance_display,
                starting_surface_raw=shot.starting_surface_raw,
                ending_surface_raw=shot.ending_surface_raw,
            ),
            fit_ball_flight(relative) or None,
        )
        self._show_putt_path(shot)

    def _show_putt_path(self, shot: ObservedRoundShot) -> None:
        """Draw the roll, or say in one line why there is nothing to draw.

        The reason is logged at info rather than debug because "why is there no
        line after my putt" is a question somebody asks of a live bay, and the
        answer is always one of a handful of refusals from
        `caddiej.floor.putting`.
        """

        if not (self._putting or shot.is_putt):
            return
        outcome = putt_trace(
            shot,
            self._layout.ball_position(PlacementMode.PUTTER),
            self._calibration,
        )
        if isinstance(outcome, PuttTraceRefused):
            self._floor.clear_putt_path()
            logger.info(
                "floor_putt_path_unavailable",
                extra={
                    "shot_id": shot.shot_id,
                    "floor_putt_reason": outcome.reason,
                    "floor_putt_samples": len(shot.ball_path),
                },
            )
            return
        self._floor.set_putt_path(outcome)
        logger.info(
            "floor_putt_path_drawn",
            extra={
                "shot_id": shot.shot_id,
                "floor_putt_points": len(outcome.points),
                "round_total_distance_raw": shot.total_distance_raw,
            },
        )

    def _stand_down_the_banner(self) -> None:
        """Clear the startup text, which is drawn across the whole surface.

        It says CaddieJ is running, which stops being worth a projector once
        there is a ball to place or a shot to read. Repeated calls cost nothing:
        setting state that is already set is not a change.
        """

        self._floor.set_status_text("")

    def clear_shot(self) -> None:
        self._floor.clear_club_path()
        self._floor.clear_metrics()
        self._floor.clear_round_result()
        self._floor.clear_putt_path()

    def show_advice(
        self, text: str, severity: float, result: CoachingResult | None = None
    ) -> None:
        """Draw one coaching line, coloured by how bad the fault was.

        Severity comes in as a number and the band is worked out here, because
        which colour 0.42 deserves is a presentation decision while what 0.42
        means is not. The rest of the result is copied across as it arrived:
        the evidence becomes ids the HUD resolves, never a path it opens.
        """

        self._stand_down_the_banner()
        result = result or CoachingResult()
        evidence = result.evidence
        self._floor.set_advice(
            FloorAdvice(
                text=text,
                band=band_for(severity),
                observation=result.observation,
                swing_score=result.swing_score,
                evidence=(
                    None
                    if evidence is None
                    else FloorEvidence(
                        capture_id=str(evidence.capture_id),
                        frame_id=evidence.frame_id,
                    )
                ),
                metrics=tuple(
                    FloorMetric(label=metric.label, value=metric.value)
                    for metric in result.metrics
                ),
                phases=tuple(
                    FloorPhase(label=phase.label, value=phase.value)
                    for phase in result.phases
                ),
                lesson=(
                    None
                    if result.progress is None
                    else FloorLesson(
                        label=result.progress.label,
                        value=result.progress.value,
                        delta=result.progress.delta,
                        headline=result.progress.headline,
                        verdict=result.progress.verdict,
                        mode=result.progress.mode,
                    )
                ),
            )
        )

    def show_advice_pending(self) -> None:
        """Keep the templated line and mark that a model is still thinking."""

        current = self._floor.state.advice
        if current is None:
            self._floor.set_advice(FloorAdvice(text="", pending=True))
            return
        self._floor.set_advice(replace(current, pending=True))

    def clear_advice(self) -> None:
        self._floor.clear_advice()

    def show_progress(self, stage: CoachingStage) -> None:
        """Hold the stage, keeping when it began if it has not changed.

        A repeat of the stage already up is not a restart: the clock it carries
        is when this part of the work began, and resetting it on a duplicate
        would make a long analysis look like a fresh one.
        """

        current = self._floor.state.progress
        if current is not None and current.stage is stage:
            return
        self._floor.set_progress(FloorProgress(stage=stage, started_at=self._now()))
        logger.info("floor_progress_shown", extra={"floor_progress_stage": stage.value})

    def clear_progress(self) -> None:
        if self._floor.state.progress is None:
            return
        self._floor.clear_progress()
        logger.info("floor_progress_cleared")

    def _show_club_path(self, shot: Shot) -> None:
        delivery = shot.club_delivery
        angle = None if delivery is None else delivery.path_degrees
        if angle is None:
            # Many launch monitors report no club data at all. Drawing the ball's
            # launch direction instead and calling it the club path would be
            # inventing a measurement, so the floor simply shows no path.
            self._floor.clear_club_path()
            logger.debug("floor_shot_had_no_club_path")
            return
        self._floor.set_club_path(
            ClubPath(
                start=self._ball_on_surface(shot.club),
                angle_degrees=angle,
                length=self._club_path_length,
                calibration=self._calibration,
            )
        )

    def _show_metrics(self, shot: Shot) -> None:
        ball = shot.ball
        if ball is None:
            # A club-only shot has nothing a player would recognise as a result.
            self._floor.clear_metrics()
            return
        self._floor.set_metrics(
            ShotMetrics(
                ball_speed_mph=ball.speed_mph,
                launch_angle_degrees=ball.launch_angle_degrees,
                carry_yards=ball.carry_yards,
            )
        )


def _club_display(club: Club | None) -> str:
    """The club name as the source wrote it, for the overlay.

    Vendor text is preferred so ``7 Iron`` stays ``7 IRON`` rather than the
    generic kind ``IRON``. Ball placement still uses ``club.kind``.
    """

    if club is None:
        return ""
    code = " ".join((club.vendor_code or "").split())
    if code:
        return code.upper()
    if club.kind is ClubKind.UNKNOWN:
        return ""
    return club.kind.value.upper()
