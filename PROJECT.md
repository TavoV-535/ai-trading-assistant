# AI Trading Assistant
## Version 2.0
### Event-Driven Discord Trading Intelligence Platform

---

# Mission

Build an AI-powered Discord Trading Assistant that helps traders make informed decisions through education, market intelligence, and data-driven analysis.

The assistant is **not** a signal-selling bot.

It is an intelligent market assistant capable of gathering information, reasoning about it, explaining its conclusions, testing strategies, and helping users improve over time.

The entire application is controlled through Discord and runs locally on my laptop.

Every feature should be modular.

Every system should be extendable.

Nothing should require modifying the core architecture.

---

# Core Design Principles

The application must follow these principles.

- Plugin First
- Event Driven
- AI Assisted
- Local First
- Fully Modular
- Easy to Extend
- Educational
- Explainable
- Backtest Everything
- Configuration over Code

---

# Primary Goals

The assistant should answer these questions.

What is happening?

Why is it happening?

Should I care?

What strategies fit this situation?

What happened historically in similar situations?

What risks exist?

How confident is the system?

---

# High Level Architecture

Discord

│

Command Engine

│

Event Bus / Message Queue

┌──────────────┼──────────────┐

Plugins Reasoning Engine Database

│ │

└──────────────┬──────────────┘

Discord Responses

Everything communicates using events.

Nothing communicates directly.

---

# Every Feature Is A Plugin

The application should automatically discover plugins.

Adding a folder should automatically add functionality.

Example

/plugins

indicators/

strategies/

scanners/

news/

earnings/

macro/

ai/

broker/

journals/

alerts/

dashboards/

education/

commands/

integrations/

risk/

No plugin should modify core code.

---

# Universal Plugin Contract

Every plugin must implement

initialize()

shutdown()

health()

config()

permissions()

Every plugin can subscribe to events.

Every plugin can publish events.

Plugins communicate only through the Event Bus.

---

# Event Bus

Everything becomes an event.

Examples

MarketDataUpdated

PriceMoved

IndicatorCalculated

NewsReceived

EarningsReleased

TradeOpened

TradeClosed

PositionUpdated

WatchlistTriggered

StrategyMatched

BacktestFinished

JournalCreated

DailySummary

RiskWarning

The Event Bus distributes events to every interested plugin.

---

# Universal Evidence Object

Plugins do NOT return signals.

Plugins return evidence.

Example

{
"source": "EMA",

"category": "Trend",

"title": "Bullish EMA Cross",

"score": 15,

"confidence": 91,

"direction": "Bullish",

"metadata": {

"fast":20,

"slow":50

}
}

The AI combines evidence.

No plugin decides trades.

---

# Reasoning Engine

The Reasoning Engine gathers evidence from every plugin.

Example

Trend

Momentum

Relative Volume

News

Sector Strength

Macro

Options Flow

Earnings

Risk

Historical Patterns

The AI generates

Market Summary

Trade Thesis

Risk Assessment

Alternative Scenario

Confidence

Suggested Strategies

Historical Similarity

Nothing is hardcoded.

---

# Strategy Engine

Strategies are NOT Python.

Strategies are JSON or YAML recipes.

Example

Momentum.yaml

BullFlag.yaml

OpeningRangeBreakout.yaml

Example

Required

EMA Cross

Relative Volume

VWAP

Optional

Sector Strength

Positive News

Institution Buying

Minimum Score

75

Strategies only reference evidence.

---

# Interactive Strategy Builder

Strategies should be created entirely through Discord.

Example

/strategy create

Bot asks

Select Indicator

Select Condition

Select Value

Add Another Condition

Save Strategy

Strategies can be

Edited

Duplicated

Exported

Imported

Shared

Enabled

Disabled

---

# Indicator System

Indicators are plugins.

Examples

EMA

SMA

VWAP

RSI

MACD

ATR

ADX

Bollinger

Supertrend

OBV

CCI

Ichimoku

Donchian

Volume Profile

No duplicate calculations.

---

# Scanner Engine

Runs continuously.

Every minute.

Supports multiple timeframes.

Supports

Stocks

Crypto

Forex

Futures

Scanners create evidence.

Never signals.

---

# News Engine

Monitor

Reuters

Polygon

Finnhub

Yahoo Finance

SEC

MarketWatch

Government filings

Monitor

Mergers

Acquisitions

Lawsuits

FDA

CEO Changes

Buybacks

Insider Buying

Dividends

Stock Splits

Summarize with AI.

---

# Earnings Engine

Track

Upcoming Earnings

Historical Reactions

EPS

Revenue

Guidance

IV Crush

Expected Move

---

# Macro Engine

Track

FOMC

CPI

PPI

GDP

Retail Sales

Jobs

Treasury Auctions

Fed Speakers

Countdowns.

Alerts.

Impact analysis.

---

# Watchlists

Users can create multiple watchlists.

Examples

Growth

Swing

Long Term

High Volume

AI

Semiconductors

Bot automatically monitors them.

---

# Interactive Discord Messages

Every message should contain buttons.

Analyze

Chart

News

History

Backtest

Journal

Watch

Dismiss

Refresh

No wall of text.

---

# AI Trade Analysis

Every trade includes

Confidence

Reasoning

Evidence

Risk

Targets

Stop

Alternative Outcome

Historical Comparison

Education Section

---

# Replay Mode

Replay historical candles.

One candle at a time.

User chooses

Entry

Exit

Stop

Position Size

AI critiques decisions.

---

# AI Coach

Analyze every trade.

Find mistakes.

Find strengths.

Suggest improvements.

Detect recurring habits.

Generate weekly coaching reports.

---

# Journaling

Automatically store

Screenshots

Strategy

Notes

Emotion

Confidence

Market Conditions

Trade Result

Review every week.

---

# Risk Engine

Support

FTMO

Topstep

Apex

Other Prop Firms

Track

Daily Loss

Overall Drawdown

Trailing Drawdown

Consistency

Warn before violations.

---

# Backtesting

Backtest

Single Strategy

Multiple Strategies

Portfolio

Date Range

Ticker List

Timeframes

Export

CSV

HTML

Charts

Statistics

Sharpe

Sortino

Profit Factor

Win Rate

Expectancy

Drawdown

Monte Carlo

---

# Optimization Engine

Optimize

Indicator Values

Stops

Targets

Risk

Rank by

Profit

Sharpe

Consistency

Drawdown

---

# Personal Statistics

Track

Best Day

Worst Day

Best Time

Worst Time

Best Strategy

Worst Strategy

Average Hold Time

Average R

Win Rate

Emotional Trends

---

# Database

PostgreSQL

SQLAlchemy

Alembic

Repository Pattern

No raw SQL.

---

# Configuration

Everything configurable.

YAML

TOML

Environment Variables

Never hardcode settings.

---

# Logging

Everything logged.

Errors

Signals

Evidence

Commands

API Calls

Trades

Performance

---

# API

FastAPI

REST

WebSocket

Future GraphQL

---

# Security

.env

Rate Limiting

Retries

Caching

Health Checks

Graceful Shutdown

---

# Local Deployment

Runs on my laptop.

Docker Compose

Single command startup.

Automatic restart.

---

# Discord Command Examples

/help

/analyze NVDA

/news

/earnings

/watchlist

/backtest

/replay

/coach

/journal

/strategy

/scan

/settings

/performance

/risk

---

# Future Expansion

Options Flow

Dark Pool

Machine Learning Ranking

Portfolio Optimization

Voice Commands

Telegram

Slack

Web Dashboard

Mobile App

Multiple Discord Servers

Custom AI Agents

Multi-User Accounts

Cloud Sync

---

# Development Requirements

Claude Code must build the project in milestones.

Do NOT attempt to build everything at once.

At the end of every milestone:

- Run tests.
- Explain what was completed.
- Commit changes.
- Wait for approval before continuing.

Every new feature must include:

- Documentation
- Unit Tests
- Type Hints
- Logging
- Configuration
- Error Handling

Code quality should prioritize readability, modularity, and long-term maintainability over shortcuts.

The architecture should allow a new plugin, strategy, indicator, or integration to be added without modifying existing core modules.
