"""Tests for the `bq --tui` planner — stability and MCP-aligned risk."""

import json
import time

import pytest

from build_q import tui, tui_catalog


CATALOG = {
    "tools": {
        "bq_doctor": {
            "desc": "Preflight", "template": "bq --doctor", "params": {},
            "_risky": False, "_category": "preflight",
        },
        "bq_sops_decrypt": {
            "desc": "Decrypt", "template": "bq --sops-decrypt {path}",
            "params": {"path": {"type": "text"}},
            "_risky": False, "_category": "sops",
        },
        "bq_set_image": {
            "desc": "Set image",
            "template": "bq --set-image {namespace} {deployment} {image}",
            "params": {
                "namespace": {"type": "choice", "options": ["develop-qoin", "staging-qoin"]},
                "deployment": {"type": "text"},
                "image": {"type": "text"},
            },
            "_risky": True, "_category": "k8s",
        },
    },
    "providers": {"typesafe": {"name": "typesafe", "url": "https://x", "model": "m",
                               "key_env": "TYPESAFE_API_KEY", "is_default": True}},
    "patterns": {},
    "risky": {"bq_set_image"},
    "default_provider": "typesafe",
    "source": "cache",
    "fetched_at": 0,
}

PROVIDER = tui.Provider(name="typesafe", url="https://x", model="m", key_env="TYPESAFE_API_KEY")


@pytest.fixture(autouse=True)
def fake_catalog(monkeypatch):
    monkeypatch.setattr(tui, "_catalog_cache", CATALOG)
    monkeypatch.setattr(tui, "_catalog_loaded_at", time.monotonic())


def _resp(tool="bq_doctor", noul=0.1, **extra):
    return {"answers": {
        "tool": {"choice": tool, "confidence": 0.9, "probabilities": {tool: 0.9}, **extra},
        "risky": {"noul": noul},
    }}


class TestPreviewCommand:
    def test_missing_param_renders_placeholder(self):
        assert tui.preview_command("bq_sops_decrypt", {}) == "bq --sops-decrypt <path>"

    def test_invalid_template_raises_value_error(self, monkeypatch):
        bad = {**CATALOG, "tools": {"x": {**CATALOG["tools"]["bq_doctor"], "template": "bq {"}}}
        monkeypatch.setattr(tui, "_catalog_cache", bad)
        with pytest.raises(ValueError, match="template"):
            tui.preview_command("x", {})


class TestParseDecision:
    def test_valid(self):
        d = tui.parse_decision(_resp(noul=0.7))
        assert d.tool == "bq_doctor"
        assert d.noul == 0.7

    @pytest.mark.parametrize("resp", [{}, {"answers": None}, {"answers": {"tool": {}}}, "oops"])
    def test_malformed_raises(self, resp):
        with pytest.raises(ValueError):
            tui.parse_decision(resp)

    def test_unknown_tool_raises(self):
        with pytest.raises(ValueError, match="/refresh"):
            tui.parse_decision(_resp(tool="bq_ghost"))

    def test_none_allowed_and_bad_noul_defaults_zero(self):
        resp = _resp(tool="none")
        resp["answers"]["risky"] = {"noul": "abc"}
        d = tui.parse_decision(resp)
        assert d.tool == "none"
        assert d.noul == 0.0


class TestAssessRisk:
    def test_safe_read_only(self):
        assert tui.assess_risk("bq_doctor", 0.1, "bq --doctor") == (False, [])

    def test_mcp_tier_elevates_catalog_safe_tool(self):
        risky, reasons = tui.assess_risk("bq_sops_decrypt", 0.1, "bq --sops-decrypt s.yaml")
        assert risky
        assert any("sops_decrypt" in r for r in reasons)

    def test_catalog_and_jev_flags(self):
        risky, reasons = tui.assess_risk("bq_set_image", 0.9)
        assert risky
        assert len(reasons) == 2


class TestFillChoiceParams:
    def test_ignores_invalid_or_malformed_choices(self, monkeypatch):
        monkeypatch.setattr(tui, "jev_decide", lambda *a, **k: {"answers": {
            "namespace": {"choice": "production-payout"},  # bukan option
        }})
        assert tui.fill_missing_choice_params("set image", "bq_set_image", PROVIDER) == {}

    def test_accepts_valid_choice_and_redacts_request(self, monkeypatch):
        seen = {}

        def fake(state, questions, provider):
            seen.update(state)
            return {"answers": {"namespace": {"choice": "staging-qoin"}}}

        monkeypatch.setattr(tui, "jev_decide", fake)
        out = tui.fill_missing_choice_params(
            "set image ghp_" + "a" * 36, "bq_set_image", PROVIDER,
        )
        assert out == {"namespace": "staging-qoin"}
        assert "ghp_" not in seen["user_request"]


class TestRepl:
    @pytest.fixture
    def repl_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BQ_HOME", str(tmp_path))
        monkeypatch.setattr(tui, "HISTORY_FILE", tmp_path / ".tui-history")
        monkeypatch.setattr(tui, "load_env", lambda: None)
        monkeypatch.setattr(tui, "resolve_provider", lambda name=None: PROVIDER)
        yield tmp_path
        for h in list(tui.audit_log.handlers):
            tui.audit_log.removeHandler(h)
            h.close()

    def _inputs(self, monkeypatch, answers):
        it = iter(answers)

        def ask(*a, **k):
            v = next(it)
            if isinstance(v, BaseException):
                raise v
            return v

        monkeypatch.setattr(tui.Prompt, "ask", ask)

    def test_turn_error_does_not_kill_session(self, monkeypatch, repl_env):
        calls = []

        def pick(msg, provider):
            calls.append(msg)
            return {"answers": {}}  # malformed → ValueError di parse_decision

        monkeypatch.setattr(tui, "pick_tool", pick)
        self._inputs(monkeypatch, ["first", "second", EOFError()])
        tui.repl()
        assert calls == ["first", "second"]
        events = [json.loads(l)["event"] for l in (repl_env / ".tui-audit.log").read_text().splitlines()]
        assert events.count("error") == 2

    def test_execute_logged_with_mcp_tool(self, monkeypatch, repl_env):
        monkeypatch.setattr(tui, "pick_tool", lambda msg, p: _resp())
        monkeypatch.setattr(tui.Confirm, "ask", lambda *a, **k: True)
        ran = []

        class Done:
            returncode = 0

        monkeypatch.setattr(tui.subprocess, "run", lambda argv, check: ran.append(argv) or Done())
        self._inputs(monkeypatch, ["cek doctor", EOFError()])
        tui.repl()
        assert ran == [["bq", "--doctor"]]
        entries = [json.loads(l) for l in (repl_env / ".tui-audit.log").read_text().splitlines()]
        execute = next(e for e in entries if e["event"] == "execute")
        assert execute["mcp_tool"] == "build_doctor"
        assert execute["exit_code"] == 0
        assert execute["source"] == "tui"

    def test_risky_command_defaults_to_no(self, monkeypatch, repl_env):
        monkeypatch.setattr(tui, "pick_tool", lambda msg, p: _resp(tool="bq_sops_decrypt"))
        defaults = []
        monkeypatch.setattr(tui.Confirm, "ask", lambda *a, default: defaults.append(default) or False)
        self._inputs(monkeypatch, ["decrypt secret.yaml", "secret.yaml", EOFError()])
        tui.repl()
        assert defaults == [False]


class TestLoadEnv:
    def test_cwd_env_not_loaded(self, monkeypatch, tmp_path):
        (tmp_path / ".env").write_text("BQ_TEST_CWD_SECRET=leak\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("BQ_TEST_CWD_SECRET", raising=False)
        monkeypatch.setattr(tui, "ENV_FILE", tmp_path / "missing" / ".env")
        assert tmp_path / ".env" not in tui._candidate_env_paths()


class TestCatalogSource:
    @pytest.fixture
    def cache_file(self, monkeypatch, tmp_path):
        path = tmp_path / ".tui-cache.json"
        monkeypatch.setattr(tui_catalog, "CACHE_FILE", path)
        monkeypatch.setattr(tui_catalog, "CONFIG_DIR", tmp_path)
        return path

    def _fail(self):
        raise tui_catalog.CatalogError("down")

    def test_stale_cache_labelled(self, monkeypatch, cache_file):
        cache_file.write_text(json.dumps({"fetched_at": 1, "collections": {}}))
        monkeypatch.setattr(tui_catalog, "_fetch_all_from_pb", self._fail)
        assert tui_catalog.load_catalog()["source"] == "stale-cache"

    def test_bootstrap_labelled(self, monkeypatch, cache_file):
        monkeypatch.setattr(tui_catalog, "_fetch_all_from_pb", self._fail)
        cat = tui_catalog.load_catalog()
        assert cat["source"] == "bootstrap"
        assert "bq_doctor" in cat["tools"]

    def test_fresh_fetch_and_cache_labelled(self, monkeypatch, cache_file):
        monkeypatch.setattr(tui_catalog, "_fetch_all_from_pb", lambda: {"build_q_tools": []})
        assert tui_catalog.load_catalog()["source"] == "pb"
        assert tui_catalog.load_catalog()["source"] == "cache"


def test_in_process_catalog_respects_ttl(monkeypatch):
    loads = []
    monkeypatch.setattr(tui_catalog, "load_catalog", lambda force_refresh=False: loads.append(1) or CATALOG)
    monkeypatch.setenv("BUILD_Q_TUI_CACHE_TTL", "10")
    monkeypatch.setattr(tui, "_catalog_loaded_at", time.monotonic() - 11)
    tui.catalog()
    tui.catalog()
    assert loads == [1]
