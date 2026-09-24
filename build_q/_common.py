"""Shared helpers — collapse duplikasi antara check.py, pr_fix.py, cli.py, builder.py.

**Aturan modul:** hanya boleh import `config`, `templates`, `github_api`.
Tidak boleh import `builder/rollout/check/pr_fix/cli` (hindari circular).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .config import cicd_candidates
from .github_api import GitHubAPIError, get_contents_raw


# Pair (path, template_name) — 4 artifact yang di-scaffold `bq --init-jx`
# dan diperiksa `bq --check` + di-regenerate `bq --pr-fix`.
# Sumber template: build_q/templates.py (dari gist mamatnurahmat).
INIT_ARTIFACTS: List[Tuple[str, str]] = [
    ("Makefile", "makefile"),
    ("compose.yaml", "compose"),
    ("Dockerfile", "dockerfile"),
    (".github/workflows/trigger-ci.yml", "trigger_ci"),
]


def normalize_text(text: str) -> str:
    """Normalize whitespace agar trivial format drift tidak dianggap mismatch.

    Rules:
      - CRLF → LF
      - trailing whitespace per baris → strip
      - trailing blank lines → drop
      - selalu diakhiri single newline
    """
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def init_ctx_from_cicd(cicd: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, str]:
    """Context untuk render template init — INLINE dengan builder.init_jx.

    Ctx keys: IMAGE, PROJECT, PORT, CLUSTER, DEPLOYMENT, NODETYPE, ORG_REGISTRY.
    Default value konsisten dgn perilaku init_jx historis.
    """
    registry = config.get("registry", {}).get("url", "") or "loyaltolpi"
    image = cicd.get("IMAGE") or ""
    return {
        "IMAGE": image,
        "PROJECT": cicd.get("PROJECT", "qoin"),
        "PORT": cicd.get("PORT", "8080"),
        "CLUSTER": cicd.get("CLUSTER", "qoin"),
        "DEPLOYMENT": cicd.get("DEPLOYMENT", image),
        "NODETYPE": cicd.get("NODETYPE", "back"),
        "ORG_REGISTRY": registry,
    }


def fetch_cicd_data(
    api_repo: str, ref: str, cicd_path: str,
) -> Tuple[Dict[str, Any], Optional[str], Optional[bytes]]:
    """Fetch + parse cicd config dari remote GitHub (native REST).

    Coba semua kandidat dari `cicd_candidates(cicd_path)`. Return
    (cicd_data, path_yg_ditemukan, raw_bytes). Bila tidak ada yg valid,
    return ({}, None, None).

    Tidak raise — swallow GitHubAPIError/JSONDecodeError agar caller dapat
    memutuskan (mis. lanjut dgn defaults kosong, atau abort).
    """
    for cand in cicd_candidates(cicd_path):
        try:
            raw = get_contents_raw(api_repo, cand, ref)
        except GitHubAPIError:
            continue
        if not raw:
            continue
        try:
            return json.loads(raw), cand, raw
        except json.JSONDecodeError:
            continue
    return {}, None, None
