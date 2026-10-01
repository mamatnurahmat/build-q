"""`bq --cicd-trigger <repo> <ref>` — trigger manual pipeline via webhook cicd-hw.qoin.id/hook.

Kirim synthetic GitHub push event ke incoming-webhook service (di-relay ke
webhook-trigger → Tekton PipelineRun). Berguna saat commit terakhir sudah
di-push tapi webhook GitHub belum terpasang / delivery gagal / ingin re-run
tanpa dummy commit.

HMAC secret diambil dari (prioritas):
    1) env INCOMING_WEBHOOK_HMAC
    2) kubectl <context> -n <ns> get secret <name> -o jsonpath='{.data.hmac}' | base64 -d
       default: context=hw-dev, ns=jenkins-x, name=incoming-webhook
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import github_api

HOOK_URL_DEFAULT = "https://cicd-hw.qoin.id/hook"
KUBE_CONTEXT_DEFAULT = "hw-dev"
SECRET_NS_DEFAULT = "jenkins-x"
SECRET_NAME_DEFAULT = "incoming-webhook"
SECRET_KEY_DEFAULT = "hmac"


class TriggerError(RuntimeError):
    """Raised when manual trigger fails."""


def _get_hmac(
    *,
    kube_context: str,
    ns: str,
    name: str,
    key: str,
) -> str:
    env_val = os.getenv("INCOMING_WEBHOOK_HMAC")
    if env_val:
        return env_val.strip()

    cmd = [
        "kubectl", "--context", kube_context, "-n", ns,
        "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as e:
        raise TriggerError(
            "kubectl tidak ditemukan. Install kubectl atau set env INCOMING_WEBHOOK_HMAC."
        ) from e
    except subprocess.CalledProcessError as e:
        raise TriggerError(
            f"Gagal ambil secret {ns}/{name} di context {kube_context}: {e.stderr.strip()}"
        ) from e

    b64 = res.stdout.strip()
    if not b64:
        raise TriggerError(
            f"Secret {ns}/{name} key '{key}' kosong di context {kube_context}."
        )
    import base64
    try:
        return base64.b64decode(b64).decode("utf-8").strip()
    except Exception as e:
        raise TriggerError(f"Decode base64 secret gagal: {e}") from e


def _fetch_repo_meta(api_repo: str) -> dict:
    return github_api._request("GET", f"/repos/{api_repo}")


def _fetch_commit(api_repo: str, ref: str) -> dict:
    from urllib.parse import quote
    return github_api._request("GET", f"/repos/{api_repo}/commits/{quote(ref)}")


def _build_payload(
    api_repo: str,
    branch: str,
    commit: dict,
    repo_meta: dict,
    *,
    pusher_name: str,
    pusher_email: str,
) -> dict:
    sha = commit["sha"]
    message = (commit.get("commit") or {}).get("message", "manual trigger via webhook")
    author = (commit.get("commit") or {}).get("author") or {}
    a_name = author.get("name") or pusher_name
    a_email = author.get("email") or pusher_email
    ts = author.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    owner = repo_meta.get("owner") or {}
    author_block = {
        "name": a_name,
        "email": a_email,
        "username": pusher_name,
    }
    commit_block = {
        "id": sha,
        "tree_id": sha,
        "message": message,
        "timestamp": ts,
        "author": author_block,
        "committer": author_block,
        "added": [],
        "removed": [],
        "modified": [],
    }

    return {
        "ref": f"refs/heads/{branch}",
        "before": sha,
        "after": sha,
        "created": False,
        "deleted": False,
        "forced": True,
        "compare": f"{repo_meta.get('html_url','')}/compare/{sha}...{sha}",
        "repository": {
            "id": repo_meta.get("id"),
            "name": repo_meta.get("name"),
            "full_name": repo_meta.get("full_name") or api_repo,
            "default_branch": repo_meta.get("default_branch", "main"),
            "html_url": repo_meta.get("html_url"),
            "clone_url": repo_meta.get("clone_url"),
            "ssh_url": repo_meta.get("ssh_url"),
            "owner": {
                "login": owner.get("login"),
                "id": owner.get("id"),
                "type": owner.get("type", "Organization"),
                "name": owner.get("login"),
            },
        },
        "pusher": {"name": pusher_name, "email": pusher_email},
        "sender": {"login": pusher_name, "id": 1, "type": "User"},
        "head_commit": commit_block,
        "commits": [commit_block],
    }


def _sign(body: bytes, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def _post(hook_url: str, body: bytes, sig: str, delivery: str, force: bool = False) -> tuple[int, dict | str]:
    req = Request(hook_url, method="POST", data=body)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-GitHub-Event", "push")
    req.add_header("X-GitHub-Delivery", delivery)
    req.add_header("X-Hub-Signature-256", sig)
    req.add_header("User-Agent", "build-q/cicd-trigger")
    if force:
        req.add_header("X-Force-Trigger", "1")
    try:
        with urlopen(req) as resp:
            code = resp.getcode()
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        code = e.code
        raw = e.read().decode("utf-8", errors="replace")
    except URLError as e:
        raise TriggerError(f"POST {hook_url} gagal: {e.reason}") from e
    try:
        return code, json.loads(raw)
    except json.JSONDecodeError:
        return code, raw


def run_cicd_trigger(
    api_repo: str,
    ref: str,
    *,
    hook_url: str = HOOK_URL_DEFAULT,
    kube_context: str = KUBE_CONTEXT_DEFAULT,
    secret_ns: str = SECRET_NS_DEFAULT,
    secret_name: str = SECRET_NAME_DEFAULT,
    secret_key: str = SECRET_KEY_DEFAULT,
    sha_override: Optional[str] = None,
    pusher: Optional[str] = None,
    email: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    """Trigger manual pipeline untuk `api_repo` di branch `ref`.

    Return 0 = triggered (200 OK + relay 2xx), 1 = webhook accepted but relay failed,
    2 = network/HMAC/API error.

    force=True → kirim header `X-Force-Trigger: 1` yang di-relay ke webhook-trigger
    untuk bypass middleware dedup (delete record lama + claim baru).
    """
    print(f"🚀 Manual trigger: {api_repo} @ {ref} → {hook_url}" + ("  [FORCE]" if force else ""))

    try:
        repo_meta = _fetch_repo_meta(api_repo)
    except github_api.GitHubAPIError as e:
        print(f"❌ Fetch repo meta gagal: {e}", file=sys.stderr)
        return 2

    try:
        commit = _fetch_commit(api_repo, sha_override or ref)
    except github_api.GitHubAPIError as e:
        print(f"❌ Fetch commit '{ref}' gagal: {e}", file=sys.stderr)
        return 2

    sha = commit["sha"]
    print(f"   commit: {sha[:12]} — {((commit.get('commit') or {}).get('message') or '').splitlines()[0][:70]}")

    if not pusher:
        try:
            pusher = github_api.get_user_login()
        except github_api.GitHubAPIError:
            pusher = "manual-trigger"
    if not email:
        email = f"{pusher}@users.noreply.github.com"

    payload = _build_payload(
        api_repo, ref, commit, repo_meta,
        pusher_name=pusher, pusher_email=email,
    )
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    try:
        secret = _get_hmac(
            kube_context=kube_context, ns=secret_ns,
            name=secret_name, key=secret_key,
        )
    except TriggerError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    sig = _sign(body, secret)
    delivery = str(uuid.uuid4())

    if dry_run:
        print(f"   [dry-run] delivery={delivery}")
        print(f"   [dry-run] X-Hub-Signature-256: {sig}")
        print(f"   [dry-run] body ({len(body)} bytes) — payload keys: {list(payload.keys())}")
        print(f"   [dry-run] POST {hook_url}  (skipped)")
        return 0

    print(f"   delivery: {delivery}")
    try:
        code, resp = _post(hook_url, body, sig, delivery, force=force)
    except TriggerError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    if code != 200:
        print(f"❌ Webhook menolak — HTTP {code}", file=sys.stderr)
        print(f"   response: {resp}", file=sys.stderr)
        return 2

    if not isinstance(resp, dict):
        print(f"⚠️  Response bukan JSON: {resp}")
        return 1

    ok = resp.get("ok") is True
    result = resp.get("result") or {}
    commit_res = (result.get("commit") or {}) if isinstance(result, dict) else {}
    relay = (result.get("relay") or {}) if isinstance(result, dict) else {}
    relay_http = relay.get("http")
    pipelinerun = ((relay.get("response") or {}).get("pipelinerun")) if isinstance(relay.get("response"), dict) else None

    if ok:
        print(f"✅ Webhook accepted — event={resp.get('event')} branch={commit_res.get('branch')} sha={commit_res.get('sha','')[:12]}")
    else:
        print(f"⚠️  Webhook 200 tapi ok!=true: {resp}")

    if pipelinerun:
        print(f"   pipelinerun: {pipelinerun}")
        print(f"   verify: kubectl --context {kube_context} -n {secret_ns} get pipelinerun {pipelinerun}")

    if relay_http and int(relay_http) >= 400:
        print(f"⚠️  Relay ke webhook-trigger gagal — HTTP {relay_http}")
        return 1

    return 0 if ok else 1
