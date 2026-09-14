"""The single writer of floor state."""

import logging
from collections.abc import Callable
from dataclasses import replace

from caddiej.floor.state import (
    BallMarker,
    ClubPath,
    FlightDrawPoint,
    FloorAdvice,
    FloorFlourish,
    FloorMode,
    FloorPoint,
    FloorState,
    MarkerVariant,
    RoundResultView,
    ShotMetrics,
)

logger = logging.getLogger(__name__)

StateListener = Callable[[FloorState], None]


class FloorService:
    """Owns floor state and notifies listeners when it changes.

    Listeners are synchronous on purpose: notification must not await anything,
    so a slow or dead client cannot delay a state change. Transports are
    expected to hand the state to a per-client outbound slot and return.

    **Must be called from the event loop thread.** Listeners reach asyncio
    primitives that are not thread-safe, so anything running in a thread or
    executor — video capture, for instance — has to hand the call back to the
    loop with `loop.call_soon_threadsafe` rather than calling these methods
    directly.
    """

    def __init__(self, initial: FloorState | None = None) -> None:
        self._state = initial if initial is not None else FloorState()
        self._listeners: list[StateListener] = []

    @property
    def state(self) -> FloorState:
        return self._state

    def add_listener(self, listener: StateListener) -> Callable[[], None]:
        """Register a listener and return a callable that removes it."""

        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def set_online(self, status_text: str) -> FloorState:
        return self._apply(mode=FloorMode.ONLINE, status_text=status_text)

    def set_shutting_down(self, status_text: str = "") -> FloorState:
        return self._apply(mode=FloorMode.SHUTTING_DOWN, status_text=status_text)

    def set_status_text(self, status_text: str) -> FloorState:
        return self._apply(status_text=status_text)

    def show_marker(
        self,
        position: FloorPoint,
        variant: MarkerVariant = MarkerVariant.NORMAL,
    ) -> FloorState:
        """Place the ball marker on the surface and show it."""

        return self._apply(
            marker=BallMarker(position=position, variant=variant, visible=True)
        )

    def hide_marker(self) -> FloorState:
        """Hide the marker, keeping where it was and which club it was for."""

        return self._apply(marker=replace(self._state.marker, visible=False))

    def set_metrics(self, metrics: ShotMetrics) -> FloorState:
        return self._apply(metrics=metrics)

    def clear_metrics(self) -> FloorState:
        return self._apply(metrics=None)

    def set_club_path(self, club_path: ClubPath) -> FloorState:
        return self._apply(club_path=club_path)

    def clear_club_path(self) -> FloorState:
        return self._apply(club_path=None)

    def set_advice(self, advice: FloorAdvice) -> FloorState:
        return self._apply(advice=advice)

    def clear_advice(self) -> FloorState:
        return self._apply(advice=None)

    def show_flourish(self, flourish: FloorFlourish) -> FloorState:
        """Draw a celebration over whatever the round has put on the floor."""

        return self._apply(flourish=flourish)

    def clear_flourish(self) -> FloorState:
        """Take the celebration away, leaving the round's own state untouched."""

        return self._apply(flourish=None)

    def set_play_status(self, status: str, club_label: str = "") -> FloorState:
        return self._apply(play_status=status, club_label=club_label)

    def set_round_result(
        self,
        result: RoundResultView,
        ball_flight: tuple[FlightDrawPoint, ...] | None,
    ) -> FloorState:
        return self._apply(round_result=result, ball_flight=ball_flight)

    def clear_round_result(self) -> FloorState:
        return self._apply(round_result=None, ball_flight=None)

    def _apply(self, **changes: object) -> FloorState:
        candidate = self._state.evolve(**changes)
        # A change that changes nothing is not a new version. Without this, a
        # repeated value would bump the version and re-broadcast to every
        # client, which matters once shot events drive this state.
        if candidate == replace(self._state, version=candidate.version):
            return self._state

        self._state = candidate
        logger.info(
            "floor_state_changed",
            extra={
                "floor_state_version": self._state.version,
                "floor_mode": self._state.mode.value,
            },
        )
        for listener in tuple(self._listeners):
            try:
                listener(self._state)
            except Exception:
                logger.exception("floor_state_listener_failed")
        return self._state
