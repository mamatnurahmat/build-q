"""Tests untuk jejak --pr-fix (pr_fix_trace) dan klasifikasi penyebab (jev)."""
import io
import json
from unittest import mock

import pytest

from build_q import jev, pr_fix_trace
from build_q.pr_fix_trace import PR_FIX_COLLECTIONS, PrFixTrace


# ── jev: aturan deterministik ─────────────────────────────────────────────
def test_rule_registry_placeholder():
    log = "Get \"https://registry.example.com/v2/\": dial tcp: lookup registry.example.com: no such host"
    v = jev.rule_classify(log)
    assert v == {"cause": "registry_placeholder", "confidence": None, "source": "rule"}


def test_rule_missing_secret():
    v = jev.rule_classify("error: secret file-config-app not found")
    assert v["cause"] == "missing_secret"


def test_rule_build_failure():
    v = jev.rule_classify("./main.go:12:2: undefined: foo")
    assert v["cause"] == "build_failure"


def test_classify_empty_log_is_none():
    assert jev.classify("") == {"cause": None, "confidence": None, "source": "none"}


def test_classify_unknown_without_key_falls_back_to_rule(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    v = jev.classify("something odd happened")
    assert v["cause"] == "other"
    assert v["source"] == "rule"
    assert "TYPESAFE_API_KEY" in v["note"]


# ── jev: integrasi Jev (HTTP di-mock) ─────────────────────────────────────
def _fake_urlopen(payload):
    body = json.dumps(payload).encode()

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return lambda req, timeout=None: _Resp(body)


def test_classify_uses_jev_when_confident(monkeypatch):
    payload = {"answers": {"cause": {"type": "choice", "choice": "dockerfile_base_image",
                                     "confidence": 0.91, "probabilities": {"dockerfile_base_image": 0.91}}}}
    monkeypatch.setattr(jev.urllib.request, "urlopen", _fake_urlopen(payload))
    v = jev.classify("weird custom failure text", api_key="k")
    assert v["source"] == "jev"
    assert v["cause"] == "dockerfile_base_image"
    assert v["confidence"] == pytest.approx(0.91)


def test_classify_jev_low_confidence_keeps_rule(monkeypatch):
    payload = {"answers": {"cause": {"type": "choice", "choice": "go_version",
                                     "confidence": 0.4, "probabilities": {}}}}
    monkeypatch.setattr(jev.urllib.request, "urlopen", _fake_urlopen(payload))
    v = jev.classify("weird custom failure text", api_key="k")
    assert v["source"] == "rule"
    assert v["cause"] == "other"
    assert v["jev_cause"] == "go_version"


def test_classify_jev_error_is_soft(monkeypatch):
    def boom(req, timeout=None):
        raise OSError("network down")
    monkeypatch.setattr(jev.urllib.request, "urlopen", boom)
    v = jev.classify("weird custom failure text", api_key="k")
    assert v["source"] == "rule"
    assert "Jev request gagal" in v["note"]


def test_jev_payload_contains_choice_criteria():
    p = jev._build_payload("log")
    q = p["questions"]["cause"]
    assert q["type"] == "choice"
    assert set(q["criteria"]) == set(jev.CAUSES)


# ── pr_fix_trace: jejak lokal + skema ─────────────────────────────────────
@pytest.fixture
def local_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pr_fix_trace, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(pr_fix_trace.pb_api, "is_enabled", lambda: False)
    return tmp_path


def test_trace_writes_local_jsonl(local_dir):
    t = PrFixTrace("Org/repo", "v1.1.4", "repo-v1.1.4-x")
    t.set(head_sha="abc")
    t.step("clone", "ok", head="abc")
    t.step("push", "fail", error="denied")
    t.outcome = "ok"
    t.finish("ok")

    lines = [json.loads(x) for x in t.local_path.read_text().splitlines()]
    kinds = [(r["type"], r.get("step", r.get("status"))) for r in lines]
    assert kinds[0] == ("run", "running")
    assert ("step", "clone") in kinds
    assert lines[-1]["type"] == "run" and lines[-1]["status"] == "ok"
    assert lines[-1]["head_sha"] == "abc"


def test_step_accepts_name_kwarg_as_detail(local_dir):
    # Regression: `name=` adalah detail (mis. nama branch), bukan nama langkah.
    t = PrFixTrace("Org/repo", "v1", "r-v1-n")
    t.step("branch", "ok", name="fix/x")
    row = json.loads(t.local_path.read_text().splitlines()[-1])
    assert row["step"] == "branch"
    assert row["detail"] == {"name": "fix/x"}


def test_trace_rejects_unknown_status(local_dir):
    t = PrFixTrace("Org/repo", "v1", "r-v1-y")
    with pytest.raises(ValueError):
        t.step("x", "bogus")
    with pytest.raises(ValueError):
        t.finish("bogus")


def test_trace_survives_pb_failure(local_dir, monkeypatch):
    monkeypatch.setattr(pr_fix_trace.pb_api, "is_enabled", lambda: True)
    monkeypatch.setattr(pr_fix_trace.pb_api, "_base_url", lambda: "https://pb.invalid")
    monkeypatch.setattr(pr_fix_trace.pb_api, "get_token", lambda base: "tok")

    def boom(*a, **k):
        raise OSError("403 forbidden")
    monkeypatch.setattr(pr_fix_trace.pb_api, "_http_json", boom)

    t = PrFixTrace("Org/repo", "v1", "r-v1-z")
    t.step("clone", "ok")          # must not raise
    t.finish("failed")             # must not raise
    assert t._pb_enabled is False
    assert t.final_status == "failed"


def test_schema_defines_both_collections():
    assert set(PR_FIX_COLLECTIONS) == {"pr_fix_runs", "pr_fix_steps"}
    run_fields = {f["name"] for f in PR_FIX_COLLECTIONS["pr_fix_runs"]["fields"]}
    step_fields = {f["name"] for f in PR_FIX_COLLECTIONS["pr_fix_steps"]["fields"]}
    assert {"run_id", "repo", "ref", "status", "cause", "pr_url"} <= run_fields
    assert {"run_id", "seq", "step", "status", "detail"} <= step_fields
