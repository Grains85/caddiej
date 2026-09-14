"""What the domain needs from the outside world, stated in its own terms.

A port names a capability, not a technology. `FloorPort` is what "the floor" can
be asked to do; whether that reaches a projector through OBS, a browser in
kiosk mode, or nothing at all is not the domain's business. Nothing here mentions
markers, coordinates, colours or pixels: those are decisions about how to draw a
club, and they belong to whatever implements the port.

Ports are synchronous. Deciding what the floor should show happens in response to
a shot that has already been hit, and it must not be able to wait on a browser.
"""

from typing import Protocol

from caddiej.domain.observation import ObservedRoundShot
from caddiej.domain.shot import Club, Shot


class FloorPort(Protocol):
    """The projected floor, as the domain thinks of it."""

    def show_address(self, club: Club | None) -> None:
        """Show the player where to place the ball for the club in play.

        A club of None means the club is unknown, which is normal before the
        simulator has said anything. The floor still has to show somewhere to
        put the ball.
        """

    def hide_address(self) -> None:
        """Stop showing where to place the ball."""

    def show_play_status(self, status: str, club: Club | None) -> None:
        """Show the launch monitor's current state, such as READY and DRIVER.

        Empty `status` hides the caption. This is diagnostic text for the
        overlay, not a substitute for the address marker.
        """

    def show_shot(self, shot: Shot) -> None:
        """Show what just happened: the club's path and the launch numbers."""

    def show_round_result(self, shot: ObservedRoundShot) -> None:
        """Show a GSPro round-play result with raw values and ball flight.

        Distinct from `show_shot`: those numbers have verified units, these
        do not, and they must not be drawn with unit suffixes.
        """

    def clear_shot(self) -> None:
        """Take the last shot off the floor."""

    def show_advice(self, text: str, severity: float) -> None:
        """Put one short coaching line on the floor.

        Text and a number, and nothing else. The floor is being told what to
        draw, not what to do, which is what keeps a line authored by a language
        model from being able to reach anything that matters.
        """

    def clear_advice(self) -> None:
        """Take the coaching line off the floor."""
