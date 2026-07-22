# Writing a plugin

No plugin ever modifies core code. Adding a folder is the whole
integration surface. This guide builds a tiny indicator plugin from
scratch — the same pattern applies to strategies, scanners, news sources,
broker integrations, anything under `/plugins`.

## 1. Pick a category folder and create your plugin's folder

```
plugins/indicators/rsi/
    plugin.py
    config.yaml     # optional
```

The category (`indicators`) must be one of `config.plugins.search_paths` in
`config/default.yaml`. The plugin folder name (`rsi`) is just for humans —
what matters is the `name` attribute on your class.

## 2. Implement the contract

```python
# plugins/indicators/rsi/plugin.py
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.plugins.base import PluginBase, PluginHealth, PluginPermission


class RSIPlugin(PluginBase):
    """14-period RSI with overbought/oversold evidence."""

    name = "RSI"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._period = int(context.plugin_config.get("period", 14))
        self._subscription = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)

    async def shutdown(self) -> None:
        if self._subscription:
            self._subscription.unsubscribe()

    async def health(self) -> PluginHealth:
        return PluginHealth(status="healthy")

    def config(self) -> dict:
        return {"period": self._period}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        rsi_value = ...  # your calculation
        await self.context.event_bus.publish(
            IndicatorCalculated(source=self.name, symbol=event.symbol, indicator="RSI", value=rsi_value)
        )
        if rsi_value > 70:
            await self.context.event_bus.publish(
                EvidenceProduced(
                    source=self.name,
                    evidence=Evidence(
                        source=self.name,
                        category=EvidenceCategory.MOMENTUM,
                        title="RSI Overbought",
                        score=10,
                        confidence=70,
                        direction="bearish",
                        symbol=event.symbol,
                        metadata={"rsi": rsi_value, "period": self._period},
                    ),
                )
            )
```

Rules the loader enforces:

- Exactly one `PluginBase` subclass per `plugin.py`
- `name` should be unique across all plugins (a collision is logged and the
  second one is skipped)
- All five contract methods are required — the loader will refuse to
  instantiate a class missing any of them (Python's `abstractmethod` won't
  let you construct it)

## 3. Optional: `config.yaml`

```yaml
# plugins/indicators/rsi/config.yaml
period: 14
```

Loaded automatically into `context.plugin_config`. This is what
"Configuration over Code" means at the plugin level — never hardcode a
tunable value in `plugin.py`.

## 3.5. Use `app.indicators`, don't recalculate

`PROJECT.md`'s Indicator System explicitly says "no duplicate
calculations" — if you're writing a new indicator plugin, the RSI example
above is illustrative, but the calculation itself (`rsi_value = ...`)
should never be hand-rolled in `plugin.py`. Use the shared library instead:

- `app.indicators.bar.SymbolWindow` — a bounded per-symbol rolling history
  of bars (default 300). Call `.append(bar_from_event(event))` on every
  `MarketDataUpdated`, then read `.closes` / `.highs` / `.lows` /
  `.volumes`.
- `app.indicators.math` — pure calculation functions (`sma`, `ema_step`,
  `rsi`, `macd`, `atr`, `adx`, `bollinger_bands`, `donchian_channel`,
  `supertrend`, `obv`, `vwap`, `volume_profile`, `cci`, `ichimoku`). Every
  one returns `None` when there isn't enough history yet instead of
  raising — check for that before publishing evidence, the same way
  `EMAPlugin` checks `state.updates < 2`.

See `plugins/indicators/rsi/plugin.py` (and any of its 12 siblings added in
Milestone 3) for the real, current version of the pattern sketched above —
in particular, note that it fires evidence only on the RSI *crossing* the
70/30 threshold (edge-triggered), not on every update where RSI happens to
already be above 70, so a symbol sitting in overbought territory doesn't
spam a fresh piece of evidence on every tick.

## 4. What you get for free

- **Auto-discovery** — drop the folder in, restart the app, it's live. No
  registration step anywhere else in the codebase.
- **Isolation** — if `initialize()` raises, your plugin is excluded and
  logged (`registry.failed["RSI"]`); nothing else breaks.
- **Health reporting** — `GET /plugins` and `GET /health` surface whatever
  your `health()` returns.
- **Everything logged** — every event you publish is automatically
  persisted to `event_log` by the database's event logger; you don't need
  to log evidence yourself.

## 5. What you must never do

- Never import another plugin's module directly. If you need something
  another plugin knows, subscribe to the event it publishes.
- Never import `app.db` and write your own queries from inside a plugin
  in this milestone's pattern — future milestones may expose a
  plugin-scoped repository; for now, evidence and events are the interface.
- Never return a trade decision. Evidence only — `direction` +
  `confidence` + `metadata` is the full vocabulary. The Reasoning Engine
  decides what evidence means; a plugin never does.

## 6. Test it

```python
# tests/test_rsi_plugin.py
async def test_rsi_publishes_evidence_on_overbought(event_bus, settings):
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)
    assert "RSI" in registry.plugins
    # feed MarketDataUpdated events, assert on published EvidenceProduced
```

See `tests/test_ema_plugin.py` for a complete worked example against the
reference `EMA` plugin.

---

# Writing a Discord command plugin

A `/slash-command` is a plugin too — the same auto-discovery, the same
isolation, the same "drop a folder in" rule. The only difference is which
base class you extend and which folder you use.

## 1. Create the folder under `plugins/commands/`

```
plugins/commands/echo/
    plugin.py
```

## 2. Extend `DiscordCommandPlugin` instead of `PluginBase`

It's the same five-method contract, plus one more: `execute()`.

```python
# plugins/commands/echo/plugin.py
from typing import Any

from app.discord.command_plugin import DiscordCommandPlugin
from app.discord.dispatch import CommandContext, CommandResponse
from app.plugins.base import PluginHealth, PluginPermission


class EchoPlugin(DiscordCommandPlugin):
    """Replies with whatever the user asked."""

    name = "Echo"
    version = "0.1.0"
    category = "commands"
    command_name = "echo"                 # this becomes /echo
    command_description = "Repeats back a message."

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def health(self) -> PluginHealth:
        return PluginHealth(status="healthy")

    def config(self) -> dict[str, Any]:
        return {}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_PUBLISH]

    async def execute(self, ctx: CommandContext) -> CommandResponse:
        return CommandResponse(content=f"You are user {ctx.user_id}")
```

That's the whole integration. The bot finds it via the same
`PluginRegistry` used for every other plugin category, registers it as a
slash command in `setup_hook()`, and every invocation is automatically
logged as a `CommandInvoked` event.

## 3. What's different from an indicator/scanner plugin

- `command_name` must be lowercase, 1-32 characters, letters/numbers/
  underscore/hyphen only (`app.discord.is_valid_command_name` — an invalid
  name is logged and skipped, not fatal).
- `execute()` receives a `CommandContext` (`user_id`, `guild_id`,
  `channel_id`, `args`) — never the raw discord.py `Interaction`. This is
  what keeps command plugins testable without a live Discord connection.
- `execute()` returns a `CommandResponse(content=..., ephemeral=...)`. In
  Milestone 2, commands take no parameters — parameterized commands (like
  `/analyze NVDA`) are a Milestone 3 extension to `DiscordCommandPlugin`.
- If `execute()` raises, `dispatch_command` catches it, publishes
  `CommandFailed`, and returns a generic apology to the user — your plugin
  never needs its own top-level try/except for this.

## 4. Test it without Discord

```python
async def test_echo_command(event_bus, settings):
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)
    echo = registry.get("Echo")

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={})
    response = await dispatch_command(echo, event_bus, ctx)

    assert response.content == "You are user 1"
```

See `tests/test_ping_plugin.py` and `tests/test_discord_dispatch.py` for
complete worked examples, including how `tests/test_discord_bot.py` verifies
the bridge to a real `discord.Interaction` with a lightweight fake object —
without ever needing a network connection to Discord.
