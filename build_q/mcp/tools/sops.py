"""MCP tools for SOPS encryption/decryption."""

from __future__ import annotations

from pathlib import Path

from build_q.mcp._capture import run_captured

SOPS_TOOLS: dict[str, dict] = {
    "sops_encrypt": {
        "name": "sops_encrypt",
        "description": (
            "Encrypt a file using SOPS with age encryption. "
            "Modifies the file in-place. REVERSIBLE: can be decrypted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to file to encrypt",
                },
                "recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Age public keys (default: from config or .sops.yaml)",
                },
            },
            "required": ["path"],
        },
    },
    "sops_decrypt": {
        "name": "sops_decrypt",
        "description": (
            "Decrypt a SOPS-encrypted file. "
            "Returns plaintext content or decrypts in-place."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to SOPS-encrypted file",
                },
                "in_place": {
                    "type": "boolean",
                    "description": "Decrypt in-place (modify file) vs return content only",
                    "default": False,
                },
            },
            "required": ["path"],
        },
    },
}


def handle_sops_tool(name: str, arguments: dict) -> dict:
    """Dispatch SOPS tool calls to underlying bq functions."""

    if name == "sops_encrypt":
        from build_q.sops import encrypt_file
        return run_captured(
            encrypt_file,
            Path(arguments["path"]),
            recipients=arguments.get("recipients"),
        )

    if name == "sops_decrypt":
        from build_q.sops import decrypt_file
        return run_captured(
            decrypt_file,
            Path(arguments["path"]),
            in_place=arguments.get("in_place", False),
        )

    return {"error": f"Unknown sops tool: {name}"}
