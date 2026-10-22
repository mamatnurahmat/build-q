"""Tests for kube context ↔ namespace knowledge + Jev planner wiring."""

import json
import time

import pytest

from build_q import kube_contexts, tui


PB_CX = [
    {"id": "c1", "name": "hw-dev", "cluster": "hw-dev-cluster", "environment": "development", "note": "dev"},
    {"id": "c2", "name": "hw-pro-p", "cluster": "hw-pro-p-cluster", "environment": "production", "note": "payout"},
    {"id": "c3", "name": "hw-pro-q", "cluster": "hw-pro-q-cluster", "environment": "production", "note": "qoin"},
    {"id": "c4", "name": "hw-pro-u", "cluster": "hw-pro-u-cluster", "environment": "production", "note": "ue"},
]
PB_NS = [{"name": n, "kube_cx": c} for n, c in [
    ("develop-qoin", "c1"), ("develop-ngenwal", "c1"), ("sandbox-qoin", "c1"), ("jenkins-x", "c1"),
    ("develop-payout", "c1"), ("develop-payout", "c2"), ("production-payout", "c2"),
    ("production-qoin", "c3"), ("staging-qoin", "c3"),
    ("production-ngenwal", "c4"), ("production-saas", "c4"), ("staging-ngenwal", "c4"),
    ("orphan", "c9"),
]]


@pytest.fixture(autouse=True)
def fake_pb(monkeypatch, tmp_path):
    calls = []

    def fetch(coll, **kw):
        calls.append(coll)
        return {"kube_cx": PB_CX, "namespace": PB_NS}[coll]

    monkeypatch.setattr(kube_contexts.pb_api, "fetch_collection_records", fetch)
    monkeypatch.setattr(kube_contexts, "CACHE_FILE", tmp_path / "cx.json")
    monkeypatch.setattr(kube_contexts, "_loaded", None)
    return calls


class TestResolve:
    @pytest.mark.parametrize("ns, ctx", [
        ("develop-qoin", "hw-dev"),
        ("develop-ngenwal", "hw-dev"),
        ("sandbox-qoin", "hw-dev"),
        ("jenkins-x", "hw-dev"),
        ("production-payout", "hw-pro-p"),
        ("production-qoin", "hw-pro-q"),
        ("staging-qoin", "hw-pro-q"),
        ("production-ngenwal", "hw-pro-u"),
        ("production-saas", "hw-pro-u"),
        ("staging-ngenwal", "hw-pro-u"),
    ])
    def test_known_namespaces(self, ns, ctx):
        assert kube_contexts.resolve(ns)[0] == ctx

    def test_prefix_fallback_is_nearest(self):
        ctx, reason = kube_contexts.resolve("develop-baru")
        assert ctx == "hw-dev"
        assert "terdekat" in reason

    @pytest.mark.parametrize("ns", ["production-unknown", "", "<namespace>"])
    def test_unresolved(self, ns):
        assert kube_contexts.resolve(ns)[0] is None

    def test_ambiguous_namespace_lists_candidates(self):
        ctx, reason = kube_contexts.resolve("develop-payout")
        assert ctx is None and "ambigu" in reason
        assert kube_contexts.candidates("develop-payout") == ["hw-dev", "hw-pro-p"]

    def test_orphan_relation_ignored(self):
        assert "orphan" not in kube_contexts.namespace_map()


class TestLoad:
    def test_pb_then_cache(self, fake_pb):
        assert kube_contexts.load()[1] == "pb"
        kube_contexts._loaded = None
        assert kube_contexts.load()[1] == "cache"
        assert fake_pb == ["kube_cx", "namespace", "nodepool"]

    def test_pb_down_uses_bootstrap(self, monkeypatch):
        def boom(*a, **k):
            raise kube_contexts.pb_api.PBAPIError("down")
        monkeypatch.setattr(kube_contexts.pb_api, "fetch_collection_records", boom)
        data, src = kube_contexts.load()
        assert src == "bootstrap"
        assert "production-payout" in data["hw-pro-p"]["namespaces"]

    def test_pb_down_prefers_stale_cache(self, monkeypatch, tmp_path):
        kube_contexts.CACHE_FILE.write_text(json.dumps(
            {"fetched_at": 0, "contexts": {"hw-dev": {"namespaces": ["x"]}}}))
        monkeypatch.setattr(kube_contexts.pb_api, "fetch_collection_records",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
        data, src = kube_contexts.load()
        assert src == "stale-cache" and data["hw-dev"]["namespaces"] == ["x"]


CATALOG = {
    "tools": {
        "bq_bootstrap": {
            "desc": "Bootstrap", "template": "bq --bootstrap -n {namespace} --context {kube_context}",
            "params": {"namespace": {"type": "text"}, "kube_context": {"type": "text"}},
            "_risky": True, "_category": "k8s",
        },
    },
    "providers": {}, "risky": set(), "default_provider": "", "source": "cache", "fetched_at": 0,
    "patterns": {"namespace": r"(?:develop|staging|production)-[\w-]+"},
}
PROVIDER = tui.Provider(name="typesafe", url="https://x", model="m", key_env="TYPESAFE_API_KEY")


class TestPlannerWiring:
    @pytest.fixture(autouse=True)
    def fake_catalog(self, monkeypatch):
        monkeypatch.setattr(tui, "_catalog_cache", CATALOG)
        monkeypatch.setattr(tui, "_catalog_loaded_at", time.monotonic())

    def test_context_derived_from_namespace(self):
        out = tui.extract_params_from_text("bootstrap di production-payout", "bq_bootstrap")
        assert out["kube_context"] == "hw-pro-p"

    def test_jev_picks_nearest_when_unresolved(self, monkeypatch):
        seen = {}

        def fake(state, questions, provider):
            seen["state"], seen["questions"] = state, questions
            return {"answers": {"kube_context": {"choice": "hw-pro-u"}}}

        monkeypatch.setattr(tui, "jev_decide", fake)
        out = tui.fill_missing_choice_params("bootstrap production-ngenloy", "bq_bootstrap", PROVIDER)
        assert out == {"kube_context": "hw-pro-u"}
        assert set(seen["questions"]["kube_context"]["criteria"]) == set(kube_contexts.contexts())
        assert seen["state"]["namespace"] == "production-ngenloy"
        assert "hw-pro-q" in seen["state"]["kube_contexts"]

    def test_jev_invalid_context_ignored(self, monkeypatch):
        monkeypatch.setattr(tui, "jev_decide", lambda *a, **k: {"answers": {
            "kube_context": {"choice": "sls-j"}}})
        assert tui.fill_missing_choice_params("bootstrap", "bq_bootstrap", PROVIDER) == {}

    def test_ambiguous_limits_jev_choices(self, monkeypatch):
        seen = {}

        def fake(state, questions, provider):
            seen.update(questions)
            return {"answers": {"kube_context": {"choice": "hw-pro-q"}}}  # bukan kandidat

        monkeypatch.setattr(tui, "jev_decide", fake)
        out = tui.fill_missing_choice_params("bootstrap develop-payout", "bq_bootstrap", PROVIDER)
        assert set(seen["kube_context"]["criteria"]) == {"hw-dev", "hw-pro-p"}
        assert out == {}

    def test_no_jev_call_when_resolved(self, monkeypatch):
        monkeypatch.setattr(tui, "jev_decide", lambda *a, **k: pytest.fail("Jev dipanggil"))
        assert tui.fill_missing_choice_params("bootstrap develop-qoin", "bq_bootstrap", PROVIDER) == {}


class TestNodepool:
    """Nodepool CCE per namespace × cluster dari PB (`namespace.nodepool_front/back`)."""

    @pytest.fixture(autouse=True)
    def pb_with_nodepools(self, monkeypatch, tmp_path):
        cx = [{"id": "c1", "name": "hw-dev", "environment": "development",
               "nodepools": ["develop-qoin-manager", "develop-qoin-service"]},
              {"id": "c2", "name": "hw-pro-p", "environment": "production",
               "nodepools": ["production-payout-nodepool-manager", "production-payout-nodepool-service"]}]
        ns = [{"name": "develop-payout", "kube_cx": "c1"}, {"name": "develop-payout", "kube_cx": "c2"},
              {"name": "jenkins-x", "kube_cx": "c1"}, {"name": "production-payout", "kube_cx": "c2"}]
        label = kube_contexts.NODEPOOL_LABEL
        pools = [{"kube_cx": "c1", "namespace": "develop-payout", "role": "front", "label": label,
                  "value": "develop-qoin-manager"},
                 {"kube_cx": "c1", "namespace": "develop-payout", "role": "back", "label": label,
                  "value": "develop-qoin-service"},
                 {"kube_cx": "c2", "namespace": "develop-payout", "role": "front", "label": label,
                  "value": "production-payout-nodepool-manager"},
                 {"kube_cx": "c2", "namespace": "develop-payout", "role": "back", "label": label,
                  "value": "production-payout-nodepool-service"},
                 {"kube_cx": "c2", "namespace": "production-payout", "role": "front", "label": "nodetype",
                  "value": "front"}]
        monkeypatch.setattr(kube_contexts.pb_api, "fetch_collection_records",
                            lambda coll, **k: {"kube_cx": cx, "namespace": ns, "nodepool": pools}[coll])
        monkeypatch.setattr(kube_contexts, "CACHE_FILE", tmp_path / "cx.json")
        monkeypatch.setattr(kube_contexts, "_loaded", None)

    def test_loaded_from_pb(self):
        data = kube_contexts.contexts()
        assert data["hw-pro-p"]["nodepools"] == ["production-payout-nodepool-manager",
                                                 "production-payout-nodepool-service"]
        assert "jenkins-x" not in data["hw-dev"]["nodepool"]

    def test_ambiguous_namespace_uses_matching_environment(self):
        assert kube_contexts.nodepool_for("develop-payout", "front") == "develop-qoin-manager"
        assert kube_contexts.nodepool_for("develop-payout", "back", "hw-pro-p") == \
            "production-payout-nodepool-service"

    def test_nodepool_collection_label(self):
        assert kube_contexts.node_selector_for("production-payout", "front") == ("nodetype", "front")
        assert kube_contexts.node_selector_for("develop-payout", "back") == (
            kube_contexts.NODEPOOL_LABEL, "develop-qoin-service")

    def test_unknown_returns_empty_and_generator_falls_back_to_rule(self):
        from build_q import manifest_check as mc
        assert kube_contexts.nodepool_for("production-baru", "front") == ""
        assert mc.nodepool_for(mc.DEFAULT_RULES, "x-manager", "production-qoin") == "production-nodepool-manager"

    def test_cache_v2_is_refetched(self):
        kube_contexts.CACHE_FILE.write_text(json.dumps({"v": 2, "fetched_at": 9e18, "contexts": {}}))
        assert kube_contexts.load()[1] == "pb"
