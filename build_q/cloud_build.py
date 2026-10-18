"""`bq <repo> <ref> --cloud` — build & push via Docker Build Cloud (produk Docker Hub).

Builder cloud dipakai lewat driver `cloud` pada buildx. Buildx yang dibundel
OrbStack/Homebrew tidak memuat driver itu (`failed to find driver "cloud"`), jadi
`bq` memanggil buildx resmi (>= v0.38.0, sudah diuji) langsung dari laptop.
Engine lokal (OrbStack, Docker Desktop, colima) hanya dipakai untuk `docker login`.

Alur:
  1. cari buildx resmi (`--cloud-buildx` / `BQ_BUILDX_BIN` / `~/.build-q/bin/docker-buildx`),
  2. ambil kredensial Docker Hub dari PocketBase (owner-only, lihat `bq auth get`),
  3. login ke `DOCKER_CONFIG` sementara (token lewat stdin, tidak masuk argv),
  4. pastikan builder cloud ada (dibuat bila belum),
  5. `buildx build --builder <cloud> --push` dari working directory,
  6. hapus config sementara (logout).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import builder as _builder
from . import pb_api, pb_auth
from .builder import BuildError
from .config import CONFIG_DIR, load_config

DEFAULT_CLOUD_ENDPOINT = "loyaltolpi/qoin-build"  # <org>/<builder> di Docker Build Cloud
MIN_BUILDX_HINT = "v0.38.0"


class CloudBuildError(BuildError):
    pass


def endpoint_to_local_name(endpoint: str) -> str:
    """`loyaltolpi/qoin-build` -> `cloud-loyaltolpi-qoin-build` (nama builder lokal buildx)."""
    return "cloud-" + endpoint.replace("/", "-")


def resolve_endpoint(override: str | None = None) -> str:
    ep = override or os.getenv("CLOUD_BUILDER") or DEFAULT_CLOUD_ENDPOINT
    if ep.count("/") != 1 or not all(ep.split("/")):
        raise CloudBuildError(f"--cloud-builder harus berbentuk <org>/<builder>, dapat: {ep!r}")
    return ep


def _supports_cloud(path: str) -> bool:
    try:
        out = subprocess.run([path, "create", "--help"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return '"cloud"' in (out.stdout + out.stderr)


def resolve_buildx(override: str | None = None) -> str:
    """Path buildx resmi yang mendukung driver cloud. Raise bila tidak ditemukan."""
    candidates = [override, os.getenv("BQ_BUILDX_BIN"), str(CONFIG_DIR / "bin" / "docker-buildx")]
    for c in candidates:
        if not c:
            continue
        p = Path(c).expanduser()
        if p.is_file() and os.access(p, os.X_OK) and _supports_cloud(str(p)):
            return str(p)
    raise CloudBuildError(
        "buildx resmi dengan driver cloud tidak ditemukan. buildx bawaan OrbStack/Homebrew tidak "
        f"memuat driver `cloud`. Pasang buildx resmi {MIN_BUILDX_HINT} ke "
        f"{CONFIG_DIR / 'bin' / 'docker-buildx'} (atau set BQ_BUILDX_BIN / --cloud-buildx)."
    )


def load_dockerhub_creds() -> tuple[str, str]:
    """(username, token) Docker Hub. Env (hasil hydrate PB) lebih dulu, lalu PB langsung."""
    user = os.getenv("DOCKERHUB_USER") or ""
    token = os.getenv("DOCKERHUB_TOKEN") or ""
    if not (user and token):
        try:
            base = pb_api._base_url()
            tok = pb_api.get_token(base)
            user = user or pb_auth.secret_bytes(
                pb_auth.get_secret(base, tok, "DOCKERHUB_USER", scope="docker", env="all"), raw=True
            ).decode("utf-8")
            token = token or pb_auth.secret_bytes(
                pb_auth.get_secret(base, tok, "DOCKERHUB_TOKEN", scope="docker", env="all"), raw=True
            ).decode("utf-8")
        except pb_api.PBAPIError as e:
            raise CloudBuildError(
                f"kredensial Docker Hub tidak ditemukan ({e}). Jalankan `bq auth login` "
                "atau simpan DOCKERHUB_USER/DOCKERHUB_TOKEN di PocketBase."
            ) from e
    if not (user and token):
        raise CloudBuildError("DOCKERHUB_USER / DOCKERHUB_TOKEN kosong")
    return user, token


def _github_token() -> str:
    """GITHUB_TOKEN untuk secret build (opsional). Env lebih dulu, lalu PB."""
    tok = os.getenv("GITHUB_TOKEN") or os.getenv("GIT_TOKEN") or ""
    if tok:
        return tok
    try:
        base = pb_api._base_url()
        rec = pb_auth.get_secret(base, pb_api.get_token(base), "GITHUB_TOKEN", scope="github", env="all")
        return pb_auth.secret_bytes(rec, raw=True).decode("utf-8")
    except pb_api.PBAPIError:
        return ""


def _docker_login(env: dict, user: str, token: str) -> None:
    res = subprocess.run(
        ["docker", "login", "-u", user, "--password-stdin"],
        input=token, capture_output=True, text=True, env=env,
    )
    if res.returncode != 0:
        raise CloudBuildError(f"docker login Docker Hub gagal: {(res.stderr or res.stdout).strip()[-200:]}")


def _ensure_cloud_builder(buildx: str, local_name: str, endpoint: str, env: dict) -> None:
    """Buat builder cloud bila belum ada (`buildx create --driver cloud`)."""
    if subprocess.run([buildx, "inspect", local_name], capture_output=True, env=env).returncode == 0:
        return
    print(f"🔧 Membuat builder cloud '{local_name}' → {endpoint} ...")
    res = subprocess.run([buildx, "create", "--driver", "cloud", "--name", local_name, endpoint],
                         capture_output=True, text=True, env=env)
    if res.returncode != 0:
        raise CloudBuildError(
            f"gagal membuat builder cloud {endpoint}: {(res.stderr or res.stdout).strip()[-300:]}. "
            "Pastikan akun Docker Hub punya akses ke org ini."
        )


_GH_BUILD_ARG_KEYS = ("GITHUB_USER", "GITHUB_TOKEN")


def image_exists_on_hub(tag: str) -> bool:
    """Cek image di Docker Hub memakai kredensial PocketBase (bukan login keychain).

    Login keychain bisa tidak punya scope pull untuk org ini, sehingga image yang
    sudah ada terlihat "belum ada". Di sini login ke config sementara, lalu
    `docker manifest inspect`. Bila kredensial tidak tersedia, fallback ke cek biasa.
    """
    try:
        user, token = load_dockerhub_creds()
    except CloudBuildError:
        return _builder.check_image_exists(tag)
    tmp = tempfile.mkdtemp(prefix="bq-cloud-chk-")
    env = {**os.environ, "DOCKER_CONFIG": tmp}
    try:
        _docker_login(env, user, token)
        del token
        res = subprocess.run(["docker", "manifest", "inspect", tag],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        return res.returncode == 0
    except CloudBuildError:
        return False
    finally:
        subprocess.run(["docker", "logout"], capture_output=True, env=env)
        shutil.rmtree(tmp, ignore_errors=True)


def to_buildx_argv(cmd: list[str], buildx: str) -> list[str]:
    """`docker buildx build …` (dari build_command) -> `<buildx> build …`.

    `--build-arg GITHUB_*=<nilai>` diganti `--build-arg GITHUB_*` (tanpa nilai) agar
    token tidak masuk argv proses. buildx mengambil nilainya dari environment.
    """
    if cmd[:2] != ["docker", "buildx"]:
        raise CloudBuildError("format perintah build tidak dikenali")
    out = [buildx]
    rest = cmd[2:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--build-arg" and i + 1 < len(rest):
            key = rest[i + 1].split("=", 1)[0]
            out += [a, key if key in _GH_BUILD_ARG_KEYS else rest[i + 1]]
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def run_cloud_build(
    repo: str,
    ref: str,
    *,
    cicd_path: str = "cicd/cicd.json",
    cicd_dict: dict[str, Any] | None = None,
    endpoint: str | None = None,
    buildx_bin: str | None = None,
    platform: str | None = "linux/amd64",
    push: bool = True,
    tag: str | None = None,
    dockerfile: str = "Dockerfile",
    context: str = ".",
    extra_build_args: list[str] | None = None,
    secrets: list[str] | None = None,
    dry_run: bool = False,
    image_check: bool = True,
) -> int:
    """Build (dan push) lewat Docker Build Cloud. Return exit code."""
    ep = resolve_endpoint(endpoint)
    local_name = endpoint_to_local_name(ep)

    config = load_config()
    config = {**config, "builder": {**config["builder"], "name": local_name}}

    if cicd_dict is not None:
        cicd = cicd_dict
    else:
        try:
            cicd = _builder.load_local_cicd(cicd_path)
        except FileNotFoundError:
            cicd = {}

    predicted = _builder._predict_image_tag(repo, tag, cicd, config)
    if image_check and not dry_run:
        print(f"\n🔍 Checking registry for existing image: {predicted} ...")
        if image_exists_on_hub(predicted):
            print(f"✅ Image ready: {predicted}")
            print("⏭️  Skipping build (use --rebuild to force).")
            return 0

    # Token GitHub: bila hanya di PB, isi env agar build_command ikut menambahkan
    # secret github_token/github_user (dibaca buildx dari environment).
    if not os.getenv("GITHUB_TOKEN"):
        gh_token = _github_token()
        if gh_token:
            os.environ["GITHUB_TOKEN"] = gh_token

    cmd, image_tag = _builder.build_command(
        repo=repo, ref=ref, cicd=cicd, config=config, platform=platform, push=push,
        tag=tag, dockerfile=dockerfile, context=context,
        extra_build_args=extra_build_args, secrets=secrets,
    )

    if dry_run:
        print(f"\n☁️  Docker Build Cloud — builder {local_name} (endpoint {ep})")
        print(f"   image: {image_tag}  push={push}")
        print("\n🔍 Dry-run — perintah yang akan dijalankan:")
        print("=" * 60)
        print(_builder.format_cmd(["<buildx>"] + cmd[2:]))
        print("=" * 60)
        print("(kredensial tidak ditampilkan)")
        return 0

    buildx = resolve_buildx(buildx_bin)
    user, token = load_dockerhub_creds()
    argv = to_buildx_argv(cmd, buildx)

    print(f"\n☁️  Docker Build Cloud — builder {local_name} (endpoint {ep})")
    print(f"   image: {image_tag}  push={push}")

    tmp = tempfile.mkdtemp(prefix="bq-cloud-")
    env = {**os.environ, "DOCKER_CONFIG": tmp}
    try:
        _docker_login(env, user, token)
        del token
        _ensure_cloud_builder(buildx, local_name, ep, env)
        print("\n🚀 Build:")
        print("=" * 60)
        print(_builder.format_cmd(argv))
        print("=" * 60)
        rc = subprocess.run(argv, env=env).returncode
    finally:
        subprocess.run(["docker", "logout"], capture_output=True, env=env)
        shutil.rmtree(tmp, ignore_errors=True)

    if rc == 0:
        print(f"\n✅ Build cloud selesai: {image_tag}")
    else:
        print(f"\n❌ Build cloud gagal (exit {rc}).", file=sys.stderr)
    return rc
