"""
ReplayProviderPlugin — the reference market data provider.

Two data sources in one plugin, both honestly labeled as development/
replay data, never real market data:

1. **CSV replay** — if ``plugin_config["data_dir"]`` is set and the
   directory exists, ``{data_dir}/{SYMBOL}.csv`` (columns: ``open``,
   ``high``, ``low``, ``close``, optional ``volume``) is parsed once and
   replayed bar-by-bar, looping back to the start when exhausted — a
   "replay engine," per PROJECT.md's Market Data Abstraction Layer
   requirement.
2. **Synthetic random walk** — for any symbol without a CSV file (or when
   no ``data_dir`` is configured at all), a deterministic (seeded) random
   walk generates the next bar on every call, so the platform has
   something to scan and reason about with zero setup.

This is the only provider that can be built honestly in this environment
— there's no real Polygon/Alpaca/Finnhub credential or network access
here. Live broker/vendor integrations are future provider plugins
implementing this exact same ``MarketDataProviderPlugin`` contract, with
zero changes anywhere else in the system (that's the whole point of the
Market Data Abstraction Layer — see ``app/marketdata/``).
"""
from __future__ import annotations

import csv
import random
import zlib
from pathlib import Path
from typing import Any

from app.indicators.bar import Bar
from app.logging import get_logger
from app.marketdata.provider import MarketDataProviderPlugin
from app.plugins.base import PluginHealth, PluginPermission

log = get_logger(__name__)


class _CsvSeries:
    """Parsed rows for one symbol's CSV replay file, plus a cursor that
    loops back to the start once exhausted."""

    __slots__ = ("bars", "cursor")

    def __init__(self, bars: list[Bar]) -> None:
        self.bars = bars
        self.cursor = 0

    def next_bar(self) -> Bar:
        bar = self.bars[self.cursor % len(self.bars)]
        self.cursor += 1
        return bar


class _SyntheticWalk:
    """Deterministic (seeded) per-symbol random walk — fabricated data,
    clearly not real market data, used only when no CSV replay data
    exists for a symbol."""

    __slots__ = ("rng", "price", "volatility")

    def __init__(self, seed: int, start_price: float, volatility: float) -> None:
        self.rng = random.Random(seed)
        self.price = start_price
        self.volatility = volatility

    def next_bar(self) -> Bar:
        pct_change = self.rng.gauss(0.0, self.volatility)
        open_price = self.price
        close_price = max(0.01, open_price * (1.0 + pct_change))
        wobble = abs(self.rng.gauss(0.0, self.volatility / 2))
        high = max(open_price, close_price) * (1.0 + wobble)
        low = min(open_price, close_price) * (1.0 - wobble)
        volume = abs(self.rng.gauss(1_000_000, 250_000))
        self.price = close_price
        return Bar(open=open_price, high=high, low=max(0.01, low), close=close_price, volume=volume)


def _stable_seed(*parts: str) -> int:
    """A hash that's stable across processes/runs — unlike the builtin
    ``hash()``, which is randomized per-process by default (PYTHONHASHSEED),
    breaking the "deterministic (seeded)" guarantee this provider promises."""
    return zlib.crc32(":".join(parts).encode("utf-8"))


class ReplayProviderPlugin(MarketDataProviderPlugin):
    """CSV replay with a deterministic synthetic-random-walk fallback —
    see module docstring."""

    name = "ReplayProvider"
    version = "0.1.0"
    category = "market_data"
    provider_name = "replay"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        cfg = context.plugin_config
        data_dir = cfg.get("data_dir")
        self._data_dir: Path | None = Path(data_dir) if data_dir else None
        self._seed = int(cfg.get("seed", 1337))
        self._start_price = float(cfg.get("start_price", 100.0))
        self._volatility = float(cfg.get("volatility", 0.015))
        # Keyed by (symbol, timeframe) so different timeframes for the same
        # symbol advance independently, the same way a real feed would.
        # A cached ``None`` means "checked, no CSV file for this key" so we
        # don't re-stat the filesystem on every fetch.
        self._csv_series: dict[tuple[str, str], _CsvSeries | None] = {}
        self._synthetic: dict[tuple[str, str], _SyntheticWalk] = {}
        self._fetch_count = 0

    async def initialize(self) -> None:
        log.info(
            "replay_provider_initialized",
            data_dir=str(self._data_dir) if self._data_dir else None,
            seed=self._seed,
        )

    async def shutdown(self) -> None:
        log.info("replay_provider_shutdown", fetches=self._fetch_count)

    async def health(self) -> PluginHealth:
        return PluginHealth(status="healthy", detail=f"{self._fetch_count} fetch(es)")

    def config(self) -> dict[str, Any]:
        return {
            "data_dir": str(self._data_dir) if self._data_dir else None,
            "seed": self._seed,
            "start_price": self._start_price,
            "volatility": self._volatility,
        }

    def permissions(self) -> list[str]:
        return [PluginPermission.MARKET_DATA_READ]

    async def fetch(self, symbols: list[str], timeframe: str) -> dict[str, Bar]:
        self._fetch_count += 1
        result: dict[str, Bar] = {}
        for symbol in symbols:
            key = (symbol, timeframe)

            if key not in self._csv_series:
                self._csv_series[key] = self._load_csv_series(symbol)
            series = self._csv_series[key]
            if series is not None:
                result[symbol] = series.next_bar()
                continue

            walk = self._synthetic.get(key)
            if walk is None:
                walk = _SyntheticWalk(
                    seed=_stable_seed(str(self._seed), symbol, timeframe),
                    start_price=self._start_price,
                    volatility=self._volatility,
                )
                self._synthetic[key] = walk
            result[symbol] = walk.next_bar()

        return result

    def _load_csv_series(self, symbol: str) -> _CsvSeries | None:
        if self._data_dir is None:
            return None
        csv_path = self._data_dir / f"{symbol}.csv"
        if not csv_path.exists():
            return None

        bars: list[Bar] = []
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    bars.append(
                        Bar(
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=float(row["volume"]) if row.get("volume") else 0.0,
                        )
                    )
        except Exception:
            log.exception("replay_provider_csv_parse_failed", symbol=symbol, path=str(csv_path))
            return None

        if not bars:
            log.warning("replay_provider_csv_empty", symbol=symbol, path=str(csv_path))
            return None

        log.info("replay_provider_csv_loaded", symbol=symbol, bars=len(bars), path=str(csv_path))
        return _CsvSeries(bars)
