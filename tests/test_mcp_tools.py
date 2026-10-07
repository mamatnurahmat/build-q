"""Tests for MCP tool handlers — unit tests with mocked underlying functions."""

from unittest.mock import patch, MagicMock
import pytest


class TestScanTools:
    def test_dockerfile_scan_dispatches(self):
        with patch("build_q.dockerfile_scanner.run_dockerfile_scan", return_value=0) as mock:
            from build_q.mcp.tools.scan import handle_scan_tool
            result = handle_scan_tool("dockerfile_scan", {"path": "/tmp/test"})
            assert result["exit_code"] == 0
            mock.assert_called_once_with(
                "/tmp/test",
                use_jev=False,
                auto_fix=False,
                scan_only=True,
            )

    def test_dockerfile_scan_default_path(self):
        with patch("build_q.dockerfile_scanner.run_dockerfile_scan", return_value=0) as mock:
            from build_q.mcp.tools.scan import handle_scan_tool
            result = handle_scan_tool("dockerfile_scan", {})
            mock.assert_called_once_with(
                ".",
                use_jev=False,
                auto_fix=False,
                scan_only=True,
            )

    def test_dockerfile_scan_remote_dispatches(self):
        with patch("build_q.dockerfile_scanner.run_dockerfile_scan_remote", return_value=1) as mock:
            from build_q.mcp.tools.scan import handle_scan_tool
            result = handle_scan_tool("dockerfile_scan_remote", {
                "repo": "org/repo",
                "ref": "develop",
            })
            assert result["exit_code"] == 1
            mock.assert_called_once_with(
                "org/repo",
                "develop",
                dockerfile_path="Dockerfile",
                use_jev=False,
            )

    def test_k8s_anomaly_scan_dispatches(self):
        with patch("build_q.anomaly.run_anomaly_scan", return_value=2) as mock:
            from build_q.mcp.tools.scan import handle_scan_tool
            result = handle_scan_tool("k8s_anomaly_scan", {
                "path": "/tmp/manifests",
                "glob_pattern": "*.yaml",
            })
            assert result["exit_code"] == 2
            mock.assert_called_once_with(
                "/tmp/manifests",
                use_jev=False,
                glob_pattern="*.yaml",
            )

    def test_dockerfile_fix_dispatches(self):
        with patch("build_q.dockerfile_scanner.run_dockerfile_scan", return_value=0) as mock:
            from build_q.mcp.tools.scan import handle_scan_tool
            result = handle_scan_tool("dockerfile_fix", {"path": "/tmp/fix"})
            mock.assert_called_once_with(
                "/tmp/fix",
                use_jev=False,
                auto_fix=True,
                scan_only=False,
            )

    def test_unknown_scan_tool(self):
        from build_q.mcp.tools.scan import handle_scan_tool
        result = handle_scan_tool("nonexistent", {})
        assert "error" in result


class TestInfraTools:
    def test_build_doctor_dispatches(self):
        with patch("build_q.doctor.run_doctor", return_value=0) as mock:
            from build_q.mcp.tools.infra import handle_infra_tool
            result = handle_infra_tool("build_doctor", {})
            assert result["exit_code"] == 0
            mock.assert_called_once()

    def test_config_show_returns_masked(self):
        fake_config = {
            "github": {"token": "ghp_abcdef123456789xyz"},
            "registry": {"url": "loyaltolpi"},
        }
        with patch("build_q.config.load_config", return_value=fake_config):
            from build_q.mcp.tools.infra import handle_infra_tool
            result = handle_infra_tool("config_show", {})
            assert result["exit_code"] == 0
            assert result["data"]["github"]["token"] != "ghp_abcdef123456789xyz"
            assert "****" in result["data"]["github"]["token"]
            assert result["data"]["registry"]["url"] == "loyaltolpi"

    def test_config_show_handles_exception(self):
        with patch("build_q.config.load_config", side_effect=RuntimeError("fail")):
            from build_q.mcp.tools.infra import handle_infra_tool
            result = handle_infra_tool("config_show", {})
            assert result["exit_code"] == 2
            assert "RuntimeError" in result["error"]

    def test_unknown_infra_tool(self):
        from build_q.mcp.tools.infra import handle_infra_tool
        result = handle_infra_tool("nonexistent", {})
        assert "error" in result


class TestMaskSensitive:
    def test_masks_token(self):
        from build_q.mcp.tools.infra import _mask_sensitive
        data = {"token": "ghp_abcdefghijklmnop"}
        masked = _mask_sensitive(data)
        assert masked["token"].startswith("ghp_")
        assert "****" in masked["token"]

    def test_masks_nested(self):
        from build_q.mcp.tools.infra import _mask_sensitive
        data = {"github": {"token": "ghp_longtoken123456"}, "name": "test"}
        masked = _mask_sensitive(data)
        assert "****" in masked["github"]["token"]
        assert masked["name"] == "test"

    def test_masks_short_value(self):
        from build_q.mcp.tools.infra import _mask_sensitive
        data = {"password": "abc"}
        masked = _mask_sensitive(data)
        assert masked["password"] == "****"

    def test_preserves_non_sensitive(self):
        from build_q.mcp.tools.infra import _mask_sensitive
        data = {"builder": "mybuilder", "registry": "loyaltolpi"}
        masked = _mask_sensitive(data)
        assert masked == data


class TestCicdTools:
    def test_pipeline_check_dispatches(self):
        with patch("build_q.check.run_check", return_value=0) as mock:
            from build_q.mcp.tools.cicd import handle_cicd_tool
            result = handle_cicd_tool("pipeline_check", {
                "repo": "org/repo",
                "ref": "staging",
                "namespace": "staging-qoin",
            })
            assert result["exit_code"] == 0
            mock.assert_called_once_with(
                "org/repo",
                "staging",
                ns="staging-qoin",
                infra=None,
            )

    def test_gitops_match_check_dispatches(self):
        with patch("build_q.is_match_image.run_is_match_image", return_value=1) as mock:
            from build_q.mcp.tools.cicd import handle_cicd_tool
            result = handle_cicd_tool("gitops_match_check", {
                "namespace": "staging-qoin",
                "deployment": "my-app",
                "gitops_repo": "org/gitops",
                "gitops_path": "cce/staging/app_deployment.yaml",
            })
            assert result["exit_code"] == 1

    def test_image_check_exists(self):
        with patch("build_q.builder.check_image_exists", return_value=True):
            from build_q.mcp.tools.cicd import handle_cicd_tool
            result = handle_cicd_tool("image_check", {
                "image": "loyaltolpi/app:v1.0",
            })
            assert result["exit_code"] == 0
            assert result["data"]["exists"] is True

    def test_image_check_not_exists(self):
        with patch("build_q.builder.check_image_exists", return_value=False):
            from build_q.mcp.tools.cicd import handle_cicd_tool
            result = handle_cicd_tool("image_check", {
                "image": "loyaltolpi/app:v999",
            })
            assert result["exit_code"] == 0
            assert result["data"]["exists"] is False

    def test_webhook_status_dispatches(self):
        with patch("build_q.cicd_webhook.run_cicd_webhook_check", return_value=0) as mock:
            from build_q.mcp.tools.cicd import handle_cicd_tool
            result = handle_cicd_tool("webhook_status", {
                "repo": "org/repo",
            })
            assert result["exit_code"] == 0
            mock.assert_called_once_with("org/repo", auto_setup=False)

    def test_unknown_cicd_tool(self):
        from build_q.mcp.tools.cicd import handle_cicd_tool
        result = handle_cicd_tool("nonexistent", {})
        assert "error" in result
