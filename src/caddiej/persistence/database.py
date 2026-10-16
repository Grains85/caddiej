"""One SQLite file, reached only from one thread.

`sqlite3` is blocking, and the event loop it would block is the one holding a
player's floor at 60 frames a second. So every statement runs on a private
single-threaded executor and callers await the result.

One thread rather than a pool, for two reasons. A `sqlite3.Connection` belongs to
the thread that created it, so a pool would need either a connection per thread
or `check_same_thread=False` and a lock — and the lock would serialise the work
anyway. And writes to one SQLite file *are* serial: two threads would take turns
through the file lock instead of through the queue, which is the same throughput
with more ways to fail.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from caddiej.persistence.schema import migrate

logger = logging.getLogger(__name__)


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run statements as one unit, or none of them.

    Explicit because the connection is in autocommit mode. `IMMEDIATE` takes the
    write lock up front rather than on the first write, so a busy database fails
    at the start of a transaction instead of halfway through one.
    """

    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except Exception:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


class Database:
    """An open SQLite file plus the thread that is allowed to touch it."""

    def __init__(
        self,
        path: Path | str,
        busy_timeout_seconds: float = 5.0,
        read_only: bool = False,
    ) -> None:
        self._path = Path(path)
        self._busy_timeout_seconds = busy_timeout_seconds
        self._read_only = read_only
        # How long shutdown waits for the database thread to exit. Derived
        # from the busy timeout because that is the longest a statement is
        # allowed to sit waiting for the file, and so the longest an honest
        # wait can be; anything past it is a thread that is not coming back.
        self._close_timeout = busy_timeout_seconds + 1.0
        self._executor: ThreadPoolExecutor | None = None
        self._connection: sqlite3.Connection | None = None
        self.schema_version = 0

    def reader(self) -> "Database":
        """A second way into the same file, for queries only.

        One thread is right for writing — see the note at the top of this module
        — and wrong for being the only way in. Everything that reads history goes
        through the same executor as the writer, so a client asking questions in
        a loop puts its queries in front of shots waiting to be recorded. That is
        the enrichment tier delaying the durable one, which is precisely the
        thing the architecture says cannot happen.

        A second connection on its own thread costs a file handle and fixes it,
        because WAL was chosen at the top of this module for exactly this: one
        writer and as many readers as like, none of them blocking each other.
        `query_only` is what keeps the promise in the name — the connection
        refuses to write even if something asks it to.
        """

        return Database(
            self._path,
            busy_timeout_seconds=self._busy_timeout_seconds,
            read_only=True,
        )

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    async def open(self) -> None:
        """Connect and migrate. Raises if either fails; the caller decides."""

        if self._connection is not None:
            return

        # Started before connecting so that the connection is created on the one
        # thread that will ever use it.
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=(
                "caddiej-sqlite-read" if self._read_only else "caddiej-sqlite"
            ),
        )
        loop = asyncio.get_running_loop()
        try:
            self._connection = await loop.run_in_executor(self._executor, self._connect)
            if self._read_only:
                # Migrating is the writer's job, and this connection could not do
                # it anyway. The version is read so health reports one number
                # whichever connection is asked.
                self.schema_version = await loop.run_in_executor(
                    self._executor, _schema_version, self._connection
                )
            else:
                self.schema_version = await loop.run_in_executor(
                    self._executor, migrate, self._connection
                )
        except Exception:
            await self.close()
            raise

        logger.info(
            "history_database_opened",
            extra={
                "history_path": str(self._path),
                "history_schema_version": self.schema_version,
            },
        )

    def _connect(self) -> sqlite3.Connection:
        if not self._read_only and self._path.parent != Path():
            self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._path,
            # Autocommit, so that transactions are the ones written in the code
            # rather than ones the driver inferred from the verb of a statement.
            isolation_level=None,
            timeout=self._busy_timeout_seconds,
        )
        connection.row_factory = sqlite3.Row
        if self._read_only:
            # `query_only` rather than opening the file with `mode=ro`: read-only
            # access to a WAL database needs to write the shared-memory index, so
            # the URI form would refuse the very journal mode chosen below.
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        # Write-ahead logging lets a reader — a future statistics view, or
        # somebody with the sqlite3 shell — read the file while a round is being
        # recorded, instead of being locked out by every insert.
        connection.execute("PRAGMA journal_mode = WAL")
        # NORMAL rather than FULL: losing the last few shots to a power cut is a
        # worse trade than an fsync per shot on a machine driving a projector.
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    async def close(self) -> None:
        connection, executor = self._connection, self._executor
        self._connection = None
        self._executor = None
        if executor is None:
            return

        loop = asyncio.get_running_loop()
        if connection is not None:
            try:
                # Bounded for the same reason as the join. The close is queued
                # behind whatever the thread is already running, so a
                # statement that will not finish would otherwise hold
                # shutdown here indefinitely.
                async with asyncio.timeout(self._close_timeout):
                    await loop.run_in_executor(executor, connection.close)
            except TimeoutError:
                logger.warning("history_database_close_timed_out")
            except Exception:
                logger.exception("history_database_close_failed")
        # Wait for the thread: on Windows the file stays locked until the
        # connection's thread has actually exited, and pytest cannot clean
        # tmp_path while that lock is held.
        #
        # Waited on another thread, and bounded, because the join itself is
        # blocking. Called directly it stops the event loop until the database
        # thread exits, which is fine when the thread is idle and is not when
        # a statement submitted before shutdown is still running: the loop
        # freezes, and the shutdown timeouts meant to bound this are all
        # timers on that loop, so none of them can fire. Better to give up the
        # lock on the file than to hang the process holding it.
        try:
            async with asyncio.timeout(self._close_timeout):
                await asyncio.to_thread(executor.shutdown, True)
        except TimeoutError:
            logger.warning("history_database_thread_lingering")
        logger.info("history_database_closed")

    async def run[ResultType](
        self, work: Callable[[sqlite3.Connection], ResultType]
    ) -> ResultType:
        """Run `work` against the connection, on the database thread."""

        connection, executor = self._connection, self._executor
        if connection is None or executor is None:
            raise RuntimeError("The history database is not open")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, work, connection)


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return 0 if row is None else int(row[0])
