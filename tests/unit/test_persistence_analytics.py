"""The arithmetic, against numbers worked out by hand.

No database here at all: analytics is a pure function of records, which is what
lets these tests state the expected mean rather than trusting SQLite and Python
to round the same way.
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from caddiej.domain.shot import BallLaunch, ClubDelivery, ClubKind, Shot
from caddiej.persistence.analytics import (
    METRICS,
    aggregate_by_club,
    dispersion_samples,
    summarise_metrics,
    summarise_session,
)
from caddiej.persistence.records import SessionRecord, StoredShot
from conftest import EPOCH, ShotBuilder, delivery, launch

SESSION = SessionRecord(session_id=uuid4(), started_at=EPOCH, label="Wednesday range")


def stored(shots: list[Shot]) -> list[StoredShot]:
    return [
        StoredShot(
            shot=shot,
            session_id=SESSION.session_id,
            ordinal=index + 1,
            recorded_at=EPOCH + timedelta(seconds=index),
        )
        for index, shot in enumerate(shots)
    ]


def speeds(make_shot: ShotBuilder, *values: float, club: ClubKind = ClubKind.DRIVER):
    return [
        make_shot(club=club, ball=launch(speed=value, carry=None)) for value in values
    ]


# --- one metric across a set of shots -------------------------------------


def test_the_average_is_the_average(make_shot: ShotBuilder) -> None:
    summary = summarise_metrics(speeds(make_shot, 100.0, 110.0, 120.0))["ball_speed"]

    assert summary.mean == pytest.approx(110.0)
    assert summary.minimum == 100.0
    assert summary.maximum == 120.0
    assert summary.count == 3
    assert summary.unit == "mph"


def test_the_median_is_the_middle_shot_not_the_average(
    make_shot: ShotBuilder,
) -> None:
    # One duffed shot drags a mean a long way. The median is why it is here.
    summary = summarise_metrics(speeds(make_shot, 148.0, 150.0, 152.0, 20.0))[
        "ball_speed"
    ]

    assert summary.median == pytest.approx(149.0)
    assert summary.mean == pytest.approx(117.5)


def test_the_spread_is_the_distance_between_the_extremes(
    make_shot: ShotBuilder,
) -> None:
    summary = summarise_metrics(speeds(make_shot, 140.0, 155.0))["ball_speed"]

    assert summary.spread == pytest.approx(15.0)


def test_the_standard_deviation_is_the_sample_one(make_shot: ShotBuilder) -> None:
    # An evening's shots are a sample of how somebody swings, not the whole of
    # it. For 100/110/120 the sample deviation is 10 and the population one is
    # 8.165, so this distinguishes them.
    summary = summarise_metrics(speeds(make_shot, 100.0, 110.0, 120.0))["ball_speed"]

    assert summary.standard_deviation == pytest.approx(10.0)


def test_one_shot_has_no_standard_deviation(make_shot: ShotBuilder) -> None:
    # A zero here would read as perfect consistency, which is the opposite of
    # what one shot tells you.
    summary = summarise_metrics(speeds(make_shot, 148.2))["ball_speed"]

    assert summary.standard_deviation is None
    assert summary.count == 1


def test_a_metric_no_shot_carried_is_absent_rather_than_zero(
    make_shot: ShotBuilder,
) -> None:
    # A device that does not measure spin must not appear to have measured zero.
    summaries = summarise_metrics(
        [make_shot(ball=launch(total_spin=None, side_spin=None, carry=None))]
    )

    assert "total_spin" not in summaries
    assert "side_spin" not in summaries
    assert "carry" not in summaries
    assert "ball_speed" in summaries


def test_a_metric_counts_only_the_shots_that_carried_it(
    make_shot: ShotBuilder,
) -> None:
    # A device that reports carry for drives but not putts would otherwise look
    # like it had hit some very soft drives.
    shots = [
        make_shot(ball=launch(carry=268.0)),
        make_shot(ball=launch(carry=None)),
    ]

    summaries = summarise_metrics(shots)

    assert summaries["ball_speed"].count == 2
    assert summaries["carry"].count == 1
    assert summaries["carry"].mean == pytest.approx(268.0)


def test_a_shot_with_no_ball_data_contributes_no_ball_metrics(
    make_shot: ShotBuilder,
) -> None:
    summaries = summarise_metrics([make_shot(ball=None, club_delivery=delivery())])

    assert "ball_speed" not in summaries
    assert "club_speed" in summaries


def test_face_to_path_is_aggregated_even_though_nothing_measures_it(
    make_shot: ShotBuilder,
) -> None:
    # The domain derives it from face and path, and it is the number that
    # explains which way the ball curved.
    shots = [
        make_shot(
            ball=launch(),
            club_delivery=ClubDelivery(path_degrees=-2.0, face_to_target_degrees=1.0),
        )
    ]

    assert summarise_metrics(shots)["face_to_path"].mean == pytest.approx(3.0)


def test_a_club_block_without_a_face_gives_no_face_to_path(
    make_shot: ShotBuilder,
) -> None:
    shots = [make_shot(ball=launch(), club_delivery=ClubDelivery(path_degrees=-2.0))]

    assert "face_to_path" not in summarise_metrics(shots)


def test_no_shots_means_no_metrics() -> None:
    assert summarise_metrics([]) == {}


def test_the_metric_order_is_fixed(make_shot: ShotBuilder) -> None:
    # A summary that reorders itself between runs cannot be diffed.
    shots = [make_shot(ball=launch(), club_delivery=delivery())]

    order = list(summarise_metrics(shots))

    assert order == [metric.name for metric in METRICS if metric.name in set(order)]


# --- per club -------------------------------------------------------------


def test_shots_are_aggregated_per_club(make_shot: ShotBuilder) -> None:
    shots = stored(
        speeds(make_shot, 148.0, 152.0, club=ClubKind.DRIVER)
        + speeds(make_shot, 118.0, club=ClubKind.IRON)
    )

    by_club = {aggregate.club: aggregate for aggregate in aggregate_by_club(shots)}

    assert by_club[ClubKind.DRIVER].shots == 2
    assert by_club[ClubKind.DRIVER].metrics["ball_speed"].mean == pytest.approx(150.0)
    assert by_club[ClubKind.IRON].metrics["ball_speed"].mean == pytest.approx(118.0)


def test_the_busiest_club_comes_first(make_shot: ShotBuilder) -> None:
    shots = stored(
        speeds(make_shot, 100.0, club=ClubKind.DRIVER)
        + speeds(make_shot, 110.0, 112.0, 114.0, club=ClubKind.IRON)
    )

    assert [aggregate.club for aggregate in aggregate_by_club(shots)] == [
        ClubKind.IRON,
        ClubKind.DRIVER,
    ]


def test_clubs_with_the_same_number_of_shots_are_ordered_by_name(
    make_shot: ShotBuilder,
) -> None:
    # Otherwise the order would depend on which club happened to be hit first.
    shots = stored(
        speeds(make_shot, 100.0, club=ClubKind.WEDGE)
        + speeds(make_shot, 110.0, club=ClubKind.IRON)
    )

    assert [aggregate.club for aggregate in aggregate_by_club(shots)] == [
        ClubKind.IRON,
        ClubKind.WEDGE,
    ]


def test_a_shot_with_no_club_counts_as_unknown(make_shot: ShotBuilder) -> None:
    # Rather than being dropped: a shot hit before GSPro named a club is still a
    # shot somebody hit.
    shots = stored([make_shot(club=None)])

    aggregates = aggregate_by_club(shots)

    assert len(aggregates) == 1
    assert aggregates[0].club is ClubKind.UNKNOWN
    assert aggregates[0].shots == 1


# --- dispersion -----------------------------------------------------------


def test_dispersion_returns_what_was_measured_for_every_ball(
    make_shot: ShotBuilder,
) -> None:
    shots = stored(
        [
            make_shot(
                club=ClubKind.DRIVER,
                ball=BallLaunch(
                    speed_mph=148.0,
                    launch_angle_degrees=12.0,
                    launch_direction_degrees=-1.5,
                    carry_yards=268.0,
                    total_spin_rpm=2300.0,
                    side_spin_rpm=-320.0,
                    spin_axis_degrees=-8.0,
                ),
            )
        ]
    )

    sample = dispersion_samples(shots)[0]

    assert sample.launch_direction_degrees == -1.5
    assert sample.carry_yards == 268.0
    assert sample.total_spin_rpm == 2300.0
    assert sample.side_spin_rpm == -320.0
    assert sample.spin_axis_degrees == -8.0
    assert sample.club is ClubKind.DRIVER
    assert sample.ordinal == 1


def test_dispersion_skips_a_shot_with_no_ball_data(make_shot: ShotBuilder) -> None:
    shots = stored(
        [make_shot(ball=None, club_delivery=delivery()), make_shot(ball=launch())]
    )

    assert len(dispersion_samples(shots)) == 1


def test_dispersion_keeps_a_shot_whose_carry_was_not_measured(
    make_shot: ShotBuilder,
) -> None:
    # Direction alone is still dispersion; most devices leave carry to the
    # simulator to compute.
    shots = stored([make_shot(ball=launch(carry=None))])

    sample = dispersion_samples(shots)[0]

    assert sample.carry_yards is None
    assert sample.launch_direction_degrees == 1.5


def test_dispersion_reports_spin_in_whichever_form_the_device_used(
    make_shot: ShotBuilder,
) -> None:
    # A device that sends a total and an axis rather than a side-spin figure
    # has said everything needed; the sample must not swallow half of it.
    shots = stored(
        [
            make_shot(
                ball=BallLaunch(
                    speed_mph=118.0,
                    launch_angle_degrees=18.0,
                    launch_direction_degrees=0.4,
                    total_spin_rpm=6200.0,
                    spin_axis_degrees=3.5,
                )
            )
        ]
    )

    sample = dispersion_samples(shots)[0]

    assert sample.total_spin_rpm == 6200.0
    assert sample.spin_axis_degrees == 3.5
    assert sample.side_spin_rpm is None


# --- a whole session ------------------------------------------------------


def test_a_session_summary_spans_its_shots(make_shot: ShotBuilder) -> None:
    shots = stored(speeds(make_shot, 148.0, 150.0, 152.0))

    summary = summarise_session(SESSION, shots)

    assert summary.session_id == SESSION.session_id
    assert summary.label == "Wednesday range"
    assert summary.shots == 3
    assert summary.overall["ball_speed"].mean == pytest.approx(150.0)
    assert len(summary.by_club) == 1
    assert len(summary.dispersion) == 3


def test_a_session_summary_reports_when_the_first_and_last_shot_landed(
    make_shot: ShotBuilder,
) -> None:
    first = EPOCH + timedelta(minutes=5)
    last = EPOCH + timedelta(minutes=95)
    shots = stored([make_shot(observed_at=last), make_shot(observed_at=first)])

    summary = summarise_session(SESSION, shots)

    assert summary.first_shot_at == first
    assert summary.last_shot_at == last


def test_a_session_with_no_shots_summarises_to_nothing() -> None:
    # Which happens every time CaddieJ is started and stopped without a swing.
    summary = summarise_session(SESSION, [])

    assert summary.shots == 0
    assert summary.overall == {}
    assert summary.by_club == ()
    assert summary.dispersion == ()
    assert summary.first_shot_at is None


def test_summarising_the_same_shots_twice_gives_the_same_answer(
    make_shot: ShotBuilder,
) -> None:
    shots = stored(
        speeds(make_shot, 148.0, 152.0, club=ClubKind.DRIVER)
        + speeds(make_shot, 118.0, club=ClubKind.IRON)
    )

    assert summarise_session(SESSION, shots) == summarise_session(SESSION, shots)
