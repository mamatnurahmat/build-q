"""Kebijakan risiko tool build-q — satu sumber untuk MCP, TUI, dan web serve.

Tier mengikuti katalog MCP (lihat MCP-SETUP.md bagian 10.1):

- ``read``     — read-only, aman dijalankan tanpa konfirmasi.
- ``local``    — mengubah file lokal / membuka rahasia (reversible tapi sensitif).
- ``external`` — aksi ke sistem luar: registry, repo GitOps, cluster, Jenkins X.

MCP memakai tabel ini untuk ``ToolAnnotations``; TUI/serve memetakan perintah
CLI ``bq --flag ...`` ke tool MCP padanannya agar penilaian RISKY konsisten.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolPolicy:
    tier: str                 # read | local | external
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False  # berinteraksi dengan sistem di luar mesin lokal

    @property
    def risky(self) -> bool:
        return self.tier != "read"


MCP_TOOL_POLICY: dict[str, ToolPolicy] = {
    # Tier 1 — read-only
    "build_doctor":           ToolPolicy("read", idempotent=True),
    "config_show":            ToolPolicy("read", idempotent=True),
    "dockerfile_scan":        ToolPolicy("read", idempotent=True),
    "dockerfile_scan_remote": ToolPolicy("read", idempotent=True, open_world=True),
    "k8s_anomaly_scan":       ToolPolicy("read", idempotent=True),
    "pipeline_check":         ToolPolicy("read", idempotent=True, open_world=True),
    "gitops_match_check":     ToolPolicy("read", idempotent=True, open_world=True),
    "image_check":            ToolPolicy("read", idempotent=True, open_world=True),
    "webhook_status":         ToolPolicy("read", idempotent=True, open_world=True),
    "docker_build_preview":   ToolPolicy("read", idempotent=True, open_world=True),
    # Tier 2 — ubah file lokal / rahasia
    "dockerfile_fix":         ToolPolicy("local", idempotent=True),
    "sops_encrypt":           ToolPolicy("local", destructive=True),
    "sops_decrypt":           ToolPolicy("local", destructive=True),
    # Tier 3 — aksi eksternal
    "docker_build":           ToolPolicy("external", destructive=True, open_world=True),
    "pipeline_trigger":       ToolPolicy("external", destructive=True, open_world=True),
    "gitops_set_image":       ToolPolicy("external", destructive=True, idempotent=True, open_world=True),
    "gitops_bootstrap":       ToolPolicy("external", destructive=True, open_world=True),
    "k8s_set_image":          ToolPolicy("external", destructive=True, idempotent=True, open_world=True),
}

# Flag CLI `bq` → tool MCP padanannya.
CLI_FLAG_TO_MCP: dict[str, str] = {
    "--doctor": "build_doctor",
    "--config": "config_show",
    "--fix-dockerfile": "dockerfile_fix",
    "--anomaly-scan": "k8s_anomaly_scan",
    "--check": "pipeline_check",
    "--is-match-image": "gitops_match_check",
    "--cicd-webhook": "webhook_status",
    "--cicd-trigger": "pipeline_trigger",
    "--gitops-set-image": "gitops_set_image",
    "--bootstrap-k8s": "gitops_bootstrap",
    "--set-image": "k8s_set_image",
    "--sops-encrypt": "sops_encrypt",
    "--sops-decrypt": "sops_decrypt",
}

# Flag yang menandakan perintah build image (`bq <repo> <ref> --local` dst.).
_BUILD_FLAGS = {"--local", "--remote", "--compose", "--clone"}


def annotations_for(tool: str) -> dict:
    """Kwargs untuk ``mcp.types.ToolAnnotations`` dari kebijakan tool MCP."""
    p = MCP_TOOL_POLICY[tool]
    return {
        "read_only_hint": p.tier == "read",
        "destructive_hint": p.destructive,
        "idempotent_hint": p.idempotent,
        "open_world_hint": p.open_world,
    }


def classify_command(cmd: str) -> str | None:
    """Petakan perintah `bq ...` ke nama tool MCP; None bila tidak dikenal."""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None
    if not argv or argv[0] != "bq":
        return None

    flags = set(argv[1:])
    for flag, tool in CLI_FLAG_TO_MCP.items():
        if flag in flags:
            return tool
    if flags & _BUILD_FLAGS:
        return "docker_build_preview" if "--dry-run" in flags else "docker_build"
    return None


def command_policy(cmd: str) -> tuple[str | None, ToolPolicy | None]:
    """Return (tool MCP, kebijakannya) untuk perintah CLI, atau (None, None)."""
    tool = classify_command(cmd)
    return tool, MCP_TOOL_POLICY.get(tool) if tool else None
