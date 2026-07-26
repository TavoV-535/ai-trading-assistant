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
