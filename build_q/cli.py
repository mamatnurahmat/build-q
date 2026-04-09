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
import subprocess
import sys

from . import __version__
from .builder import BuildError, get_git_info, run_build
from .config import init_config, load_config, ENV_FILE


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
        "--no-image-check",
        action="store_false",
        dest="image_check",
        help="Do not check registry for existing image",
    )
    parser.add_argument(
        "--platform",
        metavar="PLATFORM",
        help='Target platform (e.g. "linux/amd64" or "linux/amd64,linux/arm64")',
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
    parser.add_argument("--dry-run", action="store_true", help="Print command without executing")

    args = parser.parse_args()

    try:
        # ── Subcommands ──────────────────────────────────────────────────────────
        if args.init:
            init_config(force=args.force)
            return

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
        
        if args.clone:
            if not ref and repo:
                ref = repo
                repo = None
            if not ref:
                print("❌ Error: <ref> is required when using --clone to specify the branch/tag.", file=sys.stderr)
                sys.exit(1)
            
            if args.image_check:
                preview_tag = args.tag
                if not preview_tag:
                    print("🔍 Predicting image tag without cloning...")
                    import json
                    from .builder import check_image_exists
                    config = load_config()
                    registry_url = config.get("registry", {}).get("url", "")
                    
                    api_repo = args.clone
                    if api_repo.endswith(".git"):
                        api_repo = api_repo[:-4]
                    parts = api_repo.replace(":", "/").split("/")
                    api_repo = f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else args.clone
                    
                    try:
                        sha_res = subprocess.run(
                            ["gh", "api", f"repos/{api_repo}/commits/{ref}", "--jq", ".sha"],
                            capture_output=True, text=True, check=True
                        )
                        commit_hash = sha_res.stdout.strip()[:7]
                        
                        cicd_res = subprocess.run(
                            ["gh", "api", f"repos/{api_repo}/contents/{args.cicd}?ref={ref}", "-H", "Accept: application/vnd.github.v3.raw"],
                            capture_output=True, text=True
                        )
                        
                        clone_dir = api_repo.split("/")[-1]
                        image_name = repo if repo else clone_dir
                        
                        if cicd_res.returncode == 0 and cicd_res.stdout:
                            try:
                                cicd_data = json.loads(cicd_res.stdout)
                                image_name = cicd_data.get("IMAGE", image_name)
                            except json.JSONDecodeError:
                                pass
                                
                        preview_tag = f"{registry_url}/{image_name}:{commit_hash}" if registry_url else f"{image_name}:{commit_hash}"
                        
                    except subprocess.CalledProcessError:
                        print("⚠️ Could not fetch remote info via gh api, skipping pre-clone check.", file=sys.stderr)
                
                if preview_tag:
                    from .builder import check_image_exists
                    print(f"🔍 Checking registry for existing image: {preview_tag} ...")
                    if check_image_exists(preview_tag):
                        print(f"✅ Image {preview_tag} already exists in the registry.")
                        print("⏭️ Skipping clone and build.")
                        sys.exit(0)
            
            print(f"📥 Cloning repository {args.clone} (branch: {ref}) ...")
            try:
                subprocess.run(
                    ["gh", "repo", "clone", args.clone, "--", "--branch", ref, "--single-branch"],
                    check=True
                )
            except subprocess.CalledProcessError as e:
                print(f"❌ Failed to clone repository: {e}", file=sys.stderr)
                sys.exit(1)
            except FileNotFoundError:
                print("❌ GitHub CLI ('gh') is not installed or not in PATH.", file=sys.stderr)
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

        try:
            rc = run_build(
                repo=repo,
                ref=ref,
                cicd_path=args.cicd,
                platform=args.platform,
                push=args.push,
                tag=args.tag,
                dockerfile=args.dockerfile,
                context=args.context,
                extra_build_args=args.build_args,
                secrets=args.secret,
                dry_run=args.dry_run,
                image_check=args.image_check,
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
