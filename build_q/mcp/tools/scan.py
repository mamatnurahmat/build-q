"""MCP tools for Dockerfile and K8s manifest scanning."""

from __future__ import annotations

from build_q.mcp._capture import run_captured

SCAN_TOOLS: dict[str, dict] = {
    "dockerfile_scan": {
        "name": "dockerfile_scan",
        "description": (
            "Scan a local Dockerfile for 22+ issues covering security, "
            "performance, compliance, and build errors. "
            "Returns structured findings with severity levels."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to Dockerfile or directory containing Dockerfile",
                    "default": ".",
                },
                "use_jev": {
                    "type": "boolean",
                    "description": "Use Jev AI for severity synthesis (requires network)",
                    "default": False,
                },
            },
        },
    },
    "dockerfile_scan_remote": {
        "name": "dockerfile_scan_remote",
        "description": (
            "Scan a Dockerfile from a GitHub repository without cloning. "
            "Fetches via GitHub API and runs 22+ checks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "GitHub repo in owner/repo format",
                },
                "ref": {
                    "type": "string",
                    "description": "Git ref (branch or tag)",
                    "default": "main",
                },
                "dockerfile_path": {
                    "type": "string",
                    "description": "Path to Dockerfile in repo",
                    "default": "Dockerfile",
                },
                "use_jev": {
                    "type": "boolean",
                    "description": "Use Jev AI for severity synthesis",
                    "default": False,
                },
            },
            "required": ["repo"],
        },
    },
    "k8s_anomaly_scan": {
        "name": "k8s_anomaly_scan",
        "description": (
            "Scan Kubernetes deployment manifests for 10+ anomalies: "
            "low replicas, missing imagePullSecret, no resource limits, "
            "insecure securityContext, etc."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to YAML file or directory",
                },
                "glob_pattern": {
                    "type": "string",
                    "description": "File glob for directory scan",
                    "default": "*_deployment.yaml",
                },
                "use_jev": {
                    "type": "boolean",
                    "description": "Use Jev AI for severity verdict",
                    "default": False,
                },
            },
            "required": ["path"],
        },
    },
    "dockerfile_fix": {
        "name": "dockerfile_fix",
        "description": (
            "Scan and auto-fix Dockerfile issues. "
            "Modifies the Dockerfile in-place with fixes for security, "
            "performance, and compliance issues."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to Dockerfile or directory",
                    "default": ".",
                },
                "use_jev": {
                    "type": "boolean",
                    "description": "Use Jev AI for severity synthesis",
                    "default": False,
                },
            },
        },
    },
}


def handle_scan_tool(name: str, arguments: dict) -> dict:
    """Dispatch scan tool calls to underlying bq functions."""

    if name == "dockerfile_scan":
        from build_q.dockerfile_scanner import run_dockerfile_scan
        return run_captured(
            run_dockerfile_scan,
            arguments.get("path", "."),
            use_jev=arguments.get("use_jev", False),
            auto_fix=False,
            scan_only=True,
        )

    if name == "dockerfile_scan_remote":
        from build_q.dockerfile_scanner import run_dockerfile_scan_remote
        return run_captured(
            run_dockerfile_scan_remote,
            arguments["repo"],
            arguments.get("ref", "main"),
            dockerfile_path=arguments.get("dockerfile_path", "Dockerfile"),
            use_jev=arguments.get("use_jev", False),
        )

    if name == "k8s_anomaly_scan":
        from build_q.anomaly import run_anomaly_scan
        return run_captured(
            run_anomaly_scan,
            arguments["path"],
            use_jev=arguments.get("use_jev", False),
            glob_pattern=arguments.get("glob_pattern", "*_deployment.yaml"),
        )

    if name == "dockerfile_fix":
        from build_q.dockerfile_scanner import run_dockerfile_scan
        return run_captured(
            run_dockerfile_scan,
            arguments.get("path", "."),
            use_jev=arguments.get("use_jev", False),
            auto_fix=True,
            scan_only=False,
        )

    return {"error": f"Unknown scan tool: {name}"}
