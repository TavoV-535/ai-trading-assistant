"""
Shared indicator infrastructure — used by every plugin under
``plugins/indicators/`` so calculations are never duplicated across plugins
(PROJECT.md's Indicator System explicitly calls this out: "No duplicate
calculations"). This package is core, not a plugin — plugins import from it,
it never imports plugins.

- ``app.indicators.bar`` — ``Bar``, ``SymbolWindow`` (bounded rolling
  per-symbol OHLCV history) and ``bar_from_event`` (builds a ``Bar`` from a
  ``MarketDataUpdated`` event, degrading gracefully to tick-only data).
- ``app.indicators.math`` — pure, stateless calculation functions. Every one
  takes plain lists and returns ``None`` when there isn't enough history yet
  rather than raising, so a plugin's ``_on_market_data`` handler can just
  check for ``None`` the same way ``EMAPlugin`` checks ``state.updates < 2``.
"""
from app.indicators.bar import Bar, SymbolWindow, bar_from_event

__all__ = ["Bar", "SymbolWindow", "bar_from_event"]
