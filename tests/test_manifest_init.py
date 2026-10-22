"""Tests for `bq --manifest-init <repo> <env>` (build_q/manifest_init.py) — dunia tiruan e2e_sim."""
import copy

import pytest

from build_q import manifest_check as mc, manifest_init
from build_q.e2e_sim import (GITOPS_REPO, SAMPLE_APP, SIM_CONFIG, SIM_REPO, SIM_SHA, gitops_file,
                             healthy_world, patched, service_file, sync_kustomization)
from build_q.github_api import GitHubAPIError


def _init(world, env="develop", **kw):
    with patched(world):
        return manifest_init.init(SIM_REPO, env, config=copy.deepcopy(SIM_CONFIG), **kw)


def _kinds(res):
    return [mc.parse_yaml(s["yaml"])[0]["kind"] for s in res.suggestions]


def _wrap_gh_get(world, extra):
    orig = world.gh_get

    def gh_get(path):
        for suffix, value in extra.items():
            if path.endswith(suffix):
                if isinstance(value, Exception):
                    raise value
                return value
        return orig(path)

    world.gh_get = gh_get
    return world


class TestExisting:
    def test_healthy_manifest_reports_no_suggestion(self):
        res = _init(healthy_world())
        assert res.mode == "existing" and res.findings == [] and res.suggestions == []
        assert res.path == gitops_file() and res.gitops_branch == "develop" and res.exit_code == 0
        assert res.image == f"loyaltolpi/{SIM_REPO}:{SIM_SHA[:7]}"
        assert "tidak ada saran" in manifest_init.render_text(res)

    def test_missing_service_suggested_from_sample(self):
        world = healthy_world()
        del world.files[(GITOPS_REPO, service_file())]
        sync_kustomization(world)
        res = _init(world)
        assert [f["rule_name"] for f in res.findings] == ["service_missing"] and res.exit_code == 1
        assert "dari sample main:cce/develop-qoin" in res.suggestions[0]["title"]


class TestNew:
    def test_new_app_from_sample_with_kustomization_lines(self):
        world = healthy_world()
        for path in (gitops_file(), service_file()):
            del world.files[(GITOPS_REPO, path)]
        sync_kustomization(world)
        res = _init(world)
        assert res.mode == "new" and _kinds(res) == ["Deployment", "Service"]  # Secret sudah ada di cluster
        assert res.origin == f"sample main:cce/develop-qoin/{SAMPLE_APP}_deployment.yaml"
        assert res.kustomization_add == [f"{SIM_REPO}_deployment.yaml", f"{SIM_REPO}_services.yaml"]
        d = mc.parse_yaml(res.suggestions[0]["yaml"])[0]
        assert d["metadata"]["name"] == SIM_REPO
        assert d["spec"]["template"]["spec"]["containers"][0]["image"] == res.image

    def test_new_folder_gets_generator_and_new_kustomization(self):
        res = _init(healthy_world(), env="staging")
        assert res.path == f"cce/staging-qoin/{SIM_REPO}_deployment.yaml" and res.gitops_branch == "staging"
        assert res.origin.startswith("generator standar")
        assert _kinds(res) == ["Deployment", "Service", "Kustomization"]
        kz = mc.parse_yaml(res.suggestions[-1]["yaml"])[0]
        assert kz["namespace"] == "staging-qoin" and kz["resources"] == [
            f"{SIM_REPO}_deployment.yaml", f"{SIM_REPO}_services.yaml"]
        pod = mc.parse_yaml(res.suggestions[0]["yaml"])[0]["spec"]["template"]["spec"]
        assert pod["nodeSelector"]["cce.cloud.com/cce-nodepool"] == "production-nodepool-manager"

    def test_production_uses_latest_semver_tag(self):
        world = _wrap_gh_get(healthy_world(), {
            "/tags?per_page=100": [{"name": "v1.2.0"}, {"name": "v1.10.1"}, {"name": "latest"}]})
        res = _init(world, env="production")
        assert res.ref == "v1.10.1" and res.image.endswith(":v1.10.1")
        assert res.gitops_branch == "main" and res.path == f"cce/production-qoin/{SIM_REPO}_deployment.yaml"
        d = mc.parse_yaml(res.suggestions[0]["yaml"])[0]
        assert d["spec"]["replicas"] == 2

    def test_missing_env_branch_falls_back_to_default(self):
        world = _wrap_gh_get(healthy_world(), {
            "/commits/sandbox": GitHubAPIError("HTTP 422: No commit found"),
            f"/repos/Qoin-Digital-Indonesia/{SIM_REPO}": {"default_branch": "main"}})
        res = _init(world, env="sandbox")
        assert res.ref == "main" and res.path == f"cce/sandbox-qoin/{SIM_REPO}_deployment.yaml"
        assert any("branch sandbox belum ada di repo — dibaca dari branch default main" in d
                   for d in res.details)

    def test_invalid_env(self):
        with pytest.raises(manifest_init.ManifestInitError):
            manifest_init.init(SIM_REPO, "prod", config=copy.deepcopy(SIM_CONFIG))


def test_write_files_and_markdown(tmp_path):
    res = _init(healthy_world(), env="staging")
    paths = manifest_init.write_files(res, str(tmp_path))
    assert (tmp_path / f"cce/staging-qoin/{SIM_REPO}_deployment.yaml") in paths
    assert (tmp_path / "cce/staging-qoin/kustomization.yaml").exists()
    md = manifest_init.render_markdown(res)
    assert md.startswith(f"# Manifest init — `Qoin-Digital-Indonesia/{SIM_REPO}` (staging)")
    assert "```yaml" in md


# ─── Template standar (PB build_q_manifest_templates) & verifikasi leave-one-out ───

from build_q import e2e, manifest_template  # noqa: E402


def _template_for(world, app=SAMPLE_APP, ns="develop-qoin"):
    deploy = mc.parse_yaml(world.files[(GITOPS_REPO, gitops_file(app, ns))])[0]
    service = mc.parse_yaml(world.files[(GITOPS_REPO, service_file(app, ns))])[0]
    return mc.make_template(deploy, service, app=app, infra="cce", namespace=ns, source="main:x")


class TestTemplates:
    def test_make_template_uses_placeholder(self):
        tpl = _template_for(healthy_world())
        assert tpl["name"] == "cce-develop-qoin-manager" and tpl["app_kind"] == "manager"
        assert SAMPLE_APP not in str(tpl["deployment"]) and mc.TEMPLATE_APP in str(tpl["deployment"])

    def test_find_template_specificity_and_exclusion(self):
        world = healthy_world()
        exact = _template_for(world)
        generic = {**exact, "name": "cce-any-manager", "namespace": "*"}
        found = mc.find_template([generic, exact], infra="cce", namespace="develop-qoin", app="pay-be-new-manager")
        assert found["template"] == "cce-develop-qoin-manager"
        assert mc.find_template([exact], infra="cce", namespace="develop-qoin", app="pay-be-new-module") is None
        assert mc.find_template([exact], infra="cce", namespace="develop-qoin", app="x-manager",
                                exclude_source_app=SAMPLE_APP) is None

    def test_template_has_priority_over_neighbour(self, monkeypatch):
        world = healthy_world()
        for path in (gitops_file(), service_file()):
            del world.files[(GITOPS_REPO, path)]
        sync_kustomization(world)
        tpl = {**_template_for(world), "name": "cce-develop-qoin-manager-pb"}
        with patched(world):
            monkeypatch.setattr(mc, "load_templates", lambda **k: ([tpl], "pb"))
            res = manifest_init.init(SIM_REPO, "develop", config=copy.deepcopy(SIM_CONFIG))
        assert res.origin == "sample template cce-develop-qoin-manager-pb"
        d = mc.parse_yaml(res.suggestions[0]["yaml"])[0]
        assert d["metadata"]["name"] == SIM_REPO and mc.TEMPLATE_APP not in res.suggestions[0]["yaml"]

    def test_builder_only_accepts_standard_manifests(self):
        world = healthy_world()
        bad = mc.parse_yaml(world.files[(GITOPS_REPO, gitops_file())])[0]
        bad["spec"]["template"]["metadata"]["labels"]["env"] = "staging"
        world.files[(GITOPS_REPO, gitops_file())] = mc.dump_yaml(bad)
        with patched(world):
            records, report = manifest_template.build("develop-qoin", config=copy.deepcopy(SIM_CONFIG))
        assert [r["source_app"] for r in records] == [SAMPLE_APP]
        assert any("label tidak standar" in x for x in report[0]["rejected"])


class TestVerify:
    def test_leave_one_out_matches_existing_core(self):
        with patched(healthy_world()):
            items = manifest_init.verify_folder("develop-qoin", config=copy.deepcopy(SIM_CONFIG))
        assert {i.app: i.status for i in items} == {SIM_REPO: "match", SAMPLE_APP: "match"}
        assert manifest_init.verify_to_dict("develop-qoin", "main", items)["exit_code"] == 0

    def test_existing_nonstandard_is_reported_not_diff(self):
        world = healthy_world()
        doc = mc.parse_yaml(world.files[(GITOPS_REPO, gitops_file())])[0]
        doc["spec"]["replicas"] = 0
        world.files[(GITOPS_REPO, gitops_file())] = mc.dump_yaml(doc)
        with patched(world):
            items = manifest_init.verify_folder("develop-qoin", config=copy.deepcopy(SIM_CONFIG))
        item = next(i for i in items if i.app == SIM_REPO)
        assert item.status == "match-nonstandard" and any("replicas" in d for d in item.nonstandard)

    def test_generator_regression_is_diff(self, monkeypatch):
        orig = mc.adapt_sample_deployment

        def broken(*a, **kw):
            d = orig(*a, **kw)
            d["spec"]["selector"]["matchLabels"]["role"] = "salah"
            return d

        monkeypatch.setattr(mc, "adapt_sample_deployment", broken)
        with patched(healthy_world()):
            items = manifest_init.verify_folder("develop-qoin", config=copy.deepcopy(SIM_CONFIG))
        assert all(i.status == "diff" for i in items)
        assert "❌" in manifest_init.render_verify("develop-qoin", "main", items)


class TestNamespaceArg:
    def test_repo_namespace_format(self):
        res = _init(healthy_world(), env="staging-qoin")
        assert res.env == "staging" and res.gitops_branch == "staging"
        assert res.path == f"cce/staging-qoin/{SIM_REPO}_deployment.yaml"

    def test_namespace_overrides_cicd_project(self):
        res = _init(healthy_world(), env="develop-payout")
        assert res.path == f"cce/develop-payout/{SIM_REPO}_deployment.yaml" and res.env == "develop"

    @pytest.mark.parametrize("bad", ["qa-qoin", "Bad NS"])
    def test_invalid_namespace(self, bad):
        with pytest.raises(manifest_init.ManifestInitError):
            manifest_init.init(SIM_REPO, bad, config=copy.deepcopy(SIM_CONFIG))
