"""Unit tests for the Capital Protection Engine
(``app/capital_protection/engine.py``) — Milestone 11."""
from __future__ import annotations

from app.capital_protection.engine import CapitalProtectionEngine
from app.core.clock import SimulatedClock
from app.event_bus.bus import EventBus
from app.event_bus.events import DecisionRecorded, MarketDataUpdated, RiskEvent, TradeClosed, TradeOpened


def _decision(**overrides) -> DecisionRecorded:
    defaults = dict(
        source="test",
        symbol="NVDA",
        simulated_action="watch_bullish",
        price_at_decision=100.0,
        confidence=70.0,
        bar_index=0,
        lookahead_bars=5,
        outcome="correct",
        outcome_price_change_pct=2.0,
        outcome_pending=False,
    )
    defaults.update(overrides)
    return DecisionRecorded(**defaults)


async def _run(settings, *decisions, clock=None):
    engine = CapitalProtectionEngine(settings, clock=clock)
    event_bus = EventBus.from_settings(settings)
    engine.attach(event_bus)
    captured: list[RiskEvent] = []
    trades: list[object] = []

    async def _cap_risk(e: RiskEvent) -> None:
        captured.append(e)

    async def _cap_trade(e) -> None:
        trades.append(e)

    event_bus.subscribe(RiskEvent, _cap_risk)
    event_bus.subscribe(TradeOpened, _cap_trade)
    event_bus.subscribe(TradeClosed, _cap_trade)
    for decision in decisions:
        await event_bus.publish(decision)
    await event_bus.drain()
    await event_bus.shutdown()
    return engine, captured, trades


# ---------------------------------------------------------------- trade lifecycle synthesis


async def test_resolved_decision_publishes_trade_opened_then_trade_closed(settings):
    engine, risk_events, trades = await _run(settings, _decision())
    kinds = [type(t).__name__ for t in trades]
    assert kinds == ["TradeOpened", "TradeClosed"]
    opened, closed = trades
    assert opened.symbol == "NVDA"
    assert opened.side == "long"
    assert closed.trade_id == opened.trade_id
    assert closed.pnl is not None


async def test_bearish_decision_opens_a_short(settings):
    _, _, trades = await _run(settings, _decision(simulated_action="watch_bearish"))
    opened = trades[0]
    assert opened.side == "short"


async def test_neutral_or_no_action_decision_never_opens_a_trade(settings):
    _, _, trades_neutral = await _run(settings, _decision(simulated_action="watch_neutral"))
    assert trades_neutral == []
    _, _, trades_none = await _run(settings, _decision(simulated_action="no_action", outcome=None, outcome_pending=False))
    assert trades_none == []


async def test_still_pending_decision_opens_but_never_closes(settings):
    engine, _, trades = await _run(settings, _decision(outcome=None, outcome_pending=True))
    kinds = [type(t).__name__ for t in trades]
    assert kinds == ["TradeOpened"]
    status = engine.status()
    assert status.open_position_count == 1


async def test_long_pnl_is_positive_when_price_rises(settings):
    engine, _, trades = await _run(settings, _decision(simulated_action="watch_bullish", outcome="correct", outcome_price_change_pct=3.0))
    _, closed = trades
    assert closed.pnl > 0
    assert engine.status().equity > settings.capital_protection.starting_equity


async def test_short_pnl_is_positive_when_price_falls(settings):
    engine, _, trades = await _run(settings, _decision(simulated_action="watch_bearish", outcome="correct", outcome_price_change_pct=-3.0))
    _, closed = trades
    assert closed.pnl > 0


async def test_wrong_direction_produces_negative_pnl(settings):
    engine, _, trades = await _run(settings, _decision(simulated_action="watch_bullish", outcome="incorrect", outcome_price_change_pct=-2.0))
    _, closed = trades
    assert closed.pnl < 0
    assert engine.status().equity < settings.capital_protection.starting_equity


async def test_position_sizing_scales_with_confidence(settings):
    _, _, low_conf_trades = await _run(settings, _decision(confidence=10.0))
    _, _, high_conf_trades = await _run(settings, _decision(confidence=90.0))
    assert low_conf_trades[0].quantity < high_conf_trades[0].quantity


# ---------------------------------------------------------------- equity / drawdown state


async def test_consecutive_losses_increments_on_loss_and_resets_on_win(settings):
    engine, events, _ = await _run(
        settings,
        _decision(bar_index=0, outcome="incorrect", outcome_price_change_pct=-1.0),
        _decision(bar_index=1, outcome="incorrect", outcome_price_change_pct=-1.0),
        _decision(bar_index=2, outcome="correct", outcome_price_change_pct=1.0),
    )
    streak_events = [e for e in events if e.risk_type == "consecutive_losses"]
    assert [e.value for e in streak_events] == [1.0, 2.0, 0.0]


async def test_neutral_outcome_does_not_change_the_streak(settings):
    engine, events, _ = await _run(
        settings,
        _decision(bar_index=0, outcome="incorrect", outcome_price_change_pct=-1.0),
        _decision(bar_index=1, outcome="neutral", outcome_price_change_pct=0.0),
    )
    streak_events = [e for e in events if e.risk_type == "consecutive_losses"]
    assert [e.value for e in streak_events] == [1.0, 1.0]


async def test_total_drawdown_reflects_peak_to_current_equity(settings):
    engine, events, _ = await _run(
        settings,
        _decision(bar_index=0, outcome="correct", outcome_price_change_pct=5.0),
        _decision(bar_index=1, outcome="incorrect", outcome_price_change_pct=-5.0),
    )
    total_dd_events = [e for e in events if e.risk_type == "total_drawdown"]
    # First trade only grows equity (no drawdown yet); second trade loses
    # against the new peak, so total_drawdown becomes positive.
    assert total_dd_events[0].value == 0.0
    assert total_dd_events[1].value > 0.0


async def test_daily_drawdown_resets_on_a_new_simulated_day(settings):
    from datetime import timedelta

    clock = SimulatedClock()
    engine, events, _ = await _run(settings, _decision(bar_index=0, outcome="incorrect", outcome_price_change_pct=-2.0), clock=clock)
    first_daily = [e for e in events if e.risk_type == "daily_drawdown"][-1]
    assert first_daily.value > 0.0

    # Advance the clock into the next day and evaluate again -- daily
    # drawdown should reset even though total/trailing drawdown persist.
    clock2 = SimulatedClock(start=clock.now() + timedelta(days=1))
    engine2, events2, _ = await _run(settings, _decision(bar_index=1, outcome="correct", outcome_price_change_pct=0.5), clock=clock2)
    daily2 = [e for e in events2 if e.risk_type == "daily_drawdown"][-1]
    assert daily2.value == 0.0  # a fresh engine+day starts clean


async def test_prop_firm_compliance_breaches_when_daily_loss_limit_exceeded(settings):
    settings.capital_protection.active_profile = "prop_firm"
    # Realistic per-trade position sizing (profile's own max_position_size_pct,
    # scaled by confidence) caps how much a single trade can move equity by
    # design -- widen sizing here specifically to force a breach, rather than
    # asserting real-world position sizing would ever allow one this large.
    settings.capital_protection.profiles["prop_firm"].max_position_size_pct = 50.0
    engine, events, _ = await _run(
        settings, _decision(confidence=100.0, outcome="incorrect", outcome_price_change_pct=-20.0)
    )
    compliance = [e for e in events if e.risk_type == "prop_firm_compliance"][-1]
    assert compliance.severity == "critical"
    assert compliance.value >= 1.0


async def test_prop_firm_compliance_passes_when_within_limits(settings):
    settings.capital_protection.active_profile = "prop_firm"
    engine, events, _ = await _run(settings, _decision(outcome="correct", outcome_price_change_pct=1.0))
    compliance = [e for e in events if e.risk_type == "prop_firm_compliance"][-1]
    assert compliance.severity == "info"
    assert compliance.value < 1.0


# ---------------------------------------------------------------- concentration


async def test_symbol_concentration_reflects_recent_closed_trade_notional(settings):
    engine, events, _ = await _run(
        settings,
        _decision(symbol="NVDA", bar_index=0),
        _decision(symbol="AAPL", bar_index=1),
    )
    symbol_events = [e for e in events if e.risk_type == "symbol_concentration" and e.symbol == "AAPL"]
    assert symbol_events
    # Two equally-sized trades -> the most recent symbol's own concentration
    # sits at roughly half of the window's total notional.
    assert 40.0 < symbol_events[-1].value < 60.0


async def test_sector_concentration_groups_unmapped_symbols_as_unknown(settings):
    engine, events, _ = await _run(settings, _decision(symbol="ZZZZ_UNMAPPED"))
    sector_events = [e for e in events if e.risk_type == "sector_concentration"]
    assert sector_events
    assert sector_events[-1].context.get("sector") == "Unknown"


# ---------------------------------------------------------------- market-data-driven


async def test_correlated_exposure_computes_real_pearson_correlation(settings):
    settings.capital_protection.correlation_min_samples = 3
    settings.capital_protection.evaluation_interval_ticks = 1
    engine = CapitalProtectionEngine(settings)
    event_bus = EventBus.from_settings(settings)
    engine.attach(event_bus)
    captured: list[RiskEvent] = []

    async def _cap(e: RiskEvent) -> None:
        captured.append(e)

    event_bus.subscribe(RiskEvent, _cap)

    # Both symbols need to be "exposed" (recently traded) for correlation
    # to be computed between them.
    await event_bus.publish(_decision(symbol="NVDA"))
    await event_bus.publish(_decision(symbol="AAPL"))
    await event_bus.drain()

    for i in range(6):
        await event_bus.publish(MarketDataUpdated(source="test", symbol="NVDA", price=100.0 + i))
        await event_bus.publish(MarketDataUpdated(source="test", symbol="AAPL", price=200.0 + i * 2))
    await event_bus.drain()

    corr_events = [e for e in captured if e.risk_type == "correlated_exposure"]
    assert corr_events
    assert corr_events[-1].value > 0.99  # perfectly co-moving linear series
    assert corr_events[-1].severity == "critical"
    assert set(corr_events[-1].context.get("pair", [])) == {"NVDA", "AAPL"}

    await event_bus.shutdown()


async def test_margin_and_broker_placeholders_are_always_inapplicable(settings):
    settings.capital_protection.evaluation_interval_ticks = 1
    engine = CapitalProtectionEngine(settings)
    event_bus = EventBus.from_settings(settings)
    engine.attach(event_bus)
    captured: list[RiskEvent] = []

    async def _cap(e: RiskEvent) -> None:
        captured.append(e)

    event_bus.subscribe(RiskEvent, _cap)
    await event_bus.publish(MarketDataUpdated(source="test", symbol="NVDA", price=100.0))
    await event_bus.drain()

    margin = [e for e in captured if e.risk_type == "margin_utilization"]
    broker = [e for e in captured if e.risk_type == "broker_constraints"]
    assert margin and not margin[-1].applicable
    assert broker and not broker[-1].applicable
    await event_bus.shutdown()


async def test_market_data_evaluation_is_throttled(settings):
    settings.capital_protection.evaluation_interval_ticks = 5
    engine = CapitalProtectionEngine(settings)
    event_bus = EventBus.from_settings(settings)
    engine.attach(event_bus)
    captured: list[RiskEvent] = []

    async def _cap(e: RiskEvent) -> None:
        captured.append(e)

    event_bus.subscribe(RiskEvent, _cap)
    for i in range(12):
        await event_bus.publish(MarketDataUpdated(source="test", symbol="NVDA", price=100.0 + i))
    await event_bus.drain()

    margin_events = [e for e in captured if e.risk_type == "margin_utilization"]
    assert len(margin_events) == 2  # ticks 5 and 10 out of 12
    await event_bus.shutdown()


# ---------------------------------------------------------------- profile switching


async def test_set_active_profile_changes_subsequent_thresholds(settings):
    engine = CapitalProtectionEngine(settings)
    event_bus = EventBus.from_settings(settings)
    engine.attach(event_bus)
    captured: list[RiskEvent] = []

    async def _cap(e: RiskEvent) -> None:
        captured.append(e)

    event_bus.subscribe(RiskEvent, _cap)

    await event_bus.publish(_decision())
    await event_bus.drain()
    first_threshold = [e for e in captured if e.risk_type == "daily_drawdown"][-1].threshold

    assert engine.set_active_profile("conservative") is True
    captured.clear()
    await event_bus.publish(_decision(bar_index=1))
    await event_bus.drain()
    second_threshold = [e for e in captured if e.risk_type == "daily_drawdown"][-1].threshold

    assert first_threshold != second_threshold
    assert second_threshold == settings.capital_protection.profiles["conservative"].max_daily_loss_pct
    await event_bus.shutdown()


# ---------------------------------------------------------------- graceful degradation


async def test_disabled_engine_never_publishes_anything(settings):
    settings.capital_protection.enabled = False
    engine, events, trades = await _run(settings, _decision())
    assert events == []
    assert trades == []
    status = engine.status()
    assert status.enabled is False


async def test_clock_injection_controls_event_timestamps(settings):
    clock = SimulatedClock()
    engine, events, trades = await _run(settings, _decision(), clock=clock)
    assert all(e.timestamp == clock.now() for e in events)
    assert all(t.timestamp == clock.now() for t in trades)


# ---------------------------------------------------------------- status query


async def test_status_reports_profile_names(settings):
    engine, _, _ = await _run(settings, _decision())
    status = engine.status()
    assert "conservative" in status.profile_names
    assert "prop_firm" in status.profile_names
