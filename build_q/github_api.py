"""Native GitHub REST/git wrapper — active when GH_CLI=false.

Uses only Python stdlib (urllib) so no runtime deps are added.
`set_secret` lazily imports pynacl (optional extra: `build-q[native]`).
"""
from __future__ import annotations

import json
import os
import subprocess
from base64 import b64encode
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import get_github_credentials, load_config


class GitHubAPIError(RuntimeError):
    """Raised when a native GitHub API call fails."""


def _token() -> str:
    tok = get_github_credentials()["token"] or os.getenv("GITHUB_TOKEN", "")
    if not tok:
        raise GitHubAPIError(
            "GH_CLI=false but GITHUB_TOKEN is empty. "
            "Set GITHUB_TOKEN in ~/.build-q/.env or export it."
        )
    return tok


def _api_base() -> str:
    return load_config()["github"]["api_base"].rstrip("/")


def _request(
    method: str,
    path: str,
    *,
    accept: str = "application/vnd.github+json",
    body: Optional[bytes] = None,
    raw: bool = False,
) -> Any:
    url = f"{_api_base()}/{path.lstrip('/')}"
    req = Request(url, method=method, data=body)
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "build-q")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req) as resp:
            data = resp.read()
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise GitHubAPIError(f"GitHub API {method} {path} → HTTP {e.code}: {detail}") from e
    except URLError as e:
        raise GitHubAPIError(f"GitHub API {method} {path} failed: {e.reason}") from e
    if raw:
        return data
    if not data:
        return None
    return json.loads(data.decode("utf-8"))


def get_user_login() -> str:
    """Return the authenticated user's login. Falls back to GITHUB_USER env."""
    cached = get_github_credentials()["user"]
    if cached:
        return cached
    data = _request("GET", "/user")
    return data["login"]


def get_contents_raw(repo: str, path: str, ref: str) -> bytes:
    """Fetch a repo file's raw bytes at `ref`."""
    api_path = f"/repos/{repo}/contents/{quote(path)}?ref={quote(ref)}"
    return _request("GET", api_path, accept="application/vnd.github.v3.raw", raw=True)


def get_commit_sha(repo: str, ref: str) -> str:
    """Resolve `ref` (branch/tag/SHA) to a full commit SHA."""
    data = _request("GET", f"/repos/{repo}/commits/{quote(ref)}")
    return data["sha"]


def clone(repo: str, ref: str, *, single_branch: bool = True) -> None:
    """Native `git clone` using the token for HTTPS auth.

    `repo` may be `owner/name`, `owner/name.git`, or a full URL.
    """
    api_repo = normalize_repo(repo)
    token = _token()
    url = f"https://x-access-token:{token}@github.com/{api_repo}.git"
    cmd = ["git", "clone"]
    if single_branch:
        cmd += ["--branch", ref, "--single-branch"]
    cmd += [url]
    # Directory name defaults to the repo's short name (git's default behaviour).
    subprocess.run(cmd, check=True)


def normalize_repo(repo: str) -> str:
    """Reduce any of the input forms to `owner/name` (no .git suffix)."""
    r = repo.strip()
    if r.endswith(".git"):
        r = r[:-4]
    if r.startswith("git@github.com:"):
        r = r.split("git@github.com:", 1)[1]
    if "github.com/" in r:
        r = r.split("github.com/", 1)[1]
    return r


def set_secret(repo: str, name: str, value: str) -> None:
    """Create/update a GitHub Actions repo secret via native REST + libsodium.

    Requires the optional extra `pynacl`:
        pip install 'build-q[native]'
    """
    try:
        from nacl import encoding, public
    except ImportError as e:
        raise GitHubAPIError(
            "Setting secrets natively requires the 'pynacl' package.\n"
            "Install it with: pip install pynacl\n"
            "Or fall back to the gh CLI by setting GH_CLI=true."
        ) from e

    pubkey = _request("GET", f"/repos/{repo}/actions/secrets/public-key")
    box = public.SealedBox(public.PublicKey(pubkey["key"].encode(), encoding.Base64Encoder()))
    encrypted = b64encode(box.encrypt(value.encode("utf-8"))).decode("utf-8")

    body = json.dumps({"encrypted_value": encrypted, "key_id": pubkey["key_id"]}).encode("utf-8")
    _request("PUT", f"/repos/{repo}/actions/secrets/{name}", body=body)


def get_auth_token() -> str:
    """Public accessor mirroring `gh auth token` semantics."""
    return _token()


def list_open_prs(repo: str, head: str) -> list:
    """Return list of open PRs where head branch matches `head` (short name)."""
    # GitHub filter format for cross-fork PRs uses `<owner>:<branch>`; for same-repo,
    # just the branch name works.
    data = _request("GET", f"/repos/{repo}/pulls?state=open&head={quote(head)}")
    return data or []


def create_pull_request(repo: str, *, base: str, head: str, title: str, body: str) -> dict:
    """Open a PR from `head` → `base` on `repo`. Returns the API response dict."""
    payload = json.dumps({
        "title": title,
        "body": body,
        "head": head,
        "base": base,
    }).encode("utf-8")
    return _request("POST", f"/repos/{repo}/pulls", body=payload)
