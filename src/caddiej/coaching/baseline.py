"""What normal looks like for the person currently on the mat.

Built from the shots seen this run, not from the database. That is a deliberate
choice with three consequences worth having: coaching needs no persistence to
work, no query runs while somebody is standing over a ball, and "normal" means
how they are swinging tonight rather than how they swung last winter.

Per club, because a normal for a driver and a normal for a wedge have nothing to
do with each other, and a window rather than the whole run, because somebody who
has just changed something should be measured against what they are doing now.
"""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field

from caddiej.domain.shot import ClubKind, Shot
from caddiej.domain.statistics import MetricSummary, club_of, summarise_metrics


@dataclass(frozen=True, slots=True)
class ClubBaseline:
    """The player's own averages for one club, and how much they rest on."""

    club: ClubKind
    shots: int
    metrics: Mapping[str, MetricSummary] = field(default_factory=dict)

    @property
    def is_warm(self) -> bool:
        """Whether there is enough here to call anything unusual."""

        return self.shots > 1


class Baseline:
    """A rolling window of recent shots, grouped by club.

    Deliberately not a running mean. Recomputing over at most a few dozen shots
    costs nothing measurable and the standard deviation stays exact, which is
    what severity is scaled by. An incremental variance would save arithmetic
    nobody is short of and introduce drift into the number that decides what a
    player is told.
    """

    def __init__(self, window: int = 40) -> None:
        if window < 1:
            raise ValueError("window must hold at least one shot")
        self._window = window
        self._by_club: dict[ClubKind, deque[Shot]] = {}

    @property
    def shots(self) -> int:
        return sum(len(shots) for shots in self._by_club.values())

    def remember(self, shot: Shot) -> None:
        """Add a shot to the player's normal.

        Called after the shot has been diagnosed, never before. A shot included
        in its own baseline is by definition average, so the order is what stops
        every first shot with a club reading as unremarkable.
        """

        club = club_of(shot)
        self._by_club.setdefault(club, deque(maxlen=self._window)).append(shot)

    def for_club(self, club: ClubKind) -> ClubBaseline:
        shots = self._by_club.get(club)
        if not shots:
            return ClubBaseline(club=club, shots=0)
        return ClubBaseline(
            club=club,
            shots=len(shots),
            metrics=summarise_metrics(tuple(shots)),
        )

    def forget(self) -> None:
        """Start again, for a new player or a new session."""

        self._by_club.clear()
