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
