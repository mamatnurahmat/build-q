"""Tests for `bq --manifest-init … --pr` (build_q/manifest_pr.py)."""
import copy
import json

import pytest

from build_q import github_api, manifest_init, manifest_pr
from build_q import manifest_check as mc
from build_q.e2e_sim import (GITOPS_REPO, SIM_CONFIG, SIM_REPO, gitops_file, healthy_world, patched,
                             service_file, sync_kustomization)

KZ = """apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: develop-qoin   # namespace efektif
resources:
    # aplikasi lama
    - a_deployment.yaml
    - a_services.yaml
patches:
  - target: {kind: Deployment}
    patch: |-
      - op: add
        path: /spec/template/spec/imagePullSecrets
        value: [{name: regcred}]
"""


class TestAppendResources:
    def test_appends_with_same_indent_and_keeps_everything_else(self):
        out = manifest_pr.append_resources(KZ, ["b_deployment.yaml", "a_services.yaml"])
        assert "    - a_services.yaml\n    - b_deployment.yaml\npatches:" in out
        assert "# aplikasi lama" in out and "# namespace efektif" in out
        assert out.replace("    - b_deployment.yaml\n", "") == KZ  # satu-satunya perubahan

    def test_no_change_when_already_listed(self):
        assert manifest_pr.append_resources(KZ, ["a_deployment.yaml"]) == KZ

    def test_adds_resources_key_when_missing(self):
        out = manifest_pr.append_resources("kind: Kustomization\nnamespace: x\n", ["b.yaml"])
        assert out.endswith("resources:\n  - b.yaml\n")

    def test_inline_resources_refused(self):
        with pytest.raises(manifest_pr.ManifestPRError, match="tidak bisa diubah dengan aman"):
            manifest_pr.append_resources("kind: Kustomization\nresources: [a.yaml]\n", ["b.yaml"])


def _new_app_world():
    world = healthy_world()
    for path in (gitops_file(), service_file()):
        del world.files[(GITOPS_REPO, path)]
    sync_kustomization(world)
    world.secrets = ["regcred"]  # Secret config belum ada → saran kerangka Secret
    return world


def _init(world, ns="develop-qoin"):
    with patched(world):
        return manifest_init.init(SIM_REPO, ns, config=copy.deepcopy(SIM_CONFIG))


class TestPlan:
    def test_new_app_files_without_secret(self):
        world = _new_app_world()
        res = _init(world)
        assert any(mc.parse_yaml(s["yaml"])[0]["kind"] == "Secret" for s in res.suggestions)
        kz_text = world.files[(GITOPS_REPO, "cce/develop-qoin/kustomization.yaml")]
        p = manifest_pr.plan(res, kustomization_text=kz_text)
        assert sorted(p.files) == sorted([gitops_file(), service_file(), "cce/develop-qoin/kustomization.yaml"])
        assert not any("secret" in path for path in p.files)
        assert any("Secret" in s for s in p.skipped)
        kz = mc.parse_kustomization(p.files["cce/develop-qoin/kustomization.yaml"])
        assert f"{SIM_REPO}_deployment.yaml" in kz["resources"] and f"{SIM_REPO}_services.yaml" in kz["resources"]
        assert p.base == "develop" and p.branch.startswith(f"manifest/{SIM_REPO}-develop-qoin-")
        assert "Tidak di-commit" in p.body

    def test_new_folder_gets_new_kustomization(self):
        res = _init(healthy_world(), ns="staging-qoin")
        p = manifest_pr.plan(res, kustomization_text=None)
        assert mc.parse_yaml(p.files["cce/staging-qoin/kustomization.yaml"])[0]["kind"] == "Kustomization"

    def test_healthy_existing_has_nothing_to_pr(self):
        res = _init(healthy_world())
        with pytest.raises(manifest_pr.ManifestPRError, match="tidak ada perubahan"):
            manifest_pr.plan(res, kustomization_text="kind: Kustomization\nresources: []\n")

    def test_existing_not_in_kustomization_only_touches_kustomization(self):
        world = sync_kustomization(healthy_world(), skip=(service_file(),))
        res = _init(world)
        kz_text = world.files[(GITOPS_REPO, "cce/develop-qoin/kustomization.yaml")]
        p = manifest_pr.plan(res, kustomization_text=kz_text)
        assert list(p.files) == ["cce/develop-qoin/kustomization.yaml"]
        assert f"{SIM_REPO}_services.yaml" in mc.parse_kustomization(p.files["cce/develop-qoin/kustomization.yaml"])["resources"]


def test_create_single_commit_branch_and_pr(monkeypatch):
    world = _new_app_world()
    res = _init(world)
    p = manifest_pr.plan(res, kustomization_text=world.files[(GITOPS_REPO, "cce/develop-qoin/kustomization.yaml")])
    calls = []

    def fake_request(method, path, *, body=None, **kw):
        calls.append((method, path.split("/git/")[-1] if "/git/" in path else path,
                      json.loads(body) if body else None))
        if path.endswith("/git/ref/heads/develop"):
            return {"object": {"sha": "base1"}}
        if "/git/commits/base1" in path:
            return {"tree": {"sha": "tree0"}}
        if path.endswith("/git/trees"):
            return {"sha": "tree1"}
        if path.endswith("/git/commits"):
            return {"sha": "commit1"}
        return {}

    monkeypatch.setattr(github_api, "_request", fake_request)
    monkeypatch.setattr(github_api, "create_pull_request",
                        lambda repo, **kw: {"number": 7, "html_url": "https://x/pull/7", **kw})
    pr = manifest_pr.create(p)
    assert pr["number"] == 7 and pr["base"] == "develop" and pr["head"] == p.branch
    tree = next(b for m, path, b in calls if path == "trees")
    assert tree["base_tree"] == "tree0" and {t["path"] for t in tree["tree"]} == set(p.files)
    ref = next(b for m, path, b in calls if path == "refs")
    assert ref == {"ref": f"refs/heads/{p.branch}", "sha": "commit1"}


def test_create_refuses_secret_files():
    p = manifest_pr.PRPlan(repo="o/r", base="develop", branch="b", title="t", body="",
                           files={"cce/x/file-config-a_secret.yaml": "kind: Secret\n"})
    with pytest.raises(manifest_pr.ManifestPRError, match="menolak commit Secret"):
        manifest_pr.create(p)
