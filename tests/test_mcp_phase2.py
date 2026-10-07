"""Tests for MCP Phase 2 action tools — build, gitops, k8s, sops, pipeline_trigger."""

from pathlib import Path
from unittest.mock import patch
import pytest


class TestBuildTools:
    def test_docker_build_dispatches(self):
        with patch("build_q.builder.run_build", return_value=0) as mock:
            from build_q.mcp.tools.build import handle_build_tool
            result = handle_build_tool("docker_build", {
                "repo": "my-app",
                "ref": "staging",
                "push": True,
                "platform": "linux/amd64",
            })
            assert result["exit_code"] == 0
            mock.assert_called_once_with(
                "my-app", "staging",
                platform="linux/amd64",
                push=True,
                tag=None,
                dockerfile="Dockerfile",
                extra_build_args=None,
                dry_run=False,
            )

    def test_docker_build_with_tag(self):
        with patch("build_q.builder.run_build", return_value=0) as mock:
            from build_q.mcp.tools.build import handle_build_tool
            result = handle_build_tool("docker_build", {
                "repo": "my-app",
                "ref": "v1.2.3",
                "tag": "custom:v1",
            })
            assert mock.call_args.kwargs["tag"] == "custom:v1"

    def test_docker_build_preview_forces_dry_run(self):
        with patch("build_q.builder.run_build", return_value=0) as mock:
            from build_q.mcp.tools.build import handle_build_tool
            result = handle_build_tool("docker_build_preview", {
                "repo": "my-app",
                "ref": "main",
            })
            assert result["exit_code"] == 0
            assert mock.call_args.kwargs["dry_run"] is True

    def test_unknown_build_tool(self):
        from build_q.mcp.tools.build import handle_build_tool
        result = handle_build_tool("nonexistent", {})
        assert "error" in result


class TestGitopsTools:
    def test_gitops_set_image_dispatches(self):
        with patch("build_q.gitops_set_image.run_gitops_set_image", return_value=0) as mock:
            from build_q.mcp.tools.gitops import handle_gitops_tool
            result = handle_gitops_tool("gitops_set_image", {
                "repo": "org/gitops",
                "branch": "main",
                "path": "cce/staging/app_deployment.yaml",
                "image": "loyaltolpi/app:v1.2.3",
            })
            assert result["exit_code"] == 0
            mock.assert_called_once_with(
                "org/gitops", "main",
                "cce/staging/app_deployment.yaml",
                "loyaltolpi/app:v1.2.3",
            )

    def test_gitops_bootstrap_dispatches(self):
        with patch("build_q.bootstrap.run_bootstrap_k8s", return_value=0) as mock:
            from build_q.mcp.tools.gitops import handle_gitops_tool
            result = handle_gitops_tool("gitops_bootstrap", {
                "source_repo": "org/my-app",
                "ref": "staging",
                "gitops_repo": "org/gitops",
                "gitops_branch": "main",
                "path_yaml": "cce/staging-qoin/my-app",
                "replicas": 3,
            })
            assert result["exit_code"] == 0
            mock.assert_called_once_with(
                "org/my-app", "staging",
                "org/gitops", "main",
                "cce/staging-qoin/my-app",
                replicas=3,
                stack_override=None,
                env_override=None,
                apply_secret=False,
                kube_context=None,
                namespace_override=None,
                nodepool_override=None,
            )

    def test_gitops_bootstrap_with_overrides(self):
        with patch("build_q.bootstrap.run_bootstrap_k8s", return_value=0) as mock:
            from build_q.mcp.tools.gitops import handle_gitops_tool
            result = handle_gitops_tool("gitops_bootstrap", {
                "source_repo": "org/app",
                "ref": "main",
                "gitops_repo": "org/gitops",
                "path_yaml": "cce/prod/app",
                "stack": "dotnet",
                "env": "production",
                "namespace": "production-qoin",
                "nodepool": "prod-nodepool",
            })
            assert mock.call_args.kwargs["stack_override"] == "dotnet"
            assert mock.call_args.kwargs["env_override"] == "production"
            assert mock.call_args.kwargs["namespace_override"] == "production-qoin"
            assert mock.call_args.kwargs["nodepool_override"] == "prod-nodepool"

    def test_unknown_gitops_tool(self):
        from build_q.mcp.tools.gitops import handle_gitops_tool
        result = handle_gitops_tool("nonexistent", {})
        assert "error" in result


class TestK8sTools:
    def test_k8s_set_image_dispatches(self):
        with patch("build_q.set_image.run_set_image", return_value=0) as mock:
            from build_q.mcp.tools.k8s import handle_k8s_tool
            result = handle_k8s_tool("k8s_set_image", {
                "namespace": "staging-qoin",
                "deployment": "my-app",
                "image": "loyaltolpi/my-app:abc1234",
            })
            assert result["exit_code"] == 0
            mock.assert_called_once_with(
                "staging-qoin", "my-app", "loyaltolpi/my-app:abc1234",
                container=None,
            )

    def test_k8s_set_image_with_container(self):
        with patch("build_q.set_image.run_set_image", return_value=0) as mock:
            from build_q.mcp.tools.k8s import handle_k8s_tool
            result = handle_k8s_tool("k8s_set_image", {
                "namespace": "staging-qoin",
                "deployment": "my-app",
                "image": "new:tag",
                "container": "main-container",
            })
            assert mock.call_args.kwargs["container"] == "main-container"

    def test_unknown_k8s_tool(self):
        from build_q.mcp.tools.k8s import handle_k8s_tool
        result = handle_k8s_tool("nonexistent", {})
        assert "error" in result


class TestSopsTools:
    def test_sops_encrypt_dispatches(self):
        with patch("build_q.sops.encrypt_file", return_value=("age1xxx", "config")) as mock:
            from build_q.mcp.tools.sops import handle_sops_tool
            result = handle_sops_tool("sops_encrypt", {
                "path": "/tmp/secret.yaml",
            })
            assert result["exit_code"] == 0
            mock.assert_called_once_with(
                Path("/tmp/secret.yaml"),
                recipients=None,
            )

    def test_sops_encrypt_with_recipients(self):
        with patch("build_q.sops.encrypt_file", return_value=("age1a,age1b", "cli")) as mock:
            from build_q.mcp.tools.sops import handle_sops_tool
            result = handle_sops_tool("sops_encrypt", {
                "path": "/tmp/secret.yaml",
                "recipients": ["age1a", "age1b"],
            })
            assert mock.call_args.kwargs["recipients"] == ["age1a", "age1b"]

    def test_sops_decrypt_dispatches(self):
        with patch("build_q.sops.decrypt_file", return_value="plaintext content") as mock:
            from build_q.mcp.tools.sops import handle_sops_tool
            result = handle_sops_tool("sops_decrypt", {
                "path": "/tmp/encrypted.yaml",
                "in_place": False,
            })
            assert result["exit_code"] == 0
            mock.assert_called_once_with(
                Path("/tmp/encrypted.yaml"),
                in_place=False,
            )

    def test_sops_decrypt_in_place(self):
        with patch("build_q.sops.decrypt_file", return_value="content") as mock:
            from build_q.mcp.tools.sops import handle_sops_tool
            result = handle_sops_tool("sops_decrypt", {
                "path": "/tmp/enc.yaml",
                "in_place": True,
            })
            assert mock.call_args.kwargs["in_place"] is True

    def test_unknown_sops_tool(self):
        from build_q.mcp.tools.sops import handle_sops_tool
        result = handle_sops_tool("nonexistent", {})
        assert "error" in result


class TestPipelineTrigger:
    def test_pipeline_trigger_dispatches(self):
        with patch("build_q.cicd_trigger.run_cicd_trigger", return_value=0) as mock:
            from build_q.mcp.tools.cicd import handle_cicd_tool
            result = handle_cicd_tool("pipeline_trigger", {
                "repo": "org/my-app",
                "ref": "main",
            })
            assert result["exit_code"] == 0
            mock.assert_called_once_with(
                "org/my-app", "main",
                sha_override=None,
                force=False,
                dry_run=False,
            )

    def test_pipeline_trigger_with_force(self):
        with patch("build_q.cicd_trigger.run_cicd_trigger", return_value=0) as mock:
            from build_q.mcp.tools.cicd import handle_cicd_tool
            result = handle_cicd_tool("pipeline_trigger", {
                "repo": "org/app",
                "ref": "develop",
                "force": True,
                "sha": "abc1234",
            })
            assert mock.call_args.kwargs["force"] is True
            assert mock.call_args.kwargs["sha_override"] == "abc1234"

    def test_pipeline_trigger_dry_run(self):
        with patch("build_q.cicd_trigger.run_cicd_trigger", return_value=0) as mock:
            from build_q.mcp.tools.cicd import handle_cicd_tool
            result = handle_cicd_tool("pipeline_trigger", {
                "repo": "org/app",
                "ref": "main",
                "dry_run": True,
            })
            assert mock.call_args.kwargs["dry_run"] is True


class TestServerPhase2Registration:
    def test_total_tool_count(self):
        from build_q.mcp.server import ALL_TOOLS
        assert len(ALL_TOOLS) == 18

    def test_phase2_tools_registered(self):
        from build_q.mcp.server import ALL_TOOLS
        phase2_tools = {
            "docker_build", "docker_build_preview",
            "pipeline_trigger",
            "gitops_set_image", "gitops_bootstrap",
            "k8s_set_image",
            "sops_encrypt", "sops_decrypt",
        }
        for name in phase2_tools:
            assert name in ALL_TOOLS, f"Phase 2 tool '{name}' not registered"

    def test_all_phase2_tools_have_handlers(self):
        from build_q.mcp.server import ALL_TOOLS, TOOL_HANDLERS
        for name in ALL_TOOLS:
            assert name in TOOL_HANDLERS, f"Missing handler for tool: {name}"

    def test_phase2_tools_have_schemas(self):
        from build_q.mcp.server import ALL_TOOLS
        phase2_tools = [
            "docker_build", "docker_build_preview", "pipeline_trigger",
            "gitops_set_image", "gitops_bootstrap", "k8s_set_image",
            "sops_encrypt", "sops_decrypt",
        ]
        for name in phase2_tools:
            spec = ALL_TOOLS[name]
            assert spec["inputSchema"]["type"] == "object"
            assert len(spec["description"]) > 10
