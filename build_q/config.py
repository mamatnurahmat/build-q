"""Configuration management for build-q CLI."""
import os
import json
from pathlib import Path
from typing import Dict, Any, Optional


CONFIG_DIR = Path.home() / ".build-q"
ENV_FILE = CONFIG_DIR / ".env"


def ensure_config_dir() -> None:
    """Ensure config directory exists."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _load_dotenv(path: Path) -> None:
    """Minimal dotenv loader (no external dependency)."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def load_config() -> Dict[str, Any]:
    """Load configuration from environment variables and ~/.build-q/.env.

    Priority: env vars > .env file > defaults.
    """
    ensure_config_dir()

    if not ENV_FILE.exists():
        init_config(silent=True)

    if ENV_FILE.exists():
        _load_dotenv(ENV_FILE)

    return {
        "builder": {
            "name": os.getenv("BUILDER_NAME", "mybuilder"),
            "memory": os.getenv("DEFAULT_MEMORY", "4g"),
            "cpu_period": os.getenv("DEFAULT_CPU_PERIOD", "100000"),
            "cpu_quota": os.getenv("DEFAULT_CPU_QUOTA", "200000"),
        },
        "registry": {
            "url": os.getenv("REGISTRY_URL", ""),
        },
        "git": {
            "ssh_prefix": os.getenv("GIT_SSH_PREFIX", "git@github.com:"),
            "org": os.getenv("GITHUB_ORG", ""),
        },
        "webhook": {
            "trigger_url": os.getenv("WEBHOOK_TRIGGER_URL", "https://cicd-hw.qoin.id/trigger"),
            "k8s_context": os.getenv("JX_KUBE_CONTEXT", ""),
            "k8s_namespace": os.getenv("JX_KUBE_NAMESPACE", "jenkins-x"),
            "k8s_secret": os.getenv("JX_TOKEN_SECRET", "webhook-trigger-token"),
        },
        "github": {
            "use_cli": _parse_bool(os.getenv("GH_CLI", "true"), default=True),
            "user": os.getenv("GITHUB_USER", ""),
            "token": os.getenv("GITHUB_TOKEN", ""),
            "api_base": os.getenv("GITHUB_API_BASE", "https://api.github.com"),
        },
        "dockerhub": {
            "user": os.getenv("DOCKERHUB_USER", ""),
            "org": os.getenv("DOCKERHUB_ORG", ""),
            "token": os.getenv("DOCKERHUB_TOKEN", ""),
        },
        "gitops": {
            "repo": os.getenv("GITOPS_REPO", "Qoin-Digital-Indonesia/gitops"),
            "branch": os.getenv("GITOPS_BRANCH", "main"),
            "infra": os.getenv("GITOPS_INFRA", "cce"),
            "ns_suffix": os.getenv("NS_SUFFIX", "qoin"),
        },
    }


def _parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def use_gh_cli() -> bool:
    """Toggle: when true (default) keep existing `gh` subprocess flow.

    Set GH_CLI=false in ~/.build-q/.env to route via native REST/git.
    """
    return load_config()["github"]["use_cli"]


def get_github_credentials() -> Dict[str, str]:
    """Return {'user','token'} from env / .env (empty strings if unset)."""
    cfg = load_config()["github"]
    return {"user": cfg["user"], "token": cfg["token"]}


def init_config(force: bool = False, silent: bool = False) -> None:
    """Create default config file at ~/.build-q/.env."""
    ensure_config_dir()
    if ENV_FILE.exists() and not force:
        if not silent:
            print(f"ℹ️  Config already exists: {ENV_FILE}")
            print("   Use --init --force to recreate.")
        return

    default = """\
# build-q Configuration

# Docker builder name
BUILDER_NAME=mybuilder

# Resource limits
DEFAULT_MEMORY=4g
DEFAULT_CPU_PERIOD=100000
DEFAULT_CPU_QUOTA=200000

# Container registry URL
REGISTRY_URL=registry.example.com

# Git remote settings (for --remote / --clone)
GIT_SSH_PREFIX=git@github.com:

# Default GitHub organization — when set, shorthand works: `bq <repo> <ref> --remote`
# Example: GITHUB_ORG=Qoin-Digital-Indonesia
GITHUB_ORG=

# Jenkins X webhook trigger — used by `bq --init-secrets` to configure GitHub Actions
WEBHOOK_TRIGGER_URL=https://cicd-hw.qoin.id/trigger
# Optional: kubectl context/namespace/secret for fetching the trigger token.
# Leave JX_KUBE_CONTEXT empty to use current kubectl context.
JX_KUBE_CONTEXT=
JX_KUBE_NAMESPACE=jenkins-x
JX_TOKEN_SECRET=webhook-trigger-token

# GitHub access
# GH_CLI=true (default) → keep existing `gh` CLI flow (no change).
# GH_CLI=false          → use native REST + `git`. Requires GITHUB_TOKEN below.
GH_CLI=true
# Personal Access Token (classic or fine-grained) used when GH_CLI=false.
# Needs: repo (contents:read, actions:write for secrets), read:user.
GITHUB_USER=
GITHUB_TOKEN=

# DockerHub — dipakai untuk suggestion image prefix + docker login manual.
# DOCKERHUB_ORG (mis. loyaltolpi) menjadi prefix image saat generate suggestion.
# Fallback ke REGISTRY_URL di atas bila DOCKERHUB_ORG kosong.
DOCKERHUB_USER=
DOCKERHUB_ORG=
DOCKERHUB_TOKEN=

# GitOps rollout suggestions — dipakai pasca-build sukses untuk generate
# perintah `set-image` dan `gitops-set-image` siap copy-paste.
GITOPS_REPO=Qoin-Digital-Indonesia/gitops
GITOPS_BRANCH=main
# GITOPS_INFRA: cce (Huawei CCE) | k8s (SLS). Bisa di-override per-run: --infra
GITOPS_INFRA=cce
# NS_SUFFIX: bila --ns tidak diberikan, ns = <env>-<suffix>
# (env dihitung dari branch: main→production, staging→staging, develop→develop)
NS_SUFFIX=qoin
"""
    ENV_FILE.write_text(default)
    ENV_FILE.chmod(0o600)
    if not silent:
        print(f"✅ Config created: {ENV_FILE}")
        print("   Edit the file to set your registry and builder settings.")


DEFAULT_CICD_PATH = "cicd/cicd.json"


def cicd_candidates(cicd_path: str) -> list:
    """Search order for cicd config. If caller uses the default path,
    also try the flat `cicd.json` at repo root (some repos keep it there).
    Explicit paths are honored as-is, no fallback.
    """
    if cicd_path == DEFAULT_CICD_PATH:
        return [cicd_path, "cicd.json"]
    return [cicd_path]


def load_local_cicd(cicd_path: str = DEFAULT_CICD_PATH) -> Dict[str, Any]:
    """Load cicd.json from local filesystem.

    Args:
        cicd_path: Relative or absolute path to cicd.json

    Returns:
        Parsed JSON as dict

    Raises:
        FileNotFoundError: If no candidate exists
        json.JSONDecodeError: If invalid JSON
    """
    for candidate in cicd_candidates(cicd_path):
        p = Path(candidate)
        if p.exists():
            try:
                with open(p) as f:
                    return json.load(f)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in {candidate}: {e}")
    raise FileNotFoundError(f"CICD config not found: {cicd_path}")
