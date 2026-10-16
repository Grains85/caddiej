"""What is pending for one HUD client, and who is allowed to write it.

The rule the module exists to enforce is that a socket has exactly one writer.
Starlette does not serialise concurrent calls to its `send` callable, so a
snapshot interleaved with a control reply is a corrupt frame — which presents
in the bay as a projector that reconnects for no visible reason. So everything
outbound queues here and leaves through one task, and these tests are about the
two queueing disciplines that meet in `next_outbound`.
"""

import asyncio
import json

from caddiej.adapters.hud import ConnectionRegistry, HudConnection
from caddiej.floor import FloorState


def status_of(frame: str | None) -> str:
    assert frame is not None
    return json.loads(frame)["state"]["status_text"]


async def next_frame(connection: HudConnection) -> str | None:
    return await asyncio.wait_for(connection.next_outbound(), 1)


async def test_connection_delivers_offered_state() -> None:
    connection = HudConnection()

    connection.offer(FloorState().evolve(status_text="ONLINE"))

    assert status_of(await next_frame(connection)) == "ONLINE"


async def test_superseded_state_is_dropped_not_queued() -> None:
    """Newest-wins, so a slow client falls behind in freshness and never in
    correctness, and cannot grow a backlog the process pays for."""

    connection = HudConnection()
    first = FloorState().evolve(status_text="FIRST")

    connection.offer(first)
    connection.offer(first.evolve(status_text="SECOND"))

    assert status_of(await next_frame(connection)) == "SECOND"
    assert connection.dropped == 1


async def test_next_outbound_waits_until_something_is_pending() -> None:
    connection = HudConnection()
    waiter = asyncio.create_task(connection.next_outbound())
    await asyncio.sleep(0)

    assert not waiter.done()

    connection.offer(FloorState().evolve(status_text="LATER"))

    assert status_of(await asyncio.wait_for(waiter, 1)) == "LATER"


async def test_closing_releases_a_waiting_reader() -> None:
    connection = HudConnection()
    waiter = asyncio.create_task(connection.next_outbound())
    await asyncio.sleep(0)

    connection.close()

    assert await asyncio.wait_for(waiter, 1) is None
    assert connection.closed


async def test_closed_connection_ignores_further_state() -> None:
    connection = HudConnection()
    connection.close()

    connection.offer(FloorState().evolve(status_text="IGNORED"))

    assert await next_frame(connection) is None


class TestControlRepliesAndSnapshots:
    """The two disciplines, and why they are not the same one.

    A snapshot is the whole truth about the floor, so an older one carries
    nothing the newer does not. A reply answers a specific message the client
    sent, so dropping the older of two would answer the wrong question.
    """

    async def test_a_reply_is_written_before_a_pending_snapshot(self) -> None:
        connection = HudConnection()
        connection.offer(FloorState().evolve(status_text="STATE"))
        connection.reply("pong")

        assert await next_frame(connection) == "pong"
        assert status_of(await next_frame(connection)) == "STATE"

    async def test_replies_are_queued_rather_than_superseded(self) -> None:
        connection = HudConnection()

        connection.reply("first")
        connection.reply("second")

        assert await next_frame(connection) == "first"
        assert await next_frame(connection) == "second"

    async def test_a_client_asking_in_a_loop_cannot_grow_the_queue(self) -> None:
        # The client decides how many replies to ask for, so the bound is not
        # optional: a page pinging in a loop must not become this process's
        # memory problem.
        connection = HudConnection()

        for index in range(100):
            connection.reply(f"reply-{index}")

        assert connection.replies_dropped == 100 - 16
        assert await next_frame(connection) == "reply-84"

    async def test_closing_discards_replies_too(self) -> None:
        connection = HudConnection()
        connection.reply("never sent")

        connection.close()

        assert await next_frame(connection) is None


class TestHowLongAClientHasBeenQuiet:
    """Measured on the server's clock, because the browser's cannot be trusted.

    The overlay used to time its own silence in JavaScript and reconnect when
    the timer fired. Under OBS's embedded Chromium that timer is throttled, so
    it fired on a healthy socket and the page tore itself down; one recorded
    session did it 8,149 times. The measurement stayed, on this side, and
    nothing acts on it.
    """

    def connection(self, clock: list[float]) -> HudConnection:
        return HudConnection(monotonic=lambda: clock[0])

    async def test_a_fresh_connection_is_not_already_silent(self) -> None:
        clock = [100.0]

        assert self.connection(clock).silent_seconds == 0.0

    async def test_silence_is_measured_from_the_last_message(self) -> None:
        clock = [100.0]
        connection = self.connection(clock)

        clock[0] = 130.0
        connection.note_message()
        clock[0] = 145.0

        assert connection.silent_seconds == 15.0
        assert connection.messages_received == 1

    async def test_a_registry_reports_its_quietest_client(self) -> None:
        registry = ConnectionRegistry()
        assert registry.silent_seconds is None

        registry.register()

        assert registry.silent_seconds is not None


class TestTheRegistry:
    async def test_it_broadcasts_to_every_connection(self) -> None:
        registry = ConnectionRegistry()
        first = registry.register()
        second = registry.register()

        registry.broadcast(FloorState().evolve(status_text="FANOUT"))

        assert registry.count == 2
        assert status_of(await next_frame(first)) == "FANOUT"
        assert status_of(await next_frame(second)) == "FANOUT"

    async def test_unregister_closes_and_forgets_the_connection(self) -> None:
        registry = ConnectionRegistry()
        connection = registry.register()

        registry.unregister(connection)

        assert registry.count == 0
        assert connection.closed
        # Broadcasting after a client has gone must not raise.
        registry.broadcast(FloorState().evolve(status_text="AFTER"))

    async def test_connects_counts_every_socket_the_session_has_seen(self) -> None:
        """A floor reconnecting in a loop is invisible in `count`, which is 1
        the whole time. It is obvious here, which is the point: the reconnect
        storm was found by counting log lines."""

        registry = ConnectionRegistry()

        for _ in range(5):
            registry.unregister(registry.register())

        assert registry.count == 0
        assert registry.connects == 5
