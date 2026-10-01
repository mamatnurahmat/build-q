"""`bq --repo` — cek CICD repo config dari PocketBase collection.

Menggantikan lookup cicd.json di GitHub dengan data dari PocketBase
collection `repo`. Berguna saat pipeline config sudah terpusat di
PocketBase IDP dan tidak perlu disimpan per-repo.

Usage:
    bq --repo <repo> [--ref <ref>] [--cicd=pb] [--dry-run]
    bq --repo <repo> <ref> --cicd=pb
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from .config import load_config
from .pb_api import PBAPIError, fetch_collection_records


def _find_repo_record(
    repo: str,
    items: list,
    ref: str | None = None,
) -> dict | None:
    """Cari record yang cocok dengan nama repo (dan optional ref).

    Urutan prioritas:
      1. `repo` field exact match + `ref` match (bila ref diberikan)
      2. `repo` field exact match (record tanpa ref, atau ref tidak diberikan)
      3. `name` field exact match (fallback)
      4. `github_repo` field exact match (fallback)
    """
    if not items:
        return None

    # Normalize: hapus owner/ prefix bila ada, ambil nama repo saja
    repo_short = repo.split("/")[-1].lower()

    for item in items:
        for key in ("repo", "name", "github_repo"):
            val = (item.get(key) or "").strip().lower()
            if not val:
                continue
            val_short = val.split("/")[-1]
            if val_short == repo_short or val == repo.lower():
                if ref:
                    item_ref = (item.get("ref") or item.get("branch") or "").strip()
                    if item_ref and item_ref != ref:
                        continue
                return item
    return None


def format_repo_config(record: dict) -> str:
    """Format one repo config record for display."""
    lines: list[str] = []
    lines.append(
        f" Repo     : {record.get('repo') or record.get('name') or record.get('github_repo','?')}"
    )
    lines.append(f" Ref      : {record.get('ref') or record.get('branch') or '(all)'}")

    # The current IDP schema stores CICD values in a nested object. Keep
    # support for the old flat schema as well.
    cicd_value = record.get("cicd")
    cicd: dict = cicd_value if isinstance(cicd_value, dict) else {}
    cicd_keys = {
        "IMAGE": "Image",
        "PROJECT": "Project",
        "DEPLOYMENT": "Deployment",
        "PORT": "Port",
        "CLUSTER": "Cluster",
        "NODETYPE": "Node type",
        "REGISTRY": "Registry",
    }
    for key, label in cicd_keys.items():
        value = record.get(key, cicd.get(key))
        if value:
            lines.append(f" {label:11}= {value}")

    # Extra top-level keys (do not print the nested CICD object verbatim).
    shown = {
        "repo", "name", "github_repo", "ref", "branch", "id", "created",
        "updated", "active", "collectionId", "collectionName", "cicd",
    }
    shown.update(k.lower() for k in cicd_keys)
    extras = {
        key: value for key, value in record.items()
        if key.lower() not in shown and not key.startswith("@")
    }
    for key, value in extras.items():
        if value is not None and value != "":
            lines.append(f" {key:11}= {value}")

    return "\n".join(lines)


def run_repo_check(
    repo: str,
    ref: str | None = None,
    *,
    cicd_source: str = "pb",
    collection: str = "repo",
    dry_run: bool = False,
) -> int:
    """Check CICD repo config from PocketBase collection.

    Args:
        repo: Repository name (e.g. "plus-be-service")
        ref: Optional branch/ref filter
        cicd_source: Source of CICD config ("pb" for PocketBase)
        collection: PocketBase collection name (default: "repo")
        dry_run: If True, only print what would be done

    Returns:
        0 if config found and valid, 1 otherwise
    """
    config = load_config()  # noqa: F841 — kept for future use

    print(f"🔍 Repo CICD check: {repo}" + (f" @ {ref}" if ref else ""))
    print()

    if cicd_source == "pb":
        print(f"📡 Source: PocketBase ({collection} collection)")
        if dry_run:
            pb_url = os.getenv("PB_API_URL", "https://cicd-hw.qoin.id/devops")
            print(f" 🔄 Akan fetch collection '{collection}' dari PB API")
            print(
                f" 🔍 Mencari record untuk repo '{repo}'"
                + (f" ref '{ref}'" if ref else "")
            )
            print()
            print("📋 Perintah ekuivalen:")
            print(
                f"   curl -X GET '{pb_url}/api/collections/"
                f"{collection}/records?filter=(repo~'{repo}')'"
            )
            print()
            print("(dry-run — tidak ada perubahan nyata)")
            return 0

        # Cek PB API credentials
        pb_enabled = os.getenv("PB_API", "").strip().lower() in {"1", "true", "yes", "on"}
        if not pb_enabled:
            print(" ⚠️ PB_API belum diaktifkan (set PB_API=true di ~/.build-q/.env)")
            print(" ℹ️  Jalankan `bq --pb-login` dulu untuk autentikasi PB API.")
            return 1

        try:
            records = fetch_collection_records(collection, per_page=500)
        except PBAPIError as e:
            print(f" ❌ Gagal fetch dari PB: {e}", file=sys.stderr)
            return 1

        record = _find_repo_record(repo, records, ref)
        if record is None:
            print(
                f" ❌ Tidak ditemukan konfigurasi untuk repo '{repo}'"
                + (f" ref '{ref}'" if ref else "")
                + f" di collection '{collection}'"
            )
            print()
            print(" Collection tersedia:")
            if not records:
                print("   (kosong — belum ada data di collection ini)")
            else:
                existing = set()
                for r in records:
                    for key in ("repo", "name", "github_repo"):
                        v = r.get(key)
                        if v:
                            existing.add(str(v))
                            break
                for name in sorted(existing):
                    print(f"   • {name}")
            return 1

        print("\n📋 Repo CICD config:")
        print(format_repo_config(record))
        print()
        print("✅ Config ditemukan.")
        return 0

    print(f" ❌ Unsupported --cicd source: {cicd_source} (gunakan 'pb')", file=sys.stderr)
    return 1
