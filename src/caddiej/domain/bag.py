"""How far the player hits each club, and which one a distance calls for.

Two things kept deliberately apart. A `ClubDistanceProfile` is a statement about
a player — twelve numbers somebody measured or guessed — and `recommend_club` is
a rule about golf. Today the numbers come from configuration; the intention is
that they will later be learned from shot history, and that change must not
reach the rule or anything that renders it. `ClubDistanceProvider` is the seam
where that swap happens.

Carries are metres, matching the only target distance CaddieJ has: GSPro's
`Player.DistanceToTarget`, read passively out of the ProTee log. Nothing here
converts units, because nothing here has a second unit to convert from.

A club is a label rather than a `ClubKind`: `ClubKind` is a coarse category
("iron") and a bag has nine of them. The label is the operator's own word for
the club — `7I`, `PW` — and is shown on the floor exactly as written.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ClubCarry:
    """One club and how far the player carries it, in metres."""

    label: str
    carry_m: float

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("ClubCarry.label must name a club")
        if not math.isfinite(self.carry_m) or self.carry_m <= 0.0:
            raise ValueError("ClubCarry.carry_m must be a positive finite number")


@dataclass(frozen=True, slots=True)
class ClubDistanceProfile:
    """The clubs CaddieJ knows a distance for, longest first.

    Ordering is part of the value rather than something a caller re-derives,
    because it is what makes the recommendation's tie-break stable: two clubs
    the same distance from a target resolve to the longer one, and "the longer
    one" has to mean the same thing every time it is asked.

    An empty profile is legitimate and means nobody has measured a bag yet. It
    is not an error and must not become a guess.
    """

    carries: tuple[ClubCarry, ...] = ()

    def __post_init__(self) -> None:
        labels = [carry.label for carry in self.carries]
        if len(set(labels)) != len(labels):
            raise ValueError("ClubDistanceProfile must not name a club twice")
        object.__setattr__(
            self,
            "carries",
            tuple(sorted(self.carries, key=lambda carry: -carry.carry_m)),
        )

    @property
    def is_empty(self) -> bool:
        return not self.carries

    @classmethod
    def from_carries(cls, carries: Mapping[str, float]) -> ClubDistanceProfile:
        """Build a profile from labelled metres, dropping the clubs not in the bag.

        Zero is how configuration says "I do not carry this club", so it is
        filtered here rather than rejected: a bag is defined by what is in it,
        and a settings file that lists every club CaddieJ knows about with most
        of them at zero is easier to edit than one that must be pruned.
        """

        return cls(
            carries=tuple(
                ClubCarry(label=label, carry_m=float(metres))
                for label, metres in carries.items()
                if math.isfinite(metres) and metres > 0.0
            )
        )


@dataclass(frozen=True, slots=True)
class ClubRecommendation:
    """Which club to hit, and the carry that answer was based on."""

    club: str
    expected_carry_m: float


class ClubDistanceProvider(Protocol):
    """Where the player's carries come from.

    One method, called every time a recommendation is wanted, so a provider
    that learns from shot history can change its answer between shots without
    anything upstream holding a stale copy.
    """

    def profile(self) -> ClubDistanceProfile:
        """The player's current carries. May be empty."""


@dataclass(frozen=True, slots=True)
class StaticClubDistances:
    """A provider over carries somebody configured once."""

    carries: ClubDistanceProfile = ClubDistanceProfile()

    def profile(self) -> ClubDistanceProfile:
        return self.carries


def recommend_club(
    target_distance_m: float | None,
    profile: ClubDistanceProfile,
) -> ClubRecommendation | None:
    """The club whose carry is nearest the target, or None if that is unknowable.

    Version one on purpose: no wind, no elevation, no lie, no rollout, no
    temperature. CaddieJ observes none of those passively, and a correction
    applied to data it does not have would be a number the player could not
    check. What it does have — a target distance from GSPro and a set of
    carries — supports exactly this much, and the signature is the seam where
    more can arrive later without the floor learning any golf.

    None rather than a nearest-anything when the target is missing or the bag is
    empty. A recommendation the player cannot trust is worse than no line on
    the floor, and a dash under the word RECOMMENDED is worse than both.
    """

    if target_distance_m is None or not math.isfinite(target_distance_m):
        return None
    if target_distance_m <= 0.0 or profile.is_empty:
        return None
    # Profile order is longest first, so `min` keeps the longer club on a tie:
    # 145 m between a 140 m and a 150 m club takes the 150. That is the way a
    # golfer resolves it, and more importantly it is the same way every time.
    nearest = min(
        profile.carries,
        key=lambda carry: abs(carry.carry_m - target_distance_m),
    )
    return ClubRecommendation(club=nearest.label, expected_carry_m=nearest.carry_m)
