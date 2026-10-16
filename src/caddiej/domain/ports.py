"""What the domain needs from the outside world, stated in its own terms.

A port names a capability, not a technology. `FloorPort` is what "the floor" can
be asked to do; whether that reaches a projector through OBS, a browser in
kiosk mode, or nothing at all is not the domain's business. Nothing here mentions
markers, coordinates, colours or pixels: those are decisions about how to draw a
club, and they belong to whatever implements the port.

Ports are synchronous. Deciding what the floor should show happens in response to
a shot that has already been hit, and it must not be able to wait on a browser.
"""

from dataclasses import dataclass
from typing import Protocol

from caddiej.domain.bag import ClubRecommendation
from caddiej.domain.coaching import CoachMetric
from caddiej.domain.events import CoachingStage, EvidenceFrame
from caddiej.domain.observation import ObservedRoundShot
from caddiej.domain.shot import Club, Shot


@dataclass(frozen=True, slots=True)
class PhaseLine:
    """One scored phase as the floor writes it: `TRANSITION 54`."""

    label: str
    value: int


@dataclass(frozen=True, slots=True)
class LessonLine:
    """The session lesson's progress: `RELEASE 81 +38 MUCH BETTER RELEASE`.

    `delta` is against the previous shot the lesson was assessed on. Every
    field is CaddieJ's; `headline` is from a fixed set of reviewed words."""

    label: str
    value: int | None = None
    delta: int | None = None
    headline: str = ""
    verdict: str = ""
    mode: str = ""


@dataclass(frozen=True, slots=True)
class CoachingResult:
    """What a coached swing can show beside its action line.

    The headline that says what was seen, the swing's score, and the still
    that shows it — each optional, each independent of the others, and none of
    them a sentence. The action line stays the `text` it always was, so a floor
    that knows nothing of this still draws the instruction.

    `swing_score` is the integer and not the breakdown: the floor shows a
    number, and a renderer handed the components would be one step from
    re-weighting them. `evidence` names a frame and never carries a picture or
    a path, so the floor can only ever show the one the model was sent.
    `metrics` are already-formatted strings for the same reason the score is
    an integer — a floor that could compute a carry could disagree about one.
    """

    observation: str = ""
    swing_score: int | None = None
    evidence: EvidenceFrame | None = None
    metrics: tuple[CoachMetric, ...] = ()
    phases: tuple[PhaseLine, ...] = ()
    """The phases that were scored, in swing order. Integers for the same
    reason the Swing Score is one."""

    progress: LessonLine | None = None
    """How the session lesson moved on this shot, when there is one."""

    @property
    def is_empty(self) -> bool:
        return (
            not self.observation
            and self.swing_score is None
            and not self.evidence
            and not self.metrics
            and not self.phases
            and self.progress is None
        )


class FloorPort(Protocol):
    """The projected floor, as the domain thinks of it."""

    def light_hitting_area(self) -> None:
        """Light the physical hitting area, and leave it lit.

        The bay, not a shot: it is where the player stands, and it is as true
        while they are waiting as while they are swinging. Nothing else on this
        port may turn it off, which is the whole reason it is a call of its own
        rather than something `show_address` does on the way past. A projector
        that goes dark under a player's feet because a launch monitor changed
        its mind about a ball is a room that stopped working.
        """

    def darken_hitting_area(self) -> None:
        """Stop lighting it. Shutting down, and nothing else."""

    def show_address(self, club: Club | None) -> None:
        """Show the player where to place the ball for the club in play.

        A club of None means the club is unknown, which is normal before the
        simulator has said anything. The floor still has to show somewhere to
        put the ball.
        """

    def hide_address(self) -> None:
        """Stop showing where to place the ball."""

    def show_putting_guide(self, club: Club | None) -> None:
        """Present the floor for a putter, or for anything that is not one.

        One call in both directions, because "the club in play is a putter" is
        a single fact and a floor that is told it twice in two ways can end up
        believing both. What a putting floor looks like — a lane, a different
        ball position, the full-swing furniture left off — is a presentation
        decision and is not stated here.

        Called whenever the domain re-decides what is on the floor, not on the
        transition, so nothing has to remember which club was last in play.
        """

    def acknowledge_ball_lock(self) -> None:
        """Briefly confirm that the ball has been found.

        One pulse on the already-lit hitting area, then back to a steady light.
        Does not turn the turf light off, and is not a caption. Repeated
        observations that the ball is still there must not produce another
        pulse: the domain calls this on the absent-to-present transition only.
        """

    def clear_lock_acknowledgement(self) -> None:
        """Take the confirmation away. The turf light stays as it was."""

    def show_address_hud(
        self,
        club: Club | None,
        distance_raw: float | None,
        recommendation: ClubRecommendation | None = None,
    ) -> None:
        """Show what is worth knowing before the next shot.

        Only values that are known for the upcoming shot. Missing fields are
        omitted rather than invented, so a floor with nothing to say says
        nothing rather than drawing a dash under a label.

        The recommendation arrives already made. Which club a distance calls
        for is golf, and it is decided in `caddiej.domain.bag`; the floor is
        given the answer.
        """

    def hide_address_hud(self) -> None:
        """Take the pre-shot numbers off the floor."""

    def show_play_status(self, status: str, club: Club | None) -> None:
        """Tell the floor the launch monitor's current state.

        `status` is the device's own word (`READY`, `FINDING BALL`, and so on).
        Empty means the device has not said. The floor keeps that word for
        diagnosis and draws its own translation on the rear edge of the strip.
        """

    def show_shot(self, shot: Shot) -> None:
        """Show what just happened: the club's path and the launch numbers."""

    def show_round_result(self, shot: ObservedRoundShot) -> None:
        """Show a GSPro round-play result and its ball-flight path.

        Distinct from `show_shot`: the domain values stay raw. Any unit labels
        are a presentation choice in the floor adapter, not a change to the
        observation schema.
        """

    def clear_shot(self) -> None:
        """Take the last shot off the floor."""

    def show_advice(
        self, text: str, severity: float, result: CoachingResult | None = None
    ) -> None:
        """Put one short coaching line on the floor.

        Text and a number, and what the swing produced beside them. The floor
        is being told what to draw, not what to do, which is what keeps a line
        authored by a language model from being able to reach anything that
        matters. `text` may be empty only when `result` carries a score: a
        scored swing with nothing worth saying is still a number to show.
        """

    def show_advice_pending(self) -> None:
        """The current line stays, and the floor says a better one is coming."""

    def clear_advice(self) -> None:
        """Take the coaching line off the floor."""

    def show_progress(self, stage: CoachingStage) -> None:
        """Say which part of coaching the current swing is in.

        A stage and nothing else: how far through it is is not known, so it is
        not said. Never a replacement for the line or the result; it is drawn
        alongside whatever is already up.
        """

    def clear_progress(self) -> None:
        """Nothing is being worked on any more, or nothing current is."""
