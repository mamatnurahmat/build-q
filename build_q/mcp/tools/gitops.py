"""MCP tools for GitOps operations."""

from __future__ import annotations

from build_q.mcp._capture import run_captured

GITOPS_TOOLS: dict[str, dict] = {
    "gitops_set_image": {
        "name": "gitops_set_image",
        "description": (
            "Update image tag in a GitOps deployment YAML file and push the change. "
            "EXTERNAL ACTION: commits and pushes to the GitOps repository."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "GitOps repo (owner/repo)",
                },
                "branch": {
                    "type": "string",
                    "description": "Target branch",
                    "default": "main",
                },
                "path": {
                    "type": "string",
                    "description": "Path to deployment YAML in GitOps repo",
                },
                "image": {
                    "type": "string",
                    "description": "Full image reference (e.g. loyaltolpi/app:v1.2.3)",
                },
            },
            "required": ["repo", "path", "image"],
        },
    },
    "gitops_bootstrap": {
        "name": "gitops_bootstrap",
        "description": (
            "Generate Kubernetes manifests (Secret, Deployment, Service, HPA, PDB) "
            "and create a PR to the GitOps repository. "
            "EXTERNAL ACTION: creates Git branch and Pull Request."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_repo": {
                    "type": "string",
                    "description": "Source app repo (owner/repo)",
                },
                "ref": {
                    "type": "string",
                    "description": "Git ref of source repo",
                },
                "gitops_repo": {
                    "type": "string",
                    "description": "GitOps repo (owner/repo)",
                },
                "gitops_branch": {
                    "type": "string",
                    "description": "Base branch in GitOps repo",
                    "default": "main",
                },
                "path_yaml": {
                    "type": "string",
                    "description": "Destination folder in GitOps repo (e.g. cce/staging-qoin/myapp)",
                },
                "replicas": {
                    "type": "integer",
                    "description": "Deployment replicas (min 2 for production)",
                    "default": 2,
                },
                "stack": {
                    "type": "string",
                    "enum": ["default", "dotnet"],
                    "description": "Override stack detection",
                },
                "env": {
                    "type": "string",
                    "description": "Override environment (develop/staging/production)",
                },
                "apply_secret": {
                    "type": "boolean",
                    "description": "Apply Secret to cluster immediately",
                    "default": False,
                },
                "kube_context": {
                    "type": "string",
                    "description": "kubectl context for --apply-secret",
                },
                "namespace": {
                    "type": "string",
                    "description": "Override K8s namespace",
                },
                "nodepool": {
                    "type": "string",
                    "description": "Override CCE nodepool selector",
                },
            },
            "required": ["source_repo", "ref", "gitops_repo", "path_yaml"],
        },
    },
}


def handle_gitops_tool(name: str, arguments: dict) -> dict:
    """Dispatch GitOps tool calls to underlying bq functions."""

    if name == "gitops_set_image":
        from build_q.gitops_set_image import run_gitops_set_image
        return run_captured(
            run_gitops_set_image,
            arguments["repo"],
            arguments.get("branch", "main"),
            arguments["path"],
            arguments["image"],
        )

    if name == "gitops_bootstrap":
        from build_q.bootstrap import run_bootstrap_k8s
        return run_captured(
            run_bootstrap_k8s,
            arguments["source_repo"],
            arguments["ref"],
            arguments["gitops_repo"],
            arguments.get("gitops_branch", "main"),
            arguments["path_yaml"],
            replicas=arguments.get("replicas", 2),
            stack_override=arguments.get("stack"),
            env_override=arguments.get("env"),
            apply_secret=arguments.get("apply_secret", False),
            kube_context=arguments.get("kube_context"),
            namespace_override=arguments.get("namespace"),
            nodepool_override=arguments.get("nodepool"),
        )

    return {"error": f"Unknown gitops tool: {name}"}
