"""Pemindai rahasia bersama — dipakai `bq bin push` dan `bq --pr-rollback`.

Prinsip: yang dikembalikan hanya label pola, nama key, dan nomor baris —
TIDAK PERNAH nilai rahasianya, sehingga hasil scan aman dicetak ke log / body PR.

- ``scan_patterns`` : pola nilai rahasia (token, kunci privat, kredensial di URL, ...).
- ``scan_keys``     : heuristik nama key sensitif bergaya env (``KEY: nilai`` / ``KEY=nilai``)
                      dengan nilai literal — mis. ``environment:`` docker-compose.
- ``scan``          : gabungan keduanya, satu entri per baris.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# Pola nilai rahasia (asal: `bq bin push`). Hanya label yang dicetak, tidak pernah nilainya.
SECRET_PATTERNS = [
    ("token GitHub", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("token Slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("kunci akses AWS", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("kunci privat", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("token GitLab", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("kunci Stripe", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}")),
    ("kunci Google (AIza)", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("token Docker Hub", re.compile(r"\bdckr_pat_[A-Za-z0-9_-]{20,}")),
    ("kredensial di URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@'\"]+:(?!\$)[^\s/@'\"]+@", re.I)),
    ("basic auth", re.compile(
        r"\bAuthorization:\s*Basic\s+[A-Za-z0-9+/=]{8,}"
        r"|(?:^|\s)(?:-u|--user)[\s=]+['\"]?[^\s:'\"$]+:[^\s'\"]{4,}",
        re.I | re.M,
    )),
    ("password di argumen", re.compile(
        r"--(?:password|passwd|pass|token|secret|api-?key)(?:=|\s+)['\"]?[^\s'\"$]{6,}", re.I)),
    ("penugasan rahasia", re.compile(
        # Nilai yang diawali $ (referensi shell) atau < (placeholder) tidak dihitung; nilai berkutip boleh berspasi.
        r"^\s*(?:export\s+)?[A-Za-z0-9_-]*(?:TOKEN|SECRET|PASSWORD|PASS|APIKEY|API_KEY|KEY)[A-Za-z0-9_-]*"
        r"\s*=\s*(?:'(?![$<])[^'\n]{8,}'|\"(?![$<])[^\"\n]{8,}\"|(?![$<])[^\s'\"]{8,})",
        re.M | re.I,
    )),
    ("penugasan PWD", re.compile(r"^\s*(?:export\s+)?PWD\s*=\s*(?!\$)(?!/)[^\s'\"]{8,}", re.M)),
]


def scan_patterns(text: str) -> list[tuple[str, int]]:
    """Kembalikan (label pola, nomor baris). Nilai rahasia tidak pernah dikembalikan."""
    hits: list[tuple[str, int]] = []
    for label, rx in SECRET_PATTERNS:
        for m in rx.finditer(text):
            hits.append((label, text.count("\n", 0, m.start()) + 1))
    return hits


# ── Heuristik nama key (env / YAML) ──────────────────────────────────────

_SENSITIVE_KEY_RE = re.compile(
    r"(SECRET|PASSWORD|PASSWD|PRIV(?:ATE)?_?KEY|TOKEN|AUTH_BASIC|API_?KEY|CREDENTIAL)"
)
_PUBLIC_KEY_RE = re.compile(r"(?:^|_)PUB(?:LIC)?(?:_|$)")
_KEY_VALUE_RE = re.compile(
    r"""^[ \t]*(?:-[ \t]*)?(?:export[ \t]+)?["']?([A-Z][A-Z0-9_]*)["']?[ \t]*[:=][ \t]*(.*?)[ \t]*$""",
    re.M,
)


def _literal_value(value: str) -> bool:
    """True bila nilai berisi literal (bukan kosong, referensi ``$VAR`` / ``${VAR}``, atau placeholder ``<...>``)."""
    v = value.strip()
    if not v:
        return False
    if v[0] in "'\"":
        quote = v[0]
        inner = v[1:].split(quote, 1)[0]
    else:
        inner = v.split(" #", 1)[0].strip()
    if not inner:
        return False
    return not inner.startswith(("$", "<"))


def scan_keys(text: str) -> List[Tuple[str, int]]:
    """Kembalikan (nama key, nomor baris) untuk key sensitif bernilai literal."""
    hits: List[Tuple[str, int]] = []
    for m in _KEY_VALUE_RE.finditer(text):
        key, value = m.group(1), m.group(2)
        if not _SENSITIVE_KEY_RE.search(key) or _PUBLIC_KEY_RE.search(key):
            continue
        if not _literal_value(value):
            continue
        hits.append((key, text.count("\n", 0, m.start()) + 1))
    return hits


def scan(text: str) -> List[Dict[str, object]]:
    """Gabungan pola + key: ``[{"line": n, "key": str|None, "labels": [..]}]`` urut baris."""
    by_line: Dict[int, Dict[str, object]] = {}

    def _entry(line: int) -> Dict[str, object]:
        return by_line.setdefault(line, {"line": line, "key": None, "labels": []})

    for label, line in scan_patterns(text):
        labels = _entry(line)["labels"]
        if label not in labels:  # type: ignore[operator]
            labels.append(label)  # type: ignore[union-attr]
    for key, line in scan_keys(text):
        e = _entry(line)
        e["key"] = key
        if "nama key sensitif" not in e["labels"]:  # type: ignore[operator]
            e["labels"].append("nama key sensitif")  # type: ignore[union-attr]
    return [by_line[n] for n in sorted(by_line)]


def describe(hit: Dict[str, object]) -> str:
    """Satu baris ringkas tanpa nilai: ``baris 22: RSA_PRIV_KEY (kunci privat, nama key sensitif)``."""
    key: Optional[object] = hit.get("key")
    labels = ", ".join(hit.get("labels") or [])  # type: ignore[arg-type]
    head = f"baris {hit['line']}"
    return f"{head}: {key} ({labels})" if key else f"{head}: {labels}"
