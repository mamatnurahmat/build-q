"""`bq --cicd-webhook <repo>` — cek webhook GitHub cicd-hw.qoin.id/hook.

Cek apakah repo sudah terpasang webhook ke `https://cicd-hw.qoin.id/hook`
(incoming-webhook service). Kalau belum, print saran perintah untuk memasang.

Bisa dipanggil sendiri (`bq --cicd-webhook <repo>`) atau berbarengan dengan
`--pr-fix` (`bq --pr-fix <repo> <ref> --cicd-webhook`) — cek dijalankan setelah
pr-fix sukses.

Juga menyediakan `run_cicd_webhook_setup()` — dipakai `--pr-fix` untuk
otomatis memasang webhook kalau belum ada (soft-fail bila error).

HMAC webhook diambil dari PB collection `secrets` (key WEBHOOK_HOOK_HMAC),
lalu divalidasi ke secret yang benar-benar dipakai service untuk verifikasi
X-Hub-Signature-256 (hw-dev jenkins-x/incoming-webhook, key hmac). Webhook
yang sudah terpasang tapi delivery terakhirnya 403 (HMAC salah) di-sync ulang.

Mode auto (default CLI) juga memastikan hanya ada SATU hook ke endpoint
cicd-hw.qoin.id/hook dan wajib https (duplikat dihapus, http:// diubah),
memverifikasi HMAC via ping, lalu mencatat hasilnya ke PB collection `repo`
(dibuat bila belum ada) dan `github_hook` — lihat `hook_registry`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import List, Optional, Tuple

from . import github_api, hook_registry, pb_api
from .config import use_gh_cli

HOOK_URL_DEFAULT = "https://cicd-hw.qoin.id/hook"
DEFAULT_EVENTS = ["push", "pull_request", "issue_comment"]

HMAC_ENV_KEY = "WEBHOOK_HOOK_HMAC"
HMAC_KUBE_CONTEXT = "hw-dev"
HMAC_KUBE_NS = "jenkins-x"
# Secret yang dipakai service incoming-webhook untuk verifikasi signature.
# Jangan pakai lighthouse-hmac-token — nilainya beda → semua delivery 403.
HMAC_SERVICE_SECRET = "incoming-webhook"
HMAC_SERVICE_REF = f"{HMAC_KUBE_CONTEXT} {HMAC_KUBE_NS}/{HMAC_SERVICE_SECRET}"


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


def _endpoint(url: Optional[str]) -> str:
    """URL tanpa skema & slash akhir — http/https ke host+path sama = endpoint sama."""
    return (url or "").split("://", 1)[-1].rstrip("/").lower()


def _find_all_matching(hooks: List[dict], hook_url: str) -> List[dict]:
    """Semua hook ke endpoint `hook_url` (http maupun https), urut prioritas
    dipertahankan: https, aktif, delivery terakhir 200, lalu id terlama.
    """
    target = _endpoint(hook_url)
    found = [h for h in hooks if _endpoint((h.get("config") or {}).get("url")) == target]
    return sorted(found, key=lambda h: (
        (h.get("config") or {}).get("url") != hook_url,
        not h.get("active", False),
        (h.get("last_response") or {}).get("code") != 200,
        h.get("id") or 0,
    ))


def _ensure_single_hook(
    api_repo: str, matches: List[dict], hook_url: str,
) -> Tuple[Optional[dict], List[int]]:
    """Sisakan satu hook (https) ke endpoint; hook lain dihapus.

    Return (hook yang dipertahankan | None bila gagal ubah ke https, id hook yang dihapus).
    """
    keeper, removed = matches[0], []
    for h in matches[1:]:
        url = (h.get("config") or {}).get("url")
        try:
            github_api._request("DELETE", f"/repos/{api_repo}/hooks/{h['id']}")
        except github_api.GitHubAPIError as e:
            print(f"   ⚠️  Gagal hapus hook duplikat id={h['id']} ({url}): {e}", file=sys.stderr)
            continue
        print(f"   🗑  Hook duplikat dihapus — id={h['id']} url={url}")
        removed.append(h["id"])

    url = (keeper.get("config") or {}).get("url")
    if url != hook_url:
        print(f"   🔧 Hook id={keeper['id']} masih {url} → diubah ke {hook_url}")
        if _sync_hook_secret(api_repo, keeper, hook_url) != 0:
            return None, removed
        keeper = {**keeper, "config": {**(keeper.get("config") or {}), "url": hook_url}}
    return keeper, removed


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
    print(f"   4) Secret        : nilai PB collection secrets → {HMAC_ENV_KEY} "
          "(minta ke DevOps bila tidak punya akses)")
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
    print(f"   HMAC dari PB collection secrets ({HMAC_ENV_KEY}), divalidasi ke "
          f"secret {HMAC_SERVICE_REF}.")
    print( "   Butuh gh scope admin:repo_hook + `bq auth login` (PB).")
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

    Mode `auto_setup` (default CLI): hook ganda / http:// dirapikan jadi satu
    hook https, HMAC diverifikasi via ping (403 → secret di-sync ulang), lalu
    hasilnya dicatat ke PB collection `repo` (dibuat bila belum ada) dan
    `github_hook`. `auto_setup=False` murni read-only (dipakai MCP).

    Return 0 = sudah terpasang (aktif) atau berhasil di-setup/sync, 1 = belum /
    non-aktif / HMAC salah (dan setup/sync gagal), 2 = akses ditolak / error API.
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
            if auto_setup:
                hook_registry.ensure_repo(api_repo, hook_registry.STATUS_ACCESS_DENIED)
            _print_suggest_install(api_repo, hook_url)
            return 2
        print(f"   ❌ Gagal query hooks: {msg}", file=sys.stderr)
        print()
        _print_manual_web_setup(api_repo, hook_url)
        _maybe_open_browser(_github_webhook_new_url(api_repo))
        return 2

    matches = _find_all_matching(hooks, hook_url)
    if not matches:
        print(f"   ⚠️  Webhook {hook_url} BELUM terpasang di {api_repo}.")
        if hooks:
            urls = [(h.get("config") or {}).get("url", "?") for h in hooks]
            print(f"      Webhook lain yang terpasang: {', '.join(urls)}")
        if auto_setup:
            print()
            return run_cicd_webhook_setup(api_repo, hook_url=hook_url)
        _print_suggest_install(api_repo, hook_url)
        return 1

    removed: List[int] = []
    if auto_setup:
        match, removed = _ensure_single_hook(api_repo, matches, hook_url)
        if match is None:
            return 1
    else:
        match = matches[0]
        extra = [h["id"] for h in matches[1:]]
        url = (match.get("config") or {}).get("url")
        if extra or url != hook_url:
            print(f"   ⚠️  Hook belum rapi: duplikat={extra or '-'} url={url} — "
                  f"rapikan via `bq --cicd-webhook {api_repo}`")

    active = match.get("active", False)
    hook_id = match.get("id", "?")
    events = ",".join(match.get("events") or [])
    if active and auto_setup:
        return _verify_and_register(api_repo, match, hook_url, removed)
    if active and _hook_hmac_rejected(match):
        print(f"   ❌ Webhook aktif (id={hook_id}) tapi delivery terakhir 403 — "
              "HMAC ditolak service.")
        print(f"      Sync secret: bq --cicd-webhook {api_repo}")
        return 1
    if active:
        last = (match.get("last_response") or {}).get("code")
        print(f"   ✅ Webhook sudah terpasang & aktif — id={hook_id} events=[{events}]"
              f" last_response={last or '-'}")
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
            capture_output=True, text=True, check=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired):
        return None
    b64 = res.stdout.strip()
    if not b64:
        return None
    try:
        import base64
        return base64.b64decode(b64).decode("utf-8").strip()
    except Exception:
        return None


def _pb_hmac() -> Optional[str]:
    """HMAC dari PB collection `secrets` (key WEBHOOK_HOOK_HMAC), selalu fresh.

    Bypass cache 15 menit supaya rotasi HMAC di PB langsung terpakai.
    """
    if not pb_api.is_enabled():
        return None
    try:
        val = pb_api.pull(force=True).get(HMAC_ENV_KEY)
    except pb_api.PBAPIError as e:
        print(f"   ⚠️  PB API tidak bisa diakses ({e}) — fallback env/kubectl.",
              file=sys.stderr)
        return None
    return val.strip() if val else None


def _resolve_hmac() -> Tuple[Optional[str], str]:
    """Resolve HMAC: PB collection → env WEBHOOK_HOOK_HMAC / INCOMING_WEBHOOK_HMAC
    → kubectl secret service.

    Nilai yang dipilih divalidasi ke secret service (bila kubectl bisa akses).
    Kalau beda, nilai service yang dipakai — itu yang dipakai verifikasi
    X-Hub-Signature-256, nilai lain pasti berujung 403.

    Return (hmac, sumber). hmac None bila tidak ada sumber yang tersedia.
    """
    val, src = _pb_hmac(), f"PB secrets/{HMAC_ENV_KEY}"
    if not val:
        env_val = os.getenv(HMAC_ENV_KEY) or os.getenv("INCOMING_WEBHOOK_HMAC")
        val, src = (env_val.strip() if env_val else None), "env"

    service = _kubectl_read_secret(HMAC_KUBE_CONTEXT, HMAC_KUBE_NS, HMAC_SERVICE_SECRET)
    if service and val and val != service:
        print(f"   ⚠️  HMAC dari {src} TIDAK cocok dengan secret {HMAC_SERVICE_REF} "
              "→ pakai nilai service.", file=sys.stderr)
        print(f"      Perbarui record PB secrets/{HMAC_ENV_KEY} agar konsisten.",
              file=sys.stderr)
        return service, HMAC_SERVICE_REF
    if service and not val:
        return service, HMAC_SERVICE_REF
    return val, src


def _hook_hmac_rejected(hook: dict) -> bool:
    """Delivery terakhir 403 → service menolak signature (HMAC hook ≠ service)."""
    return (hook.get("last_response") or {}).get("code") == 403


def _sync_hook_secret(api_repo: str, hook: dict, hook_url: str) -> int:
    """Set ulang secret webhook yang sudah ada ke HMAC valid.

    PATCH /repos/{repo}/hooks/{id}/config. Return 0 = berhasil, 1 = gagal.
    """
    hook_id = hook.get("id", "?")
    hmac_secret, src = _resolve_hmac()
    if not hmac_secret:
        print(f"   ⚠️  Tidak bisa ambil HMAC (PB {HMAC_ENV_KEY} / env / kubectl) — "
              "sync dibatalkan.")
        return 1

    payload = json.dumps({
        "url": hook_url,
        "content_type": "json",
        "secret": hmac_secret,
        "insecure_ssl": "0",
    }).encode("utf-8")
    try:
        github_api._request(
            "PATCH", f"/repos/{api_repo}/hooks/{hook_id}/config", body=payload,
        )
    except github_api.GitHubAPIError as e:
        print(f"   ⚠️  Gagal sync secret webhook id={hook_id}: {e}", file=sys.stderr)
        return 1

    print(f"   🔄 Config webhook id={hook_id} di-sync (url {hook_url}, HMAC dari {src}).")
    return 0


PING_WAIT_SECONDS = 20
PING_POLL_INTERVAL = 3


def _verify_ping(api_repo: str, hook_id: int) -> Tuple[str, str]:
    """Ping hook lalu baca hasil delivery-nya → (hmac_status, detail).

    200 → valid, 403 → invalid (service menolak signature), lainnya → unknown
    (mis. timeout jaringan GitHub → cicd-hw.qoin.id, bukan masalah HMAC).
    """
    path = f"/repos/{api_repo}/hooks/{hook_id}"
    try:
        before = {d.get("id") for d in github_api._request("GET", f"{path}/deliveries?per_page=10") or []}
        github_api._request("POST", f"{path}/pings")
    except github_api.GitHubAPIError as e:
        return "unknown", f"ping gagal dikirim: {e}"[:200]

    deadline = time.monotonic() + PING_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(PING_POLL_INTERVAL)
        try:
            items = github_api._request("GET", f"{path}/deliveries?per_page=10") or []
        except github_api.GitHubAPIError:
            continue
        done = [d for d in items if d.get("event") == "ping"
                and d.get("id") not in before and d.get("status_code")]
        if not done:
            continue
        d = done[0]
        code = d.get("status_code")
        if code == 200:
            return "valid", f"ping 200 OK ({d.get('duration') or 0:.2f}s)"
        if code == 403:
            return "invalid", "ping ditolak service: invalid HMAC (403)"
        return "unknown", f"ping {code}: {(d.get('status') or '')[:120]}"
    return "unknown", f"hasil ping belum tersedia setelah {PING_WAIT_SECONDS}s"


def _verify_and_register(
    api_repo: str, hook: dict, hook_url: str, removed: Optional[List[int]] = None,
) -> int:
    """Verifikasi HMAC hook via ping, lalu catat ke PB `repo` & `github_hook`.

    Ping 403 → secret di-sync lalu ping ulang; hasil unknown (gangguan jaringan)
    di-ping ulang sekali. Return 0 = valid/unknown, 1 = HMAC tetap invalid.
    """
    hook_id = hook["id"]
    status, detail = _verify_ping(api_repo, hook_id)
    if status == "invalid":
        print(f"   ❌ Hook id={hook_id}: {detail} — sync secret.")
        if _sync_hook_secret(api_repo, hook, hook_url) == 0:
            status, detail = _verify_ping(api_repo, hook_id)
    elif status == "unknown":
        print(f"   ⚠️  Hook id={hook_id}: {detail} — ping ulang.")
        status, detail = _verify_ping(api_repo, hook_id)

    try:
        hook = github_api._request("GET", f"/repos/{api_repo}/hooks/{hook_id}") or hook
    except github_api.GitHubAPIError:
        pass
    events = ",".join(hook.get("events") or [])
    icon = {"valid": "✅", "invalid": "❌"}.get(status, "⚠️ ")
    print(f"   {icon} Webhook id={hook_id} events=[{events}] — HMAC {status}: {detail}")

    repo_rec = hook_registry.ensure_repo(api_repo, hook_url, hook_url)
    if repo_rec:
        hook_registry.upsert_hook(repo_rec, hook, status, detail, removed or [])
    return 1 if status == "invalid" else 0


def run_cicd_webhook_setup(
    api_repo: str,
    *,
    hook_url: str = HOOK_URL_DEFAULT,
    events: Optional[List[str]] = None,
) -> int:
    """Pasang webhook cicd-hw.qoin.id/hook di repo kalau belum ada.

    Sudah ada → hook ganda / http:// dirapikan jadi satu hook https, lalu skip
    (return 0), kecuali delivery terakhirnya 403 → secret di-sync ulang. Hook
    baru diverifikasi via ping dan dicatat ke PB `repo` + `github_hook`.
    Soft-fail bila error apapun (return 1, print warning, tidak raise). Cocok
    dipanggil dari `--pr-fix` — tidak boleh membatalkan PR flow.

    Return 0 = sudah/berhasil dipasang, 1 = gagal (soft-fail).
    """
    events = events or DEFAULT_EVENTS
    print(f"🪝 Setup webhook {hook_url} di {api_repo}")

    # 1) Cek dulu — skip kalau sudah ada (rapikan duplikat / http dulu)
    try:
        hooks = _list_hooks(api_repo)
    except github_api.GitHubAPIError as e:
        print(f"   ⚠️  Tidak bisa cek webhook: {e}", file=sys.stderr)
        print("   ℹ️  Lewati setup — pasang manual jika perlu.")
        return 1

    matches = _find_all_matching(hooks, hook_url)
    if matches:
        match, _ = _ensure_single_hook(api_repo, matches, hook_url)
        if match is None:
            return 1
        hook_id = match.get("id", "?")
        active = match.get("active", False)
        state = "aktif" if active else "NON-AKTIF"
        if _hook_hmac_rejected(match):
            print(f"   ❌ Sudah terpasang (id={hook_id}, {state}) tapi delivery "
                  "terakhir 403 — HMAC ditolak service.")
            return _sync_hook_secret(api_repo, match, hook_url)
        print(f"   ⏭  Sudah terpasang (id={hook_id}, {state}) — skip.")
        return 0

    # 2) Ambil HMAC (PB collection → env → kubectl secret service)
    hmac_secret, src = _resolve_hmac()
    if not hmac_secret:
        print(f"   ⚠️  Tidak bisa ambil HMAC (PB {HMAC_ENV_KEY} / env / kubectl).")
        print("   ℹ️  Lewati setup — pasang manual dengan:")
        hook_registry.ensure_repo(api_repo, hook_registry.STATUS_NO_WEBHOOK)
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
            hook_registry.ensure_repo(api_repo, hook_registry.STATUS_ACCESS_DENIED)
        print()
        _print_manual_web_setup(api_repo, hook_url)
        _maybe_open_browser(_github_webhook_new_url(api_repo))
        return 1

    hook_id = created.get("id", "?")
    ev = ",".join(created.get("events") or [])
    print(f"   ✅ Webhook dipasang — id={hook_id} events=[{ev}] (HMAC dari {src})")
    return _verify_and_register(api_repo, created, hook_url)
