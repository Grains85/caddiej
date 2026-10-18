"""The only things an assistant can change, which today is one thing.

A separate object from `ReadFacade` rather than more methods on it, and the
separation is structural rather than tidy: `ReadFacade` holds nothing it could
write through, and when mutation is switched off this class is never constructed
at all. So "an assistant cannot change anything" is provable by looking at the
object graph, not by trusting a route table.

What belongs here is bounded by one rule, which is §2 restated for this
boundary: **a command reachable from an assistant is in the enrichment tier.**
It may not influence shot ingestion, floor state, scoring, session recording, or
anything else with a latency guarantee. `set_training_focus` qualifies because
it only stores a preference. Ideal Swing does not use an old fault name as its
focus. No shot already in flight changes, nothing recomputes, and if it hung
for thirty seconds a player could still hit the next ball and read correct
numbers off the floor.

What is deliberately absent, and why, because the empty space is the design:

* **Starting or ending a session.** It would be the first thing an assistant
  could do that changes what gets recorded, and it means rolling a live batching
  writer over mid-run. Worth doing deliberately, not as a fifth route.
* **Anything about the floor.** The floor is a projection of state CaddieJ owns
  (§3). An assistant writing to it would make it a projection of two things.
* **Speaking.** A tool that puts arbitrary text through the voice is a tool that
  lets a remote model say anything in somebody's living room.
* **Reloading configuration.** Config is the operator's, from a file they own.
"""

import logging

from caddiej.assistant.contract import Availability, TrainingFocusResult
from caddiej.assistant.reads import CoachingPort
from caddiej.domain.coaching import CoachingFocus

logger = logging.getLogger(__name__)


class FocusRejected(ValueError):
    """The requested focus is not one CaddieJ recognises.

    A value error rather than a soft result, because a misspelled focus is a
    caller mistake and should be corrected rather than absorbed. The transport
    turns it into a refusal that names the legal values.
    """


def legal_focuses() -> tuple[str, ...]:
    """Every focus an assistant may ask for, from the enum itself.

    The contract spells the same list out as literals so that it imports nothing
    from CaddieJ. This is the authority; a test holds the two together.
    """

    return tuple(
        focus.value for focus in CoachingFocus if focus is not CoachingFocus.NONE
    )


def parse_focus(value: str | None) -> CoachingFocus | None:
    """Turn a wire value into a focus, or refuse it.

    Validation happens here rather than in the contract model so that the
    transport, the MCP server and a test all reject the same strings for the
    same reason. Whitespace and case are forgiven because an assistant will
    produce `\"Face Control\"` sooner or later and the intent is unambiguous;
    anything else is refused with the legal values named.
    """

    if value is None:
        return None
    candidate = value.strip().lower().replace(" ", "_").replace("-", "_")
    if not candidate:
        return None
    try:
        focus = CoachingFocus(candidate)
    except ValueError:
        raise FocusRejected(
            f"unknown training focus; expected one of: {', '.join(legal_focuses())}"
        ) from None
    if focus is CoachingFocus.NONE:
        # Accepted rather than refused, because "none" is what an assistant will
        # send when it means "clear it", and that is a reasonable reading.
        return None
    return focus


class CommandFacade:
    """Everything an assistant is allowed to change."""

    def __init__(self, coaching: CoachingPort) -> None:
        self._coaching = coaching

    def set_training_focus(self, value: str | None) -> TrainingFocusResult:
        """Point coaching at one thing, or clear the preference.

        Reports `UNAVAILABLE` when coaching is not running, because setting a
        preference nothing will read is not a success. It still takes effect:
        the operator may turn coaching on later, and silently discarding the
        request would be worse than storing it.

        The result reports the focus coaching actually kept. Ideal Swing does
        not take an old fault name, so the stored focus stays empty and the
        call is not reported as a change.
        """

        focus = parse_focus(value)
        previous = self._coaching.preferred_focus
        self._coaching.set_preferred_focus(focus)
        applied = self._coaching.preferred_focus

        changed = applied is not previous
        logger.info(
            "assistant_training_focus_set",
            extra={
                "assistant_tool": "set_training_focus",
                "coaching_focus": "none" if focus is None else focus.value,
                "assistant_changed": changed,
            },
        )
        return TrainingFocusResult(
            availability=(
                Availability.OK
                if self._coaching.is_coaching
                else Availability.UNAVAILABLE
            ),
            training_focus=None if applied is None else applied.value,
            previous_focus=None if previous is None else previous.value,
            changed=changed,
        )
