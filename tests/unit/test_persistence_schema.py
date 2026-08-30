"""The schema, and the promise that a database can move between versions.

Every test gets its own file under `tmp_path`, so nothing here can see another
test's data or a developer's real round.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from caddiej.persistence.schema import (
    MIGRATIONS,
    SCHEMA_VERSION,
    SchemaTooNewError,
    migrate,
    schema_version,
)

EXPECTED_TABLES = {"sessions", "shots", "shot_payloads", "shot_analyses"}


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with sqlite3.connect(tmp_path / "history.sqlite3", isolation_level=None) as opened:
        opened.row_factory = sqlite3.Row
        opened.execute("PRAGMA foreign_keys = ON")
        yield opened


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row["name"] for row in rows}


def test_a_fresh_database_is_brought_to_the_current_version(
    connection: sqlite3.Connection,
) -> None:
    assert schema_version(connection) == 0

    assert migrate(connection) == SCHEMA_VERSION
    assert schema_version(connection) == SCHEMA_VERSION


def test_a_fresh_database_gets_every_table(connection: sqlite3.Connection) -> None:
    migrate(connection)

    assert EXPECTED_TABLES <= table_names(connection)


def test_migrating_an_up_to_date_database_does_nothing(
    connection: sqlite3.Connection,
) -> None:
    # Every start-up runs this, so it has to be safe to run when there is
    # nothing to do.
    migrate(connection)
    connection.execute(
        "INSERT INTO sessions (session_id, started_at) VALUES ('a', '2026-04-01')"
    )

    migrate(connection)

    assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_a_database_from_a_newer_caddiej_is_refused(
    connection: sqlite3.Connection,
) -> None:
    # Carrying on would write rows the newer version considers corrupt, and it
    # is the one that has to live with the file.
    migrate(connection)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(SchemaTooNewError):
        migrate(connection)


def test_a_failed_migration_leaves_the_version_alone(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Half a migration recorded as a whole one is the worst possible state: the
    # next run would skip the rest of it.
    from caddiej.persistence import schema

    broken = (
        schema.Migration(
            version=SCHEMA_VERSION + 1,
            statements=("CREATE TABLE fine (id TEXT)", "THIS IS NOT SQL"),
        ),
    )
    migrate(connection)
    monkeypatch.setattr(schema, "MIGRATIONS", (*MIGRATIONS, *broken))

    with pytest.raises(sqlite3.Error):
        schema.migrate(connection)

    assert schema_version(connection) == SCHEMA_VERSION
    assert "fine" not in table_names(connection)


def test_migrations_are_numbered_from_one_without_gaps() -> None:
    # A gap or a repeat would make "is this migration already applied?"
    # ambiguous, and the answer is only ever a comparison.
    assert [migration.version for migration in MIGRATIONS] == list(
        range(1, len(MIGRATIONS) + 1)
    )


def test_deleting_a_session_takes_its_shots_with_it(
    connection: sqlite3.Connection,
) -> None:
    # A shot belonging to no session is unreachable, so the file would only
    # grow. Cascades are declared, and this proves the pragma is on too.
    migrate(connection)
    connection.execute(
        "INSERT INTO sessions (session_id, started_at) VALUES ('s', '2026-04-01')"
    )
    connection.execute(
        """
        INSERT INTO shots
            (shot_id, session_id, ordinal, observed_at, recorded_at, handedness,
             source_kind, source_device_session_id)
        VALUES ('sh', 's', 1, '2026-04-01', '2026-04-01', 'right', 'synthetic', 'd')
        """
    )
    connection.execute(
        """
        INSERT INTO shot_payloads
            (shot_id, captured_at, source_kind, content_type, payload)
        VALUES ('sh', '2026-04-01', 'synthetic', 'text/plain', '{}')
        """
    )

    connection.execute("DELETE FROM sessions WHERE session_id = 's'")

    assert connection.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM shot_payloads").fetchone()[0] == 0


def test_a_session_cannot_hold_the_same_shot_position_twice(
    connection: sqlite3.Connection,
) -> None:
    migrate(connection)
    connection.execute(
        "INSERT INTO sessions (session_id, started_at) VALUES ('s', '2026-04-01')"
    )
    insert = """
        INSERT INTO shots
            (shot_id, session_id, ordinal, observed_at, recorded_at, handedness,
             source_kind, source_device_session_id)
        VALUES (?, 's', 1, '2026-04-01', '2026-04-01', 'right', 'synthetic', 'd')
    """
    connection.execute(insert, ("first",))

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(insert, ("second",))
