# PRD: build-q (bq) — Simplified Docker Buildx CLI

- **Version**: 0.1.10
- **Status**: Released (PyPI: `build-q`)
- **Owner**: ngen contributors
- **Entry points**: `build-q`, `bq`

---

## 1. Overview
`build-q` (dibaca *bq*, singkatan **Build-Quick**) adalah CLI Python zero-dependency yang menyederhanakan operasi `docker buildx build` di mesin lokal. Tool ini menjembatani perintah Docker manual yang panjang dengan pipeline CI/CD produksi: developer cukup menjalankan `bq` di dalam repo, dan seluruh flag (secret, build-arg, resource limit, tag image, registry) di-assemble otomatis dari Git + `cicd/cicd.json` + config global.

## 2. Problem Statement
Developer sering perlu me-reproduksi build image Docker yang identik dengan CI/CD pipeline (secret `.netrc`, build-arg `BRANCH/PORT/PROJECT`, platform, memory/CPU limit, tag berbasis commit) di laptop untuk debugging atau hotfix. Menyusun `docker buildx build` panjang secara manual rawan salah ketik, tidak konsisten antar developer, dan tidak sinkron dengan pipeline resmi.

## 3. Goals
- **Simplicity**: satu perintah pendek (`bq`) menghasilkan build yang setara pipeline.
- **Convention over configuration**: repo, ref (branch/tag), image name, dan commit hash di-detect otomatis dari Git dan `cicd/cicd.json`.
- **Portability**: hanya butuh Python 3.7+ standard library (tanpa dependency eksternal).
- **CI/CD alignment**: memakai sumber kebenaran yang sama dengan pipeline, yaitu `cicd/cicd.json`.
- **Idempotency**: skip build bila image dengan tag yang sama sudah ada di registry.

## 4. Non-goals
- Bukan pengganti pipeline CI/CD (Jenkins X / GitHub Actions). Fokus di build lokal / ad-hoc.
- Tidak melakukan deploy ke Kubernetes/ArgoCD (di luar scope).
- Tidak mengelola login Docker/registry (asumsi user sudah `docker login`).

## 5. Personas
- **Backend engineer** yang ingin uji build image sebelum push ke pipeline.
- **DevOps** yang perlu rebuild patch tag `v*` untuk hotfix produksi tanpa memicu pipeline.
- **On-call engineer** yang perlu build image dari repo remote tanpa clone lokal (mode `--remote`).

## 6. Key Features

### 6.1 Git Auto-detection
Bila `<repo>` / `<ref>` tidak diberikan, CLI membaca dari Git lokal:
- `repo` ← nama dari `git remote get-url origin` (fallback: nama direktori).
- `ref` ← `git rev-parse --abbrev-ref HEAD` (fallback: tag `git describe --tags --exact-match`, lalu short SHA jika detached HEAD).

### 6.2 CI/CD Config Integration (`cicd/cicd.json`)
Membaca field:
- `IMAGE` → nama image.
- `PORT`, `PORT2`, `PROJECT` → dipetakan otomatis ke `--build-arg`.

### 6.3 Smart Branch Build Arg
- Tag diawali `v*` → `--build-arg BRANCH=production`.
- Lainnya → `--build-arg BRANCH=develop`.
- Bisa di-override via `--build-arg BRANCH=xxx`.

### 6.4 Default Secret `netrc`
Selalu menambahkan `--secret id=netrc,src=$HOME/.netrc` (untuk dependency privat), kecuali user sudah menyertakan secret dengan id yang sama.

### 6.5 Registry Idempotency Check
Sebelum build, cek registry via `docker buildx imagetools inspect`. Bila image sudah ada → skip. Bypass dengan `--no-image-check` atau `--rebuild`.

### 6.6 Mode Sumber Kode
- **Local (default)**: build dari direktori kerja saat ini.
- **`--clone <owner/repo>`**: clone repo via `gh CLI` ke direktori kerja, lalu build. `--clean` menghapus direktori clone setelah selesai. Pre-clone image check memakai commit SHA dari `gh api` supaya tidak clone bila image sudah ada.
- **`--remote`**: build langsung dari Git context (`git_url#ref`) — tidak clone lokal. `cicd.json` diambil via `gh api contents`. Commit hash diambil via `gh api commits/{ref}`.

### 6.7 Resource Limits
Default aman untuk laptop: `--memory 4g`, `--cpu-period 100000`, `--cpu-quota 200000` (dapat di-override via `~/.build-q/.env`).

### 6.8 Auto Tag
Bila `--tag` tidak diberikan: `{REGISTRY_URL}/{IMAGE}:{short_commit}` (7 karakter).

### 6.9 Dry Run
`--dry-run` mencetak command yang akan dijalankan tanpa eksekusi.

### 6.10 Konfigurasi Terpusat
File `~/.build-q/.env`:
| Variable | Default |
|----------|---------|
| `BUILDER_NAME` | `mybuilder` |
| `REGISTRY_URL` | `registry.example.com` |
| `DEFAULT_MEMORY` | `4g` |
| `DEFAULT_CPU_PERIOD` | `100000` |
| `DEFAULT_CPU_QUOTA` | `200000` |
| `GIT_SSH_PREFIX` | `git@github.com:` |
| `GITHUB_ORG` | (kosong) |
| `WEBHOOK_TRIGGER_URL` | `https://cicd-hw.qoin.id/trigger` |
| `JX_KUBE_CONTEXT` | (kosong = current) |
| `JX_KUBE_NAMESPACE` | `jenkins-x` |
| `JX_TOKEN_SECRET` | `webhook-trigger-token` |

Perintah bantuan: `bq --init` (buat file + init Buildx builder), `bq --config` (tampilkan aktif).

### 6.11 Auto-init Docker Buildx Builder
`bq --init` (dan `run_build` sebagai safety net) memanggil `ensure_builder(name)` yang:
- `docker buildx inspect <name>` — cek eksistensi.
- Jika ada tetapi stdout memuat `Error:` (mis. endpoint Colima yang hilang) → `docker buildx rm -f` lalu re-create.
- Jika belum ada → `docker buildx create --name <name> --use [--bootstrap]`.

### 6.12 Scaffolding CI/CD (`--init-jx`)
Generate 4 file dari `cicd/cicd.json`:
- `Makefile` — target `develop/staging/production/build/release/run`, BuildKit enabled.
- `compose.yaml` — service dengan `secrets: netrc` mount dari `$HOME/.netrc`.
- `Dockerfile` — multi-stage Go dengan `--mount=type=secret,id=netrc` (pola aman).
- `.github/workflows/trigger-ci.yml` — trigger webhook Jenkins X.

Placeholder `{{IMAGE}}/{{PROJECT}}/{{PORT}}/{{CLUSTER}}/{{DEPLOYMENT}}/{{NODETYPE}}/{{ORG_REGISTRY}}` disubstitusi dari `cicd.json` + config. Otomatis panggil `--init-secrets` bila `git remote origin` mengarah ke GitHub.

### 6.13 GitHub Actions Webhook Secrets (`--init-secrets`)
Set dua secret di target repo:
- `WEBHOOK_TRIGGER_URL` — dari config `WEBHOOK_TRIGGER_URL`.
- `WEBHOOK_TRIGGER_TOKEN` — fetch otomatis via `kubectl get secret <JX_TOKEN_SECRET> -n <JX_KUBE_NAMESPACE>` lalu `base64 -d`.

Auto-detect target repo dari `git remote get-url origin` (parse `owner/repo`). Override manual via positional `bq --init-secrets <owner>/<repo>` atau `--token <VALUE>` untuk skip kubectl.

### 6.14 Migrasi Dockerfile Legacy (`--fix-dockerfile`)
Transformasi Dockerfile yang memakai pola `ARG GITHUB_USER/TOKEN` + `echo "machine github.com..." > ~/.netrc` menjadi `RUN --mount=type=secret,id=netrc,target=/root/.netrc`. Termasuk:
- Normalisasi `FROM ... as ...` → `FROM ... AS ...`.
- Hapus `ARG GITHUB_USER` / `ARG GITHUB_TOKEN`.
- Rewrite `RUN sh -c '...'` dan `RUN echo ...` yang mem-build netrc dari ARG.
- Backup ke `<path>.bak`.

### 6.15 GitHub Auth Injection (`--gh-auth`)
Untuk Dockerfile legacy yang belum dimigrasi: inject `--build-arg GITHUB_USER=$(gh api user --jq .login)` dan `--build-arg GITHUB_TOKEN=$(gh auth token)`. Token di-mask di log output (`GITHUB_TOKEN=***`); nilai asli diteruskan ke `docker buildx`.

### 6.16 GitHub Org Shorthand
Bila `GITHUB_ORG` di-set, positional `repo` yang tidak mengandung `/` atau scheme akan di-expand: `bq foo staging --remote` → `bq Qoin-Digital-Indonesia/foo staging --remote`.

## 7. CLI Contract
```
bq [<repo> [<ref>]] [OPTIONS]
```
Flag penting: `--local`, `--remote`, `--clone <owner/repo>`, `--clean`, `--cicd <path>`, `--context <dir>`, `-f/--dockerfile`, `-t/--tag`, `--push/--no-push`, `--image-check/--no-image-check/--rebuild`, `--platform`, `--build-arg`, `--secret`, `--gh-auth`, `--dry-run`, `--init`, `--force`, `--config`, `--init-jx`, `--init-secrets`, `--fix-dockerfile [PATH]`, `--token`, `--version`.

Default: `--push=True`, `--image-check=True`, `--platform=linux/amd64`, secret `netrc` otomatis.

## 8. Arsitektur
Empat modul di `build_q/`:
- **`cli.py`** — argparse, orchestration (mode local / clone / remote), subcommands (`--init`, `--config`, `--init-jx`, `--init-secrets`, `--fix-dockerfile`), shorthand expansion (`_expand_repo`), `--gh-auth` injection.
- **`builder.py`** — `get_git_info`, `build_command`, `check_image_exists`, `run_build`, `ensure_builder` (dengan stale-endpoint detection), `fix_dockerfile`, `init_jx`, `init_secrets`, `detect_github_repo`.
- **`config.py`** — loader `.env` minimalis, `init_config`, `load_local_cicd`, sections `builder`/`registry`/`git`/`webhook`.
- **`templates.py`** — string templates untuk `--init-jx` (Makefile/compose/Dockerfile/trigger-ci.yml) dengan placeholder `{{NAME}}` (tidak konflik dengan shell/Docker `${VAR}` atau GitHub Actions `${{ }}`).

Distribusi via `pyproject.toml` (setuptools) → dua entry point script: `build-q` & `bq`.

## 9. Technical Requirements
- Python 3.7+ (standard library only).
- Docker Engine + Buildx plugin.
- Git (opsional; wajib untuk auto-detect & `--local`).
- GitHub CLI `gh` (wajib untuk `--clone` / `--remote`).

## 10. Release Process
`Makefile` menyediakan `make build` dan `make release [V=x.y.z]`:
1. `scripts/bump_version.py` bump patch version di `pyproject.toml` + `build_q/__init__.py`.
2. Build sdist + wheel.
3. Upload ke PyPI via `twine`.

## 11. Success Metrics
- **Adoption**: jumlah repo di organisasi yang punya `cicd/cicd.json` kompatibel dan memakai `bq` untuk build lokal.
- **Konsistensi**: image hasil `bq` bit-identical dengan hasil pipeline untuk commit yang sama.
- **Time-to-image**: waktu build ulang berkurang (dengan `--image-check` skip build redundan).

## 12. Future Roadmap
- Multi-registry profile (per-project).
- Integrasi konteks Kubernetes (langsung deploy hasil build ke cluster lokal / kind).
- Caching layer buildx yang lebih pintar (mount cache antar build).
- Support platform `linux/arm64` cross-build default untuk M-series Mac.
- Plugin hook pre/post build untuk custom step.
