"""Tests for MCP server — tool/resource registration and dispatch."""

import json
from unittest.mock import patch
import pytest


class TestServerToolRegistry:
    def test_all_tools_registered(self):
        from build_q.mcp.server import ALL_TOOLS
        expected = {
            "dockerfile_scan", "dockerfile_scan_remote", "k8s_anomaly_scan",
            "dockerfile_fix", "build_doctor", "config_show",
            "pipeline_check", "gitops_match_check", "image_check",
            "webhook_status", "pipeline_trigger",
            "docker_build", "docker_build_preview",
            "gitops_set_image", "gitops_bootstrap",
            "k8s_set_image", "sops_encrypt", "sops_decrypt",
        }
        assert set(ALL_TOOLS.keys()) == expected

    def test_all_tools_have_handlers(self):
        from build_q.mcp.server import ALL_TOOLS, TOOL_HANDLERS
        for name in ALL_TOOLS:
            assert name in TOOL_HANDLERS, f"Missing handler for tool: {name}"

    def test_tool_count(self):
        from build_q.mcp.server import ALL_TOOLS
        assert len(ALL_TOOLS) == 18


class TestToolSchemas:
    def test_all_tools_have_input_schema(self):
        from build_q.mcp.server import ALL_TOOLS
        for name, spec in ALL_TOOLS.items():
            assert "inputSchema" in spec, f"Tool {name} missing inputSchema"
            assert spec["inputSchema"].get("type") == "object", (
                f"Tool {name} inputSchema type must be 'object'"
            )

    def test_required_fields_exist_in_properties(self):
        from build_q.mcp.server import ALL_TOOLS
        for name, spec in ALL_TOOLS.items():
            schema = spec["inputSchema"]
            required = schema.get("required", [])
            properties = schema.get("properties", {})
            for field in required:
                assert field in properties, (
                    f"Tool {name}: required field '{field}' not in properties"
                )

    def test_all_tools_have_name_and_description(self):
        from build_q.mcp.server import ALL_TOOLS
        for name, spec in ALL_TOOLS.items():
            assert spec["name"] == name
            assert len(spec["description"]) > 10


class TestResources:
    def test_all_resources_registered(self):
        from build_q.mcp.resources import RESOURCES
        expected = {"bq://version", "bq://config", "bq://doctor", "bq://catalog"}
        assert set(RESOURCES.keys()) == expected

    def test_version_resource(self):
        from build_q.mcp.resources import handle_resource
        from build_q import __version__
        result = handle_resource("bq://version")
        assert result["version"] == __version__
        assert "capabilities" in result
        assert isinstance(result["capabilities"], list)
        assert len(result["capabilities"]) == 18

    def test_config_resource_masks_sensitive(self):
        fake_config = {"github": {"token": "ghp_verylongtoken123"}}
        with patch("build_q.config.load_config", return_value=fake_config):
            from build_q.mcp.resources import handle_resource
            result = handle_resource("bq://config")
            assert "****" in result["github"]["token"]

    def test_unknown_resource(self):
        from build_q.mcp.resources import handle_resource
        result = handle_resource("bq://nonexistent")
        assert "error" in result

    def test_resources_have_required_fields(self):
        from build_q.mcp.resources import RESOURCES
        for uri, spec in RESOURCES.items():
            assert spec["uri"] == uri
            assert "name" in spec
            assert "description" in spec
            assert spec["mimeType"] == "application/json"
