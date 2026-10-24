"""Tests for build_q.argocd_auth — login SSO ArgoCD, prioritas token PB → lokal, inline argocd CLI.

Tanpa jaringan, tanpa browser, tanpa menyentuh ~/.build-q (dialihkan ke tmp).
"""
from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import time

import pytest

from build_q import argocd_auth, config, e2e, pb_api
from build_q.argocd_auth import ArgocdAuthError
from build_q.pb_api import PBAPIError

API_URL = "https://cicd-hw.qoin.id/argocd"

CLI_CONFIG = """\
contexts:
- name: cicd-hw.qoin.id
  server: cicd-hw.qoin.id
  user: cicd-hw.qoin.id
current-context: cicd-hw.qoin.id
users:
- name: cicd-hw.qoin.id
  auth-token: {token}
"""


def _jwt(exp: int) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'HS256'})}.{enc({'exp': exp, 'sub': 'mamat'})}.sig"


FUTURE = _jwt(int(time.time()) + 3600 * 12)
PAST = _jwt(int(time.time()) - 3600)
REAL_PUSH = argocd_auth.push_pb_secret  # fixture `pushed` me-mock nama ini; tes push memakai aslinya


@pytest.fixture
def pushed(monkeypatch):
    """Rekam token yang didorong ke secret PB (tanpa jaringan)."""
    calls = []
    monkeypatch.setattr(argocd_auth, "push_pb_secret", lambda token: calls.append(token) or "created")
    return calls


@pytest.fixture
def env(tmp_path, monkeypatch, pushed):
    store_dir = tmp_path / ".build-q"
    monkeypatch.setattr(pb_api, "CONFIG_DIR", store_dir)
    monkeypatch.setattr(pb_api, "AUTH_FILE", store_dir / "auth.json")
    monkeypatch.delenv("ARGOCD_CLI", raising=False)
    monkeypatch.setattr(argocd_auth.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    return tmp_path


def _store():
    return json.loads(pb_api.AUTH_FILE.read_text(encoding="utf-8"))


def _config(token: str = "", auth_file: str | None = None):
    return {"argocd": {"api_url": API_URL, "token": token, "auto_login": True,
                       "auth_file": str(pb_api.AUTH_FILE) if auth_file is None else auth_file}}


# ── Helper dasar ─────────────────────────────────────────────────────────

def test_login_target_splits_host_and_root_path():
    assert argocd_auth._login_target(API_URL) == ("cicd-hw.qoin.id", "/argocd")
    assert argocd_auth._login_target("https://argo.example") == ("argo.example", "")


def test_login_target_rejects_invalid_url():
    with pytest.raises(ArgocdAuthError):
        argocd_auth._login_target("bukan-url")


def test_read_cli_token_uses_current_context(env):
    path = env / "config"
    path.write_text(CLI_CONFIG.format(token="eyJ.cli.token"), encoding="utf-8")
    assert argocd_auth.read_cli_token(str(path)) == "eyJ.cli.token"


def test_read_cli_token_empty_when_no_user(env):
    path = env / "empty.yaml"
    path.write_text("current-context: x\nusers: []\n", encoding="utf-8")
    assert argocd_auth.read_cli_token(str(path)) == ""


def test_is_expired_detects_jwt_exp():
    assert argocd_auth.is_expired(PAST) is True
    assert argocd_auth.is_expired(FUTURE) is False
    assert argocd_auth.is_expired("bukan-jwt") is False  # tanpa exp dianggap belum kedaluwarsa


def test_is_expired_applies_margin():
    almost = _jwt(int(time.time()) + 60)  # tersisa 1 menit < margin 5 menit
    assert argocd_auth.is_expired(almost) is True


# ── Prioritas token: PB/env → lokal ──────────────────────────────────────

def test_resolve_prefers_config_token_pb_over_local(env):
    local = _jwt(int(time.time()) + 9000)
    argocd_auth.save_token(local)
    assert argocd_auth.resolve_token(_config(token=FUTURE)) == FUTURE  # PB menang selama valid


def test_resolve_falls_back_to_local_when_pb_expired(env):
    local = _jwt(int(time.time()) + 7200)
    argocd_auth.save_token(local)
    assert argocd_auth.resolve_token(_config(token=PAST)) == local


def test_resolve_empty_when_everything_expired(env):
    argocd_auth.save_token(PAST)
    assert argocd_auth.resolve_token(_config(token=PAST)) == ""


def test_resolve_ignores_local_store_when_auth_file_disabled(env):
    # server (user_config): auth_file kosong → tidak boleh membaca file lokal
    argocd_auth.save_token(FUTURE)
    assert argocd_auth.resolve_token(_config(token="", auth_file="")) == ""


# ── Penyimpanan: lokal + secret PB ───────────────────────────────────────

def test_save_token_writes_local_store_mode_600_and_pushes_pb(env, pushed):
    pb_api.write_auth({"OTHER": "keep"})
    result = argocd_auth.save_token(FUTURE)
    assert result == {"local": str(pb_api.AUTH_FILE), "pb": "created"}
    assert _store() == {"OTHER": "keep", "ARGOCD_TOKEN": FUTURE}
    assert stat.S_IMODE(os.stat(pb_api.AUTH_FILE).st_mode) == 0o600
    assert pushed == [FUTURE]
    assert not (env / ".pi").exists()


def test_save_token_pb_failure_keeps_local(env, monkeypatch):
    def down(token):
        raise PBAPIError("belum login PocketBase")

    monkeypatch.setattr(argocd_auth, "push_pb_secret", down)
    result = argocd_auth.save_token(FUTURE)
    assert result["pb"].startswith("gagal: belum login")
    assert argocd_auth.local_token() == FUTURE


def test_push_pb_secret_creates_when_missing(env, monkeypatch):
    calls = []

    def fake_request(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return {"items": []}
        return {}

    monkeypatch.setattr(argocd_auth, "_pb_request", fake_request)
    monkeypatch.setattr(pb_api, "read_auth", lambda: {"record": {"email": "mamat@qoin.id"}})
    assert REAL_PUSH(FUTURE) == "created"
    method, path, body = calls[-1]
    assert (method, path) == ("POST", "/api/collections/secrets/records")
    assert body["key"] == "ARGOCD_TOKEN" and body["value"] == FUTURE
    assert body["scope"] == "argocd" and body["env"] == "all" and body["type"] == "token"
    assert body["active"] is True and body["owner"] == "mamat@qoin.id"
    assert body["expires_at"] and body["rotated_at"]


def test_push_pb_secret_updates_existing_record(env, monkeypatch):
    calls = []

    def fake_request(method, path, body=None):
        calls.append((method, path))
        if method == "GET":
            return {"items": [{"id": "rec123"}]}
        return {}

    monkeypatch.setattr(argocd_auth, "_pb_request", fake_request)
    monkeypatch.setattr(pb_api, "read_auth", lambda: None)
    assert REAL_PUSH(FUTURE) == "updated"
    assert calls[-1] == ("PATCH", "/api/collections/secrets/records/rec123")
    assert "filter=" in calls[0][1] and "ARGOCD_TOKEN" in calls[0][1]


# ── PB login/logout tidak menghapus token lokal ──────────────────────────

def test_pb_login_keeps_argocd_token(env):
    argocd_auth.save_token(FUTURE)
    pb_api.save_auth("https://pb.example", "a.b.c", {"id": "u1", "email": "x@qoin.id"}, method="sso")
    data = _store()
    assert data["ARGOCD_TOKEN"] == FUTURE
    assert data["token"] == "a.b.c"


def test_pb_logout_keeps_argocd_token_and_removes_session(env):
    argocd_auth.save_token(FUTURE)
    pb_api.save_auth("https://pb.example", "a.b.c", {"id": "u1"}, method="sso")
    assert pb_api.clear_auth() is True
    assert _store() == {"ARGOCD_TOKEN": FUTURE}
    assert pb_api.read_auth() is None


def test_pb_logout_deletes_file_when_nothing_left(env):
    pb_api.save_auth("https://pb.example", "a.b.c", {"id": "u1"}, method="sso")
    assert pb_api.clear_auth() is True
    assert not pb_api.AUTH_FILE.exists()


def test_token_only_file_does_not_enable_pb_api(env, monkeypatch):
    monkeypatch.delenv("PB_API", raising=False)
    argocd_auth.save_token(FUTURE)
    assert pb_api.is_enabled() is False


# ── Verifikasi & login SSO ───────────────────────────────────────────────

def test_verify_token_requires_logged_in(monkeypatch):
    class Res:
        def __init__(self, body): self._body = body
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(argocd_auth, "urlopen", lambda req, timeout: Res(b"{}"))
    with pytest.raises(ArgocdAuthError, match="loggedIn=false"):
        argocd_auth.verify_token(API_URL, "tok")

    body = b'{"loggedIn": true, "username": "mamat@qoin.id", "groups": ["ngentools:argocd-admin"]}'
    monkeypatch.setattr(argocd_auth, "urlopen", lambda req, timeout: Res(body))
    assert argocd_auth.verify_token(API_URL, "tok")["username"] == "mamat@qoin.id"


def _fake_login(calls, *, rc=0, token=FUTURE):
    """Simulasikan `argocd login --config <path>`: tulis config sementara lalu return rc."""
    def run(cmd, timeout):
        calls.append(cmd)
        if rc == 0:
            path = cmd[cmd.index("--config") + 1]
            with open(path, "w", encoding="utf-8") as f:
                f.write(CLI_CONFIG.format(token=token))
        return subprocess.CompletedProcess(cmd, rc)
    return run


def test_sso_login_uses_temp_config_and_saves_everywhere(env, monkeypatch, pushed):
    calls, cfg_seen = [], []
    run = _fake_login(calls)

    def spy(cmd, timeout):
        cfg_seen.append(cmd[cmd.index("--config") + 1])
        return run(cmd, timeout)

    monkeypatch.setattr(argocd_auth.subprocess, "run", spy)
    monkeypatch.setattr(argocd_auth, "verify_token",
                        lambda api_url, token: {"loggedIn": True, "username": "mamat@qoin.id"})

    info, saved = argocd_auth.sso_login(_config(), port=8085)

    assert info["username"] == "mamat@qoin.id"
    assert saved == {"local": str(pb_api.AUTH_FILE), "pb": "created"}
    assert calls[0][:2] == ["/usr/local/bin/argocd", "login"]
    assert "--grpc-web-root-path" in calls[0] and "/argocd" in calls[0]
    assert "--sso-port" in calls[0] and "8085" in calls[0]
    # config sementara: tidak di ~/.config/argocd dan sudah dihapus
    assert cfg_seen[0] and ".config/argocd" not in cfg_seen[0]
    assert not os.path.exists(os.path.dirname(cfg_seen[0]))
    assert _store()["ARGOCD_TOKEN"] == FUTURE
    assert pushed == [FUTURE]


def test_sso_login_no_browser_flag(env, monkeypatch):
    calls = []
    monkeypatch.setattr(argocd_auth.subprocess, "run", _fake_login(calls))
    monkeypatch.setattr(argocd_auth, "verify_token", lambda api_url, token: {"loggedIn": True})
    argocd_auth.sso_login(_config(), launch_browser=False)
    assert "--sso-launch-browser=false" in calls[0]


def test_sso_login_failure_does_not_save(env, monkeypatch, pushed):
    monkeypatch.setattr(argocd_auth.subprocess, "run", _fake_login([], rc=1))
    with pytest.raises(ArgocdAuthError, match="exit 1"):
        argocd_auth.sso_login(_config())
    assert not pb_api.AUTH_FILE.exists()
    assert pushed == []


def test_sso_login_fails_when_userinfo_rejects(env, monkeypatch, pushed):
    monkeypatch.setattr(argocd_auth.subprocess, "run", _fake_login([]))

    def reject(api_url, token):
        raise ArgocdAuthError("userinfo loggedIn=false — token tidak valid")

    monkeypatch.setattr(argocd_auth, "verify_token", reject)
    with pytest.raises(ArgocdAuthError):
        argocd_auth.sso_login(_config())
    assert not pb_api.AUTH_FILE.exists()
    assert pushed == []


def test_sso_login_missing_cli(env, monkeypatch):
    monkeypatch.setattr(argocd_auth.shutil, "which", lambda name: None)
    with pytest.raises(ArgocdAuthError, match="argocd CLI tidak ditemukan"):
        argocd_auth.sso_login(_config())


def test_ensure_token_returns_active_token_without_login(env, monkeypatch):
    monkeypatch.setattr(argocd_auth, "sso_login", lambda *a, **k: pytest.fail("tidak perlu login"))
    assert argocd_auth.ensure_token(_config(token=FUTURE)) == FUTURE


class _Tty:
    def __init__(self, tty): self._tty = tty
    def isatty(self): return self._tty


def test_ensure_token_non_tty_raises_with_hint(env, monkeypatch):
    monkeypatch.setattr(argocd_auth.sys, "stdin", _Tty(False))
    with pytest.raises(ArgocdAuthError, match="bq --argocd-auth-sso"):
        argocd_auth.ensure_token(_config(token=PAST))


def test_ensure_token_tty_triggers_login_and_refreshes(env, monkeypatch):
    monkeypatch.setattr(argocd_auth.sys, "stdin", _Tty(True))
    seen = []

    def fake_login(config, **kw):
        seen.append(True)
        argocd_auth.save_token(FUTURE)
        return {}, {"local": "", "pb": "updated"}

    monkeypatch.setattr(argocd_auth, "sso_login", fake_login)
    assert argocd_auth.ensure_token(_config(token=PAST)) == FUTURE
    assert seen == [True]


# ── Pemakaian inline official argocd CLI ─────────────────────────────────

def test_run_argocd_passes_token_via_env_not_argv(env, monkeypatch):
    seen = {}

    def fake_run(cmd, env=None):
        seen["cmd"], seen["env"] = cmd, env
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(argocd_auth.subprocess, "run", fake_run)
    rc = argocd_auth.run_argocd(["app", "list"], _config(token=FUTURE))

    assert rc == 0
    assert seen["cmd"] == ["/usr/local/bin/argocd", "--server", "cicd-hw.qoin.id", "--grpc-web",
                           "--grpc-web-root-path", "/argocd", "app", "list"]
    assert FUTURE not in " ".join(seen["cmd"])
    assert seen["env"]["ARGOCD_AUTH_TOKEN"] == FUTURE


def test_cli_main_usage_and_error_exit_codes(env, monkeypatch):
    assert argocd_auth.cli_main([], _config(token=FUTURE)) == 2
    monkeypatch.setattr(argocd_auth.sys, "stdin", _Tty(False))
    assert argocd_auth.cli_main(["app", "list"], _config(token=PAST)) == 1


# ── Config & e2e ─────────────────────────────────────────────────────────

def test_build_config_ignores_external_auth_file_env(env, monkeypatch):
    monkeypatch.setenv("ARGOCD_AUTH_FILE", str(env / "pi-auth.json"))
    monkeypatch.delenv("ARGOCD_TOKEN", raising=False)
    cfg = config.build_config(lambda key, default=None: os.environ.get(key, default or ""))
    assert cfg["argocd"]["auth_file"] == str(pb_api.AUTH_FILE)


def test_e2e_argocd_token_uses_resolve_priority(env):
    argocd_auth.save_token(FUTURE)
    assert e2e._argocd_token(_config(token=PAST)) == FUTURE  # PB kedaluwarsa → lokal
    assert e2e._argocd_token(_config(token="", auth_file="")) == ""  # server: tanpa file lokal
