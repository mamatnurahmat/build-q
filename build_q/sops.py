"""SOPS + age encryption helper.

Standar bootstrap-k8s: Secret di-encrypt dengan SOPS pakai recipient age
yang dideteksi dari (urutan prioritas):
  1. env `BUILD_Q_SOPS_AGE_RECIPIENT` — comma-separated untuk multi-recipient
  2. `.sops.yaml` di repo gitops (sops auto-pick, kita skip flag `--age`)
  3. public key lokal di `~/.config/sops/age/keys.txt` (fallback aman — identitas
     laptop pasti bisa decrypt kembali)

Key file untuk DECRYPT: `SOPS_AGE_KEY_FILE` (bila kosong, kita arahkan ke
`~/.config/sops/age/keys.txt`). Tanpa ini sops exit 128.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


DEFAULT_KEY_FILE = Path.home() / ".config" / "sops" / "age" / "keys.txt"
_PUBKEY_RE = re.compile(r"^#\s*public key:\s*(age1[0-9a-z]+)\s*$", re.IGNORECASE)


class SopsError(RuntimeError):
    """Raised when sops / age preflight or operation fails."""


def _which(binary: str) -> Optional[str]:
    return shutil.which(binary)


def _env_with_keyfile() -> dict:
    """Return os.environ copy with SOPS_AGE_KEY_FILE set to default when missing."""
    env = os.environ.copy()
    if not env.get("SOPS_AGE_KEY_FILE") and DEFAULT_KEY_FILE.exists():
        env["SOPS_AGE_KEY_FILE"] = str(DEFAULT_KEY_FILE)
    return env


def local_public_keys(key_file: Optional[Path] = None) -> List[str]:
    """Extract `age1...` public keys dari comment di keys.txt."""
    path = key_file or Path(
        os.getenv("SOPS_AGE_KEY_FILE", str(DEFAULT_KEY_FILE))
    ).expanduser()
    if not path.exists():
        return []
    keys: List[str] = []
    for line in path.read_text().splitlines():
        m = _PUBKEY_RE.match(line.strip())
        if m:
            keys.append(m.group(1))
    return keys


def _find_sops_config(start: Path) -> Optional[Path]:
    """Cari `.sops.yaml` di `start` dan parent-nya (sampai root)."""
    cur = start.resolve() if start.is_dir() else start.resolve().parent
    for p in [cur, *cur.parents]:
        candidate = p / ".sops.yaml"
        if candidate.exists():
            return candidate
    return None


def resolve_recipients(target_file: Optional[Path] = None) -> Tuple[List[str], str]:
    """Resolve daftar recipient age + sumbernya.

    Return (recipients, source). `recipients == []` → biarkan sops pakai
    `.sops.yaml` (source = "sops-config"). Raise SopsError kalau tidak ada
    sumber sama sekali.
    """
    override = os.getenv("BUILD_Q_SOPS_AGE_RECIPIENT", "").strip()
    if override:
        return (
            [r.strip() for r in override.split(",") if r.strip()],
            "env BUILD_Q_SOPS_AGE_RECIPIENT",
        )

    if target_file is not None:
        cfg = _find_sops_config(target_file)
        if cfg is not None:
            return [], f".sops.yaml ({cfg})"

    local = local_public_keys()
    if local:
        return local, f"local age key ({DEFAULT_KEY_FILE})"

    raise SopsError(
        "Tidak ada recipient age yang bisa dipakai. Set "
        "BUILD_Q_SOPS_AGE_RECIPIENT=age1xxx atau pastikan "
        f"{DEFAULT_KEY_FILE} punya baris `# public key: ageXXX`."
    )


def preflight() -> None:
    """Pastikan `sops` tersedia; raise SopsError kalau tidak."""
    if not _which("sops"):
        raise SopsError(
            "`sops` tidak ditemukan di PATH. Install dulu: "
            "https://github.com/getsops/sops/releases"
        )


def is_encrypted(path: Path) -> bool:
    """Cek apakah file sudah dalam format SOPS (ada marker `sops:` di root)."""
    if not path.exists():
        return False
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return False
    # Marker sederhana: block `sops:` di root plus field `age` atau `pgp`.
    return bool(re.search(r"^sops:\s*$", text, re.MULTILINE)) and (
        "age:" in text or "pgp:" in text or "kms:" in text
    )


def encrypt_file(
    path: Path,
    *,
    cwd: Optional[Path] = None,
    recipients: Optional[List[str]] = None,
    source: str = "",
) -> Tuple[str, str]:
    """Encrypt `path` in-place via sops+age.

    Jika `recipients` kosong (`[]`), kita skip flag `--age` dan andalkan
    `.sops.yaml`. Return (resolved_recipients_str, source_label).
    """
    preflight()
    if recipients is None:
        recipients, source = resolve_recipients(path)

    cmd = ["sops", "--encrypt", "--in-place"]
    for r in recipients:
        cmd.extend(["--age", r])
    cmd.append(str(path))

    try:
        subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=_env_with_keyfile(),
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise SopsError(
            f"sops encrypt gagal: {(e.stderr or e.stdout or '').strip()}"
        ) from e
    return (",".join(recipients) or "(from .sops.yaml)", source)


def decrypt_file(
    path: Path,
    *,
    cwd: Optional[Path] = None,
    in_place: bool = True,
) -> str:
    """Decrypt `path`. Return plaintext content; bila `in_place=True`, juga
    tulis balik file-nya.
    """
    preflight()
    cmd = ["sops", "--decrypt"]
    if in_place:
        cmd.append("--in-place")
    cmd.append(str(path))

    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=_env_with_keyfile(),
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise SopsError(
            f"sops decrypt gagal: {(e.stderr or e.stdout or '').strip()}"
        ) from e

    if in_place:
        return path.read_text()
    return result.stdout


def decrypt_to_string(path: Path, *, cwd: Optional[Path] = None) -> str:
    """Convenience: decrypt tanpa menyentuh file (buat idempotency check)."""
    return decrypt_file(path, cwd=cwd, in_place=False)
