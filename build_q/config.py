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
    }


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
"""
    ENV_FILE.write_text(default)
    ENV_FILE.chmod(0o600)
    if not silent:
        print(f"✅ Config created: {ENV_FILE}")
        print("   Edit the file to set your registry and builder settings.")


def load_local_cicd(cicd_path: str = "cicd/cicd.json") -> Dict[str, Any]:
    """Load cicd.json from local filesystem.

    Args:
        cicd_path: Relative or absolute path to cicd.json

    Returns:
        Parsed JSON as dict

    Raises:
        FileNotFoundError: If file not found
        json.JSONDecodeError: If invalid JSON
    """
    path = Path(cicd_path)
    if not path.exists():
        raise FileNotFoundError(f"CICD config not found: {cicd_path}")

    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {cicd_path}: {e}")
