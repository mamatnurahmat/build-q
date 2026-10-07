"""bq MCP server — expose build-q DevOps tools via Model Context Protocol."""

from __future__ import annotations

import json
import logging

from build_q import __version__
from build_q.mcp.tools.scan import SCAN_TOOLS, handle_scan_tool
from build_q.mcp.tools.infra import INFRA_TOOLS, handle_infra_tool
from build_q.mcp.tools.cicd import CICD_TOOLS, handle_cicd_tool
from build_q.mcp.tools.build import BUILD_TOOLS, handle_build_tool
from build_q.mcp.tools.gitops import GITOPS_TOOLS, handle_gitops_tool
from build_q.mcp.tools.k8s import K8S_TOOLS, handle_k8s_tool
from build_q.mcp.tools.sops import SOPS_TOOLS, handle_sops_tool
from build_q.mcp.resources import RESOURCES, handle_resource
from build_q.mcp.prompts import register_prompts, PROMPT_SPECS

logger = logging.getLogger("bq-mcp")

ALL_TOOLS: dict[str, dict] = {}
ALL_TOOLS.update(SCAN_TOOLS)
ALL_TOOLS.update(INFRA_TOOLS)
ALL_TOOLS.update(CICD_TOOLS)
ALL_TOOLS.update(BUILD_TOOLS)
ALL_TOOLS.update(GITOPS_TOOLS)
ALL_TOOLS.update(K8S_TOOLS)
ALL_TOOLS.update(SOPS_TOOLS)

TOOL_HANDLERS = {
    **{name: handle_scan_tool for name in SCAN_TOOLS},
    **{name: handle_infra_tool for name in INFRA_TOOLS},
    **{name: handle_cicd_tool for name in CICD_TOOLS},
    **{name: handle_build_tool for name in BUILD_TOOLS},
    **{name: handle_gitops_tool for name in GITOPS_TOOLS},
    **{name: handle_k8s_tool for name in K8S_TOOLS},
    **{name: handle_sops_tool for name in SOPS_TOOLS},
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
    _register_build_tools(server)
    _register_gitops_tools(server)
    _register_k8s_tools(server)
    _register_sops_tools(server)
    _register_resources(server)
    register_prompts(server)

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
        name="pipeline_trigger",
        description=CICD_TOOLS["pipeline_trigger"]["description"],
    )
    def pipeline_trigger(
        repo: str,
        ref: str = "main",
        sha: str | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> str:
        return json.dumps(handle_cicd_tool("pipeline_trigger", {
            "repo": repo, "ref": ref, "sha": sha,
            "force": force, "dry_run": dry_run,
        }), default=str)

    @server.tool(
        name="webhook_status",
        description=CICD_TOOLS["webhook_status"]["description"],
    )
    def webhook_status(repo: str) -> str:
        return json.dumps(handle_cicd_tool("webhook_status", {
            "repo": repo,
        }), default=str)


def _register_build_tools(server):
    """Register Docker build tools."""

    @server.tool(
        name="docker_build",
        description=BUILD_TOOLS["docker_build"]["description"],
    )
    def docker_build(
        repo: str,
        ref: str,
        push: bool = False,
        tag: str | None = None,
        platform: str = "linux/amd64",
        dockerfile: str = "Dockerfile",
        dry_run: bool = False,
        build_args: list[str] | None = None,
    ) -> str:
        return json.dumps(handle_build_tool("docker_build", {
            "repo": repo, "ref": ref, "push": push, "tag": tag,
            "platform": platform, "dockerfile": dockerfile,
            "dry_run": dry_run, "build_args": build_args,
        }), default=str)

    @server.tool(
        name="docker_build_preview",
        description=BUILD_TOOLS["docker_build_preview"]["description"],
    )
    def docker_build_preview(
        repo: str,
        ref: str,
        tag: str | None = None,
        platform: str = "linux/amd64",
    ) -> str:
        return json.dumps(handle_build_tool("docker_build_preview", {
            "repo": repo, "ref": ref, "tag": tag, "platform": platform,
        }), default=str)


def _register_gitops_tools(server):
    """Register GitOps tools."""

    @server.tool(
        name="gitops_set_image",
        description=GITOPS_TOOLS["gitops_set_image"]["description"],
    )
    def gitops_set_image(
        repo: str,
        path: str,
        image: str,
        branch: str = "main",
    ) -> str:
        return json.dumps(handle_gitops_tool("gitops_set_image", {
            "repo": repo, "branch": branch, "path": path, "image": image,
        }), default=str)

    @server.tool(
        name="gitops_bootstrap",
        description=GITOPS_TOOLS["gitops_bootstrap"]["description"],
    )
    def gitops_bootstrap(
        source_repo: str,
        ref: str,
        gitops_repo: str,
        path_yaml: str,
        gitops_branch: str = "main",
        replicas: int = 2,
        stack: str | None = None,
        env: str | None = None,
        apply_secret: bool = False,
        kube_context: str | None = None,
        namespace: str | None = None,
        nodepool: str | None = None,
    ) -> str:
        return json.dumps(handle_gitops_tool("gitops_bootstrap", {
            "source_repo": source_repo, "ref": ref,
            "gitops_repo": gitops_repo, "gitops_branch": gitops_branch,
            "path_yaml": path_yaml, "replicas": replicas,
            "stack": stack, "env": env,
            "apply_secret": apply_secret, "kube_context": kube_context,
            "namespace": namespace, "nodepool": nodepool,
        }), default=str)


def _register_k8s_tools(server):
    """Register Kubernetes direct tools."""

    @server.tool(
        name="k8s_set_image",
        description=K8S_TOOLS["k8s_set_image"]["description"],
    )
    def k8s_set_image(
        namespace: str,
        deployment: str,
        image: str,
        container: str | None = None,
    ) -> str:
        return json.dumps(handle_k8s_tool("k8s_set_image", {
            "namespace": namespace, "deployment": deployment,
            "image": image, "container": container,
        }), default=str)


def _register_sops_tools(server):
    """Register SOPS encryption tools."""

    @server.tool(
        name="sops_encrypt",
        description=SOPS_TOOLS["sops_encrypt"]["description"],
    )
    def sops_encrypt(
        path: str,
        recipients: list[str] | None = None,
    ) -> str:
        return json.dumps(handle_sops_tool("sops_encrypt", {
            "path": path, "recipients": recipients,
        }), default=str)

    @server.tool(
        name="sops_decrypt",
        description=SOPS_TOOLS["sops_decrypt"]["description"],
    )
    def sops_decrypt(
        path: str,
        in_place: bool = False,
    ) -> str:
        return json.dumps(handle_sops_tool("sops_decrypt", {
            "path": path, "in_place": in_place,
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


def _setup_audit_log() -> None:
    """Configure audit logging to ~/.build-q/.mcp-audit.log."""
    import os
    from pathlib import Path

    log_dir = Path(os.environ.get("BQ_HOME", Path.home() / ".build-q"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / ".mcp-audit.log"

    handler = logging.FileHandler(str(log_file), encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        '{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def run_mcp_server(
    transport: str = "stdio",
    port: int = 0,
    host: str = "127.0.0.1",
) -> None:
    """Run MCP server with stdio or SSE transport."""
    _setup_audit_log()
    server = create_server()
    logger.info("bq-mcp-server v%s starting (%s)", __version__, transport)

    if transport == "sse" and port:
        server.run(transport="sse", host=host, port=port)
    else:
        server.run(transport="stdio")
