"""Test gate login bq (v0.2.0): perintah wajib login dengan token yang valid. Tanpa jaringan."""
from __future__ import annotations

import base64
import json
import time

import pytest

from build_q import pb_api, pb_auth

BASE = "https://pb.example/devops"


def _jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


REC = {"id": "u1", "email": "dev@qoin.id", "role": "admin", "verified": True}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(pb_api, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(pb_api, "AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setenv("PB_API_URL", BASE)

    def no_http(*a, **k):
        raise AssertionError("HTTP tidak boleh dipanggil")
    monkeypatch.setattr(pb_api, "_http_json", no_http)
    return tmp_path


def _save(exp_offset=5 * 86400, verified_at=None):
    tok = _jwt(int(time.time()) + exp_offset)
    pb_api.save_auth(BASE, tok, REC, method="oidc")
    if verified_at is not None:
        a = pb_api.read_auth()
        a["verified_at"] = verified_at
        pb_api.write_auth(a)
    return tok


def test_requires_login_flags():
    assert not pb_auth.requires_login(["--help"])
    assert not pb_auth.requires_login(["--version"])
    assert not pb_auth.requires_login(["-h"])
    assert not pb_auth.requires_login(["--doctor"])
    assert pb_auth.requires_login(["plus-be-x", "sandbox", "--cloud"])
    assert pb_auth.requires_login([])  # `bq` tanpa argumen = build otomatis


def test_no_login_blocks(isolated):
    ok, msg = pb_auth.check_login()
    assert ok is False and "bq auth login" in msg


def test_expired_session_blocks(isolated):
    _save(exp_offset=-10)
    ok, msg = pb_auth.check_login()
    assert ok is False and "kedaluwarsa" in msg


def test_rejected_by_server_blocks(isolated, monkeypatch):
    _save()
    monkeypatch.setattr(pb_api, "user_token", lambda base=None: None)
    ok, msg = pb_auth.check_login()
    assert ok is False and "ditolak" in msg


def test_recently_verified_skips_network(isolated, monkeypatch):
    _save(verified_at=int(time.time()))
    monkeypatch.setattr(pb_api, "user_token", lambda base=None: pb_api.read_auth()["token"])
    monkeypatch.setattr(pb_auth, "verify", lambda a: pytest.fail("verify tidak boleh dipanggil"))
    ok, _ = pb_auth.check_login()
    assert ok is True


def test_stale_verification_checks_server_and_stamps(isolated, monkeypatch):
    _save(verified_at=0)
    monkeypatch.setattr(pb_api, "user_token", lambda base=None: pb_api.read_auth()["token"])
    calls = []
    monkeypatch.setattr(pb_auth, "verify", lambda a: calls.append(1) or {"id": "u1"})
    ok, _ = pb_auth.check_login()
    assert ok is True and calls == [1]
    assert pb_api.read_auth()["verified_at"] >= int(time.time()) - 5


def test_server_401_on_verify_blocks(isolated, monkeypatch):
    _save(verified_at=0)
    monkeypatch.setattr(pb_api, "user_token", lambda base=None: pb_api.read_auth()["token"])

    def reject(a):
        raise pb_api.PBAPIError("HTTP 401: invalid token")
    monkeypatch.setattr(pb_auth, "verify", reject)
    ok, msg = pb_auth.check_login()
    assert ok is False and "ditolak" in msg


def test_network_down_keeps_local_session_with_warning(isolated, monkeypatch):
    _save(verified_at=0)
    monkeypatch.setattr(pb_api, "user_token", lambda base=None: pb_api.read_auth()["token"])

    def down(a):
        raise pb_api.PBAPIError("network: timed out")
    monkeypatch.setattr(pb_auth, "verify", down)
    ok, msg = pb_auth.check_login()
    assert ok is True and "tidak terjangkau" in msg


def test_require_login_exits_when_blocked(isolated):
    with pytest.raises(SystemExit) as e:
        pb_auth.require_login()
    assert e.value.code == 1


def test_require_login_passes_when_valid(isolated, monkeypatch):
    _save(verified_at=int(time.time()))
    monkeypatch.setattr(pb_api, "user_token", lambda base=None: pb_api.read_auth()["token"])
    pb_auth.require_login()  # tidak raise
