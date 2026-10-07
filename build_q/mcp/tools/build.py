"""MCP tools for Docker build operations."""

from __future__ import annotations

from build_q.mcp._capture import run_captured

BUILD_TOOLS: dict[str, dict] = {
    "docker_build": {
        "name": "docker_build",
        "description": (
            "Build a Docker image using buildx. Reads cicd.json for image name, "
            "port, and project. Optionally pushes to registry. "
            "DESTRUCTIVE: pushes to Docker Hub when push=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repository name or path",
                },
                "ref": {
                    "type": "string",
                    "description": "Git ref (branch or tag)",
                },
                "push": {
                    "type": "boolean",
                    "description": "Push image to registry after build",
                    "default": False,
                },
                "tag": {
                    "type": "string",
                    "description": "Override image tag",
                },
                "platform": {
                    "type": "string",
                    "description": "Target platform",
                    "default": "linux/amd64",
                },
                "dockerfile": {
                    "type": "string",
                    "description": "Dockerfile path",
                    "default": "Dockerfile",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview command without executing",
                    "default": False,
                },
                "build_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra build arguments (KEY=VALUE format)",
                },
            },
            "required": ["repo", "ref"],
        },
    },
    "docker_build_preview": {
        "name": "docker_build_preview",
        "description": (
            "Preview the Docker buildx command that would be executed, "
            "without actually building. Safe read-only operation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repository name or path",
                },
                "ref": {
                    "type": "string",
                    "description": "Git ref (branch or tag)",
                },
                "tag": {
                    "type": "string",
                    "description": "Override image tag",
                },
                "platform": {
                    "type": "string",
                    "description": "Target platform",
                    "default": "linux/amd64",
                },
            },
            "required": ["repo", "ref"],
        },
    },
}


def handle_build_tool(name: str, arguments: dict) -> dict:
    """Dispatch build tool calls to underlying bq functions."""

    if name == "docker_build":
        from build_q.builder import run_build
        return run_captured(
            run_build,
            arguments["repo"],
            arguments["ref"],
            platform=arguments.get("platform", "linux/amd64"),
            push=arguments.get("push", False),
            tag=arguments.get("tag"),
            dockerfile=arguments.get("dockerfile", "Dockerfile"),
            extra_build_args=arguments.get("build_args"),
            dry_run=arguments.get("dry_run", False),
        )

    if name == "docker_build_preview":
        from build_q.builder import run_build
        return run_captured(
            run_build,
            arguments["repo"],
            arguments["ref"],
            platform=arguments.get("platform", "linux/amd64"),
            tag=arguments.get("tag"),
            dry_run=True,
        )

    return {"error": f"Unknown build tool: {name}"}
