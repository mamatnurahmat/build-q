"""Tests for `bq --manifest-check <namespace>` (build_q/manifest_scan.py) + endpoint web-nya.

Dunia tiruan `e2e_sim` (GitHub + cluster), jadi tidak ada jaringan sungguhan.
"""
import copy
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from build_q import e2e, github_api, manifest_scan, serve, user_creds
from build_q.e2e_sim import (GITOPS_REPO, SAMPLE_APP, SIM_CONFIG, SIM_REPO, healthy_world, patched,
                             service_file, service_yaml, sync_kustomization)

from .test_serve_e2e import _post as _post_json, _sse_events


def _scan(world, ns="develop-qoin", **kw):
    with patched(world):
        return manifest_scan.scan(ns, config=copy.deepcopy(SIM_CONFIG), **kw)


def _item(rep, app=SIM_REPO):
    return next(i for i in rep.items if i.app == app)


def ambiguous_bootstrap():
    """Data cluster dgn namespace `develop-payout` di 2 cluster — menguji mekanisme namespace ambigu
    (data nyata sudah tidak ambigu)."""
    from unittest import mock

    from build_q import kube_contexts

    data = copy.deepcopy(kube_contexts._BOOTSTRAP)
    data["hw-pro-p"]["namespaces"].append("develop-payout")
    return mock.patch.object(kube_contexts, "load", lambda **k: (data, "bootstrap"))


class TestScan:
    def test_healthy_namespace(self):
        rep = _scan(healthy_world())
        assert [i.app for i in rep.items] == [SIM_REPO, SAMPLE_APP]
        assert rep.totals == {"ok": 2, "fail": 0, "skip": 0, "total": 2}
        assert rep.exit_code == 0 and rep.branch == "develop"
        assert rep.secret_source.startswith("cluster hw-dev/develop-qoin")
        assert json.loads(json.dumps(rep.to_dict()))["exit_code"] == 0

    def test_failure_rendered_and_exported(self, tmp_path):
        world = healthy_world()
        world.files[(GITOPS_REPO, service_file())] = service_yaml().replace("role: front", "role: back")
        rep = _scan(world)
        assert _item(rep).status == "fail" and _item(rep).critical == ["service_selector_no_match"]
        assert rep.exit_code == 1 and rep.totals["fail"] == 1
        text = manifest_scan.render_text(rep)
        assert "❌ FAIL" in text and f"{SIM_REPO}: service_selector_no_match" in text
        md = manifest_scan.render_markdown(rep)
        assert "## Ringkasan" in md and "## Daftar Deployment" in md
        assert f"### ❌ FAIL `{SIM_REPO}`" in md and "```yaml" in md and "role: front" in md
        paths = manifest_scan.export(rep, md_path=str(tmp_path / "r.md"), json_path=str(tmp_path / "x/r.json"))
        assert [p.name for p in paths] == ["r.md", "r.json"]
        data = json.loads((tmp_path / "x/r.json").read_text())
        assert data["totals"]["fail"] == 1 and data["items"][0]["findings"][0]["rule_name"] == "service_selector_no_match"
        assert manifest_scan.default_report_path(rep, "md", tmp_path).name.startswith("manifest-develop-qoin-")

    def test_missing_service_suggested_from_sample(self):
        world = healthy_world()
        del world.files[(GITOPS_REPO, service_file())]
        sync_kustomization(world)
        rep = _scan(world)
        titles = [s["title"] for s in _item(rep).suggestions]
        assert any(f"dari sample main:cce/develop-qoin/{SAMPLE_APP}_deployment.yaml" in t for t in titles)

    def test_cluster_unreachable_is_skip(self):
        world = healthy_world()
        world.kubectl_error = "Unable to connect to the server: timeout"
        rep = _scan(world)
        assert rep.totals["skip"] == 2 and rep.exit_code == 2
        assert rep.secret_source.startswith("repo GitOps")
        assert any("Application ArgoCD tidak terbaca" in d for d in rep.details)

    def test_argocd_destination_picks_cluster(self):
        world = healthy_world()
        world.apps[0]["spec"]["destination"] = {"server": "https://10.6.20.45:5443"}
        assert "hw-pro-u/develop-qoin" in _scan(world).secret_source

    def test_ambiguous_namespace_uses_cluster_with_most_deployments(self, monkeypatch):
        world = healthy_world(ns="develop-payout")

        def names(ctx, ns, kind):
            if kind == "deployments":
                return [SIM_REPO, SAMPLE_APP] if ctx == "hw-pro-p" else ["lain"]
            return world.secrets

        with patched(world), ambiguous_bootstrap():
            monkeypatch.setattr(e2e, "kubectl_names", names)
            rep = manifest_scan.scan("develop-payout", config=copy.deepcopy(SIM_CONFIG))
        assert "hw-pro-p/develop-payout" in rep.secret_source
        assert "hw-pro-p/develop-payout: 2 dari 2 Deployment ada di sini" in rep.details

    def test_default_branch_follows_env(self):
        rep = _scan(healthy_world(ref="v1.4.2", ns="production-qoin"), ns="production-qoin")
        assert rep.branch == "main"

    @pytest.mark.parametrize("ns", ["Bad NS", "", "a/b"])
    def test_invalid_namespace(self, ns):
        with pytest.raises(manifest_scan.ManifestScanError):
            manifest_scan.scan(ns, config=copy.deepcopy(SIM_CONFIG))

    def test_empty_folder(self):
        with pytest.raises(manifest_scan.ManifestScanError, match="tidak ada"):
            _scan(healthy_world(), ns="develop-kosong")

    def test_progress_and_user_token_reach_worker_threads(self, monkeypatch):
        world = healthy_world()
        seen, progress = [], []
        orig = world.gh_raw

        def gh_raw(repo, path, ref):
            seen.append(github_api._TOKEN_OVERRIDE.get())
            return orig(repo, path, ref)

        world.gh_raw = gh_raw
        with e2e.use_credentials(github_token="user-tok", kubeconfig=""):
            _scan(world, on_progress=lambda d, t, a: progress.append((d, t)))
        assert seen and set(seen) == {"user-tok"}
        assert sorted(progress)[-1] == (2, 2) and len(progress) == 2


# ─── Web: /api/manifest-check ─────────────────────────────────────────────

USER = user_creds.UserCreds(user_id="u1", email="dev@qoin.id", role="view",
                            secrets={"GITHUB_TOKEN": "user-gh-token", "GITHUB_ORG": "Qoin-Digital-Indonesia"})


@pytest.fixture
def server(tmp_path, monkeypatch):
    def fake_resolve(token, *, base_url=""):
        if token != "pb-user-token":
            raise user_creds.UserAuthError("belum login PocketBase")
        return USER
    monkeypatch.setattr(user_creds, "resolve", fake_resolve)
    monkeypatch.setattr(serve, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(serve, "CONFIG_DIR", tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(
        {"host": "127.0.0.1", "allow_exec": False, "token": None}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _post(base, body, pb_token="pb-user-token"):
    return _post_json(base, "/api/manifest-check", body, pb_token=pb_token)


@pytest.mark.parametrize("body", [{}, {"namespace": "Bad NS"}, {"namespace": "develop-qoin", "branch": "a b"}])
def test_web_bad_request(server, body):
    status, data = _post(server, body)
    assert status == 400 and "error" in data


def test_web_requires_pocketbase_login(server):
    status, data = _post(server, {"namespace": "develop-qoin"}, pb_token="")
    assert status == 401 and data["pb_login"] is True


def test_web_stream_report_with_markdown(server, monkeypatch):
    world = healthy_world()
    seen = {}
    orig = world.gh_raw

    def gh_raw(repo, path, ref):
        seen["token"] = github_api._TOKEN_OVERRIDE.get()
        return orig(repo, path, ref)

    world.gh_raw = gh_raw
    monkeypatch.setattr(user_creds, "user_config", lambda creds: copy.deepcopy(SIM_CONFIG))
    with patched(world):
        status, spawn = _post(server, {"namespace": "develop-qoin"})
        assert status == 200 and spawn["stream_url"].startswith("/api/manifest-check/stream/")
        events = _sse_events(server, spawn["stream_url"])
    kinds = [e for e, _ in events]
    assert kinds.count("progress") == 2 and kinds[-2:] == ["report", "done"]
    report = dict(events)["report"]
    assert report["totals"]["ok"] == 2 and report["markdown"].startswith("# Laporan Manifest")
    assert dict(events)["done"]["exit"] == report["exit_code"] == 0
    assert seen["token"] == "user-gh-token"  # credential user, bukan env server


def test_tui_render_keeps_severity_brackets(monkeypatch, tmp_path):
    """`[warn]` tidak boleh hilang sebagai markup Rich; laporan diekspor ke REPORTS_DIR."""
    from rich.console import Console

    from build_q import tui

    world = healthy_world()
    world.files[(GITOPS_REPO, service_file())] = service_yaml().replace("role: front", "role: back")
    console = Console(record=True, width=160)
    monkeypatch.setattr(tui, "console", console)
    monkeypatch.setattr(tui, "REPORTS_DIR", tmp_path)
    report = _scan(world)
    monkeypatch.setattr(manifest_scan, "scan", lambda ns, **kw: report)
    tui.run_manifest_check("develop-qoin")
    out = console.export_text()
    assert "service_selector_no_match [critical]×1" in out
    assert sorted(p.suffix for p in tmp_path.iterdir()) == [".json", ".md"]
