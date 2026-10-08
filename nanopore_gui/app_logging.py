from __future__ import annotations

import logging
import faulthandler
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re

_LOGGER_NAME = "nanopore_gui"
_FATAL_HANDLE = None
_FATAL_PATH = None
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(threadName)s %(message)s"
_SECRET_QUERY = re.compile(r"([?&](?:sig|se|sp|sr|st|sv|token|code)=)[^&\s]+", re.IGNORECASE)
_AUTH_TOKEN = re.compile(r"(Authorization\s*:\s*Token\s+)[^\s]+", re.IGNORECASE)
_SECRET_FIELD = re.compile(
    r"((?:token|password|passphrase|secret|api[_-]?key|pairing[_-]?code)\s*[:=]\s*)[^\s,;|&]+",
    re.IGNORECASE,
)


class _RedactingFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)
        message = _SECRET_QUERY.sub(r"\1<redacted>", message)
        message = _AUTH_TOKEN.sub(r"\1<redacted>", message)
        return _SECRET_FIELD.sub(r"\1<redacted>", message)


def configure_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "nanopore-gui.log"
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not any(
        isinstance(handler, RotatingFileHandler)
        and Path(handler.baseFilename) == log_path
        for handler in logger.handlers
    ):
        handler = RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(_RedactingFormatter(_LOG_FORMAT))
        logger.addHandler(handler)

    return log_path


def configure_crash_diagnostics(log_dir: Path) -> Path:
    """Write fatal Python stacks to a private, per-process file."""
    global _FATAL_HANDLE, _FATAL_PATH
    if _FATAL_HANDLE is not None:
        return _FATAL_PATH
    import os
    import time
    log_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        log_dir.chmod(0o700)
    # Retain only recent sessions; never rotate the currently open fault FD.
    previous = sorted(log_dir.glob("nanopore-gui-fatal-*.log"),
                      key=lambda path: path.stat().st_mtime, reverse=True)
    for old in previous[4:]:
        old.unlink(missing_ok=True)
    path = log_dir / ("nanopore-gui-fatal-%d-%d.log" % (os.getpid(), time.time_ns()))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    _FATAL_HANDLE = os.fdopen(descriptor, "w", encoding="utf-8", buffering=1)
    _FATAL_PATH = path
    faulthandler.enable(file=_FATAL_HANDLE, all_threads=True)

    def uncaught(exc_type, value, traceback):
        logging.getLogger(_LOGGER_NAME).critical(
            "uncaught_main_exception", exc_info=(exc_type, value, traceback))
        sys.__excepthook__(exc_type, value, traceback)

    def uncaught_thread(args):
        logging.getLogger(_LOGGER_NAME).critical(
            "uncaught_thread_exception thread=%s", args.thread.name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = uncaught
    threading.excepthook = uncaught_thread
    return path
