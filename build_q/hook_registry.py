"""Registry webhook di PocketBase — collection `repo` & `github_hook`.

Dipakai `bq --cicd-webhook`: repo yang belum terdaftar di collection `repo`
dibuat (image/deployment dari cicd/cicd.json), lalu record `github_hook`
di-upsert dengan data hook GitHub + status HMAC hasil verifikasi ping
(`hmac_status`, `hmac_checked_at`, `hmac_detail`).

Soft-fail: error PB hanya di-print, tidak membatalkan alur webhook.
"""
from __future__ import annotations

import re
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Iterable, Optional, Tuple

from . import github_api, pb_api
from ._common import fetch_cicd_data
from .config import DEFAULT_CICD_PATH

REPO_COLLECTION = "repo"
HOOK_COLLECTION = "github_hook"
# Nilai cicd_webhook_status selain URL hook (konvensi data yang sudah ada).
STATUS_NO_WEBHOOK = "NO_WEBHOOK"
STATUS_ACCESS_DENIED = "ACCESS_DENIED"

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
# cicd.json sering hanya ada di branch build (default branch main belum di-merge).
CICD_REFS = ("develop", "staging", "sandbox")


def _pb(method: str, path: str, body: Optional[dict] = None) -> dict:
    base = pb_api._base_url()
    return pb_api._http_json(
        method, f"{base}{path}", body=body,
        headers={"Authorization": f"Bearer {pb_api.get_token(base)}"},
    )


def _find_one(collection: str, filter_: str) -> Optional[dict]:
    q = urllib.parse.quote(filter_)
    resp = _pb("GET", f"/api/collections/{collection}/records?perPage=1&filter={q}")
    items = resp.get("items") or []
    return items[0] if items else None


def _default_branch(api_repo: str) -> Optional[str]:
    """Default branch repo GitHub; None bila repo tidak ada / tak bisa diakses."""
    try:
        return (github_api._request("GET", f"/repos/{api_repo}") or {}).get("default_branch")
    except github_api.GitHubAPIError:
        return None


def _fetch_cicd(api_repo: str, default_branch: str) -> Tuple[dict, Optional[str]]:
    """cicd.json dari default branch, lalu branch build. Return (cicd, ref) / ({}, None)."""
    for ref in dict.fromkeys((default_branch, *CICD_REFS)):
        cicd, _, _ = fetch_cicd_data(api_repo, ref, DEFAULT_CICD_PATH)
        if cicd:
            return cicd, ref
    return {}, None


def ensure_repo(api_repo: str, webhook_status: str, hook_url: str = "") -> Optional[dict]:
    """Pastikan repo terdaftar di collection `repo`; buat bila belum ada.

    `webhook_status` = URL hook bila terpasang, atau STATUS_NO_WEBHOOK /
    STATUS_ACCESS_DENIED. Record yang sudah ada hanya diselaraskan field
    webhook-nya (+ `cicd` bila masih kosong). Return record, atau None bila
    PB nonaktif / gagal.
    """
    if not pb_api.is_enabled():
        return None
    name = api_repo.split("/")[-1]
    if not _NAME_RE.match(name):
        print(f"   ⚠️  Nama repo tidak valid untuk PB: {name!r}", file=sys.stderr)
        return None
    installed = bool(hook_url) and webhook_status == hook_url
    state = {
        "incoming_webhook": installed,
        "webhook_url": hook_url if installed else "",
        "cicd_webhook_status": webhook_status,
    }
    try:
        rec = _find_one(REPO_COLLECTION, f"name='{name}'")
        if rec is None:
            branch = _default_branch(api_repo)
            if branch is None:
                print(f"   ⚠️  Repo {api_repo} tidak bisa diakses di GitHub — "
                      f"tidak didaftarkan ke PB {REPO_COLLECTION}.", file=sys.stderr)
                return None
            cicd, ref = _fetch_cicd(api_repo, branch)
            rec = _pb("POST", f"/api/collections/{REPO_COLLECTION}/records", {
                "name": name,
                "image": cicd.get("IMAGE") or name,
                "deployment": cicd.get("DEPLOYMENT") or name,
                "project": name,
                "cicd": cicd or None,
                **state,
            })
            src = f"cicd.json@{ref}" if cicd else "default nama repo"
            print(f"   🆕 Repo {name} didaftarkan ke PB {REPO_COLLECTION} "
                  f"(id={rec.get('id')}, data dari {src}).")
            return rec
        changed = {k: v for k, v in state.items() if rec.get(k) != v}
        if not rec.get("cicd"):
            branch = _default_branch(api_repo)
            cicd, _ = _fetch_cicd(api_repo, branch) if branch else ({}, None)
            if cicd:
                changed["cicd"] = cicd
        if changed:
            rec = _pb("PATCH", f"/api/collections/{REPO_COLLECTION}/records/{rec['id']}", changed)
        return rec
    except pb_api.PBAPIError as e:
        print(f"   ⚠️  PB {REPO_COLLECTION} gagal di-update: {e}", file=sys.stderr)
        return None


def _pb_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.000Z")


def upsert_hook(
    repo_rec: dict,
    hook: dict,
    hmac_status: str,
    detail: str,
    removed_hook_ids: Iterable[int] = (),
) -> Optional[dict]:
    """Upsert record `github_hook` untuk `hook` + bersihkan record hook yang dihapus.

    Record hook yang sudah dihapus dari GitHub di-delete; bila rule PB tidak
    mengizinkan delete, record ditandai active=false.
    """
    cfg = hook.get("config") or {}
    lr = hook.get("last_response") or {}
    body = {
        "repo": repo_rec["id"],
        "hook_id": hook["id"],
        "url": cfg.get("url"),
        "events": hook.get("events"),
        "active": hook.get("active"),
        "content_type": cfg.get("content_type"),
        "insecure_ssl": cfg.get("insecure_ssl"),
        "gh_created_at": hook.get("created_at"),
        "gh_updated_at": hook.get("updated_at"),
        "last_response_code": lr.get("code") or 0,
        "last_response_status": lr.get("status"),
        "last_response_message": lr.get("message"),
        "ping_url": hook.get("ping_url"),
        "deliveries_url": hook.get("deliveries_url"),
        "test_url": hook.get("test_url"),
        "hmac_status": hmac_status,
        "hmac_checked_at": _pb_now(),
        "hmac_detail": detail,
    }
    path = f"/api/collections/{HOOK_COLLECTION}/records"
    try:
        for hid in removed_hook_ids:
            old = _find_one(HOOK_COLLECTION, f"hook_id={int(hid)}")
            if old is None:
                continue
            try:
                _pb("DELETE", f"{path}/{old['id']}")
            except pb_api.PBAPIError:
                _pb("PATCH", f"{path}/{old['id']}", {
                    "active": False, "hmac_status": "unknown", "hmac_checked_at": _pb_now(),
                    "hmac_detail": "hook dihapus dari GitHub (duplikat)"})
        rec = _find_one(HOOK_COLLECTION, f"hook_id={int(hook['id'])}")
        if rec:
            rec = _pb("PATCH", f"{path}/{rec['id']}", body)
        else:
            rec = _pb("POST", path, body)
        print(f"   🗂  PB {HOOK_COLLECTION}: hook id={hook['id']} hmac_status={hmac_status}")
        return rec
    except pb_api.PBAPIError as e:
        print(f"   ⚠️  PB {HOOK_COLLECTION} gagal di-update: {e}", file=sys.stderr)
        return None
