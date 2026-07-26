from __future__ import annotations

import asyncio

import pytest

from app.event_bus import EventBus, MarketDataUpdated, PriceMoved


async def test_publish_delivers_to_matching_subscriber(event_bus: EventBus):
    received = []

    async def handler(event):
        received.append(event)

    event_bus.subscribe(MarketDataUpdated, handler)
    await event_bus.publish(MarketDataUpdated(symbol="NVDA", price=100.0))
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0].symbol == "NVDA"
    await event_bus.shutdown()


async def test_publish_does_not_deliver_to_other_event_types(event_bus: EventBus):
    received = []

    async def handler(event):
        received.append(event)

    event_bus.subscribe(MarketDataUpdated, handler)
    await event_bus.publish(PriceMoved(symbol="NVDA", price=101.0, change_percent=1.0, direction="up"))
    await asyncio.sleep(0.05)

    assert received == []
    await event_bus.shutdown()


async def test_subscribe_all_receives_every_event(event_bus: EventBus):
    seen_types = []

    async def audit(event):
        seen_types.append(event.event_type)

    event_bus.subscribe_all(audit)
    await event_bus.publish(MarketDataUpdated(symbol="AAPL", price=200.0))
    await event_bus.publish(PriceMoved(symbol="AAPL", price=201.0, change_percent=0.5, direction="up"))
    await asyncio.sleep(0.05)

    assert seen_types == ["MarketDataUpdated", "PriceMoved"]
    await event_bus.shutdown()


async def test_unsubscribe_stops_delivery(event_bus: EventBus):
    received = []

    async def handler(event):
        received.append(event)

    sub = event_bus.subscribe(MarketDataUpdated, handler)
    sub.unsubscribe()
    await event_bus.publish(MarketDataUpdated(symbol="NVDA", price=100.0))
    await asyncio.sleep(0.05)

    assert received == []
    await event_bus.shutdown()


async def test_handler_exception_does_not_crash_bus_or_other_subscribers(event_bus: EventBus):
    received = []

    async def broken_handler(event):
        raise RuntimeError("boom")

    async def good_handler(event):
        received.append(event)

    event_bus.subscribe(MarketDataUpdated, broken_handler)
    event_bus.subscribe(MarketDataUpdated, good_handler)

    await event_bus.publish(MarketDataUpdated(symbol="NVDA", price=100.0))
    await asyncio.sleep(0.05)

    assert len(received) == 1  # good handler still ran despite the broken one
    await event_bus.shutdown()


async def test_events_are_immutable():
    event = MarketDataUpdated(symbol="NVDA", price=100.0)
    with pytest.raises(Exception):
        event.price = 200.0  # frozen model


# ---------------------------------------------------------------- drain()


async def test_drain_returns_true_immediately_with_no_subscribers(event_bus: EventBus):
    assert await event_bus.drain() is True
    await event_bus.shutdown()


async def test_drain_waits_for_a_single_hop_to_finish(event_bus: EventBus):
    processed = []

    async def slow_handler(event):
        await asyncio.sleep(0.05)
        processed.append(event)

    event_bus.subscribe(MarketDataUpdated, slow_handler)
    await event_bus.publish(MarketDataUpdated(symbol="NVDA", price=100.0))

    assert await event_bus.drain() is True
    assert len(processed) == 1  # drain() only returns once the handler actually ran
    await event_bus.shutdown()


async def test_drain_waits_out_a_multi_hop_cascade(event_bus: EventBus):
    """A handler that itself publishes another event (which another handler
    reacts to) must be fully settled before drain() returns -- the same
    guarantee the Simulation Engine relies on to advance bar-by-bar only
    once the entire downstream reaction (indicators -> aggregator ->
    strategy engine -> ...) has actually finished."""
    hops: list[str] = []

    async def first_hop(event):
        await asyncio.sleep(0.02)
        hops.append("first")
        await event_bus.publish(PriceMoved(symbol="NVDA", price=101.0, change_percent=1.0, direction="up"))

    async def second_hop(event):
        await asyncio.sleep(0.02)
        hops.append("second")

    event_bus.subscribe(MarketDataUpdated, first_hop)
    event_bus.subscribe(PriceMoved, second_hop)

    await event_bus.publish(MarketDataUpdated(symbol="NVDA", price=100.0))
    assert await event_bus.drain() is True

    assert hops == ["first", "second"]
    await event_bus.shutdown()


async def test_drain_times_out_on_a_handler_that_never_finishes(event_bus: EventBus):
    async def stuck_handler(event):
        await asyncio.sleep(10)

    event_bus.subscribe(MarketDataUpdated, stuck_handler)
    await event_bus.publish(MarketDataUpdated(symbol="NVDA", price=100.0))

    assert await event_bus.drain(timeout=0.05) is False  # logged, never raised
    await event_bus.shutdown()


async def test_shutdown_drain_true_uses_the_same_drain_primitive(event_bus: EventBus):
    processed = []

    async def handler(event):
        await asyncio.sleep(0.02)
        processed.append(event)

    event_bus.subscribe(MarketDataUpdated, handler)
    await event_bus.publish(MarketDataUpdated(symbol="NVDA", price=100.0))

    await event_bus.shutdown(drain=True)
    assert len(processed) == 1


# ---------------------------------------------------------------- Milestone 12: lightweight performance metrics


async def test_statistics_tracks_throughput_and_processing_time(event_bus: EventBus):
    async def slow_handler(event):
        await asyncio.sleep(0.02)

    event_bus.subscribe(MarketDataUpdated, slow_handler, name="slow")
    for _ in range(3):
        await event_bus.publish(MarketDataUpdated(symbol="NVDA", price=100.0))
    await event_bus.drain()

    stats = event_bus.statistics()
    assert stats["total_events_published"] == 3
    assert stats["events_by_type"] == {"MarketDataUpdated": 3}
    assert stats["pending_count"] == 0
    slow_stats = stats["subscribers"]["slow"]
    assert slow_stats["processed_count"] == 3
    assert slow_stats["average_processing_time_ms"] > 0
    assert slow_stats["queue_depth"] == 0
    await event_bus.shutdown()


async def test_statistics_reports_queue_depth_while_backlog_exists(event_bus: EventBus):
    release = asyncio.Event()

    async def blocked_handler(event):
        await release.wait()

    event_bus.subscribe(MarketDataUpdated, blocked_handler, name="blocked")
    for _ in range(4):
        await event_bus.publish(MarketDataUpdated(symbol="NVDA", price=100.0))
    await asyncio.sleep(0.02)  # let the first item be picked up, the rest queue

    stats = event_bus.statistics()
    assert stats["subscribers"]["blocked"]["queue_depth"] >= 1

    release.set()
    await event_bus.drain()
    await event_bus.shutdown()


async def test_diagnostics_reports_subscriber_and_config_shape(event_bus: EventBus):
    async def handler(event):
        pass

    event_bus.subscribe(MarketDataUpdated, handler, name="h1")
    diagnostics = event_bus.diagnostics()
    assert diagnostics["subscriber_count"] == 1
    assert diagnostics["event_types_subscribed"] == ["MarketDataUpdated"]
    assert diagnostics["queue_max_size"] > 0
    await event_bus.shutdown()


async def test_health_is_degraded_when_a_queue_is_nearly_full():
    bus = EventBus(queue_max_size=2)

    async def stuck_handler(event):
        await asyncio.sleep(10)

    bus.subscribe(MarketDataUpdated, stuck_handler, name="stuck")
    await bus.publish(MarketDataUpdated(symbol="NVDA", price=100.0))
    await bus.publish(MarketDataUpdated(symbol="NVDA", price=100.0))

    health = await bus.health()
    assert health["status"] == "degraded"
    assert "stuck" in health["queues_near_full"]
    await bus.shutdown(drain=False)


async def test_health_is_healthy_with_no_backlog(event_bus: EventBus):
    health = await event_bus.health()
    assert health["status"] == "healthy"
    assert health["queues_near_full"] == []
    await event_bus.shutdown()
