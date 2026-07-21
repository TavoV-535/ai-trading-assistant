"""
The Event Bus.

Everything communicates using events. Nothing communicates directly.

Plugins (and core systems) publish :class:`~app.event_bus.events.Event`
instances; anyone interested subscribes by event class. Each subscriber gets
its own bounded queue and background worker task, so one slow or broken
handler cannot block delivery to anyone else.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.event_bus.events import Event
from app.logging import get_logger

Handler = Callable[[Event], Awaitable[None]]

log = get_logger(__name__)


@dataclass
class Subscription:
    """Handle returned by :meth:`EventBus.subscribe`. Call ``.unsubscribe()`` to stop listening."""

    _bus: "EventBus"
    _event_name: str | None
    _subscriber: "_Subscriber"
    _unsubscribed: bool = field(default=False, init=False)

    def unsubscribe(self) -> None:
        if self._unsubscribed:
            return
        self._unsubscribed = True
        self._bus._remove_subscriber(self._event_name, self._subscriber)


@dataclass
class _Subscriber:
    name: str
    handler: Handler
    queue: "asyncio.Queue[Event]"
    task: asyncio.Task[None] | None = None


class EventBus:
    """Async pub/sub event bus.

    Parameters
    ----------
    queue_max_size:
        Max events buffered per subscriber before ``publish`` starts
        blocking (backpressure). Comes from ``config.event_bus.queue_max_size``.
    slow_handler_threshold:
        Handlers that take longer than this (seconds) to process a single
        event are logged as slow, so misbehaving plugins are visible without
        crashing the bus.
    """

    def __init__(self, queue_max_size: int = 1000, slow_handler_threshold: float = 2.0) -> None:
        self._queue_max_size = queue_max_size
        self._slow_handler_threshold = slow_handler_threshold
        self._subscribers: dict[str, list[_Subscriber]] = defaultdict(list)
        self._global_subscribers: list[_Subscriber] = []
        self._started = False

    @classmethod
    def from_settings(cls, settings: Any) -> "EventBus":
        return cls(
            queue_max_size=settings.event_bus.queue_max_size,
            slow_handler_threshold=settings.event_bus.slow_handler_threshold,
        )

    # ---------------------------------------------------------------- subscribe

    def subscribe(self, event_type: type[Event], handler: Handler, *, name: str | None = None) -> Subscription:
        """Subscribe ``handler`` to every event of ``event_type`` (exact class match)."""
        subscriber = self._make_subscriber(name or handler.__qualname__, handler)
        self._subscribers[event_type.__name__].append(subscriber)
        log.debug("event_bus_subscribed", event_type=event_type.__name__, subscriber=subscriber.name)
        return Subscription(self, event_type.__name__, subscriber)

    def subscribe_all(self, handler: Handler, *, name: str | None = None) -> Subscription:
        """Subscribe ``handler`` to every event published on the bus (audit/logging use case)."""
        subscriber = self._make_subscriber(name or handler.__qualname__, handler)
        self._global_subscribers.append(subscriber)
        log.debug("event_bus_subscribed_all", subscriber=subscriber.name)
        return Subscription(self, None, subscriber)

    def _make_subscriber(self, name: str, handler: Handler) -> _Subscriber:
        queue: "asyncio.Queue[Event]" = asyncio.Queue(maxsize=self._queue_max_size)
        subscriber = _Subscriber(name=name, handler=handler, queue=queue)
        subscriber.task = asyncio.ensure_future(self._consume(subscriber))
        return subscriber

    def _remove_subscriber(self, event_name: str | None, subscriber: _Subscriber) -> None:
        if event_name is None:
            if subscriber in self._global_subscribers:
                self._global_subscribers.remove(subscriber)
        else:
            bucket = self._subscribers.get(event_name, [])
            if subscriber in bucket:
                bucket.remove(subscriber)
        if subscriber.task and not subscriber.task.done():
            subscriber.task.cancel()

    # ---------------------------------------------------------------- publish

    async def publish(self, event: Event) -> None:
        """Publish ``event`` to every matching subscriber.

        Enqueues onto each subscriber's own queue. If a subscriber's queue is
        full, this awaits (bounded backpressure) rather than dropping events
        — a stuck handler should be visible as a growing backlog, not silent
        data loss.
        """
        targets = list(self._subscribers.get(event.event_type, [])) + list(self._global_subscribers)
        if not targets:
            log.debug("event_published_no_subscribers", event_type=event.event_type, event_id=str(event.event_id))
            return
        for subscriber in targets:
            if subscriber.queue.full():
                log.warning(
                    "event_bus_backpressure",
                    event_type=event.event_type,
                    subscriber=subscriber.name,
                    queue_size=subscriber.queue.qsize(),
                )
            await subscriber.queue.put(event)

    # ---------------------------------------------------------------- worker loop

    async def _consume(self, subscriber: _Subscriber) -> None:
        while True:
            event = await subscriber.queue.get()
            start = time.monotonic()
            try:
                await subscriber.handler(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "event_handler_error",
                    subscriber=subscriber.name,
                    event_type=event.event_type,
                    event_id=str(event.event_id),
                )
            finally:
                elapsed = time.monotonic() - start
                if elapsed > self._slow_handler_threshold:
                    log.warning(
                        "event_handler_slow",
                        subscriber=subscriber.name,
                        event_type=event.event_type,
                        elapsed_seconds=round(elapsed, 3),
                    )
                subscriber.queue.task_done()

    # ---------------------------------------------------------------- lifecycle

    def all_subscribers(self) -> list[_Subscriber]:
        result = list(self._global_subscribers)
        for bucket in self._subscribers.values():
            result.extend(bucket)
        return result

    async def shutdown(self, *, drain: bool = True, timeout: float = 5.0) -> None:
        """Stop all subscriber workers. If ``drain``, wait for queues to empty first."""
        subscribers = self.all_subscribers()
        if drain:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(s.queue.join() for s in subscribers)), timeout=timeout
                )
            except asyncio.TimeoutError:
                log.warning("event_bus_shutdown_drain_timeout", pending_subscribers=len(subscribers))
        for subscriber in subscribers:
            if subscriber.task and not subscriber.task.done():
                subscriber.task.cancel()
        await asyncio.gather(
            *(s.task for s in subscribers if s.task is not None), return_exceptions=True
        )
        self._subscribers.clear()
        self._global_subscribers.clear()
        log.info("event_bus_shutdown_complete")
