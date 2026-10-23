"""PB API (PocketBase-based IDP) — centralized credentials for build-q.

Fetches secrets from a PocketBase-backed IDP (default:
https://cicd-hw.qoin.id/devops) and hydrates them into `os.environ` before
`load_config()` reads env vars. Enables the "no per-laptop .env" workflow:
each engineer only needs PB_API_URL/USER/PASS locally; all app credentials
(GITHUB_TOKEN, DOCKERHUB_TOKEN, WEBHOOK_HOOK_HMAC, ...) come from the API.

Toggle via `PB_API=true` in ~/.build-q/.env. Resilient: if the API is
unreachable, a warning is printed and control falls back to whatever is
already in the local .env (never aborts).

Auth (urutan, lihat `get_token`):
  1. Token user dari `bq auth login` (SSO GitHub via Dex → collection
     `devops_users`) yang disimpan di ~/.build-q/auth.json — di-refresh
     otomatis (auth-refresh) bila mendekati kedaluwarsa.
  2. Legacy: PB_API_USER/PB_API_PASS (`_superusers`) dari .env.

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
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Optional


CONFIG_DIR = Path.home() / ".build-q"
CACHE_FILE = CONFIG_DIR / ".pb-cache.json"
AUTH_FILE = CONFIG_DIR / "auth.json"
AUTH_COLLECTION = "devops_users"
REFRESH_BEFORE = 24 * 3600  # auth-refresh bila sisa umur token < 1 hari
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
    """PB_API=true, atau PB_API tidak diset tapi sudah `bq auth login`."""
    raw = os.getenv("PB_API")
    if raw is None or not raw.strip():
        return AUTH_FILE.exists()
    return _parse_bool(raw)


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


# ── Token store (~/.build-q/auth.json) ──────────────────────────────────

def jwt_exp(token: str) -> int:
    """Klaim `exp` JWT (tanpa verifikasi signature — server yang memverifikasi)."""
    import base64

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp") or 0)
    except (IndexError, ValueError, TypeError):
        return 0


def read_auth() -> Optional[dict]:
    if not AUTH_FILE.exists():
        return None
    try:
        data = json.loads(AUTH_FILE.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return None
    return data if data.get("token") else None


def save_auth(base_url: str, token: str, record: dict, *, method: str,
              collection: str = AUTH_COLLECTION) -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    exp = jwt_exp(token)
    data = {
        "pb_url": base_url.rstrip("/"),
        "collection": collection,
        "method": method,
        "token": token,
        "exp": exp,
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp)) if exp else "",
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "record": {k: record.get(k) for k in ("id", "email", "full_name", "role", "verified")},
    }
    write_auth(data)
    return data


def write_auth(data: dict) -> None:
    """Tulis ulang auth.json (perm 600, atomik).

    File sementara unik per penulisan (mkstemp, perm 600) — nama tetap
    `auth.tmp` membuat proses bq paralel saling menimpa lalu `os.replace`
    salah satunya gagal FileNotFoundError.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".auth.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, AUTH_FILE)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def clear_auth() -> bool:
    if AUTH_FILE.exists():
        AUTH_FILE.unlink()
        return True
    return False


def refresh_auth(auth: dict) -> dict:
    """POST auth-refresh → token baru (umur penuh). Raise PBAPIError bila ditolak."""
    base, col = auth["pb_url"], auth.get("collection") or AUTH_COLLECTION
    resp = _http_json("POST", f"{base}/api/collections/{col}/auth-refresh",
                      headers={"Authorization": f"Bearer {auth['token']}"})
    if not resp.get("token"):
        raise PBAPIError("auth-refresh response missing 'token'")
    return save_auth(base, resp["token"], resp.get("record") or auth.get("record") or {},
                     method=auth.get("method", "oidc"), collection=col)


def user_token(base_url: Optional[str] = None) -> Optional[str]:
    """Token valid dari auth.json untuk `base_url`, atau None.

    Refresh otomatis bila sisa umur < REFRESH_BEFORE. Token yang ditolak
    server (401/403) dianggap tidak valid → None (fallback / login ulang).
    """
    auth = read_auth()
    base = (base_url or _base_url()).rstrip("/")
    if not auth or auth.get("pb_url", "").rstrip("/") != base:
        return None
    remaining = (auth.get("exp") or jwt_exp(auth["token"])) - time.time()
    if remaining <= 0:
        return None
    if remaining < REFRESH_BEFORE:
        try:
            auth = refresh_auth(auth)
        except PBAPIError as e:
            if str(e).startswith(("HTTP 401", "HTTP 403")):
                return None
            # Jaringan bermasalah: token lama masih berlaku, pakai saja.
    return auth["token"]


def get_token(base_url: Optional[str] = None) -> str:
    """Token untuk PB API: user SSO (auth.json) → fallback superuser .env."""
    base = (base_url or _base_url()).rstrip("/")
    token = user_token(base)
    if token:
        return token
    user = os.getenv("PB_API_USER") or ""
    password = os.getenv("PB_API_PASS") or ""
    if user and password:
        return authenticate(base, user, password)
    if read_auth():
        raise PBAPIError("token PocketBase kedaluwarsa/tidak valid — jalankan `bq auth login`")
    raise PBAPIError("belum login PocketBase — jalankan `bq auth login`")


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
    token = get_token(base)
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
    secrets = fetch_secrets(base, get_token(base))
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
