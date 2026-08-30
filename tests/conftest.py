"""Builders shared by the tests that need shots to exist.

A shot has a required source with required provenance, which is right for the
real thing and tedious in a test that cares about one number. This gives every
test a plausible shot it can override a field of, so a test about averages reads
as a test about averages.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import pytest

from caddiej.domain.shot import (
    BallLaunch,
    Club,
    ClubDelivery,
    ClubKind,
    Handedness,
    Shot,
    ShotSource,
)

EPOCH = datetime(2026, 4, 1, 18, 30, tzinfo=UTC)


class ShotBuilder(Protocol):
    def __call__(
        self,
        *,
        club: ClubKind | None = ...,
        vendor_code: str | None = ...,
        ball: BallLaunch | None = ...,
        club_delivery: ClubDelivery | None = ...,
        handedness: Handedness = ...,
        observed_at: datetime | None = ...,
        observed_monotonic: float | None = ...,
        device_session_id: UUID | None = ...,
        raw_payload: str | None = ...,
    ) -> Shot: ...


def launch(
    speed: float = 148.2,
    launch_angle: float = 12.6,
    direction: float = 1.5,
    carry: float | None = 268.4,
    total_spin: float | None = 2480.0,
    side_spin: float | None = -180.0,
) -> BallLaunch:
    return BallLaunch(
        speed_mph=speed,
        launch_angle_degrees=launch_angle,
        launch_direction_degrees=direction,
        carry_yards=carry,
        total_spin_rpm=total_spin,
        side_spin_rpm=side_spin,
    )


def delivery(
    speed: float | None = 112.4,
    path: float | None = -2.4,
    face_to_target: float | None = 1.1,
    attack: float | None = 3.2,
) -> ClubDelivery:
    return ClubDelivery(
        speed_mph=speed,
        path_degrees=path,
        face_to_target_degrees=face_to_target,
        angle_of_attack_degrees=attack,
    )


@pytest.fixture
def make_shot() -> ShotBuilder:
    """A factory for shots, with everything defaulted to something plausible."""

    session = uuid4()
    counter = 0

    def build(
        *,
        club: ClubKind | None = ClubKind.DRIVER,
        vendor_code: str | None = "DR",
        ball: BallLaunch | None = None,
        club_delivery: ClubDelivery | None = None,
        handedness: Handedness = Handedness.RIGHT,
        observed_at: datetime | None = None,
        observed_monotonic: float | None = None,
        device_session_id: UUID | None = None,
        raw_payload: str | None = None,
    ) -> Shot:
        nonlocal counter
        counter += 1
        # A shot needs ball or club data, so an unspecified shot gets ball data:
        # that is what a launch monitor always reports and a club is optional.
        if ball is None and club_delivery is None:
            ball = launch()
        return Shot(
            shot_id=uuid4(),
            source=ShotSource(
                kind="gspro_connect",
                device_session_id=device_session_id or session,
                observed_at=observed_at or EPOCH + timedelta(minutes=counter),
                observed_monotonic=(
                    float(counter) if observed_monotonic is None else observed_monotonic
                ),
                device_id="ProTee VX",
                device_sequence=counter,
                reported_units="Yards",
                api_version="1",
                raw_payload=raw_payload,
            ),
            ball=ball,
            club_delivery=club_delivery,
            handedness=handedness,
            club=None if club is None else Club(kind=club, vendor_code=vendor_code),
        )

    return build


@pytest.fixture
def shots_named(make_shot: ShotBuilder) -> Callable[..., list[Shot]]:
    """Several shots with one metric set to given values, for aggregate tests."""

    def build(club: ClubKind, *speeds: float) -> list[Shot]:
        return [
            make_shot(club=club, ball=launch(speed=speed, carry=None))
            for speed in speeds
        ]

    return build
