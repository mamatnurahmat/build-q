"""`bq --doctor` — cek kesiapan tools & credentials.

Preflight ringan untuk memastikan environment siap dipakai `bq`:
  1. Tools esensial (git, docker, docker buildx) + opsional (kubectl, gh)
  2. Config `~/.build-q/.env` ada + field kunci terisi (REGISTRY_URL, GITHUB_ORG)
  3. Credentials:
       - GITHUB_TOKEN valid (GET /user)  — atau `gh auth status` bila GH_CLI=true
       - Docker login (~/.docker/config.json auths) — untuk push image
  4. Buildx builder (BUILDER_NAME) exist di `docker buildx ls`
  5. kubectl context JX_KUBE_CONTEXT reachable (opsional — dipakai HMAC/JX secret)

Exit code:
  0 = semua cek esensial lulus (warning tidak menggagalkan)
  1 = ada blocker (mis. git/docker tidak terpasang, GITHUB_TOKEN kosong)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

from .config import ENV_FILE, init_config, load_config, save_env_value, use_gh_cli


# (label, ok, detail, is_blocker)
Result = Tuple[str, bool, str, bool]


def _which(cmd: str) -> str:
    return shutil.which(cmd) or ""


def _run(cmd: List[str], timeout: int = 5) -> Tuple[int, str, str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res.returncode, res.stdout.strip(), res.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)


def _icon(ok: bool, blocker: bool) -> str:
    if ok:
        return "✅"
    return "❌" if blocker else "⚠️ "


def _check_tools() -> List[Result]:
    out: List[Result] = []

    # Esensial
    git = _which("git")
    out.append(("git", bool(git), git or "not found in PATH", True))

    docker = _which("docker")
    if docker:
        rc, ver, _ = _run(["docker", "--version"])
        out.append(("docker", rc == 0, ver or docker, True))
    else:
        out.append(("docker", False, "not found in PATH", True))

    # buildx = subcommand docker
    if docker:
        rc, ver, _ = _run(["docker", "buildx", "version"])
        out.append(("docker buildx", rc == 0, ver.splitlines()[0] if ver else "buildx plugin missing", True))
    else:
        out.append(("docker buildx", False, "requires docker", True))

    # Opsional
    kubectl = _which("kubectl")
    out.append(("kubectl (opsional)", bool(kubectl), kubectl or "not installed — apply-secret & HMAC fetch skip", False))

    if use_gh_cli():
        gh = _which("gh")
        out.append(("gh CLI (GH_CLI=true)", bool(gh), gh or "GH_CLI=true tapi gh tidak ada", True))

    return out


def _check_config() -> List[Result]:
    out: List[Result] = []
    exists = ENV_FILE.exists()
    out.append((f"config file {ENV_FILE}", exists, "ok" if exists else "missing (jalankan `bq --init`)", True))
    if not exists:
        return out

    cfg = load_config()
    reg = (cfg["registry"]["url"] or "").strip()
    reg_ok = bool(reg) and reg != "registry.example.com"
    out.append(("REGISTRY_URL", reg_ok, reg or "empty", False))

    dh_org = cfg["dockerhub"]["org"]
    out.append(("DOCKERHUB_ORG (opsional)", bool(dh_org), dh_org or "empty — fallback REGISTRY_URL", False))

    gh_org = cfg["git"]["org"]
    out.append(("GITHUB_ORG (opsional)", bool(gh_org), gh_org or "empty — shorthand `bq <repo>` off", False))

    return out


def _check_github_credentials() -> List[Result]:
    out: List[Result] = []

    if use_gh_cli():
        gh = _which("gh")
        if not gh:
            out.append(("gh auth", False, "gh not installed", True))
            return out
        rc, so, se = _run(["gh", "auth", "status"], timeout=8)
        msg = (so or se).splitlines()[0] if (so or se) else ""
        out.append(("gh auth status", rc == 0, msg or ("ok" if rc == 0 else "not logged in"), True))
        return out

    cfg = load_config()
    token = cfg["github"]["token"] or os.getenv("GITHUB_TOKEN", "")
    if not token:
        out.append(("GITHUB_TOKEN", False, "empty (set di ~/.build-q/.env atau export)", True))
        return out

    # Validate token dengan panggil /user (native REST).
    try:
        from . import github_api
        data = github_api._request("GET", "/user")
        login = data.get("login", "?") if isinstance(data, dict) else "?"
        out.append(("GITHUB_TOKEN", True, f"valid — login={login}", True))
    except Exception as e:
        out.append(("GITHUB_TOKEN", False, f"invalid: {str(e)[:80]}", True))

    return out


def _check_buildx_builder() -> List[Result]:
    cfg = load_config()
    name = cfg["builder"]["name"]
    if not _which("docker"):
        return [(f"buildx builder '{name}'", False, "requires docker", False)]
    rc, so, se = _run(["docker", "buildx", "inspect", name], timeout=8)
    if rc == 0:
        first = (so.splitlines() or [""])[0]
        return [(f"buildx builder '{name}'", True, first or "exists", False)]
    hint = "buat via `bq --init` (bootstrap)"
    return [(f"buildx builder '{name}'", False, f"missing — {hint}", False)]


def _check_docker_login() -> List[Result]:
    cfg = load_config()
    reg_hint = cfg["dockerhub"]["org"] or cfg["registry"]["url"]
    path = Path.home() / ".docker" / "config.json"
    if not path.exists():
        return [("docker login", False, f"{path} missing — `docker login`", False)]
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        return [("docker login", False, f"{path} invalid JSON", False)]
    auths = data.get("auths") or {}
    if not auths:
        return [("docker login", False, "no auths entries — `docker login`", False)]
    hosts = ", ".join(sorted(auths.keys()))
    detail = f"{len(auths)} host(s): {hosts}"
    if reg_hint:
        detail += f"  (target hint: {reg_hint})"
    return [("docker login", True, detail, False)]


def _check_kube_context() -> List[Result]:
    if not _which("kubectl"):
        return []  # sudah di-report di _check_tools
    cfg = load_config()
    ctx = cfg["webhook"]["k8s_context"]
    if not ctx:
        return []
    rc, so, se = _run(
        ["kubectl", "--context", ctx, "get", "ns", cfg["webhook"]["k8s_namespace"],
         "-o", "name"],
        timeout=6,
    )
    if rc == 0:
        return [(f"kubectl context '{ctx}' (opsional)", True, so or "reachable", False)]
    msg = (se or "unreachable").splitlines()[0]
    return [(f"kubectl context '{ctx}' (opsional)", False, msg[:100], False)]


def _ask_yn(prompt: str, default_no: bool = True) -> bool:
    """TTY-only yes/no prompt. Enter = default (no)."""
    suffix = " [y/N]: " if default_no else " [Y/n]: "
    try:
        ans = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not ans:
        return not default_no
    return ans in {"y", "yes"}


def _ask_input(prompt: str, secret: bool = False) -> str:
    """TTY input; masks with getpass when `secret=True`."""
    try:
        if secret:
            import getpass
            return getpass.getpass(prompt).strip()
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _fix_config_missing() -> bool:
    print("   ▸ Membuat ~/.build-q/.env dengan default ...")
    init_config()
    return ENV_FILE.exists()


def _fix_env_value(key: str, hint: str = "") -> bool:
    prompt = f"   Isi {key}"
    if hint:
        prompt += f" ({hint})"
    prompt += ": "
    val = _ask_input(prompt)
    if not val:
        print(f"   ⏭️  Skip — {key} tidak diisi.")
        return False
    save_env_value(key, val)
    print(f"   ✅ {key} disimpan ke {ENV_FILE}")
    return True


def _fix_github_token() -> bool:
    if use_gh_cli():
        gh = _which("gh")
        if not gh:
            print("   ⚠️  gh CLI tidak ada. Set GH_CLI=false lalu isi GITHUB_TOKEN, atau install gh.")
            return False
        print("   ▸ Menjalankan `gh auth login` (interaktif) ...")
        rc = subprocess.call([gh, "auth", "login"])
        return rc == 0
    token = _ask_input(
        "   Paste GITHUB_TOKEN (scope: repo, workflow, read:user) — input disembunyikan: ",
        secret=True,
    )
    if not token:
        print("   ⏭️  Skip — token kosong.")
        return False
    save_env_value("GITHUB_TOKEN", token)
    # Re-validate
    try:
        from . import github_api
        data = github_api._request("GET", "/user")
        login = data.get("login", "?") if isinstance(data, dict) else "?"
        print(f"   ✅ GITHUB_TOKEN valid — login={login}")
        return True
    except Exception as e:
        print(f"   ❌ Token belum valid: {str(e)[:80]}")
        return False


def _fix_docker_login() -> bool:
    cfg = load_config()
    registry = cfg["dockerhub"]["org"] or cfg["registry"]["url"] or ""
    # `docker login` menerima host; org saja (mis. `loyaltolpi`) akan ditolak,
    # jadi kalau bukan host, jalankan tanpa argumen (default Docker Hub).
    args = ["docker", "login"]
    if registry and ("." in registry or ":" in registry):
        args.append(registry)
    print(f"   ▸ Menjalankan `{' '.join(args)}` (interaktif) ...")
    rc = subprocess.call(args)
    return rc == 0


def _fix_buildx_builder() -> bool:
    cfg = load_config()
    name = cfg["builder"]["name"]
    print(f"   ▸ Bootstrap buildx builder '{name}' ...")
    from .builder import ensure_builder
    ok = ensure_builder(name, bootstrap=True)
    return ok


# label-prefix → (fixer, description). Prefix supaya label ber-suffix
# dinamis (mis. "buildx builder 'mybuilder'") tetap match.
_FIXERS = [
    ("config file",             _fix_config_missing,                       "buat config default"),
    ("REGISTRY_URL",            lambda: _fix_env_value("REGISTRY_URL", "mis. loyaltolpi"), "isi REGISTRY_URL"),
    ("DOCKERHUB_ORG",           lambda: _fix_env_value("DOCKERHUB_ORG", "mis. loyaltolpi"), "isi DOCKERHUB_ORG"),
    ("GITHUB_ORG",              lambda: _fix_env_value("GITHUB_ORG", "mis. Qoin-Digital-Indonesia"), "isi GITHUB_ORG"),
    ("GITHUB_TOKEN",            _fix_github_token,                         "paste token / gh auth login"),
    ("gh auth status",          _fix_github_token,                         "gh auth login"),
    ("docker login",            _fix_docker_login,                         "docker login"),
    ("buildx builder",          _fix_buildx_builder,                       "bootstrap builder"),
]


def _find_fixer(label: str):
    for prefix, fn, desc in _FIXERS:
        if label.startswith(prefix):
            return fn, desc
    return None, None


def _run_fixers(sections) -> int:
    """Prompt user for each failing check yang punya fixer. Return jumlah fix sukses."""
    if not sys.stdin.isatty():
        return 0
    fixable = []
    for _, results in sections:
        for r in results:
            label, ok, _detail, _blocker = r
            if ok:
                continue
            fn, desc = _find_fixer(label)
            if fn:
                fixable.append((label, fn, desc))
    if not fixable:
        return 0

    print("🛠  Ada item yang bisa diperbaiki. Jawab per item (Enter = skip).\n")
    fixed = 0
    for label, fn, desc in fixable:
        if not _ask_yn(f"   Fix '{label}' — {desc}?"):
            continue
        try:
            if fn():
                fixed += 1
        except Exception as e:
            print(f"   ❌ Gagal fix '{label}': {e}")
    if fixed:
        print(f"\n✨ {fixed} item diperbaiki. Jalankan `bq --doctor` lagi untuk verifikasi.")
    return fixed


def run_doctor() -> int:
    """Jalankan semua cek. Return 0 bila blocker-free, 1 bila ada blocker."""
    print("🩺 bq doctor — cek kesiapan tools & credentials\n")

    sections = [
        ("Tools", _check_tools()),
        ("Config", _check_config()),
        ("GitHub credentials", _check_github_credentials()),
        ("Docker registry", _check_docker_login()),
        ("Buildx builder", _check_buildx_builder()),
        ("Kubernetes (opsional)", _check_kube_context()),
    ]

    blockers = 0
    warnings = 0
    passes = 0
    for title, results in sections:
        if not results:
            continue
        print(f"▸ {title}")
        for label, ok, detail, blocker in results:
            print(f"   {_icon(ok, blocker)} {label:38} {detail}")
            if ok:
                passes += 1
            elif blocker:
                blockers += 1
            else:
                warnings += 1
        print()

    print(f"📊 Summary: {passes} ok, {warnings} warning, {blockers} blocker")
    if blockers:
        print("\n💡 Fix blocker dulu sebelum jalankan `bq` build / --pr-fix / --bootstrap-k8s.")

    if blockers or warnings:
        print()
        _run_fixers(sections)

    return 1 if blockers else 0
