from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re

_LOGGER_NAME = "nanopore_gui"
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
