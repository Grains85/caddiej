"""What the floor shows as a hole is played.

This is the first piece of CaddieJ that is neither an adapter nor a transport: it
holds an opinion about golf. It watches the facts the launch monitor and the
simulator report, keeps the small amount of state needed to make sense of them,
and asks the floor for what a player should be looking at.

The rules it enforces, in the order a shot actually happens:

* the club in play decides which address marker is shown, and where;
* a ball on the mat hides the marker, because the projector would otherwise be
  shining onto the ball the player is about to hit;
* a shot replaces the address with what just happened;
* a GSPro round result stays until the next ball / READY, or a configurable cap;
* a coaching line appears beside it and has its own lifetime;
* once that has been read, the floor returns to showing where the next ball goes.

Nothing here knows how any of it is drawn, and nothing in the browser decides any
of it. The browser is given the answer.

It also announces the shot once it has attributed it, which is how anything that
wants shots — history, statistics, coaching, later speech — gets one that knows
which club hit it.

The coaching line is the only thing on the floor whose words may have come from a
language model, so it is worth being clear about what happens to it here: it is
shown, on a timer, and it is dropped if it names a shot that is no longer the
current one. It reaches nothing else. No branch in this file reads it, so there
is no path by which a sentence could change what CaddieJ does.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from uuid import UUID

from caddiej.core.event_bus import EventBus
from caddiej.core.events import Event
from caddiej.domain.events import (
    AnalysisCompleted,
    BallCleared,
    BallDetected,
    ClubSelected,
    HandednessChanged,
    LaunchMonitorLinkChanged,
    LaunchMonitorReadinessChanged,
    LinkState,
    RoundShotObserved,
    ShotCompleted,
    ShotDiagnosed,
    ShotDispatched,
    ShotIngested,
)
from caddiej.domain.ports import FloorPort
from caddiej.domain.shot import Club, Detection, Handedness

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]
Publish = Callable[[Event], Awaitable[None]]


class PlayFlow:
    """Drives the floor from what the room reports.

    One instance owns the current state of play, so it must be driven from a
    single task. Its subscriptions share the subscriber name `play`, so they
    share one worker and keep their order.
    """

    def __init__(
        self,
        floor: FloorPort,
        publish: Publish,
        shot_display_seconds: float = 6.0,
        advice_display_seconds: float = 9.0,
        shot_result_max_duration: float = 15.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._floor = floor
        self._publish = publish
        self._shot_display_seconds = shot_display_seconds
        # Longer than the shot, because a coached line arrives after the numbers
        # and takes longer to read than a number does. Its own timer for the same
        # reason: a model answering four seconds late must still get a full
        # showing rather than two seconds of what is left of the shot's.
        self._advice_display_seconds = advice_display_seconds
        # Observer round results stay until READY / a new ball, or this cap.
        self._shot_result_max_duration = shot_result_max_duration
        self._sleep = sleep

        self._club: Club | None = None
        self._ball = Detection.UNKNOWN
        self._ready: bool | None = None
        self._handedness = Handedness.UNKNOWN
        self._monitor_status = ""
        self._showing_shot = False
        self._round_result_up = False
        self._display_generation = 0
        self._display_timer: asyncio.Task[None] | None = None
        self._shot_id: UUID | None = None
        self._advice_timer: asyncio.Task[None] | None = None

    # --- lifecycle --------------------------------------------------------

    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe(ClubSelected, self.on_club_selected, name="play")
        bus.subscribe(BallDetected, self.on_ball_detected, name="play")
        bus.subscribe(BallCleared, self.on_ball_cleared, name="play")
        bus.subscribe(HandednessChanged, self.on_handedness_changed, name="play")
        bus.subscribe(
            LaunchMonitorReadinessChanged, self.on_readiness_changed, name="play"
        )
        bus.subscribe(LaunchMonitorLinkChanged, self.on_link_changed, name="play")
        bus.subscribe(ShotDispatched, self.on_shot_dispatched, name="play")
        bus.subscribe(ShotIngested, self.on_shot, name="play")
        bus.subscribe(RoundShotObserved, self.on_round_shot, name="play")
        bus.subscribe(ShotDiagnosed, self.on_shot_diagnosed, name="play")
        bus.subscribe(AnalysisCompleted, self.on_advice_ready, name="play")

    # --- what is true right now -------------------------------------------
    #
    # Four narrow reads, for anything that wants to ask what is happening
    # rather than be told. Deliberately properties over a snapshot object: this
    # is the state of play, not a report about it, and a second object holding a
    # copy is a second thing that can be out of date.
    #
    # Nothing inside CaddieJ uses these. They exist because the alternative —
    # a second subscriber assembling the same state from the same events — would
    # be a second source of truth for "which club is in play", and two answers
    # to that question is precisely what §3 exists to prevent.

    @property
    def current_club(self) -> Club | None:
        """The club the simulator last said was in play, if it has said."""

        return self._club

    @property
    def ball(self) -> Detection:
        """Whether there is a ball on the mat, as far as anything knows."""

        return self._ball

    @property
    def ready(self) -> bool | None:
        """Whether the launch monitor says it is ready. None means it has not
        said, which is different from saying no."""

        return self._ready

    @property
    def handedness(self) -> Handedness:
        """Which way round the player is standing.

        Tracked but never acted on here: nothing about the floor changes with
        handedness, because the marker sits under a ball rather than under a
        player. It is kept because it is the only fact about who is playing
        that anything reports, and coaching needs it mirrored correctly.
        """

        return self._handedness

    def start(self) -> None:
        """Show the resting floor, before anything has connected.

        A marker on an idle floor is how the projector gets aimed, and it is what
        tells someone walking in that CaddieJ is running.
        """

        self._present()

    async def aclose(self) -> None:
        await self._cancel_display_timer()
        await self._cancel_advice_timer()

    # --- what the room reports --------------------------------------------

    async def on_club_selected(self, event: ClubSelected) -> None:
        self._club = event.club
        logger.info(
            "play_club_in_play",
            extra={
                "gspro_club": event.club.kind.value,
                "gspro_club_code": event.club.vendor_code,
            },
        )
        # Changing club at address changes the marker under the player's feet,
        # which is the whole point of having variants.
        self._present()

    async def on_ball_detected(self, _event: BallDetected) -> None:
        self._ball = Detection.PRESENT
        await self._dismiss_round_result()
        self._present()

    async def on_ball_cleared(self, _event: BallCleared) -> None:
        self._ball = Detection.ABSENT
        self._present()

    async def on_handedness_changed(self, event: HandednessChanged) -> None:
        # No call to `_present()`: the address marker does not depend on this,
        # and repainting the floor because somebody turned around would be a
        # change nobody asked for.
        self._handedness = event.handedness

    async def on_readiness_changed(self, event: LaunchMonitorReadinessChanged) -> None:
        self._ready = event.ready
        self._monitor_status = "READY" if event.ready else "FINDING BALL"
        if event.ready:
            await self._dismiss_round_result()
        self._present()

    async def on_link_changed(self, event: LaunchMonitorLinkChanged) -> None:
        if event.state is LinkState.BRIDGED:
            return
        # Everything the device told us about the mat died with the connection.
        # Keeping the last "ball present" would leave the marker hidden for the
        # rest of the evening, with nothing left to clear it.
        self._ball = Detection.UNKNOWN
        self._ready = None
        self._monitor_status = ""
        self._present()

    async def on_shot(self, event: ShotIngested) -> None:
        # The club is attached here rather than at the boundary: no launch
        # monitor names the club in its shot message, so knowing which club hit
        # this shot is knowledge only the domain has.
        shot = replace(event.shot, club=self._club)
        self._shot_id = shot.shot_id
        # The floor first, then the announcement. A player is standing in front
        # of the projector waiting for a number; a subscriber is not. A
        # rendering failure must not erase the shot from history, video, or
        # coaching: those subscribe to ShotCompleted, not to the projector.
        # `_showing_shot` is set only after the floor accepts the shot, so a
        # presentation failure cannot leave the address marker suppressed with
        # no display timer to restore it.
        try:
            self._floor.show_shot(shot)
            generation = self._invalidate_shot_display()
            await self._cancel_display_timer()
            self._showing_shot = True
            self._round_result_up = False
            # Whatever was said about the previous shot is now about the wrong swing.
            await self._cancel_advice_timer()
            self._floor.clear_advice()
            self._present()
            self._arm_display_timer(generation, self._shot_display_seconds)
        except Exception:
            logger.exception("play_floor_presentation_failed")
        await self._publish(ShotCompleted(shot=shot))

    async def on_shot_dispatched(self, _event: ShotDispatched) -> None:
        self._monitor_status = "SHOT SENT"
        self._present()

    async def on_round_shot(self, event: RoundShotObserved) -> None:
        """Show a GSPro round-play result. Does not become a launch-monitor Shot."""

        try:
            self._floor.show_round_result(event.shot)
            generation = self._invalidate_shot_display()
            await self._cancel_display_timer()
            self._showing_shot = True
            self._round_result_up = True
            await self._cancel_advice_timer()
            self._floor.clear_advice()
            self._present()
            self._arm_display_timer(generation, self._shot_result_max_duration)
        except Exception:
            logger.exception("play_floor_presentation_failed")

    # --- what CaddieJ concluded -------------------------------------------

    async def on_shot_diagnosed(self, event: ShotDiagnosed) -> None:
        """Show the templated verdict, which needed nothing to produce."""

        diagnosis = event.diagnosis
        if not diagnosis.is_worth_saying:
            # A good shot. The numbers speak for themselves.
            return
        await self._show_advice(
            diagnosis.shot_id, diagnosis.display, diagnosis.severity
        )

    async def on_advice_ready(self, event: AnalysisCompleted) -> None:
        """Replace it with the coached wording, if the shot is still current."""

        advice = event.advice
        await self._show_advice(advice.shot_id, advice.display, advice.severity)

    async def _show_advice(self, shot_id: UUID, text: str, severity: float) -> None:
        if not text:
            return
        if self._shot_id is not None and shot_id != self._shot_id:
            # About a shot the player has moved on from. Showing it would
            # attach coaching to the wrong swing, which is worse than silence.
            logger.info("play_advice_ignored_as_stale", extra={"shot_id": str(shot_id)})
            return
        self._floor.show_advice(text, severity)
        self._restart_advice_timer()

    # --- deciding what is on the floor ------------------------------------

    def _present(self) -> None:
        """Put the address marker in whichever state the situation calls for."""

        self._floor.show_play_status(self._monitor_status, self._club)
        if self._should_show_address():
            self._floor.show_address(self._club)
        else:
            self._floor.hide_address()

    def _should_show_address(self) -> bool:
        if self._showing_shot:
            # The floor is busy saying what just happened.
            return False
        if self._ball is Detection.PRESENT:
            return False
        # A device that reports nothing about readiness is not a device that is
        # not ready, so only an explicit "no" suppresses the marker.
        return self._ready is not False

    async def _dismiss_round_result(self) -> None:
        """Take a GSPro result off as soon as the next ball / READY is real."""

        if not self._round_result_up:
            return
        self._invalidate_shot_display()
        await self._cancel_display_timer()
        self._showing_shot = False
        self._round_result_up = False
        self._floor.clear_shot()

    def _invalidate_shot_display(self) -> int:
        """Retire any in-flight timeout so it cannot hide a newer result."""

        self._display_generation += 1
        return self._display_generation

    def _arm_display_timer(self, generation: int, seconds: float) -> None:
        self._display_timer = asyncio.create_task(
            self._display_shot_for_a_while(generation, seconds),
            name="floor-shot-display",
        )

    async def _cancel_display_timer(self) -> None:
        timer = self._display_timer
        self._display_timer = None
        if timer is None:
            return
        timer.cancel()
        try:
            await timer
        except asyncio.CancelledError:
            pass

    async def _display_shot_for_a_while(self, generation: int, seconds: float) -> None:
        try:
            await self._sleep(seconds)
            if generation != self._display_generation:
                return
            self._showing_shot = False
            self._round_result_up = False
            self._floor.clear_shot()
            self._present()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A floor that fails to clear must not leave the flow believing a
            # shot is still on screen, or the marker would never come back.
            logger.exception("play_shot_display_failed")
            self._showing_shot = False
            self._round_result_up = False

    def _restart_advice_timer(self) -> None:
        timer = self._advice_timer
        if timer is not None:
            timer.cancel()
        self._advice_timer = asyncio.create_task(
            self._display_advice_for_a_while(), name="floor-advice-display"
        )

    async def _cancel_advice_timer(self) -> None:
        timer = self._advice_timer
        self._advice_timer = None
        if timer is None:
            return
        timer.cancel()
        try:
            await timer
        except asyncio.CancelledError:
            pass

    async def _display_advice_for_a_while(self) -> None:
        try:
            await self._sleep(self._advice_display_seconds)
            self._floor.clear_advice()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("play_advice_display_failed")
