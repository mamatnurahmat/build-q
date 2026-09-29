"""`bq --gitops-set-image <repo> <branch> <path> <image_full>` — update
image tag di deployment YAML repo GitOps, commit + push langsung ke branch
(tanpa PR). Menggantikan script bash eksternal `gitops-set-image`.

Alur:
  1. Pre-flight (fail fast, tanpa clone):
       - Format path .yaml
       - Format image (harus `name:tag`)
       - File exist di GitHub raw (native REST)
       - Image ready di Docker Hub (Registry v2)
       - Duplikasi (image di file sama dgn arg → skip, exit 0)
  2. Clone repo (shallow, branch spesifik) ke /tmp
  3. `sed`-style replace `image: <base>:...` → `image: <image_full>` (regex)
  4. Commit + push origin/<branch>

Exit code:
  0 = sukses (atau tidak ada perubahan)
  1 = validasi gagal
  2 = git error (clone/push ditolak)
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from . import github_api


# ── Docker Hub image-ready check (Registry v2 + Hub API fallback) ────────

def _image_is_ready(
    image_full: str,
    *,
    dockerhub_user: Optional[str] = None,
    dockerhub_password: Optional[str] = None,
    timeout: int = 15,
) -> bool:
    """Cek apakah tag image ada di Docker Hub. Mirror perilaku bash `image_is_ready`."""
    if ":" not in image_full:
        return False
    img_name, img_tag = image_full.rsplit(":", 1)
    if "/" not in img_name:
        img_name = f"library/{img_name}"

    # Step 1: Bearer token
    token_url = (
        "https://auth.docker.io/token"
        f"?service=registry.docker.io&scope=repository:{img_name}:pull"
    )
    req = urllib.request.Request(token_url)
    if dockerhub_user and dockerhub_password:
        creds = base64.b64encode(
            f"{dockerhub_user}:{dockerhub_password}".encode()
        ).decode()
        req.add_header("Authorization", f"Basic {creds}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            token = json.loads(r.read()).get("token", "")
    except Exception:
        return False
    if not token:
        return False

    # Step 2: check manifest
    manifest_url = f"https://registry-1.docker.io/v2/{img_name}/manifests/{img_tag}"
    accept = ", ".join([
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ])
    req = urllib.request.Request(
        manifest_url,
        headers={"Authorization": f"Bearer {token}", "Accept": accept},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        # 401/404 kadang muncul untuk tag yang tidak ada di Docker Hub.
        # Cross-check via Hub API (public endpoint).
        if e.code in (401, 404):
            hub_url = f"https://hub.docker.com/v2/repositories/{img_name}/tags/{img_tag}"
            try:
                with urllib.request.urlopen(hub_url, timeout=timeout) as r2:
                    return r2.status == 200
            except Exception:
                return False
        return False
    except Exception:
        return False


# ── Extract "image: <val>" dari YAML text (grep-style, first match) ──────

_IMAGE_LINE_RE = re.compile(r"^\s*(?:-\s+)?image:\s*(\S+)\s*$", re.MULTILINE)


def _extract_first_image(yaml_text: str) -> Optional[str]:
    m = _IMAGE_LINE_RE.search(yaml_text)
    return m.group(1) if m else None


def _replace_image(yaml_text: str, base_prefix: str, new_full: str) -> str:
    """Replace `image: <base_prefix>:*` → `image: <new_full>` (first-match line).

    Menggunakan regex yang menghormati indentasi asli.
    """
    pattern = re.compile(
        r"^(?P<indent>\s*(?:-\s+)?)image:\s*" + re.escape(base_prefix) + r":\S+\s*$",
        re.MULTILINE,
    )
    return pattern.sub(lambda m: f"{m.group('indent')}image: {new_full}", yaml_text, count=1)


# ── Main flow ────────────────────────────────────────────────────────────

def run_gitops_set_image(
    repo: str,
    branch: str,
    path: str,
    image_full: str,
) -> int:
    print("")
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║ bq --gitops-set-image: update image di GitOps               ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f" Repo:   {repo}")
    print(f" Branch: {branch}")
    print(f" File:   {path}")
    print(f" Image:  {image_full}")
    print("")

    # ── Preflight #1: format image ────────────────────────────────
    if ":" not in image_full:
        print("❌ Format image_full harus name:tag (contoh: loyaltolpi/repo:v1.0.0)",
              file=sys.stderr)
        return 1
    image_name, image_tag = image_full.rsplit(":", 1)
    if not image_name or not image_tag:
        print(f"❌ Image name / tag kosong: '{image_full}'", file=sys.stderr)
        return 1

    # ── Preflight #2: format path ─────────────────────────────────
    if not path.endswith(".yaml"):
        print("❌ File path harus berakhiran .yaml", file=sys.stderr)
        print(f"   Diberikan: {path}")
        print("   Contoh benar: cce/production-qoin/plus-be-example_deployment.yaml")
        return 1

    # ── Preflight #3: file exist di GitHub ────────────────────────
    print(f"ℹ️  Pre-flight #1: cek {path} di {repo}@{branch} ...")
    try:
        gh_raw = github_api.get_contents_raw(repo, path, branch).decode("utf-8")
    except github_api.GitHubAPIError as e:
        print(f"❌ File TIDAK ditemukan di {repo}:{branch}", file=sys.stderr)
        print(f"   Path: {path}")
        print(f"   ({e})")
        return 1
    print(f"✅ File ditemukan.\n")

    # ── Preflight #4: image ready di Docker Hub ───────────────────
    print(f"ℹ️  Pre-flight #2: cek {image_full} di Docker Hub ...")
    import os
    if not _image_is_ready(
        image_full,
        dockerhub_user=os.getenv("DOCKERHUB_USER"),
        dockerhub_password=os.getenv("DOCKERHUB_PASSWORD"),
    ):
        print(f"❌ Image {image_full} TIDAK READY di Docker Hub.", file=sys.stderr)
        print("   Kemungkinan: belum di-push, tag salah, atau nama image salah.")
        return 1
    print(f"✅ Image ready.\n")

    # ── Preflight #5: cek duplikasi (image sudah sama) ────────────
    current_online = _extract_first_image(gh_raw)
    if current_online == image_full:
        print(f"⚠️  Image di {path} sudah = {image_full}. Skip clone/commit/push.")
        return 0
    print(f"ℹ️  Image di repo: {current_online or '<tidak ditemukan>'}")
    print(f"ℹ️  Image baru:    {image_full}\n")

    # ── Step 1/4: clone ───────────────────────────────────────────
    if not shutil.which("git"):
        print("❌ git tidak ditemukan di PATH.", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="bq-gitops-set-image-"))
    repo_dir = workdir / "repo"
    try:
        print(f"ℹ️  Step 1/4: clone {repo}@{branch} ke {repo_dir} ...")
        clone_url = f"https://github.com/{repo}.git"
        try:
            token = github_api.get_auth_token()
            clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
        except github_api.GitHubAPIError:
            pass  # anon clone (public repo)

        rc = subprocess.call(
            ["git", "clone", "--depth", "1", "--branch", branch,
             clone_url, str(repo_dir)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if rc != 0:
            print(f"⚠️  Shallow clone gagal, coba full clone ...")
            rc = subprocess.call(["git", "clone", clone_url, str(repo_dir)])
            if rc != 0:
                print(f"❌ Clone gagal.", file=sys.stderr)
                return 2
            rc = subprocess.call(
                ["git", "-C", str(repo_dir), "checkout", branch],
            )
            if rc != 0:
                print(f"❌ Branch '{branch}' tidak ditemukan.", file=sys.stderr)
                return 2

        head = subprocess.run(
            ["git", "-C", str(repo_dir), "log", "--oneline", "-1"],
            capture_output=True, text=True,
        ).stdout.strip()
        print(f"✅ Repo cloned. HEAD: {head}\n")

        # ── Step 2/4: update image ──────────────────────────────────
        deploy_file = repo_dir / path
        print(f"ℹ️  Step 2/4: update image tag ...")
        text = deploy_file.read_text()
        current = _extract_first_image(text)
        if not current:
            print(f"❌ Tidak menemukan 'image:' di {path}", file=sys.stderr)
            return 1
        if current == image_full:
            print(f"⚠️  Image di {path} sudah = {image_full}. Skip commit/push.")
            return 0
        print(f"   image lama: {current}")
        print(f"   image baru: {image_full}")

        base_prefix = current.rsplit(":", 1)[0] if ":" in current else current
        new_text = _replace_image(text, base_prefix, image_full)
        if new_text == text:
            print(f"❌ Gagal replace image (regex tidak match).", file=sys.stderr)
            return 1
        deploy_file.write_text(new_text)

        updated = _extract_first_image(new_text)
        if updated != image_full:
            print(f"❌ Verifikasi gagal — image di file: {updated}", file=sys.stderr)
            return 1
        print(f"✅ image diupdate ke: {updated}\n")

        # ── Step 3/4: commit ────────────────────────────────────────
        print(f"ℹ️  Step 3/4: commit ...")
        subprocess.check_call(
            ["git", "-C", str(repo_dir), "add", path],
        )
        diff_check = subprocess.call(
            ["git", "-C", str(repo_dir), "diff", "--cached", "--quiet"],
        )
        if diff_check == 0:
            print(f"⚠️  Tidak ada perubahan setelah add.")
            return 0

        commit_title = f"chore: update image {Path(path).name} to {image_full}"
        commit_body = (
            f"Repo: {repo}\n"
            f"Branch: {branch}\n"
            f"File: {path}\n"
            f"Image: {current} → {image_full}"
        )
        rc = subprocess.call(
            ["git", "-C", str(repo_dir), "commit",
             "-m", commit_title, "-m", commit_body],
        )
        if rc != 0:
            print(f"❌ git commit gagal.", file=sys.stderr)
            return 2
        print(f"✅ Commit: {commit_title}\n")

        # ── Step 4/4: push ──────────────────────────────────────────
        print(f"ℹ️  Step 4/4: push ke origin/{branch} ...")
        rc = subprocess.call(
            ["git", "-C", str(repo_dir), "push", "origin", branch],
        )
        if rc != 0:
            print(f"❌ Push gagal. Mungkin branch diproteksi atau ada perubahan baru.",
                  file=sys.stderr)
            print(f"   Coba manual: cd {repo_dir} && git pull && git push origin {branch}")
            return 2

        print(f"\n✅ Selesai — {current} → {image_full} pushed ke {repo}:{branch}")
        return 0

    finally:
        shutil.rmtree(workdir, ignore_errors=True)
