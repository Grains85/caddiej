"""Which ProTee views CaddieJ may briefly reveal.

A table, not a class hierarchy. There used to be a package and a
`TimedObsSource` subclass for each of these; the two subclasses differed in a
string, a docstring and a default nobody used, because everything that makes a
reveal work already lives in `TimedObsSource`. What actually distinguishes
them is a name for the log and a name for a human, so that is all this is.

Adding a third ProTee view is a row here plus its settings.
"""

from __future__ import annotations

from enum import StrEnum


class RevealFeature(StrEnum):
    """One revealable ProTee view.

    The value is the log prefix and the bus subscriber name, both of which
    appear in `event_policy` and in session logs, so it is the stable
    identifier rather than a display string.
    """

    SHOT_VIDEO = "shot_video"
    TOP_IMPACT = "top_impact"

    @property
    def label(self) -> str:
        """What to call it in front of an operator running `--check-obs-*`."""

        return _LABELS[self]

    @property
    def scheduled_event(self) -> str:
        """The log line to grep for when OBS turns out to be fine."""

        return f"{self.value}_replay_scheduled"


_LABELS = {
    RevealFeature.SHOT_VIDEO: "Shot Video",
    RevealFeature.TOP_IMPACT: "Top Impact",
}
