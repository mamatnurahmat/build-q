"""TUI catalog loader — PocketBase-backed (3 collection `build_q_*`).

Collection layout di PocketBase devops:
  - `build_q_tools`     — 1 record / tool  (name, category, description,
                          template, params (json), risky, active, order)
  - `build_q_providers` — 1 record / provider (name, url, model, key_env,
                          is_default, active)
  - `build_q_patterns`  — 1 record / regex pattern (field, pattern, order)

Caching: `~/.build-q/.tui-cache.json` (TTL default 1 jam).

Fallback bila PB unreachable + cache kosong: minimal bootstrap (1 provider
default + 1 tool `bq_doctor`) agar user tetap bisa run `--tui` dan
mem-populate PB.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import pb_api
from .config import CONFIG_DIR


CACHE_FILE = CONFIG_DIR / ".tui-cache.json"
DEFAULT_TTL = 3600  # 1 jam — katalog jarang berubah
COLLECTIONS = ("build_q_tools", "build_q_providers", "build_q_patterns")


# ─── Bootstrap fallback — HANYA dipakai saat PB down + cache kosong ──────
# Minimum viable: 1 provider default + 1 read-only tool agar `bq --tui`
# tetap bisa jalan dan user bisa menjalankan `bq --tui-pull` untuk sync.
_BOOTSTRAP_PROVIDERS = [
    {
        "name": "typesafe",
        "url": "https://api.typesafe.ai/v1/systemone",
        "model": "jev-latest",
        "key_env": "TYPESAFE_API_KEY",
        "is_default": True,
        "active": True,
    },
]
_BOOTSTRAP_TOOLS = [
    {
        "name": "bq_doctor",
        "category": "preflight",
        "description": "Preflight check tools & credentials.",
        "template": "bq --doctor",
        "params": {},
        "risky": False,
        "active": True,
        "order": 0,
    },
]
_BOOTSTRAP_PATTERNS: List[dict] = []


class CatalogError(RuntimeError):
    """Raised on unrecoverable catalog load error."""


# ─── Cache ───────────────────────────────────────────────────────────────

def _ttl_seconds() -> int:
    raw = os.getenv("BUILD_Q_TUI_CACHE_TTL")
    if not raw:
        return DEFAULT_TTL
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_TTL


def _read_cache() -> Optional[dict]:
    if not CACHE_FILE.exists():
        return None
    try:
        return json.loads(CACHE_FILE.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(data: Dict[str, List[dict]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_at": int(time.time()), "collections": data}
    CACHE_FILE.write_text(json.dumps(payload, indent=2))
    try:
        CACHE_FILE.chmod(0o600)
    except OSError:
        pass


def _cache_fresh(cache: dict, ttl: int) -> bool:
    if ttl <= 0:
        return False
    return (time.time() - cache.get("fetched_at", 0)) < ttl


def cache_age_seconds() -> Optional[int]:
    cache = _read_cache()
    if not cache:
        return None
    return int(time.time() - cache.get("fetched_at", 0))


def clear_cache() -> bool:
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
        return True
    return False


# ─── Fetch from PB ────────────────────────────────────────────────────────

def _fetch_all_from_pb() -> Dict[str, List[dict]]:
    """Fetch ketiga collection dari PB. Raise CatalogError on fatal error."""
    out: Dict[str, List[dict]] = {}
    for coll in COLLECTIONS:
        try:
            records = pb_api.fetch_collection_records(
                coll, filter_="active=true", per_page=500,
            )
        except pb_api.PBAPIError as e:
            raise CatalogError(f"PB fetch {coll} gagal: {e}") from e
        out[coll] = records
    return out


# ─── Public API ───────────────────────────────────────────────────────────

def load_raw(*, force_refresh: bool = False, allow_fallback: bool = True) -> Dict[str, List[dict]]:
    """Return {collection_name: [records]}. Pakai cache bila fresh.

    Fallback urutan:
      1. Cache fresh → pakai cache
      2. PB reachable → fetch + overwrite cache
      3. PB down + cache stale → pakai cache stale (warning)
      4. PB down + no cache + allow_fallback → bootstrap minimum
      5. Else → raise CatalogError
    """
    ttl = _ttl_seconds()
    cache = _read_cache()
    if cache and not force_refresh and _cache_fresh(cache, ttl):
        return cache.get("collections", {})

    try:
        data = _fetch_all_from_pb()
        _write_cache(data)
        return data
    except CatalogError as e:
        if cache:
            print(
                f"⚠️  TUI catalog: {e} — pakai cache lama "
                f"(umur {cache_age_seconds()}s)",
                file=sys.stderr,
            )
            return cache.get("collections", {})
        if allow_fallback:
            print(
                f"⚠️  TUI catalog: {e} — pakai bootstrap minimum "
                f"(jalankan `bq --tui-pull` setelah PB online)",
                file=sys.stderr,
            )
            return {
                "build_q_tools":     _BOOTSTRAP_TOOLS,
                "build_q_providers": _BOOTSTRAP_PROVIDERS,
                "build_q_patterns":  _BOOTSTRAP_PATTERNS,
            }
        raise


def _records_to_tools(records: List[dict]) -> Dict[str, dict]:
    """Convert PB records → TOOLS dict (format legacy di tui.py)."""
    out: Dict[str, dict] = {}
    for r in sorted(records, key=lambda x: x.get("order") or 0):
        name = r.get("name")
        if not name:
            continue
        out[name] = {
            "desc":     r.get("description", ""),
            "template": r.get("template", ""),
            "params":   r.get("params") or {},
            "_risky":   bool(r.get("risky", False)),
            "_category": r.get("category", ""),
        }
    return out


def _records_to_providers(records: List[dict]) -> Dict[str, Any]:
    """Return {name: Provider-like dict} + hint default provider."""
    out: Dict[str, Any] = {}
    for r in records:
        name = r.get("name")
        if not name:
            continue
        out[name] = {
            "name":       name,
            "url":        r.get("url", ""),
            "model":      r.get("model", ""),
            "key_env":    r.get("key_env", ""),
            "is_default": bool(r.get("is_default", False)),
        }
    return out


def _records_to_patterns(records: List[dict]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for r in sorted(records, key=lambda x: x.get("order") or 0):
        field = r.get("field")
        pat = r.get("pattern")
        if field and pat:
            out[field] = pat
    return out


def load_catalog(*, force_refresh: bool = False) -> Dict[str, Any]:
    """High-level loader. Return:

        {
          "tools":            {name: {desc, template, params, _risky, _category}},
          "providers":        {name: {name, url, model, key_env, is_default}},
          "patterns":         {field: regex},
          "risky":            set(tool_names),
          "default_provider": "typesafe"|...,
          "source":           "pb"|"cache"|"bootstrap",
          "fetched_at":       epoch_int,
        }
    """
    raw = load_raw(force_refresh=force_refresh)
    tools = _records_to_tools(raw.get("build_q_tools", []))
    providers = _records_to_providers(raw.get("build_q_providers", []))
    patterns = _records_to_patterns(raw.get("build_q_patterns", []))
    risky = {name for name, meta in tools.items() if meta.get("_risky")}

    default_provider = next(
        (p["name"] for p in providers.values() if p.get("is_default")),
        next(iter(providers), ""),
    )

    cache = _read_cache() or {}
    return {
        "tools": tools,
        "providers": providers,
        "patterns": patterns,
        "risky": risky,
        "default_provider": default_provider,
        "source": "pb" if cache else "bootstrap",
        "fetched_at": cache.get("fetched_at", 0),
    }


# ─── Admin: push local → PB (seed/migrate) ────────────────────────────────

def push_from_seed(seed_file: Path) -> Dict[str, Dict[str, int]]:
    """Upload records from a seed JSON file to PB.

    Seed format:
        {
          "build_q_tools":     [ {...}, ... ],
          "build_q_providers": [ {...}, ... ],
          "build_q_patterns":  [ {...}, ... ],
        }

    Upsert strategy: lookup by unique key (name for tools/providers,
    field for patterns) → PATCH jika ada, POST jika belum.

    Return summary {collection: {"created": N, "updated": M, "failed": K}}.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    seed = json.loads(seed_file.read_text())
    base = pb_api._base_url()
    user = os.getenv("PB_API_USER") or ""
    password = os.getenv("PB_API_PASS") or ""
    if not user or not password:
        raise CatalogError("PB_API_USER / PB_API_PASS kosong — set di ~/.build-q/.env")
    token = pb_api.authenticate(base, user, password)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def http(method: str, path: str, body: Optional[dict] = None):
        url = f"{base}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {}

    unique_field = {
        "build_q_tools":     "name",
        "build_q_providers": "name",
        "build_q_patterns":  "field",
    }

    summary: Dict[str, Dict[str, int]] = {}
    for coll, records in seed.items():
        if coll not in unique_field:
            continue
        key = unique_field[coll]
        created = updated = failed = 0
        for rec in records:
            filter_ = urllib.parse.quote(f"{key}='{rec[key]}'", safe="=")
            s, resp = http("GET", f"/api/collections/{coll}/records?filter={filter_}")
            items = resp.get("items", []) if s == 200 else []
            if items:
                rid = items[0]["id"]
                s2, _ = http("PATCH", f"/api/collections/{coll}/records/{rid}", rec)
                if s2 in (200, 201):
                    updated += 1
                else:
                    failed += 1
            else:
                s2, _ = http("POST", f"/api/collections/{coll}/records", rec)
                if s2 in (200, 201):
                    created += 1
                else:
                    failed += 1
        summary[coll] = {"created": created, "updated": updated, "failed": failed}

    clear_cache()
    return summary
