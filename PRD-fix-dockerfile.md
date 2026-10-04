# PRD: `bq --fix-dockerfile` v2 — Dockerfile Scanner + Jev-Approved Auto-Fix

**Version**: 1.2.0
**Last updated**: 2026-10-04
**Author**: DevOps Engineering, Qoin
**Status**: Approved
**Depends on**: bq v0.1.42+, TypeSafe Jev System One API

---

## Daftar Isi

1. [Problem Statement](#1-problem-statement)
2. [Goals & Non-Goals](#2-goals--non-goals)
3. [Personas](#3-personas)
4. [Architecture](#4-architecture)
5. [Rule Collection — 24 Rules](#5-rule-collection--24-rules)
6. [Jev System One Integration (Deep Dive)](#6-jev-system-one-integration-deep-dive)
7. [Auto-Fix Engine](#7-auto-fix-engine)
8. [CLI Interface](#8-cli-interface)
9. [Remote Mode (GitHub API, No Clone)](#9-remote-mode-github-api-no-clone)
10. [Data Collection & PocketBase](#10-data-collection--pocketbase)
11. [Integration Points](#11-integration-points)
12. [Panduan Menambah Rule Baru (Developer Guide)](#12-panduan-menambah-rule-baru-developer-guide)
13. [Panduan Menambah Jev Question Baru](#13-panduan-menambah-jev-question-baru)
14. [Testing Strategy](#14-testing-strategy)
15. [Rollout Plan](#15-rollout-plan)
16. [Risk & Mitigation](#16-risk--mitigation)
17. [Appendix](#appendix)

---

## 1. Problem Statement

### 1.1 Situasi Saat Ini

Tim DevOps Qoin mengelola **100+ repository** Go dan .NET yang masing-masing punya
Dockerfile. Masalah berulang ditemukan saat bulk-fix pipeline Jenkins X:

1. **Build failure** — `ENV sebelum FROM`, placeholder `REGISTRY01/PROJECT` belum
   di-replace, path Bitbucket deprecated, Go version mismatch.
2. **Security risk** — credential leak via `ARG GITHUB_TOKEN`, container run as root,
   secret mount tanpa target.
3. **Image bloat** — single-stage build (1GB+), `apt-get` tanpa cleanup, `pip install`
   tanpa `--no-cache-dir`, `COPY . .` sebelum dependency install.
4. **Inkonsistensi** — sebagian Dockerfile pakai pola legacy, sebagian modern, sebagian
   campuran.

### 1.2 Pain Points

| Pain | Impact | Frekuensi |
|------|--------|-----------|
| Debugging build failure karena Dockerfile syntax | 30-60 menit per incident | 5-10x/minggu |
| Image size bloat (500MB-1.5GB) | Storage cost, deploy lambat | Setiap build |
| Credential leak via ARG | Security audit finding | Ditemukan saat audit |
| Manual review setiap Dockerfile | DevOps bottleneck | Setiap PR |

### 1.3 Solusi Sebelumnya (`--fix-dockerfile` v1)

- Hanya **4 rule** (regex) + 6 regex-based auto-fix
- Tidak ada severity synthesis — semua rule flat
- Tidak ada AI judgment untuk keamanan auto-fix
- Tidak extensible (hardcoded di `builder.py`)
- Tidak ada export (CSV/Markdown) untuk audit trail

---

## 2. Goals & Non-Goals

### 2.1 Primary Goals

| # | Goal | Ukuran |
|---|------|--------|
| G1 | Ekspansi rule collection dari 4 → 24+ checks (build, security, performance, compliance) | Count active rules |
| G2 | Jev System One integration: severity synthesis, fix approval, priority ranking | Jev call success rate |
| G3 | Auto-fix 9 rule dengan Jev safety gate (`safe_to_autofix >= 50%`) | Auto-fix success rate >90% |
| G4 | Extensible collection: rule bisa ditambah via code atau PocketBase (future) | Time-to-add < 30 menit |
| G5 | Export CSV + Markdown untuk audit trail | Format compliance |
| G6 | Backward compat: `pr_fix.py` tetap berfungsi tanpa breaking change | Zero regression |

### 2.2 Success Metrics

| Metric | Target | Pengukuran |
|--------|--------|------------|
| Rule coverage | 24+ rules across 4 categories | `len(KNOWN_ISSUES)` |
| Auto-fix success rate | >90% fix tanpa merusak build | Post-fix `docker build` test |
| False positive rate | <5% | Manual review sample 50 repo |
| Time saved per repo | 20-40 menit | Before/after comparison |
| Jev latency | <3 detik per call | P95 latency monitoring |

### 2.3 Non-Goals

- Tidak menggantikan `docker build` sebagai validator — bq hanya pre-check heuristik.
- Tidak auto-fix issue yang butuh konteks bisnis (mis. pilihan base image, port).
- Tidak replace Hadolint/Dockerfile linter — fokus pada pattern spesifik Qoin.
- Tidak online learning — Jev stateless judgment per invocation.
- Tidak scan image layer (bukan vulnerability scanner).

---

## 3. Personas

### 3.1 DevOps Engineer (Primary)

- **Workflow**: `bq --fix-dockerfile` sebelum merge PR atau saat bulk-fix.
- **Butuh**: Scan cepat, report jelas, auto-fix yang aman.
- **Context**: Tahu Dockerfile tapi tidak selalu ingat semua best practices.

### 3.2 Backend Developer

- **Workflow**: Submit PR dengan Dockerfile, pipeline gagal.
- **Butuh**: Pesan error yang actionable, fix hint yang jelas.
- **Context**: Fokus di Go/.NET code, Dockerfile bukan expertise utama.

### 3.3 CI/CD Pipeline (Automated)

- **Workflow**: `bq --fix-dockerfile --no-jev --scan-only` di pre-merge check.
- **Butuh**: Exit code (0/1/2), parseable output (CSV).
- **Context**: Headless, no interactive, no Jev API key required.

---

## 4. Architecture

### 4.1 Pipeline Overview

```
 User: bq --fix-dockerfile [PATH]
          │
          ▼
 ┌─────────────────────────────────────────────────────────┐
 │  1. PARSE                                               │
 │  ┌──────────────┐    ┌────────────────────────┐        │
 │  │  Read file   │───▶│ _parse_instructions()  │        │
 │  │  content     │    │ Tokenize Dockerfile     │        │
 │  └──────────────┘    │ into instruction list   │        │
 │                      └────────────────────────┘        │
 │                                                         │
 │  2. DETECT (deterministic, zero LLM cost)               │
 │  ┌──────────────────┐  ┌──────────────────────┐        │
 │  │ Regex checks     │  │ Structural checks    │        │
 │  │ 17 rules via     │  │ 7 rules via          │        │
 │  │ re.search()      │  │ instruction ordering │        │
 │  └──────────────────┘  └──────────────────────┘        │
 │          │                       │                      │
 │          └───────┬───────────────┘                      │
 │                  ▼                                       │
 │  3. JEV VERDICT (optional, ~$0.0002/call)               │
 │  ┌────────────────────────────────────────────┐        │
 │  │ POST api.typesafe.ai/v1/systemone          │        │
 │  │                                            │        │
 │  │ state: { findings, counts, file }          │        │
 │  │ questions:                                  │        │
 │  │   overall_severity  → choice (3 options)   │        │
 │  │   safe_to_autofix   → noul (probability)   │        │
 │  │   fix_priority      → choice (4 options)   │        │
 │  │                                            │        │
 │  │ response:                                   │        │
 │  │   answers.overall_severity.choice           │        │
 │  │   answers.safe_to_autofix.noul              │        │
 │  │   answers.fix_priority.choice               │        │
 │  └────────────────────────────────────────────┘        │
 │                  │                                       │
 │                  ▼                                       │
 │  4. AUTO-FIX (if auto_fixable && Jev OK)                │
 │  ┌────────────────────────────────────────────┐        │
 │  │ Safety gate: safe_to_autofix >= 0.5        │        │
 │  │ Backup: Dockerfile.bak                      │        │
 │  │ Apply: 9 regex-based transformations        │        │
 │  │ Verify: idempotent (2nd run = no change)    │        │
 │  └────────────────────────────────────────────┘        │
 │                  │                                       │
 │                  ▼                                       │
 │  5. REPORT                                              │
 │  ┌──────────┐  ┌──────────┐  ┌──────────────┐         │
 │  │  Rich    │  │  Export   │  │  Export       │         │
 │  │  Console │  │  CSV      │  │  Markdown     │         │
 │  └──────────┘  └──────────┘  └──────────────┘         │
 │                                                         │
 │  Exit code: 0=clean, 1=warning, 2=error                 │
 └─────────────────────────────────────────────────────────┘
```

### 4.2 Module Structure

```
build_q/
├── dockerfile_checks.py         ← Rule collection (24 rules)
│   ├── KNOWN_ISSUES[]           ← List of rule dicts (sumber kebenaran)
│   ├── _parse_instructions()    ← Dockerfile parser (line-by-line)
│   ├── _structural_checks()     ← Order-dependent checks (7 rules)
│   ├── analyze_dockerfile()     ← Public API: run semua checks
│   ├── has_error()              ← Backward-compat untuk pr_fix.py
│   ├── format_issues_markdown() ← Markdown formatting (PR body)
│   └── format_issues_console()  ← Console formatting (colored)
│
├── dockerfile_scanner.py        ← Scanner + Jev + auto-fix
│   ├── DockerfileFinding        ← Dataclass per finding
│   ├── ScanResult               ← Dataclass per file + jev_verdict
│   ├── _ask_jev()               ← Jev System One HTTP call
│   ├── _apply_fixes()           ← Auto-fix engine (9 transforms)
│   ├── _render_result()         ← Rich console output
│   ├── export_csv()             ← CSV audit trail
│   ├── export_markdown()        ← Markdown report
│   └── run_dockerfile_scan()    ← Public entry point
│
├── cli.py                       ← CLI wiring
│   └── --fix-dockerfile         ← Routes to run_dockerfile_scan()
│       ├── --scan-only          ← No file modification
│       ├── --no-autofix         ← Report only
│       ├── --no-jev             ← Skip Jev (deterministic only)
│       ├── --export-csv PATH    ← CSV output
│       └── --export-md PATH     ← Markdown output
│
scripts/
└── dockerfile-rules-seed.json   ← PocketBase collection seed
│
tests/
├── test_dockerfile_checks.py    ← 61 tests (rules, regex, structural, compat)
└── test_dockerfile_scanner.py   ← 37 tests (scanner, fix, export, Jev mock)
```

### 4.3 Rule Schema

Setiap entry di `KNOWN_ISSUES` list adalah dict dengan field:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | `str` | **Ya** | Unique slug, kebab-case. Contoh: `legacy-github-secrets` |
| `pattern` | `str \| None` | **Ya** | Regex pattern untuk deteksi. `None` = structural check |
| `severity` | `str` | **Ya** | `"error"` / `"warning"` / `"info"` |
| `category` | `str` | **Ya** | `"build"` / `"security"` / `"performance"` / `"compliance"` |
| `auto_fixable` | `bool` | **Ya** | `True` jika bq bisa auto-fix via regex substitution |
| `title` | `str` | **Ya** | One-line human-readable title (tampil di console + report) |
| `reason` | `str` | **Ya** | Penjelasan WHY ini masalah (untuk edukasi developer) |
| `fix` | `str` | **Ya** | Action yang direkomendasikan (fix hint) |
| `gist_ref` | `str` | **Ya** | Anchor di KNOWN_ISSUES.md gist untuk deep-link docs |

### 4.4 Severity Scale

| Severity | Arti | Exit Code | Auto-fix? | Jev Weight |
|----------|------|-----------|-----------|------------|
| `error` | **Blocker** — build gagal atau security risk serius | 2 | Ya (jika `auto_fixable`) | Tinggi |
| `warning` | Non-blocker — melanggar best practice, perlu dibenahi | 1 | Ya (jika `auto_fixable`) | Sedang |
| `info` | Observasi — bisa intentional, catatan untuk review | 0 | Tidak pernah | Rendah |

### 4.5 Category Glossary

| Category | Scope | Contoh Rule |
|----------|-------|-------------|
| `build` | Dockerfile syntax, FROM, RUN, instruksi order — build failure | `env-before-from`, `registry-literal-placeholder` |
| `security` | Credential leak, root user, secret exposure | `arg-github-token-legacy`, `missing-user` |
| `performance` | Image size, layer optimization, cache efficiency | `apt-get-no-clean`, `copy-all-before-deps` |
| `compliance` | Standar Qoin: EXPOSE, HEALTHCHECK, multi-stage, label | `expose-missing`, `missing-healthcheck` |

---

## 5. Rule Collection — 24 Rules

### 5.1 Build Category (11 rules)

| # | Rule ID | Severity | Auto-fix | Detection | Description |
|---|---------|----------|----------|-----------|-------------|
| 1 | `legacy-github-secrets` | error | Yes | regex | Secret `id=github_token/user` (legacy compose pattern) |
| 2 | `env-before-from` | error | No | regex | Instruksi sebelum FROM (syntax invalid) |
| 3 | `registry-literal-placeholder` | error | No | regex | FROM `REGISTRY01/PROJECT/...` (template unreplaced) |
| 4 | `deprecated-maintainer` | error | Yes | regex | `MAINTAINER` instruction deprecated → `LABEL` |
| 5 | `env-legacy-syntax` | error | Yes | regex | `ENV KEY value` tanpa `=` (ambiguous) |
| 6 | `private-repo-bitbucket` | error | No | regex | `bitbucket.org/qoin` references (deprecated) |
| 7 | `workdir-commented-out` | error | Yes | regex | `#WORKDIR /app` di-comment → binary not found |
| 8 | `go-toolchain-mismatch` | warning | No | regex | Go base image < 1.22 |
| 9 | `from-as-lowercase` | warning | Yes | regex | `as` lowercase (standar: `AS`) |
| 10 | `curl-without-fail` | warning | No | regex | `curl`/`wget` tanpa `-f`/`--fail` di RUN |
| 11 | `base-image-latest` | warning | No | regex | Tag `:latest` (non-deterministic build) |

### 5.2 Security Category (3 rules)

| # | Rule ID | Severity | Auto-fix | Detection | Description |
|---|---------|----------|----------|-----------|-------------|
| 12 | `netrc-secret-wrong-target` | warning | Yes | regex | `id=netrc` tanpa `target=/root/.netrc` |
| 13 | `arg-github-token-legacy` | warning | Yes | regex | `ARG GITHUB_TOKEN` credential leak ke layer |
| 14 | `missing-user` | warning | No | structural | No `USER` instruction → container run root |

### 5.3 Performance Category (7 rules)

| # | Rule ID | Severity | Auto-fix | Detection | Description |
|---|---------|----------|----------|-----------|-------------|
| 15 | `base-image-no-tag` | warning | No | regex | FROM tanpa tag (implicitly `:latest`) |
| 16 | `add-instead-of-copy` | warning | Yes | regex | `ADD` untuk file lokal → gunakan `COPY` |
| 17 | `apt-get-no-clean` | warning | No | regex | `apt-get install` tanpa cleanup (50-200MB+) |
| 18 | `pip-no-cache` | warning | Yes | regex | `pip install` tanpa `--no-cache-dir` |
| 19 | `copy-all-before-deps` | warning | No | structural | `COPY . .` sebelum `go mod download` / `npm install` |
| 20 | `missing-dockerignore` | warning | No | structural | Tidak ada `.dockerignore` (context besar) |
| 21 | `run-too-many-layers` | warning | No | structural | >8 `RUN` terpisah di final stage |

### 5.4 Compliance Category (3 rules)

| # | Rule ID | Severity | Auto-fix | Detection | Description |
|---|---------|----------|----------|-----------|-------------|
| 22 | `expose-missing` | info | No | structural | Tidak ada `EXPOSE` (port undocumented) |
| 23 | `missing-healthcheck` | info | No | structural | Tidak ada `HEALTHCHECK` |
| 24 | `missing-multistage` | info | No | structural | Single-stage build untuk Go/dotnet (image 1GB+) |

### 5.5 Backlog — Future Rules

| Rule ID | Category | Priority | Description | Alasan Ditunda |
|---------|----------|----------|-------------|----------------|
| `secret-in-env` | security | **P1** | ENV berisi password/token literal | Butuh heuristic NLP, false positive tinggi |
| `npm-ci-not-install` | build | P2 | `npm install` di production (harus `npm ci`) | Butuh context: dev vs prod stage |
| `shell-form-entrypoint` | build | P2 | ENTRYPOINT tanpa exec form | Butuh parse JSON vs string |
| `root-user-explicit` | security | P2 | `USER root` di-set eksplisit | Edge case: init container |
| `dotnet-publish-no-trim` | performance | P3 | .NET publish tanpa `-p:PublishTrimmed` | .NET ecosystem bervariasi |
| `alpine-apk-no-cache` | performance | P3 | `apk add` tanpa `--no-cache` | Alpine-specific |
| `multi-stage-unused` | build | P3 | Stage didefinisikan tapi tidak dipakai | Butuh cross-stage analysis |
| `go-cgo-disabled` | performance | P3 | CGO_ENABLED=0 missing untuk static binary | Go-specific |
| `npm-audit-skip` | security | P3 | `npm audit` disabled di build | Controversial |

---

## 6. Jev System One Integration (Deep Dive)

### 6.1 Mengapa Jev?

Deterministic rules bagus untuk deteksi pola yang **pasti**. Tapi beberapa keputusan
butuh **judgment** yang tidak bisa ditangkap oleh regex:

| Kebutuhan | Contoh | Kenapa Deterministic Tidak Cukup |
|-----------|--------|----------------------------------|
| **Severity synthesis** | 3 warning + 1 error → overall `critical`? atau `warn`? | Tergantung kombinasi — 3 warning performance beda impact dari 1 warning security |
| **Fix approval** | Aman auto-fix Dockerfile ini? | Tergantung apakah ada custom pattern yang regex tidak mengerti |
| **Priority ranking** | Fix security dulu atau build dulu? | Tergantung konteks: production deploy vs dev iteration |

Jev System One menjawab pertanyaan-pertanyaan ini dengan **structured decision** (choice
+ noul), bukan free-text. Hasilnya deterministik per state yang sama, dan biayanya
sangat rendah (~$0.0002/call, ~936 input tokens + 107 output tokens).

### 6.2 Request Format

```
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer $TYPESAFE_API_KEY
Content-Type: application/json
```

```json
{
  "model": "jev-latest",
  "state": {
    "file": "docker/Dockerfile",
    "findings_count": 5,
    "error_count": 2,
    "warning_count": 3,
    "findings": [
      {
        "rule": "legacy-github-secrets",
        "severity": "error",
        "category": "build",
        "title": "Dockerfile pakai secret id=github_token (legacy)",
        "auto_fixable": true
      },
      {
        "rule": "missing-user",
        "severity": "warning",
        "category": "security",
        "title": "Tidak ada instruksi USER",
        "auto_fixable": false
      }
    ]
  },
  "questions": {
    "overall_severity": {
      "type": "choice",
      "instructions": "Berikan severity gabungan untuk Dockerfile ini berdasarkan semua findings.",
      "criteria": {
        "clean": "Tidak ada issue — Dockerfile production-ready.",
        "warn": "Ada issue non-blocker, build masih jalan.",
        "critical": "Build gagal atau security risk serius."
      }
    },
    "safe_to_autofix": {
      "type": "noul",
      "instructions": "Apakah AMAN menjalankan auto-fix? Pertimbangkan custom pattern.",
      "criteria": {
        "true": "Aman auto-fix — regex-based tidak merusak logic.",
        "false": "Tidak aman — custom pattern bisa rusak."
      }
    },
    "fix_priority": {
      "type": "choice",
      "instructions": "Prioritas fix berdasarkan impact dan urgency.",
      "criteria": {
        "security": "Fix credential leak / root user dulu.",
        "build": "Fix syntax error / missing dep dulu.",
        "performance": "Fix image size / cache dulu.",
        "compliance": "Fix EXPOSE / HEALTHCHECK dulu."
      }
    }
  }
}
```

### 6.3 Response Format

```json
{
  "answers": {
    "overall_severity": {
      "choice": "critical",
      "confidence": 0.92
    },
    "safe_to_autofix": {
      "noul": 0.85
    },
    "fix_priority": {
      "choice": "security",
      "confidence": 0.78
    }
  },
  "usage": {
    "input_tokens": 936,
    "output_tokens": 107,
    "cost": 0.000234
  }
}
```

### 6.4 Jev Question Types

Jev System One mendukung 2 tipe pertanyaan yang dipakai di sini:

#### `choice` — Categorical Decision

Jev memilih **satu opsi** dari criteria dan memberikan confidence (0.0-1.0).

```json
{
  "type": "choice",
  "instructions": "Apa yang harus dilakukan?",
  "criteria": {
    "option_a": "Kapan pilih A.",
    "option_b": "Kapan pilih B."
  }
}
```

**Response**: `{ "choice": "option_a", "confidence": 0.85 }`

**Kapan pakai**: Keputusan kategorikal — pilih severity, prioritas, tool, action.

#### `noul` — Probability Belief

Jev memberikan **probabilitas** (0.0-1.0) bahwa statement bernilai true.

```json
{
  "type": "noul",
  "instructions": "Apakah X benar?",
  "criteria": {
    "true": "Kapan probabilitas tinggi.",
    "false": "Kapan probabilitas rendah."
  }
}
```

**Response**: `{ "noul": 0.85 }`

**Kapan pakai**: Keputusan boolean probabilistik — safety check, readiness, confidence.

### 6.5 Safety Gate

```
safe_to_autofix >= 0.5  →  proceed auto-fix
safe_to_autofix <  0.5  →  skip, report only
Jev unreachable         →  deterministic checks tetap jalan (fail-safe)
--no-jev flag           →  skip Jev entirely, auto-fix tanpa safety gate
```

### 6.6 Fail-Safe Design

Jev bersifat **optional dan fail-safe**:

1. **Import error** (requests/tui tidak tersedia) → skip, return `None`
2. **No API key** → skip dengan warning di console
3. **Network error / timeout** → skip dengan warning
4. **HTTP error** → skip dengan warning
5. **Invalid response** → skip

Di semua kasus, deterministic checks **tetap berjalan dan memberikan hasil**.
Jev verdict hanya menambah richness, bukan requirement.

---

## 7. Auto-Fix Engine

### 7.1 Fix Matrix

| # | Rule ID | Fix Action | Regex | Risk | Reversible |
|---|---------|-----------|-------|------|------------|
| 1 | `from-as-lowercase` | `as` → `AS` di FROM | `^(FROM\s+\S+)\s+as\s+(\S+)` | None | Ya (`.bak`) |
| 2 | `deprecated-maintainer` | `MAINTAINER x` → `LABEL maintainer="x"` | `^MAINTAINER\s+(.+)` | None | Ya |
| 3 | `env-legacy-syntax` | `ENV KEY value` → `ENV KEY=value` | `^ENV\s+([A-Z_]+)\s+([^=].*)` | Low | Ya |
| 4 | `arg-github-token-legacy` | Remove `ARG GITHUB_*` lines | `^ARG\s+GITHUB_(USER\|TOKEN\|PASSWORD)` | Low | Ya |
| 5 | `netrc-secret-wrong-target` | Add `target=/root/.netrc` | `id=netrc(?!,target=...)` | None | Ya |
| 6 | `workdir-commented-out` | Uncomment `#WORKDIR /app` | `^#\s*(WORKDIR\s+/app)` | Low | Ya |
| 7 | `add-instead-of-copy` | `ADD src dest` → `COPY src dest` | `^ADD\s+(non-URL non-tar)` | Low | Ya |
| 8 | `pip-no-cache` | Add `--no-cache-dir` | `pip\s+install(?!.*--no-cache-dir)` | None | Ya |
| 9 | `legacy-github-secrets` | Report (needs manual migration) | detect only | N/A | N/A |

### 7.2 Safety Protocol

1. **Backup always** — `Dockerfile.bak` dibuat sebelum modifikasi apapun.
2. **Jev gate** — jika Jev aktif dan `safe_to_autofix < 50%`, auto-fix dilewati.
3. **No-Jev mode** (`--no-jev`) — auto-fix tetap jalan tanpa safety gate.
4. **Scan-only mode** (`--scan-only`) — tidak ada modifikasi file sama sekali.
5. **Idempotent** — menjalankan fix 2x menghasilkan output identik (tested).
6. **Report-only items** — rule `legacy-github-secrets` yang butuh migrasi manual
   hanya dilaporkan, tidak di-auto-fix.

### 7.3 Fix Execution Order

Fix diterapkan dalam urutan yang meminimalkan konflik antar-regex:

```
1. FROM ... AS (casing)          ← mengubah baris FROM
2. MAINTAINER → LABEL            ← mengubah/menghapus baris MAINTAINER
3. ENV KEY value → ENV KEY=value ← mengubah baris ENV
4. Remove ARG GITHUB_*           ← menghapus baris ARG
5. Detect legacy secrets         ← report only (no modify)
6. Fix netrc target              ← mengubah inline --mount=
7. Uncomment WORKDIR             ← mengubah baris comment
8. ADD → COPY                    ← mengubah baris ADD
9. pip --no-cache-dir            ← mengubah inline pip command
```

---

## 8. CLI Interface

### 8.1 Usage

```bash
# Default: scan + auto-fix ./Dockerfile
bq --fix-dockerfile

# Specific file
bq --fix-dockerfile ./docker/Dockerfile

# Scan seluruh direktori (recursive, glob **/Dockerfile*)
bq --fix-dockerfile ./my-project/

# Scan only — tidak modifikasi file
bq --fix-dockerfile --scan-only

# Disable auto-fix — hanya report
bq --fix-dockerfile --no-autofix

# Tanpa Jev — deterministic only, lebih cepat, offline
bq --fix-dockerfile --no-jev

# Export untuk audit / CI
bq --fix-dockerfile --export-csv report.csv
bq --fix-dockerfile --export-md report.md

# Kombinasi penuh
bq --fix-dockerfile ./project/ --no-jev --scan-only \
  --export-csv scan.csv --export-md scan.md
```

### 8.2 Exit Codes

| Code | Severity | Arti | CI Action |
|------|----------|------|-----------|
| `0` | clean/info | Tidak ada issue, atau hanya info-level | Pass |
| `1` | warning | Ada warning-level issues | Warn / soft-fail |
| `2` | error | Ada error-level issues atau hard error | Block merge |

### 8.3 Console Output Contoh

```
  Memanggil Jev untuk 1 file dgn findings...
╭──────────────────────────────────────────────────╮
│ bq --fix-dockerfile  docker/Dockerfile           │
│ 1 file * 5 findings * 3 fixes applied * Jev: on  │
╰──────────────────────────────────────────────────╯

-- Dockerfile docker/Dockerfile --
┏━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━┓
┃ Sev      ┃ Rule                         ┃ Pesan                  ┃ Fix?┃
┡━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━┩
│ error    │ deprecated-maintainer        │ Instruksi MAINTAINER   │ Yes │
│ warning  │ from-as-lowercase            │ FROM ... as ... huruf  │ Yes │
│ warning  │ pip-no-cache                 │ pip install tanpa      │ Yes │
│ warning  │ apt-get-no-clean             │ apt-get tanpa cleanup  │     │
│ warning  │ missing-user                 │ Tidak ada USER         │     │
└──────────┴──────────────────────────────┴────────────────────────┴─────┘

Fix hints:
  * deprecated-maintainer  → Ganti MAINTAINER x → LABEL maintainer="x"
  * from-as-lowercase      → Ubah as → AS di FROM statement
  * pip-no-cache           → Tambah --no-cache-dir ke pip install
  * apt-get-no-clean       → Chain && apt-get clean && rm -rf ...
  * missing-user           → Tambah USER appuser di final stage

Jev verdict: severity=warn (90%) * safe_to_autofix=85% * fix_priority=security
tokens: in 936 * out 107 * $0.000234

  3 fix(es) applied:
   * Converted 1 deprecated MAINTAINER to LABEL maintainer=
   * Normalized 1 FROM ... AS ... casing
   * Added --no-cache-dir to 2 pip install command(s)

Overall: ERROR
```

---

## 9. Remote Mode (GitHub API, No Clone)

### 9.1 Motivasi

Bulk-checking 100+ repo Qoin membutuhkan kemampuan scan Dockerfile **tanpa clone**:
- Clone 100 repo = gigabyte bandwidth + disk + waktu.
- Banyak repo hanya perlu dicek, bukan dimodifikasi.
- CI/CD gateway: block merge berdasarkan scan results, tanpa checkout.

### 9.2 Arsitektur Remote Mode

```
User: bq --fix-dockerfile Dockerfile \
        --remote-repo owner/repo \
        --remote-branch staging

          │
          ▼
┌─────────────────────────────────────────────────────────┐
│  1. RESOLVE REPO                                        │
│  normalize_repo() → "owner/repo"                        │
│  _expand_repo() → prepend GITHUB_ORG jika shorthand    │
│                                                         │
│  2. FETCH FILE (GitHub REST API)                        │
│  GET /repos/{owner}/{repo}/contents/{path}?ref={ref}    │
│  Accept: application/vnd.github.v3.raw                  │
│  Authorization: Bearer $GITHUB_TOKEN                    │
│                                                         │
│  (ATAU: list tree jika path = "." atau trailing /)      │
│  GET /repos/{owner}/{repo}/git/trees/{ref}?recursive=1  │
│  → filter: name.startswith("Dockerfile")                │
│                                                         │
│  3. SCAN (same pipeline as local)                       │
│  regex checks + structural checks → findings            │
│                                                         │
│  4. JEV VERDICT (optional)                              │
│  severity + safe_to_autofix + priority                  │
│                                                         │
│  5. REPORT (read-only — no auto-fix)                    │
│  Rich console + CSV/Markdown export                     │
│  Hint: "X issues auto-fixable, clone lalu bq ..."      │
└─────────────────────────────────────────────────────────┘
```

### 9.3 CLI Flags

```bash
# Scan single Dockerfile di remote repo
bq --fix-dockerfile Dockerfile \
  --remote-repo Qoin-Digital-Indonesia/pay-be-topup-manager \
  --remote-branch staging

# Shorthand (jika GITHUB_ORG=Qoin-Digital-Indonesia)
bq --fix-dockerfile Dockerfile \
  --remote-repo pay-be-topup-manager \
  --remote-branch staging

# Scan semua Dockerfile di repo (recursive tree listing)
bq --fix-dockerfile . \
  --remote-repo owner/repo \
  --remote-branch main

# Scan sub-path
bq --fix-dockerfile docker/ \
  --remote-repo owner/repo \
  --remote-branch main

# Dengan Jev + export
bq --fix-dockerfile Dockerfile \
  --remote-repo owner/repo \
  --remote-branch staging \
  --export-csv scan.csv --export-md scan.md

# Tanpa Jev (headless CI)
bq --fix-dockerfile Dockerfile \
  --remote-repo owner/repo \
  --remote-branch main \
  --no-jev
```

| Flag | Default | Description |
|------|---------|-------------|
| `--remote-repo REPO` | (none) | GitHub repo. Format: `owner/repo`, URL, SSH, atau shorthand |
| `--remote-branch REF` | `main` | Branch, tag, atau commit SHA |

### 9.4 Authentication

Remote mode membutuhkan `GITHUB_TOKEN` dengan scope minimal `repo` (contents:read):

```bash
# Via ~/.build-q/.env
GITHUB_TOKEN=ghp_xxxxxxxxxxxx

# Atau via environment
export GITHUB_TOKEN=ghp_xxxxxxxxxxxx
```

Token resolution order:
1. `~/.build-q/.env` (via `load_config()`)
2. Environment variable `GITHUB_TOKEN`
3. Error jika tidak ada

### 9.5 Directory Scan (Tree Listing)

Jika `--fix-dockerfile` diisi `.` atau path diakhiri `/`, scanner melakukan
**tree listing** via GitHub Git Trees API:

```
GET /repos/{owner}/{repo}/git/trees/{ref}?recursive=1
```

Filter: hanya file yang nama filenya dimulai dengan `Dockerfile`
(Dockerfile, Dockerfile.dev, Dockerfile.prod, dll).

Setiap file di-fetch individual via Contents API, lalu di-scan.

### 9.6 Read-Only Mode

Remote mode bersifat **read-only**:
- Tidak ada auto-fix (tidak bisa menulis ke remote).
- Tidak ada `.bak` backup.
- `--scan-only` dan `--no-autofix` flags diabaikan (implicitly scan-only).
- Setelah scan, hint ditampilkan jika ada issue auto-fixable:

```
  3 issue auto-fixable. Untuk auto-fix, clone repo lalu jalankan:
  bq --fix-dockerfile <path/Dockerfile>
```

### 9.7 Implementasi

| Function | File | Description |
|----------|------|-------------|
| `_fetch_remote_file()` | `dockerfile_scanner.py` | Fetch satu file via GitHub API |
| `_list_remote_dockerfiles()` | `dockerfile_scanner.py` | List Dockerfile paths via Git Trees API |
| `run_dockerfile_scan_remote()` | `dockerfile_scanner.py` | Public entry point remote scan |
| CLI routing | `cli.py` | Detect `--remote-repo` → route ke `run_dockerfile_scan_remote()` |

### 9.8 Contoh Output Remote Scan

```
  Remote scan: Qoin-Digital-Indonesia/pay-be-topup-manager@staging path=Dockerfile
  Memanggil Jev untuk 1 file dgn findings...
╭──────────────────────────────────────────────────────────────────────╮
│ bq --fix-dockerfile (remote)                                         │
│ Qoin-Digital-Indonesia/pay-be-topup-manager@staging                  │
│ 1 file * 6 findings * remote (read-only) * Jev: on                   │
╰──────────────────────────────────────────────────────────────────────╯

-- Dockerfile ...pay-be-topup-manager@staging:Dockerfile --
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┓
┃ Sev        ┃ Rule                           ┃ Pesan                  ┃ Fix?  ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━┩
│ error      │ legacy-github-secrets          │ Secret id=github_token │       │
│ warning    │ go-toolchain-mismatch          │ Go base image < 1.22   │       │
│ warning    │ base-image-latest              │ Tag :latest            │       │
│ warning    │ missing-user                   │ No USER instruction    │       │
│ warning    │ copy-all-before-deps           │ COPY . . before deps   │       │
│ info       │ missing-healthcheck            │ No HEALTHCHECK         │       │
└────────────┴────────────────────────────────┴────────────────────────┴───────┘

Jev verdict: severity=critical (95%) * safe_to_autofix=53% * fix_priority=security

Overall: ERROR

  1 issue auto-fixable. Untuk auto-fix, clone repo lalu jalankan:
  bq --fix-dockerfile <path/Dockerfile>
```

### 9.9 Batch Remote Scan (Future — Phase 2)

```bash
# Future: scan multiple repos dari file list
bq --fix-dockerfile Dockerfile --batch repos.txt --no-jev --export-csv bulk.csv

# repos.txt:
# pay-be-topup-manager staging
# pay-be-topup-module staging
# plus-be-service main
```

---

## 10. Data Collection & PocketBase

### 9.1 PocketBase Collection

Collection `build_q_dockerfile_rules` menyimpan rule yang bisa di-update tanpa
rilis bq baru (hot-update via PocketBase admin UI).

- **Seed file**: `scripts/dockerfile-rules-seed.json`
- **Sync command**: `bq --tui-sync scripts/dockerfile-rules-seed.json`
- **Upsert key**: `rule_id`

### 9.2 Rule Priority Hierarchy

```
1. PocketBase collection     ← remote, bisa hot-update tanpa release bq
2. KNOWN_ISSUES (Python)     ← bundled offline fallback
3. Custom JSON [future]      ← ~/.build-q/dockerfile-rules.json
```

### 9.3 Seed JSON Schema

Setiap entry di `build_q_dockerfile_rules`:

```json
{
  "rule_id": "string (unique)",
  "severity": "error | warning | info",
  "category": "build | security | performance | compliance",
  "auto_fixable": "boolean",
  "title": "string (one-line)",
  "description": "string (detailed reason)",
  "detect_hint": "string (regex atau deskripsi deteksi)",
  "fix_hint": "string (action yang direkomendasikan)",
  "source": "string (skill:xxx atau best-practice)",
  "active": "boolean",
  "order": "integer (display order)"
}
```

---

## 11. Integration Points

### 10.1 `bq --pr-fix`

`pr_fix.py` menggunakan `analyze_dockerfile()` + `has_error()` dari
`dockerfile_checks.py`. Dengan ekspansi dari 4 → 24 rules, PR fix otomatis
mendapat coverage lebih luas **tanpa perubahan kode di pr_fix.py**.

### 10.2 `bq --anomaly-scan`

Share arsitektur yang sama: Finding dataclass, Jev integration, CSV/Markdown export.
**Future**: `--anomaly-scan` bisa include Dockerfile checks kalau target adalah repo
directory yang mengandung Dockerfile.

### 10.3 CI/CD Pipeline

```yaml
# GitHub Actions pre-merge check
- name: Dockerfile lint
  run: |
    pip install build-q
    bq --fix-dockerfile --scan-only --no-jev --export-csv report.csv
  continue-on-error: false  # exit code 2 = block merge
```

### 10.4 Jev Web Chat (`--serve`)

**Future**: expose Dockerfile scan via API endpoint `/api/dockerfile-scan` di
serve mode, memungkinkan web UI untuk scan dan review.

---

## 12. Panduan Menambah Rule Baru (Developer Guide)

### 11.1 Decision Tree: Perlu Rule Baru?

```
Apakah issue ini muncul ≥3x di repo Qoin?
  ├── Tidak → Terlalu niche. Cukup dokumentasi manual.
  └── Ya
      │
      Apakah bisa dideteksi dari isi Dockerfile saja?
        ├── Tidak → Butuh context luar (go.mod, compose.yaml)
        │           → Masukkan ke backlog Phase 3 (cross-file checks)
        └── Ya
            │
            Regex cukup atau butuh instruction ordering?
              ├── Regex cukup → Tipe: regex-based
              └── Butuh ordering → Tipe: structural check
                  │
                  Severity?
                    ├── Build gagal / security breach → "error"
                    ├── Best practice violation → "warning"
                    └── Nice to have → "info"
                        │
                        Bisa auto-fix dengan regex?
                          ├── Ya, sederhana dan aman → auto_fixable: True
                          └── Tidak / risky → auto_fixable: False
```

### 11.2 Langkah: Menambah Rule Regex-Based

**Contoh**: Menambah rule `alpine-apk-no-cache` (apk add tanpa --no-cache).

#### Step 1: Tambah entry di `KNOWN_ISSUES`

Edit `build_q/dockerfile_checks.py`, tambah dict baru di list `KNOWN_ISSUES`:

```python
{
    "id": "alpine-apk-no-cache",
    "pattern": r"(?m)apk\s+add(?!.*--no-cache)",
    "severity": "warning",
    "category": "performance",
    "auto_fixable": True,
    "title": "`apk add` tanpa `--no-cache` — image bloat",
    "reason": (
        "apk tanpa --no-cache menyimpan index package di layer. "
        "Menambah 5-10MB per layer tanpa manfaat."
    ),
    "fix": "Tambah `--no-cache` flag ke apk add.",
    "gist_ref": "#alpine-apk-no-cache",
},
```

**Checklist field**:
- [ ] `id`: Unique, kebab-case, deskriptif
- [ ] `pattern`: Regex yang ditest. Pakai `(?m)` untuk multiline. Pakai `(?-i:...)` kalau butuh case-sensitive di dalam `re.IGNORECASE` global
- [ ] `severity`: `error` / `warning` / `info`
- [ ] `category`: `build` / `security` / `performance` / `compliance`
- [ ] `auto_fixable`: `True` hanya kalau regex substitution **pasti aman**
- [ ] `title`: Satu baris, jelas, ada konteks (apa masalahnya)
- [ ] `reason`: WHY ini masalah (bukan WHAT). Sertakan impact nyata
- [ ] `fix`: Actionable. Sertakan command `bq` kalau applicable
- [ ] `gist_ref`: Anchor di KNOWN_ISSUES.md gist

#### Step 2: (Jika auto_fixable) Tambah fix di `_apply_fixes()`

Edit `build_q/dockerfile_scanner.py`, tambah regex substitution di fungsi
`_apply_fixes()`:

```python
# alpine-apk-no-cache
apk_re = re.compile(r"(apk\s+add)\s+(?!.*--no-cache)")
content, n = apk_re.subn(r"\1 --no-cache ", content)
if n:
    fixes.append(f"Added `--no-cache` to {n} apk add command(s)")
```

**Penting**: Perhatikan urutan fix. Letakkan setelah fix yang mungkin mengubah baris
yang sama.

#### Step 3: Tambah entry di PocketBase seed

Edit `scripts/dockerfile-rules-seed.json`, tambah entry baru di array
`build_q_dockerfile_rules`:

```json
{
  "rule_id": "alpine-apk-no-cache",
  "severity": "warning",
  "category": "performance",
  "auto_fixable": true,
  "title": "apk add tanpa --no-cache (image bloat)",
  "description": "apk tanpa --no-cache menyimpan index package 5-10MB per layer.",
  "detect_hint": "apk\\s+add(?!.*--no-cache)",
  "fix_hint": "Tambah --no-cache ke apk add. `bq --fix-dockerfile` auto-add.",
  "source": "best-practice",
  "active": true,
  "order": 25
}
```

#### Step 4: Tulis test

Edit `tests/test_dockerfile_checks.py`, tambah test case:

```python
def test_alpine_apk_no_cache(self):
    content = 'FROM alpine:3.20\nRUN apk add curl git'
    issues = analyze_dockerfile(content)
    assert any(i["id"] == "alpine-apk-no-cache" for i in issues)

def test_alpine_apk_with_cache_ok(self):
    content = 'FROM alpine:3.20\nRUN apk add --no-cache curl git'
    issues = analyze_dockerfile(content)
    assert not any(i["id"] == "alpine-apk-no-cache" for i in issues)
```

Edit `tests/test_dockerfile_scanner.py`, tambah test auto-fix:

```python
def test_fix_apk_no_cache(self):
    content = 'FROM alpine:3.20\nRUN apk add curl git'
    findings = _issues_to_findings(analyze_dockerfile(content))
    new_content, fixes = _apply_fixes(content, findings, None)
    assert "--no-cache" in new_content
```

#### Step 5: Jalankan test

```bash
python3 -m pytest tests/ -v
```

#### Step 6: Update gist (opsional tapi disarankan)

```bash
gh gist edit 35cc4c36e7c7c2d236a1b5149cdbcfd9
# Tambah section ## alpine-apk-no-cache di KNOWN_ISSUES.md
```

### 11.3 Langkah: Menambah Rule Structural

**Contoh**: Menambah rule `shell-form-entrypoint` (ENTRYPOINT tanpa exec form).

#### Step 1: Tambah entry di `KNOWN_ISSUES` dengan `pattern: None`

```python
{
    "id": "shell-form-entrypoint",
    "pattern": None,  # structural check
    "severity": "warning",
    "category": "build",
    "auto_fixable": False,
    "title": "ENTRYPOINT pakai shell form — signal handling broken",
    "reason": (
        "Shell form `ENTRYPOINT command arg` menjalankan via `/bin/sh -c`. "
        "PID 1 adalah shell, bukan app. SIGTERM tidak diteruskan → "
        "container lambat stop (30s timeout)."
    ),
    "fix": "Ubah ke exec form: `ENTRYPOINT [\"command\", \"arg\"]`.",
    "gist_ref": "#shell-form-entrypoint",
},
```

#### Step 2: Tambah logic di `_structural_checks()`

```python
# shell-form-entrypoint
for inst in instructions:
    if inst["instruction"] == "ENTRYPOINT":
        args = inst["args"].strip()
        # Exec form starts with [
        if not args.startswith("["):
            matched_ids.append("shell-form-entrypoint")
        break  # hanya cek ENTRYPOINT terakhir
```

#### Step 3-6: Sama dengan regex-based (seed JSON, test, gist)

### 11.4 Ringkasan File yang Perlu Diubah

| File | Action | Wajib? |
|------|--------|--------|
| `build_q/dockerfile_checks.py` | Tambah entry di `KNOWN_ISSUES` | **Wajib** |
| `build_q/dockerfile_checks.py` | Tambah logic di `_structural_checks()` | Wajib jika structural |
| `build_q/dockerfile_scanner.py` | Tambah regex di `_apply_fixes()` | Wajib jika `auto_fixable` |
| `scripts/dockerfile-rules-seed.json` | Tambah entry di array | **Wajib** |
| `tests/test_dockerfile_checks.py` | Tambah test deteksi | **Wajib** |
| `tests/test_dockerfile_scanner.py` | Tambah test auto-fix | Wajib jika `auto_fixable` |
| KNOWN_ISSUES.md gist | Tambah section dokumentasi | Disarankan |
| `PRD-fix-dockerfile.md` | Update tabel rule collection | Disarankan |

---

## 13. Panduan Menambah Jev Question Baru

### 12.1 Kapan Perlu Question Baru?

Tambah Jev question baru **hanya** jika ada keputusan yang:
1. **Subjective** — tidak bisa dijawab oleh regex/code logic
2. **Contextual** — jawabannya tergantung kombinasi findings
3. **Actionable** — hasilnya langsung dipakai oleh auto-fix atau report
4. **Worth the cost** — setiap question menambah ~30-50 output tokens ($)

### 12.2 Contoh Kasus: Menambah Question `needs_rebuild`

**Skenario**: Setelah auto-fix, apakah user harus rebuild image? Atau fix hanya
cosmetic?

#### Step 1: Design question di `_ask_jev()`

Edit `build_q/dockerfile_scanner.py`, tambah di dict `questions`:

```python
"needs_rebuild": {
    "type": "noul",
    "instructions": (
        "Apakah auto-fix yang dilakukan MEMERLUKAN rebuild image? "
        "Pertimbangkan: apakah fix hanya cosmetic (LABEL, casing) "
        "atau mengubah build logic (dependency, secret mount)."
    ),
    "criteria": {
        "true": "Ya, perlu rebuild — fix mengubah build output.",
        "false": "Tidak perlu rebuild — fix hanya cosmetic/documentation.",
    },
},
```

#### Step 2: Konsumsi response di rendering

```python
rebuild_ans = answers.get("needs_rebuild", {})
rebuild_prob = float(rebuild_ans.get("noul", 0.0))
if rebuild_prob > 0.7:
    console.print("[bold yellow]  Rebuild required setelah fix.[/bold yellow]")
```

#### Step 3: Tambah ke export

Tambah kolom `jev_needs_rebuild` di `export_csv()` dan `export_markdown()`.

#### Step 4: Update seed JSON

Tambah di `_meta.jev_questions`:

```json
"needs_rebuild": {
  "type": "noul",
  "description": "Probabilitas perlu rebuild setelah auto-fix"
}
```

#### Step 5: Tulis test mock

```python
@patch("build_q.dockerfile_scanner._ask_jev")
def test_jev_needs_rebuild_reported(self, mock_jev):
    mock_jev.return_value = {
        "answers": {
            "overall_severity": {"choice": "warn", "confidence": 0.8},
            "safe_to_autofix": {"noul": 0.9},
            "fix_priority": {"choice": "build", "confidence": 0.7},
            "needs_rebuild": {"noul": 0.95},
        },
    }
    # ... test that rebuild message appears
```

### 12.3 Jev Question Design Patterns

| Pattern | Type | Kapan Pakai | Contoh |
|---------|------|-------------|--------|
| **Severity synthesis** | `choice` | Gabungkan multiple findings jadi satu verdict | `overall_severity` |
| **Safety gate** | `noul` | Boolean probabilistik sebagai threshold | `safe_to_autofix` |
| **Priority ranking** | `choice` | Pilih satu dari N kategori untuk didulukan | `fix_priority` |
| **Readiness check** | `noul` | Probabilitas sesuatu ready/aman | `prod_ready`, `needs_rebuild` |
| **Classification** | `choice` | Klasifikasi input ke bucket | `dockerfile_type` (Go/Python/.NET) |

### 12.4 Best Practices Jev Questions

1. **Minimal questions** — setiap question tambah latency dan cost. 3 questions
   sudah cukup untuk kebanyakan use case.
2. **Clear criteria** — Jev hanya sebagus criteria yang diberikan. Tulis criteria
   yang SPESIFIK, bukan generik.
3. **Actionable** — jangan tanya kalau jawaban tidak mengubah behavior apapun.
4. **Fail-safe default** — selalu handle kasus Jev tidak tersedia. Default harus
   konservatif (misal: `safe_to_autofix` default = 1.0 kalau no Jev = fix tetap jalan).
5. **Log usage** — selalu tampilkan token count dan cost untuk transparansi.

---

## 14. Testing Strategy

### 13.1 Test Suite Overview

| File | Tests | Coverage |
|------|-------|----------|
| `tests/test_dockerfile_checks.py` | **61** | Rule integrity (9), regex detection (21), structural checks (15), parser (5), backward compat (7), golden path (3) |
| `tests/test_dockerfile_scanner.py` | **52** | Dataclass (3), auto-fix (12), Jev safety gate (4), CSV/MD export (4), local integration (11), Jev mock (4), remote fetch (2), remote tree list (3), remote scan integration (9) |
| **Total** | **113** | |

### 13.2 Test Categories

#### Collection Integrity (9 tests)

Memastikan `KNOWN_ISSUES` list valid:
- Minimum 22 rules
- Semua rule punya required fields
- ID unik
- Severity valid (error/warning/info)
- Category valid (build/security/performance/compliance)
- auto_fixable adalah boolean

#### Regex Detection (21 tests)

Setiap rule regex ditest dengan:
- **Positive case**: input yang seharusnya match
- **Negative case**: input yang seharusnya TIDAK match

#### Structural Checks (15 tests)

Setiap structural rule ditest dengan:
- Missing/present cases
- Multi-stage awareness (final stage only)
- Edge cases (empty content, single instruction)

#### Auto-Fix (12 tests)

Setiap auto-fix ditest:
- Fix menghasilkan output yang benar
- Clean Dockerfile tidak dimodifikasi
- Multiple fixes bisa diterapkan bersamaan
- **Idempotent**: fix 2x = output sama

#### Jev Integration (4 mocked tests)

- Jev dipanggil hanya untuk file dengan findings
- Jev TIDAK dipanggil untuk clean files
- Jev unsafe → auto-fix blocked
- Jev safe → auto-fix proceeds

### 13.3 Menjalankan Tests

```bash
# Semua test
python3 -m pytest tests/ -v

# Hanya rule collection
python3 -m pytest tests/test_dockerfile_checks.py -v

# Hanya scanner
python3 -m pytest tests/test_dockerfile_scanner.py -v

# Dengan coverage
python3 -m pytest tests/ --cov=build_q --cov-report=term-missing
```

---

## 15. Rollout Plan

### Phase 1: MVP (v0.1.43) — Current Release

- [x] 24 rules (7 error, 14 warning, 3 info) across 4 categories
- [x] 17 regex-based + 7 structural checks
- [x] Jev System One integration (3 questions: severity, safety, priority)
- [x] Auto-fix engine (9 transforms) dengan Jev safety gate
- [x] Rich console report
- [x] CSV + Markdown export
- [x] PocketBase seed file (`scripts/dockerfile-rules-seed.json`)
- [x] 113 tests (61 checks + 52 scanner)
- [x] PRD lengkap dengan developer guide
- [x] Backward compatible dengan `pr_fix.py`
- [x] **Remote mode**: `--remote-repo` + `--remote-branch` (GitHub API, no clone)
- [x] Remote tree listing (scan semua Dockerfile di repo)
- [x] Remote + Jev + CSV/MD export

### Phase 2: Enhancements (v0.1.44)

- [ ] PocketBase rule sync (load remote rules, merge dengan KNOWN_ISSUES)
- [ ] Custom rules via `~/.build-q/dockerfile-rules.json`
- [ ] Batch scan: `bq --fix-dockerfile --batch repos.txt`
- [ ] Web API endpoint di `--serve` mode (`/api/dockerfile-scan`)
- [ ] Git-diff aware: only scan changed Dockerfiles in PR

### Phase 3: Advanced (v0.1.45+)

- [ ] Dockerfile template recommendation (stack detection: Go/Python/.NET/Node)
- [ ] Image size estimation (pre-build heuristic)
- [ ] Cross-file checks (Dockerfile + compose.yaml + cicd.json consistency)
- [ ] Jev question tambahan: `needs_rebuild`, `dockerfile_type`
- [ ] Jev tracking: success/fail rate per rule per repo

---

## 16. Risk & Mitigation

| Risk | Impact | Prob. | Mitigation |
|------|--------|-------|------------|
| Auto-fix merusak Dockerfile | Build failure | Low | `.bak` backup + Jev safety gate + idempotent test |
| False positive → alert fatigue | Adoption drop | Medium | Tunable severity + `--scan-only` + test 50+ real Dockerfiles |
| Jev API unavailable | No verdict | Low | Fail-safe: deterministic checks tetap jalan |
| Regex terlalu broad | False match | Medium | `(?-i:...)` inline flags + comprehensive negative tests |
| Backward compat break `pr_fix.py` | Pipeline failure | Low | `analyze_dockerfile()` API preserved + backward compat tests |
| New rule tanpa test | Silent regression | Medium | Checklist wajib test di developer guide |

---

## Appendix

### A. Comparison with Existing Tools

| Feature | `bq --fix-dockerfile` | Hadolint | Docker Scout | Dockle |
|---------|----------------------|----------|-------------|--------|
| Qoin-specific rules | **Yes** (bitbucket, netrc, jx) | No | No | No |
| Auto-fix | **Yes** (9 rules, Jev-approved) | No | No | No |
| AI verdict | **Yes** (Jev System One) | No | Yes (paid) | No |
| Offline mode | **Yes** | Yes | No | Yes |
| PocketBase extensible | **Yes** | No | No | No |
| Structural checks | **Yes** (7 rules) | Yes | No | Yes |
| Cost | Free (Jev ~$0.0002/call) | Free | Paid | Free |
| Exit code CI | Yes | Yes | Yes | Yes |
| Export CSV/MD | **Yes** | SARIF | JSON | JSON |

### B. Glossary

| Term | Definition |
|------|-----------|
| **Jev** | TypeSafe System One model untuk structured decision-making (AI judgment) |
| **noul** | Probability type: 0.0-1.0 continuous belief. Dipakai untuk safety gate |
| **choice** | Categorical type: pilih satu dari criteria. Dipakai untuk severity/priority |
| **KNOWN_ISSUES** | Python list di `dockerfile_checks.py` — sumber kebenaran rule collection |
| **structural check** | Check yang butuh parsing instruction order (bukan regex sederhana) |
| **auto_fixable** | Rule yang bisa di-fix secara aman oleh regex substitution |
| **safety gate** | Jev `safe_to_autofix >= 0.5` threshold yang mengontrol auto-fix |
| **fail-safe** | Jev gagal → deterministic checks tetap jalan, auto-fix tanpa gate |
| **DooD** | Docker-outside-of-Docker: mount host socket ke container |
| **PocketBase** | Self-hosted backend (DB + API) untuk menyimpan rule collection remote |

### C. File Reference

| File | Path | Fungsi |
|------|------|--------|
| Rule collection | `build_q/dockerfile_checks.py` | 24 rules + parser + analyzer |
| Scanner | `build_q/dockerfile_scanner.py` | Jev + auto-fix + export |
| CLI wiring | `build_q/cli.py` | `--fix-dockerfile` flag routing |
| PocketBase seed | `scripts/dockerfile-rules-seed.json` | Remote rule collection |
| Test checks | `tests/test_dockerfile_checks.py` | 61 tests |
| Test scanner | `tests/test_dockerfile_scanner.py` | 37 tests |
| PRD | `PRD-fix-dockerfile.md` | Dokumen ini |

### D. Quick Reference — Menambah Rule Baru

```
1. Tentukan tipe: regex atau structural
2. Tentukan severity: error / warning / info
3. Tentukan category: build / security / performance / compliance
4. Tentukan auto_fixable: true (jika regex substitution aman) / false

5. Edit dockerfile_checks.py:
   - Tambah dict di KNOWN_ISSUES
   - (structural) Tambah logic di _structural_checks()

6. (auto_fixable) Edit dockerfile_scanner.py:
   - Tambah regex substitution di _apply_fixes()

7. Edit dockerfile-rules-seed.json:
   - Tambah entry di build_q_dockerfile_rules array

8. Tulis test:
   - test_dockerfile_checks.py: positive + negative case
   - (auto_fixable) test_dockerfile_scanner.py: fix test

9. Jalankan: python3 -m pytest tests/ -v

10. (Opsional) Update KNOWN_ISSUES.md gist + PRD tabel
```
