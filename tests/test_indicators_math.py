"""
Correctness tests for app/indicators/math.py — the shared calculation
library every indicator plugin builds on. These are hand-verifiable: each
case is constructed so the expected result can be computed on paper, rather
than asserting against another run of the same code.
"""
from __future__ import annotations

import math

import pytest

from app.indicators import math as im


# ---------------------------------------------------------------- moving averages


def test_sma_insufficient_data_returns_none():
    assert im.sma([1, 2], period=5) is None


def test_sma_basic():
    assert im.sma([1, 2, 3, 4, 5], period=5) == 3.0
    assert im.sma([10, 20, 30], period=2) == 25.0  # only last 2 values


def test_ema_step_seeds_at_first_price():
    assert im.ema_step(None, 100.0, period=10) == 100.0


def test_ema_step_matches_formula():
    # alpha = 2/(10+1) = 0.181818...
    result = im.ema_step(100.0, 110.0, period=10)
    alpha = 2 / 11
    assert result == pytest.approx(110.0 * alpha + 100.0 * (1 - alpha))


def test_ema_series_constant_input_is_constant():
    series = im.ema_series([50.0] * 10, period=5)
    assert all(v == pytest.approx(50.0) for v in series)


# ---------------------------------------------------------------- momentum


def test_rsi_insufficient_data_returns_none():
    assert im.rsi([1, 2, 3], period=14) is None


def test_rsi_all_gains_is_100():
    closes = [100 + i for i in range(20)]  # strictly increasing
    assert im.rsi(closes, period=14) == 100.0


def test_rsi_all_losses_is_0():
    closes = [100 - i for i in range(20)]  # strictly decreasing
    assert im.rsi(closes, period=14) == 0.0


def test_macd_insufficient_data_returns_none():
    assert im.macd(list(range(10)), fast=12, slow=26, signal=9) is None


def test_macd_constant_price_is_zero():
    closes = [100.0] * 40
    macd_line, signal_line, histogram = im.macd(closes, fast=12, slow=26, signal=9)
    assert macd_line == pytest.approx(0.0)
    assert signal_line == pytest.approx(0.0)
    assert histogram == pytest.approx(0.0)


def test_cci_flat_market_is_zero():
    # highs == lows == closes -> mean deviation is 0 -> CCI defined as 0
    assert im.cci([10.0] * 20, [10.0] * 20, [10.0] * 20, period=20) == 0.0


def test_cci_hand_computed():
    # typical price == close for these bars (high == low == close per bar)
    highs = lows = closes = [1.0, 2.0, 3.0]
    result = im.cci(highs, lows, closes, period=3)
    # sma=2, mean_deviation = (1+0+1)/3 = 0.6667, cci = (3-2)/(0.015*0.6667) = 100
    assert result == pytest.approx(100.0, abs=0.01)


def test_cci_insufficient_data_returns_none():
    assert im.cci([1.0], [1.0], [1.0], period=20) is None


# ---------------------------------------------------------------- volatility / range


def test_atr_insufficient_data_returns_none():
    assert im.atr([1.0], [1.0], [1.0], period=14) is None


def test_atr_constant_range():
    n = 30
    highs = [101.0] * n
    lows = [99.0] * n
    closes = [100.0] * n
    # TR is constant at 2.0 for every bar (range=2, gap-vs-prev-close=1 < 2)
    assert im.atr(highs, lows, closes, period=14) == pytest.approx(2.0)


def test_bollinger_insufficient_data_returns_none():
    assert im.bollinger_bands([1.0, 2.0], period=20) is None


def test_bollinger_flat_price_has_zero_width():
    upper, mid, lower = im.bollinger_bands([100.0] * 20, period=20, num_std=2.0)
    assert mid == 100.0
    assert upper == 100.0
    assert lower == 100.0


def test_bollinger_hand_computed():
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    upper, mid, lower = im.bollinger_bands(closes, period=5, num_std=2.0)
    expected_std = math.sqrt(2.0)  # population stdev of [1..5], mean 3
    assert mid == pytest.approx(3.0)
    assert upper == pytest.approx(3.0 + 2 * expected_std)
    assert lower == pytest.approx(3.0 - 2 * expected_std)


def test_donchian_insufficient_data_returns_none():
    assert im.donchian_channel([1.0], [1.0], period=20) is None


def test_donchian_hand_computed():
    highs = [1.0, 5.0, 3.0, 2.0, 4.0]
    lows = [0.5, 1.0, 0.5, 0.2, 0.1]
    upper, lower, mid = im.donchian_channel(highs, lows, period=5)
    assert upper == 5.0
    assert lower == 0.1
    assert mid == pytest.approx(2.55)


def test_supertrend_insufficient_data_returns_none():
    assert im.supertrend([1.0], [1.0], [1.0], period=10) is None


def test_supertrend_strong_uptrend_is_bullish():
    n = 60
    highs = [110.0 + i for i in range(n)]
    lows = [100.0 + i for i in range(n)]
    closes = [105.0 + i for i in range(n)]
    value, direction = im.supertrend(highs, lows, closes, period=10, multiplier=3.0)
    assert direction == "up"
    assert value < closes[-1]


def test_supertrend_strong_downtrend_is_bearish():
    n = 60
    highs = [210.0 - i for i in range(n)]
    lows = [200.0 - i for i in range(n)]
    closes = [205.0 - i for i in range(n)]
    value, direction = im.supertrend(highs, lows, closes, period=10, multiplier=3.0)
    assert direction == "down"
    assert value > closes[-1]


def test_adx_insufficient_data_returns_none():
    assert im.adx([1.0] * 5, [1.0] * 5, [1.0] * 5, period=14) is None


def test_adx_strong_uptrend_shows_bullish_directional_movement():
    n = 60
    highs = [110.0 + i for i in range(n)]
    lows = [100.0 + i for i in range(n)]
    closes = [105.0 + i for i in range(n)]
    adx_value, plus_di, minus_di = im.adx(highs, lows, closes, period=14)
    assert adx_value > 50.0  # steady, uninterrupted directional movement -> strong trend
    assert plus_di > minus_di
    assert minus_di == pytest.approx(0.0)


# ---------------------------------------------------------------- volume


def test_obv_insufficient_data_returns_none():
    assert im.obv([1.0], [100.0]) is None
    assert im.obv([1.0, 2.0], [100.0]) is None  # mismatched lengths


def test_obv_hand_computed():
    closes = [10.0, 11.0, 10.0, 12.0]
    volumes = [100.0, 100.0, 100.0, 100.0]
    # bar1: up -> +100 = 100; bar2: down -> -100 = 0; bar3: up -> +100 = 100
    assert im.obv(closes, volumes) == pytest.approx(100.0)


def test_vwap_zero_volume_returns_none():
    assert im.vwap([1.0], [1.0], [1.0], [0.0]) is None


def test_vwap_hand_computed():
    # single bar: typical price (h+l+c)/3, VWAP over one bar equals that
    result = im.vwap(closes=[10.0], highs=[12.0], lows=[8.0], volumes=[50.0])
    assert result == pytest.approx((12.0 + 8.0 + 10.0) / 3)


def test_volume_profile_insufficient_data_returns_none():
    assert im.volume_profile([1.0], [1.0]) is None


def test_volume_profile_flat_price_single_bin():
    result = im.volume_profile([10.0, 10.0, 10.0], [5.0, 5.0, 5.0])
    assert result["poc"] == 10.0
    assert result["bins"][0]["volume"] == 15.0


def test_volume_profile_hand_computed():
    closes = [10.0, 10.0, 10.0, 20.0, 20.0]
    volumes = [100.0, 100.0, 100.0, 10.0, 10.0]
    result = im.volume_profile(closes, volumes, num_bins=2)
    # bin width = (20-10)/2 = 5; bin0 = [10,15) gets the three 10s -> 300 volume
    # bin1 = [15,20] gets the two 20s (clamped into last bin) -> 20 volume
    assert result["bins"][0]["volume"] == pytest.approx(300.0)
    assert result["bins"][1]["volume"] == pytest.approx(20.0)
    assert result["poc"] == pytest.approx(12.5)


# ---------------------------------------------------------------- trend structure


def test_ichimoku_insufficient_data_returns_none():
    assert im.ichimoku([1.0] * 10, [1.0] * 10) is None


def test_ichimoku_flat_market_all_lines_equal_midpoint():
    highs = [110.0] * 60
    lows = [100.0] * 60
    result = im.ichimoku(highs, lows)
    assert result["tenkan"] == pytest.approx(105.0)
    assert result["kijun"] == pytest.approx(105.0)
    assert result["senkou_a"] == pytest.approx(105.0)
    assert result["senkou_b"] == pytest.approx(105.0)
