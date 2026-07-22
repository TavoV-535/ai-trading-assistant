"""
Discovery + evidence-emission tests for every Milestone 3 indicator plugin
(everything added alongside EMA: SMA, VWAP, RSI, MACD, ATR, ADX, Bollinger,
Supertrend, OBV, CCI, Ichimoku, Donchian, Volume Profile).

Each test crafts synthetic ``MarketDataUpdated`` events specifically
designed to deterministically trigger (or, for the "no evidence yet" cases,
deliberately not trigger) that plugin's evidence rule — the same style as
``tests/test_ema_plugin.py``. This isn't testing the math twice (that's
``tests/test_indicators_math.py``'s job); it's testing that each plugin
wires the shared math into the event bus correctly: publishes
``IndicatorCalculated`` every update, publishes ``EvidenceProduced`` only on
the documented edge-trigger condition, and never crashes on insufficient
history.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.plugins import PluginRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]

ALL_NEW_INDICATOR_NAMES = [
    "SMA",
    "VWAP",
    "RSI",
    "MACD",
    "ATR",
    "ADX",
    "Bollinger",
    "Supertrend",
    "OBV",
    "CCI",
    "Ichimoku",
    "Donchian",
    "VolumeProfile",
]


async def _load_registry(event_bus, settings) -> PluginRegistry:
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)
    return registry


async def _collect(event_bus):
    indicators: list[IndicatorCalculated] = []
    evidence: list[EvidenceProduced] = []

    async def on_indicator(event: IndicatorCalculated) -> None:
        indicators.append(event)

    async def on_evidence(event: EvidenceProduced) -> None:
        evidence.append(event)

    event_bus.subscribe(IndicatorCalculated, on_indicator)
    event_bus.subscribe(EvidenceProduced, on_evidence)
    return indicators, evidence


async def _publish_bars(event_bus, symbol, bars, timeframe="1m"):
    for bar in bars:
        await event_bus.publish(
            MarketDataUpdated(
                symbol=symbol,
                price=bar.get("close", bar.get("price", 0.0)),
                open=bar.get("open"),
                high=bar.get("high"),
                low=bar.get("low"),
                close=bar.get("close"),
                volume=bar.get("volume"),
                timeframe=timeframe,
            )
        )
    await asyncio.sleep(0.15)


def _uptrend_bars(n: int, base: float = 100.0, spread: float = 10.0, volume: float = 1000.0):
    return [
        {
            "open": base + i,
            "high": base + i + spread,
            "low": base + i,
            "close": base + i + spread / 2,
            "volume": volume,
        }
        for i in range(n)
    ]


def _downtrend_bars(n: int, base: float = 300.0, spread: float = 10.0, volume: float = 1000.0):
    return [
        {
            "open": base - i,
            "high": base - i,
            "low": base - i - spread,
            "close": base - i - spread / 2,
            "volume": volume,
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------- discovery


async def test_all_new_indicator_plugins_discovered_and_healthy(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    for name in ALL_NEW_INDICATOR_NAMES:
        assert name in registry.plugins, f"{name} was not discovered"
    assert registry.failed == {}

    health = await registry.health_check_all()
    for name in ALL_NEW_INDICATOR_NAMES:
        assert health[name].status in ("healthy", "degraded")

    await registry.shutdown_all()


# ---------------------------------------------------------------- SMA


async def test_sma_publishes_indicator_and_bullish_cross_evidence(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    indicators, evidence = await _collect(event_bus)

    # flat long enough for the slow(50) SMA to fully warm up flat, then a
    # sharp sustained move so the fast(20) SMA overtakes the still-flat-
    # anchored slow SMA -> a clean, observable cross.
    prices = [100.0] * 60 + [100 + i * 3 for i in range(1, 31)]
    for p in prices:
        await event_bus.publish(MarketDataUpdated(symbol="AAPL", price=p, timeframe="1m"))
    await asyncio.sleep(0.15)

    sma_indicators = [e for e in indicators if e.indicator == "SMA"]
    sma_evidence = [e.evidence for e in evidence if e.evidence.source == "SMA"]
    assert len(sma_indicators) > 0
    assert any(ev.direction == "bullish" for ev in sma_evidence)

    await registry.shutdown_all()


# ---------------------------------------------------------------- RSI


async def test_rsi_publishes_overbought_evidence_on_strong_uptrend(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    indicators, evidence = await _collect(event_bus)

    # choppy warmup first so RSI's first valid readings sit near neutral
    # (giving a real "prev < 70" baseline to cross from), then a sustained
    # move that pushes RSI decisively through the overbought threshold.
    prices: list[float] = []
    price = 100.0
    for i in range(20):
        price += (-1) ** i * 0.5
        prices.append(price)
    for _ in range(20):
        price += 2
        prices.append(price)
    for p in prices:
        await event_bus.publish(MarketDataUpdated(symbol="TSLA", price=p, timeframe="1m"))
    await asyncio.sleep(0.15)

    rsi_evidence = [e.evidence for e in evidence if e.evidence.source == "RSI"]
    assert any(ev.title == "RSI Overbought" and ev.direction == "bearish" for ev in rsi_evidence)

    await registry.shutdown_all()


async def test_rsi_no_evidence_with_insufficient_history(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    indicators, evidence = await _collect(event_bus)

    await event_bus.publish(MarketDataUpdated(symbol="NEW", price=100.0, timeframe="1m"))
    await asyncio.sleep(0.1)

    assert not [e for e in evidence if e.evidence.source == "RSI"]
    await registry.shutdown_all()


# ---------------------------------------------------------------- MACD


async def test_macd_publishes_bullish_cross_evidence(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    indicators, evidence = await _collect(event_bus)

    # flat, then a sharp sustained move up -> forces histogram to flip positive
    prices = [100.0] * 40 + [100 + i * 2 for i in range(1, 20)]
    for p in prices:
        await event_bus.publish(MarketDataUpdated(symbol="NVDA", price=p, timeframe="1m"))
    await asyncio.sleep(0.15)

    macd_indicators = [e for e in indicators if e.indicator == "MACD"]
    macd_evidence = [e.evidence for e in evidence if e.evidence.source == "MACD"]
    assert len(macd_indicators) > 0
    assert any(ev.direction == "bullish" for ev in macd_evidence)

    await registry.shutdown_all()


# ---------------------------------------------------------------- ATR


async def test_atr_publishes_volatility_expansion_evidence(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    indicators, evidence = await _collect(event_bus)

    calm = [{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 500} for _ in range(20)]
    volatile = [{"open": 100, "high": 120, "low": 80, "close": 100, "volume": 500} for _ in range(5)]
    await _publish_bars(event_bus, "SPY", calm + volatile)

    atr_evidence = [e.evidence for e in evidence if e.evidence.source == "ATR"]
    assert any(ev.direction == "neutral" and ev.title == "Volatility Expansion (ATR)" for ev in atr_evidence)

    await registry.shutdown_all()


# ---------------------------------------------------------------- ADX


async def test_adx_publishes_trend_strength_evidence_on_strong_uptrend(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    indicators, evidence = await _collect(event_bus)

    # choppy/directionless warmup first so ADX's first valid readings start
    # low (below the 25 trend threshold), then a sustained uptrend so ADX
    # climbs and actually crosses the threshold within the observed window
    # (a trend that's directional from bar 1 already reads "trending" on
    # its very first valid ADX value, with no earlier "untrending" reading
    # to cross from).
    choppy = []
    price = 100.0
    for i in range(40):
        price += (-1) ** i * 0.3
        choppy.append({"open": price, "high": price + 0.5, "low": price - 0.5, "close": price, "volume": 500})
    bars = choppy + _uptrend_bars(40, base=price, spread=7)
    await _publish_bars(event_bus, "AMD", bars)

    adx_evidence = [e.evidence for e in evidence if e.evidence.source == "ADX"]
    assert any(ev.direction == "bullish" for ev in adx_evidence)

    await registry.shutdown_all()


# ---------------------------------------------------------------- Bollinger


async def test_bollinger_publishes_upper_breakout_evidence(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    indicators, evidence = await _collect(event_bus)

    flat = [{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 500} for _ in range(25)]
    spike = [{"open": 100, "high": 130, "low": 100, "close": 130, "volume": 500}]
    await _publish_bars(event_bus, "MSFT", flat + spike)

    bb_evidence = [e.evidence for e in evidence if e.evidence.source == "Bollinger"]
    assert any(ev.direction == "bullish" and "Upper" in ev.title for ev in bb_evidence)

    await registry.shutdown_all()


# ---------------------------------------------------------------- Supertrend


async def test_supertrend_flips_bullish_after_downtrend_then_uptrend(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    indicators, evidence = await _collect(event_bus)

    # continuous price path: down for 40 bars, then up for 40 bars picking
    # up exactly where the downtrend left off (no gap) -> a real trend
    # reversal Supertrend can actually flip on, instead of two disconnected
    # price levels that would just register as one giant, band-blowing gap.
    down = [{"open": 300 - i, "high": 305 - i, "low": 295 - i, "close": 300 - i, "volume": 500} for i in range(40)]
    last_close = down[-1]["close"]
    up = [
        {"open": last_close + i, "high": last_close + i + 5, "low": last_close + i - 5, "close": last_close + i, "volume": 500}
        for i in range(1, 41)
    ]
    await _publish_bars(event_bus, "GOOG", down + up)

    st_evidence = [e.evidence for e in evidence if e.evidence.source == "Supertrend"]
    assert len(st_evidence) >= 1  # at least one flip across a trend reversal

    await registry.shutdown_all()


# ---------------------------------------------------------------- OBV


async def test_obv_publishes_cross_evidence(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    indicators, evidence = await _collect(event_bus)

    # oscillate down/up in volume-weighted closes to build OBV history, then
    # a sustained run of up-closes with volume to force OBV above its own SMA
    bars = []
    price = 100.0
    for i in range(30):
        price += (-1) ** i  # oscillate
        bars.append({"open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 100})
    for i in range(15):
        price += 2
        bars.append({"open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 500})
    await _publish_bars(event_bus, "QQQ", bars)

    obv_indicators = [e for e in indicators if e.indicator == "OBV"]
    obv_evidence = [e.evidence for e in evidence if e.evidence.source == "OBV"]
    assert len(obv_indicators) > 0
    assert any(ev.direction == "bullish" for ev in obv_evidence)

    await registry.shutdown_all()


async def test_obv_degraded_health_without_volume(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    await event_bus.publish(MarketDataUpdated(symbol="NOVOL", price=100.0, timeframe="1m"))
    await asyncio.sleep(0.1)

    health = await registry.health_check_all()
    assert health["OBV"].status == "degraded"

    await registry.shutdown_all()


# ---------------------------------------------------------------- CCI


async def test_cci_publishes_bullish_breakout_evidence(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    indicators, evidence = await _collect(event_bus)

    flat = [{"open": 100, "high": 100, "low": 100, "close": 100, "volume": 500} for _ in range(25)]
    spike = [{"open": 130, "high": 130, "low": 130, "close": 130, "volume": 500}]
    await _publish_bars(event_bus, "IBM", flat + spike)

    cci_evidence = [e.evidence for e in evidence if e.evidence.source == "CCI"]
    assert any(ev.direction == "bullish" for ev in cci_evidence)

    await registry.shutdown_all()


# ---------------------------------------------------------------- Ichimoku


async def test_ichimoku_publishes_cloud_breakout_evidence(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    indicators, evidence = await _collect(event_bus)

    flat = [{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 500} for _ in range(55)]
    breakout = [{"open": 100, "high": 200, "low": 100, "close": 200, "volume": 500} for _ in range(3)]
    await _publish_bars(event_bus, "AMZN", flat + breakout)

    ich_evidence = [e.evidence for e in evidence if e.evidence.source == "Ichimoku"]
    assert any(ev.direction == "bullish" for ev in ich_evidence)

    await registry.shutdown_all()


# ---------------------------------------------------------------- Donchian


async def test_donchian_publishes_breakout_evidence(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    indicators, evidence = await _collect(event_bus)

    flat = [{"open": 100, "high": 105, "low": 95, "close": 100, "volume": 500} for _ in range(25)]
    breakout = [{"open": 105, "high": 120, "low": 105, "close": 118, "volume": 500}]
    await _publish_bars(event_bus, "NFLX", flat + breakout)

    don_evidence = [e.evidence for e in evidence if e.evidence.source == "Donchian"]
    assert any(ev.direction == "bullish" and "New High" in ev.title for ev in don_evidence)

    await registry.shutdown_all()


# ---------------------------------------------------------------- Volume Profile


async def test_volume_profile_publishes_poc_cross_evidence(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    indicators, evidence = await _collect(event_bus)

    # a varied (not perfectly flat) heavy-volume cluster so the POC lands on
    # a computed bin midpoint rather than exactly on every close (a flat
    # cluster makes the POC exactly equal every close, which never produces
    # a genuine "below the POC" reading to cross from), then a decisive,
    # light-volume breakout that doesn't drag the POC up with it.
    cluster_closes = [95, 96, 97, 98, 96, 97, 95, 98, 96, 97, 95, 96, 97, 98, 96, 97, 95, 96, 97, 98]
    low_cluster = [{"open": c, "high": c, "low": c, "close": c, "volume": 1000} for c in cluster_closes]
    breakout = [{"open": 115, "high": 115, "low": 115, "close": 115, "volume": 20} for _ in range(5)]
    await _publish_bars(event_bus, "CRM", low_cluster + breakout)

    vp_indicators = [e for e in indicators if e.indicator == "VolumeProfile"]
    vp_evidence = [e.evidence for e in evidence if e.evidence.source == "VolumeProfile"]
    assert len(vp_indicators) > 0
    assert any(ev.direction == "bullish" for ev in vp_evidence)

    await registry.shutdown_all()


async def test_volume_profile_degraded_health_without_volume(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    await event_bus.publish(MarketDataUpdated(symbol="NOVOL2", price=100.0, timeframe="1m"))
    await asyncio.sleep(0.1)

    health = await registry.health_check_all()
    assert health["VolumeProfile"].status == "degraded"

    await registry.shutdown_all()


# ---------------------------------------------------------------- VWAP


async def test_vwap_publishes_cross_evidence(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    indicators, evidence = await _collect(event_bus)

    # VWAP is cumulative over the whole retained window, so it always lags
    # the current close — a monotonic move in one direction alone never
    # produces a genuine "below VWAP" reading to cross from (the cumulative
    # average trails behind, keeping close on the same side throughout). A
    # falling-then-sharply-rising ("V"-shaped) price path gives a real
    # below-VWAP state during the fall, then a clean cross above during the
    # sharp recovery.
    falling = [{"open": 110 - i, "high": 111 - i, "low": 109 - i, "close": 110 - i, "volume": 500} for i in range(1, 16)]
    rising = [{"open": 95 + i * 5, "high": 96 + i * 5, "low": 94 + i * 5, "close": 95 + i * 5, "volume": 500} for i in range(1, 11)]
    await _publish_bars(event_bus, "META", falling + rising)

    vwap_indicators = [e for e in indicators if e.indicator == "VWAP"]
    vwap_evidence = [e.evidence for e in evidence if e.evidence.source == "VWAP"]
    assert len(vwap_indicators) > 0
    assert any(ev.direction == "bullish" for ev in vwap_evidence)

    await registry.shutdown_all()


async def test_vwap_degraded_health_without_volume(event_bus, settings):
    registry = await _load_registry(event_bus, settings)
    await event_bus.publish(MarketDataUpdated(symbol="NOVOL3", price=100.0, timeframe="1m"))
    await asyncio.sleep(0.1)

    health = await registry.health_check_all()
    assert health["VWAP"].status == "degraded"

    await registry.shutdown_all()


# ---------------------------------------------------------------- no plugin ever emits a directive


@pytest.mark.parametrize("name", ALL_NEW_INDICATOR_NAMES)
async def test_evidence_never_contains_a_trade_directive(event_bus, settings, name):
    """PROJECT.md: plugins publish evidence, never a decision. Spot-check
    that every evidence object from every new plugin still conforms to the
    Universal Evidence Object's vocabulary (direction/confidence/metadata
    only) rather than anything resembling a buy/sell instruction."""
    registry = await _load_registry(event_bus, settings)
    indicators, evidence = await _collect(event_bus)

    bars = _uptrend_bars(60, base=50, spread=15) + _downtrend_bars(60, base=250, spread=15)
    await _publish_bars(event_bus, "ZZZZ", bars)

    matching = [e.evidence for e in evidence if e.evidence.source == name]
    for ev in matching:
        assert ev.direction in ("bullish", "bearish", "neutral")
        assert not any(word in ev.title.lower() for word in ("buy", "sell", "should"))

    await registry.shutdown_all()
