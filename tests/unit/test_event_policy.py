"""The delivery table is the policy. If a fact becomes droppable, it is here."""

from caddiej.core.delivery import Delivery
from caddiej.core.events import Event
from caddiej.domain.event_policy import (
    EVENT_DELIVERY,
    QUEUE_SIZES,
    SUBSCRIBER_DELIVERY,
    delivery_for,
    queue_size_for,
)
from caddiej.domain.events import (
    AnalysisCompleted,
    BallDetected,
    ClubSelected,
    ShotCompleted,
    ShotIngested,
    SwingClipReady,
)
from caddiej.domain.scoring import HoleCompleted


def test_shot_facts_are_critical_by_default() -> None:
    assert EVENT_DELIVERY[ShotIngested] is Delivery.CRITICAL
    assert EVENT_DELIVERY[ShotCompleted] is Delivery.CRITICAL
    assert delivery_for("play", ShotIngested) is Delivery.CRITICAL
    assert delivery_for("history", ShotCompleted) is Delivery.CRITICAL


def test_enrichment_subscribers_may_drop_stale_shot_work() -> None:
    assert SUBSCRIBER_DELIVERY[("coaching", ShotCompleted)] is Delivery.DROP
    assert SUBSCRIBER_DELIVERY[("video", ShotCompleted)] is Delivery.DROP
    assert delivery_for("coaching", ShotCompleted) is Delivery.DROP
    assert delivery_for("video", ShotCompleted) is Delivery.DROP
    assert delivery_for("coaching", SwingClipReady) is Delivery.DROP


def test_state_notifications_coalesce() -> None:
    assert EVENT_DELIVERY[ClubSelected] is Delivery.COALESCE
    assert EVENT_DELIVERY[BallDetected] is Delivery.COALESCE
    assert delivery_for("play", ClubSelected) is Delivery.COALESCE


def test_an_unlisted_event_is_critical() -> None:
    class Mystery(Event):
        pass

    assert delivery_for("play", Mystery) is Delivery.CRITICAL


def test_hole_completed_is_a_fact_except_for_effects() -> None:
    assert delivery_for("history", HoleCompleted) is Delivery.CRITICAL
    assert delivery_for("effects", HoleCompleted) is Delivery.DROP


def test_play_treats_coaching_lines_as_replaceable_state() -> None:
    assert delivery_for("play", AnalysisCompleted) is Delivery.COALESCE


def test_named_subscribers_have_explicit_queue_bounds() -> None:
    assert QUEUE_SIZES["history"] == 128
    assert QUEUE_SIZES["play"] == 64
    assert queue_size_for("history") == 128
    assert queue_size_for("unknown") >= 1
