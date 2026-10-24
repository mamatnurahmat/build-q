"""Login SSO ArgoCD + pemakaian official `argocd` CLI secara inline.

Sentral: PocketBase. Token ArgoCD disimpan di dua tempat dengan urutan prioritas:

  1. Secret PB `ARGOCD_TOKEN` (collection `secrets`, scope `argocd`) — di-hydrate ke
     env oleh `pb_api.hydrate_env()`; atau ARGOCD_TOKEN dari shell.
  2. ~/.build-q/auth.json (key `ARGOCD_TOKEN`) — salinan lokal, satu-satunya file
     lokal bq. Dipakai bila secret PB kosong / kedaluwarsa.

Token yang sudah kedaluwarsa (klaim `exp` JWT) dilewati. Setiap login SSO
memperbarui secret PB (`expires_at`, `rotated_at`) dan salinan lokal.

Login memakai official `argocd login` dengan `--config` ke direktori sementara,
sehingga tidak ada file di ~/.config/argocd. Pemakaian inline: `bq argocd <args>`
menjalankan official `argocd` dengan `--server`/`--grpc-web` dan token lewat env
`ARGOCD_AUTH_TOKEN` (tidak muncul di argv / `ps`).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml

from . import pb_api

DEFAULT_SSO_PORT = 8085
LOGIN_TIMEOUT = 600  # detik — menunggu user menyelesaikan login di browser
HTTP_TIMEOUT = 30
EXPIRY_MARGIN = 300  # detik — token dianggap kedaluwarsa bila sisa umurnya kurang dari ini

PB_SECRET_KEY = "ARGOCD_TOKEN"
PB_SECRET_SCOPE = "argocd"
PB_TIME_FMT = "%Y-%m-%d %H:%M:%S.000Z"  # format datetime PocketBase


class ArgocdAuthError(RuntimeError):
    pass


def _login_target(api_url: str) -> Tuple[str, str]:
    """(host, root-path) dari ARGOCD_API_URL, mis. https://cicd-hw.qoin.id/argocd → (cicd-hw.qoin.id, /argocd)."""
    parsed = urlparse(api_url or "")
    if not parsed.netloc:
        raise ArgocdAuthError(f"ARGOCD_API_URL tidak valid: '{api_url}'")
    return parsed.netloc, parsed.path.rstrip("/")


def _argocd_bin() -> str:
    exe = shutil.which(os.environ.get("ARGOCD_CLI") or "argocd")
    if not exe:
        raise ArgocdAuthError("argocd CLI tidak ditemukan — install: brew install argocd")
    return exe


def read_cli_token(path: str) -> str:
    """auth-token dari context aktif (current-context) di config argocd CLI."""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ArgocdAuthError(f"config argocd CLI tidak bisa dibaca ({path}): {e}") from e
    current = str(data.get("current-context") or "")
    for user in data.get("users") or []:
        if isinstance(user, dict) and user.get("name") == current and user.get("auth-token"):
            return str(user["auth-token"])
    return ""


def verify_token(api_url: str, token: str) -> Dict[str, Any]:
    """GET userinfo. ArgoCD menjawab 200 `{}` untuk token tak valid, jadi cek loggedIn."""
    base = (api_url or "").rstrip("/")
    req = Request(f"{base}/api/v1/session/userinfo",
                  headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as res:
            info = json.loads(res.read() or b"{}") or {}
    except HTTPError as e:
        raise ArgocdAuthError(f"userinfo HTTP {e.code} dari {base}") from e
    except (URLError, OSError, ValueError) as e:
        raise ArgocdAuthError(f"tidak bisa menghubungi {base}: {getattr(e, 'reason', e)}") from e
    if not info.get("loggedIn"):
        raise ArgocdAuthError("userinfo loggedIn=false — token tidak valid")
    return info


# ── Kedaluwarsa & prioritas ──────────────────────────────────────────────

def is_expired(token: str, *, margin: int = EXPIRY_MARGIN) -> bool:
    """True bila klaim exp JWT sudah lewat (atau tinggal `margin` detik). Tanpa exp → False."""
    exp = pb_api.jwt_exp(token)
    return bool(exp) and exp - margin <= time.time()


def local_token() -> str:
    """ARGOCD_TOKEN dari ~/.build-q/auth.json."""
    return str(pb_api.read_store().get("ARGOCD_TOKEN") or "")


def resolve_token(config: Dict[str, Any]) -> str:
    """Token ArgoCD aktif menurut prioritas PB → lokal. "" bila tidak ada yang valid."""
    acfg = config.get("argocd") or {}
    candidates: List[str] = [acfg.get("token") or ""]
    if acfg.get("auth_file"):  # server (user_config) tidak membaca file lokal
        candidates.append(local_token())
    for tok in candidates:
        if tok and not is_expired(tok):
            return tok
    return ""


# ── Penyimpanan: PB (sentral) + lokal ───────────────────────────────────

def _pb_request(method: str, path: str, body: Optional[dict] = None) -> dict:
    base = pb_api._base_url()
    return pb_api._http_json(method, f"{base}{path}", body=body,
                             headers={"Authorization": f"Bearer {pb_api.get_token(base)}"})


def push_pb_secret(token: str) -> str:
    """Upsert secret PB `ARGOCD_TOKEN`. Return 'created' | 'updated'. Raise PBAPIError."""
    exp = pb_api.jwt_exp(token)
    now = time.strftime(PB_TIME_FMT, time.gmtime())
    owner = ((pb_api.read_auth() or {}).get("record") or {}).get("email") or ""
    body = {
        "key": PB_SECRET_KEY,
        "value": token,
        "scope": PB_SECRET_SCOPE,
        "env": "all",
        "type": "token",
        "active": True,
        "owner": owner,
        "description": "Token SSO ArgoCD — diperbarui oleh `bq --argocd-auth-sso`",
        "rotated_at": now,
        "expires_at": time.strftime(PB_TIME_FMT, time.gmtime(exp)) if exp else "",
    }
    flt = urllib.parse.quote(f'key="{PB_SECRET_KEY}"')
    found = _pb_request("GET", f"/api/collections/secrets/records?perPage=1&filter={flt}").get("items") or []
    if found:
        _pb_request("PATCH", f"/api/collections/secrets/records/{found[0]['id']}", body)
        return "updated"
    _pb_request("POST", "/api/collections/secrets/records", body)
    return "created"


def save_token(token: str) -> Dict[str, str]:
    """Simpan token ke salinan lokal dan secret PB. Kegagalan PB tidak membatalkan login.

    Return {"local": path, "pb": "created"|"updated"|"gagal: <alasan>"}.
    """
    pb_api.update_store({"ARGOCD_TOKEN": token})
    result = {"local": str(pb_api.AUTH_FILE), "pb": ""}
    try:
        result["pb"] = push_pb_secret(token)
    except pb_api.PBAPIError as e:
        result["pb"] = f"gagal: {e}"
    return result


# ── Login SSO ────────────────────────────────────────────────────────────

def sso_login(config: Dict[str, Any], *, port: int = DEFAULT_SSO_PORT,
              launch_browser: bool = True) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Login SSO via official `argocd login` (config sementara) → verifikasi → simpan.

    Return (userinfo, hasil simpan). Config sementara dihapus setelah selesai.
    """
    api_url = (config.get("argocd") or {}).get("api_url") or ""
    exe = _argocd_bin()
    host, root = _login_target(api_url)

    tmp = tempfile.mkdtemp(prefix="bq-argocd-")
    try:
        cfg_path = os.path.join(tmp, "config")
        cmd = [exe, "login", host, "--config", cfg_path,
               "--grpc-web", "--sso", "--sso-port", str(port)]
        if root:
            cmd += ["--grpc-web-root-path", root]
        if not launch_browser:
            cmd.append("--sso-launch-browser=false")

        print(f"🔐 Login SSO ke {host}{root} ...")
        print("   (browser terbuka → login dengan GitHub org ngentools)")
        try:
            rc = subprocess.run(cmd, timeout=LOGIN_TIMEOUT).returncode
        except subprocess.TimeoutExpired as e:
            raise ArgocdAuthError("login SSO melewati batas waktu") from e
        if rc != 0:
            raise ArgocdAuthError(f"argocd login gagal (exit {rc})")

        token = read_cli_token(cfg_path)
        if not token:
            raise ArgocdAuthError("auth-token tidak ditemukan setelah login SSO")
        info = verify_token(api_url, token)
        saved = save_token(token)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return info, saved


def ensure_token(config: Dict[str, Any]) -> str:
    """Token aktif; bila tidak ada dan terminal interaktif (auto-login aktif) → login SSO."""
    token = resolve_token(config)
    if token:
        return token
    acfg = config.get("argocd") or {}
    if acfg.get("auto_login", True) and sys.stdin.isatty():
        print("🔐 Token ArgoCD tidak ada / kedaluwarsa — login SSO ...", file=sys.stderr)
        sso_login(config)
        token = resolve_token(config)
        if token:
            return token
    raise ArgocdAuthError("token ArgoCD tidak valid / kedaluwarsa — jalankan `bq --argocd-auth-sso`")


# ── Pemakaian inline official argocd CLI ────────────────────────────────

def run_argocd(args: List[str], config: Dict[str, Any]) -> int:
    """Jalankan official `argocd <args>` dengan server & token inline. Return exit code."""
    exe = _argocd_bin()
    host, root = _login_target((config.get("argocd") or {}).get("api_url") or "")
    token = ensure_token(config)
    cmd = [exe, "--server", host, "--grpc-web"]
    if root:
        cmd += ["--grpc-web-root-path", root]
    cmd += args
    env = {**os.environ, "ARGOCD_AUTH_TOKEN": token}
    return subprocess.run(cmd, env=env).returncode


USAGE = """\
Usage:
  bq argocd <args...>     jalankan official `argocd` dengan token SSO (inline, tanpa ~/.config/argocd)
  bq argocd app list      contoh
  bq --argocd-auth-sso    login SSO → token ke PB secret ARGOCD_TOKEN + ~/.build-q/auth.json
"""


def cli_main(argv: List[str], config: Dict[str, Any]) -> int:
    if not argv:
        print(USAGE)
        return 2
    try:
        return run_argocd(argv, config)
    except ArgocdAuthError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
