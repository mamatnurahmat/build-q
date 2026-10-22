"""Knowledge kube context Huawei CCE (hw-*) ↔ namespace — terpusat di PocketBase.

Sumber utama: PocketBase devops, collection
  - `kube_cx`   — 1 record / context (name, cluster, server, environment, note)
  - `namespace` — 1 record / (namespace, context) (name, description,
                  kube_cx → relation ke `kube_cx`)

Satu namespace boleh ada di >1 context (mis. develop-payout) → dianggap
ambigu dan Jev memilih di antara kandidatnya saja.

Caching: `~/.build-q/.kube-cx-cache.json` (TTL sama dgn katalog TUI).
Fallback bila PB unreachable + cache kosong: `_BOOTSTRAP` di bawah (snapshot
2026-10-21, nodepool `~/gitops/cce/<ns>/*_deployment.yaml`).

Dipakai Jev planner (`tui.py`) untuk mengisi param `kube_context` dari
namespace, dan sebagai knowledge saat Jev harus memilih context terdekat.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Dict, List, Optional, Tuple

from . import pb_api
from .config import CONFIG_DIR
from .tui_catalog import _ttl_seconds


CACHE_FILE = CONFIG_DIR / ".kube-cx-cache.json"
_CACHE_VERSION = 3  # v2: + server (destination ArgoCD) · v3: + nodepool per namespace & cluster
CX_COLLECTION = "kube_cx"
NS_COLLECTION = "namespace"
NODEPOOL_COLLECTION = "nodepool"
NODEPOOL_LABEL = "cce.cloud.com/cce-nodepool"

# Fallback minimum — HANYA dipakai saat PB down + cache kosong.
_BOOTSTRAP: Dict[str, dict] = {
    "hw-dev": {
        "server": "https://10.2.20.121:5443",
        "environment": "development",
        "note": "Huawei CCE develop/sandbox + Jenkins X. Nodepool develop-qoin-*, develop-qoincash-*, sandbox-qoin-*",
        "namespaces": [
            "default", "develop-qoin", "develop-payout", "develop-qoinhub", "develop-saas",
            "develop-ngenwal", "sandbox-qoin", "jenkins-x",
        ],
        "nodepools": ["develop-qoin-manager", "develop-qoin-service", "develop-qoincash-manager",
                      "develop-qoincash-service", "sandbox-qoin-manager", "sandbox-qoin-service"],
        "nodepool": {
            **{ns: {"front": "develop-qoin-manager", "back": "develop-qoin-service"}
               for ns in ("develop-qoin", "develop-payout", "develop-qoinhub", "develop-saas")},
            "develop-ngenwal": {"front": "develop-qoincash-manager", "back": "develop-qoincash-service"},
            "sandbox-qoin": {"front": "sandbox-qoin-manager", "back": "sandbox-qoin-service"},
        },
    },
    "hw-pro-p": {
        "server": "https://10.7.20.103:5443",
        "environment": "production",
        "note": "Huawei CCE production payout + ArgoCD. Nodepool production-payout-nodepool-*",
        "namespaces": ["production-payout", "argocd"],
        "nodepools": ["production-payout-nodepool-manager", "production-payout-nodepool-service"],
        # `nodetype=front` di hw-pro-p juga menempel di pool monitoring/workflow → pakai nama pool presisi.
        "nodepool": {"production-payout": {"front": "production-payout-nodepool-manager",
                                           "back": "production-payout-nodepool-service"}},
    },
    "hw-pro-q": {
        "server": "https://10.5.20.23:5443",
        "environment": "production",
        "note": "Huawei CCE production Qoin (AP/QA) + staging Qoin. Nodepool production-nodepool-*",
        "namespaces": ["production-qoin", "staging-qoin"],
        "nodepools": ["production-nodepool-manager", "production-nodepool-service"],
        "nodepool": {ns: {"front": "production-nodepool-manager", "back": "production-nodepool-service"}
                     for ns in ("production-qoin", "staging-qoin")},
    },
    "hw-pro-u": {
        "server": "https://10.6.20.45:5443",
        "environment": "production",
        "note": "Huawei CCE production UE region (ngenwal, saas) + staging ngenwal. Nodepool production-ue-nodepool-*",
        "namespaces": ["production-ngenwal", "production-saas", "staging-ngenwal"],
        "nodepools": ["production-ue-nodepool-manager", "production-ue-nodepool-service"],
        "nodepool": {ns: {"front": "production-ue-nodepool-manager", "back": "production-ue-nodepool-service"}
                     for ns in ("production-ngenwal", "production-saas", "staging-ngenwal")},
    },
}

# Fallback prefix bila namespace belum terdaftar — hanya yang aman ditebak.
# production-* lain sengaja TIDAK ditebak: biar Jev / user yang memilih.
_PREFIX_FALLBACK: List[Tuple[str, str]] = [
    ("develop-", "hw-dev"),
    ("sandbox-", "hw-dev"),
    ("staging-", "hw-pro-q"),
]

_loaded: Optional[Tuple[Dict[str, dict], str]] = None


# ─── Load (PB → cache → bootstrap) ────────────────────────────────────────

def _fetch_from_pb() -> Dict[str, dict]:
    cxs = pb_api.fetch_collection_records(CX_COLLECTION, per_page=500)
    nss = pb_api.fetch_collection_records(NS_COLLECTION, per_page=500)
    by_id = {r["id"]: r for r in cxs if r.get("name")}
    out: Dict[str, dict] = {
        r["name"]: {
            "cluster": r.get("cluster", ""),
            "server": r.get("server", ""),
            "environment": r.get("environment", ""),
            "note": r.get("note", ""),
            "nodepools": r.get("nodepools") if isinstance(r.get("nodepools"), list) else [],
            "namespaces": [],
            "nodepool": {},
        }
        for r in by_id.values()
    }
    for r in nss:
        cx = by_id.get(r.get("kube_cx", ""))
        if cx and r.get("name"):
            out[cx["name"]]["namespaces"].append(r["name"])
    try:
        pools = pb_api.fetch_collection_records(NODEPOOL_COLLECTION, per_page=500)
    except Exception:  # collection belum ada / akses ditolak → generator pakai params rule
        pools = []
    for r in pools:
        cx = by_id.get(r.get("kube_cx", ""))
        if not (cx and r.get("namespace") and r.get("role") and r.get("value")):
            continue
        entry = out[cx["name"]]["nodepool"].setdefault(r["namespace"], {})
        entry[r["role"]] = r["value"]
        if r.get("label") and r["label"] != NODEPOOL_LABEL:
            entry["label"] = r["label"]
    return out


def _read_cache() -> Optional[dict]:
    try:
        return json.loads(CACHE_FILE.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(data: Dict[str, dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(
        {"v": _CACHE_VERSION, "fetched_at": int(time.time()), "contexts": data}, indent=2))
    try:
        CACHE_FILE.chmod(0o600)
    except OSError:
        pass


def load(*, force_refresh: bool = False) -> Tuple[Dict[str, dict], str]:
    """Return ({context: {cluster, environment, note, namespaces}}, source).

    source: pb | cache | stale-cache | bootstrap.
    """
    global _loaded
    if _loaded and not force_refresh:
        return _loaded
    cache = _read_cache()
    ttl = _ttl_seconds()
    if (cache and not force_refresh and ttl > 0
            and cache.get("v") == _CACHE_VERSION
            and time.time() - cache.get("fetched_at", 0) < ttl):
        _loaded = (cache.get("contexts", {}), "cache")
        return _loaded
    try:
        data = _fetch_from_pb()
        if not data:
            raise pb_api.PBAPIError(f"collection {CX_COLLECTION} kosong")
        _write_cache(data)
        _loaded = (data, "pb")
    except Exception as e:  # PB down / auth gagal → jangan matikan planner
        if cache and cache.get("contexts"):
            print(f"⚠️  kube_cx: {e} — pakai cache lama", file=sys.stderr)
            _loaded = (cache["contexts"], "stale-cache")
        else:
            print(f"⚠️  kube_cx: {e} — pakai mapping bootstrap", file=sys.stderr)
            _loaded = (_BOOTSTRAP, "bootstrap")
    return _loaded


def contexts() -> Dict[str, dict]:
    return load()[0]


# ─── Query ────────────────────────────────────────────────────────────────

def namespace_map() -> Dict[str, List[str]]:
    """{namespace: [context, ...]} — >1 context berarti ambigu."""
    out: Dict[str, List[str]] = {}
    for ctx, meta in contexts().items():
        for ns in meta.get("namespaces", []):
            out.setdefault(ns, []).append(ctx)
    return out


def candidates(namespace: str) -> List[str]:
    """Context kandidat untuk namespace (exact match, lalu prefix terdekat)."""
    ns = (namespace or "").strip()
    if not ns or ns.startswith("<"):
        return []
    mapping = namespace_map()
    if ns in mapping:
        return mapping[ns]
    known = contexts()
    for prefix, ctx in _PREFIX_FALLBACK:
        if ns.startswith(prefix) and ctx in known:
            return [ctx]
    return []


def resolve(namespace: str) -> Tuple[Optional[str], str]:
    """Return (context, alasan). context None bila kosong / ambigu / tak dikenal."""
    ns = (namespace or "").strip()
    found = candidates(ns)
    if len(found) == 1:
        how = "mapping" if ns in namespace_map() else "terdekat (prefix)"
        return found[0], f"{how} {ns} → {found[0]}"
    if found:
        return None, f"namespace {ns} ambigu: {', '.join(found)}"
    if not ns or ns.startswith("<"):
        return None, "namespace kosong"
    return None, f"namespace {ns} belum ada di mapping"


_ENV_CLUSTER = {"develop": "development", "sandbox": "development"}  # lainnya → production


def node_selector_for(namespace: str, role: str, ctx: Optional[str] = None) -> Tuple[str, str]:
    """(label, nilai) nodeSelector standar namespace di cluster; ("", "") bila tak ada data."""
    pool = nodepool_for(namespace, role, ctx)
    if not pool:
        return "", ""
    data = contexts()
    for c in ([ctx] if ctx else list(data)):
        entry = ((data.get(c) or {}).get("nodepool") or {}).get(namespace, {})
        if entry.get(role) == pool:
            return entry.get("label") or NODEPOOL_LABEL, pool
    return NODEPOOL_LABEL, pool


def nodepool_for(namespace: str, role: str, ctx: Optional[str] = None) -> str:
    """Nodepool CCE (`front` = manager/apigateway, `back` = module/…) untuk namespace di cluster.

    Tanpa `ctx`: cluster dari mapping; namespace ambigu (mis. develop-payout di hw-dev & hw-pro-p)
    → cluster yang environment-nya cocok dgn prefix namespace. "" bila tidak ada data.
    """
    data = contexts()
    if ctx:
        candidates = [ctx]
    else:
        candidates = [c for c, meta in data.items() if namespace in (meta.get("nodepool") or {})]
        if len(candidates) > 1:
            want = _ENV_CLUSTER.get(namespace.split("-", 1)[0], "production")
            candidates.sort(key=lambda c: data[c].get("environment") != want)
    for c in candidates:
        pool = ((data.get(c) or {}).get("nodepool") or {}).get(namespace, {}).get(role, "")
        if pool:
            return pool
    return ""


IN_CLUSTER = "https://kubernetes.default.svc"


def context_for_destination(dest: dict, argocd_context: str = "") -> Optional[str]:
    """Context dari `spec.destination` Application ArgoCD (server/name ↔ kube_cx).

    `in-cluster` = cluster tempat ArgoCD berjalan (`argocd_context`).
    """
    server = (dest.get("server") or "").rstrip("/")
    name = dest.get("name") or ""
    if server == IN_CLUSTER or name == "in-cluster":
        return argocd_context or None
    for ctx, meta in contexts().items():
        if server and (meta.get("server") or "").rstrip("/") == server:
            return ctx
        if name and name in (ctx, meta.get("cluster")):
            return ctx
    return None


def knowledge() -> Dict[str, dict]:
    """Ringkasan untuk state Jev / MCP resource: {context: {...}}."""
    return {
        ctx: {
            "environment": meta.get("environment", ""),
            "note": meta.get("note", ""),
            "namespaces": sorted(meta.get("namespaces", [])),
        }
        for ctx, meta in contexts().items()
    }


def jev_criteria(only: Optional[List[str]] = None) -> Dict[str, str]:
    """Criteria choice Jev: satu entri per context berisi namespace-nya."""
    return {
        ctx: f"[{info['environment']}] {info['note']}. Namespace: {', '.join(info['namespaces'])}."
        for ctx, info in knowledge().items()
        if not only or ctx in only
    }
