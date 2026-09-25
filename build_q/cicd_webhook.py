"""`bq --cicd-webhook <repo>` — cek webhook GitHub cicd-hw.qoin.id/hook.

Cek apakah repo sudah terpasang webhook ke `https://cicd-hw.qoin.id/hook`
(incoming-webhook service). Kalau belum, print saran perintah untuk memasang.

Bisa dipanggil sendiri (`bq --cicd-webhook <repo>`) atau berbarengan dengan
`--pr-fix` (`bq --pr-fix <repo> <ref> --cicd-webhook`) — cek dijalankan setelah
pr-fix sukses.
"""
from __future__ import annotations

import subprocess
import sys
from typing import List, Optional

from . import github_api
from .config import use_gh_cli

HOOK_URL_DEFAULT = "https://cicd-hw.qoin.id/hook"


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


def _print_suggest_install(api_repo: str, hook_url: str) -> None:
    repo_short = api_repo.split("/")[-1]
    print("💡 Cara memasang webhook:")
    print(f"   1) CLI (butuh gh admin:repo_hook + kubectl access ke cluster jenkins-x):")
    print(f"      kubectl config use-context hw-dev")
    print(f"      cicd-webhook create {repo_short}")
    print(f"   2) Alternatif via API self-service (butuh HMAC secret):")
    print(f"      SECRET=$(cicd-webhook hmac | awk '{{print $NF}}')")
    print(f"      BODY='{{\"repo\":\"{repo_short}\"}}'")
    print(f"      SIG=\"sha256=$(printf %s \"$BODY\" | openssl dgst -sha256 -hmac \"$SECRET\" | awk '{{print $2}}')\"")
    print(f"      curl -X POST {hook_url}/register \\")
    print(f"           -H 'Content-Type: application/json' \\")
    print(f"           -H \"X-Hub-Signature-256: $SIG\" \\")
    print(f"           -d \"$BODY\"")


def run_cicd_webhook_check(api_repo: str, *, hook_url: str = HOOK_URL_DEFAULT) -> int:
    """Cek apakah webhook `hook_url` sudah terpasang di `api_repo`.

    Return 0 = sudah terpasang (aktif), 1 = belum / non-aktif (saran ditampilkan),
    2 = akses ditolak / error API.
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
        return 2

    match = _find_matching(hooks, hook_url)
    if match is None:
        print(f"   ⚠️  Webhook {hook_url} BELUM terpasang di {api_repo}.")
        if hooks:
            urls = [(h.get("config") or {}).get("url", "?") for h in hooks]
            print(f"      Webhook lain yang terpasang: {', '.join(urls)}")
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
    print("      Aktifkan lagi via:")
    print(f"      cicd-webhook update {repo_short} {hook_id}")
    return 1
