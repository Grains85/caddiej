"""Which physical tee a club uses.

Marker *appearance* (wide driver halo, flat putter) is ``marker_variant_for``.
This module is only *where* the ball goes, in metres on the hitting bay.
A later club group (putter, and so on) is another entry in ``by_kind``.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from caddiej.domain.shot import Club, ClubKind
from caddiej.floor.geometry import PhysicalPoint


@dataclass(frozen=True, slots=True)
class TeePositions:
    """Physical tee points, keyed by club kind, with one default for the rest."""

    default: PhysicalPoint
    by_kind: Mapping[ClubKind, PhysicalPoint] = field(default_factory=dict)

    def for_club(self, club: Club | None) -> PhysicalPoint:
        """DRIVER uses its own point; every other club, and no club, uses default."""

        if club is None:
            return self.default
        return self.by_kind.get(club.kind, self.default)
