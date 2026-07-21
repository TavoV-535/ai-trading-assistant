from __future__ import annotations

from pathlib import Path

import pytest

from app.plugins import PluginRegistry
from app.plugins.loader import discover_plugins

_GOOD_PLUGIN_SRC = '''
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

class GoodPlugin(PluginBase):
    name = "good-plugin"
    version = "1.0.0"
    category = "test"

    async def initialize(self) -> None:
        self.received = self.context.plugin_config.get("greeting", "none")

    async def shutdown(self) -> None:
        pass

    async def health(self) -> PluginHealth:
        return PluginHealth(status="healthy")

    def config(self) -> dict:
        return {"greeting": self.received}

    def permissions(self) -> list:
        return [PluginPermission.EVENTS_PUBLISH]
'''

_BROKEN_PLUGIN_SRC = '''
from app.plugins.base import PluginBase, PluginHealth

class BrokenPlugin(PluginBase):
    name = "broken-plugin"
    version = "1.0.0"
    category = "test"

    async def initialize(self) -> None:
        raise RuntimeError("intentional failure for isolation test")

    async def shutdown(self) -> None:
        pass

    async def health(self) -> PluginHealth:
        return PluginHealth(status="unhealthy")

    def config(self) -> dict:
        return {}

    def permissions(self) -> list:
        return []
'''


def _write_plugin(base_dir: Path, category: str, plugin_name: str, source: str, config_yaml: str | None = None) -> None:
    plugin_dir = base_dir / category / plugin_name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(source, encoding="utf-8")
    if config_yaml is not None:
        (plugin_dir / "config.yaml").write_text(config_yaml, encoding="utf-8")


@pytest.fixture
def plugin_project(tmp_path: Path) -> Path:
    _write_plugin(tmp_path, "indicators", "good", _GOOD_PLUGIN_SRC, "greeting: hello\n")
    _write_plugin(tmp_path, "indicators", "broken", _BROKEN_PLUGIN_SRC)
    return tmp_path


def test_discover_plugins_finds_both(plugin_project: Path):
    discovered = discover_plugins(["indicators"], plugin_project)
    names = sorted(d.plugin_class.name for d in discovered)
    assert names == ["broken-plugin", "good-plugin"]


def test_discover_plugins_loads_config_yaml(plugin_project: Path):
    discovered = discover_plugins(["indicators"], plugin_project)
    good = next(d for d in discovered if d.plugin_class.name == "good-plugin")
    assert good.plugin_config == {"greeting": "hello"}


def test_discover_plugins_respects_disabled_list(plugin_project: Path):
    discovered = discover_plugins(["indicators"], plugin_project, disabled=["good-plugin"])
    names = [d.plugin_class.name for d in discovered]
    assert "good-plugin" not in names
    assert "broken-plugin" in names


async def test_registry_isolates_failed_plugin(plugin_project: Path, event_bus, settings):
    settings.plugins.search_paths = ["indicators"]
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(plugin_project)

    assert "good-plugin" in registry.plugins
    assert "broken-plugin" not in registry.plugins
    assert "broken-plugin" in registry.failed
    assert "intentional failure" in registry.failed["broken-plugin"]

    health = await registry.health_check_all()
    assert health["good-plugin"].status == "healthy"

    await registry.shutdown_all()
