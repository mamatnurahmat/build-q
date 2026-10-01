"""PB API (PocketBase-based IDP) — centralized credentials for build-q.

Fetches secrets from a PocketBase-backed IDP (default:
https://cicd-hw.qoin.id/devops) and hydrates them into `os.environ` before
`load_config()` reads env vars. Enables the "no per-laptop .env" workflow:
each engineer only needs PB_API_URL/USER/PASS locally; all app credentials
(GITHUB_TOKEN, DOCKERHUB_TOKEN, WEBHOOK_HOOK_HMAC, ...) come from the API.

Toggle via `PB_API=true` in ~/.build-q/.env. Resilient: if the API is
unreachable, a warning is printed and control falls back to whatever is
already in the local .env (never aborts).

Kontrak API (PocketBase v0.22+):
    POST /api/collections/_superusers/auth-with-password
         {"identity": "<email>", "password": "<pass>"}
      -> {"token": "<jwt>", "record": {...}}

    GET  /api/collections/secrets/records?perPage=200&filter=(active=true)
      Authorization: Bearer <jwt>
      -> {"items": [{"key": "...", "value": "...", "active": true, ...}], ...}
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Optional


CONFIG_DIR = Path.home() / ".build-q"
CACHE_FILE = CONFIG_DIR / ".pb-cache.json"
DEFAULT_TTL = 900  # 15 menit
DEFAULT_URL = "https://cicd-hw.qoin.id/devops"

# Env keys yang mengontrol PB API itu sendiri — TIDAK boleh di-hydrate dari
# PB (chicken-and-egg). Selalu dibaca dari .env / shell env.
_BOOTSTRAP_KEYS = {"PB_API", "PB_API_URL", "PB_API_USER", "PB_API_PASS", "PB_API_CACHE_TTL"}


class PBAPIError(Exception):
    """Raised when PB API auth or fetch fails hard (network, 4xx, 5xx)."""


def _parse_bool(value: Optional[str]) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    return _parse_bool(os.getenv("PB_API"))


def _base_url() -> str:
    return (os.getenv("PB_API_URL") or DEFAULT_URL).rstrip("/")


def _ttl_seconds() -> int:
    raw = os.getenv("PB_API_CACHE_TTL")
    if not raw:
        return DEFAULT_TTL
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_TTL


def _http_json(
    method: str, url: str, *, body: Optional[dict] = None,
    headers: Optional[dict] = None, timeout: int = 10,
) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = {"Accept": "application/json"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", errors="replace"))
            msg = detail.get("message") or str(detail)
        except Exception:
            msg = e.reason
        raise PBAPIError(f"HTTP {e.code}: {msg}") from e
    except urllib.error.URLError as e:
        raise PBAPIError(f"network: {e.reason}") from e
    if not payload:
        return {}
    return json.loads(payload.decode("utf-8"))


def authenticate(base_url: str, user: str, password: str) -> str:
    """Return a JWT token for the `_superusers` collection."""
    url = f"{base_url}/api/collections/_superusers/auth-with-password"
    resp = _http_json("POST", url, body={"identity": user, "password": password})
    token = resp.get("token")
    if not token:
        raise PBAPIError("auth response missing 'token'")
    return token


def fetch_secrets(base_url: str, token: str) -> Dict[str, str]:
    """Fetch all active secrets from the `secrets` collection.

    Returns {KEY: VALUE, ...}. Inactive records are filtered server-side.
    """
    filter_ = urllib.parse.quote("active=true", safe="=")
    url = (f"{base_url}/api/collections/secrets/records"
           f"?perPage=500&filter={filter_}")
    resp = _http_json("GET", url, headers={"Authorization": f"Bearer {token}"})
    out: Dict[str, str] = {}
    for item in resp.get("items", []):
        key = (item.get("key") or "").strip()
        value = item.get("value")
        if not key or value is None:
            continue
        out[key] = str(value)
    return out


# ── Cache ────────────────────────────────────────────────────────────────

def _read_cache() -> Optional[dict]:
    if not CACHE_FILE.exists():
        return None
    try:
        return json.loads(CACHE_FILE.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(secrets: Dict[str, str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_at": int(time.time()), "secrets": secrets}
    CACHE_FILE.write_text(json.dumps(payload))
    try:
        CACHE_FILE.chmod(0o600)
    except OSError:
        return


def _cache_fresh(cache: dict, ttl: int) -> bool:
    if ttl <= 0:
        return False
    fetched = cache.get("fetched_at", 0)
    return (time.time() - fetched) < ttl


def clear_cache() -> bool:
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
        return True
    return False


def fetch_collection_records(
    collection: str,
    filter_: str = "",
    per_page: int = 100,
) -> list:
    """Fetch records from an arbitrary PocketBase collection.

    Args:
        collection: Collection name (e.g. "repo_config", "secrets")
        filter_: Optional PB filter expression (e.g. "active=true")
        per_page: Max records per page (default 100, max 500)

    Returns:
        List of record dicts from the collection.

    Raises:
        PBAPIError: If auth fails or API returns error
    """
    base = _base_url()
    user = os.getenv("PB_API_USER") or ""
    password = os.getenv("PB_API_PASS") or ""
    if not user or not password:
        raise PBAPIError("PB_API_USER / PB_API_PASS empty")
    token = authenticate(base, user, password)
    url = f"{base}/api/collections/{collection}/records?perPage={min(per_page, 500)}"
    if filter_:
        url += f"&filter={urllib.parse.quote(filter_, safe='=')}"
    resp = _http_json("GET", url, headers={"Authorization": f"Bearer {token}"})
    return resp.get("items", [])


def cache_age_seconds() -> Optional[int]:
    cache = _read_cache()
    if not cache:
        return None
    return int(time.time() - cache.get("fetched_at", 0))


# ── Public entry points ─────────────────────────────────────────────────

def pull(*, force: bool = False) -> Dict[str, str]:
    """Fetch secrets (respecting cache unless `force=True`). Raises PBAPIError."""
    ttl = _ttl_seconds()
    if not force:
        cache = _read_cache()
        if cache and _cache_fresh(cache, ttl):
            return cache.get("secrets", {})
    base = _base_url()
    user = os.getenv("PB_API_USER") or ""
    password = os.getenv("PB_API_PASS") or ""
    if not user or not password:
        raise PBAPIError("PB_API_USER / PB_API_PASS empty — set di ~/.build-q/.env")
    token = authenticate(base, user, password)
    secrets = fetch_secrets(base, token)
    _write_cache(secrets)
    return secrets


def hydrate_env(*, quiet: bool = True) -> int:
    """Overlay PB secrets on os.environ. Return #keys set.

    Prioritas: shell env (yang sudah diset sebelum kita jalan) tetap menang —
    kita hanya set key yang belum ada di os.environ. Bootstrap keys
    (PB_API_*) tidak pernah di-overlay.

    Resilient: bila API gagal, print warning ke stderr dan return 0
    (fallback ke .env / default). Tidak pernah raise.
    """
    try:
        secrets = pull()
    except PBAPIError as e:
        print(f"⚠️  PB API unavailable ({e}) — fallback ke .env lokal", file=sys.stderr)
        return 0

    count = 0
    for k, v in secrets.items():
        if k in _BOOTSTRAP_KEYS:
            continue
        if k in os.environ and os.environ[k]:
            continue  # shell env menang
        os.environ[k] = v
        count += 1

    if not quiet:
        print(f"🔐 PB API: {count} env vars hydrated from {_base_url()}")
    return count
