"""
CoreWatchlistScanner — the reference scanner plugin.

Proves the Scanner Engine pipeline end to end, the same way ``EMA`` proved
the indicator pipeline, ``Ping`` proved the Discord command pipeline, and
``Momentum Breakout`` proved the Strategy Engine: entirely configuration-
driven (see ``config.yaml``), zero scanner-specific Python logic beyond
what ``ScannerPlugin`` already provides generically.

Adding a second, differently-configured watchlist (a different symbol
list, timeframe set, or interval) is another ``plugins/scanners/<name>/``
folder like this one — never a change to this file or to
``app/scanner/plugin.py``.
"""
from __future__ import annotations

from app.scanner.plugin import ScannerPlugin


class CoreWatchlistScanner(ScannerPlugin):
    """Continuously scans a configurable watchlist across configurable
    timeframes — see ``config.yaml`` alongside this file."""

    name = "CoreWatchlistScanner"
    version = "0.1.0"
    category = "scanners"
