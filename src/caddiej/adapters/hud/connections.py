"""Per-client outbound slots and the connection registry."""

import asyncio
import logging

from caddiej.floor.state import FloorState

logger = logging.getLogger(__name__)


class HudConnection:
    """Holds the newest state pending delivery to one client.

    Only the latest state is kept. A superseded snapshot is dropped rather than
    queued, so a slow client falls behind in freshness but never in correctness,
    and cannot grow an unbounded backlog.
    """

    def __init__(self) -> None:
        self._pending: FloorState | None = None
        self._wakeup = asyncio.Event()
        self._closed = False
        self.dropped = 0

    @property
    def closed(self) -> bool:
        return self._closed

    def offer(self, state: FloorState) -> None:
        if self._closed:
            return
        if self._pending is not None:
            self.dropped += 1
        self._pending = state
        self._wakeup.set()

    async def next_state(self) -> FloorState | None:
        """Wait for the next state, or None once the connection is closed."""

        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            if self._closed:
                return None
            state, self._pending = self._pending, None
            if state is not None:
                return state

    def close(self) -> None:
        self._closed = True
        self._pending = None
        self._wakeup.set()


class ConnectionRegistry:
    """Tracks live connections and fans state out to them."""

    def __init__(self) -> None:
        self._connections: set[HudConnection] = set()

    @property
    def count(self) -> int:
        return len(self._connections)

    def register(self) -> HudConnection:
        connection = HudConnection()
        self._connections.add(connection)
        return connection

    def unregister(self, connection: HudConnection) -> None:
        connection.close()
        self._connections.discard(connection)

    def broadcast(self, state: FloorState) -> None:
        """Hand the state to every connection without awaiting anything."""

        for connection in tuple(self._connections):
            connection.offer(state)
