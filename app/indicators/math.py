"""
Pure, stateless indicator calculations shared by every indicator plugin.

Every function here takes plain lists (the retained window from a
``SymbolWindow``) and returns either a value (or tuple/dict of values) or
``None`` when there isn't enough history yet — never raises on insufficient
data, mirroring the rest of the codebase's "explicit, never silent" pattern.
Nothing in this module touches the Event Bus, plugin config, or logging —
that's the plugin's job. This module exists purely so the same formula is
never written twice (PROJECT.md's Indicator System: "No duplicate
calculations").

Where a function recomputes over the whole retained window rather than
carrying continuous incremental state (RSI, MACD, ATR, ADX, Supertrend), the
window (``SymbolWindow.DEFAULT_WINDOW`` = 300 bars) is deliberately much
larger than any period used in this milestone, so Wilder/EMA seed bias from
"restarting" at the start of the window is negligible by the time the
warmup period has elapsed. This is a standard tradeoff for a fixed-lookback
indicator library and keeps every formula easy to read, test, and verify by
hand — a true continuously-compounding EMA would require each plugin to
carry per-symbol state, which is exactly what ``EMAPlugin`` does for its own
cross detection, and remains the right choice there.
"""
from __future__ import annotations

import statistics
from collections.abc import Sequence

# ---------------------------------------------------------------- moving averages


def sma(values: Sequence[float], period: int) -> float | None:
    """Simple moving average of the last ``period`` values."""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def ema_step(previous: float | None, price: float, period: int) -> float:
    """One step of an exponential moving average. ``previous is None`` seeds
    the EMA at ``price`` (matches ``EMAPlugin``'s original incremental
    implementation — moved here so EMA and every EMA-derived indicator
    (MACD, Supertrend's smoothing) share one definition)."""
    alpha = 2 / (period + 1)
    if previous is None:
        return price
    return price * alpha + previous * (1 - alpha)


def ema_series(values: Sequence[float], period: int) -> list[float]:
    """Full EMA series over ``values``, seeded at ``values[0]``. Used by
    indicators (MACD) that need the EMA's history, not just its latest
    value."""
    result: list[float] = []
    prev: float | None = None
    for v in values:
        prev = ema_step(prev, v, period)
        result.append(prev)
    return result


# ---------------------------------------------------------------- momentum


def rsi(closes: Sequence[float], period: int = 14) -> float | None:
    """Wilder's RSI. Needs at least ``period + 1`` closes."""
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(
    closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float, float, float] | None:
    """MACD line, signal line, histogram. Needs at least ``slow + signal``
    closes for the signal line to have run its own warmup."""
    if len(closes) < slow + signal:
        return None
    fast_series = ema_series(closes, fast)
    slow_series = ema_series(closes, slow)
    macd_series = [f - s for f, s in zip(fast_series, slow_series)]
    signal_series = ema_series(macd_series, signal)
    macd_line = macd_series[-1]
    signal_line = signal_series[-1]
    return macd_line, signal_line, macd_line - signal_line


def cci(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 20) -> float | None:
    """Commodity Channel Index using the standard 0.015 scaling constant."""
    if len(closes) < period:
        return None
    typical_prices = [(h + lo + c) / 3 for h, lo, c in zip(highs, lows, closes)]
    window = typical_prices[-period:]
    tp_sma = sum(window) / period
    mean_deviation = sum(abs(tp - tp_sma) for tp in window) / period
    if mean_deviation == 0:
        return 0.0
    return (typical_prices[-1] - tp_sma) / (0.015 * mean_deviation)


# ---------------------------------------------------------------- volatility / range


def _true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    return trs


def _wilder_smooth(values: Sequence[float], period: int) -> list[float]:
    """Wilder's smoothing: seed at the simple average of the first
    ``period`` values, then smooth the remainder. Returns a series aligned
    to ``values[period - 1:]``."""
    smoothed = [sum(values[:period]) / period]
    for v in values[period:]:
        smoothed.append((smoothed[-1] * (period - 1) + v) / period)
    return smoothed


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> float | None:
    """Wilder's Average True Range."""
    if len(closes) < period + 1:
        return None
    trs = _true_ranges(highs, lows, closes)
    return _wilder_smooth(trs, period)[-1]


def bollinger_bands(
    closes: Sequence[float], period: int = 20, num_std: float = 2.0
) -> tuple[float, float, float] | None:
    """(upper, mid, lower) Bollinger Bands. ``mid`` is the SMA, band width
    is ``num_std`` population standard deviations of the same window."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    std = statistics.pstdev(window)
    return mid + num_std * std, mid, mid - num_std * std


def donchian_channel(highs: Sequence[float], lows: Sequence[float], period: int = 20) -> tuple[float, float, float] | None:
    """(upper, lower, mid) Donchian Channel — highest high / lowest low over
    the period, and their midpoint."""
    if len(highs) < period or len(lows) < period:
        return None
    upper = max(highs[-period:])
    lower = min(lows[-period:])
    return upper, lower, (upper + lower) / 2


def supertrend(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[float, str] | None:
    """(value, direction) where direction is ``"up"`` (bullish — price is
    above the trailing stop line) or ``"down"`` (bearish). Standard
    Supertrend: an ATR-based trailing band that flips sides when price
    closes through it."""
    if len(closes) < period + 2:
        return None

    trs = _true_ranges(highs, lows, closes)
    atr_series = _wilder_smooth(trs, period)  # aligned to index `period - 1` of the bars
    offset = period - 1  # first index with a valid ATR

    final_upper: list[float] = []
    final_lower: list[float] = []
    trend: list[str] = []
    st_value: list[float] = []

    for j, i in enumerate(range(offset, len(closes))):
        atr_i = atr_series[j]
        basic_upper = (highs[i] + lows[i]) / 2 + multiplier * atr_i
        basic_lower = (highs[i] + lows[i]) / 2 - multiplier * atr_i

        if j == 0:
            final_upper.append(basic_upper)
            final_lower.append(basic_lower)
            # Seed direction by comparing the close to the basic bands.
            if closes[i] <= basic_upper:
                trend.append("down")
                st_value.append(basic_upper)
            else:
                trend.append("up")
                st_value.append(basic_lower)
            continue

        prev_upper, prev_lower = final_upper[-1], final_lower[-1]
        fu = basic_upper if (basic_upper < prev_upper or closes[i - 1] > prev_upper) else prev_upper
        fl = basic_lower if (basic_lower > prev_lower or closes[i - 1] < prev_lower) else prev_lower
        final_upper.append(fu)
        final_lower.append(fl)

        prev_trend = trend[-1]
        if prev_trend == "down":
            if closes[i] > fu:
                trend.append("up")
                st_value.append(fl)
            else:
                trend.append("down")
                st_value.append(fu)
        else:
            if closes[i] < fl:
                trend.append("down")
                st_value.append(fu)
            else:
                trend.append("up")
                st_value.append(fl)

    return st_value[-1], trend[-1]


def adx(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> tuple[float, float, float] | None:
    """(adx, plus_di, minus_di) — Wilder's Average Directional Index.
    Needs roughly ``2 * period`` bars: one period to warm up +DM/-DM/TR
    smoothing, another to warm up the ADX smoothing itself."""
    if len(closes) < 2 * period + 1:
        return None

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, len(closes)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

    trs = _true_ranges(highs, lows, closes)[1:]  # align with plus_dm/minus_dm (both start at index 1)

    smoothed_tr = _wilder_smooth(trs, period)
    smoothed_plus_dm = _wilder_smooth(plus_dm, period)
    smoothed_minus_dm = _wilder_smooth(minus_dm, period)

    plus_di_series = [100 * pdm / tr if tr else 0.0 for pdm, tr in zip(smoothed_plus_dm, smoothed_tr)]
    minus_di_series = [100 * mdm / tr if tr else 0.0 for mdm, tr in zip(smoothed_minus_dm, smoothed_tr)]
    dx_series = [
        100 * abs(p - m) / (p + m) if (p + m) else 0.0 for p, m in zip(plus_di_series, minus_di_series)
    ]

    if len(dx_series) < period:
        return None
    adx_series = _wilder_smooth(dx_series, period)

    return adx_series[-1], plus_di_series[-1], minus_di_series[-1]


# ---------------------------------------------------------------- volume


def obv(closes: Sequence[float], volumes: Sequence[float]) -> float | None:
    """On-Balance Volume — cumulative volume signed by the direction of each
    close-to-close move."""
    if len(closes) < 2 or len(closes) != len(volumes):
        return None
    running = 0.0
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            running += volumes[i]
        elif closes[i] < closes[i - 1]:
            running -= volumes[i]
    return running


def vwap(closes: Sequence[float], highs: Sequence[float], lows: Sequence[float], volumes: Sequence[float]) -> float | None:
    """Volume-Weighted Average Price over the retained window, using the
    typical price (H+L+C)/3 per bar — the standard VWAP definition when a
    genuine tick-by-tick trade tape isn't available."""
    if not closes or len(closes) != len(volumes):
        return None
    total_volume = sum(volumes)
    if total_volume == 0:
        return None
    weighted = sum(((h + lo + c) / 3) * v for h, lo, c, v in zip(highs, lows, closes, volumes))
    return weighted / total_volume


def volume_profile(
    closes: Sequence[float], volumes: Sequence[float], num_bins: int = 10
) -> dict[str, object] | None:
    """Buckets the retained window's closes into ``num_bins`` price bins
    weighted by volume, returning the Point of Control (the bin with the
    most volume, reported as its midpoint price) and the full bin
    breakdown."""
    if len(closes) < 2 or len(closes) != len(volumes):
        return None

    lo, hi = min(closes), max(closes)
    if lo == hi:
        return {"poc": lo, "bins": [{"price": lo, "volume": sum(volumes)}]}

    bin_width = (hi - lo) / num_bins
    bin_volumes = [0.0] * num_bins
    for price, vol in zip(closes, volumes):
        idx = int((price - lo) / bin_width)
        idx = min(idx, num_bins - 1)
        bin_volumes[idx] += vol

    poc_idx = max(range(num_bins), key=lambda i: bin_volumes[i])
    poc_price = lo + (poc_idx + 0.5) * bin_width
    bins = [{"price": lo + (i + 0.5) * bin_width, "volume": bin_volumes[i]} for i in range(num_bins)]
    return {"poc": poc_price, "bins": bins}


# ---------------------------------------------------------------- trend structure


def ichimoku(
    highs: Sequence[float],
    lows: Sequence[float],
    tenkan_period: int = 9,
    kijun_period: int = 26,
    senkou_b_period: int = 52,
) -> dict[str, float] | None:
    """Tenkan-sen, Kijun-sen, Senkou Span A/B. This reports the lines'
    current computed values (not shifted forward for charting — the 26-period
    forward projection of the Senkou spans is a plotting concern, not
    something the Evidence layer needs)."""
    if len(highs) < senkou_b_period or len(lows) < senkou_b_period:
        return None

    def _midpoint(period: int) -> float:
        return (max(highs[-period:]) + min(lows[-period:])) / 2

    tenkan = _midpoint(tenkan_period)
    kijun = _midpoint(kijun_period)
    senkou_a = (tenkan + kijun) / 2
    senkou_b = _midpoint(senkou_b_period)
    return {"tenkan": tenkan, "kijun": kijun, "senkou_a": senkou_a, "senkou_b": senkou_b}
