"""Post-build rollout suggestions — bungkus tools eksternal
`set-image` (imperative) dan `gitops-set-image` (declarative GitOps).

Fase 1: hanya print suggestion siap copy-paste. Tidak mengeksekusi.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def compute_rollout(
    *,
    cicd: Dict[str, Any],
    repo: str,
    ref: str,
    image_tag: str,
    config: Dict[str, Any],
    ns: Optional[str] = None,
    infra: Optional[str] = None,
    gitops_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute rollout parameters (pure function).

    Priority for each field:
      - ns          : `ns` param > `<env>-<NS_SUFFIX>` fallback
      - deployment  : cicd.DEPLOYMENT > cicd.IMAGE > repo
      - image_full  : `DOCKERHUB_ORG/<image_name>:<tag>` bila DOCKERHUB_ORG set,
                      else fallback ke `image_tag` apa adanya
      - infra       : `infra` param > config.gitops.infra
      - gitops_path : `gitops_path` param > f"{infra}/{ns}/{deployment}_deployment.yaml"
    """
    from .builder import _env_from_ref  # lazy: avoid circular import

    env = _env_from_ref(ref)
    ns_suffix = config["gitops"]["ns_suffix"]
    dh_org = config["dockerhub"]["org"] or config["registry"]["url"]

    ns_from_fallback = ns is None
    ns_final = ns or f"{env}-{ns_suffix}"

    deployment = cicd.get("DEPLOYMENT") or cicd.get("IMAGE") or repo
    image_name = cicd.get("IMAGE") or repo

    tag_part = image_tag.rsplit(":", 1)[-1] if ":" in image_tag else ""

    if dh_org and tag_part:
        image_full = f"{dh_org}/{image_name}:{tag_part}"
    else:
        image_full = image_tag

    infra_final = infra or config["gitops"]["infra"]
    gitops_path_final = (
        gitops_path
        or f"{infra_final}/{ns_final}/{deployment}_deployment.yaml"
    )

    return {
        "ns": ns_final,
        "ns_from_fallback": ns_from_fallback,
        "deployment": deployment,
        "image_full": image_full,
        "image_tag": tag_part,
        "infra": infra_final,
        "gitops_repo": config["gitops"]["repo"],
        "gitops_branch": config["gitops"]["branch"],
        "gitops_path": gitops_path_final,
    }


def render_suggestion(r: Dict[str, Any]) -> str:
    """Render 2 copy-paste-able commands + hints. Returns a multi-line string."""
    lines = [
        "",
        "📤 Rollout suggestions (copy-paste ke terminal):",
        "",
        "   # 1) Hot-patch cluster (imperative, cepat)",
        f"   set-image {r['ns']} {r['deployment']} {r['image_tag']}",
        "",
        "   # 2) Via GitOps (declarative, ArgoCD sync)",
        f"   gitops-set-image {r['gitops_repo']} {r['gitops_branch']} \\",
        f"     {r['gitops_path']} \\",
        f"     {r['image_full']}",
        "",
    ]
    if r["ns_from_fallback"]:
        lines.append(f"   ℹ️  ns fallback: {r['ns']} — override dengan --ns <name>")
    lines.append("   Tip: --ns <name>  |  --infra {cce|k8s}  |  --gitops-path <path>")
    return "\n".join(lines)
