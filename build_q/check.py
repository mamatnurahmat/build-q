"""`bq --check` — verifikasi kesiapan repo untuk build/deploy tanpa clone.

Cek via native REST (butuh GH_CLI=false + GITHUB_TOKEN):
  1. Repo & ref dapat diakses (SHA resolvable)
  2. Config cicd (cicd.json atau cicd/cicd.json)
  3. Artifact jx-init: Makefile, compose.yaml, Dockerfile, .github/workflows/trigger-ci.yml
  4. Image di registry (loyaltolpi/<image>:<tag>)
  5. File deployment GitOps ({infra}/<ns>/<deployment>_deployment.yaml)

Output: baris pass/miss per cek + ringkasan.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .config import cicd_candidates, load_config
from .github_api import GitHubAPIError, get_commit_sha, get_contents_raw

# Artifact yang diharapkan hadir pasca `bq --init-jx` (source of truth: templates.py).
_INIT_ARTIFACTS = [
    "Makefile",
    "compose.yaml",
    "Dockerfile",
    ".github/workflows/trigger-ci.yml",
]


def _exists(api_repo: str, path: str, ref: str) -> bool:
    try:
        data = get_contents_raw(api_repo, path, ref)
        return bool(data)
    except GitHubAPIError:
        return False


def run_check(
    api_repo: str,
    ref: str,
    *,
    cicd_path: str = "cicd/cicd.json",
    ns: Optional[str] = None,
    infra: Optional[str] = None,
) -> int:
    """Return exit code (0 = semua wajib lulus, 1 = ada yang miss)."""
    from .rollout import compute_rollout  # lazy, avoid circular

    config = load_config()
    results: List[Tuple[str, bool, str]] = []  # (label, ok, detail)
    warnings: List[str] = []

    print(f"🔍 Checking {api_repo} @ {ref}\n")

    # 1) Repo + ref
    print("📦 Repository access:")
    try:
        sha = get_commit_sha(api_repo, ref)
        results.append(("ref → SHA", True, sha[:12]))
        print(f"   ✅ ref '{ref}' → {sha[:12]}")
    except GitHubAPIError as e:
        results.append(("ref → SHA", False, str(e)[:80]))
        print(f"   ❌ ref '{ref}' — {str(e)[:120]}")
        print("\n⛔ Tidak bisa lanjut cek: repo/ref tak dapat diakses.")
        return 1

    # 2) cicd config
    print("\n🧩 CICD config:")
    cicd_data: Dict[str, Any] = {}
    cicd_found_path: Optional[str] = None
    for cand in cicd_candidates(cicd_path):
        raw = None
        try:
            raw = get_contents_raw(api_repo, cand, ref)
        except GitHubAPIError:
            pass
        if raw:
            try:
                cicd_data = json.loads(raw)
                cicd_found_path = cand
                print(f"   ✅ {cand} ({len(raw)} bytes)")
                for k in ("IMAGE", "PROJECT", "DEPLOYMENT", "PORT", "CLUSTER", "NODETYPE"):
                    v = cicd_data.get(k)
                    if v:
                        print(f"        • {k:11}= {v}")
                break
            except json.JSONDecodeError as e:
                results.append((f"{cand} parse", False, str(e)[:80]))
                print(f"   ❌ {cand} — JSON invalid: {e}")
    if cicd_found_path is None:
        results.append(("cicd config", False, f"none of {cicd_candidates(cicd_path)}"))
        print(f"   ❌ tidak ditemukan (tried: {', '.join(cicd_candidates(cicd_path))})")
    else:
        results.append(("cicd config", True, cicd_found_path))

    # 3) jx-init artifacts (recommended, bukan blocker)
    print("\n🧱 Init artifacts (jx-init):")
    for path in _INIT_ARTIFACTS:
        ok = _exists(api_repo, path, ref)
        icon = "✅" if ok else "⚠️ "
        print(f"   {icon} {path}")
        if not ok:
            warnings.append(f"missing: {path}")

    # 4) Registry image (best-effort; butuh docker manifest inspect / DockerHub API)
    print("\n📦 Registry:")
    dh_org = config["dockerhub"]["org"] or config["registry"]["url"]
    image_name = cicd_data.get("IMAGE") or api_repo.split("/")[-1]
    if not dh_org:
        print("   ⚠️  DOCKERHUB_ORG / REGISTRY_URL kosong — skip cek image")
    else:
        from .builder import check_image_exists
        # Untuk cek image kita butuh tag. Di --remote flow kita tidak clone,
        # jadi pakai short SHA saja (mirror _predict_image_tag di buildx mode).
        tag_id = sha[:7]
        image_ref = f"{dh_org}/{image_name}:{tag_id}"
        exists = check_image_exists(image_ref)
        icon = "✅" if exists else "⚠️ "
        state = "READY" if exists else "NOT BUILT"
        print(f"   {icon} {image_ref} — {state}")
        if not exists:
            warnings.append(f"image not built: {image_ref}")

    # 5) GitOps deployment file
    print("\n🎯 GitOps:")
    if not cicd_data:
        print("   ⚠️  skip (cicd config tidak dibaca)")
    else:
        rollout = compute_rollout(
            cicd=cicd_data, repo=image_name, ref=ref,
            image_tag=f"{dh_org}/{image_name}:{sha[:7]}",
            config=config, ns=ns, infra=infra,
        )
        gitops_repo = rollout["gitops_repo"]
        gitops_branch = rollout["gitops_branch"]
        gitops_path = rollout["gitops_path"]
        ok = _exists(gitops_repo, gitops_path, gitops_branch)
        icon = "✅" if ok else "⚠️ "
        print(f"   {icon} {gitops_repo}@{gitops_branch}:{gitops_path}")
        if not ok:
            warnings.append(f"gitops manifest missing: {gitops_path}")

    # Ringkasan
    passed = sum(1 for _, ok, _ in results if ok)
    total_required = len(results)
    print(f"\n📊 Summary: {passed}/{total_required} wajib lulus, {len(warnings)} warning")
    if warnings:
        for w in warnings:
            print(f"   ⚠️  {w}")

    return 0 if passed == total_required else 1
