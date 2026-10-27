"""Jejak langkah `bq --pr-fix` — disimpan ke PocketBase dan fallback lokal.

Dua collection (lihat `PR_FIX_COLLECTIONS` untuk schema lengkap):
  - `pr_fix_runs`  : satu record per eksekusi --pr-fix (status akhir, PR, penyebab)
  - `pr_fix_steps` : satu record per langkah (preflight, clone, render, push, PR, ...)

Prinsip:
  - Setiap langkah SELALU ditulis ke file lokal JSONL
    (`~/.build-q/pr-fix-runs/<run_id>.jsonl`) lebih dulu, sehingga jejak tidak hilang.
  - Penulisan ke PocketBase bersifat best-effort. Bila collection belum ada
    (404), user tidak punya izin (403), atau PB tidak aktif, sinkronisasi
    dimatikan untuk sisa run dan --pr-fix tetap jalan.
  - Tidak pernah melempar exception ke alur --pr-fix.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import pb_api

LOCAL_DIR = Path(os.getenv("BUILD_Q_HOME", str(Path.home() / ".build-q"))) / "pr-fix-runs"

RUNS_COLLECTION = "pr_fix_runs"
STEPS_COLLECTION = "pr_fix_steps"

RUN_STATUSES = ["running", "ok", "no_change", "dry_run", "failed"]
STEP_STATUSES = ["ok", "skip", "fail", "info"]

# Schema yang diharapkan di PocketBase. Dipakai untuk `bq --pr-fix-schema`
# (cetak JSON untuk dibuat superuser) dan sebagai dokumentasi.
# Sementara (prioritas fungsi berjalan): rules dikosongkan = hanya superuser yang
# bisa baca/tulis. Penulisan jejak memakai token superuser dari PB_API_USER/PASS.
PR_FIX_COLLECTIONS: Dict[str, Dict[str, Any]] = {
    RUNS_COLLECTION: {
        "name": RUNS_COLLECTION,
        "type": "base",
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
        "fields": [
            {"name": "run_id", "type": "text", "required": True},
            {"name": "repo", "type": "text", "required": True},
            {"name": "ref", "type": "text", "required": True},
            {"name": "status", "type": "select", "values": RUN_STATUSES, "maxSelect": 1, "required": True},
            {"name": "head_sha", "type": "text"},
            {"name": "branch", "type": "text"},
            {"name": "pr_url", "type": "url"},
            {"name": "cause", "type": "text"},
            {"name": "cause_source", "type": "text"},
            {"name": "cause_confidence", "type": "number"},
            {"name": "started_at", "type": "date"},
            {"name": "finished_at", "type": "date"},
        ],
        "indexes": ["CREATE UNIQUE INDEX idx_pr_fix_runs_run_id ON pr_fix_runs (run_id)"],
    },
    STEPS_COLLECTION: {
        "name": STEPS_COLLECTION,
        "type": "base",
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
        "fields": [
            {"name": "run_id", "type": "text", "required": True},
            {"name": "seq", "type": "number", "required": True},
            {"name": "step", "type": "text", "required": True},
            {"name": "status", "type": "select", "values": STEP_STATUSES, "maxSelect": 1, "required": True},
            {"name": "detail", "type": "json"},
            {"name": "at", "type": "date"},
        ],
        "indexes": ["CREATE INDEX idx_pr_fix_steps_run ON pr_fix_steps (run_id)"],
    },
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _superuser_token(base: str) -> Optional[str]:
    """Token superuser dari PB_API_USER/PB_API_PASS (.env). None bila tidak diset."""
    user = os.getenv("PB_API_USER") or ""
    password = os.getenv("PB_API_PASS") or ""
    if not (user and password):
        return None
    return pb_api.authenticate(base, user, password)


def write_token(base: str) -> str:
    """Token untuk jejak pr-fix: superuser bila tersedia, lainnya token SSO user."""
    return _superuser_token(base) or pb_api.get_token(base)


def ensure_collections(base: str, token: str) -> Dict[str, str]:
    """Buat collection pr-fix yang belum ada. Return {nama: 'created'|'exists'|error}."""
    import json
    import urllib.request

    base = base.rstrip("/")
    req = urllib.request.Request(
        f"{base}/api/collections?perPage=500", headers={"Authorization": token}
    )
    existing = {c["name"] for c in json.load(urllib.request.urlopen(req, timeout=20))["items"]}
    result: Dict[str, str] = {}
    for name, spec in PR_FIX_COLLECTIONS.items():
        if name in existing:
            result[name] = "exists"
            continue
        try:
            pb_api._http_json(
                "POST", f"{base}/api/collections", body=spec,
                headers={"Authorization": token}, timeout=20,
            )
            result[name] = "created"
        except Exception as e:
            result[name] = f"error: {type(e).__name__}: {e}"[:300]
    return result


class PrFixTrace:
    """Pencatat satu run --pr-fix. Aman dipakai walau PB mati."""

    def __init__(self, repo: str, ref: str, run_id: str) -> None:
        self.repo = repo
        self.ref = ref
        self.run_id = run_id
        self.seq = 0
        self.final_status: Optional[str] = None
        # Diisi alur utama bila hasilnya bukan "ok": no_change | dry_run
        self.outcome: Optional[str] = None
        self.fields: Dict[str, Any] = {}
        self._pb_base: Optional[str] = None
        self._pb_token: Optional[str] = None
        self._pb_enabled = self._init_pb()
        LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        self.local_path = LOCAL_DIR / f"{run_id}.jsonl"
        self._create_run({"status": "running", "started_at": _now()})

    # ── PocketBase plumbing ────────────────────────────────────────────
    def _init_pb(self) -> bool:
        try:
            if not pb_api.is_enabled():
                return False
            self._pb_base = pb_api._base_url().rstrip("/")
            self._pb_token = write_token(self._pb_base)
            return True
        except Exception:
            return False

    def _pb_post(self, collection: str, record: Dict[str, Any]) -> bool:
        if not self._pb_enabled or not self._pb_base:
            return False
        try:
            pb_api._http_json(
                "POST",
                f"{self._pb_base}/api/collections/{collection}/records",
                body=record,
                headers={"Authorization": self._pb_token or ""},
                timeout=10,
            )
            return True
        except Exception as e:  # 403/404/timeout — matikan sinkron untuk sisa run
            self._pb_enabled = False
            self._warn(f"PocketBase {collection} dilewati ({type(e).__name__}); jejak tetap di {self.local_path}")
            return False

    def _warn(self, msg: str) -> None:
        import sys
        print(f"   ⚠️  pr-fix trace: {msg}", file=sys.stderr)

    # ── Public API ─────────────────────────────────────────────────────
    def step(self, step_name: str, status: str = "ok", /, **detail: Any) -> None:
        if status not in STEP_STATUSES:
            raise ValueError(f"status langkah tidak valid: {status}")
        self.seq += 1
        row = {
            "run_id": self.run_id,
            "seq": self.seq,
            "step": step_name,
            "status": status,
            "detail": detail,
            "at": _now(),
        }
        self._append_local({"type": "step", **row})
        self._pb_post(STEPS_COLLECTION, row)

    def set(self, **fields: Any) -> None:
        """Tambah/ubah field run (cause, head_sha, branch, pr_url, ...)."""
        self.fields.update(fields)

    def finish(self, status: str) -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"status run tidak valid: {status}")
        self.final_status = status
        final = {**self.fields, "status": status, "finished_at": _now()}
        self._append_local({"type": "run", "run_id": self.run_id, **final})
        self._update_run(final)

    # ── internals ──────────────────────────────────────────────────────
    def _create_run(self, extra: Dict[str, Any]) -> None:
        base = {"run_id": self.run_id, "repo": self.repo, "ref": self.ref}
        self._append_local({"type": "run", **base, **extra})
        self._pb_post(RUNS_COLLECTION, {**base, **extra})

    def _update_run(self, fields: Dict[str, Any]) -> None:
        if not self._pb_enabled or not self._pb_base:
            return
        try:
            # Cari record berdasarkan run_id, lalu PATCH.
            flt = f'run_id="{self.run_id}"'
            found = pb_api._http_json(
                "GET",
                f"{self._pb_base}/api/collections/{RUNS_COLLECTION}/records?filter={_q(flt)}&perPage=1",
                headers={"Authorization": self._pb_token or ""},
                timeout=10,
            )
            items = found.get("items") or []
            if not items:
                self._pb_post(RUNS_COLLECTION, {"run_id": self.run_id, "repo": self.repo,
                                                "ref": self.ref, **fields})
                return
            pb_api._http_json(
                "PATCH",
                f"{self._pb_base}/api/collections/{RUNS_COLLECTION}/records/{items[0]['id']}",
                body=fields,
                headers={"Authorization": self._pb_token or ""},
                timeout=10,
            )
        except Exception as e:
            self._warn(f"update pr_fix_runs dilewati ({type(e).__name__})")

    def _append_local(self, obj: Dict[str, Any]) -> None:
        try:
            with self.local_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            self._warn(f"gagal tulis jejak lokal: {e}")


def _q(text: str) -> str:
    from urllib.parse import quote
    return quote(text, safe="")
