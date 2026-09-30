"""`bq --cicd-webhook <repo>` — cek webhook GitHub cicd-hw.qoin.id/hook.

Cek apakah repo sudah terpasang webhook ke `https://cicd-hw.qoin.id/hook`
(incoming-webhook service). Kalau belum, print saran perintah untuk memasang.

Bisa dipanggil sendiri (`bq --cicd-webhook <repo>`) atau berbarengan dengan
`--pr-fix` (`bq --pr-fix <repo> <ref> --cicd-webhook`) — cek dijalankan setelah
pr-fix sukses.

Juga menyediakan `run_cicd_webhook_setup()` — dipakai `--pr-fix` untuk
otomatis memasang webhook kalau belum ada (soft-fail bila error).
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import List, Optional

from . import github_api
from .config import save_env_value, use_gh_cli

HOOK_URL_DEFAULT = "https://cicd-hw.qoin.id/hook"
DEFAULT_EVENTS = ["push", "pull_request", "issue_comment"]

HMAC_ENV_KEY = "WEBHOOK_HOOK_HMAC"
HMAC_KUBE_CONTEXT = "hw-dev"
HMAC_KUBE_NS = "jenkins-x"
HMAC_SECRET_CANDIDATES = ("lighthouse-hmac-token", "incoming-webhook")
_HMAC_SAVE_PROMPTED = False


def _list_hooks(api_repo: str) -> List[dict]:
    """List webhooks untuk repo. Raises GitHubAPIError kalau 404/403/dsb.

    Menghormati GH_CLI: kalau true → pakai `gh api`, kalau false → native.
    """
    if use_gh_cli():
        res = subprocess.run(
            ["gh", "api", f"repos/{api_repo}/hooks"],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            raise github_api.GitHubAPIError(
                f"gh api repos/{api_repo}/hooks gagal: {res.stderr.strip()}"
            )
        import json
        try:
            return json.loads(res.stdout) or []
        except json.JSONDecodeError as e:
            raise github_api.GitHubAPIError(f"parse hooks response gagal: {e}") from e
    return github_api._request("GET", f"/repos/{api_repo}/hooks") or []


def _find_matching(hooks: List[dict], hook_url: str) -> Optional[dict]:
    for h in hooks:
        cfg = h.get("config") or {}
        if cfg.get("url") == hook_url:
            return h
    return None


def _github_webhook_new_url(api_repo: str) -> str:
    """URL halaman 'New webhook' di GitHub UI untuk repo."""
    return f"https://github.com/{api_repo}/settings/hooks/new"


def _print_manual_web_setup(api_repo: str, hook_url: str) -> None:
    """Panduan setup MANUAL via GitHub UI — dipakai bila CLI mode gagal
    (mis. token bukan admin, HMAC tidak bisa diambil, dsb).
    """
    web_url = _github_webhook_new_url(api_repo)
    print("🌐 Setup MANUAL via GitHub UI:")
    print(f"   1) Buka: {web_url}")
    print(f"   2) Payload URL   : {hook_url}")
    print( "   3) Content type  : application/json")
    print( "   4) Secret        : (minta ke DevOps — HMAC secret 'incoming-webhook')")
    print( "   5) SSL           : Enable SSL verification")
    print( "   6) Which events? : 'Let me select individual events' →")
    print(f"                       centang: {', '.join(DEFAULT_EVENTS)}")
    print( "   7) Active        : ✅ (default)")
    print( "   8) Klik 'Add webhook'.")


def _maybe_open_browser(url: str) -> None:
    """Prompt user (TTY only) untuk buka URL di browser default."""
    if not sys.stdin.isatty():
        return
    try:
        ans = input(f"   Buka {url} di browser sekarang? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if ans not in {"y", "yes"}:
        return
    try:
        import webbrowser
        if webbrowser.open(url):
            print(f"   ✅ Browser dibuka: {url}")
        else:
            print(f"   ⚠️  Gagal buka browser. Salin URL manual: {url}")
    except Exception as e:
        print(f"   ⚠️  Gagal buka browser ({e}). Salin URL manual: {url}")


def _print_suggest_install(api_repo: str, hook_url: str) -> None:
    repo_short = api_repo.split("/")[-1]
    print("💡 Pasang webhook — one-shot (auto ambil HMAC + register):")
    print(f"      bq --cicd-webhook {repo_short}")
    print( "   HMAC di-cache di ~/.build-q/.env (WEBHOOK_HOOK_HMAC).")
    print( "   Butuh gh scope admin:repo_hook + kubectl context hw-dev "
           "(secret jenkins-x/lighthouse-hmac-token).")
    print()
    _print_manual_web_setup(api_repo, hook_url)
    _maybe_open_browser(_github_webhook_new_url(api_repo))


def run_cicd_webhook_check(
    api_repo: str,
    *,
    hook_url: str = HOOK_URL_DEFAULT,
    auto_setup: bool = True,
) -> int:
    """Cek webhook `hook_url` di `api_repo`; one-shot register bila belum ada.

    Return 0 = sudah terpasang (aktif) atau berhasil di-setup, 1 = belum / non-aktif
    (dan setup gagal), 2 = akses ditolak / error API.
    """
    print(f"🔎 Cek webhook di {api_repo} → {hook_url}")
    try:
        hooks = _list_hooks(api_repo)
    except github_api.GitHubAPIError as e:
        msg = str(e)
        # 404 di endpoint /hooks umumnya berarti token bukan admin di repo.
        if "HTTP 404" in msg or "Not Found" in msg:
            print(f"   ⚠️  Tidak bisa akses daftar webhook (404 Not Found).")
            print(f"      Kemungkinan: user bukan admin di {api_repo}, atau repo tidak ada.")
            print("      Minta admin/owner repo untuk memasang, atau delegasi.")
            _print_suggest_install(api_repo, hook_url)
            return 2
        print(f"   ❌ Gagal query hooks: {msg}", file=sys.stderr)
        print()
        _print_manual_web_setup(api_repo, hook_url)
        _maybe_open_browser(_github_webhook_new_url(api_repo))
        return 2

    match = _find_matching(hooks, hook_url)
    if match is None:
        print(f"   ⚠️  Webhook {hook_url} BELUM terpasang di {api_repo}.")
        if hooks:
            urls = [(h.get("config") or {}).get("url", "?") for h in hooks]
            print(f"      Webhook lain yang terpasang: {', '.join(urls)}")
        if auto_setup:
            print()
            return run_cicd_webhook_setup(api_repo, hook_url=hook_url)
        _print_suggest_install(api_repo, hook_url)
        return 1

    active = match.get("active", False)
    hook_id = match.get("id", "?")
    events = ",".join(match.get("events") or [])
    if active:
        print(f"   ✅ Webhook sudah terpasang & aktif — id={hook_id} events=[{events}]")
        return 0
    print(f"   ⚠️  Webhook terpasang tapi NON-AKTIF — id={hook_id}.")
    repo_short = api_repo.split("/")[-1]
    print("      Aktifkan lagi via CLI:")
    print(f"      cicd-webhook update {repo_short} {hook_id}")
    print()
    edit_url = f"https://github.com/{api_repo}/settings/hooks/{hook_id}"
    print("      Atau via GitHub UI (centang 'Active'):")
    print(f"      {edit_url}")
    _maybe_open_browser(edit_url)
    return 1


# ── Setup (create) — dipakai --pr-fix, soft-fail bila error ──────────────

def _kubectl_read_secret(
    kube_context: str, ns: str, name: str, key: str = "hmac",
) -> Optional[str]:
    try:
        res = subprocess.run(
            ["kubectl", "--context", kube_context, "-n", ns,
             "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}"],
            capture_output=True, text=True, check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    b64 = res.stdout.strip()
    if not b64:
        return None
    try:
        import base64
        return base64.b64decode(b64).decode("utf-8").strip()
    except Exception:
        return None


def _fetch_hmac_secret(
    *,
    kube_context: str = HMAC_KUBE_CONTEXT,
    ns: str = HMAC_KUBE_NS,
    candidates=HMAC_SECRET_CANDIDATES,
) -> Optional[str]:
    """Ambil HMAC dari kubectl (coba beberapa nama secret standar)."""
    for name in candidates:
        val = _kubectl_read_secret(kube_context, ns, name)
        if val:
            return val
    return None


def _get_hmac_with_cache(*, prompt_save: bool = True) -> Optional[str]:
    """Resolve HMAC dengan urutan: env WEBHOOK_HOOK_HMAC / INCOMING_WEBHOOK_HMAC
    → kubectl. Bila diambil dari kubectl dan cache di ~/.build-q/.env belum
    ada, tawarkan simpan (TTY only, sekali per proses).
    """
    import os
    cached = os.getenv(HMAC_ENV_KEY) or os.getenv("INCOMING_WEBHOOK_HMAC")
    if cached:
        return cached.strip()

    hmac_val = _fetch_hmac_secret()
    if not hmac_val:
        return None

    global _HMAC_SAVE_PROMPTED
    if prompt_save and not _HMAC_SAVE_PROMPTED and sys.stdin.isatty():
        _HMAC_SAVE_PROMPTED = True
        try:
            ans = input(
                f"   💾 Simpan HMAC ke ~/.build-q/.env sebagai {HMAC_ENV_KEY}? [Y/n]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
            print()
        if ans in {"", "y", "yes"}:
            try:
                save_env_value(HMAC_ENV_KEY, hmac_val)
                print(f"   ✅ Tersimpan di ~/.build-q/.env ({HMAC_ENV_KEY})")
            except Exception as e:
                print(f"   ⚠️  Gagal simpan HMAC: {e}", file=sys.stderr)
    return hmac_val


def run_cicd_webhook_setup(
    api_repo: str,
    *,
    hook_url: str = HOOK_URL_DEFAULT,
    events: Optional[List[str]] = None,
) -> int:
    """Pasang webhook cicd-hw.qoin.id/hook di repo kalau belum ada.

    Skip bila sudah ada (return 0). Soft-fail bila error apapun (return 1,
    print warning, tidak raise). Cocok dipanggil dari `--pr-fix` — tidak
    boleh membatalkan PR flow.

    Return 0 = sudah/berhasil dipasang, 1 = gagal (soft-fail).
    """
    events = events or DEFAULT_EVENTS
    print(f"🪝 Setup webhook {hook_url} di {api_repo}")

    # 1) Cek dulu — skip kalau sudah ada
    try:
        hooks = _list_hooks(api_repo)
    except github_api.GitHubAPIError as e:
        print(f"   ⚠️  Tidak bisa cek webhook: {e}", file=sys.stderr)
        print("   ℹ️  Lewati setup — pasang manual jika perlu.")
        return 1

    match = _find_matching(hooks, hook_url)
    if match is not None:
        hook_id = match.get("id", "?")
        active = match.get("active", False)
        state = "aktif" if active else "NON-AKTIF"
        print(f"   ⏭  Sudah terpasang (id={hook_id}, {state}) — skip.")
        return 0

    # 2) Ambil HMAC (env WEBHOOK_HOOK_HMAC → kubectl → prompt simpan)
    hmac_secret = _get_hmac_with_cache()
    if not hmac_secret:
        print(f"   ⚠️  Tidak bisa ambil HMAC (env {HMAC_ENV_KEY} / kubectl).")
        print("   ℹ️  Lewati setup — pasang manual dengan:")
        _print_suggest_install(api_repo, hook_url)
        return 1

    # 3) Create via native REST — POST /repos/{repo}/hooks
    payload = json.dumps({
        "name": "web",
        "active": True,
        "events": events,
        "config": {
            "url": hook_url,
            "content_type": "json",
            "secret": hmac_secret,
            "insecure_ssl": "0",
        },
    }).encode("utf-8")

    try:
        created = github_api._request(
            "POST", f"/repos/{api_repo}/hooks", body=payload,
        )
    except github_api.GitHubAPIError as e:
        msg = str(e)
        print(f"   ⚠️  Gagal buat webhook: {msg}", file=sys.stderr)
        if "HTTP 404" in msg or "Not Found" in msg or "HTTP 403" in msg:
            print("   ℹ️  Token mungkin bukan admin repo (butuh scope admin:repo_hook).")
        print()
        _print_manual_web_setup(api_repo, hook_url)
        _maybe_open_browser(_github_webhook_new_url(api_repo))
        return 1

    hook_id = created.get("id", "?")
    ev = ",".join(created.get("events") or [])
    print(f"   ✅ Webhook dipasang — id={hook_id} events=[{ev}]")
    return 0
