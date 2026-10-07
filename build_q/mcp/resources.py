"""MCP resources — read-only context available to AI assistants."""

from __future__ import annotations

from build_q import __version__

RESOURCES: dict[str, dict] = {
    "bq://version": {
        "uri": "bq://version",
        "name": "build-q version",
        "description": "Version string and capabilities of the bq tool",
        "mimeType": "application/json",
    },
    "bq://config": {
        "uri": "bq://config",
        "name": "build-q configuration",
        "description": "Current bq configuration (sensitive values masked)",
        "mimeType": "application/json",
    },
    "bq://doctor": {
        "uri": "bq://doctor",
        "name": "build-q preflight status",
        "description": "Latest preflight check results (tools, creds, builder)",
        "mimeType": "application/json",
    },
    "bq://catalog": {
        "uri": "bq://catalog",
        "name": "Jev tool catalog",
        "description": "Available tools, providers, and patterns from Jev catalog",
        "mimeType": "application/json",
    },
}


def handle_resource(uri: str) -> dict:
    """Fetch resource data by URI."""

    if uri == "bq://version":
        from build_q.mcp.server import ALL_TOOLS
        return {
            "version": __version__,
            "capabilities": list(ALL_TOOLS.keys()),
            "transport": ["stdio"],
        }

    if uri == "bq://config":
        from build_q.mcp.tools.infra import _mask_sensitive
        try:
            from build_q.config import load_config
            return _mask_sensitive(load_config())
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    if uri == "bq://doctor":
        from build_q.mcp._capture import run_captured
        from build_q.doctor import run_doctor
        return run_captured(run_doctor)

    if uri == "bq://catalog":
        try:
            from build_q.tui_catalog import load_catalog
            catalog = load_catalog()
            return {
                "tools_count": len(catalog.get("tools", [])),
                "providers_count": len(catalog.get("providers", [])),
                "patterns_count": len(catalog.get("patterns", [])),
                "tools": [t.get("name", "") for t in catalog.get("tools", [])],
            }
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    return {"error": f"Unknown resource URI: {uri}"}
