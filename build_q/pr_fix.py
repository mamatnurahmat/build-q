"""`bq --pr-fix <repo> <ref>` — one-shot fix for OUTDATED jx-init artifacts.

Orchestrator: preflight → clone → branch → render → commit → push → set repo
secrets → open PR. Semua via jalur native (butuh GH_CLI=false + GITHUB_TOKEN).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import github_api
from ._common import (
    INIT_ARTIFACTS, LEGACY_ARTIFACTS_TO_REMOVE,
    fetch_cicd_data, init_ctx_from_cicd, normalize_text,
)
from .config import cicd_candidates, load_config, save_env_value


def _preflight(config: Dict) -> Optional[str]:
    """Return error string bila prasyarat kurang, None kalau OK.

    Auto-fetch WEBHOOK_TRIGGER_TOKEN dari k8s + save ke .env bila kosong.
    """
    gh = config["github"]
    if gh["use_cli"]:
        return ("GH_CLI=true — --pr-fix butuh jalur native. "
                "Set GH_CLI=false di ~/.build-q/.env.")
    if not gh["token"]:
        return "GITHUB_TOKEN kosong di ~/.build-q/.env."

    try:
        import nacl  # noqa: F401
    except ImportError:
        return "pynacl belum terinstall — jalankan: pip install pynacl"

    webhook = config["webhook"]
    if not webhook["trigger_token"]:
        ctx = webhook["k8s_context"] or "(current)"
        ns = webhook["k8s_namespace"]
        secret = webhook["k8s_secret"]
        print(f"🔐 WEBHOOK_TRIGGER_TOKEN kosong — auto-fetch dari k8s "
              f"(ctx={ctx}, ns={ns}, secret={secret}) ...")
        from .builder import _fetch_jx_token
        token = _fetch_jx_token(
            webhook["k8s_context"], webhook["k8s_namespace"], webhook["k8s_secret"]
        )
        if not token:
            return (
                f"Gagal fetch webhook token dari k8s (ctx={ctx}, ns={ns}, secret={secret}).\n"
                "   Pilihan perbaikan:\n"
                "     a) Set kubectl context yg punya jenkins-x: kubectl config use-context hw-dev\n"
                "     b) Ubah JX_KUBE_CONTEXT di ~/.build-q/.env (default: hw-dev)\n"
                "     c) Set token manual: WEBHOOK_TRIGGER_TOKEN=<value> di ~/.build-q/.env"
            )
        save_env_value("WEBHOOK_TRIGGER_TOKEN", token)
        print(f"   ✅ tersimpan ke ~/.build-q/.env (len={len(token)})")
    return None


def _sh(cmd: List[str], *, cwd: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def _diff_lines(cwd: str, path: str) -> Tuple[int, int]:
    """Return (added, removed) counts for a single path vs index."""
    try:
        out = _sh(["git", "diff", "--numstat", "HEAD", "--", path], cwd=cwd).stdout.strip()
    except subprocess.CalledProcessError:
        return (0, 0)
    if not out:
        return (0, 0)
    parts = out.split()
    if len(parts) >= 2:
        try:
            return (int(parts[0]), int(parts[1]))
        except ValueError:
            return (0, 0)
    return (0, 0)


def run_pr_fix(
    api_repo: str,
    ref: str,
    *,
    cicd_path: str = "cicd/cicd.json",
    dry_run: bool = False,
    keep_workdir: bool = False,
    pr_branch: Optional[str] = None,
) -> int:
    config = load_config()

    print("🔐 Preflight:")
    err = _preflight(config)
    if err:
        print(f"   ❌ {err}", file=sys.stderr)
        return 1
    print(f"   ✅ GITHUB_TOKEN ({len(config['github']['token'])} chars)")
    print("   ✅ WEBHOOK_TRIGGER_TOKEN")
    print("   ✅ pynacl installed")

    # Fetch cicd (native REST)
    print(f"\n📡 Fetching cicd config from {api_repo}@{ref} ...")
    cicd, cicd_found, _ = fetch_cicd_data(api_repo, ref, cicd_path)
    if not cicd:
        print(f"   ❌ Tidak ada cicd config valid (tried: {', '.join(cicd_candidates(cicd_path))})", file=sys.stderr)
        return 1
    print(f"   ✅ {cicd_found} — IMAGE={cicd.get('IMAGE')} PROJECT={cicd.get('PROJECT')} PORT={cicd.get('PORT')}")

    # Prepare workdir + clone
    ts = time.strftime("%Y%m%d-%H%M%S")
    base_workdir = Path(config["pr_fix"]["base_workdir"])
    workdir = base_workdir / f"bq-pr-fix-{api_repo.split('/')[-1]}-{ts}"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    repo_dir = workdir / "repo"

    print(f"\n📥 Clone shallow {api_repo}@{ref} → {repo_dir} ...")
    token = config["github"]["token"]
    clone_url = f"https://x-access-token:{token}@github.com/{api_repo}.git"
    try:
        _sh(["git", "clone", "--depth", "1", "--branch", ref, clone_url, str(repo_dir)])
    except subprocess.CalledProcessError as e:
        print(f"   ❌ Clone gagal: {e.stderr}", file=sys.stderr)
        return 1

    head_sha = _sh(["git", "rev-parse", "HEAD"], cwd=str(repo_dir)).stdout.strip()
    print(f"   ↪ HEAD {head_sha[:12]}")

    # Determine branch name
    branch_name = pr_branch or f"{config['pr_fix']['branch_prefix']}{ts}"
    print(f"\n🌿 Branch: {branch_name} (dari {ref})")
    _sh(["git", "checkout", "-b", branch_name], cwd=str(repo_dir))

    # Render + write artifacts
    from .templates import load_template, render
    ctx = init_ctx_from_cicd(cicd, config)
    print(f"\n📝 Regenerating artifacts (ctx: IMAGE={ctx['IMAGE']} PROJECT={ctx['PROJECT']} PORT={ctx['PORT']}):")
    changes: List[str] = []
    for path_str, tpl_name in INIT_ARTIFACTS:
        target = repo_dir / path_str
        # Preserve Dockerfile bila sudah ada — bisa jadi hasil kustomisasi
        # (multi-stage khusus, base image lain, tambah RUN). Regenerasi bakal
        # menimpa kerja tim.
        if path_str == "Dockerfile" and target.exists():
            print(f"   ⏭  {path_str} (preserved — sudah ada, tidak di-overwrite)")
            continue
        tpl = load_template(tpl_name)
        new_content = render(tpl, ctx)
        current = target.read_text() if target.exists() else ""
        if normalize_text(current) == normalize_text(new_content):
            print(f"   ⏭  {path_str} (already up-to-date)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content)
        added, removed = _diff_lines(str(repo_dir), path_str)
        print(f"   ✏️  {path_str} (+{added}, -{removed})")
        changes.append(path_str)

    # ── Fase 3 cleanup: hapus artifact legacy (mis. trigger-ci.yml action) ──
    # Sekarang standar CI/CD pakai GitHub webhook cicd-hw.qoin.id/hook.
    removals: List[str] = []
    for legacy_path in LEGACY_ARTIFACTS_TO_REMOVE:
        target = repo_dir / legacy_path
        if target.exists():
            try:
                _sh(["git", "rm", "-f", legacy_path], cwd=str(repo_dir))
                print(f"   🗑  {legacy_path} (removed — legacy Fase 3 cleanup)")
                removals.append(legacy_path)
            except subprocess.CalledProcessError as e:
                print(f"   ⚠️  git rm {legacy_path} gagal: {e.stderr}", file=sys.stderr)
        else:
            # Tidak dicetak — mayoritas repo baru tidak punya file ini
            pass

    if not changes and not removals:
        print("\n✅ Semua artifact sudah up-to-date & clean. Tidak perlu PR.")
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        return 0

    # Commit
    print(f"\n📦 Commit …")
    if changes:
        _sh(["git", "add", "--"] + changes, cwd=str(repo_dir))
    # `git rm` sudah stage removals — tidak perlu re-add.
    commit_parts = []
    if changes:
        commit_parts.append(f"Regenerated: {', '.join(changes)}")
    if removals:
        commit_parts.append(f"Removed (legacy Fase 3): {', '.join(removals)}")
    commit_msg = (
        "chore: sync jx-init artifacts to latest template + Fase 3 cleanup\n\n"
        + "\n".join(commit_parts) + "\n\n"
        f"Base: {ref} @ {head_sha[:12]}\n"
        "Standar trigger: GitHub webhook cicd-hw.qoin.id/hook (bukan action).\n"
        "Generated with bq --pr-fix (build-q)"
    )
    _sh(
        ["git", "-c", "user.name=build-q bot", "-c", "user.email=bq@build-q.local",
         "commit", "-m", commit_msg], cwd=str(repo_dir),
    )
    total = len(changes) + len(removals)
    print(f"   ✅ {total} file ({len(changes)} regen + {len(removals)} legacy removed)")

    if dry_run:
        print(f"\n🔍 Dry-run: skip push, set-secret, open PR. Workdir: {repo_dir}")
        return 0

    # Push
    print(f"\n🚀 Push → origin/{branch_name} ...")
    try:
        _sh(["git", "push", "origin", branch_name], cwd=str(repo_dir))
    except subprocess.CalledProcessError as e:
        err = (e.stderr or "").strip()
        print(f"   ❌ Push gagal: {err}", file=sys.stderr)
        if "workflow" in err and "scope" in err:
            # Terjadi kalau removals berisi file .github/workflows/* (Fase 3 cleanup
            # menghapus trigger-ci.yml). GitHub tetap butuh scope `workflow` utk push
            # perubahan di path tsb, bahkan hanya delete.
            print(
                "\n💡 GITHUB_TOKEN kurang scope `workflow` (perlu untuk delete file di\n"
                "   .github/workflows/, termasuk trigger-ci.yml cleanup). Perbaikan:\n"
                "     1) Buka https://github.com/settings/tokens → edit token Anda\n"
                "     2) Centang scope: workflow (juga repo, read:user)\n"
                "     3) Regenerate → copy → update GITHUB_TOKEN di ~/.build-q/.env\n"
                f"     4) Retry: bq --pr-fix {api_repo.split('/')[-1]} {ref}",
                file=sys.stderr,
            )
        return 2

    # Repo secrets — DIHAPUS (Fase 3): WEBHOOK_TRIGGER_URL/TOKEN dipakai oleh
    # trigger-ci.yml action. Setelah action dihapus, secret tsb tidak diperlukan.
    # Untuk backward-compat repo yg masih pakai action (fallback tanpa akses
    # setup webhook), bisa di-set manual via `bq --init-secrets` (deprecated).

    # Deduplicate PR
    print(f"\n🔀 Opening PR → {ref} ...")
    try:
        existing = github_api.list_open_prs(api_repo, branch_name)
        if existing:
            url = existing[0].get("html_url", "(unknown)")
            print(f"   ℹ️  PR sudah ada: {url}")
            return 0
    except github_api.GitHubAPIError:
        pass

    body_lines = [
        "Regenerate jx-init artifacts agar match template terkini + Fase 3 cleanup migrasi CI/CD trigger.",
        "",
        f"**Base:** `{ref}` @ `{head_sha[:12]}`",
    ]
    if changes:
        body_lines += ["", "**Regenerated:**"] + [f"- `{p}`" for p in changes]
    if removals:
        body_lines += ["", "**Removed (legacy, Fase 3):**"] + [f"- `{p}` — action digantikan webhook `cicd-hw.qoin.id/hook`" for p in removals]
    body_lines += [
        "",
        "**Standar trigger CI/CD sekarang:** GitHub webhook `https://cicd-hw.qoin.id/hook` (di-relay ke webhook-trigger service, dedup middleware Fase 1).",
        "",
        "Kalau webhook belum terpasang di repo ini, jalankan:",
        f"```",
        f"bq --cicd-webhook {api_repo.split('/')[-1]}",
        f"```",
        "",
        "Generated with `bq --pr-fix`.",
    ]
    body = "\n".join(body_lines)
    total = len(changes) + len(removals)
    try:
        pr = github_api.create_pull_request(
            api_repo, base=ref, head=branch_name,
            title=f"chore: sync jx-init + Fase 3 cleanup ({total} file)",
            body=body,
        )
        print(f"   ✅ {pr.get('html_url')}")
    except github_api.GitHubAPIError as e:
        print(f"   ❌ Gagal buat PR: {e}", file=sys.stderr)
        return 2

    # Verifikasi webhook standar (Fase 3) — informatif, tidak affect exit code
    try:
        from .cicd_webhook import run_cicd_webhook_check
        print()
        print("🔎 Verifikasi webhook standar Fase 3:")
        run_cicd_webhook_check(api_repo)
    except Exception as e:
        print(f"   ⚠️  Cek webhook gagal: {e}", file=sys.stderr)

    if not keep_workdir:
        shutil.rmtree(workdir, ignore_errors=True)
        print(f"\n🧹 Cleanup {workdir}")
    else:
        print(f"\n📂 Workdir dipertahankan: {workdir}")

    return 0
