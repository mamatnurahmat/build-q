"""Test registry PB `repo` & `github_hook` untuk `bq --cicd-webhook` (tanpa IO jaringan)."""
from __future__ import annotations

import pytest

from build_q import hook_registry, pb_api

REPO = "Qoin-Digital-Indonesia/hub-be-nonqris-manager"
URL = "https://cicd-hw.qoin.id/hook"
CICD = {"IMAGE": "hub-be-nonqris-manager", "DEPLOYMENT": "hub-be-nonqris-manager", "PROJECT": "qoinhub"}


@pytest.fixture
def pb(monkeypatch):
    """Fake PB: `records` = {collection: [record]}; `calls` mencatat request tulis."""
    state = {"records": {"repo": [], "github_hook": []}, "calls": [], "deny_delete": False,
             "branch": "main", "cicd_refs": {"main": CICD}}

    def fake_pb(method, path, body=None):
        col = path.split("/api/collections/")[1].split("/")[0]
        if method == "GET":
            return {"items": state["records"][col][:1]}
        if method == "DELETE" and state["deny_delete"]:
            raise pb_api.PBAPIError("HTTP 403: Only superusers can perform this action.")
        state["calls"].append((method, col, body))
        return {"id": "new1", **(body or {})}

    monkeypatch.setattr(pb_api, "is_enabled", lambda: True)
    monkeypatch.setattr(hook_registry, "_pb", fake_pb)
    monkeypatch.setattr(hook_registry, "_default_branch", lambda _r: state["branch"])
    monkeypatch.setattr(hook_registry, "fetch_cicd_data",
                        lambda repo, ref, path: (state["cicd_refs"].get(ref, {}), path, b"{}"))
    return state


def test_ensure_repo_creates_missing_record_from_cicd_json(pb):
    rec = hook_registry.ensure_repo(REPO, URL, URL)
    (method, col, body), = pb["calls"]
    assert (method, col, rec["id"]) == ("POST", "repo", "new1")
    assert body["name"] == body["image"] == body["deployment"] == "hub-be-nonqris-manager"
    assert body["cicd"] == CICD
    assert (body["incoming_webhook"], body["webhook_url"], body["cicd_webhook_status"]) == (True, URL, URL)


def test_ensure_repo_existing_only_syncs_webhook_fields(pb):
    pb["records"]["repo"] = [{"id": "r1", "name": "hub-be-nonqris-manager", "image": "custom",
                              "incoming_webhook": False, "webhook_url": "", "cicd_webhook_status": "NO_WEBHOOK",
                              "cicd": CICD}]
    hook_registry.ensure_repo(REPO, URL, URL)
    (method, col, body), = pb["calls"]
    assert method == "PATCH" and body == {"incoming_webhook": True, "webhook_url": URL, "cicd_webhook_status": URL}


def test_ensure_repo_cicd_only_on_develop(pb):
    pb["cicd_refs"] = {"develop": CICD}
    hook_registry.ensure_repo(REPO, URL, URL)
    assert pb["calls"][0][2]["cicd"] == CICD


def test_ensure_repo_backfills_empty_cicd(pb):
    pb["records"]["repo"] = [{"id": "r1", "name": "hub-be-nonqris-manager", "cicd": None,
                              "incoming_webhook": True, "webhook_url": URL, "cicd_webhook_status": URL}]
    pb["cicd_refs"] = {"develop": CICD}
    hook_registry.ensure_repo(REPO, URL, URL)
    (method, col, body), = pb["calls"]
    assert method == "PATCH" and body == {"cicd": CICD}


def test_ensure_repo_skips_repo_missing_on_github(pb):
    pb["branch"] = None
    assert hook_registry.ensure_repo(REPO, hook_registry.STATUS_ACCESS_DENIED) is None
    assert pb["calls"] == []


def test_ensure_repo_noop_when_pb_disabled(pb, monkeypatch):
    monkeypatch.setattr(pb_api, "is_enabled", lambda: False)
    assert hook_registry.ensure_repo(REPO, URL, URL) is None and pb["calls"] == []


def test_upsert_hook_creates_with_hmac_status(pb):
    hook = {"id": 700670793, "active": True, "events": ["push"], "config": {"url": URL},
            "last_response": {"code": 200, "status": "active", "message": "OK"}}
    hook_registry.upsert_hook({"id": "r1"}, hook, "valid", "ping 200 OK")
    (method, col, body), = pb["calls"]
    assert (method, col) == ("POST", "github_hook")
    assert (body["repo"], body["hook_id"], body["hmac_status"], body["last_response_code"]) == \
        ("r1", 700670793, "valid", 200)
    assert body["hmac_checked_at"].endswith(".000Z")


def test_upsert_hook_marks_removed_inactive_when_delete_denied(pb):
    pb["deny_delete"] = True
    pb["records"]["github_hook"] = [{"id": "g1", "hook_id": 11}]
    hook_registry.upsert_hook({"id": "r1"}, {"id": 10, "config": {"url": URL}}, "valid", "ok", [11])
    first = pb["calls"][0]
    assert first[0] == "PATCH" and first[2]["active"] is False
