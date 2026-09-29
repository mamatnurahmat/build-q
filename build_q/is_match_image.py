"""`bq --is-match-image <ns> <deployment> <gitops-repo> <branch> <path>` —
bandingkan image container yang sedang berjalan di K8s dengan image tag di
file deployment YAML GitOps.

Exit code:
  0 = MATCH
  1 = MISMATCH atau validasi gagal
  2 = error kubectl / GitHub fetch
"""
from __future__ import annotations

import shutil
import subprocess
import sys

from . import github_api
from .gitops_set_image import _extract_first_image


def _kubectl_image(ns: str, deploy: str) -> str:
    """Ambil image container[0] deployment via kubectl. Return '' bila gagal."""
    if not shutil.which("kubectl"):
        return ""
    try:
        out = subprocess.run(
            [
                "kubectl", "get", "deployment", "-n", ns, deploy,
                "-o", "jsonpath={.spec.template.spec.containers[0].image}",
            ],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip().strip('"').strip("'")
    except subprocess.CalledProcessError:
        return ""


def _current_kube_context() -> str:
    try:
        out = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def run_is_match_image(
    ns: str,
    deploy: str,
    gitops_repo: str,
    gitops_branch: str,
    gitops_path: str,
) -> int:
    print("")
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║ bq --is-match-image: cek sinkronisasi K8s vs GitOps         ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f" NS/Deploy: {ns}/{deploy}")
    print(f" Repo:      {gitops_repo}")
    print(f" Branch:    {gitops_branch}")
    print(f" File:      {gitops_path}")
    print("")

    # ── 1. Image live di K8s ──────────────────────────────────────
    print(f"ℹ️  Step 1/2: ambil image live dari K8s ...")
    k8s_image = _kubectl_image(ns, deploy)
    if not k8s_image:
        print(
            f"❌ K8S: Deployment '{deploy}' tidak ditemukan di namespace "
            f"'{ns}' (atau kubectl error).",
            file=sys.stderr,
        )
        print(f"   Context saat ini: {_current_kube_context()}", file=sys.stderr)
        return 2
    print(f"   image K8s: {k8s_image}\n")

    # ── 2. Image di GitOps YAML ───────────────────────────────────
    print(f"ℹ️  Step 2/2: ambil image dari GitOps YAML ...")
    try:
        yaml_text = github_api.get_contents_raw(
            gitops_repo, gitops_path, gitops_branch,
        ).decode("utf-8")
    except github_api.GitHubAPIError as e:
        print(
            f"❌ GITOPS: File '{gitops_path}' tidak ditemukan di "
            f"{gitops_repo}:{gitops_branch}",
            file=sys.stderr,
        )
        print(f"   ({e})", file=sys.stderr)
        return 2

    gitops_image = _extract_first_image(yaml_text)
    if not gitops_image:
        print(
            f"❌ GITOPS: Tidak menemukan key 'image:' di file {gitops_path}",
            file=sys.stderr,
        )
        return 1
    print(f"   image GitOps: {gitops_image}\n")

    # ── 3. Bandingkan ─────────────────────────────────────────────
    print(f"■ K8S (Live) : {k8s_image}")
    print(f"■ GitOps     : {gitops_image}")
    print("")

    if k8s_image == gitops_image:
        print("✅ MATCH — Image K8s dan GitOps sudah sinkron.")
        return 0

    print("⚠️  MISMATCH — Image K8s berbeda dengan target di GitOps.")
    print("")
    print("💡 Sinkronkan GitOps ke image K8s live dengan:")
    print(
        f"   bq --gitops-set-image {gitops_repo} {gitops_branch} "
        f"{gitops_path} {k8s_image}"
    )
    return 1
