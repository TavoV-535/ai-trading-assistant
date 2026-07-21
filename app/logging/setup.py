"""
Central logging configuration.

Everything gets logged: errors, evidence, commands, API calls, trades,
performance. This module wires stdlib ``logging`` (rotating file handler +
console handler) together with ``structlog`` so every log line carries
structured key/value context instead of ad-hoc string formatting.

Usage
-----
    from app.logging import configure_logging, get_logger

    configure_logging(settings)          # once, at process startup
    log = get_logger(__name__)
    log.info("plugin_loaded", plugin="ema", version="0.1.0")
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog

_CONFIGURED = False


def configure_logging(settings: Any) -> None:
    """Configure stdlib logging + structlog once per process.

    ``settings`` is the app :class:`~app.config.Settings` object (typed as
    ``Any`` here to avoid a circular import between config and logging).
    Safe to call more than once — subsequent calls are no-ops.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_cfg = settings.logging
    level = getattr(logging, str(log_cfg.level).upper(), logging.INFO)

    log_dir = Path(log_cfg.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    root.addHandler(console_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=log_cfg.max_bytes,
        backupCount=log_cfg.backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    # error.log gets ERROR+ only, so incident review doesn't have to wade
    # through INFO noise.
    error_handler = logging.handlers.RotatingFileHandler(
        log_dir / "error.log",
        maxBytes=log_cfg.max_bytes,
        backupCount=log_cfg.backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    root.addHandler(error_handler)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if log_cfg.json_logs:
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    for handler in (console_handler, file_handler, error_handler):
        handler.setFormatter(formatter)

    _CONFIGURED = True
    get_logger(__name__).info(
        "logging_configured",
        level=log_cfg.level,
        json=log_cfg.json_logs,
        log_dir=str(log_dir),
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structured logger bound to ``name`` (usually ``__name__``)."""
    return structlog.get_logger(name)


def reset_logging_state() -> None:
    """Test helper — allows ``configure_logging`` to run again in tests."""
    global _CONFIGURED
    _CONFIGURED = False
