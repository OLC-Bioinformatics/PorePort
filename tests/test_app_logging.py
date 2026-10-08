"""Tests for the GUI diagnostic log formatter and file configuration."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from nanopore_gui.app_logging import _RedactingFormatter, configure_logging


@pytest.fixture
def clean_gui_logger():
    """Keep tests from leaking handlers into other tests or the user's logs."""
    logger = logging.getLogger("nanopore_gui")
    previous_handlers = logger.handlers[:]
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.handlers = []
    try:
        yield logger
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logger.handlers = previous_handlers
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def _format(message, *args):
    record = logging.LogRecord("nanopore_gui.api", logging.INFO, __file__, 1,
                               message, args, None)
    return _RedactingFormatter("%(message)s").format(record)


def test_log_formatter_redacts_credentials_in_status_messages():
    formatted = _format(
        "message=worker failed token=abc123 password=hunter2 pairing_code=998877"
    )
    assert "worker failed" in formatted
    for secret in ("abc123", "hunter2", "998877"):
        assert secret not in formatted
    assert formatted.count("<redacted>") == 3


@pytest.mark.parametrize("key", ["sig", "se", "sp", "sr", "st", "sv", "token", "code"])
def test_sas_query_fields_are_redacted_without_erasing_safe_fields(key):
    formatted = _format("https://blob.test/data?%s=SECRET123&filename=reads.pod5", key)
    assert "SECRET123" not in formatted
    assert "filename=reads.pod5" in formatted
    assert "%s=<redacted>" % key in formatted


@pytest.mark.parametrize("field", [
    "token", "password", "passphrase", "secret", "api_key", "api-key", "pairing_code",
])
def test_named_secret_fields_are_redacted_case_insensitively(field):
    formatted = _format("Request %s: PRIVATE987, status=ready", field.upper())
    assert "PRIVATE987" not in formatted
    assert "status=ready" in formatted
    assert "<redacted>" in formatted


def test_authorization_header_and_formatted_args_are_redacted():
    formatted = _format("Authorization: Token %s", "BEARER_SECRET")
    assert "BEARER_SECRET" not in formatted
    assert "Authorization: Token <redacted>" in formatted


def test_non_secret_context_is_preserved():
    formatted = _format("run_id=1835 status=complete filename=reads.pod5")
    assert formatted == "run_id=1835 status=complete filename=reads.pod5"


def test_configure_logging_creates_rotating_handler_and_is_idempotent(
    clean_gui_logger, tmp_path
):
    log_dir = tmp_path / "nested" / "logs"
    log_path = configure_logging(log_dir)
    assert log_path == log_dir / "nanopore-gui.log"
    assert log_path.is_file()
    assert clean_gui_logger.level == logging.INFO
    assert clean_gui_logger.propagate is False
    assert len(clean_gui_logger.handlers) == 1
    handler = clean_gui_logger.handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 3
    assert handler.encoding == "utf-8"
    assert isinstance(handler.formatter, _RedactingFormatter)
    assert configure_logging(log_dir) == log_path
    assert clean_gui_logger.handlers == [handler]


def test_configured_log_file_redacts_formatted_child_logger_messages(
    clean_gui_logger, tmp_path
):
    log_path = configure_logging(tmp_path / "logs")
    logging.getLogger("nanopore_gui.api").info(
        "run_id=%s url=https://blob.test/file?sig=%s password=%s",
        1835, "SAS_SECRET", "PASS_SECRET",
    )
    for handler in clean_gui_logger.handlers:
        handler.flush()
    contents = log_path.read_text(encoding="utf-8")
    assert "run_id=1835" in contents
    assert "SAS_SECRET" not in contents
    assert "PASS_SECRET" not in contents
    assert "sig=<redacted>" in contents
    assert "password=<redacted>" in contents


def test_configure_logging_adds_one_handler_per_distinct_log_directory(
    clean_gui_logger, tmp_path
):
    first = configure_logging(tmp_path / "first")
    second = configure_logging(tmp_path / "second")
    assert first != second
    assert len(clean_gui_logger.handlers) == 2
    assert {Path(handler.baseFilename) for handler in clean_gui_logger.handlers} == {
        first, second
    }


def test_private_per_session_fatal_log_and_retention(tmp_path, monkeypatch):
    import os
    from nanopore_gui import app_logging
    import faulthandler
    monkeypatch.setattr(faulthandler, "enable", lambda **kwargs: None)
    monkeypatch.setattr(app_logging, "_FATAL_HANDLE", None)
    monkeypatch.setattr(app_logging, "_FATAL_PATH", None)
    folder = tmp_path / "logs"
    folder.mkdir()
    for index in range(7):
        path = folder / ("nanopore-gui-fatal-old%d.log" % index)
        path.write_text("old")
        os.utime(path, (index, index))
    path = app_logging.configure_crash_diagnostics(folder)
    assert path.exists()
    assert app_logging.configure_crash_diagnostics(folder) == path
    assert len(list(folder.glob("nanopore-gui-fatal-*.log"))) == 5
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert folder.stat().st_mode & 0o777 == 0o700
    app_logging._FATAL_HANDLE.close()
    monkeypatch.setattr(app_logging, "_FATAL_HANDLE", None)
    monkeypatch.setattr(app_logging, "_FATAL_PATH", None)
