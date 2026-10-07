"""bq MCP server — expose build-q DevOps tools via Model Context Protocol."""

from __future__ import annotations

import json
import logging

from build_q import __version__
from build_q.mcp.tools.scan import SCAN_TOOLS, handle_scan_tool
from build_q.mcp.tools.infra import INFRA_TOOLS, handle_infra_tool
from build_q.mcp.tools.cicd import CICD_TOOLS, handle_cicd_tool
from build_q.mcp.resources import RESOURCES, handle_resource

logger = logging.getLogger("bq-mcp")

ALL_TOOLS: dict[str, dict] = {}
ALL_TOOLS.update(SCAN_TOOLS)
ALL_TOOLS.update(INFRA_TOOLS)
ALL_TOOLS.update(CICD_TOOLS)

TOOL_HANDLERS = {
    **{name: handle_scan_tool for name in SCAN_TOOLS},
    **{name: handle_infra_tool for name in INFRA_TOOLS},
    **{name: handle_cicd_tool for name in CICD_TOOLS},
}


def create_server():
    """Create and configure the MCP server with all tools and resources."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name="bq-mcp-server",
        version=__version__,
        description="DevOps tools from build-q (bq) CLI — Dockerfile scanning, "
                    "K8s anomaly detection, CI/CD pipeline checks, GitOps ops.",
    )

    _register_scan_tools(server)
    _register_infra_tools(server)
    _register_cicd_tools(server)
    _register_resources(server)

    return server


def _register_scan_tools(server):
    """Register scan tools with typed function signatures."""

    @server.tool(
        name="dockerfile_scan",
        description=SCAN_TOOLS["dockerfile_scan"]["description"],
    )
    def dockerfile_scan(path: str = ".", use_jev: bool = False) -> str:
        return json.dumps(handle_scan_tool("dockerfile_scan", {
            "path": path, "use_jev": use_jev,
        }), default=str)

    @server.tool(
        name="dockerfile_scan_remote",
        description=SCAN_TOOLS["dockerfile_scan_remote"]["description"],
    )
    def dockerfile_scan_remote(
        repo: str,
        ref: str = "main",
        dockerfile_path: str = "Dockerfile",
        use_jev: bool = False,
    ) -> str:
        return json.dumps(handle_scan_tool("dockerfile_scan_remote", {
            "repo": repo, "ref": ref,
            "dockerfile_path": dockerfile_path, "use_jev": use_jev,
        }), default=str)

    @server.tool(
        name="k8s_anomaly_scan",
        description=SCAN_TOOLS["k8s_anomaly_scan"]["description"],
    )
    def k8s_anomaly_scan(
        path: str,
        glob_pattern: str = "*_deployment.yaml",
        use_jev: bool = False,
    ) -> str:
        return json.dumps(handle_scan_tool("k8s_anomaly_scan", {
            "path": path, "glob_pattern": glob_pattern, "use_jev": use_jev,
        }), default=str)

    @server.tool(
        name="dockerfile_fix",
        description=SCAN_TOOLS["dockerfile_fix"]["description"],
    )
    def dockerfile_fix(path: str = ".", use_jev: bool = False) -> str:
        return json.dumps(handle_scan_tool("dockerfile_fix", {
            "path": path, "use_jev": use_jev,
        }), default=str)


def _register_infra_tools(server):
    """Register infrastructure tools with typed function signatures."""

    @server.tool(
        name="build_doctor",
        description=INFRA_TOOLS["build_doctor"]["description"],
    )
    def build_doctor() -> str:
        return json.dumps(handle_infra_tool("build_doctor", {}), default=str)

    @server.tool(
        name="config_show",
        description=INFRA_TOOLS["config_show"]["description"],
    )
    def config_show() -> str:
        return json.dumps(handle_infra_tool("config_show", {}), default=str)


def _register_cicd_tools(server):
    """Register CI/CD tools with typed function signatures."""

    @server.tool(
        name="pipeline_check",
        description=CICD_TOOLS["pipeline_check"]["description"],
    )
    def pipeline_check(
        repo: str,
        ref: str = "main",
        namespace: str | None = None,
        infra: str | None = None,
    ) -> str:
        return json.dumps(handle_cicd_tool("pipeline_check", {
            "repo": repo, "ref": ref,
            "namespace": namespace, "infra": infra,
        }), default=str)

    @server.tool(
        name="gitops_match_check",
        description=CICD_TOOLS["gitops_match_check"]["description"],
    )
    def gitops_match_check(
        namespace: str,
        deployment: str,
        gitops_repo: str,
        gitops_path: str,
        gitops_branch: str = "main",
    ) -> str:
        return json.dumps(handle_cicd_tool("gitops_match_check", {
            "namespace": namespace, "deployment": deployment,
            "gitops_repo": gitops_repo, "gitops_branch": gitops_branch,
            "gitops_path": gitops_path,
        }), default=str)

    @server.tool(
        name="image_check",
        description=CICD_TOOLS["image_check"]["description"],
    )
    def image_check(image: str) -> str:
        return json.dumps(handle_cicd_tool("image_check", {
            "image": image,
        }), default=str)

    @server.tool(
        name="webhook_status",
        description=CICD_TOOLS["webhook_status"]["description"],
    )
    def webhook_status(repo: str) -> str:
        return json.dumps(handle_cicd_tool("webhook_status", {
            "repo": repo,
        }), default=str)


def _register_resources(server):
    """Register all MCP resources."""

    for uri, spec in RESOURCES.items():
        _make_resource(server, uri, spec)


def _make_resource(server, uri: str, spec: dict):
    """Register a single resource handler."""

    @server.resource(
        uri,
        name=spec.get("name", uri),
        description=spec.get("description", ""),
        mime_type=spec.get("mimeType", "application/json"),
    )
    def _handler() -> str:
        return json.dumps(handle_resource(uri), default=str)


def run_mcp_server():
    """Run MCP server with stdio transport (blocking)."""
    server = create_server()
    logger.info("bq-mcp-server v%s starting (stdio)", __version__)
    server.run(transport="stdio")
