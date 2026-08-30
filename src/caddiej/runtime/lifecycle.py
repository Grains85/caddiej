"""Supervision of long-lived tasks and shutdown signalling."""

import asyncio
import logging
import os
import signal
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from typing import Any

logger = logging.getLogger(__name__)

_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class TaskSupervisor:
    """Owns long-lived tasks, reports their death, and stops them in a budget.

    A supervised task that raises is a failure of the whole process: the caller
    is not awaiting it, so without this the task would disappear and CaddieJ
    would keep running with a dead subsystem. `on_failure` is called so the
    application can begin shutting down, and the exception is kept in `failure`
    so it can be re-raised and reach the exit code.
    """

    def __init__(
        self,
        shutdown_timeout: float = 5.0,
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        self._shutdown_timeout = shutdown_timeout
        self._on_failure = on_failure
        self._tasks: set[asyncio.Task[Any]] = set()
        self.failure: BaseException | None = None

    @property
    def tasks(self) -> frozenset[asyncio.Task[Any]]:
        return frozenset(self._tasks)

    def start(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)
        return task

    def _task_finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return

        # Retrieving the exception here is also what keeps it from surfacing
        # later as an unretrieved-exception warning at garbage collection.
        error = task.exception()
        if error is None:
            return

        logger.error(
            "supervised_task_failed",
            extra={"task_name": task.get_name()},
            exc_info=error,
        )
        if self.failure is None:
            self.failure = error
        if self._on_failure is not None:
            self._on_failure()

    async def shutdown(self) -> None:
        """Cancel every supervised task, waiting no longer than the budget."""

        tasks = tuple(self._tasks)
        if not tasks:
            return

        for task in tasks:
            task.cancel()

        done, pending = await asyncio.wait(tasks, timeout=self._shutdown_timeout)
        for task in pending:
            logger.warning(
                "task_shutdown_timed_out", extra={"task_name": task.get_name()}
            )

        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                logger.error(
                    "task_failed_during_shutdown",
                    extra={"task_name": task.get_name()},
                    exc_info=error,
                )

        self._tasks.clear()


@asynccontextmanager
async def shutdown_signals(
    on_force: Callable[[], None] | None = None,
) -> AsyncIterator[asyncio.Event]:
    """Yield an event that is set when the process is asked to terminate.

    Handlers are installed through the running loop so shutdown proceeds on the
    normal code path rather than relying on cancellation semantics.

    Installing them has a cost worth being deliberate about: it replaces the
    interpreter's own SIGINT handler, so for as long as they are installed
    Ctrl+C no longer raises `KeyboardInterrupt`. A shutdown that then wedged —
    a sound card that will not release, a bridge that will not answer — could
    not be interrupted at all, and the only way out would be `kill -9` from
    another window. So the second signal is a force quit: the first asks
    politely, the second stops asking.
    """

    event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    previous: dict[signal.Signals, Any] = {}

    def requested() -> None:
        def apply() -> None:
            if not event.is_set():
                event.set()
                return
            logger.warning("shutdown_forced")
            if on_force is not None:
                on_force()
            else:
                _force_exit()

        # `signal.signal` handlers on Windows run outside the loop; without
        # this the Event is set and nothing waiting on it is woken.
        try:
            loop.call_soon_threadsafe(apply)
        except RuntimeError:
            apply()

    for sig in _SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, requested)
        except (NotImplementedError, RuntimeError):
            # Windows, or a loop that cannot own signals; fall back to the
            # interpreter-level handler. Restored in `finally` so a test or a
            # later run does not inherit this process's shutdown policy.
            with suppress(ValueError, OSError):
                previous[sig] = signal.getsignal(sig)
                signal.signal(sig, lambda *_: requested())
        else:
            installed.append(sig)

    try:
        yield event
    finally:
        for sig in installed:
            with suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
        for sig, handler in previous.items():
            with suppress(ValueError, OSError):
                signal.signal(sig, handler)


def _force_exit() -> None:  # pragma: no cover - replaced in tests
    """Leave immediately, without unwinding anything.

    Deliberately `os._exit`: the graceful path is what the operator has just
    told us twice is not working, and every alternative — cancelling the task
    running shutdown, raising into the loop — goes back through the same code
    that is stuck.
    """

    logging.shutdown()
    os._exit(130)
