"""Tests for MCP Phase 3 — prompts, SSE transport, audit logging, error handling."""

from unittest.mock import patch
import pytest


class TestPrompts:
    def test_prompt_specs_registered(self):
        from build_q.mcp.prompts import PROMPT_SPECS
        assert len(PROMPT_SPECS) == 3
        expected = {"deploy-new-service", "scan-and-fix", "rollout-update"}
        assert set(PROMPT_SPECS.keys()) == expected

    def test_deploy_new_service_messages(self):
        from build_q.mcp.prompts import get_deploy_new_service_messages
        msgs = get_deploy_new_service_messages("org/app", "v1.0", "production")
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert "org/app" in msgs[0]["content"]
        assert "v1.0" in msgs[0]["content"]
        assert "production" in msgs[0]["content"]
        assert "build_doctor" in msgs[0]["content"]
        assert "gitops_bootstrap" in msgs[0]["content"]

    def test_scan_and_fix_messages(self):
        from build_q.mcp.prompts import get_scan_and_fix_messages
        msgs = get_scan_and_fix_messages("org/app", "develop")
        assert len(msgs) == 1
        assert "dockerfile_scan_remote" in msgs[0]["content"]
        assert "k8s_anomaly_scan" in msgs[0]["content"]
        assert "develop" in msgs[0]["content"]

    def test_scan_and_fix_default_ref(self):
        from build_q.mcp.prompts import get_scan_and_fix_messages
        msgs = get_scan_and_fix_messages("org/app")
        assert "main" in msgs[0]["content"]

    def test_rollout_update_messages(self):
        from build_q.mcp.prompts import get_rollout_update_messages
        msgs = get_rollout_update_messages("org/app", "v2.0", "staging-qoin")
        assert len(msgs) == 1
        assert "image_check" in msgs[0]["content"]
        assert "k8s_set_image" in msgs[0]["content"]
        assert "gitops_set_image" in msgs[0]["content"]
        assert "staging-qoin" in msgs[0]["content"]

    def test_prompt_specs_have_required_fields(self):
        from build_q.mcp.prompts import PROMPT_SPECS
        for name, spec in PROMPT_SPECS.items():
            assert spec["name"] == name
            assert len(spec["description"]) > 10
            assert isinstance(spec["arguments"], list)


class TestCaptureAuditLogging:
    def test_elapsed_ms_in_result(self):
        from build_q.mcp._capture import run_captured

        def slow_func():
            return 0

        result = run_captured(slow_func)
        assert "elapsed_ms" in result
        assert isinstance(result["elapsed_ms"], int)
        assert result["elapsed_ms"] >= 0

    def test_error_type_in_exception(self):
        from build_q.mcp._capture import run_captured

        def fails():
            raise ValueError("boom")

        result = run_captured(fails)
        assert result["exit_code"] == 2
        assert result["error_type"] == "ValueError"
        assert "boom" in result["error"]

    def test_logger_called(self):
        from build_q.mcp._capture import run_captured
        with patch("build_q.mcp._capture.logger") as mock_logger:
            run_captured(lambda: 0)
            mock_logger.info.assert_called_once()
            call_args = mock_logger.info.call_args[0]
            assert "tool=" in call_args[0]
            assert "exit_code=" in call_args[0]


class TestSSETransportArgs:
    def test_run_mcp_server_accepts_transport(self):
        from build_q.mcp.server import run_mcp_server
        import inspect
        sig = inspect.signature(run_mcp_server)
        params = list(sig.parameters.keys())
        assert "transport" in params
        assert "port" in params
        assert "host" in params

    def test_run_mcp_server_defaults(self):
        from build_q.mcp.server import run_mcp_server
        import inspect
        sig = inspect.signature(run_mcp_server)
        assert sig.parameters["transport"].default == "stdio"
        assert sig.parameters["port"].default == 0
        assert sig.parameters["host"].default == "127.0.0.1"


class TestServerPhase3:
    def test_prompts_importable(self):
        from build_q.mcp.prompts import PROMPT_SPECS, register_prompts
        assert callable(register_prompts)

    def test_server_imports_prompts(self):
        from build_q.mcp.server import PROMPT_SPECS
        assert len(PROMPT_SPECS) == 3
