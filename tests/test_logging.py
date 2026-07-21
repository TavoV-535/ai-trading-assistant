from __future__ import annotations

from pathlib import Path

from app.logging.setup import configure_logging, get_logger, reset_logging_state


def test_configure_logging_creates_log_files(tmp_path: Path, settings, monkeypatch):
    settings.logging.log_dir = str(tmp_path / "logs")
    monkeypatch.chdir(tmp_path)
    reset_logging_state()

    configure_logging(settings)
    log = get_logger("test.logger")
    log.info("hello_from_test", value=1)
    log.error("error_from_test", reason="testing")

    app_log = tmp_path / "logs" / "app.log"
    error_log = tmp_path / "logs" / "error.log"
    assert app_log.exists()
    assert error_log.exists()
    assert "hello_from_test" in app_log.read_text()
    assert "error_from_test" in error_log.read_text()
    # INFO-level lines should NOT leak into error.log
    assert "hello_from_test" not in error_log.read_text()

    reset_logging_state()


def test_configure_logging_is_idempotent(tmp_path: Path, settings, monkeypatch):
    settings.logging.log_dir = str(tmp_path / "logs2")
    monkeypatch.chdir(tmp_path)
    reset_logging_state()

    configure_logging(settings)
    configure_logging(settings)  # second call should be a no-op, not raise
    get_logger("test.logger2").info("still_works")

    reset_logging_state()
