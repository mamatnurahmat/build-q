# Panduan Setup MCP `bq` untuk User Baru

> Panduan langkah demi langkah memasang **MCP server build-q (`bq --mcp`)** dan menghubungkannya ke tiga agent:
> **Claude Code**, **Antigravity (`agy`)**, dan **pi agent** — lengkap dengan contoh kasus nyata.
>
> Versi acuan: `build-q 0.1.46` · MCP SDK `mcp 2.3.0` · 18 tools · 4 resources · 3 prompts.

---

## Daftar Isi

1. [Apa itu MCP `bq`?](#1-apa-itu-mcp-bq)
2. [Arsitektur singkat](#2-arsitektur-singkat)
3. [Prasyarat](#3-prasyarat)
4. [Langkah 1 — Install `bq` + dependency MCP](#4-langkah-1--install-bq--dependency-mcp)
5. [Langkah 2 — Konfigurasi `bq` (`~/.build-q/.env`)](#5-langkah-2--konfigurasi-bq-build-qenv)
6. [Langkah 3 — Uji MCP server secara mandiri](#6-langkah-3--uji-mcp-server-secara-mandiri)
7. [Langkah 4a — Hubungkan ke Claude Code](#7-langkah-4a--hubungkan-ke-claude-code)
8. [Langkah 4b — Hubungkan ke Antigravity (`agy`)](#8-langkah-4b--hubungkan-ke-antigravity-agy)
9. [Langkah 4c — Hubungkan ke pi agent](#9-langkah-4c--hubungkan-ke-pi-agent)
10. [Katalog tools, resources & prompts](#10-katalog-tools-resources--prompts)
11. [Contoh kasus per agent](#11-contoh-kasus-per-agent)
12. [Keamanan & guardrail](#12-keamanan--guardrail)
13. [Mode remote (SSE)](#13-mode-remote-sse)
14. [Troubleshooting](#14-troubleshooting)
15. [Checklist akhir](#15-checklist-akhir)

---

## 1. Apa itu MCP `bq`?

**MCP (Model Context Protocol)** adalah protokol standar agar AI agent bisa memanggil *tools* eksternal secara terstruktur (input JSON schema → output JSON), bukan menyusun perintah shell lalu membaca stdout.

`bq --mcp` menjalankan `bq` sebagai **MCP server**. Semua kemampuan CLI `bq` (scan Dockerfile, scan manifest K8s, cek pipeline, build image, GitOps, SOPS, dsb.) diekspos sebagai **18 tools** yang bisa dipanggil langsung oleh agent.

Manfaatnya:

| Tanpa MCP | Dengan MCP |
|---|---|
| Agent menebak flag CLI (`bq --fix-dockerfile --scan-only ...`) | Agent memanggil `dockerfile_scan(path=".")` dengan parameter jelas |
| Output berwarna (ANSI) harus di-parse | Output JSON: `exit_code`, `stdout`, `data`, `error` |
| Tidak ada jejak | Setiap panggilan tercatat di `~/.build-q/.mcp-audit.log` |
| Agent tidak tahu mana yang berbahaya | Deskripsi tool menandai `DESTRUCTIVE` / `EXTERNAL ACTION` |

---

## 2. Arsitektur singkat

```
┌──────────────────┐   stdio (JSON-RPC)   ┌───────────────────────────┐
│  Claude Code     │ ───────────────────▶ │                           │
│  Antigravity agy │   spawn: bq --mcp    │   bq MCP server           │──▶ Docker / buildx
│  pi agent        │ ◀─────────────────── │   (build_q/mcp/server.py) │──▶ GitHub API
└──────────────────┘    hasil JSON        │                           │──▶ kubectl / cluster
                                          └───────────────────────────┘──▶ Jenkins X webhook
                                                     │                ──▶ SOPS + age
                                                     ▼
                                      ~/.build-q/.env  (config & token)
                                      ~/.build-q/.mcp-audit.log
```

Poin penting:

- **Agent yang menjalankan `bq --mcp`** sebagai proses anak (transport `stdio`). Anda **tidak perlu** menjalankan server manual.
- `bq` membaca kredensial dari `~/.build-q/.env`, `gh`, `kubectl` context, dan `docker login` **milik user lokal** — jadi agent punya hak akses yang sama dengan Anda. Itulah sebabnya guardrail (bagian 12) penting.
- Struktur kode:

  ```
  build_q/mcp/
  ├── server.py      # registrasi 18 tools + entry point run_mcp_server()
  ├── resources.py   # bq://version, bq://config, bq://doctor, bq://catalog
  ├── prompts.py     # deploy-new-service, scan-and-fix, rollout-update
  ├── _capture.py    # tangkap stdout/stderr, strip ANSI, audit log
  └── tools/         # scan, infra, cicd, build, gitops, k8s, sops
  ```

---

## 3. Prasyarat

### 3.1 Wajib

| Kebutuhan | Kegunaan | Install (macOS) | Cek |
|---|---|---|---|
| Python ≥ 3.10 | Runtime `bq` + MCP SDK (kode memakai sintaks `str \| None`) | `brew install python` | `python3 --version` |
| pipx | Install CLI terisolasi | `brew install pipx && pipx ensurepath` | `pipx --version` |
| Git | Deteksi repo/branch | `brew install git` | `git --version` |
| Akses GitHub | Baca `cicd.json`, scan remote, PR GitOps | `brew install gh && gh auth login` **atau** PAT di `GITHUB_TOKEN` | `gh auth status` |
| Minimal 1 agent | Klien MCP | Claude Code / `agy` / `pi` | `claude --version`, `agy --help`, `pi --help` |

### 3.2 Opsional (tergantung tools yang dipakai)

| Kebutuhan | Dibutuhkan oleh tools | Install |
|---|---|---|
| Docker Desktop / Colima + buildx | `docker_build`, `docker_build_preview`, `build_doctor` | `brew install --cask docker` |
| `docker login` ke Docker Hub | `docker_build` (push), `image_check` untuk repo private | `docker login` |
| kubectl + kubeconfig | `gitops_match_check`, `k8s_set_image`, `gitops_bootstrap --apply_secret` | `brew install kubectl` |
| sops + age (key di `~/.config/sops/age/keys.txt`) | `sops_encrypt`, `sops_decrypt` | `brew install sops age` |
| Akses PocketBase IDP | Fallback `cicd.json`/secret (`PB_API=true`) | `bq --pb-login` |

### 3.3 Hak akses yang perlu diminta ke tim DevOps

- Membership org GitHub `Qoin-Digital-Indonesia` (minimal read ke repo service + write ke repo `gitops` bila ingin buat PR).
- PAT GitHub dengan scope `repo`, `read:user`, `workflow`, `actions:write` (bila memakai mode native `GH_CLI=false`).
- Kubeconfig untuk cluster yang relevan (mis. `hw-dev`). **Akses production cukup read-only** untuk keperluan cek drift.
- Public key age Anda didaftarkan sebagai recipient SOPS bila perlu dekripsi.

---

## 4. Langkah 1 — Install `bq` + dependency MCP

```bash
# User baru (belum punya bq)
pipx install 'build-q[mcp]'

# Sudah punya bq, cukup tambahkan MCP SDK
pipx inject build-q mcp

# Verifikasi
bq --version                     # build-q (bq) 0.1.46 atau lebih baru
bq --help | grep -- --mcp        # harus muncul --mcp, --mcp-transport, --mcp-port, --mcp-host
pipx runpip build-q show mcp     # Version: 2.x
```

> ⚠️ **Catatan versi MCP SDK.** Server memakai `mcp.server.mcpserver.MCPServer`, yang tersedia di **MCP SDK 2.x**.
> Bila muncul `ModuleNotFoundError: No module named 'mcp.server.mcpserver'`, upgrade SDK-nya:
>
> ```bash
> pipx inject build-q 'mcp>=2' --force
> ```

Catat **path absolut** `bq` — akan dipakai saat registrasi ke agent (agent GUI/daemon sering tidak membaca `PATH` shell Anda):

```bash
which bq
# contoh: /Users/<user>/.local/bin/bq
```

---

## 5. Langkah 2 — Konfigurasi `bq` (`~/.build-q/.env`)

```bash
bq --init        # buat ~/.build-q/.env + Docker buildx builder
bq --config      # tampilkan konfigurasi aktif (token di-mask)
```

Edit `~/.build-q/.env`, minimal isi bagian berikut:

```env
# --- GitHub ---
GITHUB_ORG=Qoin-Digital-Indonesia
GH_CLI=false                    # jalur native (REST API), butuh token di bawah
GITHUB_USER=<username-github>
GITHUB_TOKEN=ghp_xxx            # scope: repo, read:user, workflow, actions:write

# --- Registry ---
REGISTRY_URL=loyaltolpi
DOCKERHUB_ORG=loyaltolpi

# --- GitOps ---
GITOPS_REPO=Qoin-Digital-Indonesia/gitops
GITOPS_BRANCH=main
GITOPS_INFRA=cce                # cce (Huawei CCE, cluster hw-*) | k8s (SLS, cluster sls-*)

# --- Jenkins X webhook ---
WEBHOOK_TRIGGER_URL=https://cicd-hw.qoin.id/trigger
JX_KUBE_CONTEXT=hw-dev
JX_KUBE_NAMESPACE=jenkins-x
```

> 🔒 `~/.build-q/.env` berisi token — **jangan pernah di-commit** dan jangan ditempel ke chat agent. Pastikan permission-nya ketat: `chmod 600 ~/.build-q/.env`.

Jalankan preflight:

```bash
bq --doctor
```

Semua item kritis (Docker, buildx, Git, GitHub token) harus ✅ sebelum lanjut. Item yang tidak relevan (mis. Docker bila Anda hanya ingin scan) boleh dilewati.

---

## 6. Langkah 3 — Uji MCP server secara mandiri

Sebelum menghubungkan ke agent, pastikan server bisa start:

```bash
# Server stdio akan "diam" menunggu input JSON-RPC — itu normal. Tekan Ctrl+C untuk keluar.
bq --mcp

# Cek log start
tail -n 3 ~/.build-q/.mcp-audit.log
# {"ts":"...","level":"INFO","msg":"bq-mcp-server v0.1.46 starting (stdio)"}
```

Opsional — uji interaktif dengan **MCP Inspector** (butuh Node.js):

```bash
npx @modelcontextprotocol/inspector "$(which bq)" --mcp
```

Buka URL yang ditampilkan → tab **Tools** → harus ada 18 tools. Coba jalankan `build_doctor` atau `config_show`.

---

## 7. Langkah 4a — Hubungkan ke Claude Code

### 7.1 Registrasi (cara yang direkomendasikan)

```bash
# Scope user → tersedia di semua project
claude mcp add bq -s user -- "$(which bq)" --mcp

# Verifikasi
claude mcp list
# bq: /Users/<user>/.local/bin/bq --mcp - ✓ Connected
```

Pilihan scope:

| Scope | Flag | Disimpan di | Kapan dipakai |
|---|---|---|---|
| local (default) | `-s local` | `~/.claude.json` (per project) | Hanya untuk satu project di mesin Anda |
| user | `-s user` | `~/.claude.json` (global) | **Rekomendasi** — semua project |
| project | `-s project` | `.mcp.json` di root repo | Dibagikan ke tim lewat git |

Contoh `.mcp.json` (scope project) bila ingin dibagikan ke tim:

```json
{
  "mcpServers": {
    "bq": {
      "type": "stdio",
      "command": "bq",
      "args": ["--mcp"]
    }
  }
}
```

> Di `.mcp.json` yang di-commit, pakai `"command": "bq"` (bukan path absolut milik Anda) agar portabel; setiap anggota tim cukup memastikan `bq` ada di `PATH`.

### 7.2 Verifikasi di dalam sesi

```text
claude
> /mcp
```

Status `bq` harus **connected** dengan 18 tools. Di Claude Code, nama tools menjadi `mcp__bq__<nama_tool>`, misalnya `mcp__bq__dockerfile_scan`.

### 7.3 Atur izin (permission) tools

Tambahkan ke `~/.claude/settings.json` agar tools **read-only** tidak selalu minta konfirmasi, sementara tools berisiko **tetap ditanya**:

```json
{
  "permissions": {
    "allow": [
      "mcp__bq__build_doctor",
      "mcp__bq__config_show",
      "mcp__bq__dockerfile_scan",
      "mcp__bq__dockerfile_scan_remote",
      "mcp__bq__k8s_anomaly_scan",
      "mcp__bq__pipeline_check",
      "mcp__bq__gitops_match_check",
      "mcp__bq__image_check",
      "mcp__bq__webhook_status",
      "mcp__bq__docker_build_preview"
    ],
    "ask": [
      "mcp__bq__docker_build",
      "mcp__bq__pipeline_trigger",
      "mcp__bq__gitops_set_image",
      "mcp__bq__gitops_bootstrap",
      "mcp__bq__k8s_set_image",
      "mcp__bq__sops_decrypt"
    ]
  }
}
```

> Catatan: README menulis contoh `mcpServers` di `.claude/settings.json`. Untuk Claude Code, tempat resmi server MCP adalah `claude mcp add` (→ `~/.claude.json`) atau `.mcp.json`. Pakai salah satu dari keduanya agar server pasti terbaca.

### 7.4 Memakai MCP prompts

Prompt `bq` muncul sebagai slash command:

```text
> /mcp__bq__scan-and-fix
> /mcp__bq__rollout-update
> /mcp__bq__deploy-new-service
```

Claude akan meminta argumen (`repo`, `ref`, `env`/`namespace`) lalu menjalankan urutan tools sesuai template.

---

## 8. Langkah 4b — Hubungkan ke Antigravity (`agy`)

### 8.1 Registrasi via CLI

```bash
# Flag harus diletakkan SEBELUM nama server; pakai `--` karena argumen diawali '-'
agy mcp add bq -- "$(which bq)" --mcp

# Verifikasi
agy mcp list
# NAME  TYPE   STATUS   COMMAND/URL
# bq    stdio  enabled  /Users/<user>/.local/bin/bq --mcp
```

Perintah lain yang berguna:

```bash
agy mcp disable bq      # nonaktifkan sementara
agy mcp enable bq
agy mcp remove bq
```

### 8.2 Registrasi manual (alternatif)

Konfigurasi tersimpan di `~/.gemini/antigravity/mcp_config.json` (IDE juga membaca `~/.gemini/config/mcp_config.json`):

```json
{
  "mcpServers": {
    "bq": {
      "command": "/Users/<user>/.local/bin/bq",
      "args": ["--mcp"]
    }
  }
}
```

Setelah konfigurasi berubah, restart sesi `agy` / reload Antigravity IDE. Schema tools akan di-cache di `~/.gemini/antigravity-cli/mcp/bq/*.json` (18 file) — ini tanda koneksi berhasil.

### 8.3 Aturan agent (AGENTS.md)

Antigravity membaca `AGENTS.md` di workspace. Tambahkan aturan pemakaian `bq` agar agent disiplin, contoh:

```markdown
## Aturan MCP bq
- Untuk scan/cek, utamakan tools MCP bq (dockerfile_scan, k8s_anomaly_scan, pipeline_check, image_check).
- Tools bertanda DESTRUCTIVE / EXTERNAL ACTION (docker_build push=true, pipeline_trigger,
  gitops_set_image, gitops_bootstrap, k8s_set_image) WAJIB minta approval user dulu.
- Jalankan dulu dengan dry_run=true bila parameter tersedia.
- k8s_set_image DILARANG untuk namespace production-* — pakai jalur GitOps PR.
```

---

## 9. Langkah 4c — Hubungkan ke pi agent

### 9.1 Registrasi via CLI

```bash
pi mcp add bq \
  --exposure deferred \
  --description "build-q Docker, CI/CD, GitOps, Kubernetes, SOPS tools" \
  -- "$(which bq)" --mcp

# Verifikasi (exit code 1 bila ada server gagal)
pi mcp list
# bq: connected, 18 tools (deferred, global)
#   tools: dockerfile_scan, dockerfile_scan_remote, k8s_anomaly_scan, ...
#   resources: 4
```

Opsi penting:

| Opsi | Arti |
|---|---|
| `--exposure codemode` (default) | Tools dipanggil lewat kode (codemode) |
| `--exposure deferred` | **Rekomendasi** — tools baru dimuat saat dibutuhkan, hemat konteks |
| `--exposure direct` | Semua 18 tools langsung masuk prompt |
| `--exposure hidden` | Server tersambung tapi tools disembunyikan |
| `--cwd <dir>` | Working directory proses `bq` (berpengaruh ke `path="."` pada `dockerfile_scan`) |
| `--env KEY=VALUE` | Env tambahan, mis. `BQ_HOME=/path/lain` |
| `-l, --local` | Simpan di `.pi/mcp.json` project (hanya untuk project trusted) |

### 9.2 Registrasi manual (alternatif)

File global: `~/.pi/agent/mcp.json`

```json
{
  "mcpServers": {
    "bq": {
      "command": "/Users/<user>/.local/bin/bq",
      "args": ["--mcp"],
      "exposure": "deferred",
      "description": "build-q Docker, CI/CD, GitOps, Kubernetes, SOPS tools"
    }
  }
}
```

### 9.3 Menjalankan pi hanya dengan tools bq

```bash
# Sesi fokus: tools bawaan baca/bash + seluruh tools MCP bq
pi --tools read,bash,'mcp__bq__*'

# Matikan semua MCP (debug)
pi --no-mcp
```

---

## 10. Katalog tools, resources & prompts

### 10.1 Tools (18) dikelompokkan per tingkat risiko

**Tier 1 — Read-only (aman)**

| Tool | Parameter utama | Kegunaan |
|---|---|---|
| `build_doctor` | – | Preflight Docker, buildx, builder, kredensial, Git |
| `config_show` | – | Tampilkan config aktif (token di-mask) |
| `dockerfile_scan` | `path="."`, `use_jev` | Scan Dockerfile lokal (22+ rule) |
| `dockerfile_scan_remote` | `repo`, `ref="main"`, `dockerfile_path` | Scan Dockerfile di GitHub tanpa clone |
| `k8s_anomaly_scan` | `path`, `glob_pattern="*_deployment.yaml"` | Scan manifest K8s (replicas, limits, securityContext, dst.) |
| `pipeline_check` | `repo`, `ref`, `namespace`, `infra` | Kesiapan CI/CD: ref, cicd.json, artefak, image, GitOps |
| `gitops_match_check` | `namespace`, `deployment`, `gitops_repo`, `gitops_path` | Bandingkan image live vs YAML GitOps (drift) |
| `image_check` | `image` | Cek `image:tag` sudah ada di Docker Hub |
| `webhook_status` | `repo` | Cek webhook CI/CD terpasang di repo |
| `docker_build_preview` | `repo`, `ref`, `tag` | Tampilkan perintah buildx tanpa eksekusi |

**Tier 2 — Ubah file lokal (reversible)**

| Tool | Parameter utama | Catatan |
|---|---|---|
| `dockerfile_fix` | `path` | Auto-fix Dockerfile, backup ke `.bak` |
| `sops_encrypt` | `path`, `recipients` | Enkripsi in-place dengan age |
| `sops_decrypt` | `path`, `in_place=false` | ⚠️ Mengembalikan **plaintext rahasia** ke konteks agent |

**Tier 3 — Aksi eksternal (perlu approval)**

| Tool | Parameter utama | Dampak |
|---|---|---|
| `docker_build` | `repo`, `ref`, `push`, `tag`, `dry_run` | Build; `push=true` mendorong image ke registry |
| `pipeline_trigger` | `repo`, `ref`, `sha`, `dry_run` | Memicu build Jenkins X di cluster |
| `gitops_set_image` | `repo`, `path`, `image`, `branch` | Commit + push ke repo GitOps |
| `gitops_bootstrap` | `source_repo`, `ref`, `gitops_repo`, `path_yaml`, ... | Generate Secret/Deployment/Service/HPA/PDB + buat PR |
| `k8s_set_image` | `namespace`, `deployment`, `image` | Hot-patch deployment **live** di cluster |

### 10.2 Resources (4)

| URI | Isi |
|---|---|
| `bq://version` | Versi + daftar kapabilitas |
| `bq://config` | Konfigurasi aktif (ter-mask) |
| `bq://doctor` | Hasil preflight |
| `bq://catalog` | Katalog tool Jev |

### 10.3 Prompts (3)

| Prompt | Argumen | Alur |
|---|---|---|
| `scan-and-fix` | `repo`, `ref` | scan Dockerfile remote → fix → scan manifest K8s → laporan |
| `rollout-update` | `repo`, `ref`, `namespace` | image_check → build → set-image (GitOps/imperatif) → verifikasi |
| `deploy-new-service` | `repo`, `ref`, `env` | doctor → pipeline_check → scan → build+push → bootstrap → match_check |

### 10.4 Format respons

```json
{ "exit_code": 0, "stdout": "...", "stderr": "", "elapsed_ms": 123, "data": { } }
```

Bila gagal: `{"exit_code": 2, "error": "ValueError: ...", "error_type": "ValueError", "elapsed_ms": 1}`.
Aturan umum untuk agent: **berhenti bila `exit_code != 0`** dan laporkan ke user.

---

## 11. Contoh kasus per agent

> Nama repo/namespace di bawah adalah contoh dari lingkungan Qoin. Sesuaikan dengan service Anda.

### 11.1 Claude Code — Audit Dockerfile + manifest sebelum release

**Konteks:** Anda ingin memastikan `pay-be-topup-manager` siap dirilis ke production.

```text
$ cd ~/gitops && claude

> Tolong audit kesiapan release pay-be-topup-manager tag v1.4.2:
  scan Dockerfile di GitHub, cek pipeline-nya, cek image sudah ada di registry,
  dan scan manifest di cce/production-payout. Jangan ubah apa pun, laporkan saja.
```

Alur yang dijalankan Claude:

| # | Tool | Argumen | Hasil yang diharapkan |
|---|---|---|---|
| 1 | `mcp__bq__dockerfile_scan_remote` | `repo=Qoin-Digital-Indonesia/pay-be-topup-manager`, `ref=v1.4.2` | Daftar issue per severity |
| 2 | `mcp__bq__pipeline_check` | `repo=...`, `ref=v1.4.2`, `infra=cce` | ref ✅, cicd.json ✅, image status |
| 3 | `mcp__bq__image_check` | `image=loyaltolpi/pay-be-topup-manager:v1.4.2` | `exists: true/false` |
| 4 | `mcp__bq__k8s_anomaly_scan` | `path=cce/production-payout` | Anomali replicas/limits/securityContext |

Semua tools di atas masuk daftar `allow` (bagian 7.3), jadi berjalan tanpa prompt izin. Claude lalu merangkum: *"Dockerfile 2 warning (HEALTHCHECK belum ada), image v1.4.2 sudah READY, 1 deployment hanya replicas=1 di production"*.

**Lanjutan (aksi, butuh approval):**

```text
> Update image di GitOps ke v1.4.2 lewat PR, jangan push ke main.
```

Claude akan memakai `mcp__bq__gitops_set_image` — karena tool ini ada di daftar `ask`, Claude Code **meminta konfirmasi** dulu. Sesuai aturan tim, minta agent memakai branch `release/...` + PR, bukan push langsung ke `main`.

### 11.2 Antigravity (`agy`) — Investigasi drift image di cluster

**Konteks:** ArgoCD menunjukkan OutOfSync terus-menerus untuk satu service; Anda curiga image live berbeda dengan YAML GitOps.

```text
$ agy
> Cek apakah deployment pay-be-topup-module di namespace production-payout
  sama image-nya dengan file cce/production-payout/pay-be-topup-module_deployment.yaml
  di repo Qoin-Digital-Indonesia/gitops branch main.
```

Alur:

1. `gitops_match_check(namespace="production-payout", deployment="pay-be-topup-module", gitops_repo="Qoin-Digital-Indonesia/gitops", gitops_path="cce/production-payout/pay-be-topup-module_deployment.yaml")`
   → `MATCH` atau `NOT_MATCH` beserta kedua tag image.
2. Bila `NOT_MATCH`: `image_check(image="loyaltolpi/pay-be-topup-module:<tag-gitops>")` untuk memastikan image target memang ada.
3. Agent **menyarankan** perbaikan via GitOps PR — **tidak** memanggil `k8s_set_image` karena namespace `production-*` dilindungi (lihat aturan di `AGENTS.md`, bagian 8.3).

> Tips: pastikan `kubectl config current-context` mengarah ke cluster yang benar (`hw-pro-p`, `sls-pro-q`, dst.) sebelum memulai, karena `bq` memakai kubeconfig Anda.

### 11.3 pi agent — Onboard repo baru ke CI/CD (develop)

**Konteks:** Repo baru `pay-be-voucher-service` perlu dicek dan dibangun di environment develop.

```bash
cd ~/pay-be-voucher-service
pi --tools read,bash,'mcp__bq__*'
```

```text
> Repo ini baru. Cek webhook CI/CD-nya, scan & perbaiki Dockerfile lokal,
  lalu preview build untuk branch develop. Trigger pipeline hanya kalau saya setujui.
```

Alur:

| # | Tool | Catatan |
|---|---|---|
| 1 | `webhook_status(repo="Qoin-Digital-Indonesia/pay-be-voucher-service")` | Bila belum terpasang → sarankan `bq --gh-action-init` |
| 2 | `dockerfile_scan(path=".")` | Path relatif terhadap `cwd` server — set `--cwd` atau beri path absolut |
| 3 | `dockerfile_fix(path=".")` | Ubah file lokal, backup `.bak`; tinjau `git diff Dockerfile` |
| 4 | `docker_build_preview(repo=..., ref="develop")` | Lihat perintah buildx tanpa build |
| 5 | `pipeline_trigger(repo=..., ref="develop", dry_run=true)` | Simulasi dulu |
| 6 | `pipeline_trigger(repo=..., ref="develop")` | **Hanya setelah user setuju** |

> ⚠️ Karena `bq` di pi dijalankan dengan `cwd` dari konfigurasi server (mis. `--cwd ~/build-q`), `path="."` menunjuk ke direktori itu, **bukan** direktori sesi pi. Untuk tools berbasis path lokal, minta agent memakai **path absolut** (mis. `path="/Users/<user>/pay-be-voucher-service"`).

### 11.4 Kasus bersama — Workflow prompt `scan-and-fix`

Bisa dijalankan dari agent mana pun yang mendukung MCP prompts:

```text
Claude Code : /mcp__bq__scan-and-fix   → repo=Qoin-Digital-Indonesia/pay-be-topup-manager, ref=develop
agy / pi    : "Jalankan prompt scan-and-fix dari server bq untuk repo ... ref develop"
```

---

## 12. Keamanan & guardrail

1. **Kredensial agent = kredensial Anda.** Semua aksi `bq` memakai token GitHub, Docker login, dan kubeconfig Anda. Gunakan token dengan scope minimum.
2. **Read-only boleh otomatis, aksi wajib approval.** Terapkan daftar `allow`/`ask` (Claude Code), aturan `AGENTS.md` (agy), dan instruksi sistem/approval (pi).
3. **Production dilindungi.** Jangan izinkan `k8s_set_image` atau `gitops_bootstrap apply_secret=true` ke namespace `production-*` tanpa izin tertulis. Jalur yang benar: GitOps PR → review → merge → ArgoCD sync.
4. **Tidak push langsung ke `main`.** Untuk `gitops_set_image` / `gitops_bootstrap`, pakai branch `release/*` / `feat/*` + Pull Request.
5. **Gunakan `dry_run=true` dulu** pada `docker_build` dan `pipeline_trigger`.
6. **Hati-hati `sops_decrypt`.** Plaintext akan masuk ke konteks percakapan (dan log model). Lebih aman `in_place=true` lalu tinjau sendiri, atau jangan dipakai lewat agent.
7. **Audit.** Pantau `~/.build-q/.mcp-audit.log`:

   ```bash
   tail -f ~/.build-q/.mcp-audit.log
   ```

---

## 13. Mode remote (SSE)

Untuk klien yang tidak bisa spawn proses lokal (mis. server bersama di jump host):

```bash
# Hanya localhost (aman)
bq --mcp --mcp-transport sse --mcp-port 3001

# Expose ke jaringan — HANYA di jaringan privat / di balik reverse proxy ber-auth
bq --mcp --mcp-transport sse --mcp-port 3001 --mcp-host 0.0.0.0
```

> ⚠️ Mode SSE **tidak memiliki autentikasi bawaan**. Siapa pun yang bisa menjangkau port tersebut dapat memanggil seluruh 18 tools dengan kredensial pemilik server. Jangan expose ke internet publik.

Akses lewat SSH tunnel (rekomendasi):

```bash
ssh -N -L 3001:127.0.0.1:3001 user@jump-host
# lalu arahkan klien ke http://127.0.0.1:3001/sse
```

Registrasi di Claude Code: `claude mcp add --transport sse bq-remote http://127.0.0.1:3001/sse`.

---

## 14. Troubleshooting

| Gejala | Penyebab umum | Solusi |
|---|---|---|
| `bq: command not found` di agent, padahal di terminal jalan | Agent tidak membaca `PATH` shell | Pakai path absolut dari `which bq` |
| `ModuleNotFoundError: No module named 'mcp'` | MCP SDK belum terpasang | `pipx inject build-q mcp` |
| `No module named 'mcp.server.mcpserver'` | MCP SDK versi 1.x | `pipx inject build-q 'mcp>=2' --force` |
| Server "failed" / timeout di `/mcp` | Ada output non-JSON ke stdout saat start | Jalankan `bq --mcp` manual, cek error; pastikan tidak ada `echo` di wrapper |
| Tools tidak muncul di agy | Cache lama / belum reload | `agy mcp disable bq && agy mcp enable bq`, restart sesi |
| `pi mcp list` exit 1 | Server gagal start | Baca pesan error di output; cek path & `--cwd` |
| `dockerfile_scan` "path not found" | `cwd` server berbeda dengan direktori kerja | Pakai path absolut |
| `401 Bad credentials` | `GITHUB_TOKEN` salah/kedaluwarsa | Perbarui token di `~/.build-q/.env`, cek dengan `bq --doctor` |
| `push=true` gagal `authentication required` | Belum `docker login` | `docker login` |
| `gitops_match_check` salah cluster | kubectl context keliru | `kubectl config use-context <ctx>` |
| `sops_decrypt` gagal | Key age tidak ada | Pastikan `~/.config/sops/age/keys.txt` berisi key yang terdaftar |

Perintah diagnostik cepat:

```bash
bq --version && bq --doctor
pipx runpip build-q show mcp
claude mcp list        # Claude Code
agy mcp list           # Antigravity
pi mcp list            # pi agent
tail -n 20 ~/.build-q/.mcp-audit.log
```

---

## 15. Checklist akhir

- [ ] `bq --version` ≥ 0.1.46 dan `bq --help` menampilkan `--mcp`
- [ ] `pipx runpip build-q show mcp` → versi 2.x
- [ ] `~/.build-q/.env` terisi (GitHub token, org, GitOps) dan `chmod 600`
- [ ] `bq --doctor` lulus untuk item yang Anda butuhkan
- [ ] `bq --mcp` start tanpa error (cek `~/.build-q/.mcp-audit.log`)
- [ ] Terdaftar di agent pilihan:
  - [ ] Claude Code → `claude mcp list` menunjukkan `bq ✓ Connected`
  - [ ] Antigravity → `agy mcp list` menunjukkan `bq enabled`
  - [ ] pi → `pi mcp list` menunjukkan `bq: connected, 18 tools`
- [ ] Guardrail aktif: permission `allow/ask` (Claude), aturan `AGENTS.md` (agy/pi)
- [ ] Uji coba pertama: minta agent menjalankan `build_doctor` dan `config_show`

Selamat — `bq` siap dipakai sebagai "tangan" DevOps untuk agent Anda. 🚀
