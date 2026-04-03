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
        )
        sys.exit(rc)

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
