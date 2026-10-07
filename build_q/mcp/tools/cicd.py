"""MCP tools for CI/CD pipeline checks and GitOps verification."""

from __future__ import annotations

from build_q.mcp._capture import run_captured

CICD_TOOLS: dict[str, dict] = {
    "pipeline_check": {
        "name": "pipeline_check",
        "description": (
            "Verify repo readiness for CI/CD: checks ref exists, "
            "cicd.json valid, build artifacts present, image status "
            "in registry, GitOps alignment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "GitHub repo (owner/repo format)",
                },
                "ref": {
                    "type": "string",
                    "description": "Git ref (branch or tag)",
                    "default": "main",
                },
                "namespace": {
                    "type": "string",
                    "description": "K8s namespace for rollout suggestion",
                },
                "infra": {
                    "type": "string",
                    "enum": ["cce", "k8s"],
                    "description": "Infrastructure type",
                },
            },
            "required": ["repo"],
        },
    },
    "gitops_match_check": {
        "name": "gitops_match_check",
        "description": (
            "Compare the image running in a Kubernetes deployment "
            "against the image declared in the GitOps repository YAML. "
            "Detects drift between live cluster and declared state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "K8s namespace",
                },
                "deployment": {
                    "type": "string",
                    "description": "Deployment name",
                },
                "gitops_repo": {
                    "type": "string",
                    "description": "GitOps repo (owner/repo)",
                },
                "gitops_branch": {
                    "type": "string",
                    "description": "GitOps branch",
                    "default": "main",
                },
                "gitops_path": {
                    "type": "string",
                    "description": "Path to deployment YAML in GitOps repo",
                },
            },
            "required": ["namespace", "deployment", "gitops_repo", "gitops_path"],
        },
    },
    "image_check": {
        "name": "image_check",
        "description": (
            "Check if a Docker image:tag already exists in Docker Hub registry. "
            "Returns exists/not-exists status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "image": {
                    "type": "string",
                    "description": "Full image reference (e.g. loyaltolpi/pay-be-topup-manager:v1.2.3)",
                },
            },
            "required": ["image"],
        },
    },
    "pipeline_trigger": {
        "name": "pipeline_trigger",
        "description": (
            "Trigger a Jenkins X pipeline build by sending a synthetic GitHub "
            "push event to the webhook relay. "
            "EXTERNAL ACTION: starts a CI/CD build in the cluster."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "GitHub repo (owner/repo format)",
                },
                "ref": {
                    "type": "string",
                    "description": "Git ref to build",
                    "default": "main",
                },
                "sha": {
                    "type": "string",
                    "description": "Override commit SHA (default: resolve from ref)",
                },
                "force": {
                    "type": "boolean",
                    "description": "Bypass webhook deduplication",
                    "default": False,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview payload without sending",
                    "default": False,
                },
            },
            "required": ["repo"],
        },
    },
    "webhook_status": {
        "name": "webhook_status",
        "description": (
            "Check if the CI/CD webhook (cicd-hw.qoin.id/hook) is installed "
            "on a GitHub repository. Reports webhook URL, events, and active status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "GitHub repo (owner/repo format)",
                },
            },
            "required": ["repo"],
        },
    },
}


def handle_cicd_tool(name: str, arguments: dict) -> dict:
    """Dispatch CICD tool calls to underlying bq functions."""

    if name == "pipeline_check":
        from build_q.check import run_check
        return run_captured(
            run_check,
            arguments["repo"],
            arguments.get("ref", "main"),
            ns=arguments.get("namespace"),
            infra=arguments.get("infra"),
        )

    if name == "gitops_match_check":
        from build_q.is_match_image import run_is_match_image
        return run_captured(
            run_is_match_image,
            arguments["namespace"],
            arguments["deployment"],
            arguments["gitops_repo"],
            arguments.get("gitops_branch", "main"),
            arguments["gitops_path"],
        )

    if name == "image_check":
        from build_q.builder import check_image_exists
        try:
            exists = check_image_exists(arguments["image"])
            return {
                "exit_code": 0,
                "data": {"image": arguments["image"], "exists": exists},
            }
        except Exception as exc:
            return {
                "exit_code": 2,
                "error": f"{type(exc).__name__}: {exc}",
            }

    if name == "pipeline_trigger":
        from build_q.cicd_trigger import run_cicd_trigger
        return run_captured(
            run_cicd_trigger,
            arguments["repo"],
            arguments.get("ref", "main"),
            sha_override=arguments.get("sha"),
            force=arguments.get("force", False),
            dry_run=arguments.get("dry_run", False),
        )

    if name == "webhook_status":
        from build_q.cicd_webhook import run_cicd_webhook_check
        return run_captured(
            run_cicd_webhook_check,
            arguments["repo"],
            auto_setup=False,
        )

    return {"error": f"Unknown cicd tool: {name}"}
