"""Klasifikasi penyebab kegagalan pipeline untuk `bq --pr-fix` memakai Jev (TypeSafe System One).

Prinsip (sesuai pedoman TypeSafe): aturan yang bisa dihitung tetap di code.
Jev hanya dipakai untuk penilaian semantik — memilih satu kategori dari log
yang tidak cocok dengan pola yang sudah dikenal, lengkap dengan confidence.

Alur `classify()`:
  1. Aturan regex (`rule_classify`) — selalu dijalankan, murah, deterministik.
  2. Bila aturan menemukan kategori spesifik → pakai itu (source=rule).
  3. Bila aturan tidak yakin (`other`) dan ada TYPESAFE_API_KEY → tanya Jev.
     Hasil Jev dipakai hanya bila confidence >= threshold (source=jev).
  4. Selain itu → source=rule ("other"). Error Jev tidak menggagalkan --pr-fix.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any, Dict, Optional

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = os.getenv("JEV_MODEL", "jev-latest")
DEFAULT_THRESHOLD = 0.7
MAX_LOG_CHARS = 6000

# Kategori penyebab + deskripsi yang juga dipakai sebagai `criteria` Jev.
CAUSES: Dict[str, str] = {
    "registry_placeholder": "Push/pull image gagal karena hostname registry placeholder atau tidak bisa di-resolve (mis. registry.example.com, no such host).",
    "missing_secret": "Secret, env, atau kredensial yang dibutuhkan build tidak ada atau salah (secret not found, auth failed, unauthorized, env kosong).",
    "dockerfile_base_image": "Base image di Dockerfile tidak ditemukan atau salah tag (manifest unknown, failed to resolve source metadata).",
    "go_version": "Versi Go di go.mod tidak cocok dengan image builder (requires go >=, toolchain).",
    "build_failure": "Kompilasi atau test aplikasi gagal (compile error, test FAIL, undefined).",
    "other": "Penyebab lain atau tidak jelas dari log.",
}

_RULES = [
    ("registry_placeholder", re.compile(r"registry\.example\.com|no such host", re.I)),
    ("missing_secret", re.compile(r"secret.*not found|unauthorized|authentication required|auth failed|denied: requested access", re.I)),
    ("dockerfile_base_image", re.compile(r"manifest unknown|failed to resolve source metadata|pull access denied", re.I)),
    ("go_version", re.compile(r"requires go\s*>=|go\.mod requires|toolchain", re.I)),
    ("build_failure", re.compile(r"undefined:|compilation failed|--- FAIL|cannot find package", re.I)),
]


def rule_classify(log: str) -> Dict[str, Any]:
    """Aturan deterministik. Mengembalikan kategori pertama yang cocok."""
    for cause, pattern in _RULES:
        if pattern.search(log or ""):
            return {"cause": cause, "confidence": None, "source": "rule"}
    return {"cause": "other", "confidence": None, "source": "rule"}


def _build_payload(log: str) -> Dict[str, Any]:
    state = (log or "")[-MAX_LOG_CHARS:]
    return {
        "model": MODEL,
        "state": state,
        "questions": {
            "cause": {
                "type": "choice",
                "instructions": (
                    "Ini log step pipeline CI/CD yang gagal (Jenkins X / Tekton). "
                    "Pilih satu penyebab kegagalan paling mungkin."
                ),
                "criteria": CAUSES,
            }
        },
    }


def jev_classify(log: str, api_key: str, *, timeout: int = 30) -> Dict[str, Any]:
    """Panggil Jev. Raise RuntimeError bila gagal (dipanggil dari `classify`)."""
    body = json.dumps(_build_payload(log)).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception as e:
        raise RuntimeError(f"Jev request gagal: {type(e).__name__}: {e}") from e

    answer = (data.get("answers") or {}).get("cause") or {}
    choice = answer.get("choice")
    if choice not in CAUSES:
        raise RuntimeError(f"Jev mengembalikan pilihan tidak dikenal: {choice!r}")
    return {
        "cause": choice,
        "confidence": answer.get("confidence"),
        "probabilities": answer.get("probabilities"),
        "source": "jev",
    }


def classify(
    log: Optional[str],
    *,
    api_key: Optional[str] = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> Dict[str, Any]:
    """Klasifikasi gabungan: aturan dulu, Jev bila aturan tidak yakin."""
    if not log or not log.strip():
        return {"cause": None, "confidence": None, "source": "none"}

    rule = rule_classify(log)
    if rule["cause"] != "other":
        return rule

    key = api_key or os.getenv("TYPESAFE_API_KEY") or ""
    if not key:
        return {**rule, "note": "TYPESAFE_API_KEY kosong; Jev dilewati"}

    try:
        jev = jev_classify(log, key)
    except RuntimeError as e:
        return {**rule, "note": str(e)}

    conf = jev.get("confidence")
    if conf is not None and conf >= threshold:
        return jev
    return {**rule, "note": f"Jev confidence {conf} < {threshold}", "jev_cause": jev["cause"]}
