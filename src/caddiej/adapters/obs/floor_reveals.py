"""Which floor visibility changes CaddieJ made on purpose.

Swing capture watches the floor scene while it takes stills, because a coach
source drawn on the projector is the one failure that capture-scene isolation
exists to make impossible. But the floor is not static during that window: a
reveal shows ProTee's Top Impact view a moment after the strike, and that view
is often the very source capture reads. Comparing before and after cannot tell
those apart, and a warning that fires on every shot teaches the operator to
ignore it — including on the shot where a swing camera really did reach the
floor.

The two sides cannot simply ask each other. Each reveal and the capture service
hold their own OBS connection, so nothing in one `ObsClient` knows what another
asked OBS to do. What they share is this process, so the reveal writes down
what it is about to change and capture reads what was written during its
window. Nothing is matched by name: a source is excused because a reveal said
it was changing that item in that scene, and for no other reason. A source no
reveal is configured for — a swing camera, the metrics panel — can never be
excused, whatever it is called.

The intent is recorded before the OBS command and again after it, and a change
still in flight counts. OBS applies the command at some point between the two,
and capture can take its snapshots on either side of that point.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


class FloorReveals:
    """A sequence number per scene item, bumped whenever a reveal touches it."""

    def __init__(self) -> None:
        self._sequence = 0
        self._latest: dict[tuple[str, str], int] = {}
        self._in_flight: dict[tuple[str, str], int] = {}

    def mark(self) -> int:
        return self._sequence

    @contextmanager
    def changing(self, scene: str, source: str) -> Iterator[None]:
        key = (scene, source)
        self._touch(key)
        self._in_flight[key] = self._in_flight.get(key, 0) + 1
        try:
            yield
        finally:
            remaining = self._in_flight[key] - 1
            if remaining:
                self._in_flight[key] = remaining
            else:
                del self._in_flight[key]
            self._touch(key)

    def since(self, mark: int, scene: str) -> frozenset[str]:
        """Sources in `scene` a reveal changed after `mark`, or is changing now."""

        return frozenset(
            source
            for (item_scene, source), sequence in self._latest.items()
            if item_scene == scene
            and (sequence > mark or (item_scene, source) in self._in_flight)
        )

    def _touch(self, key: tuple[str, str]) -> None:
        self._sequence += 1
        self._latest[key] = self._sequence


FLOOR_REVEALS = FloorReveals()
