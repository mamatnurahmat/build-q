"""Koleksi rule deteksi issue Dockerfile — extensible collection.

Referensi lengkap: `KNOWN_ISSUES.md` di gist central
https://gist.github.com/mamatnurahmat/35cc4c36e7c7c2d236a1b5149cdbcfd9

Dipakai oleh:
- `bq --pr-fix` untuk memutuskan apakah Dockerfile boleh di-preserve
  (default) atau harus di-regenerate paksa (kalau ada issue severity="error").
- `bq --fix-dockerfile` (v2) untuk scan + Jev-approved auto-fix.

Cara nambah check baru:
1. Update `KNOWN_ISSUES.md` di gist dgn detail.
2. Tambah entry di `KNOWN_ISSUES` list bawah — pattern regex + severity + fix hint.
3. `severity="error"` → pr-fix akan overwrite Dockerfile.
4. `severity="warning"` → pr-fix preserve tapi include finding di PR body.
5. `category` sesuai glossary: build, security, performance, compliance.
6. `auto_fixable=True` → `bq --fix-dockerfile` bisa auto-fix pattern ini.
"""
from __future__ import annotations

import re
from typing import Dict, List


KNOWN_ISSUES: List[Dict] = [
    # ── CRITICAL / ERROR ─────────────────────────────────────────────────

    {
        "id": "legacy-github-secrets",
        "pattern": r"--mount=type=secret,id=github_(token|user)",
        "severity": "error",
        "category": "build",
        "auto_fixable": True,
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
        "id": "env-before-from",
        "pattern": r"(?m)\A(?:(?:#[^\n]*|[ \t]*)\n)*(?:ENV|RUN|COPY|ADD|WORKDIR|EXPOSE|CMD|ENTRYPOINT|LABEL|VOLUME|USER|HEALTHCHECK|STOPSIGNAL|SHELL|ONBUILD)\s",
        "severity": "error",
        "category": "build",
        "auto_fixable": False,
        "title": "Instruksi (ENV/RUN/etc.) sebelum FROM — syntax Dockerfile invalid",
        "reason": (
            "Dockerfile HARUS dimulai dengan FROM (atau ARG global). "
            "Instruksi lain sebelum FROM menyebabkan build error immediate: "
            "`failed to solve: dockerfile parse error`."
        ),
        "fix": "Pindahkan semua instruksi ke setelah FROM. Hanya ARG global diizinkan sebelum FROM.",
        "gist_ref": "#env-before-from",
    },
    {
        "id": "registry-literal-placeholder",
        "pattern": r"(?mi)FROM\s+(?:REGISTRY\d*|PROJECT)/",
        "severity": "error",
        "category": "build",
        "auto_fixable": False,
        "title": "FROM pakai placeholder `REGISTRY01/PROJECT/...` — template belum di-replace",
        "reason": (
            "FROM mengandung literal placeholder (REGISTRY, PROJECT) yang "
            "seharusnya sudah di-substitusi. Docker pull gagal: "
            "`pull access denied for REGISTRY01/PROJECT/...`."
        ),
        "fix": "Replace dengan registry path aktual (loyaltolpi/...) atau gunakan ARG + --build-arg.",
        "gist_ref": "#registry-literal",
    },
    {
        "id": "deprecated-maintainer",
        "pattern": r"(?im)^[ \t]*MAINTAINER\s+",
        "severity": "error",
        "category": "build",
        "auto_fixable": True,
        "title": "Instruksi `MAINTAINER` deprecated — gunakan LABEL",
        "reason": (
            "MAINTAINER deprecated sejak Docker 1.13. "
            "Build masih jalan tapi trigger warning dan tidak sesuai best practice."
        ),
        "fix": "Ganti `MAINTAINER x` → `LABEL maintainer=\"x\"`. `bq --fix-dockerfile` auto-convert.",
        "gist_ref": "#deprecated-maintainer",
    },
    {
        "id": "env-legacy-syntax",
        "pattern": r"(?m)^[ \t]*ENV\s+[A-Z_][A-Z0-9_]*\s+[^=\n][^\n]*$",
        "severity": "error",
        "category": "build",
        "auto_fixable": True,
        "title": "Legacy `ENV KEY value` syntax (tanpa `=`)",
        "reason": (
            "Syntax `ENV KEY value` masih valid tapi ambiguous untuk multi-value. "
            "Standar modern pakai `ENV KEY=value`. Bisa menyebabkan "
            "surprising behavior saat ada spasi di value."
        ),
        "fix": "Convert ke `ENV KEY=value`. `bq --fix-dockerfile` auto-convert.",
        "gist_ref": "#env-legacy-syntax",
    },
    {
        "id": "private-repo-bitbucket",
        "pattern": r"(?i)bitbucket\.org/qoin",
        "severity": "error",
        "category": "build",
        "auto_fixable": False,
        "title": "Private repo referensi Bitbucket (deprecated) — harus GitHub",
        "reason": (
            "Qoin sudah migrasi dari Bitbucket ke GitHub. "
            "Go mod yg masih referensi bitbucket.org/qoin* akan gagal "
            "Authentication failed. GOPRIVATE juga harus diupdate."
        ),
        "fix": "Migrate import path ke github.com/Qoin-Digital-Indonesia/*. Update GOPRIVATE.",
        "gist_ref": "#private-repo-bitbucket",
    },
    {
        "id": "workdir-commented-out",
        "pattern": r"(?m)^[ \t]*#\s*WORKDIR\s+/app",
        "severity": "error",
        "category": "build",
        "auto_fixable": True,
        "title": "WORKDIR di-comment-out — binary tidak ditemukan saat run",
        "reason": (
            "WORKDIR /app di-comment menyebabkan COPY dan RUN bekerja di `/` "
            "atau lokasi unexpected. Binary hasil build tidak ditemukan → "
            "container exit 1 saat startup."
        ),
        "fix": "Uncomment `WORKDIR /app`. Pastikan binary di-COPY ke path yang benar.",
        "gist_ref": "#workdir-commented",
    },

    # ── WARNING ──────────────────────────────────────────────────────────

    {
        "id": "netrc-secret-wrong-target",
        "pattern": r"--mount=type=secret,id=netrc(?!,target=/root/\.netrc)",
        "severity": "warning",
        "category": "security",
        "auto_fixable": True,
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
        "category": "security",
        "auto_fixable": True,
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
        "category": "build",
        "auto_fixable": False,
        "title": "Go base image versi lama (< 1.22)",
        "reason": (
            "Go 1.22+ diperlukan untuk sebagian modul Qoin (mis. temporal, "
            "otel v1). Base image lama bisa gagal `go mod download` untuk "
            "modul yg require `go >= 1.22`."
        ),
        "fix": "Upgrade FROM ke `golang:1.22-alpine` atau lebih baru.",
        "gist_ref": "#go-toolchain-mismatch",
    },
    {
        "id": "from-as-lowercase",
        "pattern": r"(?m)^[ \t]*FROM\s+\S+\s+(?-i:as)\s+\S+",
        "severity": "warning",
        "category": "build",
        "auto_fixable": True,
        "title": "`FROM ... as ...` huruf kecil — standar pakai `AS` (uppercase)",
        "reason": (
            "Docker menerima `as` lowercase tapi standar dokumentasi dan "
            "best practice pakai `AS` uppercase. Konsistensi memudahkan "
            "grepping dan parsing oleh tool CI/CD."
        ),
        "fix": "Ubah `as` → `AS` di semua FROM statement. `bq --fix-dockerfile` auto-fix.",
        "gist_ref": "#from-as-lowercase",
    },
    {
        "id": "base-image-latest",
        "pattern": r"(?m)^[ \t]*FROM\s+\S+:latest(\s|$)",
        "severity": "warning",
        "category": "build",
        "auto_fixable": False,
        "title": "Base image pakai tag `:latest` — non-deterministic build",
        "reason": (
            "Tag `:latest` bisa berubah kapan saja. Build hari ini dan "
            "besok bisa menghasilkan image berbeda. Sulit debug dan "
            "rollback kalau base image berubah."
        ),
        "fix": "Pin ke tag versi spesifik (mis. `alpine:3.20`, `golang:1.23-alpine`).",
        "gist_ref": "#base-image-latest",
    },
    {
        "id": "base-image-no-tag",
        "pattern": r"(?m)^[ \t]*FROM\s+(?!scratch)([a-z][a-z0-9._/-]*)(?:\s+AS|\s*$)",
        "severity": "warning",
        "category": "build",
        "auto_fixable": False,
        "title": "Base image tanpa tag — implicitly `:latest`",
        "reason": (
            "FROM tanpa tag secara implisit menjadi `:latest`. "
            "Sama risikonya dengan pakai `:latest` eksplisit: "
            "non-deterministic, sulit reproduce, dan debug."
        ),
        "fix": "Tambahkan tag versi eksplisit ke base image.",
        "gist_ref": "#base-image-no-tag",
    },
    {
        "id": "add-instead-of-copy",
        "pattern": r"(?m)^[ \t]*ADD\s+(?!https?://)(?!.*\.tar)(?!.*\.gz)\S+\s+\S+",
        "severity": "warning",
        "category": "build",
        "auto_fixable": True,
        "title": "Pakai `ADD` untuk file lokal — gunakan `COPY`",
        "reason": (
            "ADD punya behavior hidden (auto-extract tar, download URL). "
            "Untuk file lokal biasa, COPY lebih explicit dan predictable. "
            "ADD hanya dipakai kalau butuh auto-extract."
        ),
        "fix": "Ganti `ADD src dest` → `COPY src dest` kecuali butuh auto-extract.",
        "gist_ref": "#add-instead-of-copy",
    },
    {
        "id": "missing-user",
        "pattern": None,  # structural check — no simple regex
        "severity": "warning",
        "category": "security",
        "auto_fixable": False,
        "title": "Tidak ada instruksi USER — container run sebagai root",
        "reason": (
            "Tanpa USER instruction, container berjalan sebagai root. "
            "Kalau attacker exploit app, mereka dapat akses root di container. "
            "Security best practice: run sebagai non-root user."
        ),
        "fix": "Tambah `RUN adduser -D appuser && USER appuser` di final stage.",
        "gist_ref": "#missing-user",
    },
    {
        "id": "apt-get-no-clean",
        "pattern": r"(?m)apt-get\s+install(?!.*(?:&&\s*(?:apt-get\s+clean|rm\s+-rf\s+/var/lib/apt)))",
        "severity": "warning",
        "category": "performance",
        "auto_fixable": False,
        "title": "`apt-get install` tanpa cleanup — image bloat",
        "reason": (
            "apt-get install tanpa `apt-get clean && rm -rf /var/lib/apt/lists/*` "
            "menyisakan cache package di layer. Menambah ukuran image "
            "signifikan (50-200MB+)."
        ),
        "fix": "Chain `&& apt-get clean && rm -rf /var/lib/apt/lists/*` di RUN yang sama.",
        "gist_ref": "#apt-get-no-clean",
    },
    {
        "id": "pip-no-cache",
        "pattern": r"(?m)pip3?\s+install(?!.*--no-cache-dir)",
        "severity": "warning",
        "category": "performance",
        "auto_fixable": True,
        "title": "`pip install` tanpa `--no-cache-dir` — image bloat",
        "reason": (
            "pip menyimpan cache download di layer. Menambah ukuran image "
            "tanpa manfaat (container tidak akan install ulang)."
        ),
        "fix": "Tambah `--no-cache-dir` flag ke pip install.",
        "gist_ref": "#pip-no-cache",
    },
    {
        "id": "curl-without-fail",
        "pattern": r"(?m)^[ \t]*RUN\b.*(?:curl|wget)\s+(?!.*(?:-f|--fail))\S+",
        "severity": "warning",
        "category": "build",
        "auto_fixable": False,
        "title": "`curl`/`wget` tanpa `-f`/`--fail` — error HTTP tidak terdeteksi",
        "reason": (
            "Tanpa `-f` flag, curl return exit 0 bahkan saat server response "
            "404/500. Build tetap lanjut dengan file corrupt/HTML error page "
            "→ runtime failure."
        ),
        "fix": "Tambah `-f` / `--fail` flag ke curl. Untuk wget: `--fail-on-error` (GNU wget).",
        "gist_ref": "#curl-without-fail",
    },
    {
        "id": "copy-all-before-deps",
        "pattern": None,  # structural check
        "severity": "warning",
        "category": "performance",
        "auto_fixable": False,
        "title": "`COPY . .` sebelum dependency install — cache layer selalu bust",
        "reason": (
            "COPY seluruh context sebelum `go mod download` / `npm install` "
            "menyebabkan setiap perubahan code membuat layer dependency "
            "re-download. Cache Docker layer tidak efektif."
        ),
        "fix": "COPY go.mod go.sum dulu → RUN go mod download → baru COPY . .",
        "gist_ref": "#copy-all-before-deps",
    },
    {
        "id": "missing-dockerignore",
        "pattern": None,  # filesystem check
        "severity": "warning",
        "category": "performance",
        "auto_fixable": False,
        "title": "Tidak ada `.dockerignore` — build context besar",
        "reason": (
            "Tanpa .dockerignore, seluruh working directory (termasuk .git, "
            "node_modules, vendor, binary) dikirim ke Docker daemon. "
            "Build lambat dan context bisa puluhan/ratusan MB."
        ),
        "fix": "Buat `.dockerignore` yang exclude .git, node_modules, vendor, *.exe, dll.",
        "gist_ref": "#missing-dockerignore",
    },
    {
        "id": "run-too-many-layers",
        "pattern": None,  # structural check
        "severity": "warning",
        "category": "performance",
        "auto_fixable": False,
        "title": "Terlalu banyak RUN terpisah (>8) — layer bloat",
        "reason": (
            "Setiap RUN membuat layer baru. Banyak RUN terpisah untuk "
            "command yang terkait (apt-get update, install, cleanup) "
            "menambah layer dan ukuran image. Chain dengan `&&`."
        ),
        "fix": "Gabung RUN yang terkait dengan `&&` dan `\\` untuk line continuation.",
        "gist_ref": "#run-too-many-layers",
    },
    {
        "id": "expose-missing",
        "pattern": None,  # structural check
        "severity": "info",
        "category": "compliance",
        "auto_fixable": False,
        "title": "Tidak ada instruksi EXPOSE — port tidak terdokumentasi",
        "reason": (
            "EXPOSE mendokumentasikan port yang digunakan container. "
            "Tanpa EXPOSE, developer/ops harus menebak port. "
            "Tidak blocking tapi best practice untuk visibility."
        ),
        "fix": "Tambah `EXPOSE <PORT>` sesuai port aplikasi.",
        "gist_ref": "#expose-missing",
    },
    {
        "id": "missing-healthcheck",
        "pattern": None,  # structural check
        "severity": "info",
        "category": "compliance",
        "auto_fixable": False,
        "title": "Tidak ada instruksi HEALTHCHECK — Docker Compose health unknown",
        "reason": (
            "Tanpa HEALTHCHECK, `docker compose ps` tidak bisa menampilkan "
            "status kesehatan container. Berguna untuk depends_on condition "
            "dan auto-restart."
        ),
        "fix": "Tambah `HEALTHCHECK CMD curl -f http://localhost:<PORT>/health || exit 1`.",
        "gist_ref": "#missing-healthcheck",
    },
    {
        "id": "missing-multistage",
        "pattern": None,  # structural check
        "severity": "info",
        "category": "performance",
        "auto_fixable": False,
        "title": "Single-stage build — image mengandung build tools",
        "reason": (
            "Tanpa multi-stage, image final mengandung compiler, SDK, "
            "build cache, dan tools yang tidak diperlukan runtime. "
            "Image Go bisa 1GB+ vs 20MB dengan multi-stage."
        ),
        "fix": "Pisah ke builder stage (FROM golang AS builder) dan runtime stage (FROM alpine).",
        "gist_ref": "#missing-multistage",
    },
]


# ─── Structural check helpers ──────────────────────────────────────────────

def _parse_instructions(content: str) -> list[dict]:
    """Parse Dockerfile into list of {line, lineno, instruction, args}."""
    instructions = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i].rstrip()
        # skip comments and blanks
        if not raw.strip() or raw.strip().startswith("#"):
            i += 1
            continue
        # handle line continuations
        full = raw
        while full.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            full = full.rstrip()[:-1] + " " + lines[i].strip()
        parts = full.strip().split(None, 1)
        if parts:
            instructions.append({
                "lineno": i + 1,
                "instruction": parts[0].upper(),
                "args": parts[1] if len(parts) > 1 else "",
                "raw": full,
            })
        i += 1
    return instructions


def _structural_checks(content: str) -> list[dict]:
    """Run structural checks that need instruction ordering, return list of matched rule IDs."""
    if not content or not content.strip():
        return []

    instructions = _parse_instructions(content)
    if not instructions:
        return []

    matched_ids = []

    # missing-user: no USER instruction in final stage
    from_indices = [i for i, inst in enumerate(instructions) if inst["instruction"] == "FROM"]
    if from_indices:
        last_from = from_indices[-1]
        final_stage = instructions[last_from:]
        if not any(inst["instruction"] == "USER" for inst in final_stage):
            matched_ids.append("missing-user")

    # copy-all-before-deps: COPY . . before go mod download / npm install
    for i, inst in enumerate(instructions):
        if inst["instruction"] == "COPY" and ". ." in inst["args"]:
            remaining = instructions[i + 1:]
            if any("go mod" in r["args"] or "npm install" in r["args"] or "npm ci" in r["args"]
                   for r in remaining if r["instruction"] == "RUN"):
                matched_ids.append("copy-all-before-deps")
            break

    # run-too-many-layers: >8 RUN in final stage
    if from_indices:
        last_from = from_indices[-1]
        run_count = sum(1 for inst in instructions[last_from:] if inst["instruction"] == "RUN")
        if run_count > 8:
            matched_ids.append("run-too-many-layers")

    # expose-missing
    if not any(inst["instruction"] == "EXPOSE" for inst in instructions):
        matched_ids.append("expose-missing")

    # missing-healthcheck
    if not any(inst["instruction"] == "HEALTHCHECK" for inst in instructions):
        matched_ids.append("missing-healthcheck")

    # missing-multistage: only 1 FROM
    if len(from_indices) < 2:
        has_go = any("golang" in inst["args"].lower() for inst in instructions if inst["instruction"] == "FROM")
        has_dotnet = any("sdk" in inst["args"].lower() and "dotnet" in inst["args"].lower()
                        for inst in instructions if inst["instruction"] == "FROM")
        if has_go or has_dotnet:
            matched_ids.append("missing-multistage")

    return matched_ids


# ─── Public API (backward-compatible) ──────────────────────────────────────

def analyze_dockerfile(content: str) -> list[dict]:
    """Scan content (str isi Dockerfile), return list issue yang terdeteksi.

    Includes both regex-based and structural checks.
    Kalau file kosong / None → return list kosong.
    """
    if not content:
        return []
    found: list[dict] = []
    seen_ids: set[str] = set()

    # regex-based checks
    for check in KNOWN_ISSUES:
        if check["pattern"] is None:
            continue
        if re.search(check["pattern"], content, re.IGNORECASE):
            found.append(check)
            seen_ids.add(check["id"])

    # structural checks
    structural_ids = _structural_checks(content)
    by_id = {c["id"]: c for c in KNOWN_ISSUES}
    for sid in structural_ids:
        if sid not in seen_ids and sid in by_id:
            found.append(by_id[sid])
            seen_ids.add(sid)

    return found


def has_error(issues: list[dict]) -> bool:
    """True kalau ada issue dgn severity='error' (bq harus auto-fix)."""
    return any(i["severity"] == "error" for i in issues)


def format_issues_markdown(issues: list[dict]) -> str:
    """Format list issue jadi markdown snippet — dipakai di PR body."""
    if not issues:
        return ""
    lines = ["**Dockerfile issues terdeteksi:**", ""]
    for issue in issues:
        icon = "\u274c" if issue["severity"] == "error" else "\u26a0\ufe0f"
        lines.append(f"- {icon} **{issue['id']}** \u2014 {issue['title']}")
        lines.append(f"  - Reason: {issue['reason']}")
        lines.append(f"  - Fix: {issue['fix']}")
        lines.append(
            f"  - Docs: [KNOWN_ISSUES.md{issue['gist_ref']}]"
            f"(https://gist.github.com/mamatnurahmat/35cc4c36e7c7c2d236a1b5149cdbcfd9#file-known_issues-md)"
        )
    return "\n".join(lines)


def format_issues_console(issues: list[dict]) -> list[str]:
    """Format list issue jadi baris console (colored icon + summary)."""
    lines: list[str] = []
    for issue in issues:
        icon = "\u274c" if issue["severity"] == "error" else "\u26a0\ufe0f"
        lines.append(f"      {icon} {issue['id']}: {issue['title']}")
    return lines
