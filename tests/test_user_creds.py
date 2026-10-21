"""Tests for build_q.user_creds — credential per-request dari PocketBase user (stateless)."""
import base64
import json
import os

import pytest

from build_q import e2e, github_api, pb_api, user_creds
from build_q.pb_api import PBAPIError


def _jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'HS256'})}.{enc(claims)}.sig"


TOKEN = _jwt({"id": "u1", "type": "auth", "collectionId": "c1", "exp": 9999999999})


@pytest.fixture
def pb(monkeypatch):
    calls = []

    def fake_http(method, url, *, headers=None, **_):
        calls.append((method, url, headers))
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            raise PBAPIError("HTTP 401: invalid token")
        if "/records/u1" in url:
            return {"id": "u1", "email": "dev@qoin.id", "role": "view"}
        return {"items": [{"key": "GITHUB_TOKEN", "value": "gh-user"},
                          {"key": "KUBECONFIG_A_B64", "value": base64.b64encode(b"a: 1").decode()},
                          {"key": "KUBECONFIG_RUSAK_B64", "value": "!!bukan-base64"}]}

    monkeypatch.setattr(pb_api, "_http_json", fake_http)
    monkeypatch.setattr(pb_api, "_write_cache", lambda *_: pytest.fail("tidak boleh menulis cache"))
    monkeypatch.setattr(pb_api, "save_auth", lambda *_, **__: pytest.fail("tidak boleh menulis auth.json"))
    return calls


def test_resolve_fetches_user_secrets_with_user_token(pb):
    creds = user_creds.resolve(TOKEN, base_url="https://pb.example")
    assert (creds.email, creds.role, creds.get("GITHUB_TOKEN")) == ("dev@qoin.id", "view", "gh-user")
    assert all(h["Authorization"] == f"Bearer {TOKEN}" for _, _, h in pb)
    assert all(u.startswith("https://pb.example/") for _, u, _ in pb)
    assert "token" not in repr(creds) and "gh-user" not in repr(creds)


@pytest.mark.parametrize("token", ["", "bukan-jwt", _jwt({"id": "u1", "type": "auth"})])
def test_resolve_rejects_bad_token(pb, token):
    with pytest.raises(user_creds.UserAuthError):
        user_creds.resolve(token, base_url="https://pb.example")


def test_user_config_ignores_server_env(pb, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "server-token")
    monkeypatch.setenv("JX_KUBE_CONTEXT", "server-ctx")
    cfg = user_creds.user_config(user_creds.resolve(TOKEN, base_url="https://pb.example"))
    assert cfg["github"]["token"] == "gh-user"
    assert cfg["webhook"]["k8s_context"] == "hw-dev"  # default, bukan env server
    assert cfg["argocd"]["auth_file"] == "" and cfg["argocd"]["auto_login"] is False


def test_kubeconfig_path_is_temporary(pb):
    creds = user_creds.resolve(TOKEN, base_url="https://pb.example")
    with user_creds.kubeconfig_path(creds) as path:
        assert os.pathsep not in path  # value rusak diabaikan
        assert open(path, "rb").read() == b"a: 1"
        assert os.stat(os.path.dirname(path)).st_mode & 0o777 == 0o700
    assert not os.path.exists(path)
    with user_creds.kubeconfig_path(user_creds.UserCreds("u", "e", "view")) as empty:
        assert empty == ""


def test_no_fallback_when_user_lacks_credentials(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "server-token")
    with e2e.use_credentials(github_token="", kubeconfig=""):
        with pytest.raises(github_api.GitHubAPIError, match="PocketBase"):
            github_api._token()
        with pytest.raises(e2e.KubectlError, match="PocketBase"):
            e2e.kubectl_json("hw-dev", ["get", "pods"])
        assert e2e.kubectl_logs("hw-dev", "ns", "pod", "c") == ""


def test_kubectl_env_is_minimal(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "server-token")
    with e2e.use_credentials(github_token="t", kubeconfig="/tmp/x"):
        env = e2e._kubectl_env()
        base = github_api._api_base()
    assert env["KUBECONFIG"] == "/tmp/x" and "GITHUB_TOKEN" not in env
    assert base == "https://api.github.com" or base.startswith("http")
