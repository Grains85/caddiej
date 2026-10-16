"""Every statement CaddieJ runs against its history, and nothing else.

The repository is synchronous and takes a connection. It does no threading, no
queueing, no aggregation and no error handling: it is called on the database
thread by `HistoryService`, which owns all of those. Keeping it that way is what
makes it testable against a real temporary database with no event loop in sight,
and it is where to look when a query needs explaining.
"""

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from caddiej.domain.shot import ClubKind
from caddiej.domain.swing_score import SWING_SCORE_RUBRIC_VERSION
from caddiej.persistence.database import transaction
from caddiej.persistence.records import (
    AnalysisReference,
    RawPayload,
    SessionRecord,
    StoredAdvice,
    StoredShot,
    StoredSwingScore,
    advice_row,
    analysis_reference,
    analysis_row,
    payload_row,
    raw_payload,
    session_record,
    session_row,
    shot_row,
    stored_advice,
    stored_shot,
    stored_swing_score,
)

_SHOT_COLUMNS = (
    "shot_id",
    "session_id",
    "ordinal",
    "observed_at",
    "recorded_at",
    "player_name",
    "handedness",
    "club_kind",
    "club_vendor_code",
    "ball_speed_mph",
    "launch_angle_degrees",
    "launch_direction_degrees",
    "total_spin_rpm",
    "spin_axis_degrees",
    "back_spin_rpm",
    "side_spin_rpm",
    "carry_yards",
    "club_speed_mph",
    "club_speed_at_impact_mph",
    "angle_of_attack_degrees",
    "club_path_degrees",
    "face_to_target_degrees",
    "loft_degrees",
    "lie_degrees",
    "closure_rate_deg_per_sec",
    "impact_horizontal_mm",
    "impact_vertical_mm",
    "source_kind",
    "source_device_id",
    "source_device_session_id",
    "source_device_sequence",
    "source_reported_units",
    "source_api_version",
)

_INSERT_SHOT = (
    f"INSERT INTO shots ({', '.join(_SHOT_COLUMNS)}) "
    f"VALUES ({', '.join(':' + column for column in _SHOT_COLUMNS)})"
)

_INSERT_PAYLOAD = """
    INSERT INTO shot_payloads
        (shot_id, captured_at, source_kind, content_type, payload)
    VALUES
        (:shot_id, :captured_at, :source_kind, :content_type, :payload)
"""

# OR IGNORE: the first shot and the first ProTee-only coaching result can each
# be the write that brings the session row into existence.
_INSERT_SESSION = """
    INSERT OR IGNORE INTO sessions
        (session_id, started_at, ended_at, label, app_version, shot_count)
    VALUES
        (:session_id, :started_at, :ended_at, :label, :app_version, :shot_count)
"""

_INSERT_ANALYSIS = """
    INSERT INTO shot_analyses
        (analysis_id, shot_id, kind, reference, created_at)
    VALUES
        (:analysis_id, :shot_id, :kind, :reference, :created_at)
"""

_ADVICE_COLUMNS = (
    "advice_id",
    "shot_id",
    "created_at",
    "status",
    "focus",
    "severity",
    "severity_source",
    "source",
    "baseline_shots",
    "display",
    "speech",
    "provider",
    "model",
    "prompt_version",
    "latency_ms",
    "input_images",
    "input_tokens",
    "output_tokens",
    "cache_hit",
    "fingerprint",
    "failure_reason",
    "detail",
    "clip_path",
    "cue_id",
    "confidence",
    "confidence_source",
    "evidence_grades",
    "capture_id",
    "panels_used",
    "kind",
)

_INSERT_ADVICE = (
    f"INSERT INTO shot_advice ({', '.join(_ADVICE_COLUMNS)}) "
    f"VALUES ({', '.join(':' + column for column in _ADVICE_COLUMNS)})"
)


class HistoryRepository:
    """Reads and writes the history tables."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # --- sessions ---------------------------------------------------------

    def start_session(self, record: SessionRecord) -> None:
        self._connection.execute(_INSERT_SESSION, session_row(record))

    def close_session(
        self, session_id: UUID, ended_at: datetime, shot_count: int
    ) -> None:
        """Stamp a session as finished, with the number of shots it holds.

        The count is stored rather than derived so that listing sessions costs
        one query. It is written from the recorded total, not from the shots
        offered, so a session that dropped writes does not claim them.
        """

        self._connection.execute(
            "UPDATE sessions SET ended_at = ?, shot_count = ? WHERE session_id = ?",
            (ended_at.isoformat(), shot_count, str(session_id)),
        )

    def session(self, session_id: UUID) -> SessionRecord | None:
        row = self._connection.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)
        ).fetchone()
        return None if row is None else session_record(row)

    def sessions(self, limit: int = 50) -> list[SessionRecord]:
        """Most recent first, which is the order anyone reviewing wants."""

        rows = self._connection.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [session_record(row) for row in rows]

    # --- shots ------------------------------------------------------------

    def insert_shot(self, stored: StoredShot, payload: RawPayload | None) -> None:
        """Write a shot and its payload together, or neither of them.

        One transaction because a payload with no shot is unreadable and a shot
        whose payload half-arrived is worse than one with none.
        """

        with transaction(self._connection):
            self._connection.execute(_INSERT_SHOT, shot_row(stored))
            if payload is not None:
                self._connection.execute(_INSERT_PAYLOAD, payload_row(payload))

    def shots(self, session_id: UUID) -> list[StoredShot]:
        rows = self._connection.execute(
            "SELECT * FROM shots WHERE session_id = ? ORDER BY ordinal",
            (str(session_id),),
        ).fetchall()
        return [stored_shot(row) for row in rows]

    def shot(self, shot_id: UUID) -> StoredShot | None:
        row = self._connection.execute(
            "SELECT * FROM shots WHERE shot_id = ?", (str(shot_id),)
        ).fetchone()
        return None if row is None else stored_shot(row)

    def last_shot(self, session_id: UUID) -> StoredShot | None:
        """The most recent shot of a session, as one row rather than all of them.

        Its own query rather than the tail of `shots`, because "what did I just
        hit" is asked far more often than "show me the session" and loading an
        evening to answer it would get slower every ball. Ordered by ordinal
        rather than by a timestamp: ordinals are assigned in arrival order and
        are unique, so two shots recorded in the same millisecond still have an
        unambiguous last.
        """

        row = self._connection.execute(
            "SELECT * FROM shots WHERE session_id = ? ORDER BY ordinal DESC LIMIT 1",
            (str(session_id),),
        ).fetchone()
        return None if row is None else stored_shot(row)

    def shot_count(self, session_id: UUID) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM shots WHERE session_id = ?", (str(session_id),)
        ).fetchone()
        return int(row[0])

    # --- payloads and analyses --------------------------------------------

    def payload(self, shot_id: UUID) -> RawPayload | None:
        row = self._connection.execute(
            "SELECT * FROM shot_payloads WHERE shot_id = ?", (str(shot_id),)
        ).fetchone()
        return None if row is None else raw_payload(row)

    def forget_payloads(self, session_id: UUID) -> int:
        """Drop the debugging payloads for a session, keeping its shots.

        Payloads are the only part of the file that grows without bound, and
        they are the only part nothing depends on.
        """

        cursor = self._connection.execute(
            "DELETE FROM shot_payloads WHERE shot_id IN "
            "(SELECT shot_id FROM shots WHERE session_id = ?)",
            (str(session_id),),
        )
        return cursor.rowcount

    def attach_analysis(self, reference: AnalysisReference) -> None:
        self._connection.execute(_INSERT_ANALYSIS, analysis_row(reference))

    def analyses(self, shot_id: UUID) -> list[AnalysisReference]:
        rows = self._connection.execute(
            "SELECT * FROM shot_analyses WHERE shot_id = ? ORDER BY created_at",
            (str(shot_id),),
        ).fetchall()
        return [analysis_reference(row) for row in rows]

    # --- coaching ---------------------------------------------------------

    def insert_advice(self, stored: StoredAdvice) -> None:
        self._connection.execute(_INSERT_ADVICE, advice_row(stored))

    def advice(self, shot_id: UUID) -> list[StoredAdvice]:
        rows = self._connection.execute(
            "SELECT * FROM shot_advice WHERE shot_id = ? ORDER BY created_at",
            (str(shot_id),),
        ).fetchall()
        return [stored_advice(row) for row in rows]

    def session_advice(self, session_id: UUID) -> list[StoredAdvice]:
        """Every line of a session, in the order the shots were hit.

        Ordered by the shot rather than by when the advice was written, because
        a model answering slowly for one shot must not reorder the round.
        """

        rows = self._connection.execute(
            "SELECT shot_advice.* FROM shot_advice "
            "JOIN shots ON shots.shot_id = shot_advice.shot_id "
            "WHERE shots.session_id = ? "
            "ORDER BY shots.ordinal, shot_advice.created_at",
            (str(session_id),),
        ).fetchall()
        return [stored_advice(row) for row in rows]

    def recent_advice(self, session_id: UUID, limit: int) -> list[StoredAdvice]:
        """The last few lines of a session, oldest of those few first.

        Selected newest-first so the database does the limiting, then reversed
        so the caller reads them in the order they were said. Returning them
        newest-first would be the wrong shape for anything that wants to
        summarise a stretch of practice, and reversing a handful in Python is
        cheaper than teaching every caller to do it.
        """

        rows = self._connection.execute(
            "SELECT shot_advice.* FROM shot_advice "
            "JOIN shots ON shots.shot_id = shot_advice.shot_id "
            "WHERE shots.session_id = ? "
            "ORDER BY shots.ordinal DESC, shot_advice.created_at DESC "
            "LIMIT ?",
            (str(session_id), limit),
        ).fetchall()
        return [stored_advice(row) for row in reversed(rows)]

    def focus_counts(self, session_id: UUID) -> dict[str, int]:
        """How many shots each fault accounted for, for a session review.

        The one aggregate worth a query rather than a Python loop: it answers
        "what was actually wrong tonight" over a whole evening without loading
        it, which is what makes the structured table worth having.

        Shots with nothing wrong are left out. They are the bulk of a good
        evening, and a tally led by "none: 40" answers a question nobody asked
        while burying the one they did. The count of shots played is a different
        query, which already exists.

        Failed analyses are counted, because a fault was still measured and its
        templated line was still on the floor. What is being counted is what the
        player was told, not what a model managed to say.
        """

        rows = self._connection.execute(
            "SELECT shot_advice.focus, COUNT(*) FROM shot_advice "
            "JOIN shots ON shots.shot_id = shot_advice.shot_id "
            "WHERE shots.session_id = ? AND shot_advice.focus <> 'none' "
            "GROUP BY shot_advice.focus ORDER BY COUNT(*) DESC, shot_advice.focus",
            (str(session_id),),
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    # --- ProTee-only coaching ---------------------------------------------

    def coach_results(self, session_id: UUID) -> list[dict[str, object]]:
        """Every ProTee-only coaching outcome of a session, oldest first."""

        rows = self._connection.execute(
            "SELECT * FROM coach_results WHERE session_id = ? ORDER BY created_at",
            (str(session_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def swing_scores(
        self,
        session_id: UUID,
        club: ClubKind | None = None,
        limit: int = 50,
        rubric_version: str = SWING_SCORE_RUBRIC_VERSION,
    ) -> list[StoredSwingScore]:
        """The last `limit` scored strikes of a session, oldest of them first.

        Only strikes that earned a number: a trend drawn through "too little
        evidence" would draw it as a value. Only one rubric, the current one by
        default, because a line between two yardsticks is a line about the
        yardsticks. `club` narrows it to one club, which is the trend worth
        drawing; None means every club, unknown included.
        """

        clauses = [
            "session_id = ?",
            "swing_score IS NOT NULL",
            "swing_score_rubric = ?",
        ]
        parameters: list[object] = [str(session_id), rubric_version]
        if club is not None:
            clauses.append("swing_score_club = ?")
            parameters.append(club.value)
        parameters.append(limit)
        rows = self._connection.execute(
            "SELECT strike_id, created_at, swing_score_json FROM coach_results "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            parameters,
        ).fetchall()
        return [stored_swing_score(row) for row in reversed(rows)]


def insert_coach_result(
    connection: sqlite3.Connection,
    row: dict[str, object],
    session: SessionRecord | None = None,
) -> None:
    """One coaching outcome, and the session row with it if none exists yet."""

    columns = tuple(row)
    statement = (
        f"INSERT INTO coach_results ({', '.join(columns)}) "
        f"VALUES ({', '.join(':' + column for column in columns)})"
    )
    with transaction(connection):
        if session is not None:
            connection.execute(_INSERT_SESSION, session_row(session))
        connection.execute(statement, row)


def insert_shots(
    connection: sqlite3.Connection,
    batch: Sequence[tuple[StoredShot, RawPayload | None]],
    session: SessionRecord | None = None,
) -> None:
    """Write several shots in one transaction, opening a session if needed.

    A batch because the writer may have fallen behind: ten shots then cost one
    commit instead of ten, and catching up is exactly when that matters.

    `session` is given for the first batch of a run and creates the session row.
    Doing it here rather than at start-up means a run that recorded nothing
    leaves nothing behind, and reading the database never writes to it.
    """

    with transaction(connection):
        if session is not None:
            connection.execute(_INSERT_SESSION, session_row(session))
        for stored, payload in batch:
            connection.execute(_INSERT_SHOT, shot_row(stored))
            if payload is not None:
                connection.execute(_INSERT_PAYLOAD, payload_row(payload))
