"""Configuration management for build-q CLI."""
import os
import json
from pathlib import Path
from typing import Dict, Any, Optional


# Default config remains ~/.build-q, but deployments can select a separate
# local env file (for example ~/build-q/.env) without changing credentials.
_DEFAULT_CONFIG_DIR = Path.home() / ".build-q"
CONFIG_DIR = Path(os.getenv("BUILD_Q_CONFIG_DIR", str(_DEFAULT_CONFIG_DIR))).expanduser()
ENV_FILE = Path(
    os.getenv("BUILD_Q_ENV_FILE", str(CONFIG_DIR / ".env"))
).expanduser()


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

    Priority: shell env > PB API (bila PB_API=true) > .env file > defaults.
    """
    ensure_config_dir()

    if not ENV_FILE.exists():
        init_config(silent=True)

    if ENV_FILE.exists():
        _load_dotenv(ENV_FILE)

    # Bila PB_API=true, overlay credentials dari PB (PocketBase IDP) ke
    # os.environ SEBELUM kita baca nilainya di bawah. Shell env tetap menang
    # (hydrate_env pakai setdefault). Fallback resilient: warning + lanjut.
    from . import pb_api
    # Set BUILD_Q_DISABLE_PB_API=true for an explicitly local-only run.
    # This is useful on Docker-context hosts where credentials must stay in
    # the selected local env file and no PocketBase request should be made.
    if pb_api.is_enabled() and not _parse_bool(
        os.getenv("BUILD_Q_DISABLE_PB_API", "false")
    ):
        pb_api.hydrate_env(quiet=True)

    return build_config(os.getenv)


def build_config(getenv) -> Dict[str, Any]:
    """Susun dict config dari `getenv(key, default)` — tanpa efek samping.

    `load_config()` memakai `os.getenv`; `bq --serve` memakai secret PocketBase
    milik user yang me-request (lihat `user_creds.user_config`).
    """
    return {
        "builder": {
            "name": getenv("BUILDER_NAME", "mybuilder"),
            "memory": getenv("DEFAULT_MEMORY", "4g"),
            "cpu_period": getenv("DEFAULT_CPU_PERIOD", "100000"),
            "cpu_quota": getenv("DEFAULT_CPU_QUOTA", "200000"),
        },
        "registry": {
            "url": getenv("REGISTRY_URL", ""),
        },
        "git": {
            "ssh_prefix": getenv("GIT_SSH_PREFIX", "git@github.com:"),
            "org": getenv("GITHUB_ORG", ""),
        },
        "webhook": {
            "trigger_url": getenv("WEBHOOK_TRIGGER_URL", "https://cicd-hw.qoin.id/trigger"),
            "trigger_token": getenv("WEBHOOK_TRIGGER_TOKEN", ""),
            "k8s_context": getenv("JX_KUBE_CONTEXT", "hw-dev"),
            "k8s_namespace": getenv("JX_KUBE_NAMESPACE", "jenkins-x"),
            "k8s_secret": getenv("JX_TOKEN_SECRET", "webhook-trigger-token"),
        },
        "pr_fix": {
            "branch_prefix": getenv("PR_FIX_BRANCH_PREFIX", "fix/jx-init-"),
            "base_workdir": getenv("PR_FIX_BASE_WORKDIR", "/tmp"),
        },
        "github": {
            "use_cli": _parse_bool(getenv("GH_CLI", "false"), default=False),
            "user": getenv("GITHUB_USER", ""),
            "token": getenv("GITHUB_TOKEN", ""),
            "api_base": getenv("GITHUB_API_BASE", "https://api.github.com"),
        },
        "dockerhub": {
            "user": getenv("DOCKERHUB_USER", ""),
            "org": getenv("DOCKERHUB_ORG", ""),
            "token": getenv("DOCKERHUB_TOKEN", ""),
        },
        "argocd": {
            "k8s_context": getenv("ARGOCD_KUBE_CONTEXT", "hw-pro-p"),
            "k8s_namespace": getenv("ARGOCD_NAMESPACE", "argocd"),
            # Fallback bila cluster tak terjangkau kubectl: REST API ArgoCD
            # (sama dgn MCP argocd / skill argocd-app). Token SSO dari env
            # ARGOCD_TOKEN, atau key ARGOCD_TOKEN di ARGOCD_AUTH_FILE.
            "api_url": getenv("ARGOCD_API_URL", "https://cicd-hw.qoin.id/argocd"),
            "token": getenv("ARGOCD_TOKEN", ""),
            "auth_file": getenv("ARGOCD_AUTH_FILE", "~/.pi/agent/auth.json"),
            # Token kedaluwarsa → jalankan `argocd-app token-login` (TTY saja).
            "auto_login": _parse_bool(getenv("ARGOCD_AUTO_LOGIN", "true"), default=True),
        },
        "gitops": {
            "repo": getenv("GITOPS_REPO", "Qoin-Digital-Indonesia/gitops"),
            "branch": getenv("GITOPS_BRANCH", "main"),
            "infra": getenv("GITOPS_INFRA", "cce"),
            "ns_suffix": getenv("NS_SUFFIX", "qoin"),
        },
    }


def _parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def use_gh_cli() -> bool:
    """Toggle: when false (default sejak v0.1.22) use native REST + git.

    Set GH_CLI=true in ~/.build-q/.env to fall back to `gh` CLI (legacy).
    """
    return load_config()["github"]["use_cli"]


def get_github_credentials() -> Dict[str, str]:
    """Return {'user','token'} from env / .env (empty strings if unset)."""
    cfg = load_config()["github"]
    return {"user": cfg["user"], "token": cfg["token"]}


def save_env_value(key: str, value: str) -> None:
    """Persist a single KEY=VALUE to ~/.build-q/.env (in-place update or append).

    Kalau `.env` belum ada, dibuat dulu via init_config(silent=True). Bila `key`
    sudah ada, baris di-update; kalau belum, di-append. Juga memutakhirkan
    os.environ agar run selanjutnya di proses ini langsung ambil nilai baru.
    """
    ensure_config_dir()
    if not ENV_FILE.exists():
        init_config(silent=True)
    lines = ENV_FILE.read_text().splitlines()
    found = False
    new_lines = []
    for line in lines:
        stripped = line.lstrip()
        if not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k == key:
                new_lines.append(f"{key}={value}")
                found = True
                continue
        new_lines.append(line)
    if not found:
        if new_lines and new_lines[-1] != "":
            new_lines.append("")
        new_lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(new_lines) + "\n")
    ENV_FILE.chmod(0o600)
    os.environ[key] = value


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

# ── PB API (centralized credentials, PocketBase IDP) ────────────────────
# Bila PB_API=true, credentials aplikasi (GITHUB_TOKEN, DOCKERHUB_TOKEN,
# WEBHOOK_HOOK_HMAC, dst) diambil dari API — tidak perlu diisi di file ini.
# Login: `bq auth login` (SSO GitHub via Dex) → token di ~/.build-q/auth.json,
# PB_API_USER/PASS (superuser) tidak perlu lagi — hanya fallback legacy.
# Cache secrets: ~/.build-q/.pb-cache.json (TTL default 15 menit).
PB_API=false
PB_API_URL=https://cicd-hw.qoin.id/devops
PB_API_USER=
PB_API_PASS=
# PB_API_CACHE_TTL=900

# Docker builder name
BUILDER_NAME=mybuilder

# Resource limits
DEFAULT_MEMORY=4g
DEFAULT_CPU_PERIOD=100000
DEFAULT_CPU_QUOTA=200000

# Container registry URL (Qoin default: loyaltolpi — Docker Hub org)
REGISTRY_URL=loyaltolpi

# Git remote settings (for --remote / --clone)
GIT_SSH_PREFIX=git@github.com:

# Default GitHub organization — when set, shorthand works: `bq <repo> <ref> --remote`
# Example: GITHUB_ORG=Qoin-Digital-Indonesia
GITHUB_ORG=

# Jenkins X webhook trigger — used by `bq --init-secrets` to configure GitHub Actions
WEBHOOK_TRIGGER_URL=https://cicd-hw.qoin.id/trigger
# Optional: kubectl context/namespace/secret for fetching the trigger token.
# Default JX_KUBE_CONTEXT=hw-dev (cluster Jenkins X Qoin). Kosongkan untuk pakai
# current kubectl context.
JX_KUBE_CONTEXT=hw-dev
JX_KUBE_NAMESPACE=jenkins-x
JX_TOKEN_SECRET=webhook-trigger-token
# ArgoCD (dibaca `bq --e2e` / `/e2e` di TUI): context & namespace tempat
# Application ArgoCD berada. Hanya akses baca (get applications).
ARGOCD_KUBE_CONTEXT=hw-pro-p
ARGOCD_NAMESPACE=argocd
# Fallback bila kubectl ke cluster ArgoCD gagal/timeout: REST API ArgoCD.
# ARGOCD_TOKEN kosong → dibaca dari ARGOCD_AUTH_FILE (key ARGOCD_TOKEN,
# diperbarui via `argocd-app token-login`).
ARGOCD_API_URL=https://cicd-hw.qoin.id/argocd
ARGOCD_TOKEN=
ARGOCD_AUTH_FILE=~/.pi/agent/auth.json
# Token kedaluwarsa (401) → otomatis `argocd-app token-login` (terminal interaktif saja)
ARGOCD_AUTO_LOGIN=true
# WEBHOOK_TRIGGER_TOKEN: auto-fetched dari k8s pada `bq --pr-fix` preflight
# (sekali saja; disimpan di sini untuk run selanjutnya).
WEBHOOK_TRIGGER_TOKEN=

# `bq --pr-fix` options
PR_FIX_BRANCH_PREFIX=fix/jx-init-
PR_FIX_BASE_WORKDIR=/tmp

# GitHub access
# GH_CLI=false (default sejak v0.1.22) → jalur native (urllib + git).
#                Wajib GITHUB_TOKEN di bawah, tidak butuh `gh` CLI.
# GH_CLI=true  → tetap pakai `gh` CLI (perilaku lama).
GH_CLI=false
# Personal Access Token (classic atau fine-grained). WAJIB bila GH_CLI=false.
# Scope minimum:
#   - repo             (contents:read/write, PR)
#   - workflow         (WAJIB untuk `bq --pr-fix` — update .github/workflows/*)
#   - read:user
# Untuk --init-secrets: actions:write (biasanya include di scope repo).
# Generate: https://github.com/settings/tokens
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
