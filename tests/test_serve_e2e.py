"""Tests for `bq --serve` E2E endpoints — /api/e2e, /api/e2e/stream, /api/e2e/scenarios.

Server sungguhan (ThreadingHTTPServer, port acak) + skenario `e2e_sim`,
jadi tidak ada GitHub/kubectl sungguhan yang tersentuh.
"""
import base64
import json
import os
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from build_q import e2e, github_api, serve, user_creds
from build_q.e2e_sim import SCENARIOS

KUBECONFIG_TEXT = "apiVersion: v1\nkind: Config\ncontexts: []\n"
USER = user_creds.UserCreds(
    user_id="u1", email="dev@qoin.id", role="view",
    secrets={"GITHUB_TOKEN": "user-gh-token", "GITHUB_ORG": "Qoin-Digital-Indonesia",
             "KUBECONFIG_READONLY_HW_B64": base64.b64encode(KUBECONFIG_TEXT.encode()).decode()},
)


@pytest.fixture
def server(tmp_path, monkeypatch):
    def fake_resolve(token, *, base_url=""):
        if token != "pb-user-token":
            raise user_creds.UserAuthError("belum login PocketBase")
        return USER
    monkeypatch.setattr(user_creds, "resolve", fake_resolve)
    monkeypatch.setattr(serve, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(serve, "CONFIG_DIR", tmp_path)
    cfg = {"host": "127.0.0.1", "allow_exec": False, "token": None}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(cfg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _post(base, path, body, pb_token="pb-user-token"):
    headers = {"Content-Type": "application/json"}
    if pb_token:
        headers["X-PB-Token"] = pb_token
    req = Request(base + path, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def _sse_events(base, path):
    events, event = [], "message"
    with urlopen(base + path, timeout=30) as r:
        for raw in r:
            line = raw.decode().rstrip("\n")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                events.append((event, json.loads(line[6:])))
                if event == "done":
                    return events
                event = "message"
    return events


def test_scenarios(server):
    with urlopen(server + "/api/e2e/scenarios", timeout=10) as r:
        data = json.loads(r.read())
    assert {s["name"] for s in data["scenarios"]} == set(SCENARIOS)


@pytest.mark.parametrize("body", [{}, {"repo": "x"}, {"simulate": "nope"},
                                  {"repo": "a b", "ref": "develop"}])
def test_e2e_bad_request(server, body):
    status, data = _post(server, "/api/e2e", body)
    assert status == 400 and "error" in data


@pytest.mark.parametrize("name", ["pipeline-build-failed", "release-live"])
def test_e2e_simulate_stream(server, name):
    """Read-only: tetap jalan walau allow_exec=False (--serve-no-exec)."""
    status, spawn = _post(server, "/api/e2e", {"simulate": name})
    assert status == 200
    events = _sse_events(server, spawn["stream_url"])
    kinds = [e for e, _ in events]
    assert [d["name"] for e, d in events if e == "stage"][:1] == ["source"]
    assert kinds[-2:] == ["report", "done"]
    report = dict(events)["report"]
    sc = SCENARIOS[name]
    assert report["simulate"] == name
    assert report["sim_problems"] == []
    assert report["break_point"] == sc.break_point
    assert report["verdict"] == sc.verdict
    assert report["verdict_line"] and report["text"]
    assert dict(events)["done"]["exit"] == report["exit_code"]


def test_e2e_stream_unknown_job(server):
    with pytest.raises(HTTPError) as ei:
        urlopen(server + "/api/e2e/stream/doesnotexist", timeout=10)
    assert ei.value.code == 404


@pytest.mark.parametrize("token", ["", "token-lain"])
def test_e2e_requires_pocketbase_login(server, token):
    status, data = _post(server, "/api/e2e", {"simulate": "release-live"}, pb_token=token)
    assert status == 401 and data["pb_login"] is True


def test_e2e_uses_only_user_credentials(server, monkeypatch):
    """Analisa sungguhan: token GitHub, kubeconfig & config dari PB user — bukan env server."""
    monkeypatch.setenv("GITHUB_TOKEN", "server-owner-token")
    monkeypatch.setenv("ARGOCD_TOKEN", "server-owner-argocd")
    seen = {}

    def fake_analyze(repo, ref, *, config, on_stage=None, **_):
        kubeconfig = e2e._KUBECONFIG.get()
        seen.update(
            gh=github_api._token(), kubeconfig=kubeconfig,
            kube_text=open(kubeconfig).read(),
            kube_mode=oct(os.stat(kubeconfig).st_mode & 0o777),
            config=config,
        )
        on_stage("source")
        return e2e.E2EReport(repo=repo, ref=ref)

    monkeypatch.setattr(e2e, "analyze", fake_analyze)
    status, spawn = _post(server, "/api/e2e", {"repo": "pay-be-x", "ref": "develop"})
    assert status == 200
    events = _sse_events(server, spawn["stream_url"])
    assert [e for e, _ in events][-2:] == ["report", "done"]
    assert seen["gh"] == "user-gh-token"
    assert seen["kube_text"] == KUBECONFIG_TEXT and seen["kube_mode"] == "0o600"
    assert not os.path.exists(seen["kubeconfig"])  # dihapus setelah job
    cfg = seen["config"]
    assert cfg["github"]["token"] == "user-gh-token"
    assert cfg["git"]["org"] == "Qoin-Digital-Indonesia"
    assert cfg["argocd"]["token"] == ""  # tidak mewarisi ARGOCD_TOKEN server
    assert cfg["argocd"]["auth_file"] == "" and cfg["argocd"]["auto_login"] is False
    # override hanya berlaku di thread job
    assert github_api._TOKEN_OVERRIDE.get() is None and e2e._KUBECONFIG.get() is None
