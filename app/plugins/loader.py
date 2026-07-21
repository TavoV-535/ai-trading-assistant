"""
Plugin auto-discovery.

Adding a folder under one of the configured search paths (see
``config/default.yaml`` → ``plugins.search_paths``) automatically adds
functionality — nothing needs to be registered by hand.

Convention: each plugin lives in its own directory containing a
``plugin.py`` module with exactly one class inheriting
:class:`~app.plugins.base.PluginBase`. An optional ``config.yaml`` alongside
it supplies that plugin's own configuration.

    plugins/indicators/ema/
        plugin.py     # class EMAPlugin(PluginBase): ...
        config.yaml   # optional — fast: 20, slow: 50
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.logging import get_logger
from app.plugins.base import PluginBase

log = get_logger(__name__)


@dataclass
class DiscoveredPlugin:
    plugin_class: type[PluginBase]
    module_path: Path
    plugin_config: dict[str, Any]
    category: str


def _load_module(plugin_py: Path) -> Any:
    # unique module name so plugins in different folders never collide,
    # even if two folders happen to both be named e.g. "plugin.py"
    module_name = f"_ata_plugin_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, plugin_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin module at {plugin_py}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_plugin_config(config_yaml: Path) -> dict[str, Any]:
    if not config_yaml.exists():
        return {}
    with config_yaml.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        log.warning("plugin_config_not_a_mapping", path=str(config_yaml))
        return {}
    return data


def discover_plugins(
    search_paths: list[str],
    project_root: Path,
    disabled: list[str] | None = None,
) -> list[DiscoveredPlugin]:
    """Scan every configured search path and return every plugin found.

    Does not instantiate or initialize anything — that's
    :class:`~app.plugins.registry.PluginRegistry`'s job. A folder that fails
    to import is logged and skipped; it never aborts discovery of the rest.
    """
    disabled_set = set(disabled or [])
    discovered: list[DiscoveredPlugin] = []

    for rel_path in search_paths:
        base_dir = (project_root / rel_path).resolve()
        if not base_dir.is_dir():
            log.debug("plugin_search_path_missing", path=str(base_dir))
            continue

        category = base_dir.name  # e.g. "indicators", "strategies"

        for plugin_dir in sorted(p for p in base_dir.iterdir() if p.is_dir()):
            if plugin_dir.name.startswith((".", "_")):
                continue
            if plugin_dir.name in disabled_set:
                log.info("plugin_disabled_by_config", plugin=plugin_dir.name)
                continue

            plugin_py = plugin_dir / "plugin.py"
            if not plugin_py.exists():
                continue

            try:
                module = _load_module(plugin_py)
            except Exception:
                log.exception("plugin_import_failed", path=str(plugin_py))
                continue

            plugin_classes = [
                obj
                for _, obj in inspect.getmembers(module, inspect.isclass)
                if issubclass(obj, PluginBase) and obj is not PluginBase and obj.__module__ == module.__name__
            ]

            if not plugin_classes:
                log.warning("plugin_no_class_found", path=str(plugin_py))
                continue
            if len(plugin_classes) > 1:
                log.warning(
                    "plugin_multiple_classes_found",
                    path=str(plugin_py),
                    classes=[c.__name__ for c in plugin_classes],
                )

            plugin_class = plugin_classes[0]
            if plugin_class.name in disabled_set:
                log.info("plugin_disabled_by_config", plugin=plugin_class.name)
                continue

            plugin_config = _load_plugin_config(plugin_dir / "config.yaml")

            discovered.append(
                DiscoveredPlugin(
                    plugin_class=plugin_class,
                    module_path=plugin_py,
                    plugin_config=plugin_config,
                    category=category,
                )
            )
            log.debug(
                "plugin_discovered",
                plugin=plugin_class.name,
                category=category,
                path=str(plugin_py),
            )

    log.info("plugin_discovery_complete", count=len(discovered))
    return discovered
