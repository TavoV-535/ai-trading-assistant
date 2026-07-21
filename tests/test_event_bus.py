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
