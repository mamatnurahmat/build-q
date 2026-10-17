"""Test `bq auth` — token store ~/.build-q/auth.json & get_token (tanpa IO jaringan)."""
from __future__ import annotations

import base64
import json
import stat
import time

import pytest

from build_q import pb_api, pb_auth

BASE = "https://pb.example/devops"


def _jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(pb_api, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(pb_api, "AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setattr(pb_api, "CACHE_FILE", tmp_path / ".pb-cache.json")
    monkeypatch.setenv("PB_API_URL", BASE)
    for k in ("PB_API", "PB_API_USER", "PB_API_PASS"):
        monkeypatch.delenv(k, raising=False)

    def no_http(*a, **k):
        raise AssertionError("HTTP sungguhan dipanggil dalam test")
    monkeypatch.setattr(pb_api, "_http_json", no_http)


REC = {"id": "u1", "email": "dev@qoin.id", "role": "view", "verified": True, "tokenKey": "rahasia"}


def test_save_auth_perm_600_and_strips_record():
    exp = int(time.time()) + 5 * 86400
    pb_api.save_auth(BASE + "/", _jwt(exp), REC, method="oidc")
    assert stat.S_IMODE(pb_api.AUTH_FILE.stat().st_mode) == 0o600
    data = pb_api.read_auth()
    assert data["exp"] == exp and data["pb_url"] == BASE
    assert "tokenKey" not in data["record"] and data["record"]["email"] == "dev@qoin.id"


def test_get_token_prefers_user_token(monkeypatch):
    monkeypatch.setenv("PB_API_USER", "admin")
    monkeypatch.setenv("PB_API_PASS", "x")
    tok = _jwt(int(time.time()) + 5 * 86400)
    pb_api.save_auth(BASE, tok, REC, method="oidc")
    assert pb_api.get_token() == tok


def test_get_token_other_url_falls_back_to_superuser(monkeypatch):
    pb_api.save_auth("https://lain/devops", _jwt(int(time.time()) + 86400 * 5), REC, method="oidc")
    monkeypatch.setenv("PB_API_USER", "admin")
    monkeypatch.setenv("PB_API_PASS", "x")
    monkeypatch.setattr(pb_api, "authenticate", lambda b, u, p: "su-token")
    assert pb_api.get_token() == "su-token"


def test_near_expiry_refreshes(monkeypatch):
    pb_api.save_auth(BASE, _jwt(int(time.time()) + 3600), REC, method="oidc")
    fresh = _jwt(int(time.time()) + 5 * 86400)
    calls = []

    def fake(method, url, **kw):
        calls.append(url)
        return {"token": fresh, "record": REC}
    monkeypatch.setattr(pb_api, "_http_json", fake)
    assert pb_api.get_token() == fresh
    assert calls == [f"{BASE}/api/collections/devops_users/auth-refresh"]
    assert pb_api.read_auth()["token"] == fresh


def test_refresh_rejected_means_login_again(monkeypatch):
    pb_api.save_auth(BASE, _jwt(int(time.time()) + 3600), REC, method="oidc")

    def reject(*a, **k):
        raise pb_api.PBAPIError("HTTP 401: invalid token")
    monkeypatch.setattr(pb_api, "_http_json", reject)
    with pytest.raises(pb_api.PBAPIError, match="bq auth login"):
        pb_api.get_token()


def test_refresh_network_error_keeps_old_token(monkeypatch):
    old = _jwt(int(time.time()) + 3600)
    pb_api.save_auth(BASE, old, REC, method="oidc")

    def down(*a, **k):
        raise pb_api.PBAPIError("network: timed out")
    monkeypatch.setattr(pb_api, "_http_json", down)
    assert pb_api.get_token() == old


def test_expired_and_no_creds():
    with pytest.raises(pb_api.PBAPIError, match="belum login"):
        pb_api.get_token()
    pb_api.save_auth(BASE, _jwt(int(time.time()) - 10), REC, method="oidc")
    with pytest.raises(pb_api.PBAPIError, match="kedaluwarsa"):
        pb_api.get_token()


def test_is_enabled(monkeypatch):
    assert not pb_api.is_enabled()
    pb_api.save_auth(BASE, _jwt(int(time.time()) + 86400 * 5), REC, method="oidc")
    assert pb_api.is_enabled()
    monkeypatch.setenv("PB_API", "false")
    assert not pb_api.is_enabled()


class TestLoginSSO:
    PROVIDER = {"name": "oidc", "displayName": "GitHub (Dex)", "codeVerifier": "ver",
                "authURL": "https://dex/auth?client_id=pocketbase&state=old&redirect_uri="}

    def _run(self, monkeypatch, oauth_msg):
        posted, opened = [], []
        token = _jwt(int(time.time()) + 5 * 86400)

        def fake_http(method, url, body=None, headers=None, timeout=10):
            if url.endswith("/auth-methods"):
                return {"oauth2": {"providers": [self.PROVIDER]}}
            posted.append((url, body))
            if url.endswith("/auth-with-oauth2"):
                return {"token": token, "record": REC}
            return {}

        def fake_reader(base, events, stop):
            events.put(("PB_CONNECT", json.dumps({"clientId": "cid123"})))
            events.put(("@oauth2", json.dumps(oauth_msg)))

        monkeypatch.setattr(pb_api, "_http_json", fake_http)
        monkeypatch.setattr(pb_auth, "_realtime_reader", fake_reader)
        monkeypatch.setattr(pb_auth.webbrowser, "open", opened.append)
        return pb_auth.login_sso(BASE), posted, opened, token

    def test_success_saves_token(self, monkeypatch):
        auth, posted, opened, token = self._run(monkeypatch, {"state": "cid123", "code": "c0de"})
        assert auth["token"] == token and pb_api.read_auth()["record"]["email"] == "dev@qoin.id"
        assert posted[0] == (f"{BASE}/api/realtime", {"clientId": "cid123", "subscriptions": ["@oauth2"]})
        url, body = posted[1]
        assert url.endswith("/collections/devops_users/auth-with-oauth2")
        assert body == {"provider": "oidc", "code": "c0de", "codeVerifier": "ver",
                        "redirectURL": f"{BASE}/api/oauth2-redirect", "createData": {"role": "view"}}
        assert "state=cid123" in opened[0] and "oauth2-redirect" in opened[0]

    def test_state_mismatch_rejected(self, monkeypatch):
        with pytest.raises(pb_api.PBAPIError, match="state"):
            self._run(monkeypatch, {"state": "lain", "code": "c0de"})
        assert pb_api.read_auth() is None
