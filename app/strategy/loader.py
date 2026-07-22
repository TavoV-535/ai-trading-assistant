"""
Loads every ``plugins/strategies/<name>/strategy.yaml`` into a compiled
strategy. Deliberately not the same machinery as ``app.plugins.loader``
(which imports Python ``plugin.py`` files defining a ``PluginBase``
subclass) — a strategy is pure declarative data, never Python. Adding a
new strategy is dropping a YAML file in a new folder; adding a new
indicator plugin's evidence to an existing strategy is editing that
strategy's ``required``/``optional`` lists. Neither ever touches this
module or any other Python code.

A broken strategy file (bad YAML, schema validation failure) is logged and
skipped — the same isolation policy ``PluginRegistry.load_all`` uses for a
broken plugin, so one strategy author's typo can't take down every other
strategy.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.logging import get_logger
from app.strategy.compiler import CompiledStrategy, compile_strategy
from app.strategy.models import StrategyDefinition

log = get_logger(__name__)


def load_strategies(strategies_dir: Path) -> list[CompiledStrategy]:
    """Scans ``strategies_dir`` for ``*/strategy.yaml`` files, parses and
    compiles each one. Returns an empty list (not an error) if the
    directory doesn't exist yet — mirrors how plugin search paths that
    don't exist are logged and skipped, not fatal."""
    compiled: list[CompiledStrategy] = []
    if not strategies_dir.is_dir():
        log.debug("strategy_search_path_missing", path=str(strategies_dir))
        return compiled

    for folder in sorted(p for p in strategies_dir.iterdir() if p.is_dir()):
        yaml_path = folder / "strategy.yaml"
        if not yaml_path.is_file():
            continue
        try:
            raw = yaml.safe_load(yaml_path.read_text()) or {}
            definition = StrategyDefinition(**raw)
            compiled.append(compile_strategy(definition))
            log.info(
                "strategy_loaded",
                name=definition.name,
                path=str(yaml_path),
                required=len(definition.required),
                optional=len(definition.optional),
                minimum_score=definition.minimum_score,
            )
        except Exception:
            log.exception("strategy_load_failed", path=str(yaml_path))

    return compiled
