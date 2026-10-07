"""MCP tools for infrastructure checks and configuration."""

from __future__ import annotations

from build_q.mcp._capture import run_captured

INFRA_TOOLS: dict[str, dict] = {
    "build_doctor": {
        "name": "build_doctor",
        "description": (
            "Run preflight checks: Docker daemon, buildx plugin, "
            "builder instance, registry credentials, GitHub token, Git config. "
            "Returns pass/fail for each check."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "config_show": {
        "name": "config_show",
        "description": (
            "Show current build-q configuration: builder name, registry, "
            "GitHub org, webhook URLs, GitOps settings. "
            "Sensitive values are masked."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
}


def _mask_sensitive(config: dict) -> dict:
    """Mask sensitive values in config dict before returning."""
    import copy
    masked = copy.deepcopy(config)

    sensitive_keys = {"token", "password", "secret", "pass", "hmac"}

    def _walk(d: dict) -> None:
        for key, val in d.items():
            if isinstance(val, dict):
                _walk(val)
            elif isinstance(val, str) and any(s in key.lower() for s in sensitive_keys):
                if len(val) > 8:
                    d[key] = val[:4] + "****" + val[-4:]
                elif val:
                    d[key] = "****"

    _walk(masked)
    return masked


def handle_infra_tool(name: str, arguments: dict) -> dict:
    """Dispatch infra tool calls to underlying bq functions."""

    if name == "build_doctor":
        from build_q.doctor import run_doctor
        return run_captured(run_doctor)

    if name == "config_show":
        from build_q.config import load_config
        try:
            config = load_config()
            return {
                "exit_code": 0,
                "data": _mask_sensitive(config),
            }
        except Exception as exc:
            return {
                "exit_code": 2,
                "error": f"{type(exc).__name__}: {exc}",
            }

    return {"error": f"Unknown infra tool: {name}"}
