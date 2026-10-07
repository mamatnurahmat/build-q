"""MCP tools for direct Kubernetes operations."""

from __future__ import annotations

from build_q.mcp._capture import run_captured

K8S_TOOLS: dict[str, dict] = {
    "k8s_set_image": {
        "name": "k8s_set_image",
        "description": (
            "Hot-patch a Kubernetes deployment with a new image tag using "
            "kubectl set image. DESTRUCTIVE: modifies live cluster state."
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
                "image": {
                    "type": "string",
                    "description": "New image reference (can be full image:tag or just short SHA)",
                },
                "container": {
                    "type": "string",
                    "description": "Container name (default: auto-detect)",
                },
            },
            "required": ["namespace", "deployment", "image"],
        },
    },
}


def handle_k8s_tool(name: str, arguments: dict) -> dict:
    """Dispatch K8s tool calls to underlying bq functions."""

    if name == "k8s_set_image":
        from build_q.set_image import run_set_image
        return run_captured(
            run_set_image,
            arguments["namespace"],
            arguments["deployment"],
            arguments["image"],
            container=arguments.get("container"),
        )

    return {"error": f"Unknown k8s tool: {name}"}
