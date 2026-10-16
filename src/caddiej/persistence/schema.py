"""The database schema, and how it moves forward.

Migrations are an ordered tuple of statements and SQLite's own `user_version`
pragma. That is the whole strategy, and for this project it is the right one: it
adds no dependency, the version travels inside the file rather than beside it, it
is set inside the same transaction as the statements it describes, and anyone
holding an unfamiliar database can find out what it is with one line of `sqlite3`.

What it deliberately gives up is a record of when each migration ran and who ran
it. A migration framework would keep that; nobody debugging a single-user golf
simulator has ever needed it.

Rules for adding a migration:

* append, never edit. A released migration has already run on someone's data, so
  changing it means their database and a fresh one disagree while both claim the
  same version.
* one `Migration` per release that changes the schema, with the next version.
* SQLite cannot drop or retype a column before 3.35 and cannot rename one
  cheaply, so prefer adding a nullable column over reshaping a table.
"""

import logging
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class SchemaTooNewError(RuntimeError):
    """The file was written by a later CaddieJ than the one opening it.

    Downgrading is refused rather than attempted. A newer CaddieJ may have added
    a column this one does not write, and carrying on would quietly produce rows
    that the newer version considers corrupt.
    """


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    statements: tuple[str, ...]


_INITIAL = (
    """
    CREATE TABLE sessions (
        session_id      TEXT    PRIMARY KEY,
        started_at      TEXT    NOT NULL,
        ended_at        TEXT,
        label           TEXT,
        app_version     TEXT,
        shot_count      INTEGER NOT NULL DEFAULT 0
    )
    """,
    # One row per shot, flat and denormalized on purpose. A shot is written once
    # and read in aggregate; joins would buy nothing and cost every query.
    # Every measurement is nullable because every device measures a different
    # subset, and a zero standing in for "not measured" would corrupt an average.
    """
    CREATE TABLE shots (
        shot_id                     TEXT    PRIMARY KEY,
        session_id                  TEXT    NOT NULL
                                            REFERENCES sessions(session_id)
                                            ON DELETE CASCADE,
        ordinal                     INTEGER NOT NULL,
        observed_at                 TEXT    NOT NULL,
        recorded_at                 TEXT    NOT NULL,
        player_name                 TEXT,
        handedness                  TEXT    NOT NULL,
        club_kind                   TEXT,
        club_vendor_code            TEXT,

        ball_speed_mph              REAL,
        launch_angle_degrees        REAL,
        launch_direction_degrees    REAL,
        total_spin_rpm              REAL,
        spin_axis_degrees           REAL,
        back_spin_rpm               REAL,
        side_spin_rpm               REAL,
        carry_yards                 REAL,

        club_speed_mph              REAL,
        club_speed_at_impact_mph    REAL,
        angle_of_attack_degrees     REAL,
        club_path_degrees           REAL,
        face_to_target_degrees      REAL,
        loft_degrees                REAL,
        lie_degrees                 REAL,
        closure_rate_deg_per_sec    REAL,
        impact_horizontal_mm        REAL,
        impact_vertical_mm          REAL,

        source_kind                 TEXT    NOT NULL,
        source_device_id            TEXT,
        source_device_session_id    TEXT    NOT NULL,
        source_device_sequence      INTEGER,
        source_reported_units       TEXT,
        source_api_version          TEXT,

        UNIQUE (session_id, ordinal)
    )
    """,
    "CREATE INDEX shots_by_session ON shots (session_id, ordinal)",
    "CREATE INDEX shots_by_club ON shots (club_kind)",
    "CREATE INDEX shots_by_time ON shots (observed_at)",
    # Kept apart from `shots` so that the debugging artifact can be deleted
    # wholesale without touching the measurements, and so that reading a
    # session's shots never drags a payload off disk with them.
    """
    CREATE TABLE shot_payloads (
        shot_id         TEXT    PRIMARY KEY
                                REFERENCES shots(shot_id) ON DELETE CASCADE,
        captured_at     TEXT    NOT NULL,
        source_kind     TEXT    NOT NULL,
        content_type    TEXT    NOT NULL,
        payload         TEXT    NOT NULL
    )
    """,
    # A seam for later milestones. A swing video, an AI analysis, or anything
    # else produced about a shot is referenced from here by an opaque string, so
    # attaching a new kind of analysis needs no schema change and the shots
    # table never learns what a video is.
    """
    CREATE TABLE shot_analyses (
        analysis_id     TEXT    PRIMARY KEY,
        shot_id         TEXT    NOT NULL
                                REFERENCES shots(shot_id) ON DELETE CASCADE,
        kind            TEXT    NOT NULL,
        reference       TEXT    NOT NULL,
        created_at      TEXT    NOT NULL,
        UNIQUE (shot_id, kind, reference)
    )
    """,
    "CREATE INDEX analyses_by_shot ON shot_analyses (shot_id)",
)


# What CaddieJ concluded about a shot, and what concluding it cost.
#
# A table of its own rather than a row in `shot_analyses`, which references
# artefacts produced elsewhere by an opaque string. This is structured data
# CaddieJ generated itself and will want to query: which faults an evening was
# full of, how often the model was unavailable, how many calls a round costs.
# None of that is answerable through an opaque reference.
#
# One row per diagnosed shot, including the good ones and the failed ones.
# Recording the absence is the point: without it, "that shot was fine" and "the
# provider was rate-limited" are indistinguishable afterwards.
_ADVICE = (
    """
    CREATE TABLE shot_advice (
        advice_id       TEXT    PRIMARY KEY,
        shot_id         TEXT    NOT NULL
                                REFERENCES shots(shot_id) ON DELETE CASCADE,
        created_at      TEXT    NOT NULL,
        status          TEXT    NOT NULL,
        focus           TEXT    NOT NULL,
        severity        REAL    NOT NULL,
        severity_source TEXT    NOT NULL,
        source          TEXT    NOT NULL,
        baseline_shots  INTEGER NOT NULL DEFAULT 0,

        display         TEXT,
        speech          TEXT,

        -- Null whenever no model was asked, which is the common case.
        provider        TEXT,
        model           TEXT,
        prompt_version  INTEGER,
        latency_ms      INTEGER,
        input_images    INTEGER NOT NULL DEFAULT 0,
        input_tokens    INTEGER,
        output_tokens   INTEGER,
        cache_hit       INTEGER NOT NULL DEFAULT 0,
        fingerprint     TEXT,

        failure_reason  TEXT,
        detail          TEXT,
        -- The clip the frames came from, relative to the media root. Enough to
        -- find the swing a line was written about without video having to know
        -- that history exists.
        clip_path       TEXT
    )
    """,
    "CREATE INDEX advice_by_shot ON shot_advice (shot_id)",
    "CREATE INDEX advice_by_focus ON shot_advice (focus)",
)


_ADVICE_V3 = (
    "ALTER TABLE shot_advice ADD COLUMN cue_id TEXT",
    "ALTER TABLE shot_advice ADD COLUMN confidence REAL",
    "ALTER TABLE shot_advice ADD COLUMN confidence_source TEXT",
    "ALTER TABLE shot_advice ADD COLUMN evidence_grades TEXT",
    "ALTER TABLE shot_advice ADD COLUMN capture_id TEXT",
    "ALTER TABLE shot_advice ADD COLUMN panels_used TEXT",
    "ALTER TABLE shot_advice ADD COLUMN kind TEXT",
)


# ProTee-only coaching, keyed on the strike. No foreign key to `shots`: without
# GSPro there is no shot row, and a coaching outcome is still worth keeping.
# `metrics_json` holds every VX reading as it was read — value, unit and
# direction — nulls included, and `metrics_provenance` says they were read off
# ProTee's All Values panel by a model, not reported by a device API.
_COACH_RESULTS_V4 = (
    """
    CREATE TABLE coach_results (
        result_id           TEXT    PRIMARY KEY,
        session_id          TEXT    NOT NULL,
        strike_id           TEXT    NOT NULL,
        trigger_id          TEXT,
        capture_id          TEXT,
        created_at          TEXT    NOT NULL,
        status              TEXT    NOT NULL,
        kind                TEXT    NOT NULL,
        focus               TEXT    NOT NULL,
        cue_id              TEXT,
        display             TEXT,
        speech              TEXT,
        severity            REAL,
        confidence          REAL,
        confidence_source   TEXT,
        provider            TEXT,
        model               TEXT,
        prompt_version      TEXT,
        latency_ms          INTEGER,
        input_images        INTEGER NOT NULL DEFAULT 0,
        swing_images        INTEGER NOT NULL DEFAULT 0,
        input_tokens        INTEGER,
        output_tokens       INTEGER,
        metrics_source      TEXT,
        metrics_status      TEXT    NOT NULL,
        metrics_provenance  TEXT,
        metrics_read        INTEGER NOT NULL DEFAULT 0,
        metrics_json        TEXT,
        metrics_image       TEXT,
        metrics_captured_at TEXT,
        panels_used         TEXT,
        evidence_grades     TEXT,
        failure_reason      TEXT,
        detail              TEXT
    )
    """,
    "CREATE INDEX coach_results_by_session ON coach_results (session_id, created_at)",
    "CREATE INDEX coach_results_by_strike ON coach_results (strike_id)",
)


# The Swing Score, on the row of the strike it rates. `swing_score` is null both
# when nothing was read and when too little was; `swing_score_rubric` tells the
# two apart, because it is only written when a score was attempted. The
# components go whole into `swing_score_json`, so a stored score can be taken
# apart again. The club is a column of its own because a trend is drawn per
# club and the index has to be able to reach it.
_SWING_SCORE_V5 = (
    "ALTER TABLE coach_results ADD COLUMN swing_score INTEGER",
    "ALTER TABLE coach_results ADD COLUMN swing_score_rubric TEXT",
    "ALTER TABLE coach_results ADD COLUMN swing_score_club TEXT",
    "ALTER TABLE coach_results ADD COLUMN swing_score_json TEXT",
    "CREATE INDEX coach_results_by_score "
    "ON coach_results (session_id, swing_score_club, created_at)",
)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, statements=_INITIAL),
    Migration(version=2, statements=_ADVICE),
    Migration(version=3, statements=_ADVICE_V3),
    Migration(version=4, statements=_COACH_RESULTS_V4),
    Migration(version=5, statements=_SWING_SCORE_V5),
)

SCHEMA_VERSION = MIGRATIONS[-1].version


def schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(connection: sqlite3.Connection) -> int:
    """Bring a database up to the version this code writes, and say which.

    Each migration commits on its own. An interrupted upgrade therefore leaves
    the file at the last version that fully applied, which is a state this
    function can be run against again.
    """

    current = schema_version(connection)
    if current > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"The database is at schema version {current}, but this CaddieJ "
            f"only understands {SCHEMA_VERSION}"
        )

    for migration in MIGRATIONS:
        if migration.version <= current:
            continue
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in migration.statements:
                connection.execute(statement)
            # Not a parameter: SQLite does not bind pragma arguments. The value
            # comes from the tuple above and is typed as an int.
            connection.execute(f"PRAGMA user_version = {int(migration.version)}")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
        logger.info(
            "history_schema_migrated",
            extra={"history_schema_version": migration.version},
        )

    return SCHEMA_VERSION
