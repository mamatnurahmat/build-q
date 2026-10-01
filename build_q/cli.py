#!/usr/bin/env python3
"""build-q (bq) — Simple Docker Buildx local build CLI.

Usage:
    build-q <repo> <ref> [OPTIONS]
    bq <repo> <ref> [OPTIONS]

Examples:
    bq plus-be-service staging \\
        --secret id=netrc,src=$HOME/.netrc \\
        --platform linux/amd64 \\
        --local --no-push \\
        --build-arg BRANCH=staging

    bq my-service develop --local --dry-run
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

from . import __version__
from .builder import (
    BuildError, ensure_builder, fix_dockerfile, get_git_info,
    init_gh_action, init_jx, init_legacy, init_secrets, run_build, run_compose,
)
from .config import ENV_FILE, cicd_candidates, init_config, load_config, use_gh_cli
from .github_api import GitHubAPIError, normalize_repo


def _expand_repo(name: str, default_org: str) -> str:
    """Prepend default org if `name` is a bare repo shorthand (no `/`, no scheme)."""
    if not name or not default_org:
        return name
    if "/" in name or "://" in name or name.startswith("git@"):
        return name
    return f"{default_org}/{name}"


_TAG_LIKE = re.compile(r"^v\d")


def _ref_id_for_image_tag(ref: str, sha: str) -> str:
    """Identifier untuk image tag saat kita belum clone repo.

    Mirror-kan aturan Makefile `IMAGE_TAG` yang jalan pasca-clone:
        git describe --tags --exact-match || git rev-parse --short HEAD
    Bila `ref` cocok pola tag versi (v1.0.1, v2.3.0-rc1, dsb), post-clone
    `git describe` akan mengembalikan `ref` — jadi kita pakai `ref` sebagai
    identifier. Selain itu (branch), fallback ke short SHA.
    """
    if ref and _TAG_LIKE.match(ref):
        return ref
    return sha[:7]


# ── GitHub dispatchers ─────────────────────────────────────────────────────────
# When GH_CLI=true (default) the existing `gh` subprocess path runs untouched.
# When GH_CLI=false these helpers route to build_q.github_api (native REST/git).

def _github_contents(api_repo: str, path: str, ref: str) -> str | None:
    """Return raw file contents at `ref`, or None on any failure."""
    if use_gh_cli():
        res = subprocess.run(
            ["gh", "api", f"repos/{api_repo}/contents/{path}?ref={ref}",
             "-H", "Accept: application/vnd.github.v3.raw"],
            capture_output=True, text=True,
        )
        return res.stdout if res.returncode == 0 and res.stdout else None
    from . import github_api
    try:
        return github_api.get_contents_raw(api_repo, path, ref).decode("utf-8")
    except GitHubAPIError:
        return None


def _github_commit_sha(api_repo: str, ref: str) -> str:
    """Resolve `ref` to a commit SHA. Raises CalledProcessError or GitHubAPIError."""
    if use_gh_cli():
        res = subprocess.run(
            ["gh", "api", f"repos/{api_repo}/commits/{ref}", "--jq", ".sha"],
            capture_output=True, text=True, check=True,
        )
        return res.stdout.strip()
    from . import github_api
    return github_api.get_commit_sha(api_repo, ref)


def _github_auth_token() -> str:
    """Return a GitHub token. Raises CalledProcessError/FileNotFoundError/GitHubAPIError."""
    if use_gh_cli():
        return subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True,
        ).stdout.strip()
    from . import github_api
    return github_api.get_auth_token()


def _github_user_login() -> str:
    """Return authenticated user login. Raises CalledProcessError/FileNotFoundError/GitHubAPIError."""
    if use_gh_cli():
        return subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    from . import github_api
    return github_api.get_user_login()


def _github_clone(clone_arg: str, ref: str) -> None:
    """Clone a repo into the current directory. Raises CalledProcessError/FileNotFoundError/GitHubAPIError."""
    if use_gh_cli():
        subprocess.run(
            ["gh", "repo", "clone", clone_arg, "--", "--branch", ref, "--single-branch"],
            check=True,
        )
        return
    from . import github_api
    github_api.clone(clone_arg, ref, single_branch=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="build-q (bq)",
        description="Simple Docker Buildx CLI — local builds only",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Auto-detect repo & branch from git
  bq --local

  # Explicit repo and ref
  bq plus-be-service staging --local

  # Full example with secrets, platform, and build-arg
  bq plus-be-service staging \\
      --secret id=netrc,src=$HOME/.netrc \\
      --platform linux/amd64 \\
      --local --no-push \\
      --build-arg BRANCH=staging

  # Dry-run (show command only, don't execute)
  bq plus-be-service staging --local --dry-run

  # Initialize config file (~/.build-q/.env)
  bq --init

  # Show active configuration
  bq --config

  # Preflight — cek tools & credentials
  bq --doctor

Config file: ~/.build-q/.env
""",
    )

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Buka Jev Agent Planner TUI interaktif",
    )
    parser.add_argument(
        "--tui-pull",
        action="store_true",
        help="Refresh TUI catalog cache dari PocketBase (collection "
             "build_q_tools / build_q_providers / build_q_patterns).",
    )
    parser.add_argument(
        "--tui-sync",
        metavar="SEED_FILE",
        help="Upload katalog dari file seed JSON ke PocketBase (upsert "
             "by unique field). Format: {\"build_q_tools\": [...], ...}.",
    )
    parser.add_argument(
        "--tui-ls",
        action="store_true",
        help="Tampilkan ringkasan katalog TUI aktif (tools/providers/patterns).",
    )

    # Subcommand flags
    parser.add_argument("--init", action="store_true", help="Initialize ~/.build-q/.env config file")
    parser.add_argument("--force", action="store_true", help="Force recreate config (use with --init) / bypass dedup (use with --cicd-trigger)")
    parser.add_argument("--config", action="store_true", help="Show current configuration")
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Cek kesiapan tools (git/docker/buildx/kubectl) & credentials "
             "(GITHUB_TOKEN, docker login, buildx builder, kubectl context). "
             "Exit 0 bila blocker-free.",
    )
    parser.add_argument(
        "--pb-login",
        action="store_true",
        help="Verifikasi kredensial PB API (PocketBase IDP) & warm cache "
             "secrets ke ~/.build-q/.pb-cache.json. Butuh PB_API=true + "
             "PB_API_URL/USER/PASS di ~/.build-q/.env.",
    )
    parser.add_argument(
        "--pb-status",
        action="store_true",
        help="Tampilkan status PB API: toggle, URL, user, cache age, jumlah "
             "secrets yang di-cache.",
    )
    parser.add_argument(
        "--pb-pull",
        action="store_true",
        help="Force refresh cache PB API (skip TTL) — auth ulang & fetch secrets.",
    )
    parser.add_argument(
        "--pb-logout",
        action="store_true",
        help="Hapus ~/.build-q/.pb-cache.json (paksa re-auth pada run berikutnya).",
    )
    parser.add_argument(
        "--fix-dockerfile",
        nargs="?",
        const="Dockerfile",
        default=None,
        metavar="PATH",
        help="Migrate ARG-based netrc + FROM casing to buildx-native pattern (default: ./Dockerfile)",
    )
    parser.add_argument(
        "--init-jx",
        action="store_true",
        help="Scaffold Makefile / compose.yaml / Dockerfile dari cicd/cicd.json. "
             "Standar trigger CI/CD: GitHub webhook cicd-hw.qoin.id/hook (bukan action). "
             "Setelah scaffold, otomatis cek status webhook di repo.",
    )
    parser.add_argument(
        "--init-legacy",
        action="store_true",
        help="Scaffold Makefile + compose.yaml pola legacy (GITHUB_USER/TOKEN build-args, auto gh auth token)",
    )
    parser.add_argument(
        "--init-secrets",
        action="store_true",
        help="[DEPRECATED] Set GitHub Actions webhook secrets (WEBHOOK_TRIGGER_URL/TOKEN) — "
             "hanya dibutuhkan kalau repo masih pakai trigger-ci.yml action sebagai fallback. "
             "Standar sekarang: webhook cicd-hw.qoin.id/hook, tanpa secret action.",
    )
    parser.add_argument(
        "--gh-action-init",
        action="store_true",
        help="[DEPRECATED] Sekarang HANYA cleanup: hapus trigger-ci.yml lokal + cek status webhook. "
             "Standar CI/CD Fase 3: GitHub webhook cicd-hw.qoin.id/hook.",
    )
    parser.add_argument(
        "--token",
        metavar="VALUE",
        help="Webhook trigger token (skip kubectl fetch, used with --init-secrets)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verifikasi kesiapan repo di remote (tanpa clone): repo/ref, cicd.json, "
             "artifact jx-init, image registry, deployment GitOps. Butuh --remote.",
    )
    parser.add_argument(
        "--repo-check",
        action="store_true",
        help="Cek CICD repo config dari PocketBase collection. Menampilkan konfigurasi "
             "CICD repo (IMAGE, PROJECT, DEPLOYMENT, PORT, dll) tanpa perlu clone. "
             "Gunakan --cicd=pb untuk tarik dari PocketBase. "
             "Usage: bq --repo-check <repo> [<ref>] [--cicd=pb] [--dry-run]",
    )
    parser.add_argument(
        "--pr-fix",
        action="store_true",
        help="One-shot fix: clone → branch → regenerate jx-init artifacts (Makefile/compose/Dockerfile) "
             "+ Fase 3 cleanup (hapus trigger-ci.yml legacy) → push → open PR → verify webhook. "
             "Tidak set secret action (standar webhook cicd-hw.qoin.id/hook).",
    )
    parser.add_argument(
        "--pr-branch",
        metavar="NAME",
        help="Override nama branch fix (default: fix/jx-init-<timestamp>)",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Jangan hapus /tmp workdir setelah --pr-fix selesai (untuk debugging)",
    )
    parser.add_argument(
        "--cicd-webhook",
        action="store_true",
        help="Cek apakah repo sudah terpasang webhook cicd-hw.qoin.id/hook. "
             "Berjalan sendiri (`bq --cicd-webhook [<repo>]`) atau berbarengan "
             "dengan --pr-fix (dijalankan setelah pr-fix sukses).",
    )
    parser.add_argument(
        "--cicd-trigger",
        action="store_true",
        help="Trigger MANUAL pipeline via webhook cicd-hw.qoin.id/hook — kirim "
             "synthetic push event untuk <repo> <ref>. HMAC diambil dari secret "
             "jenkins-x/incoming-webhook (context hw-dev) atau env INCOMING_WEBHOOK_HMAC. "
             "Usage: bq --cicd-trigger <repo> <ref> [--sha SHA] [--force] [--dry-run]. "
             "--force: bypass middleware dedup di webhook-trigger "
             "(delete dedup record lama + claim baru), berguna untuk re-run commit yg sama.",
    )
    parser.add_argument(
        "--sha",
        metavar="SHA",
        help="Override commit SHA untuk --cicd-trigger (default: resolve dari <ref>)",
    )
    parser.add_argument(
        "--hook-url",
        metavar="URL",
        default=None,
        help="Override webhook URL untuk --cicd-trigger (default: https://cicd-hw.qoin.id/hook)",
    )
    parser.add_argument(
        "--bootstrap-k8s",
        action="store_true",
        help="Bootstrap manifest K8s (Secret + Deployment + Service) ke repo GitOps. "
             "Butuh --gitops-repo, --gitops-branch, --path-yaml. "
             "Usage: bq --bootstrap-k8s <repo> <ref> --gitops-repo <r> "
             "--gitops-branch <b> --path-yaml <p>",
    )
    parser.add_argument(
        "--gitops-repo",
        metavar="OWNER/REPO",
        help="Target GitOps repo untuk --bootstrap-k8s (mis. Qoin-Digital-Indonesia/gitops)",
    )
    parser.add_argument(
        "--gitops-branch",
        metavar="BRANCH",
        help="Base branch di GitOps repo untuk PR --bootstrap-k8s (mis. main)",
    )
    parser.add_argument(
        "--path-yaml",
        metavar="PATH",
        help="Folder tujuan di GitOps repo (mis. cce/production-qoin). "
             "Segmen terakhir jadi namespace.",
    )
    parser.add_argument(
        "--stack",
        choices=["dotnet", "default"],
        help="Override deteksi stack untuk --bootstrap-k8s "
             "(dotnet → appsettings.{Env}.json ; default → .env)",
    )
    parser.add_argument(
        "--env",
        metavar="NAME",
        help="Override env yg diturunkan dari <ref> untuk --bootstrap-k8s "
             "(develop/staging/production)",
    )
    parser.add_argument(
        "--replicas",
        type=int,
        default=2,
        metavar="N",
        help="Jumlah replicas di Deployment untuk --bootstrap-k8s (default: 2)",
    )
    parser.add_argument(
        "--apply-secret",
        action="store_true",
        help="Setelah render, apply Secret ke cluster via kubectl bila belum ada "
             "(hanya Secret; Deployment/Service tetap via PR). Untuk --bootstrap-k8s.",
    )
    parser.add_argument(
        "--kube-context",
        metavar="NAME",
        help="kubectl context untuk --apply-secret (default: current context)",
    )
    parser.add_argument(
        "--image-pull-secret",
        metavar="NAME",
        default="regcred",
        help="Nama imagePullSecret di Deployment (default: regcred — standar Qoin)",
    )
    parser.add_argument(
        "--force-recreate-deploy",
        action="store_true",
        help="Delete Deployment lama bila selector.matchLabels drift dari ctx "
             "(K8s selector IMMUTABLE). Butuh --kube-context. Refuse untuk "
             "env=production. Untuk --bootstrap-k8s.",
    )
    parser.add_argument(
        "--namespace",
        metavar="NAME",
        help="Override target K8s namespace (default: segmen terakhir --path-yaml). "
             "Berguna untuk cross-env: gitops di cce/staging-qoin/ tapi apply "
             "ke ns production-qoin di cluster prod.",
    )
    parser.add_argument(
        "--nodepool",
        metavar="NAME",
        help="Override cce-nodepool selector (default: {namespace}-{manager|service}). "
             "Berguna bila cluster pakai pattern beda mis. production-nodepool-service.",
    )

    # ── SOPS encrypt / decrypt manual (standalone) ────────────────────────────
    parser.add_argument(
        "--sops-encrypt",
        metavar="FILE",
        help="Encrypt FILE in-place pakai sops + age. Recipient dari "
             "BUILD_Q_SOPS_AGE_RECIPIENT, atau .sops.yaml di parent dir, "
             "atau fallback public key di ~/.config/sops/age/keys.txt.",
    )
    parser.add_argument(
        "--sops-decrypt",
        metavar="FILE",
        help="Decrypt FILE SOPS-encrypted. Default: tulis ke stdout. "
             "Pakai --sops-in-place untuk tulis balik ke file.",
    )
    parser.add_argument(
        "--sops-in-place",
        action="store_true",
        help="Dengan --sops-decrypt: tulis plaintext balik ke file "
             "(bukan ke stdout).",
    )
    parser.add_argument(
        "--sops-recipient",
        metavar="AGE_PUBKEY",
        help="Override recipient saat --sops-encrypt (comma-separated untuk "
             "multi-recipient). Sama dengan env BUILD_Q_SOPS_AGE_RECIPIENT.",
    )

    # Positional args (optional — auto-detected from git when --local is used)
    parser.add_argument("repo", nargs="?", help="Repository / service name")
    parser.add_argument("ref", nargs="?", help="Branch or tag (e.g. staging, main, v1.0.0)")

    # Build options
    parser.add_argument(
        "--clone",
        metavar="GITHUB_REPO",
        help="Clone repository using gh CLI before building (e.g., owner/repo)",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Build remotely from git using buildx instead of cloning locally",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete the freshly cloned repository directory after build (requires --clone)",
    )
    parser.add_argument("--local", action="store_true", help="Build from local directory (assumed by default)")
    parser.add_argument(
        "--cicd",
        metavar="PATH",
        default="cicd/cicd.json",
        help="Path to cicd.json (default: cicd/cicd.json)",
    )
    parser.add_argument(
        "--context",
        default=".",
        metavar="DIR",
        help="Build context directory (default: .)",
    )
    parser.add_argument(
        "-f", "--dockerfile",
        default="Dockerfile",
        metavar="PATH",
        help="Path to Dockerfile (default: Dockerfile)",
    )
    parser.add_argument(
        "-t", "--tag",
        metavar="IMAGE:TAG",
        help="Image tag override (default: from cicd.json + git commit)",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        default=True,
        help="Push image to registry after build (default: True)",
    )
    parser.add_argument(
        "--no-push",
        action="store_false",
        dest="push",
        help="Do not push image to registry",
    )
    parser.add_argument(
        "--image-check",
        action="store_true",
        default=True,
        help="Check registry if image exists and skip build if it does (default: True)",
    )
    parser.add_argument(
        "--no-image-check", "--rebuild",
        action="store_false",
        dest="image_check",
        help="Do not check registry for existing image (force rebuild)",
    )
    parser.add_argument(
        "--platform",
        metavar="PLATFORM",
        default="linux/amd64",
        help='Target platform (default: linux/amd64)',
    )
    parser.add_argument(
        "--build-arg",
        action="append",
        metavar="KEY=VALUE",
        dest="build_args",
        help="Pass build argument (can be repeated)",
    )
    parser.add_argument(
        "--secret",
        action="append",
        metavar="id=ID,src=PATH",
        help="Expose secret to build (can be repeated)",
    )
    parser.add_argument(
        "--gh-auth",
        action="store_true",
        help="Auto-inject --build-arg GITHUB_USER and GITHUB_TOKEN from `gh` CLI (for legacy Dockerfiles)",
    )
    parser.add_argument(
        "--compose",
        action="store_true",
        help="Run make build & release instead of buildx",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print command without executing")

    # ── Rollout suggestion overrides (dipakai pasca-build sukses) ────────────
    parser.add_argument(
        "--ns",
        metavar="NAME",
        help="Namespace target untuk rollout suggestion (default: <env>-<NS_SUFFIX>)",
    )
    parser.add_argument(
        "--infra",
        choices=["cce", "k8s"],
        help="Infra GitOps: cce (Huawei) atau k8s (SLS). Default dari GITOPS_INFRA di .env",
    )
    parser.add_argument(
        "--gitops-path",
        metavar="PATH",
        help="Override path YAML lengkap (bypass template {infra}/{ns}/{app}_deployment.yaml)",
    )

    # ── Native set-image / gitops-set-image (ganti script bash eksternal) ────
    parser.add_argument(
        "--set-image",
        action="store_true",
        help="Hot-patch K8s Deployment via `kubectl set image` + wait rollout. "
             "Usage: bq --set-image <ns> <deployment> <image> [container]. "
             "Bila <image> tanpa `/`, jadi <REGISTRY_URL>/<deployment>:<image>.",
    )
    parser.add_argument(
        "--gitops-set-image",
        action="store_true",
        help="Update image di deployment YAML repo GitOps, commit + push langsung "
             "ke branch (tanpa PR). Ada pre-flight: file exist di GitHub, image "
             "ready di Docker Hub, duplikasi. "
             "Usage: bq --gitops-set-image <gitops-repo> <branch> <path.yaml> <image_full>",
    )
    parser.add_argument(
        "--is-match-image",
        action="store_true",
        help="Bandingkan image container[0] deployment K8s (live) dengan image "
             "di file deployment YAML repo GitOps. Kalau MISMATCH, sarankan "
             "perintah `bq --gitops-set-image` untuk sync GitOps ke image K8s. "
             "Usage: bq --is-match-image <ns> <deployment> <gitops-repo> <branch> <path.yaml>",
    )

    # Extra positional args untuk --set-image / --gitops-set-image
    # (repo & ref existing menampung 2 pertama; sisanya ke sini)
    parser.add_argument("extra_args", nargs="*", help=argparse.SUPPRESS)

    args = parser.parse_args()

    try:
        # ── Subcommands ──────────────────────────────────────────────────────────
        if args.tui:
            from .tui import repl
            repl()
            return

        if args.tui_pull:
            from . import tui_catalog
            load_config()  # hydrate PB_API_* ke os.environ
            tui_catalog.clear_cache()
            cat = tui_catalog.load_catalog(force_refresh=True)
            print(
                f"✅ TUI catalog refreshed from PocketBase:\n"
                f"   tools    : {len(cat['tools'])}\n"
                f"   providers: {len(cat['providers'])} "
                f"(default: {cat['default_provider']})\n"
                f"   patterns : {len(cat['patterns'])}\n"
                f"   risky    : {len(cat['risky'])}\n"
                f"   cache    : {tui_catalog.CACHE_FILE}"
            )
            return

        if args.tui_sync:
            from pathlib import Path
            from . import tui_catalog
            load_config()
            seed = Path(args.tui_sync).expanduser()
            if not seed.exists():
                print(f"❌ Seed file tidak ada: {seed}", file=sys.stderr)
                sys.exit(2)
            try:
                summary = tui_catalog.push_from_seed(seed)
            except tui_catalog.CatalogError as e:
                print(f"❌ {e}", file=sys.stderr)
                sys.exit(1)
            print(f"📤 Uploaded dari {seed}:")
            for coll, stats in summary.items():
                print(
                    f"   {coll:22s} created={stats['created']} "
                    f"updated={stats['updated']} failed={stats['failed']}"
                )
            print(f"   Cache cleared — jalankan `bq --tui` akan refetch.")
            return

        if args.tui_ls:
            from . import tui_catalog
            load_config()
            cat = tui_catalog.load_catalog()
            age = tui_catalog.cache_age_seconds()
            print(
                f"📚 TUI catalog (source: {cat['source']}, "
                f"cache_age: {age}s)\n"
                f"   default provider: {cat['default_provider']}\n"
            )
            print(f"── Providers ({len(cat['providers'])}) ──")
            for p in cat["providers"].values():
                flag = "★" if p["is_default"] else " "
                print(f"   {flag} {p['name']:12s} {p['model']:25s} url={p['url']}")
            print(f"\n── Tools ({len(cat['tools'])}) ──")
            cats: dict = {}
            for name, meta in cat["tools"].items():
                cats.setdefault(meta.get("_category") or "-", []).append(
                    (name, "⚠️" if meta.get("_risky") else "✓")
                )
            for cname in sorted(cats):
                print(f"   [{cname}]")
                for n, flag in cats[cname]:
                    print(f"     {flag} {n}")
            print(f"\n── Patterns ({len(cat['patterns'])}) ──")
            for field in sorted(cat["patterns"]):
                print(f"   {field}")
            return

        if args.init:
            init_config(force=args.force)
            config = load_config()
            ensure_builder(config["builder"]["name"], bootstrap=True)
            return

        if args.doctor:
            from .doctor import run_doctor
            sys.exit(run_doctor())

        if args.pb_login or args.pb_status or args.pb_pull or args.pb_logout:
            from . import pb_api
            from .config import _load_dotenv
            # Baca .env tanpa memicu hydrate_env (side-effect: refill cache)
            if ENV_FILE.exists():
                _load_dotenv(ENV_FILE)

            if args.pb_logout:
                removed = pb_api.clear_cache()
                print("🧹 PB cache dihapus." if removed else "ℹ️  Tidak ada cache untuk dihapus.")
                sys.exit(0)

            if not pb_api.is_enabled():
                print("⚠️  PB_API=false — set PB_API=true di ~/.build-q/.env dulu.",
                      file=sys.stderr)
                sys.exit(1)

            if args.pb_status:
                age = pb_api.cache_age_seconds()
                cache = pb_api._read_cache() or {}
                secrets = cache.get("secrets", {})
                print(f"   URL       : {pb_api._base_url()}")
                print(f"   User      : {os.getenv('PB_API_USER', '(unset)')}")
                print(f"   Enabled   : {pb_api.is_enabled()}")
                print(f"   TTL       : {pb_api._ttl_seconds()}s")
                if age is None:
                    print("   Cache     : (empty) — jalankan `bq --pb-login`")
                else:
                    print(f"   Cache age : {age}s ({len(secrets)} secrets)")
                sys.exit(0)

            try:
                secrets = pb_api.pull(force=args.pb_pull)
            except pb_api.PBAPIError as e:
                print(f"❌ PB API error: {e}", file=sys.stderr)
                sys.exit(1)
            action = "refreshed" if args.pb_pull else "warmed"
            print(f"✅ PB cache {action}: {len(secrets)} secrets dari {pb_api._base_url()}")
            sys.exit(0)

        if args.fix_dockerfile is not None:
            ok = fix_dockerfile(args.fix_dockerfile)
            sys.exit(0 if ok else 1)

        if args.init_jx:
            ok = init_jx(cicd_path=args.cicd, force=args.force)
            sys.exit(0 if ok else 1)

        if args.init_legacy:
            ok = init_legacy(cicd_path=args.cicd, force=args.force)
            sys.exit(0 if ok else 1)

        if args.gh_action_init:
            ok = init_gh_action(token=args.token)
            sys.exit(0 if ok else 1)

        if args.init_secrets:
            ok = init_secrets(repo=args.repo, token=args.token)
            sys.exit(0 if ok else 1)

        if args.pr_fix:
            if not args.repo or not args.ref:
                print("❌ Usage: bq --pr-fix <repo> <ref>", file=sys.stderr)
                sys.exit(2)
            from .pr_fix import run_pr_fix
            config = load_config()
            default_org = config.get("git", {}).get("org", "")
            api_repo = normalize_repo(_expand_repo(args.repo, default_org))
            rc = run_pr_fix(
                api_repo, args.ref,
                cicd_path=args.cicd,
                dry_run=args.dry_run,
                keep_workdir=args.keep_workdir,
                pr_branch=args.pr_branch,
            )
            # Note: --pr-fix now runs webhook SETUP internally (skip bila ada,
            # soft-fail bila error). Flag --cicd-webhook di sini menjadi
            # verifikasi tambahan (check) — informatif, tidak affect exit code.
            if rc == 0 and args.cicd_webhook:
                print()  # spacer
                from .cicd_webhook import run_cicd_webhook_check
                run_cicd_webhook_check(api_repo)
            sys.exit(rc)

        if args.cicd_webhook:
            from .cicd_webhook import run_cicd_webhook_check
            config = load_config()
            default_org = config.get("git", {}).get("org", "")
            repo = args.repo
            if not repo:
                print("🔍 Auto-detect repo dari git ...")
                try:
                    info = get_git_info()
                    repo = info["repo"]
                    print(f"   repo: {repo}")
                except BuildError as e:
                    print(f"❌ {e}", file=sys.stderr)
                    print("   Berikan <repo> eksplisit atau jalankan dari direktori git.",
                          file=sys.stderr)
                    sys.exit(1)
            api_repo = normalize_repo(_expand_repo(repo, default_org))
            sys.exit(run_cicd_webhook_check(api_repo))

        if args.cicd_trigger:
            from .cicd_trigger import run_cicd_trigger, HOOK_URL_DEFAULT
            config = load_config()
            default_org = config.get("git", {}).get("org", "")
            repo = args.repo
            ref = args.ref
            if not repo or not ref:
                print("🔍 Auto-detect repo/ref dari git ...")
                try:
                    info = get_git_info()
                    if not repo:
                        repo = info["repo"]
                    if not ref:
                        ref = info["ref"]
                    print(f"   repo: {repo}  ref: {ref}")
                except BuildError as e:
                    print(f"❌ {e}", file=sys.stderr)
                    print("   Usage: bq --cicd-trigger <repo> <ref>", file=sys.stderr)
                    sys.exit(2)
            api_repo = normalize_repo(_expand_repo(repo, default_org))
            sys.exit(run_cicd_trigger(
                api_repo, ref,
                hook_url=args.hook_url or HOOK_URL_DEFAULT,
                sha_override=args.sha,
                dry_run=args.dry_run,
                force=args.force,
            ))

        if args.sops_encrypt or args.sops_decrypt:
            from pathlib import Path
            from . import sops as _sops
            target = args.sops_encrypt or args.sops_decrypt
            path = Path(target).expanduser()
            if not path.exists():
                print(f"❌ File tidak ada: {path}", file=sys.stderr)
                sys.exit(2)
            try:
                if args.sops_encrypt:
                    if args.sops_recipient:
                        recipients = [
                            r.strip() for r in args.sops_recipient.split(",")
                            if r.strip()
                        ]
                        source = "--sops-recipient"
                    else:
                        recipients, source = _sops.resolve_recipients(path)
                    out_recipients, out_source = _sops.encrypt_file(
                        path, recipients=recipients, source=source,
                    )
                    print(f"🔒 Encrypted: {path}")
                    print(f"   via      : {out_source or source}")
                    print(f"   recipient: {out_recipients}")
                else:
                    text = _sops.decrypt_file(
                        path, in_place=args.sops_in_place,
                    )
                    if args.sops_in_place:
                        print(f"🔓 Decrypted in-place: {path}")
                    else:
                        sys.stdout.write(text)
                sys.exit(0)
            except _sops.SopsError as e:
                print(f"❌ {e}", file=sys.stderr)
                sys.exit(1)

        if args.bootstrap_k8s:
            missing = []
            if not args.repo:            missing.append("<repo>")
            if not args.ref:             missing.append("<ref>")
            if not args.gitops_repo:     missing.append("--gitops-repo")
            if not args.gitops_branch:   missing.append("--gitops-branch")
            if not args.path_yaml:       missing.append("--path-yaml")
            if missing:
                print(
                    "❌ Usage: bq --bootstrap-k8s <repo> <ref> --gitops-repo <r> "
                    "--gitops-branch <b> --path-yaml <p>\n"
                    f"   Kurang: {', '.join(missing)}",
                    file=sys.stderr,
                )
                sys.exit(2)
            from .bootstrap import run_bootstrap_k8s
            config = load_config()
            default_org = config.get("git", {}).get("org", "")
            source_repo = normalize_repo(_expand_repo(args.repo, default_org))
            gitops_repo = normalize_repo(_expand_repo(args.gitops_repo, default_org))
            rc = run_bootstrap_k8s(
                source_repo, args.ref,
                gitops_repo, args.gitops_branch, args.path_yaml,
                cicd_path=args.cicd,
                dry_run=args.dry_run,
                keep_workdir=args.keep_workdir,
                replicas=args.replicas,
                stack_override=args.stack,
                env_override=args.env,
                pr_branch=args.pr_branch,
                apply_secret=args.apply_secret,
                kube_context=args.kube_context,
                image_pull_secret=args.image_pull_secret,
                force_recreate_deploy=args.force_recreate_deploy,
                namespace_override=args.namespace,
                nodepool_override=args.nodepool,
            )
            sys.exit(rc)

        if args.set_image:
            # Positional: <ns> <deployment> <image> [container]
            positionals = [args.repo, args.ref, *args.extra_args]
            positionals = [p for p in positionals if p]
            if len(positionals) < 3:
                print(
                    "❌ Usage: bq --set-image <ns> <deployment> <image> [container]",
                    file=sys.stderr,
                )
                sys.exit(2)
            ns_arg, deploy_arg, image_arg = positionals[0], positionals[1], positionals[2]
            container_arg = positionals[3] if len(positionals) > 3 else None
            from .set_image import run_set_image
            sys.exit(run_set_image(
                ns_arg, deploy_arg, image_arg, container=container_arg,
            ))

        if args.gitops_set_image:
            # Positional: <gitops-repo> <branch> <path.yaml> <image_full>
            positionals = [args.repo, args.ref, *args.extra_args]
            positionals = [p for p in positionals if p]
            if len(positionals) < 4:
                print(
                    "❌ Usage: bq --gitops-set-image <repo> <branch> <path.yaml> <image_full>",
                    file=sys.stderr,
                )
                sys.exit(2)
            gr, gb, gp, gi = positionals[0], positionals[1], positionals[2], positionals[3]
            config = load_config()
            default_org = config.get("git", {}).get("org", "")
            gr = normalize_repo(_expand_repo(gr, default_org))
            from .gitops_set_image import run_gitops_set_image
            sys.exit(run_gitops_set_image(gr, gb, gp, gi))

        if args.is_match_image:
            # Positional: <ns> <deployment> <gitops-repo> <branch> <path.yaml>
            positionals = [args.repo, args.ref, *args.extra_args]
            positionals = [p for p in positionals if p]
            if len(positionals) < 5:
                print(
                    "❌ Usage: bq --is-match-image <ns> <deployment> "
                    "<gitops-repo> <branch> <path.yaml>",
                    file=sys.stderr,
                )
                sys.exit(2)
            ns_, dep_, gr, gb, gp = positionals[:5]
            config = load_config()
            default_org = config.get("git", {}).get("org", "")
            gr = normalize_repo(_expand_repo(gr, default_org))
            from .is_match_image import run_is_match_image
            sys.exit(run_is_match_image(ns_, dep_, gr, gb, gp))

        if args.check:
            if not args.repo or not args.ref:
                print("❌ Usage: bq --check <repo> <ref> --remote", file=sys.stderr)
                sys.exit(2)
            from .check import run_check
            config = load_config()
            default_org = config.get("git", {}).get("org", "")
            api_repo = normalize_repo(_expand_repo(args.repo, default_org))
            rc = run_check(
                api_repo, args.ref,
                cicd_path=args.cicd,
                ns=args.ns,
                infra=args.infra,
            )
            sys.exit(rc)

        if args.repo_check:
            if not args.repo:
                print("❌ Usage: bq --repo-check <repo> [<ref>] [--cicd=pb] [--dry-run]",
                      file=sys.stderr)
                sys.exit(2)
            from .repo import run_repo_check
            rc = run_repo_check(
                repo=args.repo,
                ref=args.ref,
                cicd_source=args.cicd,
                dry_run=args.dry_run,
            )
            sys.exit(rc)

        if args.config:
            config = load_config()
            builder = config["builder"]
            registry = config["registry"]
            print(f"📋 build-q configuration  ({ENV_FILE})")
            print(f"   Builder name : {builder['name']}")
            print(f"   Memory       : {builder['memory']}")
            print(f"   CPU period   : {builder['cpu_period']}")
            print(f"   CPU quota    : {builder['cpu_quota']}")
            print(f"   Registry URL : {registry['url'] or '(not set)'}")
            return

        # ── Build command ─────────────────────────────────────────────────────────
        # Auto-detect repo/ref from git when not provided
        repo = args.repo
        ref = args.ref
        
        original_cwd = os.getcwd()
        clone_dir = None
        cicd_data = None
        
        if args.remote:
            if not repo or not ref:
                print("❌ Error: <repo> and <ref> are required when using --remote.", file=sys.stderr)
                sys.exit(1)
            
            import json
            config = load_config()
            ssh_prefix = config.get("git", {}).get("ssh_prefix", "git@github.com:")
            default_org = config.get("git", {}).get("org", "")

            expanded = _expand_repo(repo, default_org)
            if expanded != repo:
                print(f"🏷️ Expanded '{repo}' → '{expanded}' using GITHUB_ORG")
                repo = expanded

            api_repo = normalize_repo(repo)

            candidates = cicd_candidates(args.cicd)
            print(f"🔍 Fetching {' | '.join(candidates)} from remote {api_repo}@{ref} ...")
            cicd_data = None
            try:
                for cand in candidates:
                    cicd_raw = _github_contents(api_repo, cand, ref)
                    if cicd_raw:
                        cicd_data = json.loads(cicd_raw)
                        if cand != args.cicd:
                            print(f"   ↪ fell back to {cand}")
                        args.cicd = cand
                        break
                if cicd_data is None:
                    print(f"⚠️ Warning: Could not fetch cicd config from remote (tried: {', '.join(candidates)}). Using empty defaults.")
                    cicd_data = {}
            except Exception as e:
                print(f"⚠️ Warning: Error fetching remote cicd.json: {e}. Using empty defaults.")
                cicd_data = {}

            if repo.startswith("http://") or repo.startswith("https://") or repo.startswith("git@") or repo.startswith("git://"):
                git_url = repo
            else:
                git_url = f"{ssh_prefix}{repo}.git"

            # Buildkit auto-enables SSH agent forwarding for SSH git contexts.
            # Without SSH_AUTH_SOCK, buildx fails with:
            #   "invalid empty ssh agent socket: make sure SSH_AUTH_SOCK is set"
            # Fall back to HTTPS + GIT_AUTH_TOKEN secret when the agent isn't reachable.
            if git_url.startswith("git@") and not os.environ.get("SSH_AUTH_SOCK"):
                try:
                    gh_token = _github_auth_token()
                except (subprocess.CalledProcessError, FileNotFoundError, GitHubAPIError):
                    print(
                        "❌ SSH_AUTH_SOCK is not set and GitHub token lookup failed.\n"
                        "   Either start ssh-agent (`eval $(ssh-agent) && ssh-add`),\n"
                        "   run `gh auth login`, or set GITHUB_TOKEN in ~/.build-q/.env, then retry.",
                        file=sys.stderr,
                    )
                    sys.exit(1)

                git_url = f"https://github.com/{api_repo}.git"
                os.environ["GIT_AUTH_TOKEN"] = gh_token
                args.secret = list(args.secret or [])
                if not any(s.startswith("id=GIT_AUTH_TOKEN") for s in args.secret):
                    args.secret.append("id=GIT_AUTH_TOKEN,env=GIT_AUTH_TOKEN")
                print("ℹ️ SSH_AUTH_SOCK not set — using HTTPS + GIT_AUTH_TOKEN secret for remote git context.")

            args.context = f"{git_url}#{ref}"
            print(f"🌐 Remote context set to: {args.context}")
            
            repo_name = api_repo.split("/")[-1]
            if not args.tag:
                ref_id = "unknown"
                try:
                    sha = _github_commit_sha(api_repo, ref)
                    if sha:
                        ref_id = _ref_id_for_image_tag(ref, sha)
                except Exception:
                    pass
                registry_url = config.get("registry", {}).get("url", "")
                image_name = cicd_data.get("IMAGE", repo_name)
                args.tag = f"{registry_url}/{image_name}:{ref_id}" if registry_url else f"{image_name}:{ref_id}"
                print(f"🏷️ Auto-generated tag for remote: {args.tag}")
            
            repo = repo_name

        elif args.clone:
            if not ref and repo:
                ref = repo
                repo = None
            if not ref:
                print("❌ Error: <ref> is required when using --clone to specify the branch/tag.", file=sys.stderr)
                sys.exit(1)

            config = load_config()
            default_org = config.get("git", {}).get("org", "")
            expanded_clone = _expand_repo(args.clone, default_org)
            if expanded_clone != args.clone:
                print(f"🏷️ Expanded '{args.clone}' → '{expanded_clone}' using GITHUB_ORG")
                args.clone = expanded_clone

            if args.image_check:
                preview_tag = args.tag
                if not preview_tag:
                    print("🔍 Predicting image tag without cloning...")
                    import json
                    from .builder import check_image_exists
                    config = load_config()
                    registry_url = config.get("registry", {}).get("url", "")
                    
                    api_repo = normalize_repo(args.clone)


                    try:
                        sha = _github_commit_sha(api_repo, ref)
                        ref_id = _ref_id_for_image_tag(ref, sha)

                        clone_dir = api_repo.split("/")[-1]
                        image_name = repo if repo else clone_dir

                        for cand in cicd_candidates(args.cicd):
                            cicd_raw = _github_contents(api_repo, cand, ref)
                            if not cicd_raw:
                                continue
                            try:
                                cicd_data = json.loads(cicd_raw)
                                image_name = cicd_data.get("IMAGE", image_name)
                                if cand != args.cicd:
                                    args.cicd = cand
                                break
                            except json.JSONDecodeError:
                                pass

                        preview_tag = f"{registry_url}/{image_name}:{ref_id}" if registry_url else f"{image_name}:{ref_id}"

                    except (subprocess.CalledProcessError, GitHubAPIError):
                        print("⚠️ Could not fetch remote info, skipping pre-clone check.", file=sys.stderr)
                
                if preview_tag:
                    from .builder import check_image_exists
                    print(f"🔍 Checking registry for existing image: {preview_tag} ...")
                    if check_image_exists(preview_tag):
                        print(f"✅ Image {preview_tag} already exists in the registry.")
                        print("⏭️ Skipping clone and build.")
                        sys.exit(0)
            
            clone_dir = args.clone.split("/")[-1]
            if os.path.isdir(clone_dir):
                if not os.path.isdir(os.path.join(clone_dir, ".git")):
                    print(
                        f"❌ Directory '{clone_dir}' already exists and is not a git repository.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                print(f"📂 Directory '{clone_dir}' already exists — reusing existing checkout.")
                try:
                    remote_url = subprocess.run(
                        ["git", "-C", clone_dir, "remote", "get-url", "origin"],
                        capture_output=True, text=True, check=True,
                    ).stdout.strip()
                except subprocess.CalledProcessError:
                    print(
                        f"❌ Failed to read origin remote of '{clone_dir}'.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                expected_repo = normalize_repo(args.clone).lower()
                if expected_repo not in normalize_repo(remote_url).lower():
                    print(
                        f"❌ Existing '{clone_dir}' points to '{remote_url}', "
                        f"expected '{expected_repo}'. Remove or rename it and retry.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                try:
                    subprocess.run(
                        ["git", "-C", clone_dir, "fetch", "origin", ref],
                        check=True,
                    )
                    subprocess.run(
                        ["git", "-C", clone_dir, "checkout", ref],
                        check=True,
                    )
                    subprocess.run(
                        ["git", "-C", clone_dir, "merge", "--ff-only", f"origin/{ref}"],
                        check=True,
                    )
                except subprocess.CalledProcessError as e:
                    print(
                        f"❌ Failed to update existing '{clone_dir}' to '{ref}': {e}. "
                        "Resolve the working tree or remove the directory and retry.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                except FileNotFoundError:
                    print("❌ `git` not found in PATH.", file=sys.stderr)
                    sys.exit(1)
            else:
                print(f"📥 Cloning repository {args.clone} (branch: {ref}) ...")
                try:
                    _github_clone(args.clone, ref)
                except subprocess.CalledProcessError as e:
                    print(f"❌ Failed to clone repository: {e}", file=sys.stderr)
                    sys.exit(1)
                except FileNotFoundError:
                    print("❌ Required CLI not found (`gh` or `git`). Install one, or toggle GH_CLI.", file=sys.stderr)
                    sys.exit(1)
                except GitHubAPIError as e:
                    print(f"❌ Failed to clone repository: {e}", file=sys.stderr)
                    sys.exit(1)

            print(f"📁 Changing directory to {clone_dir} ...")
            os.chdir(clone_dir)
            
            if not repo:
                repo = clone_dir
        else:
            if not repo or not ref:
                print("🔍 Auto-detecting repo and branch from git...")
                try:
                    info = get_git_info()
                except BuildError as e:
                    print(f"❌ {e}", file=sys.stderr)
                    print("   Provide <repo> and <ref> explicitly, or run from a git directory.", file=sys.stderr)
                    sys.exit(1)
                if not repo:
                    repo = info["repo"]
                if not ref:
                    ref = info["ref"]
                print(f"   repo: {repo}  ref: {ref}")

        if args.gh_auth:
            try:
                gh_user = _github_user_login()
                gh_token = _github_auth_token()
            except FileNotFoundError:
                print("❌ 'gh' CLI not found. Install (brew install gh) or set GH_CLI=false + GITHUB_TOKEN.", file=sys.stderr)
                sys.exit(1)
            except subprocess.CalledProcessError as e:
                print(f"❌ Failed to fetch gh credentials: {e.stderr.strip()}", file=sys.stderr)
                sys.exit(1)
            except GitHubAPIError as e:
                print(f"❌ Failed to fetch GitHub credentials: {e}", file=sys.stderr)
                sys.exit(1)

            args.build_args = list(args.build_args or [])
            if not any(a.startswith("GITHUB_USER=") for a in args.build_args):
                args.build_args.append(f"GITHUB_USER={gh_user}")
            if not any(a.startswith("GITHUB_TOKEN=") for a in args.build_args):
                args.build_args.append(f"GITHUB_TOKEN={gh_token}")
            print(f"🔐 --gh-auth: injected GITHUB_USER={gh_user}, GITHUB_TOKEN=***")

        try:
            if args.compose:
                rc = run_compose(
                    repo=repo,
                    ref=ref,
                    cicd_path=args.cicd,
                    cicd_dict=cicd_data,
                    tag=args.tag,
                    dry_run=args.dry_run,
                    image_check=args.image_check,
                    rollout_ns=args.ns,
                    rollout_infra=args.infra,
                    rollout_path=args.gitops_path,
                )
            else:
                rc = run_build(
                    repo=repo,
                    ref=ref,
                    cicd_path=args.cicd,
                    cicd_dict=cicd_data,
                    platform=args.platform,
                    push=args.push,
                    tag=args.tag,
                    dockerfile=args.dockerfile,
                    context=args.context,
                    extra_build_args=args.build_args,
                    secrets=args.secret,
                    dry_run=args.dry_run,
                    image_check=args.image_check,
                    rollout_ns=args.ns,
                    rollout_infra=args.infra,
                    rollout_path=args.gitops_path,
                )
            sys.exit(rc)
        finally:
            if args.clone and args.clean and clone_dir:
                os.chdir(original_cwd)
                print(f"🧹 Cleaning up: removing {clone_dir} ...")
                import shutil
                shutil.rmtree(clone_dir, ignore_errors=True)

    except (BuildError, FileNotFoundError) as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n Aborted by user.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"❌ Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
