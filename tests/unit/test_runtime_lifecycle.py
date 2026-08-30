import asyncio
import os
import signal
import sys
import time

import pytest

from caddiej.runtime import TaskSupervisor, shutdown_signals


def _deliver(sig: signal.Signals) -> None:
    """Ask this process to shut down, without `TerminateProcess` on Windows.

    `os.kill(pid, SIGTERM)` on Windows ends the interpreter. The production
    path on that platform is `signal.signal`, so invoking the registered
    handler is what the operator's Ctrl+C actually does.
    """

    if sys.platform == "win32":
        handler = signal.getsignal(sig)
        if callable(handler):
            handler(sig, None)
        return
    os.kill(os.getpid(), sig)


async def test_supervisor_tracks_and_cancels_started_tasks() -> None:
    supervisor = TaskSupervisor(shutdown_timeout=1)
    started = asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.Event().wait()

    task = supervisor.start(forever(), name="forever")
    await started.wait()

    assert task in supervisor.tasks

    await supervisor.shutdown()

    assert task.cancelled()
    assert supervisor.tasks == frozenset()


async def test_supervisor_forgets_tasks_that_finish_on_their_own() -> None:
    supervisor = TaskSupervisor(shutdown_timeout=1)

    task = supervisor.start(asyncio.sleep(0), name="brief")
    await task
    await asyncio.sleep(0)

    assert supervisor.tasks == frozenset()


async def test_shutdown_is_bounded_when_a_task_ignores_cancellation() -> None:
    supervisor = TaskSupervisor(shutdown_timeout=0.05)
    started = asyncio.Event()

    async def stubborn() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.4)

    task = supervisor.start(stubborn(), name="stubborn")
    await started.wait()

    began = time.monotonic()
    await supervisor.shutdown()
    elapsed = time.monotonic() - began

    assert elapsed < 0.3
    assert not task.done()

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_shutdown_without_tasks_is_a_no_op() -> None:
    await TaskSupervisor(shutdown_timeout=1).shutdown()


async def test_a_dying_task_is_reported_and_recorded() -> None:
    # Without this, a dead HUD server would leave CaddieJ running as a zombie:
    # nobody awaits the task, so its exception would vanish.
    stop = asyncio.Event()
    supervisor = TaskSupervisor(shutdown_timeout=1, on_failure=stop.set)
    failure = RuntimeError("hud server died")

    async def crashes() -> None:
        raise failure

    supervisor.start(crashes(), name="hud-server")
    await asyncio.wait_for(stop.wait(), timeout=1)

    assert supervisor.failure is failure
    assert supervisor.tasks == frozenset()


async def test_only_the_first_failure_is_kept() -> None:
    supervisor = TaskSupervisor(shutdown_timeout=1)
    first = RuntimeError("first")

    async def crashes(error: Exception) -> None:
        raise error

    supervisor.start(crashes(first), name="one")
    await asyncio.sleep(0)
    supervisor.start(crashes(RuntimeError("second")), name="two")
    await asyncio.sleep(0)

    assert supervisor.failure is first


async def test_a_cancelled_task_is_not_a_failure() -> None:
    stop = asyncio.Event()
    supervisor = TaskSupervisor(shutdown_timeout=1, on_failure=stop.set)

    task = supervisor.start(asyncio.Event().wait(), name="forever")
    await asyncio.sleep(0)
    await supervisor.shutdown()

    assert task.cancelled()
    assert supervisor.failure is None
    assert not stop.is_set()


async def test_a_task_that_finishes_normally_is_not_a_failure() -> None:
    stop = asyncio.Event()
    supervisor = TaskSupervisor(shutdown_timeout=1, on_failure=stop.set)

    await supervisor.start(asyncio.sleep(0), name="brief")
    await asyncio.sleep(0)

    assert supervisor.failure is None
    assert not stop.is_set()


async def test_sigterm_sets_the_shutdown_event() -> None:
    async with shutdown_signals() as shutdown:
        assert not shutdown.is_set()
        _deliver(signal.SIGTERM)
        await asyncio.wait_for(shutdown.wait(), timeout=1)


async def test_a_loop_that_cannot_own_signals_still_shuts_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows: `add_signal_handler` is not implemented. The fallback is
    `signal.signal`, and that is what Ctrl+C actually hits."""

    loop = asyncio.get_running_loop()

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", refuse)

    async with shutdown_signals() as shutdown:
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
        await asyncio.wait_for(shutdown.wait(), timeout=1)


async def test_handlers_are_removed_on_exit() -> None:
    loop = asyncio.get_running_loop()

    async with shutdown_signals():
        pass

    # Reinstalling must succeed, proving the previous handler was released.
    async with shutdown_signals() as shutdown:
        loop.call_soon(shutdown.set)
        await asyncio.wait_for(shutdown.wait(), timeout=1)


async def test_a_second_signal_forces_the_process_out() -> None:
    """Installing a handler takes `KeyboardInterrupt` away from Ctrl-C, so for
    as long as shutdown runs there is no other way out of a wedged one — and the
    person pressing it has just said twice that it is wedged."""

    forced = 0

    def force() -> None:
        nonlocal forced
        forced += 1

    async with shutdown_signals(on_force=force) as shutdown:
        _deliver(signal.SIGINT)
        await asyncio.wait_for(shutdown.wait(), timeout=1)
        assert forced == 0, "the first signal asks politely"

        _deliver(signal.SIGINT)
        await asyncio.sleep(0.05)

    assert forced == 1


async def test_asking_twice_over_is_still_only_a_force() -> None:
    """Somebody holding Ctrl-C down must not queue up a dozen exits."""

    forces = 0

    def force() -> None:
        nonlocal forces
        forces += 1

    async with shutdown_signals(on_force=force) as shutdown:
        _deliver(signal.SIGTERM)
        await asyncio.wait_for(shutdown.wait(), timeout=1)
        for _ in range(3):
            _deliver(signal.SIGTERM)
        await asyncio.sleep(0.05)

    assert forces == 3, "every signal after the first is a force"
