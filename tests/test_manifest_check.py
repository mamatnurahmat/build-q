"""Tests for manifest_check (Deployment ↔ Service ↔ Secret) + tahap e2e manifest."""

import copy
import json

import pytest

from build_q import e2e, manifest_check as mc
from build_q.e2e_sim import SIM_CONFIG, SIM_REPO, healthy_world, patched, service_file, simulate

APP = "pay-be-demo-module"
NS = "production-qoin"
PATH = f"cce/{NS}/{APP}_deployment.yaml"
SVC = f"cce/{NS}/{APP}_services.yaml"


@pytest.fixture
def rules():
    return copy.deepcopy(mc.DEFAULT_RULES)


def _deploy(rules, **kw):
    return mc.suggest_deployment(rules, app=APP, ns=NS, image=f"loyaltolpi/{APP}:v1.0.0", **kw)


def _check(rules, deploy, service, cluster=frozenset({f"file-config-{APP}", "regcred"}), **kw):
    return mc.check(rules, deploy=deploy, service=service, app=APP, ns=NS, deploy_path=PATH,
                    service_path=SVC, secrets_in_cluster=None if cluster is None else set(cluster), **kw)


def _rules_hit(res):
    return [f.rule_name for f in res.findings]


class TestGenerator:
    def test_standard_deployment_passes_all_rules(self, rules):
        d = _deploy(rules)
        res = _check(rules, d, mc.suggest_service(d))
        assert res.findings == [] and res.suggestions == []
        pod = d["spec"]["template"]["spec"]
        assert d["spec"]["replicas"] == 2
        assert pod["nodeSelector"]["cce.cloud.com/cce-nodepool"] == "production-nodepool-service"
        assert pod["imagePullSecrets"] == [{"name": "regcred"}]
        assert pod["containers"][0]["volumeMounts"][0]["mountPath"] == "/app/.env"

    def test_dotnet_mounts_appsettings_env(self, rules):
        d = _deploy(rules, stack="dotnet")
        c = d["spec"]["template"]["spec"]["containers"][0]
        assert c["args"][-1] == "--environment=production"
        assert c["volumeMounts"][0]["subPath"] == "appsettings.production.json"

    def test_manager_uses_manager_pool(self, rules):
        d = mc.suggest_deployment(rules, app="pay-be-x-manager", ns="production-ngenwal", image="i:1")
        assert d["spec"]["template"]["spec"]["nodeSelector"]["cce.cloud.com/cce-nodepool"] \
            == "production-ue-nodepool-manager"

    def test_fix_keeps_valid_immutable_selector(self, rules):
        d = _deploy(rules)
        del d["spec"]["template"]["metadata"]["labels"]["project"]
        d["spec"]["selector"]["matchLabels"] = {"app": APP}
        fixed = mc.suggest_deployment(rules, app=APP, ns=NS, image="", base=d)
        assert fixed["spec"]["selector"]["matchLabels"] == {"app": APP}
        assert fixed["spec"]["template"]["metadata"]["labels"]["project"] == "qoin"  # akhiran namespace


class TestCheck:
    def test_service_selector_mismatch_suggests_fixed_service(self, rules):
        d = _deploy(rules)
        svc = mc.suggest_service(d)
        svc["spec"]["selector"]["role"] = "front"
        svc["spec"]["ports"][0]["port"] = 8080
        res = _check(rules, d, svc)
        assert _rules_hit(res) == ["service_selector_no_match"]
        fixed = mc.parse_yaml(res.suggestions[0]["yaml"])[0]
        assert fixed["spec"]["selector"]["role"] == "back"
        assert fixed["spec"]["ports"][0]["port"] == 8080  # port klien dipertahankan

    def test_numeric_target_port_is_warn_and_left_alone(self, rules):
        d = _deploy(rules)
        svc = mc.suggest_service(d)
        svc["spec"]["ports"][0]["targetPort"] = 9090
        res = _check(rules, d, svc)
        assert [(f.rule_name, f.severity) for f in res.findings] == [("service_port_targetport_mismatch", "warn")]
        assert res.suggestions == []  # tidak menebak port — bisa saja aplikasi listen di 9090

    def test_named_target_port_routes_to_declared_service_port(self, rules):
        """Kasus saas-be-loyalty-module: grpc 9541 → targetPort grpc, container 9541 bernama http."""
        d = _deploy(rules)
        d["spec"]["template"]["spec"]["containers"][0]["ports"] = [
            {"containerPort": 9541, "name": "http", "protocol": "TCP"}]
        svc = mc.suggest_service(d)
        svc["spec"]["ports"] = [{"name": "http", "port": 9542, "protocol": "TCP", "targetPort": "http"},
                                {"name": "grpc", "port": 9541, "protocol": "TCP", "targetPort": "grpc"}]
        res = _check(rules, d, svc)
        assert _rules_hit(res) == ["service_port_targetport_mismatch"]
        ports = mc.parse_yaml(res.suggestions[0]["yaml"])[0]["spec"]["ports"]
        assert ports == [{"name": "http", "port": 9542, "protocol": "TCP", "targetPort": "http"},
                         {"name": "grpc", "port": 9541, "protocol": "TCP", "targetPort": 9541}]

    def test_named_target_port_unknown_number_declared_in_deployment(self, rules):
        d = _deploy(rules)
        svc = mc.suggest_service(d)
        svc["spec"]["ports"].append({"name": "grpc", "port": 9000, "protocol": "TCP", "targetPort": "grpc"})
        res = _check(rules, d, svc)
        assert [s["path"] for s in res.suggestions] == [PATH]  # Service tidak diubah
        c0 = mc.parse_yaml(res.suggestions[0]["yaml"])[0]["spec"]["template"]["spec"]["containers"][0]
        assert {"containerPort": 9000, "name": "grpc", "protocol": "TCP"} in c0["ports"]
        assert any("grpc=9000" in n for n in res.notes)

    def test_fix_keeps_original_key_order(self, rules):
        d = _deploy(rules)
        svc = mc.suggest_service(d)
        svc = {"spec": svc["spec"], "metadata": svc["metadata"], "kind": "Service", "apiVersion": "v1"}
        svc["spec"]["selector"] = {"app": "lain"}
        out = mc.parse_yaml(_check(rules, d, svc).suggestions[0]["yaml"])[0]
        assert list(out) == ["spec", "metadata", "kind", "apiVersion"]  # diff minimal vs file asli

    def test_service_missing(self, rules):
        res = _check(rules, _deploy(rules), None)
        assert _rules_hit(res) == ["service_missing"]
        assert res.suggestions[0]["path"] == SVC

    def test_namespace_mismatch(self, rules):
        d = _deploy(rules)
        svc = mc.suggest_service(d)
        svc["metadata"]["namespace"] = "production-saas"
        assert _rules_hit(_check(rules, d, svc)) == ["service_namespace_mismatch"]

    def test_deploy_selector_template_mismatch(self, rules):
        d = _deploy(rules)
        d["spec"]["selector"]["matchLabels"]["app"] = "lain"
        res = _check(rules, d, mc.suggest_service(_deploy(rules)))
        assert "deploy_selector_template_mismatch" in _rules_hit(res)
        assert res.suggestions[0]["path"] == PATH

    def test_secret_missing_in_cluster(self, rules):
        res = _check(rules, _deploy(rules), mc.suggest_service(_deploy(rules)), cluster={"regcred"})
        assert _rules_hit(res) == ["secret_missing"]
        assert res.secrets[f"file-config-{APP}"] == "tidak-ada"
        sec = mc.parse_yaml(res.suggestions[0]["yaml"])[0]
        assert sec["kind"] == "Secret" and ".env" in sec["stringData"]

    def test_missing_file_config_volume_and_pullsecret(self, rules):
        d = _deploy(rules)
        pod = d["spec"]["template"]["spec"]
        pod.pop("volumes"), pod.pop("imagePullSecrets")
        pod["containers"][0].pop("volumeMounts")
        hit = _rules_hit(_check(rules, d, mc.suggest_service(d)))
        assert {"missing_file_config_volume", "missing_image_pullsecret"} <= set(hit)

    def test_cluster_unknown_uses_repo_hint(self, rules):
        d = _deploy(rules)
        res = _check(rules, d, mc.suggest_service(d), cluster=None,
                     secrets_in_repo={f"{APP}-production.yaml"})
        assert res.findings == []
        assert res.secrets[f"file-config-{APP}"] == "repo"
        assert res.secrets["regcred"] == "tidak-diketahui"

    def test_inactive_rule_skipped(self, rules):
        rules = mc._merge([{"rule_name": "service_missing", "active": False}])
        assert _rules_hit(_check(rules, _deploy(rules), None)) == []

    def test_envfrom_and_secretkeyref_referenced(self, rules):
        d = _deploy(rules)
        c = d["spec"]["template"]["spec"]["containers"][0]
        c["envFrom"] = [{"secretRef": {"name": "db-cred"}}]
        c["env"] = [{"name": "X", "valueFrom": {"secretKeyRef": {"name": "api-key", "key": "k"}}}]
        assert {"db-cred", "api-key"} <= set(mc.referenced_secrets(d))


class TestLoadRules:
    @pytest.fixture(autouse=True)
    def cache(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mc, "CACHE_FILE", tmp_path / "rules.json")

    def test_pb_overrides_severity_and_params(self, monkeypatch):
        monkeypatch.setattr(mc.pb_api, "fetch_collection_records", lambda *a, **k: [
            {"rule_name": "missing_standard_labels", "severity": "critical",
             "params": {"required_labels": ["app", "team"]}},
            {"rule_name": "rule_tanpa_check", "severity": "warn"},
        ])
        rules, src = mc.load_rules()
        assert src == "pb"
        assert rules["missing_standard_labels"]["severity"] == "critical"
        assert rules["missing_standard_labels"]["params"]["required_labels"] == ["app", "team"]
        assert "rule_tanpa_check" not in rules
        assert mc.load_rules()[1] == "cache"

    def test_pb_down_uses_default(self, monkeypatch):
        def boom(*a, **k):
            raise mc.pb_api.PBAPIError("collection belum ada")
        monkeypatch.setattr(mc.pb_api, "fetch_collection_records", boom)
        rules, src = mc.load_rules()
        assert src == "default" and set(rules) == set(mc.DEFAULT_RULES)


class TestE2EStage:
    def test_jev_verdict_added_when_findings(self, monkeypatch):
        world = healthy_world()
        del world.files[("Qoin-Digital-Indonesia/gitops", service_file())]
        monkeypatch.setattr(e2e, "_manifest_jev", lambda findings, **k: {
            "deploy_ready": 0.1, "fix_first": findings[0].rule_name})
        with patched(world):
            rep = e2e.analyze(world.repo, world.ref, config=copy.deepcopy(SIM_CONFIG), use_jev=True)
        st = next(s for s in rep.stages if s.key == "manifest")
        assert st.data["jev"]["fix_first"] == "service_missing"
        assert any("Jev: siap melayani traffic 10%" in d for d in st.details)
        json.dumps(rep.to_dict())  # web payload tetap serialisable

    def test_jev_not_called_without_findings(self, monkeypatch):
        monkeypatch.setattr(e2e, "_manifest_jev", lambda *a, **k: pytest.fail("Jev dipanggil"))
        world = healthy_world()
        with patched(world):
            e2e.analyze(world.repo, world.ref, config=copy.deepcopy(SIM_CONFIG), use_jev=True)

    def test_yaml_text_not_leaked_to_report(self):
        rep = simulate("healthy")
        assert all("_yaml" not in s.data for s in rep.stages)

    def test_deployment_missing_suggests_full_set(self):
        rep = simulate("gitops-not-found")
        st = next(s for s in rep.stages if s.key == "manifest")
        kinds = [mc.parse_yaml(x["yaml"])[0]["kind"] for x in st.data["suggestions"]]
        assert kinds == ["Deployment", "Service", "Secret"]
        assert rep.break_point.key == "gitops"
        assert f"file-config-{SIM_REPO}" in st.data["suggestions"][2]["yaml"]


class TestAmbiguousNamespace:
    def test_picks_context_running_the_deployment(self, monkeypatch):
        from build_q import kube_contexts

        world = healthy_world(ns="develop-payout")
        calls = []

        def names(ctx, ns, kind):
            calls.append((ctx, kind))
            if kind == "deployments":
                return [SIM_REPO] if ctx == "hw-pro-p" else []
            return world.secrets

        with patched(world):
            monkeypatch.setattr(e2e, "kubectl_names", names)
            assert kube_contexts.candidates("develop-payout") == ["hw-dev", "hw-pro-p"]
            rep = e2e.analyze(world.repo, world.ref, config=copy.deepcopy(SIM_CONFIG))
        st = next(s for s in rep.stages if s.key == "manifest")
        assert ("hw-pro-p", "secrets") in calls and ("hw-dev", "secrets") not in calls
        assert st.status == "ok"


# ─── Sample YAML dari GitOps main:cce (struktur mengikuti file asli) ──────

REAL_SAMPLE = """apiVersion: apps/v1
kind: Deployment
metadata:
  annotations:
    argocd.argoproj.io/tracking-id: production-ngenwal:apps/Deployment:production-ngenwal/ngenwal-be-saasaltoservice-module
    meta.helm.sh/release-name: ngenwal-be-saasaltoservice-module
    meta.helm.sh/release-namespace: production-qoin
  labels:
    app: ngenwal-be-saasaltoservice-module
    app.kubernetes.io/managed-by: Helm
    env: production
    project: ngenwal
    role: back
  name: ngenwal-be-saasaltoservice-module
  namespace: production-qoin
spec:
  progressDeadlineSeconds: 600
  replicas: 2
  selector:
    matchLabels: {app: ngenwal-be-saasaltoservice-module, env: production, project: ngenwal, role: back}
  strategy: {rollingUpdate: {maxSurge: 1, maxUnavailable: 0}, type: RollingUpdate}
  template:
    metadata:
      labels: {app: ngenwal-be-saasaltoservice-module, env: production, project: ngenwal, role: back}
    spec:
      containers:
        - args: [dotnet, ngenwal-be-saasaltoservice-module.dll, --environment=production]
          image: loyaltolpi/ngenwal-be-saasaltoservice-module:v1.0.0
          name: ngenwal-be-saasaltoservice-module
          ports: [{containerPort: 80, name: http, protocol: TCP}]
          resources: {limits: {cpu: 500m, memory: 100Mi}, requests: {cpu: 30m, memory: 100Mi}}
          volumeMounts:
            - {mountPath: /etc/localtime, name: tz-config}
            - {mountPath: /app/appsettings.production.json, name: file-config-volume, subPath: appsettings.production.json}
      imagePullSecrets: [{name: regcred}]
      nodeSelector: {cce.cloud.com/cce-nodepool: production-nodepool-service}
      volumes:
        - {hostPath: {path: /usr/share/zoneinfo/Asia/Jakarta, type: ""}, name: tz-config}
        - {name: file-config-volume, secret: {defaultMode: 420, secretName: file-config-ngenwal-be-saasaltoservice-module}}
"""
REAL_SAMPLE_SVC = """apiVersion: v1
kind: Service
metadata:
  annotations: {meta.helm.sh/release-name: ngenwal-be-saasaltoservice-module}
  labels: {app: ngenwal-be-saasaltoservice-module, app.kubernetes.io/managed-by: Helm, env: production,
           app.kubernetes.io/instance: production-ngenwal, project: ngenwal, role: back}
  name: ngenwal-be-saasaltoservice-module
  namespace: production-qoin
spec:
  clusterIP: 10.247.1.2
  clusterIPs: [10.247.1.2]
  internalTrafficPolicy: Cluster
  ports: [{name: http, port: 80, protocol: TCP, targetPort: http}]
  selector: {app: ngenwal-be-saasaltoservice-module, env: production, project: ngenwal, role: back}
  sessionAffinity: None
  type: ClusterIP
"""


@pytest.fixture
def sample():
    return {"app": "ngenwal-be-saasaltoservice-module", "deploy": mc.parse_yaml(REAL_SAMPLE)[0],
            "service": mc.parse_yaml(REAL_SAMPLE_SVC)[0],
            "path": "main:cce/production-qoin/ngenwal-be-saasaltoservice-module_deployment.yaml"}


class TestSample:
    def test_rank_prefers_same_role_and_project(self):
        files = ["a-be-x-manager_deployment.yaml", "pay-be-y-module_deployment.yaml",
                 "pay-be-z-manager_deployment.yaml", "pay-be-z-manager_services.yaml",
                 "pay-be-new-manager_deployment.yaml", "kustomization.yaml"]
        assert mc.rank_samples(files, "pay-be-new-manager") == [
            "pay-be-z-manager", "a-be-x-manager", "pay-be-y-module"]

    def test_adapt_go_module_from_dotnet_sample(self, rules, sample):
        app = "pay-be-newthing-module"
        d = mc.adapt_sample_deployment(rules, sample, app=app, ns=NS, image=f"loyaltolpi/{app}:v2",
                                       port=8080, stack="go")
        text = mc.dump_yaml(d)
        assert "ngenwal-be-saasaltoservice-module" not in text
        assert "annotations" not in d["metadata"] and "app.kubernetes.io/managed-by" not in text
        assert list(d) == ["apiVersion", "kind", "metadata", "spec"]  # manifest baru: urutan rapi
        c = d["spec"]["template"]["spec"]["containers"][0]
        assert c["image"] == f"loyaltolpi/{app}:v2" and "args" not in c
        assert c["ports"] == [{"containerPort": 8080, "name": "http", "protocol": "TCP"}]
        mounts = {m["name"]: m for m in c["volumeMounts"]}
        assert mounts["tz-config"]["mountPath"] == "/etc/localtime"  # konvensi sample dipertahankan
        assert (mounts["file-config-volume"]["mountPath"], mounts["file-config-volume"]["subPath"]) \
            == ("/app/.env", ".env")
        pod = d["spec"]["template"]["spec"]
        assert {"name": "file-config-volume",
                "secret": {"defaultMode": 420, "secretName": f"file-config-{app}"}} in pod["volumes"]
        assert pod["nodeSelector"]["cce.cloud.com/cce-nodepool"] == "production-nodepool-service"
        assert d["spec"]["selector"]["matchLabels"] == {"app": app, "env": "production",
                                                        "project": "qoin", "role": "back"}
        assert d["spec"]["replicas"] == 2 and d["spec"]["progressDeadlineSeconds"] == 600

        svc = mc.adapt_sample_service(sample, d)
        assert "clusterIP" not in svc["spec"] and "clusterIPs" not in svc["spec"]
        assert svc["spec"]["selector"] == d["spec"]["selector"]["matchLabels"]
        assert svc["spec"]["ports"] == [{"name": "http", "port": 8080, "protocol": "TCP", "targetPort": "http"}]
        assert svc["spec"]["sessionAffinity"] == "None" and svc["metadata"]["name"] == app
        assert "app.kubernetes.io/instance" not in svc["metadata"]["labels"]  # tracking ArgoCD tdk tersalin
        res = mc.check(rules, deploy=d, service=svc, app=app, ns=NS, deploy_path=PATH, service_path=SVC,
                       secrets_in_cluster={f"file-config-{app}", "regcred"})
        assert res.findings == []

    def test_adapt_manager_switches_pool_and_keeps_dotnet(self, rules, sample):
        app = "pay-be-newthing-manager"
        d = mc.adapt_sample_deployment(rules, sample, app=app, ns=NS, image="i:1", stack="dotnet")
        pod = d["spec"]["template"]["spec"]
        assert pod["nodeSelector"]["cce.cloud.com/cce-nodepool"] == "production-nodepool-manager"
        c = pod["containers"][0]
        assert c["args"] == ["dotnet", f"{app}.dll", "--environment=production"]
        assert any(m.get("subPath") == "appsettings.production.json" for m in c["volumeMounts"])

    def test_adapt_skips_regcred_when_kustomization_injects(self, rules, sample):
        deploy = copy.deepcopy(sample["deploy"])
        deploy["spec"]["template"]["spec"].pop("imagePullSecrets")
        kz = mc.parse_kustomization(KZ_PATCH)
        d = mc.adapt_sample_deployment(rules, {**sample, "deploy": deploy}, app=APP, ns=NS, image="i:1", kz=kz)
        assert "imagePullSecrets" not in d["spec"]["template"]["spec"]


KZ_PATCH = """apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: production-qoin
resources:
  - pay-be-demo-module_deployment.yaml
patches:
  - target: {kind: Deployment}
    patch: |-
      - op: add
        path: /spec/template/spec/imagePullSecrets
        value:
          - name: regcred
      - op: replace
        path: /spec/template/spec/containers/0/imagePullPolicy
        value: Always
"""


class TestKustomization:
    def _no_pull(self, rules):
        d = _deploy(rules)
        d["spec"]["template"]["spec"].pop("imagePullSecrets")
        return d

    def test_patch_injects_regcred(self, rules):
        d = self._no_pull(rules)
        kz = mc.parse_kustomization(KZ_PATCH)
        kz["resources"].append(SVC.rsplit("/", 1)[-1])
        res = _check(rules, d, mc.suggest_service(d), kustomization=kz)
        assert res.findings == []
        assert "missing_image_pullsecret" in _rules_hit(_check(rules, self._no_pull(rules), mc.suggest_service(d)))

    def test_file_not_registered(self, rules):
        d = _deploy(rules)
        res = _check(rules, d, mc.suggest_service(d), kustomization=mc.parse_kustomization(KZ_PATCH))
        assert _rules_hit(res) == ["manifest_not_in_kustomization"]
        assert f"{APP}_services.yaml tidak ada di resources" in res.findings[0].message

    def test_namespace_from_kustomization_wins(self, rules):
        d = _deploy(rules)
        svc = mc.suggest_service(d)
        svc["metadata"]["namespace"] = "staging-qoin"  # ditimpa namespace: di kustomization
        kz = {"namespace": NS, "resources": None, "patches": []}
        assert _check(rules, d, svc, kustomization=kz).findings == []
        assert _rules_hit(_check(rules, d, svc)) == ["service_namespace_mismatch"]


class TestFalsePositives:
    def test_named_target_port_missing_is_critical(self, rules):
        d = _deploy(rules)
        svc = mc.suggest_service(d)
        svc["spec"]["ports"][0]["targetPort"] = "grpc"
        f = _check(rules, d, svc).findings[0]
        assert (f.rule_name, f.severity) == ("service_port_targetport_mismatch", "critical")

    def test_numeric_target_port_undeclared_is_warn(self, rules):
        d = _deploy(rules)
        svc = mc.suggest_service(d)
        svc["spec"]["ports"][0]["targetPort"] = 15008
        f = _check(rules, d, svc).findings[0]
        assert f.severity == "warn"

    def test_worker_without_ports_needs_no_service(self, rules):
        d = _deploy(rules)
        d["spec"]["template"]["spec"]["containers"][0].pop("ports")
        res = _check(rules, d, None)
        assert res.findings == [] and any("worker" in n for n in res.notes)

    def test_public_image_needs_no_regcred(self, rules):
        d = _deploy(rules)
        pod = d["spec"]["template"]["spec"]
        pod.pop("imagePullSecrets")
        pod["containers"][0]["image"] = "nginx:stable"
        assert "missing_image_pullsecret" not in _rules_hit(_check(rules, d, mc.suggest_service(d)))

    def test_baked_config_is_advice_not_secret_missing(self, rules):
        d = _deploy(rules)
        pod = d["spec"]["template"]["spec"]
        pod.pop("volumes")
        pod["containers"][0].pop("volumeMounts")
        res = _check(rules, d, mc.suggest_service(d), cluster={"regcred"})
        assert [(f.rule_name, f.severity) for f in res.findings] == [("missing_file_config_volume", "warn")]
        kinds = [mc.parse_yaml(s["yaml"])[0]["kind"] for s in res.suggestions]
        assert kinds == ["Deployment", "Secret"]

    @pytest.mark.parametrize("files, found", [
        ({f"{APP}_deployment.yaml", f"{APP}_services.yaml"}, False),  # file deploy ≠ secret
        ({f"file-config-{APP}_secret_sops.yaml"}, True),
        ({f"{APP}-production.yaml"}, True),                           # file-config/<app>-<env>.yaml
        ({f"{APP}-v2-production.yaml"}, False),                       # app lain berawalan sama
    ])
    def test_secret_in_repo_is_strict(self, files, found):
        assert mc.secret_in_repo(f"file-config-{APP}", "production", files) is found


class TestE2EContext:
    def _names_spy(self, world, calls):
        def names(ctx, ns, kind):
            calls.append((ctx, ns, kind))
            return world.secrets if kind == "secrets" else [SIM_REPO]
        return names

    def test_destination_argocd_picks_context(self, monkeypatch):
        world = healthy_world()
        world.apps[0]["spec"]["destination"] = {"server": "https://10.6.20.45:5443", "namespace": "develop-qoin"}
        calls = []
        with patched(world):
            monkeypatch.setattr(e2e, "kubectl_names", self._names_spy(world, calls))
            rep = e2e.analyze(world.repo, world.ref, config=copy.deepcopy(SIM_CONFIG))
        st = next(s for s in rep.stages if s.key == "manifest")
        assert calls == [("hw-pro-u", "develop-qoin", "secrets")]
        assert any("destination ArgoCD https://10.6.20.45:5443" in d for d in st.details)

    def test_kustomization_namespace_used_for_secrets(self, monkeypatch):
        import yaml

        world = healthy_world(ns="staging-qoin", ref="staging")
        kfile = ("Qoin-Digital-Indonesia/gitops", "cce/staging-qoin/kustomization.yaml")
        kz = yaml.safe_load(world.files[kfile])
        kz["namespace"] = "production-qoin"
        world.files[kfile] = yaml.safe_dump(kz)
        calls = []
        with patched(world):
            monkeypatch.setattr(e2e, "kubectl_names", self._names_spy(world, calls))
            rep = e2e.analyze(world.repo, world.ref, config=copy.deepcopy(SIM_CONFIG))
        st = next(s for s in rep.stages if s.key == "manifest")
        assert calls == [("hw-pro-q", "production-qoin", "secrets")]
        assert "namespace efektif: production-qoin (folder staging-qoin)" in st.details
        assert st.status == "ok"


class TestSlsPath:
    def test_k8s_path_does_not_use_cce_mapping(self, monkeypatch):
        from build_q.e2e_sim import argocd_app, task_run

        world = healthy_world()
        world.files = {(r, p.replace("cce/develop-qoin/", "k8s/develop-qoin/")): v
                       for (r, p), v in world.files.items()}
        gfile = f"k8s/develop-qoin/{SIM_REPO}_deployment.yaml"
        tag = e2e.expected_tag(world.ref, world.sha)
        world.taskrun = task_run({"build-mode": "built", "image-tag": tag, "gitops-file": gfile,
                                  "gitops-result": "pushed"})
        world.apps = [argocd_app("k8s/develop-qoin", "develop", tag)]
        with patched(world):
            monkeypatch.setattr(e2e, "kubectl_names", lambda *a: pytest.fail("cluster CCE dipakai utk SLS"))
            rep = e2e.analyze(world.repo, world.ref, config=copy.deepcopy(SIM_CONFIG))
        st = next(s for s in rep.stages if s.key == "manifest")
        assert any("infra k8s/ (SLS)" in d for d in st.details)
        assert st.status == "skip" and st.applicable  # secret belum terverifikasi → incomplete

    def test_sample_cce_for_sls_target_drops_nodepool(self, rules, sample):
        d = mc.adapt_sample_deployment(rules, sample, app=APP, ns="production-saas", image="i:1",
                                       infra="k8s")
        assert "cce.cloud.com/cce-nodepool" not in (d["spec"]["template"]["spec"].get("nodeSelector") or {})


class TestExistingConfigSecret:
    """Kasus develop-qoin (aplikasi Rust): Secret `file-config-<app>-develop` ada tapi tak di-mount."""

    def _baked(self, rules, ns="develop-qoin"):
        d = mc.suggest_deployment(rules, app=APP, ns=ns, image=f"loyaltolpi/{APP}:abc")
        pod = d["spec"]["template"]["spec"]
        pod.pop("volumes")
        pod["containers"][0].pop("volumeMounts")
        return d

    def test_mounts_existing_secret_name(self, rules):
        d = self._baked(rules)
        res = mc.check(rules, deploy=d, service=mc.suggest_service(d), app=APP, ns="develop-qoin",
                       deploy_path=f"cce/develop-qoin/{APP}_deployment.yaml",
                       service_path=f"cce/develop-qoin/{APP}_services.yaml",
                       secrets_in_cluster={f"file-config-{APP}-develop", "regcred"})
        f = res.findings[0]
        assert (f.rule_name, f.severity) == ("missing_file_config_volume", "warn")
        assert f"Secret file-config-{APP}-develop sudah ada di namespace tetapi tidak di-mount" in f.message
        assert [mc.parse_yaml(s["yaml"])[0]["kind"] for s in res.suggestions] == ["Deployment"]  # tanpa kerangka Secret
        pod = mc.parse_yaml(res.suggestions[0]["yaml"])[0]["spec"]["template"]["spec"]
        assert {"name": "file-config-volume",
                "secret": {"secretName": f"file-config-{APP}-develop", "defaultMode": 420}} in pod["volumes"]

    def test_repo_only_gives_note_not_guess(self, rules):
        d = self._baked(rules)
        res = mc.check(rules, deploy=d, service=mc.suggest_service(d), app=APP, ns="develop-qoin",
                       deploy_path=f"cce/develop-qoin/{APP}_deployment.yaml",
                       service_path=f"cce/develop-qoin/{APP}_services.yaml",
                       secrets_in_repo={f"{APP}-develop.yaml"})
        assert any(f"{APP}-develop.yaml" in n and "pastikan nama Secret" in n for n in res.notes)
        kinds = [mc.parse_yaml(s["yaml"])[0]["kind"] for s in res.suggestions]
        assert kinds == ["Deployment", "Secret"]
