"""Tes `bq --cicd-trigger --force` (HAG-84): flag force harus ada di body yang ditandatangani."""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from build_q import cicd_trigger

SECRET = "rahasia-uji"
COMMIT = {"sha": "a" * 40, "commit": {"message": "uji\nbaris dua", "author": {"name": "x", "email": "x@x", "date": "2026-11-03T00:00:00Z"}}}
META = {"id": 1, "name": "repo", "full_name": "org/repo", "default_branch": "main", "owner": {"login": "org"}}


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(cicd_trigger, "_fetch_repo_meta", lambda repo: META)
    monkeypatch.setattr(cicd_trigger, "_fetch_commit", lambda repo, ref: COMMIT)
    monkeypatch.setattr(cicd_trigger, "_get_hmac", lambda **kw: SECRET)

    def fake_post(url, body, sig, delivery, force=False):
        calls.append({"body": body, "sig": sig, "delivery": delivery, "force": force})
        return 200, {"ok": True, "event": "push", "result": {"commit": {"branch": "main", "sha": COMMIT["sha"]}}}

    monkeypatch.setattr(cicd_trigger, "_post", fake_post)
    return calls


def _run(force: bool) -> int:
    return cicd_trigger.run_cicd_trigger("org/repo", "main", pusher="uji", email="uji@x", force=force)


def test_force_flag_inside_signed_body(sent):
    assert _run(force=True) == 0
    call = sent[0]
    payload = json.loads(call["body"])
    assert payload["x_force_trigger"] is True
    assert len(payload["x_nonce"]) == 32
    assert isinstance(payload["x_timestamp"], int)
    # Tanda tangan dihitung dari body yang sama, jadi flag tidak bisa diubah tanpa membatalkan HMAC.
    expected = "sha256=" + hmac.new(SECRET.encode(), call["body"], hashlib.sha256).hexdigest()
    assert call["sig"] == expected
    assert call["force"] is True  # header legacy tetap dikirim lewat _post


def test_force_nonce_differs_per_call(sent):
    _run(force=True)
    _run(force=True)
    a, b = (json.loads(c["body"]) for c in sent)
    assert a["x_nonce"] != b["x_nonce"]
    assert a["x_timestamp"] <= b["x_timestamp"]
    assert sent[0]["delivery"] != sent[1]["delivery"]


def test_no_force_keeps_body_unchanged(sent):
    assert _run(force=False) == 0
    payload = json.loads(sent[0]["body"])
    assert not any(k.startswith("x_") for k in payload)
    assert sent[0]["force"] is False


def test_force_legacy_header_path_still_passed(sent):
    _run(force=True)
    assert sent[0]["force"] is True
