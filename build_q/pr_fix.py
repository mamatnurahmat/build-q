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
from .config import cicd_candidates, load_config, save_env_value


_ARTIFACTS: List[Tuple[str, str]] = [
    ("Makefile", "makefile"),
    ("compose.yaml", "compose"),
    ("Dockerfile", "dockerfile"),
    (".github/workflows/trigger-ci.yml", "trigger_ci"),
]


def _normalize(text: str) -> str:
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def _init_ctx(cicd: Dict, config: Dict) -> Dict[str, str]:
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
    cicd: Dict = {}
    cicd_found: Optional[str] = None
    for cand in cicd_candidates(cicd_path):
        try:
            raw = github_api.get_contents_raw(api_repo, cand, ref)
            cicd = json.loads(raw)
            cicd_found = cand
            print(f"   ✅ {cand} — IMAGE={cicd.get('IMAGE')} PROJECT={cicd.get('PROJECT')} PORT={cicd.get('PORT')}")
            break
        except github_api.GitHubAPIError:
            continue
    if not cicd:
        print(f"   ❌ Tidak ada cicd config valid (tried: {', '.join(cicd_candidates(cicd_path))})", file=sys.stderr)
        return 1

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
    ctx = _init_ctx(cicd, config)
    print(f"\n📝 Regenerating artifacts (ctx: IMAGE={ctx['IMAGE']} PROJECT={ctx['PROJECT']} PORT={ctx['PORT']}):")
    changes: List[str] = []
    for path_str, tpl_name in _ARTIFACTS:
        tpl = load_template(tpl_name)
        new_content = render(tpl, ctx) if tpl_name != "trigger_ci" else tpl
        target = repo_dir / path_str
        current = target.read_text() if target.exists() else ""
        if _normalize(current) == _normalize(new_content):
            print(f"   ⏭  {path_str} (already up-to-date)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content)
        added, removed = _diff_lines(str(repo_dir), path_str)
        print(f"   ✏️  {path_str} (+{added}, -{removed})")
        changes.append(path_str)

    if not changes:
        print("\n✅ Semua artifact sudah up-to-date. Tidak perlu PR.")
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        return 0

    # Commit
    print(f"\n📦 Commit …")
    _sh(["git", "add", "--"] + changes, cwd=str(repo_dir))
    commit_msg = (
        "chore: regenerate jx-init artifacts to match latest template\n\n"
        f"Regenerated: {', '.join(changes)}\n"
        f"Base: {ref} @ {head_sha[:12]}\n"
        "Generated with bq --pr-fix (build-q)"
    )
    _sh(
        ["git", "-c", "user.name=build-q bot", "-c", "user.email=bq@build-q.local",
         "commit", "-m", commit_msg], cwd=str(repo_dir),
    )
    print(f"   ✅ {len(changes)} file")

    if dry_run:
        print(f"\n🔍 Dry-run: skip push, set-secret, open PR. Workdir: {repo_dir}")
        return 0

    # Push
    print(f"\n🚀 Push → origin/{branch_name} ...")
    try:
        _sh(["git", "push", "origin", branch_name], cwd=str(repo_dir))
    except subprocess.CalledProcessError as e:
        print(f"   ❌ Push gagal: {e.stderr}", file=sys.stderr)
        return 2

    # Secrets
    print(f"\n🔐 Setting repo secrets di {api_repo}:")
    webhook = config["webhook"]
    entries = [
        ("WEBHOOK_TRIGGER_URL", webhook["trigger_url"], webhook["trigger_url"]),
        ("WEBHOOK_TRIGGER_TOKEN", webhook["trigger_token"], "***"),
    ]
    for name, value, display in entries:
        try:
            github_api.set_secret(api_repo, name, value)
            print(f"   ✅ {name} = {display}")
        except github_api.GitHubAPIError as e:
            print(f"   ⚠️  {name}: {e}", file=sys.stderr)

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

    body = (
        f"Regenerate jx-init artifacts agar match template terkini (bq v0.1.24+).\n\n"
        f"**Base:** `{ref}` @ `{head_sha[:12]}`\n"
        f"**Files changed:**\n"
        + "\n".join(f"- `{p}`" for p in changes)
        + "\n\nGenerated with `bq --pr-fix`."
    )
    try:
        pr = github_api.create_pull_request(
            api_repo, base=ref, head=branch_name,
            title=f"chore: sync jx-init artifacts ({len(changes)} file)",
            body=body,
        )
        print(f"   ✅ {pr.get('html_url')}")
    except github_api.GitHubAPIError as e:
        print(f"   ❌ Gagal buat PR: {e}", file=sys.stderr)
        return 2

    if not keep_workdir:
        shutil.rmtree(workdir, ignore_errors=True)
        print(f"\n🧹 Cleanup {workdir}")
    else:
        print(f"\n📂 Workdir dipertahankan: {workdir}")

    return 0
