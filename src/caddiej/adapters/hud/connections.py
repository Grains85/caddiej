"""Per-client outbound slots and the connection registry.

One rule shapes this file: **a socket has exactly one writer.** Everything the
server sends to a client goes through the client's `HudConnection` and out of
the single send task that owns it. Starlette does not serialise concurrent
calls to its `send` callable, and a snapshot interleaved with a pong is a
corrupt frame that presents as a mysterious reconnect.
"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace

from caddiej.adapters.hud.protocol import snapshot_message
from caddiej.floor.state import FloorState

logger = logging.getLogger(__name__)

# Control replies are answers to something the client asked for, so they are
# queued rather than superseded. Bounded because the client decides how many
# to ask for: a page in a loop sending pings must not grow memory here.
_REPLY_LIMIT = 16


def without_coaching(state: FloorState) -> FloorState:
    """The same floor with the coached result taken out of it.

    What a client gets once it has reported loading a build CaddieJ is not
    serving and cannot be sent to the current one.

    The first hardware test had two overlays connected. One reconnected and
    came up to the served build; the other reported a different version,
    answered the reload request with `unknown message type: reload` -- it
    predates the handler -- and went on drawing. There is no message that
    fixes that, because the fix is in code the page does not have, and nothing
    outside the page was going to reload it either. So it stayed, indefinitely,
    rendering the coaching with a layout whose faults had already been found.

    A page that cannot be corrected can at least be told nothing. `snapshot`
    is the one message every build of the overlay has ever understood, so a
    snapshot with no advice in it clears the coaching on any of them, however
    old. The result is a stale overlay that shows the marker, the ball status
    and the shot -- geometry it renders correctly -- and no lesson at all,
    which is the honest outcome: a cut-off instruction is worse than silence,
    and the log says exactly which client was refused and why.
    """

    if state.advice is None and state.progress is None:
        return state
    return replace(state, advice=None, progress=None)


class HudConnection:
    """Holds everything pending delivery to one client.

    Two kinds of thing are pending, and they behave differently. State is
    newest-wins: a superseded snapshot is dropped rather than queued, so a slow
    client falls behind in freshness but never in correctness and cannot grow
    an unbounded backlog. Control replies are a bounded queue, because each one
    answers a specific message and dropping the older of two pongs would answer
    the wrong ping.
    """

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._pending: FloorState | None = None
        self._replies: deque[str] = deque()
        self._wakeup = asyncio.Event()
        self._closed = False
        self._monotonic = monotonic
        self.dropped = 0
        self.replies_dropped = 0
        self.messages_received = 0
        # Seeded at the handshake so an overlay that says nothing at all still
        # has an age rather than a None that has to be special-cased.
        self.last_message_monotonic: float = self._monotonic()
        # What this page said it loaded, and where it says it is. Both come
        # from the hello and are kept because the first hardware test could
        # not answer "which of these two clients is the projector?" -- every
        # overlay reports `surface: floor`, and with two of them connected and
        # one of them stale there was no way to know which one the screenshot
        # was of. A page id and its own URL make that a lookup.
        self.assets_version: str = ""
        self.page_id: str = ""
        self.href: str = ""
        # True once this client has reported loading a build that is not the
        # one being served. A stale page draws the floor with a renderer whose
        # bugs are already fixed, and the coaching is the part where that
        # matters: the layout is most of what changes between builds, so a
        # stale overlay shows a wrong *result*, not merely an old-looking one.
        self.stale = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def silent_seconds(self) -> float:
        """How long since this client last said anything.

        The measurement the overlay used to attempt in JavaScript and get
        wrong, made on the server's clock where no browser can throttle it.
        Reported, never acted on: a quiet page is usually a page with nothing
        to say.
        """

        return max(0.0, self._monotonic() - self.last_message_monotonic)

    def note_message(self) -> None:
        self.messages_received += 1
        self.last_message_monotonic = self._monotonic()

    def offer(self, state: FloorState) -> None:
        if self._closed:
            return
        if self.stale:
            state = without_coaching(state)
        if self._pending is not None:
            self.dropped += 1
        self._pending = state
        self._wakeup.set()

    def reply(self, text: str) -> None:
        """Queue a control reply for the send task to write."""

        if self._closed:
            return
        if len(self._replies) >= _REPLY_LIMIT:
            self._replies.popleft()
            self.replies_dropped += 1
        self._replies.append(text)
        self._wakeup.set()

    async def next_outbound(self) -> str | None:
        """Wait for the next frame to send, or None once the connection closes.

        Replies go first. They are answers to something the client asked a
        moment ago, and a snapshot is never late in a way anybody can see.
        """

        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            if self._closed:
                return None
            if self._replies:
                text = self._replies.popleft()
                if self._replies or self._pending is not None:
                    self._wakeup.set()
                return text
            state, self._pending = self._pending, None
            if state is not None:
                return snapshot_message(state).model_dump_json()

    def close(self) -> None:
        self._closed = True
        self._pending = None
        self._replies.clear()
        self._wakeup.set()


class ConnectionRegistry:
    """Tracks live connections and fans state out to them."""

    def __init__(self) -> None:
        self._connections: set[HudConnection] = set()
        # Every connection since the process started. A floor that reconnects
        # in a loop is invisible in `count`, which is 1 the whole time; it is
        # obvious here. One recorded session reached 8,141.
        self.connects = 0

    @property
    def count(self) -> int:
        return len(self._connections)

    @property
    def silent_seconds(self) -> float | None:
        """The quietest live client, or None when nothing is connected."""

        if not self._connections:
            return None
        return max(connection.silent_seconds for connection in self._connections)

    def register(self) -> HudConnection:
        connection = HudConnection()
        self._connections.add(connection)
        self.connects += 1
        return connection

    def unregister(self, connection: HudConnection) -> None:
        connection.close()
        self._connections.discard(connection)

    def broadcast(self, state: FloorState) -> None:
        """Hand the state to every connection without awaiting anything."""

        for connection in tuple(self._connections):
            connection.offer(state)
