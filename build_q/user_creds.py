"""Credential per-request dari PocketBase milik user — untuk `bq --serve`.

Stateless: tiap request membawa token PocketBase user (`X-PB-Token`), lalu
server membaca identitas & secret user itu langsung dari PocketBase:

- tidak memakai `~/.build-q/auth.json`, `.pb-cache.json`, `.env`, maupun
  `os.environ` milik proses server (yang berisi credential pemilik server);
- tidak menulis cache; secret hanya hidup di memori selama request/job;
- kubeconfig (`KUBECONFIG*_B64`) ditulis ke direktori temp 0700 selama job
  berjalan lalu dihapus (kubectl hanya bisa membaca kubeconfig dari file).

Hak akses mengikuti aturan PocketBase: user hanya mendapat secret miliknya.
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator

from . import pb_api
from .config import build_config
from .pb_api import AUTH_COLLECTION, PBAPIError

_KUBECONFIG_KEY = re.compile(r"^KUBECONFIG(_[A-Z0-9_]+)?$")


class UserAuthError(PBAPIError):
    """Token PocketBase user tidak ada / ditolak — klien harus login ulang."""


@dataclass
class UserCreds:
    user_id: str
    email: str
    role: str
    secrets: Dict[str, str] = field(repr=False, default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        return self.secrets.get(key) or default

    def kubeconfigs(self) -> Dict[str, bytes]:
        """{KEY: isi kubeconfig} dari key KUBECONFIG / KUBECONFIG_*(_B64)."""
        out: Dict[str, bytes] = {}
        for key, value in sorted(self.secrets.items()):
            if not _KUBECONFIG_KEY.match(key) or not value.strip():
                continue
            if key.endswith("_B64"):
                try:
                    out[key] = base64.b64decode(value, validate=True)
                except (binascii.Error, ValueError):
                    continue  # value rusak → abaikan, kubectl akan melaporkan context tak ada
            else:
                out[key] = value.encode("utf-8")
        return out


def _token_claims(token: str) -> Dict[str, Any]:
    """Klaim JWT tanpa verifikasi — verifikasi dilakukan server PocketBase."""
    import json
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, TypeError):
        return {}


def resolve(pb_token: str, *, base_url: str = "") -> UserCreds:
    """Verifikasi token user ke PocketBase lalu ambil secret miliknya (tanpa cache)."""
    pb_token = (pb_token or "").strip()
    if not pb_token:
        raise UserAuthError("belum login PocketBase")
    base = (base_url or pb_api._base_url()).rstrip("/")
    claims = _token_claims(pb_token)
    user_id = str(claims.get("id") or "")
    if not user_id or claims.get("type") not in (None, "auth"):
        raise UserAuthError("token PocketBase tidak valid")
    headers = {"Authorization": f"Bearer {pb_token}"}
    try:
        record = pb_api._http_json(
            "GET", f"{base}/api/collections/{AUTH_COLLECTION}/records/{user_id}", headers=headers)
        secrets = pb_api.fetch_secrets(base, pb_token)
    except PBAPIError as e:
        if str(e).startswith(("HTTP 401", "HTTP 403", "HTTP 404")):
            raise UserAuthError("token PocketBase ditolak/kedaluwarsa — login ulang") from e
        raise
    return UserCreds(user_id=user_id, email=str(record.get("email") or ""),
                     role=str(record.get("role") or ""), secrets=secrets)


def user_config(creds: UserCreds) -> Dict[str, Any]:
    """Config `bq` murni dari secret user + default — tanpa env/file server."""
    cfg = build_config(lambda key, default=None: creds.get(key, default or ""))
    # Tidak ada fallback ke token/file lokal server, dan tidak ada login interaktif.
    cfg["argocd"]["auth_file"] = ""
    cfg["argocd"]["auto_login"] = False
    return cfg


@contextmanager
def kubeconfig_path(creds: UserCreds) -> Iterator[str]:
    """Nilai KUBECONFIG (file temp 0600 di dir 0700, dihapus setelah blok); "" bila tidak ada."""
    configs = creds.kubeconfigs()
    if not configs:
        yield ""
        return
    tmp = tempfile.mkdtemp(prefix="bq-kube-")
    try:
        paths = []
        for i, data in enumerate(configs.values()):
            path = os.path.join(tmp, f"config-{i}")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            paths.append(path)
        yield os.pathsep.join(paths)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
