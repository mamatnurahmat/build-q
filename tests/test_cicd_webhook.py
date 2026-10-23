"""Test `bq --cicd-webhook` — HMAC dari PB, satu hook https, verifikasi ping, registry PB (tanpa IO jaringan)."""
from __future__ import annotations

import json

import pytest

from build_q import cicd_webhook, github_api, hook_registry, pb_api

REPO = "Qoin-Digital-Indonesia/hub-be-nonqris-manager"
URL = cicd_webhook.HOOK_URL_DEFAULT
HTTP_URL = URL.replace("https://", "http://")


@pytest.fixture
def env(monkeypatch):
    """PB, kubectl, GitHub, dan registry di-mock. Atur perilaku lewat dict `state`."""
    state = {"pb": "hmac-valid", "service": "hmac-valid", "hooks": [], "calls": [],
             "ping_codes": [200], "registry": []}
    for k in (cicd_webhook.HMAC_ENV_KEY, "INCOMING_WEBHOOK_HMAC"):
        monkeypatch.delenv(k, raising=False)

    def fake_pull(force=False):
        if isinstance(state["pb"], Exception):
            raise state["pb"]
        return {cicd_webhook.HMAC_ENV_KEY: state["pb"]} if state["pb"] else {}

    pings = {"sent": 0}

    def fake_request(method, path, body=None, **_):
        state["calls"].append((method, path, json.loads(body) if body else None))
        if path.endswith("/pings"):
            pings["sent"] += 1
            return None
        if "/deliveries" in path:
            codes = state["ping_codes"][:pings["sent"]]
            return [{"id": i, "event": "ping", "status_code": c, "status": "x", "duration": 0.5}
                    for i, c in reversed(list(enumerate(codes, 1)))]
        if method == "GET" and "/hooks/" in path:
            hid = int(path.rsplit("/", 1)[-1])
            return next((h for h in state["hooks"] if h["id"] == hid), None)
        return {"id": 1, "events": cicd_webhook.DEFAULT_EVENTS, "config": {"url": URL}}

    monkeypatch.setattr(pb_api, "is_enabled", lambda: True)
    monkeypatch.setattr(pb_api, "pull", fake_pull)
    monkeypatch.setattr(cicd_webhook, "_kubectl_read_secret", lambda *a, **k: state["service"])
    monkeypatch.setattr(cicd_webhook, "_list_hooks", lambda _repo: state["hooks"])
    monkeypatch.setattr(cicd_webhook, "_maybe_open_browser", lambda *_: None)
    monkeypatch.setattr(cicd_webhook.time, "sleep", lambda *_: None)
    monkeypatch.setattr(github_api, "_request", fake_request)
    monkeypatch.setattr(hook_registry, "ensure_repo",
                        lambda repo, status, url="": state["registry"].append(("repo", status)) or {"id": "r1"})
    monkeypatch.setattr(hook_registry, "upsert_hook",
                        lambda rec, hook, st, detail, removed=(): state["registry"].append(
                            ("hook", hook["id"], st, list(removed))))
    return state


def _hook(hid=700670793, code=200, url=URL, active=True):
    return {"id": hid, "active": active, "events": cicd_webhook.DEFAULT_EVENTS,
            "config": {"url": url}, "last_response": {"code": code}}


def _writes(state):
    return [(m, p) for m, p, _ in state["calls"] if m != "GET" and not p.endswith("/pings")]


# ── Resolusi HMAC ────────────────────────────────────────────────────────

def test_resolve_prefers_pb(env):
    assert cicd_webhook._resolve_hmac() == ("hmac-valid", f"PB secrets/{cicd_webhook.HMAC_ENV_KEY}")


def test_resolve_pb_mismatch_uses_service(env):
    env["pb"] = "hmac-lama"
    assert cicd_webhook._resolve_hmac() == ("hmac-valid", cicd_webhook.HMAC_SERVICE_REF)


def test_resolve_pb_down_falls_back_env(env, monkeypatch):
    env["pb"], env["service"] = pb_api.PBAPIError("network: timeout"), None
    monkeypatch.setenv(cicd_webhook.HMAC_ENV_KEY, "hmac-env\n")
    assert cicd_webhook._resolve_hmac() == ("hmac-env", "env")


# ── Satu hook https ──────────────────────────────────────────────────────

def test_matching_covers_http_and_prefers_https():
    hooks = [_hook(1, url=HTTP_URL), _hook(2, url="https://lain.example/hook"), _hook(3)]
    assert [h["id"] for h in cicd_webhook._find_all_matching(hooks, URL)] == [3, 1]


def test_check_removes_duplicates_and_keeps_https(env):
    env["hooks"] = [_hook(10), _hook(11, url=HTTP_URL), _hook(12, code=None)]
    assert cicd_webhook.run_cicd_webhook_check(REPO) == 0
    assert _writes(env) == [("DELETE", f"/repos/{REPO}/hooks/12"), ("DELETE", f"/repos/{REPO}/hooks/11")]
    assert env["registry"] == [("repo", URL), ("hook", 10, "valid", [12, 11])]


def test_check_http_only_hook_converted_to_https(env):
    env["hooks"] = [_hook(20, url=HTTP_URL)]
    assert cicd_webhook.run_cicd_webhook_check(REPO) == 0
    (method, path, body), = [c for c in env["calls"] if c[0] == "PATCH"]
    assert path == f"/repos/{REPO}/hooks/20/config"
    assert body["url"] == URL and body["secret"] == "hmac-valid"


def test_readonly_check_never_writes(env):
    env["hooks"] = [_hook(30), _hook(31, url=HTTP_URL)]
    assert cicd_webhook.run_cicd_webhook_check(REPO, auto_setup=False) == 0
    assert _writes(env) == [] and env["registry"] == []
    assert not any(p.endswith("/pings") for _, p, _ in env["calls"])


# ── Verifikasi ping & sync HMAC ──────────────────────────────────────────

def test_check_ping_403_syncs_secret_then_valid(env):
    env["hooks"], env["ping_codes"] = [_hook(40, code=None)], [403, 200]
    assert cicd_webhook.run_cicd_webhook_check(REPO) == 0
    assert _writes(env) == [("PATCH", f"/repos/{REPO}/hooks/40/config")]
    assert env["registry"][-1] == ("hook", 40, "valid", [])


def test_check_ping_stays_403_is_invalid(env):
    env["hooks"], env["ping_codes"] = [_hook(41)], [403, 403]
    assert cicd_webhook.run_cicd_webhook_check(REPO) == 1
    assert env["registry"][-1] == ("hook", 41, "invalid", [])


def test_check_ping_network_error_retried(env):
    env["hooks"], env["ping_codes"] = [_hook(42)], [502, 200]
    assert cicd_webhook.run_cicd_webhook_check(REPO) == 0
    assert _writes(env) == []
    assert env["registry"][-1] == ("hook", 42, "valid", [])


def test_readonly_check_reports_403_without_patch(env):
    env["hooks"] = [_hook(43, code=403)]
    assert cicd_webhook.run_cicd_webhook_check(REPO, auto_setup=False) == 1
    assert _writes(env) == []


# ── Setup (create) & registry ────────────────────────────────────────────

def test_setup_creates_hook_verifies_and_registers(env):
    assert cicd_webhook.run_cicd_webhook_setup(REPO) == 0
    method, path, body = env["calls"][0]
    assert (method, path) == ("POST", f"/repos/{REPO}/hooks")
    assert body["config"] == {"url": URL, "content_type": "json", "secret": "hmac-valid", "insecure_ssl": "0"}
    assert env["registry"] == [("repo", URL), ("hook", 1, "valid", [])]


def test_setup_without_any_hmac_registers_no_webhook(env):
    env["pb"], env["service"] = None, None
    assert cicd_webhook.run_cicd_webhook_setup(REPO) == 1
    assert _writes(env) == []
    assert env["registry"] == [("repo", hook_registry.STATUS_NO_WEBHOOK)]


def test_check_access_denied_registers_status(env, monkeypatch):
    def denied(_repo):
        raise github_api.GitHubAPIError("HTTP 404: Not Found")
    monkeypatch.setattr(cicd_webhook, "_list_hooks", denied)
    assert cicd_webhook.run_cicd_webhook_check(REPO) == 2
    assert env["registry"] == [("repo", hook_registry.STATUS_ACCESS_DENIED)]
