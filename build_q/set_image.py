"""`bq --set-image <ns> <deployment> <image> [container]` — hot-patch
Kubernetes Deployment via `kubectl set image` (imperative, cepat).

Menggantikan script eksternal `set-image` (bash). Perilaku dipertahankan:
  - Bila <image> tidak mengandung `/`, treat sebagai tag → construct
    `<REGISTRY_URL>/<deployment>:<tag>` (fallback registry `loyaltolpi`).
  - Tanpa arg <container>, pakai `*=<image>` (update semua container).
  - Setelah set, tunggu rollout status sampai selesai (tanpa timeout).

Exit code:
  0 = rollout sukses
  1 = kubectl error / rollout gagal
  2 = usage error (arg kurang)
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Optional

from ._common import resolve_registry
from .config import load_config


def _resolve_image(deployment: str, raw: str) -> str:
    """Bila `raw` plain tag (tanpa `/`), construct `<registry>/<deployment>:<tag>`.
    Selain itu return apa adanya.
    """
    if "/" in raw:
        return raw
    cfg = load_config()
    registry = resolve_registry(cfg)
    return f"{registry}/{deployment}:{raw}"


def _current_image(ns: str, deployment: str) -> str:
    try:
        res = subprocess.run(
            ["kubectl", "get", "deployment", deployment, "-n", ns,
             "-o", "jsonpath={.spec.template.spec.containers[0].image}"],
            capture_output=True, text=True, timeout=10,
        )
        return (res.stdout or "").strip() or "<unknown>"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "<unknown>"


def run_set_image(
    ns: str,
    deployment: str,
    raw_image: str,
    *,
    container: Optional[str] = None,
) -> int:
    if not shutil.which("kubectl"):
        print("❌ kubectl tidak ditemukan di PATH.", file=sys.stderr)
        return 1

    image = _resolve_image(deployment, raw_image)
    prev = _current_image(ns, deployment)
    print(f"🔄 {prev} → {image}")

    target = "deployment/" + deployment
    if container:
        set_arg = f"{container}={image}"
    else:
        set_arg = f"*={image}"

    try:
        rc = subprocess.call(
            ["kubectl", "set", "image", target, set_arg, "-n", ns],
        )
    except FileNotFoundError:
        print("❌ kubectl tidak ditemukan.", file=sys.stderr)
        return 1
    if rc != 0:
        print(f"❌ kubectl set image gagal (rc={rc}).", file=sys.stderr)
        return 1

    print("⏳ waiting for rollout...")
    try:
        rc = subprocess.call(
            ["kubectl", "rollout", "status", target, "-n", ns, "--watch"],
        )
    except FileNotFoundError:
        return 1
    return 0 if rc == 0 else 1
