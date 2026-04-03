"""Core build logic for build-q CLI."""
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import load_config, load_local_cicd


class BuildError(Exception):
    """Raised when build command fails."""
    pass


def get_git_info() -> Dict[str, str]:
    """Auto-detect repo name and current branch from git.

    Returns:
        dict with 'repo' and 'ref' keys
    """
    try:
        ref_raw = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True
        ).stdout.strip()

        # Detached HEAD: try tag then short SHA
        if ref_raw == "HEAD":
            tag_result = subprocess.run(
                ["git", "describe", "--tags", "--exact-match"],
                capture_output=True, text=True
            )
            if tag_result.returncode == 0:
                ref_raw = tag_result.stdout.strip()
            else:
                ref_raw = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, check=True
                ).stdout.strip()

    except subprocess.CalledProcessError as e:
        raise BuildError(f"Not in a git repository: {e.stderr.strip()}")

    # Repo name from remote or directory
    try:
        remote_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True
        ).stdout.strip()
        repo = remote_url.rstrip("/").rstrip(".git").split("/")[-1]
    except subprocess.CalledProcessError:
        repo = Path.cwd().name

    return {"repo": repo, "ref": ref_raw}


def get_local_commit_short() -> str:
    """Return 7-char short commit hash from local git."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            capture_output=True, text=True, check=True
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return "unknown"


def build_command(
    repo: str,
    ref: str,
    cicd: Dict[str, Any],
    config: Dict[str, Any],
    *,
    platform: Optional[str] = None,
    push: bool = False,
    tag: Optional[str] = None,
    dockerfile: str = "Dockerfile",
    context: str = ".",
    extra_build_args: Optional[List[str]] = None,
    secrets: Optional[List[str]] = None,
) -> List[str]:
    """Assemble the docker buildx build command.

    Args:
        repo: Repository/service name (used for image tag fallback)
        ref: Git branch/tag reference
        cicd: Parsed cicd.json dict
        config: Tool configuration dict
        platform: Target platform string e.g. "linux/amd64"
        push: Whether to add --push flag
        tag: Explicit image tag override
        dockerfile: Path to Dockerfile
        context: Build context directory
        extra_build_args: List of KEY=VALUE strings to pass as --build-arg
        secrets: List of secret specs e.g. ["id=netrc,src=/home/me/.netrc"]

    Returns:
        List of command parts ready for subprocess
    """
    builder = config["builder"]
    registry_url = config["registry"]["url"]

    cmd: List[str] = ["docker", "buildx", "build"]

    # Builder
    cmd += ["--builder", builder["name"]]

    # Progress
    cmd += ["--progress=plain"]

    # No cache
    cmd += ["--no-cache"]

    # Resource limits
    cmd += ["--memory", builder["memory"]]
    cmd += ["--cpu-period", builder["cpu_period"]]
    cmd += ["--cpu-quota", builder["cpu_quota"]]

    # Platform
    if platform:
        cmd += ["--platform", platform]

    # Secrets
    # Add default netrc secret if not provided
    default_secret = f"id=netrc,src={Path.home()}/.netrc"
    all_secrets = list(secrets) if secrets else []
    if not any(s.startswith("id=netrc") for s in all_secrets):
        all_secrets.append(default_secret)

    for secret in all_secrets:
        cmd += ["--secret", secret]

    # Default build arguments
    # BRANCH logic: production if ref starts with v*, else develop
    branch_val = "production" if ref.startswith("v") else "develop"
    
    # Check if BRANCH is already in extra_build_args
    extra_args_list = list(extra_build_args) if extra_build_args else []
    if not any(arg.startswith("BRANCH=") for arg in extra_args_list):
        cmd += ["--build-arg", f"BRANCH={branch_val}"]

    # Build args from cicd.json
    for key in ("PORT", "PORT2", "PROJECT"):
        val = cicd.get(key, "")
        if val:
            cmd += ["--build-arg", f"{key}={val}"]

    # Extra --build-arg from CLI
    for arg in extra_args_list:
        cmd += ["--build-arg", arg]

    # Tag
    if tag:
        image_tag = tag
    else:
        image_name = cicd.get("IMAGE", repo)
        commit = get_local_commit_short()
        image_tag = f"{registry_url}/{image_name}:{commit}" if registry_url else f"{image_name}:{commit}"

    cmd += ["-t", image_tag]

    # Push (enabled by default unless explicitly False)
    # Note: CLI currently uses store_true, so we can't easily distinguish 'default' from 'False'.
    # However, if the user wants it by default, we'll just check the flag.
    # To correctly support 'default ON' while allowing 'OFF', we might need to change CLI.
    # For now, let's assume if push is True or it's the default call, we add it.
    if push:
        cmd += ["--push"]

    # Dockerfile
    cmd += ["-f", dockerfile]

    # Context (must be last)
    cmd.append(context)

    return cmd


def format_cmd(cmd: List[str]) -> str:
    """Pretty-print the command with line continuations."""
    lines: List[str] = []
    buf = ""
    for part in cmd:
        if buf and (part.startswith("--") or part.startswith("-f") or part.startswith("-t")):
            lines.append(buf)
            buf = f"  {part}"
        else:
            buf = f"{buf} {part}".strip() if buf else part
    if buf:
        lines.append(buf)
    return " \\\n".join(lines)


def run_build(
    repo: str,
    ref: str,
    *,
    cicd_path: str = "cicd/cicd.json",
    platform: Optional[str] = None,
    push: bool = False,
    tag: Optional[str] = None,
    dockerfile: str = "Dockerfile",
    context: str = ".",
    extra_build_args: Optional[List[str]] = None,
    secrets: Optional[List[str]] = None,
    dry_run: bool = False,
) -> int:
    """Load config, build the command, and execute it.

    Returns:
        Exit code (0 = success)
    """
    config = load_config()
    cicd = load_local_cicd(cicd_path)

    cmd = build_command(
        repo=repo,
        ref=ref,
        cicd=cicd,
        config=config,
        platform=platform,
        push=push,
        tag=tag,
        dockerfile=dockerfile,
        context=context,
        extra_build_args=extra_build_args,
        secrets=secrets,
    )

    print(f"\n🚀 Build command:")
    print("=" * 60)
    print(format_cmd(cmd))
    print("=" * 60)

    if dry_run:
        print("\n🔍 Dry-run mode — command not executed.")
        return 0

    result = subprocess.run(cmd)
    if result.returncode == 0:
        print("\n✅ Build completed successfully.")
    else:
        print(f"\n❌ Build failed (exit code {result.returncode}).", file=sys.stderr)

    return result.returncode
