"""Deteksi pola Dockerfile yang tidak kompatibel dgn compose.yaml modern.

Referensi lengkap: `KNOWN_ISSUES.md` di gist central
https://gist.github.com/mamatnurahmat/35cc4c36e7c7c2d236a1b5149cdbcfd9

Dipakai `bq --pr-fix` untuk memutuskan apakah Dockerfile boleh di-preserve
(default) atau harus di-regenerate paksa (kalau ada issue `severity="error"`).

Cara nambah check baru:
1. Update `KNOWN_ISSUES.md` di gist dgn detail.
2. Tambah entry di `KNOWN_ISSUES` list bawah — pattern regex + severity + fix hint.
3. `severity="error"` → pr-fix akan overwrite Dockerfile.
4. `severity="warning"` → pr-fix preserve tapi include finding di PR body.
"""
from __future__ import annotations

import re
from typing import Dict, List


# List of known issues. Setiap check punya:
#   id       : short slug (dipakai di PR body + log)
#   pattern  : regex utk detect di Dockerfile content
#   severity : "error" (auto-fix) atau "warning" (report only)
#   title    : ringkas 1 baris
#   reason   : penjelasan kenapa broken
#   fix      : action yg diambil bq
#   gist_ref : anchor di KNOWN_ISSUES.md gist
KNOWN_ISSUES: List[Dict[str, str]] = [
    {
        "id": "legacy-github-secrets",
        "pattern": r"--mount=type=secret,id=github_(token|user)",
        "severity": "error",
        "title": "Dockerfile pakai secret `id=github_token`/`id=github_user` (legacy)",
        "reason": (
            "Standar compose.yaml modern (dari `bq --init-jx`) hanya mount "
            "secret `id=netrc`. Dockerfile yg cat `/run/secrets/github_token` "
            "akan gagal dengan `cat: can't open ... No such file or directory` "
            "→ exit 1 di step `go mod download`."
        ),
        "fix": "Regenerate Dockerfile pakai template modern (netrc-based).",
        "gist_ref": "#legacy-github-secrets",
    },
    {
        "id": "netrc-secret-wrong-target",
        "pattern": r"--mount=type=secret,id=netrc(?!,target=/root/\.netrc)",
        "severity": "warning",
        "title": "`id=netrc` tanpa `target=/root/.netrc`",
        "reason": (
            "Standar modern mount netrc ke `/root/.netrc` supaya git config "
            "otomatis pick up. Tanpa target explicit, secret di-drop ke "
            "`/run/secrets/netrc` yg tidak dibaca git."
        ),
        "fix": "Ubah ke `--mount=type=secret,id=netrc,target=/root/.netrc`.",
        "gist_ref": "#netrc-target-missing",
    },
    {
        "id": "arg-github-token-legacy",
        "pattern": r"ARG\s+GITHUB_(TOKEN|USER|PASSWORD)",
        "severity": "warning",
        "title": "Pakai `ARG GITHUB_TOKEN`/`GITHUB_USER` (pola sangat lama)",
        "reason": (
            "ARG-based github credential leak ke image layer + history. "
            "Standar modern pakai BuildKit `--mount=type=secret,id=netrc`."
        ),
        "fix": "Migrasi ke netrc secret mount atau pakai `bq --init-legacy` kalau perlu preserve.",
        "gist_ref": "#arg-github-token-legacy",
    },
    {
        "id": "go-toolchain-mismatch",
        "pattern": r"FROM\s+.*golang:1\.(1[0-9]|20|21)-",
        "severity": "warning",
        "title": "Go base image versi lama (< 1.22)",
        "reason": (
            "Go 1.22+ diperlukan untuk sebagian modul Qoin (mis. temporal, "
            "otel v1). Base image lama bisa gagal `go mod download` untuk "
            "modul yg require `go >= 1.22`."
        ),
        "fix": "Upgrade FROM ke `golang:1.22-alpine` atau lebih baru.",
        "gist_ref": "#go-toolchain-mismatch",
    },
]


def analyze_dockerfile(content: str) -> List[Dict[str, str]]:
    """Scan `content` (str isi Dockerfile), return list issue yg terdeteksi.

    Kalau file kosong / None → return list kosong.
    """
    if not content:
        return []
    found: List[Dict[str, str]] = []
    for check in KNOWN_ISSUES:
        if re.search(check["pattern"], content, re.IGNORECASE):
            found.append(check)
    return found


def has_error(issues: List[Dict[str, str]]) -> bool:
    """True kalau ada issue dgn severity='error' (bq harus auto-fix)."""
    return any(i["severity"] == "error" for i in issues)


def format_issues_markdown(issues: List[Dict[str, str]]) -> str:
    """Format list issue jadi markdown snippet — dipakai di PR body."""
    if not issues:
        return ""
    lines = ["**Dockerfile issues terdeteksi:**", ""]
    for issue in issues:
        icon = "❌" if issue["severity"] == "error" else "⚠️"
        lines.append(f"- {icon} **{issue['id']}** — {issue['title']}")
        lines.append(f"  - Reason: {issue['reason']}")
        lines.append(f"  - Fix: {issue['fix']}")
        lines.append(
            f"  - Docs: [KNOWN_ISSUES.md{issue['gist_ref']}]"
            f"(https://gist.github.com/mamatnurahmat/35cc4c36e7c7c2d236a1b5149cdbcfd9#file-known_issues-md)"
        )
    return "\n".join(lines)


def format_issues_console(issues: List[Dict[str, str]]) -> List[str]:
    """Format list issue jadi baris console (colored icon + summary)."""
    lines: List[str] = []
    for issue in issues:
        icon = "❌" if issue["severity"] == "error" else "⚠️"
        lines.append(f"      {icon} {issue['id']}: {issue['title']}")
    return lines
