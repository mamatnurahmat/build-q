"""`bq auth` — login ke PocketBase devops, simpan token ke ~/.build-q/auth.json.

Default: SSO GitHub (provider OIDC "GitHub (Dex)" di collection `devops_users`,
Dex yang sama dengan ArgoCD). Alurnya sama dengan PocketBase JS SDK
`authWithOAuth2()` (all-in-one):

  1. GET  /api/realtime                → event PB_CONNECT {clientId}
  2. POST /api/realtime                  subscribe topik `@oauth2`
  3. Browser → authURL + redirect_uri={PB}/api/oauth2-redirect, state=clientId
  4. PB mengirim {state, code} lewat realtime `@oauth2`
  5. POST /api/collections/devops_users/auth-with-oauth2 (code + PKCE verifier)

Token (umur 5 hari) dipakai `pb_api.get_token()` untuk semua request PB dan
di-refresh otomatis. Opsi `--password` untuk login email/password.

    bq auth login [--password] [--email <e>] [--no-browser]
    bq auth status
    bq auth refresh
    bq auth logout
"""
from __future__ import annotations

import argparse
import getpass
import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Optional

from . import pb_api
from .pb_api import AUTH_COLLECTION, PBAPIError

LOGIN_TIMEOUT = 300
NEW_USER_ROLE = "view"  # createRule devops_users mewajibkan role=view saat daftar via OAuth2


def _oidc_provider(base: str, collection: str) -> dict:
    methods = pb_api._http_json("GET", f"{base}/api/collections/{collection}/auth-methods")
    providers = (methods.get("oauth2") or {}).get("providers") or []
    if not providers:
        raise PBAPIError(f"collection {collection} tidak punya provider OAuth2")
    return next((p for p in providers if p.get("name") == "oidc"), providers[0])


def _realtime_reader(base: str, events: "queue.Queue", stop: threading.Event) -> None:
    """Baca SSE /api/realtime → queue (event, data). Berhenti bila `stop` diset."""
    req = urllib.request.Request(f"{base}/api/realtime", headers={"Accept": "text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=LOGIN_TIMEOUT + 30) as resp:
            event, data = "", []
            for raw in resp:
                if stop.is_set():
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    if event or data:
                        events.put((event, "\n".join(data)))
                    event, data = "", []
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
    except Exception as e:  # noqa: BLE001 — diteruskan ke thread utama
        events.put(("__error__", str(e)))


def _wait_event(events: "queue.Queue", name: str, deadline: float) -> dict:
    while True:
        left = deadline - time.time()
        if left <= 0:
            raise PBAPIError(f"timeout menunggu {name} ({LOGIN_TIMEOUT}s)")
        try:
            event, data = events.get(timeout=left)
        except queue.Empty:
            continue
        if event == "__error__":
            raise PBAPIError(f"koneksi realtime terputus: {data}")
        if event == name:
            try:
                return json.loads(data or "{}")
            except ValueError as e:
                raise PBAPIError(f"payload {name} bukan JSON") from e


def login_sso(base: str, collection: str = AUTH_COLLECTION, *, open_browser: bool = True) -> dict:
    provider = _oidc_provider(base, collection)
    events: "queue.Queue" = queue.Queue()
    stop = threading.Event()
    threading.Thread(target=_realtime_reader, args=(base, events, stop), daemon=True).start()
    deadline = time.time() + LOGIN_TIMEOUT
    try:
        client_id = _wait_event(events, "PB_CONNECT", deadline).get("clientId")
        if not client_id:
            raise PBAPIError("PB_CONNECT tanpa clientId")
        pb_api._http_json("POST", f"{base}/api/realtime",
                          body={"clientId": client_id, "subscriptions": ["@oauth2"]})

        redirect_url = f"{base}/api/oauth2-redirect"
        parts = urllib.parse.urlsplit(provider["authURL"] + urllib.parse.quote(redirect_url, safe=""))
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        query["state"] = client_id
        auth_url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))

        print(f"🔐 Login SSO {provider.get('displayName') or provider['name']} → {base} ({collection})")
        print(f"   Buka URL berikut bila browser tidak terbuka otomatis:\n   {auth_url}")
        if open_browser:
            webbrowser.open(auth_url)

        msg = _wait_event(events, "@oauth2", deadline)
    finally:
        stop.set()

    if msg.get("error"):
        raise PBAPIError(f"OAuth2 ditolak: {msg['error']}")
    if msg.get("state") != client_id:
        raise PBAPIError("state OAuth2 tidak cocok — login dibatalkan")
    resp = pb_api._http_json(
        "POST", f"{base}/api/collections/{collection}/auth-with-oauth2",
        body={"provider": provider["name"], "code": msg.get("code"),
              "codeVerifier": provider.get("codeVerifier"), "redirectURL": redirect_url,
              "createData": {"role": NEW_USER_ROLE}},
    )
    if not resp.get("token"):
        raise PBAPIError("auth-with-oauth2 response missing 'token'")
    return pb_api.save_auth(base, resp["token"], resp.get("record") or {},
                            method=provider["name"], collection=collection)


def login_password(base: str, email: str, password: str, collection: str = AUTH_COLLECTION) -> dict:
    resp = pb_api._http_json("POST", f"{base}/api/collections/{collection}/auth-with-password",
                             body={"identity": email, "password": password})
    if not resp.get("token"):
        raise PBAPIError("auth response missing 'token'")
    return pb_api.save_auth(base, resp["token"], resp.get("record") or {},
                            method="password", collection=collection)


def verify(auth: dict) -> dict:
    """Validasi token ke server (read-only): GET record milik user sendiri."""
    col = auth.get("collection") or AUTH_COLLECTION
    rid = (auth.get("record") or {}).get("id", "")
    return pb_api._http_json("GET", f"{auth['pb_url']}/api/collections/{col}/records/{rid}",
                             headers={"Authorization": f"Bearer {auth['token']}"})


def _describe(auth: dict) -> str:
    rec = auth.get("record") or {}
    return (f"{rec.get('email') or rec.get('id')} (role={rec.get('role') or '?'}) "
            f"via {auth.get('method')} — berlaku s/d {auth.get('expires_at') or '?'}")


def cli_main(argv: Optional[list] = None) -> int:
    from .config import ENV_FILE, _load_dotenv

    parser = argparse.ArgumentParser(prog="bq auth", description="Login PocketBase devops (SSO GitHub)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_login = sub.add_parser("login", help="Login (default SSO GitHub via Dex) & simpan token")
    p_login.add_argument("--password", action="store_true", help="Login email/password, bukan SSO")
    p_login.add_argument("--email", help="Email untuk --password (default: prompt)")
    p_login.add_argument("--no-browser", action="store_true", help="Jangan buka browser otomatis")
    p_login.add_argument("--url", help="PocketBase URL (default PB_API_URL)")
    sub.add_parser("status", help="Tampilkan & validasi token tersimpan")
    sub.add_parser("refresh", help="Perpanjang token (auth-refresh)")
    sub.add_parser("logout", help="Hapus token & cache secrets")
    args = parser.parse_args(argv)

    if ENV_FILE.exists():
        _load_dotenv(ENV_FILE)  # hanya PB_API_URL dkk — tanpa hydrate secrets

    try:
        if args.cmd == "login":
            base = (args.url or pb_api._base_url()).rstrip("/")
            if args.password:
                email = args.email or input("Email: ").strip()
                auth = login_password(base, email, getpass.getpass("Password: "))
            else:
                auth = login_sso(base, open_browser=not args.no_browser)
            pb_api.clear_cache()  # secrets di-fetch ulang dengan identitas baru
            print(f"✅ Login berhasil: {_describe(auth)}")
            print(f"   Token disimpan di {pb_api.AUTH_FILE} (perm 600)")
            return 0

        if args.cmd == "logout":
            removed = pb_api.clear_auth()
            pb_api.clear_cache()
            print("🧹 Token & cache PB dihapus." if removed else "ℹ️  Belum ada token tersimpan.")
            return 0

        auth = pb_api.read_auth()
        if not auth:
            print("❌ Belum login — jalankan `bq auth login`", file=sys.stderr)
            return 1
        if args.cmd == "refresh":
            auth = pb_api.refresh_auth(auth)
            print(f"✅ Token diperpanjang: {_describe(auth)}")
            return 0

        # status
        print(f"   URL   : {auth.get('pb_url')} ({auth.get('collection')})")
        print(f"   User  : {_describe(auth)}")
        if (auth.get("exp") or 0) <= time.time():
            print("❌ Token kedaluwarsa — jalankan `bq auth login`", file=sys.stderr)
            return 1
        rec = verify(auth)
        print(f"✅ Token valid (server): {rec.get('email')} role={rec.get('role')}")
        return 0
    except PBAPIError as e:
        print(f"❌ {e}", file=sys.stderr)
        if str(e).startswith(("HTTP 401", "HTTP 403")):
            print("   Token ditolak server — jalankan `bq auth login`", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n⏹️  Dibatalkan.", file=sys.stderr)
        return 130
