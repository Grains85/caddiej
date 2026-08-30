"""Keeping what was said about a shot, and being able to ask about it later.

A coaching row is only worth writing if it can be trusted afterwards, so these
tests care about two things: that everything needed to interpret a line survives
the round trip — which model, which prompt, how severe, from a cache or not —
and that a failed analysis is recorded as a failure rather than as an absence.

An absence would be indistinguishable from a shot nothing was ever asked about,
and those are different evenings.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from caddiej.domain.coaching import (
    Advice,
    AdviceFailure,
    AdviceOutcome,
    AdviceSource,
    AdviceStatus,
    CoachingFocus,
    SeveritySource,
)
from caddiej.domain.events import ShotCompleted
from caddiej.domain.shot import Shot
from caddiej.persistence.schema import SCHEMA_VERSION
from caddiej.persistence.service import HistoryService, build_history_service
from conftest import ShotBuilder

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 1, 18, 30, tzinfo=UTC)


@pytest.fixture
async def history(tmp_path: Path) -> AsyncIterator[HistoryService]:
    subject = build_history_service(
        enabled=True, path=tmp_path / "history.sqlite3", player_name="Johan"
    )
    await subject.open()
    yield subject
    await subject.aclose()


def an_advice(
    shot_id: UUID,
    focus: CoachingFocus = CoachingFocus.FACE_CONTROL,
    severity: float = 0.62,
    source: AdviceSource = AdviceSource.MODEL,
) -> Advice:
    return Advice(
        shot_id=shot_id,
        focus=focus,
        display="FACE OPEN",
        speech="Square the face a little earlier.",
        severity=severity,
        source=source,
        created_at=NOW,
        severity_source=SeveritySource.MEASURED,
    )


def an_outcome(
    shot_id: UUID,
    status: AdviceStatus = AdviceStatus.OK,
    focus: CoachingFocus = CoachingFocus.FACE_CONTROL,
    failure: AdviceFailure | None = None,
    **overrides: object,
) -> AdviceOutcome:
    fields: dict[str, object] = {
        "advice": an_advice(shot_id, focus=focus),
        "status": status,
        "baseline_shots": 12,
        "provider": "openai",
        "model": "gpt-test",
        "prompt_version": 3,
        "latency_ms": 812,
        "input_images": 2,
        "input_tokens": 910,
        "output_tokens": 22,
        "cache_hit": False,
        "failure": failure,
        "detail": None,
        "clip_path": "clips/2026/04/01/swing.mp4",
        "fingerprint": "a1b2c3",
    }
    fields.update(overrides)
    return AdviceOutcome(**fields)  # type: ignore[arg-type]


async def a_recorded_shot(history: HistoryService, shot: Shot) -> Shot:
    """Put a shot in the database, so advice has something to point at.

    Polled rather than counted in event-loop turns, because the write really
    does happen on another thread.
    """

    wanted = history.shots_recorded + 1
    await history.on_shot_completed(ShotCompleted(shot=shot))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while history.shots_recorded < wanted:
        if loop.time() >= deadline:
            raise AssertionError("the writer never caught up")
        await asyncio.sleep(0.005)
    return shot


# --- the round trip -------------------------------------------------------


async def test_the_schema_makes_room_for_coaching() -> None:
    # A migration, not an edit to the original schema: an existing history file
    # has to survive the upgrade.
    assert SCHEMA_VERSION >= 2


async def test_everything_needed_to_read_a_line_later_survives(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    shot = await a_recorded_shot(history, make_shot())

    stored = await history.record_advice(an_outcome(shot.shot_id))

    assert stored is not None
    [read] = await history.advice(shot.shot_id)
    assert read.advice_id == stored.advice_id
    outcome = read.outcome
    assert outcome.status is AdviceStatus.OK
    assert outcome.advice.display == "FACE OPEN"
    assert outcome.advice.speech == "Square the face a little earlier."
    assert outcome.advice.focus is CoachingFocus.FACE_CONTROL
    assert outcome.advice.severity == pytest.approx(0.62)
    assert outcome.advice.source is AdviceSource.MODEL
    assert outcome.advice.severity_source is SeveritySource.MEASURED
    assert outcome.advice.created_at == NOW


async def test_which_model_said_it_is_kept(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    # Without this a row is uninterpretable the moment the model or the prompt
    # changes, which they will.
    shot = await a_recorded_shot(history, make_shot())

    await history.record_advice(an_outcome(shot.shot_id))

    [read] = await history.advice(shot.shot_id)
    assert read.outcome.provider == "openai"
    assert read.outcome.model == "gpt-test"
    assert read.outcome.prompt_version == 3


async def test_what_it_cost_is_kept(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    shot = await a_recorded_shot(history, make_shot())

    await history.record_advice(an_outcome(shot.shot_id))

    [read] = await history.advice(shot.shot_id)
    assert read.outcome.input_tokens == 910
    assert read.outcome.output_tokens == 22
    assert read.outcome.latency_ms == 812
    assert read.outcome.input_images == 2
    assert read.outcome.cache_hit is False


async def test_the_clip_it_was_looking_at_is_kept(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    # The link between a coaching line and the footage behind it, which is what
    # makes a later review of either one possible.
    shot = await a_recorded_shot(history, make_shot())

    await history.record_advice(an_outcome(shot.shot_id))

    [read] = await history.advice(shot.shot_id)
    assert read.outcome.clip_path == "clips/2026/04/01/swing.mp4"


async def test_a_cached_line_is_marked_as_one(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    shot = await a_recorded_shot(history, make_shot())

    await history.record_advice(an_outcome(shot.shot_id, cache_hit=True))

    [read] = await history.advice(shot.shot_id)
    assert read.outcome.cache_hit is True


async def test_a_templated_line_is_distinguishable_from_a_coached_one(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    # Otherwise an evening of failed calls reads as an evening of AI coaching.
    shot = await a_recorded_shot(history, make_shot())

    await history.record_advice(
        an_outcome(
            shot.shot_id,
            status=AdviceStatus.TEMPLATE,
            advice=an_advice(shot.shot_id, source=AdviceSource.TEMPLATE),
            provider=None,
            model=None,
            prompt_version=None,
        )
    )

    [read] = await history.advice(shot.shot_id)
    assert read.outcome.status is AdviceStatus.TEMPLATE
    assert read.outcome.advice.source is AdviceSource.TEMPLATE
    assert read.outcome.model is None


# --- failures are recorded, not omitted -----------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        AdviceFailure.TIMED_OUT,
        AdviceFailure.INVALID_REPLY,
        AdviceFailure.PROVIDER_ERROR,
        AdviceFailure.RATE_LIMITED,
        AdviceFailure.SUPERSEDED,
        AdviceFailure.BUDGET_EXHAUSTED,
        AdviceFailure.THROTTLED,
        AdviceFailure.NO_CREDENTIALS,
        AdviceFailure.DISABLED,
    ],
)
async def test_every_way_it_can_fail_is_recorded_as_that_failure(
    history: HistoryService, make_shot: ShotBuilder, failure: AdviceFailure
) -> None:
    shot = await a_recorded_shot(history, make_shot())

    await history.record_advice(
        an_outcome(
            shot.shot_id,
            status=AdviceStatus.UNAVAILABLE,
            failure=failure,
            detail="something went wrong",
        )
    )

    [read] = await history.advice(shot.shot_id)
    assert read.outcome.status is AdviceStatus.UNAVAILABLE
    assert read.outcome.failure is failure
    assert read.outcome.detail == "something went wrong"


async def test_a_failure_still_keeps_the_line_the_player_read(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    # The templated line was on the floor whatever the model did, so the row has
    # to say what was actually shown.
    shot = await a_recorded_shot(history, make_shot())

    await history.record_advice(
        an_outcome(
            shot.shot_id,
            status=AdviceStatus.UNAVAILABLE,
            failure=AdviceFailure.TIMED_OUT,
            advice=an_advice(shot.shot_id, source=AdviceSource.TEMPLATE),
        )
    )

    [read] = await history.advice(shot.shot_id)
    assert read.outcome.advice.display == "FACE OPEN"


# --- asking about a session -----------------------------------------------


async def test_a_session_can_be_asked_what_it_was_full_of(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    # The point of keeping any of this: what an evening was actually about.
    for focus in (
        CoachingFocus.FACE_CONTROL,
        CoachingFocus.FACE_CONTROL,
        CoachingFocus.PATH,
    ):
        shot = await a_recorded_shot(history, make_shot())
        await history.record_advice(an_outcome(shot.shot_id, focus=focus))

    counts = await history.focus_counts()

    assert counts["face_control"] == 2
    assert counts["path"] == 1


async def test_a_shot_nothing_was_said_about_has_no_advice(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    shot = await a_recorded_shot(history, make_shot())

    assert await history.advice(shot.shot_id) == []


async def test_a_session_lists_everything_said_in_it(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    for _ in range(3):
        shot = await a_recorded_shot(history, make_shot())
        await history.record_advice(an_outcome(shot.shot_id))

    assert len(await history.session_advice()) == 3


async def test_another_session_is_not_counted(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    shot = await a_recorded_shot(history, make_shot())
    await history.record_advice(an_outcome(shot.shot_id))

    assert await history.session_advice(uuid4()) == []
    assert await history.focus_counts(uuid4()) == {}


async def test_a_fault_still_counts_when_the_model_failed_to_word_it(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    # What is being counted is what the player was told. A failed call still
    # left the templated line on the floor, so the fault was still named.
    coached = await a_recorded_shot(history, make_shot())
    await history.record_advice(an_outcome(coached.shot_id))
    templated = await a_recorded_shot(history, make_shot())
    await history.record_advice(
        an_outcome(
            templated.shot_id,
            status=AdviceStatus.UNAVAILABLE,
            failure=AdviceFailure.TIMED_OUT,
            focus=CoachingFocus.PATH,
        )
    )

    counts = await history.focus_counts()

    assert counts == {"face_control": 1, "path": 1}


async def test_shots_with_nothing_wrong_do_not_lead_the_tally(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    # They are the bulk of a good evening. A summary led by "none: 40" answers
    # a question nobody asked and buries the one they did.
    for _ in range(4):
        good = await a_recorded_shot(history, make_shot())
        await history.record_advice(
            an_outcome(
                good.shot_id, status=AdviceStatus.TEMPLATE, focus=CoachingFocus.NONE
            )
        )
    bad = await a_recorded_shot(history, make_shot())
    await history.record_advice(an_outcome(bad.shot_id))

    counts = await history.focus_counts()

    assert counts == {"face_control": 1}


# --- history being unavailable --------------------------------------------


async def test_with_history_switched_off_recording_is_simply_skipped(
    tmp_path: Path,
) -> None:
    # The supported configuration of somebody who does not want a database.
    service = build_history_service(enabled=False, path=tmp_path / "unused.sqlite3")
    await service.open()

    assert await service.record_advice(an_outcome(uuid4())) is None
    assert await service.advice(uuid4()) == []
    assert await service.focus_counts() == {}

    await service.aclose()


async def test_a_line_that_cannot_be_stored_is_not_an_exception(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    # A coaching line the player already read is not worth raising over because
    # the row describing it did not land.
    service = build_history_service(enabled=True, path=tmp_path / "history.sqlite3")
    await service.open()
    await service.aclose()

    assert await service.record_advice(an_outcome(uuid4())) is None


# --- the last few lines, for anything summarising a stretch of practice ----


async def test_recent_advice_returns_the_last_lines_in_the_order_they_were_said(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    for _ in range(5):
        shot = await a_recorded_shot(history, make_shot())
        await history.record_advice(an_outcome(shot.shot_id))

    recent = await history.recent_advice(limit=2)

    ordinals = [row.outcome.advice.shot_id for row in recent]
    everything = [row.outcome.advice.shot_id for row in await history.session_advice()]
    # The last two of the five, still oldest-first: a summary of a stretch of
    # practice reads forwards even when only the tail of it was asked for.
    assert ordinals == everything[-2:]


async def test_recent_advice_asked_for_more_than_exists_returns_what_exists(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    shot = await a_recorded_shot(history, make_shot())
    await history.record_advice(an_outcome(shot.shot_id))

    assert len(await history.recent_advice(limit=50)) == 1


async def test_recent_advice_of_nothing_is_nothing(history: HistoryService) -> None:
    assert await history.recent_advice(limit=5) == []


async def test_recent_advice_refuses_a_limit_of_nothing(
    history: HistoryService, make_shot: ShotBuilder
) -> None:
    # Zero and below are answered without a query rather than passed to SQLite,
    # where a negative LIMIT means "no limit" and would return the session.
    shot = await a_recorded_shot(history, make_shot())
    await history.record_advice(an_outcome(shot.shot_id))

    assert await history.recent_advice(limit=0) == []
    assert await history.recent_advice(limit=-1) == []


async def test_recent_advice_is_empty_when_history_is_not_working(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    service = build_history_service(enabled=False, path=tmp_path / "history.sqlite3")
    await service.open()

    assert await service.recent_advice(limit=5) == []
    assert await service.last_shot() is None
    await service.aclose()
