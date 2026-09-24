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

Config file: ~/.build-q/.env
""",
    )

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    # Subcommand flags
    parser.add_argument("--init", action="store_true", help="Initialize ~/.build-q/.env config file")
    parser.add_argument("--force", action="store_true", help="Force recreate config (use with --init)")
    parser.add_argument("--config", action="store_true", help="Show current configuration")
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
        help="Scaffold Makefile / compose.yaml / Dockerfile / trigger-ci.yml from cicd/cicd.json",
    )
    parser.add_argument(
        "--init-legacy",
        action="store_true",
        help="Scaffold Makefile + compose.yaml pola legacy (GITHUB_USER/TOKEN build-args, auto gh auth token)",
    )
    parser.add_argument(
        "--init-secrets",
        action="store_true",
        help="Set GitHub Actions webhook secrets on target repo (auto-detect from git)",
    )
    parser.add_argument(
        "--gh-action-init",
        action="store_true",
        help="Bootstrap standar .github/workflows/trigger-ci.yml — hapus workflow lain + set webhook secrets",
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
        "--pr-fix",
        action="store_true",
        help="One-shot fix: clone → branch → regenerate jx-init artifacts → push → set secrets → open PR.",
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

    args = parser.parse_args()

    try:
        # ── Subcommands ──────────────────────────────────────────────────────────
        if args.init:
            init_config(force=args.force)
            config = load_config()
            ensure_builder(config["builder"]["name"], bootstrap=True)
            return

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
            sys.exit(rc)

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

            clone_dir = args.clone.split("/")[-1]
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
