"""Tests for build_q.e2e + build_q.e2e_sim — analisa CI/CD end-to-end & simulasinya.

Dunia tiruan (GitHub + Tekton + ArgoCD) berasal dari `build_q.e2e_sim`, sama
dengan yang dipakai `bq --e2e --simulate`, jadi test dan simulasi tidak
bisa berbeda arah.
"""
import json

import pytest

from build_q import e2e, e2e_sim
from build_q.e2e_sim import SCENARIOS, SIM_CONFIG, SIM_SHA, healthy_world, patched


@pytest.fixture(autouse=True)
def no_real_io(monkeypatch):
    """Pastikan tidak ada test yang menyentuh GitHub/kubectl sungguhan."""
    def boom(*a, **k):
        raise AssertionError("IO sungguhan dipanggil dalam test")
    monkeypatch.setattr("build_q.github_api._request", boom)
    monkeypatch.setattr("subprocess.run", boom)


def _analyze(world):
    with patched(world):
        return e2e.analyze(world.repo, world.ref, config=SIM_CONFIG)


class TestHelpers:
    @pytest.mark.parametrize("ref,tag", [("develop", "c0ffee1"), ("v1.2.2-sls", "v1.2.2-sls"),
                                         ("refs/tags/v2.0.0", "v2.0.0")])
    def test_expected_tag(self, ref, tag):
        assert e2e.expected_tag(ref, SIM_SHA) == tag

    @pytest.mark.parametrize("env,branch", [("develop", "develop"), ("staging", "staging"),
                                            ("sandbox", "sandbox"), ("production", "main")])
    def test_gitops_branch(self, env, branch):
        assert e2e.gitops_branch_for(env) == branch

    def test_image_tag(self):
        assert e2e.image_tag("registry:5000/org/app:v1") == "v1"
        assert e2e.image_tag("registry:5000/org/app") == ""
        assert e2e.image_tag(None) == ""

    def test_select_build_run_prefers_exact_commit_and_skips_devsecops(self):
        runs = [
            e2e_sim.pipeline_run("app", "develop", SIM_SHA, suffix="sec",
                                 ts="2026-10-14T03:00:00Z", labels={"pipeline": "devsecops"}),
            e2e_sim.pipeline_run("app", "develop", "ffffff1", suffix="new",
                                 ts="2026-10-14T02:59:00Z"),
            e2e_sim.pipeline_run("app", "develop", SIM_SHA, suffix="old",
                                 ts="2026-10-14T02:00:00Z"),
            e2e_sim.pipeline_run("app", "staging", SIM_SHA, suffix="stg"),
        ]
        run, exact = e2e.select_build_run(runs, "develop", SIM_SHA)
        assert run["metadata"]["name"] == "webhook-app-old" and exact

    def test_select_build_run_falls_back_to_latest(self):
        runs = [e2e_sim.pipeline_run("app", "develop", "1111111", suffix="a",
                                     ts="2026-10-14T01:00:00Z"),
                e2e_sim.pipeline_run("app", "develop", "2222222", suffix="b",
                                     ts="2026-10-14T02:00:00Z")]
        run, exact = e2e.select_build_run(runs, "develop", SIM_SHA)
        assert run["metadata"]["name"] == "webhook-app-b" and not exact

    def test_find_argocd_app_matches_path_and_revision(self):
        apps = [e2e_sim.argocd_app("cce/develop-qoin", "develop", "x"),
                e2e_sim.argocd_app("cce/production-payout", "main", "x")]
        gitops = e2e_sim.GITOPS_REPO
        assert e2e.find_argocd_app(apps, gitops, "cce/develop-qoin", "develop") is apps[0]
        assert e2e.find_argocd_app(apps, gitops, "cce/develop-qoin", "main") is None

    def test_empty_automated_counts_as_auto_sync(self):
        rep = _analyze(e2e_sim._argocd_out_of_sync())
        assert rep.stages[-1].data["auto_sync"] is True


class TestScenarios:
    @pytest.mark.parametrize("name", list(SCENARIOS))
    def test_scenario_matches_expectation(self, name):
        report = e2e_sim.simulate(name)
        assert e2e_sim.verify(name, report) == []

    def test_every_break_point_kind_covered(self):
        kinds = {sc.break_point for sc in SCENARIOS.values()}
        assert {"source", "trigger", "pipeline", "gitops", "argocd", None} <= kinds
        assert {sc.verdict for sc in SCENARIOS.values()} == {"healthy", "broken", "incomplete"}

    def test_simulation_restores_real_io(self):
        real = e2e.gh_get
        e2e_sim.simulate("healthy")
        assert e2e.gh_get is real

    def test_report_dict_is_json_serialisable(self):
        d = e2e_sim.simulate("argocd-outofsync").to_dict()
        json.dumps(d)
        assert d["break_point"] == "argocd"
        assert d["verdict"] == "broken"

    @pytest.mark.parametrize("name,code", [
        ("healthy", 0), ("pipeline-build-failed", 1), ("argocd-outofsync", 2),
        ("cluster-unreachable", 2), ("bad-ref", 1),
    ])
    def test_exit_code(self, name, code):
        assert e2e.exit_code(e2e_sim.simulate(name)) == code

    def test_pipeline_log_tail_attached(self):
        pipe = e2e_sim.simulate("pipeline-build-failed").stages[2]
        assert pipe.data["failed_step"] == "build"
        assert "410 Gone" in pipe.data["log_tail"]

    def test_bad_ref_stops_after_source(self):
        rep = e2e_sim.simulate("bad-ref")
        assert [s.key for s in rep.stages] == ["source"]

    def test_hooks_404_is_not_fatal(self):
        world = healthy_world()
        world.hooks = None  # token bukan admin repo
        rep = _analyze(world)
        trigger = rep.stages[1]
        assert trigger.status == "ok"
        assert any("butuh akses admin" in d for d in trigger.details)

    def test_shorthand_repo_expands_with_org(self):
        world = healthy_world()
        with patched(world):
            rep = e2e.analyze("pay-be-demo-manager", "develop", config=SIM_CONFIG)
        assert rep.repo == world.repo
        assert rep.verdict == "healthy"


class TestSimulateCli:
    def test_list(self, capsys):
        assert e2e_sim.run_cli("list") == 0
        out = capsys.readouterr().out
        assert all(name in out for name in SCENARIOS)

    def test_all_passes(self, capsys):
        assert e2e_sim.run_cli("all") == 0
        assert f"{len(SCENARIOS)}/{len(SCENARIOS)} skenario sesuai harapan" in capsys.readouterr().out

    def test_all_reports_mismatch(self, monkeypatch, capsys):
        sc = SCENARIOS["healthy"]
        monkeypatch.setitem(SCENARIOS, "healthy", e2e_sim.Scenario(
            sc.name, sc.title, sc.build, "argocd", sc.stages, sc.verdict))
        assert e2e_sim.run_cli("all") == 1
        assert "FAIL  healthy" in capsys.readouterr().out

    def test_single_uses_real_exit_semantics(self, capsys):
        assert e2e_sim.run_cli("pipeline-build-failed") == 1
        out = capsys.readouterr().out
        assert "[SIMULASI]" in out and "Sesuai harapan" in out

    def test_unknown(self):
        assert e2e_sim.run_cli("ngawur") == 2


class TestTui:
    @pytest.fixture
    def audit(self, monkeypatch, tmp_path):
        from build_q import tui
        from build_q.audit import setup_audit_logger

        monkeypatch.setenv("BQ_HOME", str(tmp_path))
        setup_audit_logger(tui.audit_log, ".tui-audit.log")
        yield lambda: [json.loads(l) for l in (tmp_path / ".tui-audit.log").read_text().splitlines()]
        for h in list(tui.audit_log.handlers):
            tui.audit_log.removeHandler(h)
            h.close()

    def test_slash_e2e_real_path(self, monkeypatch, audit):
        from build_q import tui

        monkeypatch.setattr("build_q.config.load_config", lambda: SIM_CONFIG)
        with patched(healthy_world()):
            assert tui.handle_slash("/e2e pay-be-demo-manager develop", [None]) is True
        entry = audit()[-1]
        assert entry["event"] == "e2e"
        assert entry["verdict"] == "healthy"
        assert entry["stages"]["argocd"] == "ok"

    def test_slash_e2e_sim(self, audit, capsys):
        from build_q import tui

        assert tui.handle_slash("/e2e --sim gitops-pr-open", [None]) is True
        entry = audit()[-1]
        assert entry == {**entry, "event": "e2e_sim", "scenario": "gitops-pr-open", "ok": True}
        assert "PR GitOps #701" in capsys.readouterr().out

    def test_slash_e2e_sim_list_and_unknown(self, capsys):
        from build_q import tui

        tui.handle_slash("/e2e --sim", [None])
        tui.handle_slash("/e2e --sim ngawur", [None])
        out = capsys.readouterr().out
        assert "argocd-kustomize-override" in out
        assert "tidak dikenal" in out
