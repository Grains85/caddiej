"""The one thing an assistant can change, and everything it cannot.

Half of this file is about validation, because `focus` is the only string this
whole surface accepts and it is therefore the only place an injection of any
kind could begin. The other half is about the mutation being harmless: it must
be safe to call at any moment, from a process CaddieJ makes no promises about,
while somebody is standing over a ball.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from assistant_support import Boundary, boundary, closing

from caddiej.assistant.commands import (
    CommandFacade,
    FocusRejected,
    legal_focuses,
    parse_focus,
)
from caddiej.assistant.contract import Availability
from caddiej.coaching.service import AnalysisService
from caddiej.domain.coaching import CoachingFocus

# No module-level asyncio mark: half of this file is synchronous validation, and
# `asyncio_mode = auto` already runs the rest.


@pytest.fixture
async def subject(tmp_path: Path) -> AsyncIterator[Boundary]:
    async for ready in closing(await boundary(tmp_path)):
        yield ready


# --- validation -----------------------------------------------------------


def test_every_focus_the_domain_knows_is_accepted() -> None:
    for value in legal_focuses():
        assert parse_focus(value) is CoachingFocus(value)


def test_nothing_means_no_preference() -> None:
    assert parse_focus(None) is None
    assert parse_focus("") is None
    assert parse_focus("   ") is None


def test_the_word_none_clears_the_preference_rather_than_being_refused() -> None:
    # `NONE` names a shot with nothing wrong, so it is not something to work on.
    # But "none" is what an assistant will send when it means "clear it", and
    # that reading is unambiguous.
    assert parse_focus("none") is None


@pytest.mark.parametrize(
    "written", ["Face Control", "FACE_CONTROL", "  face-control  ", "face control"]
)
def test_a_reasonable_spelling_is_understood(written: str) -> None:
    # An assistant will write it as prose sooner or later, and the intent is not
    # in doubt. Forgiving case and separators is not the same as accepting
    # anything: the result still has to be a member of the enum.
    assert parse_focus(written) is CoachingFocus.FACE_CONTROL


@pytest.mark.parametrize(
    "hostile",
    [
        "path; rm -rf /",
        "../../etc/passwd",
        "{{ 7*7 }}",
        "path\x00",
        "'; DROP TABLE shots; --",
        "path path",
        "swing harder",
        "consistency2",
    ],
)
def test_anything_that_is_not_a_focus_is_refused(hostile: str) -> None:
    # There is no request this surface accepts that can carry a path, an
    # expression or a sentence. That is a property of the closed enum rather
    # than of any filtering, which is why none of these needs a special case.
    with pytest.raises(FocusRejected):
        parse_focus(hostile)


def test_a_refusal_names_the_legal_values() -> None:
    # More useful to an assistant than a rejection: it can correct itself.
    with pytest.raises(FocusRejected, match="face_control"):
        parse_focus("nonsense")


def test_the_legal_values_exclude_saying_nothing() -> None:
    assert CoachingFocus.NONE.value not in legal_focuses()
    assert len(legal_focuses()) == len(CoachingFocus) - 1


# --- the mutation ---------------------------------------------------------


async def test_setting_a_focus_reaches_coaching(subject: Boundary) -> None:
    result = subject.commands.set_training_focus("path")

    assert result.training_focus == "path"
    assert result.changed is True
    assert subject.coaching.preferred_focus is CoachingFocus.PATH


async def test_setting_a_focus_reports_what_it_changed_from(
    subject: Boundary,
) -> None:
    subject.commands.set_training_focus("path")

    result = subject.commands.set_training_focus("strike")

    assert result.previous_focus == "path"
    assert result.training_focus == "strike"


async def test_setting_the_same_focus_twice_changes_nothing_the_second_time(
    subject: Boundary,
) -> None:
    subject.commands.set_training_focus("path")

    result = subject.commands.set_training_focus("path")

    assert result.changed is False
    assert result.training_focus == "path"


async def test_a_null_focus_clears_the_preference(subject: Boundary) -> None:
    subject.commands.set_training_focus("path")

    result = subject.commands.set_training_focus(None)

    assert result.training_focus is None
    assert result.previous_focus == "path"
    assert subject.coaching.preferred_focus is None


async def test_a_bad_focus_changes_nothing(subject: Boundary) -> None:
    subject.commands.set_training_focus("path")

    with pytest.raises(FocusRejected):
        subject.commands.set_training_focus("nonsense")

    assert subject.coaching.preferred_focus is CoachingFocus.PATH


async def test_the_change_is_reported_as_unavailable_when_coaching_is_off(
    tmp_path: Path,
) -> None:
    # Setting a preference nothing will read is not a success. It still takes
    # effect, because the operator may switch coaching on later and silently
    # discarding the request would be worse.
    ready = await boundary(tmp_path)
    try:
        commands = CommandFacade(
            AnalysisService(publish=ready.coaching._publish, enabled=False)
        )

        result = commands.set_training_focus("path")

        assert result.availability is Availability.UNAVAILABLE
        assert result.training_focus == "path"
    finally:
        await ready.coaching.aclose()
        await ready.play.aclose()
        await ready.history.aclose()


async def test_coaching_without_a_model_is_not_reported_as_unavailable(
    subject: Boundary,
) -> None:
    """The default configuration, and it must not read as broken.

    With no provider configured CaddieJ still names the fault and words it from
    a template, and the preference biases exactly that choice. Answering
    `unavailable` here would tell an assistant the feature did not work when it
    did.
    """

    assert subject.coaching.is_available is False

    result = subject.commands.set_training_focus("path")

    assert result.availability is Availability.OK


async def test_the_read_facade_reports_the_change_afterwards(
    subject: Boundary,
) -> None:
    assert subject.reads.status().training_focus is None

    subject.commands.set_training_focus("early_extension")

    assert subject.reads.status().training_focus == "early_extension"


# --- what is not here -----------------------------------------------------


def test_the_command_facade_exposes_exactly_one_mutation() -> None:
    """The empty space is the design.

    Nothing here can start or end a session, write to the floor, speak, reload
    configuration, or touch OBS. Each of those was considered and left out, and
    this test is what makes adding one a deliberate act rather than a drive-by.
    """

    mutations = {
        name
        for name in dir(CommandFacade)
        if not name.startswith("_") and callable(getattr(CommandFacade, name))
    }

    assert mutations == {"set_training_focus"}
