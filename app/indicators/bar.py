"""
Bounded rolling per-symbol bar history shared by every indicator plugin.

Every bar-based indicator (ATR, ADX, Bollinger, Supertrend, OBV, CCI,
Ichimoku, Donchian, Volume Profile) needs the same thing: a bounded window of
recent OHLCV bars per symbol. Without this module every one of those plugins
would reimplement its own deque-management and "how do I turn a
MarketDataUpdated tick into a bar" logic — exactly the duplicate-calculation
problem PROJECT.md's Indicator System warns against. Plugins hold one
``SymbolWindow`` per symbol and only ever call ``append`` and the ``closes``/
``highs``/``lows``/``volumes`` properties; the math itself lives in
``app.indicators.math``.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from app.event_bus.events import MarketDataUpdated

#: Default bounded window length. Deliberately generous relative to every
#: indicator's period in this milestone (the longest is Ichimoku's 52-period
#: senkou span B) so that indicators computed by recomputing over the whole
#: retained window (rather than maintained as continuous incremental state)
#: have enough warmup history for seed bias to be negligible.
DEFAULT_WINDOW = 300


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV bar. Immutable, like everything else that represents a fact
    that already happened."""

    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def bar_from_event(event: MarketDataUpdated) -> Bar:
    """Builds a :class:`Bar` from a ``MarketDataUpdated`` event.

    If the event carries real OHLC fields (a bar/candle feed), those are
    used directly. If it only carries ``price`` (a raw tick feed), the tick
    is treated as a degenerate bar where open == high == low == close ==
    price — bar-based indicators still work on tick data, just with less
    intrabar range information until a real bar-aggregating feed plugin
    exists.
    """
    close = event.close if event.close is not None else event.price
    return Bar(
        open=event.open if event.open is not None else event.price,
        high=event.high if event.high is not None else event.price,
        low=event.low if event.low is not None else event.price,
        close=close,
        volume=float(event.volume) if event.volume is not None else 0.0,
    )


class SymbolWindow:
    """A bounded rolling history of bars for one symbol."""

    __slots__ = ("_bars", "maxlen")

    def __init__(self, maxlen: int = DEFAULT_WINDOW) -> None:
        self.maxlen = maxlen
        self._bars: deque[Bar] = deque(maxlen=maxlen)

    def append(self, bar: Bar) -> None:
        self._bars.append(bar)

    def __len__(self) -> int:
        return len(self._bars)

    @property
    def bars(self) -> list[Bar]:
        return list(self._bars)

    @property
    def closes(self) -> list[float]:
        return [b.close for b in self._bars]

    @property
    def highs(self) -> list[float]:
        return [b.high for b in self._bars]

    @property
    def lows(self) -> list[float]:
        return [b.low for b in self._bars]

    @property
    def volumes(self) -> list[float]:
        return [b.volume for b in self._bars]

    @property
    def has_volume(self) -> bool:
        """Whether any bar so far has carried real (non-zero) volume — used
        by volume-based plugins (OBV, Volume Profile, VWAP) to report a
        degraded health status instead of silently publishing all-zero
        evidence forever."""
        return any(v for v in self.volumes)
