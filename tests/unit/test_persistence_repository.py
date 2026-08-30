"""Every statement, against a real database in a temporary directory.

These use SQLite rather than a fake, because a fake repository would agree with
whatever the code believes about its own SQL. Each test gets its own file, so
they are isolated from each other and from any real history.

The round-trip tests are the important ones: a shot goes in, comes back, and must
be the same shot. That is what catches a column written into the wrong place,
which is a mistake nothing else here would notice.
"""

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from caddiej.domain.shot import BallLaunch, ClubDelivery, ClubKind, Handedness, Shot
from caddiej.persistence.records import (
    AnalysisReference,
    RawPayload,
    SessionRecord,
    StoredShot,
)
from caddiej.persistence.repository import HistoryRepository
from caddiej.persistence.schema import migrate
from conftest import EPOCH, ShotBuilder, delivery, launch

SESSION = uuid4()


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[HistoryRepository]:
    with sqlite3.connect(tmp_path / "history.sqlite3", isolation_level=None) as opened:
        opened.row_factory = sqlite3.Row
        opened.execute("PRAGMA foreign_keys = ON")
        migrate(opened)
        subject = HistoryRepository(opened)
        subject.start_session(SessionRecord(session_id=SESSION, started_at=EPOCH))
        yield subject


def stored(shot: Shot, ordinal: int = 1, player: str | None = "Johan") -> StoredShot:
    return StoredShot(
        shot=shot,
        session_id=SESSION,
        ordinal=ordinal,
        recorded_at=EPOCH + timedelta(seconds=ordinal),
        player_name=player,
    )


# --- shots come back as the shots they were -------------------------------


def test_a_whole_shot_survives_the_round_trip(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    shot = make_shot(ball=launch(), club_delivery=delivery())

    repository.insert_shot(stored(shot), None)

    read = repository.shot(shot.shot_id)
    assert read is not None
    # Everything except the monotonic reading, which is the next test's subject.
    assert read.shot == replace(
        shot, source=replace(shot.source, observed_monotonic=0.0)
    )


def test_the_monotonic_reading_is_not_stored(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    # It counts from an arbitrary point chosen by the machine at boot, so it is
    # meaningless outside the process that took it. Storing it would invite
    # somebody to subtract two of them across a restart.
    shot = make_shot()
    assert shot.source.observed_monotonic != 0.0

    repository.insert_shot(stored(shot), None)

    read = repository.shot(shot.shot_id)
    assert read is not None
    assert read.shot.source.observed_monotonic == 0.0


def test_the_things_only_storage_knows_come_back_too(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    repository.insert_shot(stored(make_shot(), ordinal=7, player="Johan"), None)

    read = repository.shots(SESSION)[0]
    assert read.ordinal == 7
    assert read.player_name == "Johan"
    assert read.session_id == SESSION


def test_a_shot_with_no_club_data_comes_back_with_none(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    # Most launch monitors report no club data at all, so this is the common
    # case rather than the edge one.
    shot = make_shot(ball=launch(), club_delivery=None)

    repository.insert_shot(stored(shot), None)

    read = repository.shot(shot.shot_id)
    assert read is not None
    assert read.shot.club_delivery is None
    assert read.shot.ball == shot.ball


def test_a_club_only_shot_comes_back_with_no_ball(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    shot = make_shot(ball=None, club_delivery=delivery())

    repository.insert_shot(stored(shot), None)

    read = repository.shot(shot.shot_id)
    assert read is not None
    assert read.shot.ball is None
    assert read.shot.club_delivery == shot.club_delivery


def test_a_measurement_the_device_omitted_stays_omitted(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    # A zero here would be a number a player might believe, and it would drag
    # every average computed from the column towards it.
    shot = make_shot(ball=launch(carry=None, total_spin=None, side_spin=None))

    repository.insert_shot(stored(shot), None)

    read = repository.shot(shot.shot_id)
    assert read is not None
    assert read.shot.ball is not None
    assert read.shot.ball.carry_yards is None
    assert read.shot.ball.total_spin_rpm is None
    assert read.shot.ball.side_spin_rpm is None


def test_a_zero_measurement_is_kept_as_a_zero(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    # The other side of the same coin: a putter really does have a launch angle
    # of zero sometimes, and that is a measurement.
    shot = make_shot(
        club=ClubKind.PUTTER,
        ball=BallLaunch(
            speed_mph=6.2, launch_angle_degrees=0.0, launch_direction_degrees=0.0
        ),
    )

    repository.insert_shot(stored(shot), None)

    read = repository.shot(shot.shot_id)
    assert read is not None
    assert read.shot.ball is not None
    assert read.shot.ball.launch_angle_degrees == 0.0


def test_a_shot_hit_before_a_club_was_named_stores_no_club(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    shot = make_shot(club=None)

    repository.insert_shot(stored(shot), None)

    read = repository.shot(shot.shot_id)
    assert read is not None
    assert read.shot.club is None


def test_handedness_and_the_vendor_code_are_kept(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    shot = make_shot(club=ClubKind.IRON, vendor_code="I7", handedness=Handedness.LEFT)

    repository.insert_shot(stored(shot), None)

    read = repository.shot(shot.shot_id)
    assert read is not None
    assert read.shot.handedness is Handedness.LEFT
    assert read.shot.club is not None
    assert read.shot.club.vendor_code == "I7"


def test_provenance_is_kept_so_a_shot_can_be_traced_to_its_device(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    shot = make_shot()

    repository.insert_shot(stored(shot), None)

    read = repository.shot(shot.shot_id)
    assert read is not None
    assert read.shot.source.device_id == "ProTee VX"
    assert read.shot.source.device_sequence == shot.source.device_sequence
    assert read.shot.source.device_session_id == shot.source.device_session_id
    assert read.shot.source.reported_units == "Yards"
    assert read.shot.source.api_version == "1"


def test_a_shot_that_was_never_stored_reads_as_nothing(
    repository: HistoryRepository,
) -> None:
    assert repository.shot(uuid4()) is None


# --- time -----------------------------------------------------------------


def test_a_timestamp_comes_back_as_the_same_instant(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    observed = datetime(2026, 4, 1, 18, 42, 3, 250000, tzinfo=UTC)

    repository.insert_shot(stored(make_shot(observed_at=observed)), None)

    read = repository.shots(SESSION)[0]
    assert read.shot.source.observed_at == observed


def test_a_naive_timestamp_is_stored_as_utc(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    # A timestamp with no offset is one nobody can interpret in a year, so the
    # boundary forces one rather than leaving it to whoever reads the file.
    naive = datetime(2026, 4, 1, 18, 42)

    repository.insert_shot(stored(make_shot(observed_at=naive)), None)

    read = repository.shots(SESSION)[0]
    assert read.shot.source.observed_at == naive.replace(tzinfo=UTC)


# --- ordering and counting ------------------------------------------------


def test_shots_come_back_in_the_order_they_were_hit(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    for ordinal in (3, 1, 2):
        repository.insert_shot(stored(make_shot(), ordinal=ordinal), None)

    assert [read.ordinal for read in repository.shots(SESSION)] == [1, 2, 3]


def test_shots_of_another_session_are_not_returned(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    other = uuid4()
    repository.start_session(SessionRecord(session_id=other, started_at=EPOCH))
    repository.insert_shot(stored(make_shot()), None)

    assert repository.shots(other) == []
    assert repository.shot_count(SESSION) == 1


# --- sessions -------------------------------------------------------------


def test_closing_a_session_records_when_it_ended_and_how_many_shots(
    repository: HistoryRepository,
) -> None:
    ended = EPOCH + timedelta(hours=2)

    repository.close_session(SESSION, ended, shot_count=41)

    session = repository.session(SESSION)
    assert session is not None
    assert session.ended_at == ended
    assert session.shot_count == 41


def test_a_session_that_never_closed_reads_as_unfinished(
    repository: HistoryRepository,
) -> None:
    # Which is exactly what a crash or a kill leaves behind, and worth seeing.
    session = repository.session(SESSION)

    assert session is not None
    assert session.ended_at is None


def test_sessions_are_listed_most_recent_first(
    repository: HistoryRepository,
) -> None:
    older = uuid4()
    newer = uuid4()
    repository.start_session(
        SessionRecord(session_id=older, started_at=EPOCH - timedelta(days=1))
    )
    repository.start_session(
        SessionRecord(session_id=newer, started_at=EPOCH + timedelta(days=1))
    )

    listed = [session.session_id for session in repository.sessions()]

    assert listed[0] == newer
    assert listed.index(newer) < listed.index(SESSION) < listed.index(older)


def test_the_caddiej_that_wrote_a_session_is_recorded(
    repository: HistoryRepository,
) -> None:
    labelled = uuid4()
    repository.start_session(
        SessionRecord(
            session_id=labelled,
            started_at=EPOCH,
            label="Wednesday range",
            app_version="0.1.0",
        )
    )

    session = repository.session(labelled)
    assert session is not None
    assert session.label == "Wednesday range"
    assert session.app_version == "0.1.0"


# --- raw payloads ---------------------------------------------------------


def payload_for(shot: Shot, text: str = '{"ShotNumber": 1}') -> RawPayload:
    return RawPayload(
        shot_id=shot.shot_id,
        captured_at=EPOCH,
        source_kind="gspro_connect",
        payload=text,
    )


def test_a_raw_payload_is_stored_beside_its_shot(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    shot = make_shot()

    repository.insert_shot(stored(shot), payload_for(shot))

    payload = repository.payload(shot.shot_id)
    assert payload is not None
    assert payload.payload == '{"ShotNumber": 1}'
    assert payload.content_type == "application/x-ndjson"


def test_a_shot_stored_without_a_payload_has_none(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    shot = make_shot()

    repository.insert_shot(stored(shot), None)

    assert repository.payload(shot.shot_id) is None


def test_payloads_can_be_thrown_away_without_losing_the_shots(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    # They are the only part of the file that grows without bound, and the only
    # part nothing depends on.
    first, second = make_shot(), make_shot()
    repository.insert_shot(stored(first, ordinal=1), payload_for(first))
    repository.insert_shot(stored(second, ordinal=2), payload_for(second))

    removed = repository.forget_payloads(SESSION)

    assert removed == 2
    assert repository.payload(first.shot_id) is None
    assert repository.shot_count(SESSION) == 2


def test_a_payload_that_cannot_be_written_takes_its_shot_with_it(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    # A shot whose payload half-arrived is worse than one with no payload, so
    # the pair is one transaction.
    shot = make_shot()
    repository.insert_shot(stored(shot, ordinal=1), payload_for(shot))
    duplicate = make_shot()

    with pytest.raises(sqlite3.IntegrityError):
        repository.insert_shot(
            stored(duplicate, ordinal=2),
            # Same shot id as the first payload, which the table forbids.
            payload_for(shot, text="second"),
        )

    assert repository.shot(duplicate.shot_id) is None
    assert repository.shot_count(SESSION) == 1


# --- analysis references --------------------------------------------------


def test_an_analysis_can_be_attached_to_a_shot(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    # The seam for swing video and AI analysis: an opaque reference, so neither
    # needs a schema change when it arrives.
    shot = make_shot()
    repository.insert_shot(stored(shot), None)
    reference = AnalysisReference(
        analysis_id=uuid4(),
        shot_id=shot.shot_id,
        kind="swing_video",
        reference="clips/2026-04-01/shot-1.mp4",
        created_at=EPOCH,
    )

    repository.attach_analysis(reference)

    assert repository.analyses(shot.shot_id) == [reference]


def test_several_kinds_of_analysis_can_reference_one_shot(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    shot = make_shot()
    repository.insert_shot(stored(shot), None)
    for index, kind in enumerate(("swing_video", "ai_swing")):
        repository.attach_analysis(
            AnalysisReference(
                analysis_id=uuid4(),
                shot_id=shot.shot_id,
                kind=kind,
                reference=f"reference-{index}",
                created_at=EPOCH + timedelta(seconds=index),
            )
        )

    kinds = [analysis.kind for analysis in repository.analyses(shot.shot_id)]

    assert kinds == ["swing_video", "ai_swing"]


def test_the_same_analysis_cannot_be_attached_twice(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    shot = make_shot()
    repository.insert_shot(stored(shot), None)
    reference = AnalysisReference(
        analysis_id=uuid4(),
        shot_id=shot.shot_id,
        kind="swing_video",
        reference="clip.mp4",
        created_at=EPOCH,
    )
    repository.attach_analysis(reference)

    with pytest.raises(sqlite3.IntegrityError):
        repository.attach_analysis(
            AnalysisReference(
                analysis_id=uuid4(),
                shot_id=shot.shot_id,
                kind="swing_video",
                reference="clip.mp4",
                created_at=EPOCH,
            )
        )


def test_an_analysis_cannot_reference_a_shot_that_does_not_exist(
    repository: HistoryRepository,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        repository.attach_analysis(
            AnalysisReference(
                analysis_id=uuid4(),
                shot_id=uuid4(),
                kind="swing_video",
                reference="clip.mp4",
                created_at=EPOCH,
            )
        )


def test_a_club_delivery_of_nothing_but_nulls_reads_as_no_club_data(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    # Every column being null is indistinguishable from having no club block, and
    # treating it as one is the honest reading.
    shot = make_shot(ball=launch(), club_delivery=ClubDelivery())

    repository.insert_shot(stored(shot), None)

    read = repository.shot(shot.shot_id)
    assert read is not None
    assert read.shot.club_delivery is None


# --- the two queries the assistant boundary needs -------------------------


def test_the_last_shot_is_the_highest_ordinal_not_the_latest_timestamp(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    # Ordinals are assigned in arrival order and are unique; two shots recorded
    # in the same millisecond must still have an unambiguous last.
    first, second = make_shot(), make_shot()
    same_moment = EPOCH + timedelta(seconds=5)
    repository.insert_shot(
        replace(stored(first, ordinal=1), recorded_at=same_moment), None
    )
    repository.insert_shot(
        replace(stored(second, ordinal=2), recorded_at=same_moment), None
    )

    last = repository.last_shot(SESSION)

    assert last is not None
    assert last.shot.shot_id == second.shot_id
    assert last.ordinal == 2


def test_the_last_shot_of_a_session_with_nothing_in_it_is_nothing(
    repository: HistoryRepository,
) -> None:
    assert repository.last_shot(uuid4()) is None


def test_the_last_shot_ignores_other_sessions(
    repository: HistoryRepository, make_shot: ShotBuilder
) -> None:
    other = uuid4()
    repository.start_session(SessionRecord(session_id=other, started_at=EPOCH))
    mine, theirs = make_shot(), make_shot()
    repository.insert_shot(stored(mine, ordinal=1), None)
    repository.insert_shot(replace(stored(theirs, ordinal=9), session_id=other), None)

    last = repository.last_shot(SESSION)

    assert last is not None
    assert last.shot.shot_id == mine.shot_id
