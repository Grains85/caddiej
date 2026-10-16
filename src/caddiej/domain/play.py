"""What the floor shows as a hole is played.

This is the first piece of CaddieJ that is neither an adapter nor a transport: it
holds an opinion about golf. It watches the facts the launch monitor and the
simulator report, keeps the small amount of state needed to make sense of them,
and asks the floor for what a player should be looking at.

The rules it enforces, in the order a shot actually happens:

* the hitting area is lit for as long as the floor is up, because it is a fact
  about the room rather than about a shot;
* the club in play decides which address marker is shown, and where, and
  whether the floor is presenting a putt at all;
* a ball on the mat hides the marker, because the projector would otherwise be
  shining onto the ball the player is about to hit, and the already-lit
  hitting strip gives one short pulse so the find is obvious without a caption;
* no ball on the mat brings it back, whatever the launch monitor thinks of
  itself, because placement is the one thing the marker is for;
* one information area holds either what the last shot did or what the next one
  needs, never both, because they are the same numbers in the same place and
  reading them stacked is reading neither;
* a shot replaces the address with what just happened;
* a GSPro round result is readable for a guaranteed minimum, then gives way to
  the next address cycle, and in any case not past a configurable cap;
* while waiting for the next ball, the address marker returns even if that result
  is still on screen;
* a coaching line appears beside it and has its own lifetime;
* once that has been read, the floor returns to showing where the next ball goes.

The device's own words for its state — READY, FINDING BALL — are kept here and
published, because the marker and the information area both branch on them and
because they are the first thing to look at when a bay misbehaves. They are not
the words on the floor. The floor translates them, at the rear edge of the
strip, into BALL READY or PLACE BALL. A player standing over a ball still gets
the distance to the pin; the chamfer only says whether a ball is there to hit.

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

Coaching progress follows the same currency as the line, for the same reason:
a bar that says "analyzing" over the wrong swing is the line's mistake made
before the line exists. It is taken down by whatever ends the work it
describes — the line arriving, the line failing, a templated verdict settling
it — and by the next strike, which makes all of that work stale at once. It
also has a ceiling of its own, because the events that end it are droppable
and a bar that outlived its job would be a floor that never stopped thinking.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from uuid import UUID

from caddiej.core.event_bus import EventBus
from caddiej.core.events import Event
from caddiej.domain.bag import (
    ClubDistanceProvider,
    ClubRecommendation,
    recommend_club,
)
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisFailed,
    AnalysisStarted,
    BallCleared,
    BallDetected,
    ClubSelected,
    CoachingProgress,
    HandednessChanged,
    LaunchMonitorLinkChanged,
    LaunchMonitorReadinessChanged,
    LinkState,
    RoundShotObserved,
    ShotCompleted,
    ShotDetected,
    ShotDiagnosed,
    ShotDispatched,
    ShotIngested,
    TargetDistanceObserved,
)
from caddiej.domain.ports import CoachingResult, FloorPort, LessonLine, PhaseLine
from caddiej.domain.shot import Club, Detection, Handedness, Shot
from caddiej.domain.swing_phase import phase_label
from caddiej.domain.swing_review import SwingReview
from caddiej.domain.swing_score import SwingScore

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]
Publish = Callable[[Event], Awaitable[None]]
_Line = tuple[UUID, str, float, CoachingResult | None]

# The longest any one coaching stage may stay on the floor without something
# ending it. Longer than the slowest real stage — a 30 s model request inside
# its budget — so it never cuts a live one short, and short enough that a lost
# completion costs a minute of bar rather than the rest of the session.
PROGRESS_MAX_SECONDS = 60.0

# One pulse, then the pad is a work light again. Long enough to read as "found",
# short enough that it is gone before the player is over the ball.
LOCK_ACK_SECONDS = 0.65

# How long a detection stays the explanation for the next result. GSPro was
# measured finishing a ball 2.6 to 9.3 seconds after the trigger across the
# 2026-09-16 session; thirty seconds is generous against that and still short
# enough that a result with no detection behind it says so instead of
# borrowing one from an earlier hole.
_TRIGGER_MAX_AGE_NS = 30_000_000_000

# How many unreported detections to keep. Deep enough that a rapid-fire
# sequence is matched ball for ball, shallow enough that a simulator which has
# stopped reporting cannot grow this for the length of a session.
_TRIGGER_BACKLOG = 8


class PlayFlow:
    """Drives the floor from what the room reports.

    One instance owns the current state of play, so it must be driven from a
    single task. Its subscriptions share the subscriber name `play`, so they
    share one worker and keep their order.

    The display timers are the exception, and the one place to be careful when
    changing anything here. Each runs as its own task and writes the same state
    the worker does, coordinated only by `_display_generation`: a timer checks
    the generation it was armed with after waking and before touching anything,
    and every path that changes what is on the floor bumps it first. That works
    because no timer suspends between checking the generation and writing —
    inserting an `await` between those two lines is how a stale timer gets to
    clear a newer result. Each timer also owns whatever it was holding: a path
    that takes the result off the floor releases or drops the advice waiting
    for it, rather than leaving it to be forgotten.

    The coached line is deliberately not among them. It is cleared by events
    and never by a clock — see `__init__` — so the ways it leaves the floor are
    the next strike, the next completed shot, the next round result, a newer
    line replacing it, and a ball being positively found on the mat.

    That last one was the opposite way round until the first hardware test.
    Holding the lesson until the next strike reads well and plays badly: a
    player with a ball down is past the lesson and choosing a club, and the
    floor cannot show them a distance to the pin while last swing's coaching is
    still sitting on it. See `_retire_coaching_for_new_ball` for what counts as
    found, which is narrower than the device saying anything about the mat.
    """

    def __init__(
        self,
        floor: FloorPort,
        publish: Publish,
        shot_display_seconds: float = 6.0,
        shot_result_min_duration: float = 8.0,
        shot_result_max_duration: float = 30.0,
        sleep: Sleep = asyncio.sleep,
        club_distances: ClubDistanceProvider | None = None,
        lock_ack_seconds: float = LOCK_ACK_SECONDS,
        progress_max_seconds: float = PROGRESS_MAX_SECONDS,
    ) -> None:
        self._floor = floor
        self._publish = publish
        # Where the player's carries come from, asked fresh every time a
        # recommendation is wanted. None means nobody has measured a bag, and
        # the floor then shows a distance with no club beside it rather than a
        # club CaddieJ cannot justify.
        self._club_distances = club_distances
        self._shot_display_seconds = shot_display_seconds
        # A coached line has no timer of its own and is not swept off the
        # floor by a clock.
        #
        # It used to be, after nine seconds, and that number was indefensible
        # from the moment the ProTee path existed: the line arrives six to
        # twelve seconds after the strike, the player is usually still
        # watching the ball flight when it lands, and the whole result —
        # score, picture, observation, instruction, four readings — was gone
        # before it had been read once. There is also nothing for a blank
        # floor to be better than. Between two shots a player is picking a
        # club and setting up; the last swing's score sitting there is the
        # only thing on the projector worth looking at, and it is exactly
        # what somebody practising wants to see while they think about the
        # next one.
        #
        # What replaces it is the rule coaching staleness already used: a ball
        # placed on the mat is not a new shot, and a ball struck is. So the
        # line stays until the next strike, the next completed shot or the
        # next round result replaces it, and the ball status lamp changing to
        # BALL READY underneath it changes nothing.
        #
        # How long an observer round result is guaranteed to stay readable, and
        # the longest it may stay at all. Both are needed because the two ends of
        # the lifecycle fail in opposite directions.
        #
        # The result cannot appear until GSPro has finished flying the ball, which
        # on real hardware is three to six seconds after the strike. The next
        # address cycle — a ball back on the mat, the monitor ready again — was
        # measured arriving as little as ten seconds after the strike, so
        # dismissing on that alone left roughly four seconds of a number the
        # player had asked for. The minimum is a floor under that.
        #
        # It is a floor and not a hold: a dismissal that arrives inside it is
        # remembered and applied the moment it expires, so the player never waits
        # out the cap to get the address back. The address marker is not held at
        # all — it returns as soon as the room asks for it, with the result above
        # it, because the two occupy different parts of the surface.
        self._shot_result_min_duration = shot_result_min_duration
        self._shot_result_max_duration = shot_result_max_duration
        self._sleep = sleep
        self._lock_ack_seconds = lock_ack_seconds
        self._progress_max_seconds = progress_max_seconds

        self._club: Club | None = None
        self._ball = Detection.UNKNOWN
        self._ready: bool | None = None
        self._handedness = Handedness.UNKNOWN
        self._monitor_status = ""
        self._target_distance_raw: float | None = None
        self._showing_shot = False
        self._round_result_up = False
        self._waiting_for_next_ball = False
        self._club_from_completed_shot = False
        self._display_generation = 0
        self._display_timer: asyncio.Task[None] | None = None
        self._result_readable = False
        self._result_dismissal_deferred = False
        self._readable_timer: asyncio.Task[None] | None = None
        self._shot_id: UUID | None = None
        # The last strike ProTee reported, by its `ShotDetected` event id. The
        # ProTee-only coach keys its advice on this, because without GSPro
        # there is no shot id at all. A strike makes earlier advice stale, and
        # since the first hardware test so does the next ball being found.
        self._strike_id: UUID | None = None
        self._last_trigger_id: str | None = None
        self._shown_advice: _Line | None = None
        # Coaching that arrived while the result was still inside its guaranteed
        # window. Held rather than shown, because a player reading how far the
        # ball went and a player reading what to change are doing two different
        # things, and the floor doing both at once is the floor doing neither.
        self._pending_advice: _Line | None = None
        # Shots whose coaching a newly placed ball has already taken down. Kept
        # so that a model answering late cannot put the last swing's lesson back
        # over this one's address information. Emptied by the next strike.
        self._coaching_retired: frozenset[UUID] = frozenset()
        self._lock_ack_timer: asyncio.Task[None] | None = None
        # The stage on the floor and the work it belongs to. Remembered so that
        # nothing is asked of the floor when there is nothing to take down, and
        # so a log line can say whose progress was retired.
        self._progress: CoachingProgress | None = None
        self._progress_timer: asyncio.Task[None] | None = None
        # What the information area is currently saying, and what CaddieJ last
        # recommended. Both exist only so the log records a change rather than a
        # state: `_present` runs on every device poll, and a line each time
        # would bury the two transitions that matter in a few thousand that do
        # not.
        self._information_mode = ""
        self._recommendation: ClubRecommendation | None = None
        # The launch monitor's own counter for the last strike it detected,
        # with the bus clock reading at which it arrived. Carried onto
        # `ShotCompleted` so the two halves of a swing — the camera trigger and
        # the simulator's result, seconds apart on this hardware — can be
        # joined by something other than each subscriber's private guess.
        # A queue rather than one slot, because more than one ball can be in
        # the air as far as this is concerned: GSPro reports a result seconds
        # after the trigger, and the next trigger can beat it. Bounded because
        # nothing else empties it if the simulator stops reporting.
        self._triggers: deque[tuple[str, int]] = deque(maxlen=_TRIGGER_BACKLOG)

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
        bus.subscribe(ShotDetected, self.on_shot_detected, name="play")
        bus.subscribe(ShotDispatched, self.on_shot_dispatched, name="play")
        bus.subscribe(ShotIngested, self.on_shot, name="play")
        bus.subscribe(RoundShotObserved, self.on_round_shot, name="play")
        bus.subscribe(TargetDistanceObserved, self.on_target_distance, name="play")
        bus.subscribe(ShotDiagnosed, self.on_shot_diagnosed, name="play")
        bus.subscribe(AnalysisStarted, self.on_analysis_started, name="play")
        bus.subscribe(AnalysisCompleted, self.on_advice_ready, name="play")
        bus.subscribe(AnalysisFailed, self.on_analysis_failed, name="play")
        bus.subscribe(CoachingProgress, self.on_coaching_progress, name="play")

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

        The hitting area is lit here, once, and nothing below ever puts it out.
        It is a fact about the room rather than about a shot: the turf is where
        the player stands whether a ball has been detected, a monitor is ready,
        a result is up, or a replay is playing. Tying it to the address cycle —
        which is what used to happen — meant a projector that went dark the
        moment a ball landed on the mat.
        """

        self._floor.light_hitting_area()
        self._present()

    async def aclose(self) -> None:
        # The only thing that may take the turf light away, because this is the
        # only place that knows the floor is going away with it.
        self._floor.darken_hitting_area()
        await self._cancel_display_timer()
        await self._cancel_readable_timer()
        self._pending_advice = None
        await self._cancel_lock_ack()
        timer = self._progress_timer
        self._progress_timer = None
        if timer is not None:
            timer.cancel()
            try:
                await timer
            except asyncio.CancelledError:
                pass

    # --- what the room reports --------------------------------------------

    async def on_club_selected(self, event: ClubSelected) -> None:
        self._club = event.club
        self._club_from_completed_shot = False
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
        found = self._ball is not Detection.PRESENT
        self._ball = Detection.PRESENT
        self._waiting_for_next_ball = False
        await self._dismiss_round_result()
        if found:
            # The transition, not the observation. ProTee writes relative
            # ballPos while the ball stays on the mat; pulsing on each of
            # those would be a strobe, not an acknowledgement.
            self._acknowledge_lock()
            self._retire_coaching_for_new_ball()
        self._present()

    async def on_ball_cleared(self, _event: BallCleared) -> None:
        self._ball = Detection.ABSENT
        await self._cancel_lock_ack()
        self._present()

    async def on_handedness_changed(self, event: HandednessChanged) -> None:
        # No call to `_present()`: the address marker does not depend on this,
        # and repainting the floor because somebody turned around would be a
        # change nobody asked for.
        self._handedness = event.handedness

    async def on_readiness_changed(self, event: LaunchMonitorReadinessChanged) -> None:
        self._ready = event.ready
        self._monitor_status = "READY" if event.ready else "FINDING BALL"
        if self._waiting_for_next_ball and self._ball is Detection.PRESENT:
            # The previous shot's ball detection is still sticky, and whichever
            # way the device has just gone, it has stopped talking about the ball
            # that was hit: READY means it has found the next one, FINDING BALL
            # means it is looking for one. Either way this is a new placement
            # cycle, so the marker comes back and a later relative ballPos hides
            # it again.
            #
            # FINDING BALL counting here is the other half of the marker
            # regression. On real hardware that is the state between shots, and
            # leaving the ball sticky through it meant the marker stayed hidden
            # from the first shot of a session until somebody restarted CaddieJ.
            self._ball = Detection.UNKNOWN
        if event.ready:
            await self._dismiss_round_result()
            if self._club_from_completed_shot:
                # The completed shot must not own the next address. If ProTee
                # has not yet named the upcoming club, the default tee is used
                # until that detection arrives.
                self._club = None
                self._club_from_completed_shot = False
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
        self._forget_target_distance()
        await self._cancel_lock_ack()
        self._present()

    async def on_shot(self, event: ShotIngested) -> None:
        # The club is attached here rather than at the boundary: no launch
        # monitor names the club in its shot message, so knowing which club hit
        # this shot is knowledge only the domain has.
        shot = replace(event.shot, club=self._club)
        self._shot_id = shot.shot_id
        # Same reasoning as `on_shot_dispatched`, for the interpose path, which
        # does not produce a dispatch of its own.
        self._forget_target_distance()
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
            await self._cancel_readable_timer()
            self._showing_shot = True
            self._round_result_up = False
            # A new shot outranks the previous result's readable window: the
            # player is looking at what just happened, not at what did.
            self._result_readable = False
            self._result_dismissal_deferred = False
            # Whatever was said about the previous shot is now about the wrong swing.
            self._drop_pending_advice("a newer shot arrived")
            self._shown_advice = None
            self._floor.clear_advice()
            self._present()
            self._arm_display_timer(generation, self._shot_display_seconds)
        except Exception:
            logger.exception("play_floor_presentation_failed")
        # The interpose path may have no strike to retire earlier work, so the
        # shot itself does; progress about this ball's own strike survives it.
        held = self._progress
        if held is not None and not self._progress_is_current(held):
            self._retire_progress("a newer shot arrived")
        trigger_id = self._trigger_for(event)
        self._log_shot_completed(shot, trigger_id)
        await self._publish(ShotCompleted(shot=shot, trigger_id=trigger_id))

    async def on_shot_detected(self, event: ShotDetected) -> None:
        """Queue the detection. Changes nothing on screen.

        The floor already reacts to the ball leaving through readiness and ball
        detection, and a strike is not itself something to draw.
        """

        if event.trigger_id and event.trigger_id == self._last_trigger_id:
            # ProTee re-logging the same strike is not a second ball.
            return
        self._last_trigger_id = event.trigger_id or None
        self._triggers.append((event.trigger_id, event.monotonic_ns))
        self._strike_id = event.event_id
        self._shot_id = None
        logger.info(
            "shot_detected",
            extra={
                "shot_trigger_id": event.trigger_id,
                "coaching_strike_id": str(event.event_id),
                "coaching_detail": "strike detected; earlier coaching is now stale",
            },
        )
        self._retire_progress("a newer strike was detected")
        # This strike's own coaching has not happened yet, so nothing about it
        # can have been retired by a ball. Forgetting here keeps the set at the
        # two ids one placement can close rather than a session's worth.
        self._coaching_retired = frozenset()
        held = self._pending_advice
        shown = self._shown_advice
        if held is not None or shown is not None:
            self._drop_pending_advice("a newer strike was detected")
            self._shown_advice = None
            try:
                self._floor.clear_advice()
            except Exception:
                logger.exception("play_floor_presentation_failed")
            if shown is not None:
                logger.info(
                    "play_advice_cleared",
                    extra={
                        "shot_id": str(shown[0]),
                        "coaching_detail": "a newer strike was detected",
                    },
                )

    def _retire_coaching_for_new_ball(self) -> None:
        """Give the floor back to the next shot, now that there is a next ball.

        Coach V3 kept the coached line up through BALL READY and took it down
        only at the next strike. That was chosen on the reasoning that a player
        should not lose their lesson to a sensor, and on the mat it was simply
        wrong: by the time the next ball is down the player has read the
        lesson and is choosing a club, and the floor is the only place the
        distance to the pin and the club can go. So a ball positively found is
        now the end of the previous shot's coaching.

        "Positively found" is the transition into a detected ball, which is the
        narrowest thing that can be called a new placement. ProTee's noise does
        not reach here: `FINDING BALL` publishes `BallCleared` and a readiness
        change, neither of which clears anything, and a repeated relative
        ballPos within one placement is not a transition. Something has to have
        been struck, too — the floor is not cleared on the first ball of the
        evening, because there is nothing on it to clear.

        The retired ids are remembered because clearing the floor is only half
        of it. The coach can still be mid-request about the previous swing, and
        `_is_current` would happily call that advice current right up until the
        next strike replaces `_strike_id`. A lesson about the last ball must not
        appear over the address information for this one, however late it is.
        """

        if self._strike_id is None and self._shot_id is None:
            return
        held = self._pending_advice
        shown = self._shown_advice
        self._coaching_retired = frozenset(
            shot_id for shot_id in (self._strike_id, self._shot_id) if shot_id
        )
        if held is None and shown is None:
            return
        self._drop_pending_advice("a new ball was detected")
        self._shown_advice = None
        try:
            self._floor.clear_advice()
        except Exception:
            logger.exception("play_floor_presentation_failed")
        if shown is not None:
            logger.info(
                "play_advice_cleared",
                extra={
                    "shot_id": str(shown[0]),
                    "coaching_detail": "a new ball was detected",
                },
            )

    def _is_current(self, shot_id: UUID) -> bool:
        """Whether advice about this shot or strike still describes the last ball."""

        if shot_id in self._coaching_retired:
            return False
        if self._shot_id is None and self._strike_id is None:
            return True
        return shot_id in (self._shot_id, self._strike_id)

    def _trigger_for(self, event: Event) -> str | None:
        """The detection a result belongs to: the oldest one still unclaimed.

        Oldest rather than newest, and consumed once used. GSPro was measured
        finishing a ball 2.6 to 9.3 seconds after the trigger, and a second
        ball inside that window is ordinary — a mulligan, a chip and an
        immediate re-hit, two off a range pile. Reading the newest detection
        meant the first ball's numbers were published under the *second*
        ball's trigger, and since video and swing capture both join on that
        id, the numbers of one swing were handed to a coach alongside the
        pictures of another. There is no worse answer available: a model shown
        the wrong swing does not hedge, it explains what it sees.

        Results arrive in the order the balls were struck, so the oldest
        unclaimed detection is the one that caused this one. What this cannot
        see is a detection that produces no result at all — a strike GSPro
        never recorded. That leaves the queue one ahead, which costs the next
        result its correct trigger and then ages out. One shot either way, but
        the case this now gets right is the common one and the case it gets
        wrong needs ProTee to log a strike the simulator never received.
        """

        self._expire_triggers(event.monotonic_ns)
        if not self._triggers:
            return None
        trigger_id, _detected_ns = self._triggers.popleft()
        if self._triggers:
            # Worth a line on real hardware: detections still unclaimed when a
            # result is matched is what divergence between the two looks like
            # from in here, and how often it happens is not yet known.
            logger.info(
                "play_detections_outstanding",
                extra={
                    "shot_trigger_id": trigger_id,
                    "trigger_backlog": len(self._triggers),
                },
            )
        return trigger_id or None

    def _expire_triggers(self, now_ns: int) -> None:
        """Drop detections too old to explain anything arriving now.

        Without this a detection nothing ever reported would sit at the head
        of the queue for the rest of the session and every later result would
        be one behind it.
        """

        while self._triggers and now_ns - self._triggers[0][1] > _TRIGGER_MAX_AGE_NS:
            trigger_id, _detected_ns = self._triggers.popleft()
            logger.info(
                "play_detection_unclaimed",
                extra={"shot_trigger_id": trigger_id},
            )
            logger.info(
                "coaching_advice_suppressed",
                extra={
                    "shot_trigger_id": trigger_id,
                    "coaching_reason": "no_round_result",
                    "coaching_detail": (
                        "ProTee reported the strike but GSPro did not write "
                        "a measured round shot before the trigger expired; "
                        "coaching was not started"
                    ),
                },
            )

    async def on_shot_dispatched(self, _event: ShotDispatched) -> None:
        self._monitor_status = "SHOT SENT"
        # The ball has gone, so the distance it was going to be played over is
        # history. GSPro sends the next one when it has finished flying the
        # ball — measured at 2.6 to 9.3 s after the trigger across the
        # 2026-09-16 session — and until then CaddieJ knows no current target.
        # Keeping the old number would put the distance the player just played
        # *from* under a label that says how far is left.
        self._forget_target_distance()
        self._present()

    async def on_target_distance(self, event: TargetDistanceObserved) -> None:
        if event.distance_raw != self._target_distance_raw:
            logger.info(
                "address_target_updated",
                extra={
                    "target_distance_m": event.distance_raw,
                    "target_distance_source": event.source,
                },
            )
        self._target_distance_raw = event.distance_raw
        self._present()

    def _forget_target_distance(self) -> None:
        self._target_distance_raw = None
        self._recommendation = None

    async def on_round_shot(self, event: RoundShotObserved) -> None:
        """Show a round-play result, and announce the shot behind it.

        Two things, in that order, because the player is waiting for one of
        them and nothing is waiting for the other. The result is what the floor
        shows; the announcement is what history, video and coaching work from,
        and without it a passively observed session is never coached at all —
        which is how CaddieJ actually runs.
        """

        # Coaching already keyed on the current strike is about this very ball:
        # GSPro writing its result late is not a newer shot. Held back behind
        # the result's readable window rather than dropped.
        keep = self._strike_advice()
        try:
            self._floor.show_round_result(event.shot)
            generation = self._invalidate_shot_display()
            await self._cancel_display_timer()
            await self._cancel_readable_timer()
            self._showing_shot = True
            self._round_result_up = True
            self._waiting_for_next_ball = True
            self._club_from_completed_shot = True
            self._result_readable = True
            self._result_dismissal_deferred = False
            if keep is None:
                self._drop_pending_advice("a newer result arrived")
            self._pending_advice = keep
            self._shown_advice = None
            self._floor.clear_advice()
            self._present()
            self._arm_readable_timer(generation)
            self._arm_display_timer(generation, self._shot_result_max_duration)
        except Exception:
            logger.exception("play_floor_presentation_failed")

        if event.measured is None:
            # A round entry with no measurement in it. The result panel is
            # still right; there is simply nothing for anything downstream to
            # work from, and announcing an empty shot would mean coaching a
            # swing that was never measured.
            logger.info(
                "coaching_advice_suppressed",
                extra={
                    "coaching_reason": "no_measurement",
                    "coaching_detail": (
                        "the round entry had no verified ball speed; "
                        "coaching was not started"
                    ),
                },
            )
            return
        # The club and the handedness are attached here for the same reason
        # they are on the interpose path: neither is in the shot data, and
        # which club is in play is knowledge only the domain has. On this path
        # both came from the launch monitor's own log.
        shot = replace(event.measured, club=self._club, handedness=self._handedness)
        self._shot_id = shot.shot_id
        trigger_id = self._trigger_for(event)
        self._log_shot_completed(shot, trigger_id)
        await self._publish(ShotCompleted(shot=shot, trigger_id=trigger_id))

    # --- what CaddieJ concluded -------------------------------------------

    async def on_shot_diagnosed(self, event: ShotDiagnosed) -> None:
        """Show the templated verdict, which needed nothing to produce."""

        diagnosis = event.diagnosis
        self._settle_progress(diagnosis.shot_id, "a verdict settled the swing")
        score = _score_value(event.swing_score)
        scored = CoachingResult(
            swing_score=score,
            metrics=event.metrics,
            phases=_phase_lines(event.swing_score),
        )
        result = None if scored.is_empty else scored
        if not diagnosis.is_worth_saying:
            if result is not None:
                # Nothing to say, and still a number: the readings were good
                # enough to score and not to name a fault.
                await self._show_advice(diagnosis.shot_id, "", 0.0, result)
                return
            # Nothing was measured well enough to say anything. Not the same as
            # a good shot, which is a reinforcement and does reach the floor.
            missing = ", ".join(diagnosis.unmeasured) or "no ranked measurement"
            logger.info(
                "play_advice_suppressed",
                extra={
                    "shot_id": str(diagnosis.shot_id),
                    "coaching_reason": "insufficient_evidence",
                    "coaching_detail": f"missing: {missing}",
                    "coaching_focus": diagnosis.focus.value,
                    "coaching_baseline_shots": diagnosis.baseline_shots,
                },
            )
            self._clear_placeholder(diagnosis.shot_id)
            return
        await self._show_advice(
            diagnosis.shot_id, diagnosis.display, diagnosis.severity, result
        )

    async def on_coaching_progress(self, event: CoachingProgress) -> None:
        """Hold the stage the current swing's coaching is in. Never anyone else's.

        Currency is decided exactly as it is for the line: by the shot or
        strike id when one exists, and by the device's trigger before one does.
        Anything else is a job the player has already moved on from, and its
        progress is not drawn at all rather than drawn and then corrected.
        """

        if not self._progress_is_current(event):
            logger.info(
                "play_progress_ignored_as_stale",
                extra={
                    "shot_id": "" if event.shot_id is None else str(event.shot_id),
                    "shot_trigger_id": event.trigger_id,
                    "coaching_progress_stage": event.stage.value,
                    "coaching_detail": "a newer strike or shot owns the floor",
                },
            )
            return
        self._progress = event
        try:
            self._floor.show_progress(event.stage)
        except Exception:
            logger.exception("play_floor_presentation_failed")
        timer = self._progress_timer
        if timer is not None:
            timer.cancel()
        # Cancelled rather than generation-guarded, for the reason set out in
        # `_acknowledge_lock`.
        self._progress_timer = asyncio.create_task(
            self._expire_progress(), name="floor-progress"
        )

    def _progress_is_current(self, event: CoachingProgress) -> bool:
        if event.shot_id is not None:
            return self._is_current(event.shot_id)
        return bool(event.trigger_id) and event.trigger_id == self._last_trigger_id

    def _settle_progress(self, shot_id: UUID, why: str) -> None:
        """Work on this swing has ended, so its progress goes, if it is current.

        A stale ending changes nothing: whatever is on the floor belongs to a
        newer job, which this event knows nothing about.
        """

        if self._is_current(shot_id):
            self._retire_progress(why)

    def _retire_progress(self, why: str) -> None:
        held = self._progress
        timer = self._progress_timer
        self._progress = None
        self._progress_timer = None
        if timer is not None:
            timer.cancel()
        if held is None:
            return
        try:
            self._floor.clear_progress()
        except Exception:
            logger.exception("play_floor_presentation_failed")
        logger.info(
            "play_progress_cleared",
            extra={
                "shot_id": "" if held.shot_id is None else str(held.shot_id),
                "shot_trigger_id": held.trigger_id,
                "coaching_progress_stage": held.stage.value,
                "coaching_detail": why,
            },
        )

    async def _expire_progress(self) -> None:
        try:
            await self._sleep(self._progress_max_seconds)
            self._progress_timer = None
            self._retire_progress("nothing ended it before the ceiling")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("play_progress_failed")

    async def on_analysis_started(self, event: AnalysisStarted) -> None:
        """Mark the current line as pending while a model is being asked."""

        if not self._is_current(event.shot_id):
            return
        if self._result_readable:
            return
        self._floor.show_advice_pending()

    async def on_analysis_failed(self, event: AnalysisFailed) -> None:
        """No line is coming. Take down an empty "thinking" mark if one is up."""

        self._settle_progress(event.shot_id, "the analysis failed")
        self._clear_placeholder(event.shot_id)

    def _clear_placeholder(self, shot_id: UUID) -> None:
        """Only the empty placeholder: a line already shown stays shown.

        On the ProTee-only path nothing is on the floor before the model has
        answered, so the pending mark is the whole of the advice area.
        """

        if self._shown_advice is not None or not self._is_current(shot_id):
            return
        try:
            self._floor.clear_advice()
        except Exception:
            logger.exception("play_floor_presentation_failed")

    async def on_advice_ready(self, event: AnalysisCompleted) -> None:
        """Replace it with the coached wording, if the shot is still current."""

        advice = event.advice
        self._settle_progress(advice.shot_id, "the coached line arrived")
        result = CoachingResult(
            observation=advice.observation,
            swing_score=_score_value(advice.swing_score),
            evidence=event.evidence,
            metrics=advice.metrics,
            phases=_phase_lines(advice.swing_score),
            progress=_lesson_line(advice.review),
        )
        await self._show_advice(
            advice.shot_id,
            advice.display,
            advice.severity,
            None if result.is_empty else result,
        )

    def _log_shot_completed(self, shot: Shot, trigger_id: str | None) -> None:
        """One line that ties a shot id to the trigger capture is using."""

        logger.info(
            "shot_completed",
            extra={
                "shot_id": str(shot.shot_id),
                "shot_trigger_id": trigger_id or "",
            },
        )

    async def _show_advice(
        self,
        shot_id: UUID,
        text: str,
        severity: float,
        result: CoachingResult | None = None,
    ) -> None:
        if not text and (result is None or result.swing_score is None):
            logger.info(
                "play_advice_suppressed",
                extra={
                    "shot_id": str(shot_id),
                    "coaching_reason": "empty_display",
                    "coaching_detail": "the diagnosis carried no floor text",
                },
            )
            return
        if not self._is_current(shot_id):
            # About a shot the player has moved on from. Showing it would
            # attach coaching to the wrong swing, which is worse than silence.
            logger.info(
                "play_advice_ignored_as_stale",
                extra={
                    "shot_id": str(shot_id),
                    "coaching_reason": "stale",
                    "coaching_detail": "a newer strike or shot owns the floor",
                },
            )
            return
        if self._result_readable:
            # The result is inside the window it is guaranteed. Coaching waits
            # for it rather than appearing beside it: the player asked for the
            # number, and one large phrase arriving over the top of it is how
            # both become unreadable.
            self._pending_advice = (shot_id, text, severity, result)
            logger.info(
                "play_advice_waiting_for_result", extra={"shot_id": str(shot_id)}
            )
            return
        self._floor.show_advice(text, severity, result)
        self._shown_advice = (shot_id, text, severity, result)
        # The last step of the shot that is visible to the player, and so the
        # one a hardware test needs in the log: everything before it can be
        # working with nothing on the projector.
        logger.info(
            "play_advice_shown",
            extra={"shot_id": str(shot_id), "coaching_display": text},
        )

    def _strike_advice(self) -> _Line | None:
        """Advice waiting or showing that is keyed on the current strike."""

        if self._strike_id is None:
            return None
        for held in (self._pending_advice, self._shown_advice):
            if held is not None and held[0] == self._strike_id:
                return held
        return None

    def _drop_pending_advice(self, why: str) -> None:
        held = self._pending_advice
        self._pending_advice = None
        if held is None:
            return
        logger.info(
            "play_advice_dropped",
            extra={"shot_id": str(held[0]), "coaching_detail": why},
        )

    async def _release_pending_advice(self) -> None:
        """Show coaching that waited out the result's window, if it still holds."""

        held = self._pending_advice
        self._pending_advice = None
        if held is None:
            return
        await self._show_advice(*held)

    # --- deciding what is on the floor ------------------------------------

    def _present(self) -> None:
        """Set each floor layer from what is true, independently of the others.

        Three decisions, and deliberately no `return` between them, because the
        bug this shape replaces was exactly one layer's branch skipping another
        layer's update:

        * the rear-edge status, which is the device state translated into
          BALL READY or PLACE BALL;
        * the information area, which is either the last result or the next
          shot and never both;
        * the ball marker, which follows the placement lifecycle alone.

        The hitting area is not here at all. It is lit at `start` and stays lit.
        """

        self._floor.show_play_status(self._monitor_status, self._club)
        self._floor.show_putting_guide(self._club)
        if self._should_show_address_hud():
            self._note_information_mode("address")
            self._floor.show_address_hud(
                self._club, self._target_distance_raw, self._current_recommendation()
            )
        else:
            self._note_information_mode("result" if self._showing_shot else "idle")
            self._floor.hide_address_hud()
        if self._should_show_address():
            self._floor.show_address(self._club)
        else:
            self._floor.hide_address()

    def _should_show_address_hud(self) -> bool:
        """What the next shot needs, whenever the last one is not still being read.

        Not gated on a ball being on the mat any more. READY and FINDING BALL
        are when a player is deciding what to hit, which is exactly when a
        distance to the pin and a club are worth having; waiting for the ball to
        be detected meant the information arrived at the moment it stopped being
        useful. What it *is* gated on is the result, because both occupy the
        same part of the surface and a number the player asked for must not be
        written over by one they did not.

        There is nothing to suppress when nothing is known: the floor adapter
        draws no panel for a target it does not have.
        """

        if self._showing_shot:
            return False
        return self._monitor_status != "SHOT SENT"

    def _current_recommendation(self) -> ClubRecommendation | None:
        """Ask for a club, and log it when the answer changes.

        Asked here rather than cached on the target distance because a provider
        that learns from shot history changes its mind between shots without
        anything publishing an event about it.
        """

        if self._club_distances is None:
            return None
        recommendation = recommend_club(
            self._target_distance_raw, self._club_distances.profile()
        )
        if recommendation != self._recommendation:
            self._recommendation = recommendation
            if recommendation is not None:
                logger.info(
                    "club_recommendation_updated",
                    extra={
                        "target_distance_m": self._target_distance_raw,
                        "recommended_club": recommendation.club,
                        "expected_carry_m": recommendation.expected_carry_m,
                    },
                )
        return recommendation

    def _note_information_mode(self, mode: str) -> None:
        if mode == self._information_mode:
            return
        self._information_mode = mode
        logger.info(
            "floor_information_mode_changed",
            extra={"floor_information_mode": mode},
        )

    def _should_show_address(self) -> bool:
        """Ball placement owns the marker. Readiness does not.

        This used to end with `return self._ready is not False`, and on real
        hardware that was exactly backwards. ProTee reports `FINDING BALL` —
        which CaddieJ reads as not ready — while it is *looking* for a ball,
        which is the one moment a player needs to be shown where to put one;
        and it reports `READY FOR SHOT` only once it has found one, which is
        the one moment the marker must be out of the way. The observed symptom
        was a marker that was visible at startup, when nothing had said
        anything about readiness, and then never came back: after the first
        shot `_ready` was `False` for the rest of the session.

        So the question is only whether there is a ball on the mat. A device
        that has said nothing, a device that says it is hunting, and a device
        that has gone away all mean the same thing here: no ball, so show where
        one goes. The result panel lives in its own part of the surface and
        does not hold the marker back.

        The one thing that does is an interpose shot, which draws a club path
        out of the ball position and would otherwise draw it through a marker.
        """

        if self._ball is Detection.PRESENT:
            return False
        if self._showing_shot and not self._round_result_up:
            return False
        return True

    async def _dismiss_round_result(self) -> None:
        """Take a GSPro result off for the next ball / READY, once it has been read.

        Inside the readable minimum this only records that the room has asked, so
        the panel survives a ball that is teed up straight away and comes off the
        instant the minimum is over. The caller's `_present()` still runs, so the
        address marker returns underneath it either way.
        """

        if not self._round_result_up:
            return
        if self._result_readable:
            self._result_dismissal_deferred = True
            logger.debug("play_round_result_still_readable")
            return
        await self._clear_round_result()

    async def _clear_round_result(self) -> None:
        self._invalidate_shot_display()
        await self._cancel_display_timer()
        await self._cancel_readable_timer()
        self._result_readable = False
        self._result_dismissal_deferred = False
        self._showing_shot = False
        self._round_result_up = False
        self._floor.clear_shot()

    def _invalidate_shot_display(self) -> int:
        """Retire any in-flight timeout so it cannot hide a newer result."""

        self._display_generation += 1
        return self._display_generation

    def _arm_readable_timer(self, generation: int) -> None:
        self._readable_timer = asyncio.create_task(
            self._keep_result_readable(generation),
            name="floor-result-readable",
        )

    async def _cancel_readable_timer(self) -> None:
        timer = self._readable_timer
        self._readable_timer = None
        if timer is None:
            return
        timer.cancel()
        try:
            await timer
        except asyncio.CancelledError:
            pass

    async def _keep_result_readable(self, generation: int) -> None:
        """Hold the guaranteed window, then honour a dismissal that arrived in it."""

        try:
            await self._sleep(self._shot_result_min_duration)
            if generation != self._display_generation:
                return
            # Cleared first: this task is the readable timer, and the clear it is
            # about to ask for cancels that timer.
            self._readable_timer = None
            self._result_readable = False
            # Coaching was held so the result could be read. It has its own
            # position and its own lifetime, so it is shown now whether or not
            # the next ball is already down. A ball on the mat is not a newer
            # shot: dropping the line here is how a bay that tees up inside
            # this window never sees coaching at all. A newer completed shot
            # still clears it, in `on_shot` and `on_round_shot`.
            await self._release_pending_advice()
            if not self._result_dismissal_deferred:
                return
            await self._clear_round_result()
            self._present()
        except asyncio.CancelledError:
            raise
        except Exception:
            # The result must not be left believing it is still protected, or
            # nothing after this would ever take it off before the cap.
            logger.exception("play_result_readable_failed")
            self._result_readable = False

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
            self._result_readable = False
            self._result_dismissal_deferred = False
            # The cap took the result off the floor, so the window coaching
            # was being held for is gone with it. Dropped rather than shown:
            # the result it belongs to is no longer on screen and a phrase
            # about a shot the player can no longer see is a phrase about
            # nothing. Without this the held line was neither shown nor
            # released nor logged — it simply stopped existing.
            self._drop_pending_advice("the result reached its maximum duration")
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
            self._result_readable = False
            self._result_dismissal_deferred = False
            self._drop_pending_advice("the result display failed")

    def _acknowledge_lock(self) -> None:
        """Publish one pulse, then take it off without blocking ingestion."""

        self._floor.acknowledge_ball_lock()
        timer = self._lock_ack_timer
        if timer is not None:
            timer.cancel()
        # Cancelling without awaiting is enough here, unlike the shot display
        # timer, which needs a generation. Nothing is read after this and the
        # replacement is armed immediately, and `cancel` is not a request that
        # can arrive too late: a task whose sleep has already expired but has
        # not run yet is still cancelled before its next line, because asyncio
        # holds the cancellation and raises it at the next step.
        self._lock_ack_timer = asyncio.create_task(
            self._expire_lock_acknowledgement(), name="floor-lock-ack"
        )
        logger.info(
            "floor_ball_lock_acknowledged",
            extra={"lock_ack_seconds": self._lock_ack_seconds},
        )

    async def _cancel_lock_ack(self) -> None:
        timer = self._lock_ack_timer
        self._lock_ack_timer = None
        if timer is not None:
            timer.cancel()
            try:
                await timer
            except asyncio.CancelledError:
                pass
        self._floor.clear_lock_acknowledgement()

    async def _expire_lock_acknowledgement(self) -> None:
        try:
            await self._sleep(self._lock_ack_seconds)
            self._lock_ack_timer = None
            self._floor.clear_lock_acknowledgement()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("play_lock_ack_failed")
            self._lock_ack_timer = None


def _score_value(score: SwingScore | None) -> int | None:
    """The number the floor may show, and None whenever there honestly is none.

    A score whose value is None is a swing too little was read of. It is not a
    zero, so it does not reach the floor as one.
    """

    return None if score is None else score.value


def _phase_lines(score: SwingScore | None) -> tuple[PhaseLine, ...]:
    """The scored phases, in swing order. An unscored phase is left out."""

    if score is None or score.mechanics is None:
        return ()
    return tuple(
        PhaseLine(label=phase_label(phase.phase), value=phase.value)
        for phase in score.mechanics.phases
        if phase.value is not None
    )


def _lesson_line(review: SwingReview | None) -> LessonLine | None:
    """The lesson's progress, once there is a previous shot to compare with."""

    progress = None if review is None else review.progress
    if progress is None:
        return None
    return LessonLine(
        label=phase_label(progress.phase),
        value=progress.score,
        delta=progress.delta,
        headline=progress.headline,
        verdict=progress.verdict.value,
        mode=progress.mode.value,
    )
