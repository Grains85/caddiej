import asyncio

from caddiej.adapters.hud import ConnectionRegistry, HudConnection
from caddiej.floor import FloorState


async def test_connection_delivers_offered_state() -> None:
    connection = HudConnection()
    state = FloorState().evolve(status_text="ONLINE")

    connection.offer(state)

    assert await asyncio.wait_for(connection.next_state(), 1) is state


async def test_superseded_state_is_dropped_not_queued() -> None:
    connection = HudConnection()
    first = FloorState().evolve(status_text="FIRST")
    second = first.evolve(status_text="SECOND")

    connection.offer(first)
    connection.offer(second)

    assert await asyncio.wait_for(connection.next_state(), 1) is second
    assert connection.dropped == 1


async def test_next_state_waits_until_a_state_arrives() -> None:
    connection = HudConnection()
    waiter = asyncio.create_task(connection.next_state())
    await asyncio.sleep(0)

    assert not waiter.done()

    state = FloorState().evolve(status_text="LATER")
    connection.offer(state)

    assert await asyncio.wait_for(waiter, 1) is state


async def test_closing_releases_a_waiting_reader() -> None:
    connection = HudConnection()
    waiter = asyncio.create_task(connection.next_state())
    await asyncio.sleep(0)

    connection.close()

    assert await asyncio.wait_for(waiter, 1) is None
    assert connection.closed


async def test_closed_connection_ignores_further_state() -> None:
    connection = HudConnection()
    connection.close()

    connection.offer(FloorState().evolve(status_text="IGNORED"))

    assert await asyncio.wait_for(connection.next_state(), 1) is None


async def test_registry_broadcasts_to_every_connection() -> None:
    registry = ConnectionRegistry()
    first = registry.register()
    second = registry.register()
    state = FloorState().evolve(status_text="FANOUT")

    registry.broadcast(state)

    assert registry.count == 2
    assert await asyncio.wait_for(first.next_state(), 1) is state
    assert await asyncio.wait_for(second.next_state(), 1) is state


async def test_unregister_closes_and_forgets_the_connection() -> None:
    registry = ConnectionRegistry()
    connection = registry.register()

    registry.unregister(connection)

    assert registry.count == 0
    assert connection.closed
    # Broadcasting after a client has gone must not raise.
    registry.broadcast(FloorState().evolve(status_text="AFTER"))
