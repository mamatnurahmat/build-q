"""Tests for build_q.audit — shared JSON-lines audit log + secret redaction."""

import json
import logging

import pytest

from build_q.audit import redact, setup_audit_logger


class TestRedact:
    @pytest.mark.parametrize("text,secret", [
        ("token ghp_" + "a" * 36, "ghp_" + "a" * 36),
        ("key sk-or-v1-" + "b" * 32, "sk-or-v1-" + "b" * 32),
        ("AGE-SECRET-KEY-1" + "Q" * 40, "AGE-SECRET-KEY-1" + "Q" * 40),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),
        ("GITHUB_TOKEN=supersecretvalue", "supersecretvalue"),
        ("pakai PB_API_PASS: hunter2hunter2", "hunter2hunter2"),
    ])
    def test_secrets_masked(self, text, secret):
        out = redact(text)
        assert secret not in out
        assert "***" in out

    def test_plain_text_unchanged(self):
        text = "build pay-be-topup-manager v1.4.2 ke develop"
        assert redact(text) == text


class TestAuditLogger:
    @pytest.fixture
    def logger(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BQ_HOME", str(tmp_path))
        lg = logging.getLogger("bq-test-audit")
        yield lg
        for h in list(lg.handlers):
            lg.removeHandler(h)
            h.close()

    def test_writes_valid_json_with_audit_fields(self, logger, tmp_path):
        path = setup_audit_logger(logger, ".test-audit.log")
        logger.info('msg with "quotes"', extra={"audit": {"source": "tui", "exit_code": 0}})
        line = path.read_text().strip()
        entry = json.loads(line)
        assert entry["msg"] == 'msg with "quotes"'
        assert entry["source"] == "tui"
        assert entry["exit_code"] == 0
        assert path.parent == tmp_path

    def test_setup_is_idempotent(self, logger):
        setup_audit_logger(logger, ".test-audit.log")
        setup_audit_logger(logger, ".test-audit.log")
        from logging.handlers import RotatingFileHandler
        assert sum(isinstance(h, RotatingFileHandler) for h in logger.handlers) == 1
