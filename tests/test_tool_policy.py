"""Tests for build_q.tool_policy — risk tiers shared by MCP, TUI, and serve."""

import pytest

from build_q.tool_policy import (
    CLI_FLAG_TO_MCP,
    MCP_TOOL_POLICY,
    annotations_for,
    classify_command,
    command_policy,
)


class TestPolicyCoverage:
    def test_every_mcp_tool_has_policy(self):
        from build_q.mcp.server import ALL_TOOLS
        assert set(MCP_TOOL_POLICY) == set(ALL_TOOLS)

    def test_cli_flags_map_to_known_tools(self):
        for flag, tool in CLI_FLAG_TO_MCP.items():
            assert tool in MCP_TOOL_POLICY, flag

    @pytest.mark.parametrize("tool,tier", [
        ("build_doctor", "read"),
        ("docker_build_preview", "read"),
        ("dockerfile_fix", "local"),
        ("sops_decrypt", "local"),
        ("docker_build", "external"),
        ("k8s_set_image", "external"),
    ])
    def test_tiers_match_mcp_setup_doc(self, tool, tier):
        assert MCP_TOOL_POLICY[tool].tier == tier

    def test_external_tools_described_as_destructive_or_external(self):
        from build_q.mcp.server import ALL_TOOLS
        for name, policy in MCP_TOOL_POLICY.items():
            if policy.tier == "external":
                desc = ALL_TOOLS[name]["description"]
                assert "DESTRUCTIVE" in desc or "EXTERNAL ACTION" in desc, name


class TestAnnotations:
    def test_read_only_tool(self):
        ann = annotations_for("image_check")
        assert ann["read_only_hint"] is True
        assert ann["destructive_hint"] is False
        assert ann["open_world_hint"] is True

    def test_external_tool(self):
        ann = annotations_for("pipeline_trigger")
        assert ann["read_only_hint"] is False
        assert ann["destructive_hint"] is True

    def test_annotations_accepted_by_sdk(self):
        types = pytest.importorskip("mcp.types")
        for tool in MCP_TOOL_POLICY:
            types.ToolAnnotations(**annotations_for(tool))


class TestClassifyCommand:
    @pytest.mark.parametrize("cmd,expected", [
        ("bq --doctor", "build_doctor"),
        ("bq --sops-decrypt secret.yaml", "sops_decrypt"),
        ("bq --set-image production-payout app v1.2.3", "k8s_set_image"),
        ("bq --gitops-set-image org/gitops main cce/x.yaml img:v1", "gitops_set_image"),
        ("bq --bootstrap-k8s org/app v1 --gitops-repo org/gitops", "gitops_bootstrap"),
        ("bq --cicd-trigger org/app develop", "pipeline_trigger"),
        ("bq --check org/app main --remote", "pipeline_check"),
        ("bq app develop --local", "docker_build"),
        ("bq app develop --local --no-push", "docker_build"),
        ("bq app develop --local --dry-run", "docker_build_preview"),
        ("bq --clone org/app v1 --local --clean", "docker_build"),
    ])
    def test_known_commands(self, cmd, expected):
        assert classify_command(cmd) == expected

    @pytest.mark.parametrize("cmd", [
        "bq --pb-status",
        "python3 -m drift_checker.worker ns",
        "",
        "bq 'unterminated",
    ])
    def test_unknown_commands(self, cmd):
        assert classify_command(cmd) is None
        assert command_policy(cmd) == (None, None)
