"""Audit log JSON-lines bersama untuk MCP server dan TUI.

Setiap baris: ``{"ts", "level", "msg", "source", ...field audit}``. Field
tambahan dikirim via ``logger.info(msg, extra={"audit": {...}})``.
File dirotasi (5 MB x 3) di ``$BQ_HOME`` atau ``~/.build-q``.
"""
from __future__ import annotations

import json
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import CONFIG_DIR

_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 3

# Pola token yang tidak boleh keluar mesin / masuk log apa adanya.
_SECRET_PATTERNS = [
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bAGE-SECRET-KEY-1[0-9A-Z]{20,}\b"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASS(?:WORD)?|API_KEY)[A-Z0-9_]*\s*[=:]\s*)\S+"),
]


def redact(text: str) -> str:
    """Samarkan token/secret yang dikenali di dalam teks bebas."""
    for pat in _SECRET_PATTERNS:
        if pat.groups:
            text = pat.sub(lambda m: m.group(1) + "***", text)
        else:
            text = pat.sub("***", text)
    return text


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "audit", None) or {})
        return json.dumps(entry, default=str, ensure_ascii=False)


def audit_dir() -> Path:
    return Path(os.environ.get("BQ_HOME", str(CONFIG_DIR))).expanduser()


def setup_audit_logger(logger: logging.Logger, filename: str) -> Path:
    """Pasang handler audit (idempotent) ke ``logger``; return path file log."""
    log_dir = audit_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / filename

    for h in logger.handlers:
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == log_file:
            return log_file

    handler = RotatingFileHandler(
        str(log_file), maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8",
    )
    handler.setFormatter(JsonLineFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return log_file
