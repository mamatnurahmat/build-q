# build-q (bq) 🚀

**build-q** (dibaca *bq*) adalah CLI Python zero-dependency untuk operasi `docker buildx` lokal & remote yang selaras dengan pipeline CI/CD (Jenkins X). Satu perintah pendek `bq` menggantikan `docker buildx build` yang panjang, dengan auto-detect Git, `cicd/cicd.json`, secret `netrc`, resource limit, dan idempotency check terhadap registry.

Selain build, `bq` menyediakan scaffolding CI/CD (`--init-jx`, `--init-legacy`), bootstrap GitHub Actions trigger (`--gh-action-init`), setup secrets (`--init-secrets`), migrasi Dockerfile legacy ke pola `--mount=type=secret` (`--fix-dockerfile`), auto-init Docker Buildx builder (`--init`), dan mode compose (`--compose`) untuk build via `make build && make release`.

---

## 📚 Panduan Cepat untuk Pemula

Bagian ini untuk yang baru pertama kali pakai `bq`. Kalau sudah familiar, lanjut ke [Instalasi](#-instalasi).

### Apa saja yang perlu di-install?

Sebelum pakai `bq`, siapkan tools berikut di laptop:

| Tools | Wajib? | Kegunaan | Cara install (macOS via Homebrew) |
| ------- | -------- | ---------- | ----------------------------------- |
| **Python 3.7+** | ✅ Wajib | Runtime `bq` | `brew install python` (biasanya sudah ada) |
| **pipx** | ✅ Rekomendasi | Install CLI Python secara terisolasi | `brew install pipx && pipx ensurepath` |
| **Docker Desktop** / **Colima** | ✅ Wajib | Docker engine + Buildx plugin | `brew install --cask docker` atau `brew install colima docker docker-buildx` |
| **Git** | ✅ Wajib | Auto-detect repo/branch | `brew install git` (biasanya sudah ada) |
| **GitHub CLI (`gh`)** | ⚠️ Wajib untuk `--clone`, `--remote`, `--gh-auth`, `--init-secrets`, `--gh-action-init` | Autentikasi GitHub, fetch token, clone repo | `brew install gh && gh auth login` |
| **kubectl** | ⚙️ Opsional | Fetch webhook token otomatis dari k8s (`--init-secrets`) | `brew install kubectl` |

Cek versi setelah install:

```bash
python3 --version        # >= 3.7
pipx --version
docker --version
docker buildx version
git --version
gh --version && gh auth status
kubectl version --client  # opsional
```

### Install `bq` (pemula, langkah demi langkah)

```bash
# 1. Install pipx (kalau belum ada)
brew install pipx
pipx ensurepath              # tambahkan pipx ke PATH — restart terminal setelah ini

# 2. Install build-q
pipx install build-q

# 3. Verifikasi
bq --version                 # harus menampilkan versi terpasang
build-q --version            # alias — sama saja

# 4. Bootstrap awal (buat config + Docker buildx builder)
bq --init
```

Setelah `bq --init` selesai, file config akan ada di `~/.build-q/.env` dan Docker Buildx builder siap dipakai.

### Upgrade ke versi terbaru

```bash
# Rekomendasi (pipx)
pipx upgrade build-q

# Kalau pakai pip biasa
pip install --upgrade build-q

# Pin versi tertentu (opsional)
pipx install --force build-q==0.1.11

# Cek versi yang sedang terpasang
bq --version
```

Tips: jika `pipx upgrade` bilang "already at latest" tapi kamu yakin ada rilis baru, refresh cache PyPI:

```bash
pipx install --force build-q     # reinstall dari PyPI terbaru
```

### Uninstall

```bash
pipx uninstall build-q            # kalau install via pipx
pip uninstall build-q             # kalau install via pip
rm -rf ~/.build-q                 # hapus config (opsional)
```

### Alur 5 menit pertama (pemula)

```bash
# 1. Masuk ke folder service Go/.NET yang sudah ada Dockerfile
cd my-service

# 2. Preview command yang akan dijalankan (aman — tidak eksekusi)
bq --dry-run

# 3. Build tanpa push ke registry
bq --no-push

# 4. Build + push ke registry (mode default)
bq
```

Kalau muncul error `builder not found` → jalankan `bq --init` sekali dulu.
Kalau muncul error `authentication required` saat push → login ke Docker Hub / registry: `docker login`.

---

## 🛠 Fitur

### Build & Push

- **Zero-dependency**: hanya Python 3.7+ standard library.
- **Git auto-detection**: nama repo & ref (branch/tag/short-SHA) dari Git.
- **Mode sumber kode**: build lokal, `--clone` (via `gh` CLI), atau `--remote` (Buildx Git context).
- **Registry idempotency**: cek image di registry sebelum build → skip bila sudah ada (bypass `--rebuild`).
- **Smart env mapping (ref → env)** — **INLINE dengan pipeline Tekton** di `~/jenkins-x/pipeline` (lighthouse `triggers.yaml` + `webhook-server.py`). `bq` (`bq --clone` / `bq --compose`) menyimpulkan environment otomatis dari nama ref, sama persis dengan case shell di pipeline. Nilai dipakai sebagai `--build-arg BRANCH=` (buildx) dan `ENV=` (compose):

  | Ref (branch/tag) | ENV | IMAGE_TAG di pipeline |
  |------------------|-----|----------------------|
  | `v1.2.3`, `refs/tags/v*` | `production` | `v1.2.3` (basename ref) |
  | `develop` | `develop` | short SHA |
  | `staging` | `staging` | short SHA |
  | `sandbox` | `sandbox` | short SHA |
  | `main`, `master`, unknown, kosong | **`staging`** (fallback Tekton `*)`) | short SHA |

  Perhatikan: push ke `main`/`master` **TIDAK** memicu production di JX — tag `v*` yang memicu. Override manual: `--build-arg BRANCH=custom` (buildx).
- **Default netrc secret**: `--secret id=netrc,src=$HOME/.netrc` otomatis.
- **Resource limit default**: memory & CPU aman untuk laptop.
- **CI/CD alignment**: baca `PORT/PORT2/PROJECT/IMAGE` dari `cicd/cicd.json`.
- **Dry run**: pratinjau command tanpa eksekusi.
- **GitHub org shorthand**: set `GITHUB_ORG=...` di config → cukup `bq <repo>` bukan `bq <owner>/<repo>`.
- **`--remote` fallback HTTPS**: bila `SSH_AUTH_SOCK` tidak tersetel, `bq` otomatis pindah ke HTTPS + `GIT_AUTH_TOKEN` (via `gh auth token`) untuk Buildx Git context — tidak perlu ssh-agent.
- **`--compose` mode**: jalankan `make build ENV=...` + `make release ENV=...` sebagai alternatif `docker buildx` (cocok untuk repo yang alur build-nya via Makefile + docker compose).

### Setup & Automation

- **`--init`**: buat file config `~/.build-q/.env` **dan** auto-create Docker Buildx builder (bootstrap).
- **Template terpusat di gist**: sejak `v0.1.14`, `--init-jx`, `--init-legacy`, dan `--gh-action-init` fetch template dari [gist mamatnurahmat](https://gist.github.com/mamatnurahmat/35cc4c36e7c7c2d236a1b5149cdbcfd9) (6 file). Cache lokal di `~/.build-q/templates/` (TTL 1 jam). Fallback ke bundled snapshot bila offline. **Update / perbaikan template harus dilakukan di gist** (via `gh gist edit`) — semua user `bq` otomatis dapat versi terbaru pada scaffold berikutnya.
- **`--init-jx`**: scaffold `Makefile` + `compose.yaml` + `Dockerfile` (modern secret mount) + `.github/workflows/trigger-ci.yml` dari `cicd/cicd.json`. Auto-panggil `--init-secrets` bila git remote GitHub terdeteksi.
- **`--init-legacy`**: scaffold `Makefile` + `compose.yaml` untuk pola **legacy** (Dockerfile pakai `ARG GITHUB_USER/GITHUB_TOKEN` — bukan BuildKit secret). Makefile auto-ambil `gh auth token` untuk local dev. **Dockerfile tidak di-overwrite** — cocok untuk repo lama yang belum bisa migrasi ke secret mount.
- **`--gh-action-init`**: bootstrap standar `.github/workflows/trigger-ci.yml` sebagai **satu-satunya** workflow — hapus semua workflow YAML lain di `.github/workflows/`, tulis ulang trigger-ci.yml, lalu set webhook secrets (`WEBHOOK_TRIGGER_URL`, `WEBHOOK_TRIGGER_TOKEN`).
- **`--init-secrets`**: set GitHub Actions secrets (`WEBHOOK_TRIGGER_URL`, `WEBHOOK_TRIGGER_TOKEN`) — auto-fetch token dari k8s secret `webhook-trigger-token` di namespace `jenkins-x`, auto-detect target repo dari `git remote`.
- **`--fix-dockerfile`**: migrasi Dockerfile legacy menjadi modern. Deteksi & auto-fix:
  - `FROM ... as ...` → `AS` (uppercase)
  - Hapus `ARG GITHUB_USER` / `ARG GITHUB_TOKEN`
  - `RUN echo "machine github.com ..." > ~/.netrc && chmod ... && <cmd>` → `RUN --mount=type=secret,id=netrc,target=/root/.netrc \ ...`
  - **Baru**: standalone `RUN echo ... > ~/.netrc` (tanpa `&& chmod && ...`) → dihapus, lalu mount secret otomatis ditambahkan ke RUN line yang berisi `go mod tidy/download` atau `go get`.
  - **Baru**: `MAINTAINER foo` (deprecated) → `LABEL maintainer="foo"`
  - **Baru**: `ENV KEY value` (legacy) → `ENV KEY=value`
  - Backup asli disimpan ke `Dockerfile.bak`.
- **`--gh-auth`**: injeksi `--build-arg GITHUB_USER` + `GITHUB_TOKEN` dari `gh` CLI (workaround untuk Dockerfile legacy yang belum dimigrasi).
- **Auto-recover builder stale**: `ensure_builder` deteksi endpoint rusak (misal socket Colima lama) dan recreate otomatis.

---

## 📦 Instalasi

```bash
pipx install build-q       # rekomendasi (CLI global, virtualenv terisolasi)
# atau
pip install build-q
```

Dua entry point tersedia: `build-q` dan shorthand `bq`.

**Upgrade ke versi terbaru:**

```bash
pipx upgrade build-q
# atau
pip install --upgrade build-q
```

---

## ⚙️ Konfigurasi Awal (sekali saja)

```bash
bq --init         # buat ~/.build-q/.env + create Docker buildx builder
bq --init --force # recreate config; builder tetap aman (cek dulu, buat bila tidak ada)
bq --config       # tampilkan konfigurasi aktif
```

### `~/.build-q/.env`

| Variable | Default | Keterangan |
| ---------- | --------- | ----------- |
| `BUILDER_NAME` | `mybuilder` | Nama Docker Buildx builder |
| `REGISTRY_URL` | `registry.example.com` | Docker registry (Qoin: `loyaltolpi`) |
| `DEFAULT_MEMORY` | `4g` | Memory limit build |
| `DEFAULT_CPU_PERIOD` | `100000` | CPU period |
| `DEFAULT_CPU_QUOTA` | `200000` | CPU quota |
| `GIT_SSH_PREFIX` | `git@github.com:` | Prefix SSH untuk `--remote` |
| `GITHUB_ORG` | (kosong) | Default org untuk shorthand `bq <repo>` |
| `WEBHOOK_TRIGGER_URL` | `https://cicd-hw.qoin.id/trigger` | Endpoint webhook Jenkins X |
| `JX_KUBE_CONTEXT` | (kosong = current) | kubectl context untuk fetch token |
| `JX_KUBE_NAMESPACE` | `jenkins-x` | Namespace secret |
| `JX_TOKEN_SECRET` | `webhook-trigger-token` | Nama k8s secret berisi token |
| `GH_CLI` | `true` | `true` = pakai `gh` CLI (default, tidak ada perubahan). `false` = jalur native (REST + `git`) |
| `GITHUB_USER` | (kosong) | Username GitHub. Dipakai bila `GH_CLI=false`; bila kosong, di-fetch dari `/user` |
| `GITHUB_TOKEN` | (kosong) | Personal Access Token — WAJIB bila `GH_CLI=false`. Scope: `repo`, `read:user`, `actions:write` (untuk `--init-secrets`) |

#### Toggle native (tanpa `gh` CLI)

Default `GH_CLI=true` — `bq` tetap memakai `gh` CLI persis seperti sebelumnya, tidak ada breaking change. Untuk mengaktifkan jalur native (stdlib `urllib` + `git`):

```env
GH_CLI=false
GITHUB_USER=your-login
GITHUB_TOKEN=ghp_xxx
```

Fitur yang dialihkan: fetch `cicd.json`, resolve commit SHA, ambil auth token, `--gh-auth`, `--clone`, `--init-secrets`. Untuk `--init-secrets` di mode native, install extra: `pip install pynacl` (dibutuhkan enkripsi libsodium sebelum PUT ke API).

> Bila `~/.build-q/.env` sudah ada dari versi sebelumnya, tambahkan tiga baris di atas secara manual — atau jalankan `bq --init --force` untuk regenerate (perhatikan config lain akan direset ke default).

Contoh minimal untuk Qoin:

```env
BUILDER_NAME=cloud-loyaltolpi
REGISTRY_URL=loyaltolpi
GITHUB_ORG=Qoin-Digital-Indonesia
WEBHOOK_TRIGGER_URL=https://cicd-hw.qoin.id/trigger
```

---

## 🚀 Panduan Penggunaan

### 1. Build dari direktori lokal

Auto-detect nama repo & branch dari `git`:

```bash
cd my-service
bq                                    # build + push (default)
bq --no-push                          # build tanpa push
bq --dry-run                          # preview command saja
bq my-service staging                 # eksplisit repo/ref
```

### 2. Build dari repo remote tanpa clone

```bash
# Dengan GITHUB_ORG di config → shorthand
bq plus-be-paymentlink-manager staging --remote

# Tanpa GITHUB_ORG → sertakan owner
bq Qoin-Digital-Indonesia/plus-be-paymentlink-manager staging --remote

# URL SSH/HTTPS penuh juga OK
bq git@github.com:owner/repo.git v1.0.0 --remote
```

> **Catatan `--remote`:** bila `SSH_AUTH_SOCK` tidak tersetel (tidak jalanin `ssh-agent`), `bq` otomatis fallback ke HTTPS + `GIT_AUTH_TOKEN` secret (via `gh auth token`) supaya Buildx Git context tetap bisa jalan tanpa perlu bootstrap ssh-agent.

### 3. Build dengan clone via `gh` CLI

```bash
bq --clone plus-be-paymentlink-manager staging
bq --clone plus-be-paymentlink-manager staging --clean   # hapus folder setelah build
```

### 4. Preview command (dry-run)

```bash
bq --dry-run
bq plus-be-paymentlink-manager staging --remote --no-image-check --dry-run
```

### 5. Paksa rebuild (bypass image check)

```bash
bq plus-be-paymentlink-manager staging --remote --rebuild
```

### 6. Bootstrap CI/CD service baru (Jenkins X — modern, secret mount)

```bash
mkdir my-new-service && cd my-new-service
mkdir cicd && cat > cicd/cicd.json <<EOF
{"IMAGE":"my-new-service","PROJECT":"qoinplus","PORT":"8080"}
EOF
bq --init-jx                 # scaffold Makefile/compose/Dockerfile/workflow
git init && git remote add origin git@github.com:Qoin-Digital-Indonesia/my-new-service.git
bq --init-secrets            # set webhook secrets di GitHub (auto-detect repo)
git add . && git commit -m "chore: bootstrap CI/CD"
git push -u origin main      # trigger Jenkins X pipeline via GitHub Actions
```

### 7. Bootstrap CI/CD service **legacy** (Dockerfile pakai ARG GITHUB_USER/TOKEN)

Kalau Dockerfile di repo lama belum bisa dimigrasi ke `--mount=type=secret`, pakai `--init-legacy`. Makefile-nya akan auto-ambil `gh auth token` untuk local dev.

```bash
cd my-old-service              # Dockerfile-nya masih pakai ARG GITHUB_USER/TOKEN
bq --init-legacy               # scaffold Makefile + compose.yaml (Dockerfile TIDAK ditimpa)
gh auth login                  # sekali saja
make build ENV=staging         # token auto-diambil dari `gh auth token`
make release ENV=staging
```

### 8. Bootstrap GitHub Actions trigger sebagai satu-satunya workflow

Berguna kalau repo punya banyak workflow lama yang tidak dipakai lagi:

```bash
cd existing-repo
bq --gh-action-init            # hapus workflow lain + tulis trigger-ci.yml + set webhook secrets
bq --gh-action-init --token xxxx   # skip kubectl fetch, pakai token eksplisit
```

### 9. Migrasi Dockerfile legacy (ARG-based netrc → secret mount)

```bash
cd my-old-service
bq --fix-dockerfile                # migrasi ./Dockerfile (backup ke .bak)
bq --fix-dockerfile path/to/Dockerfile
bq --no-push --rebuild             # test build hasil migrasi
```

Yang di-fix otomatis:

- `FROM x as y` → `FROM x AS y`
- `ARG GITHUB_USER/TOKEN` dihapus
- `RUN echo "machine github.com ..." > ~/.netrc && chmod ... && <cmd>` → `RUN --mount=type=secret,id=netrc,...`
- Standalone `RUN echo ... > ~/.netrc` → dihapus, secret mount otomatis nempel di `RUN go mod tidy/download/get`
- `MAINTAINER foo` → `LABEL maintainer="foo"`
- `ENV KEY value` → `ENV KEY=value`

### 10. Workaround Dockerfile legacy tanpa migrasi (--gh-auth)

```bash
bq plus-be-service staging --remote --rebuild --gh-auth
# → inject GITHUB_USER dari `gh api user` + GITHUB_TOKEN dari `gh auth token`
```

### 11. Setup secrets Jenkins X untuk repo existing

```bash
cd existing-repo
bq --init-secrets                                        # auto-detect dari git
bq --init-secrets Qoin-Digital-Indonesia/foo-service     # eksplisit
bq --init-secrets foo-service --token xxxx               # skip kubectl
```

### 12. Mode `--compose` (build via `make build && make release`)

Untuk repo yang alur build-nya sudah pakai Makefile + docker compose (mis. hasil `--init-jx` / `--init-legacy`). `ENV=` disimpulkan dari ref — lihat tabel **Smart env mapping** di atas:

```bash
# auto-detect: repo & ref dari git di direktori aktif
bq --compose                                # branch `develop` → ENV=develop
bq --compose my-service staging             # ENV=staging
bq --compose my-service v1.2.3              # ENV=production
bq --compose my-service main                # ENV=production
bq --compose --dry-run                      # preview command

# gabung dengan --clone (clone dulu, cd, lalu make build+release)
bq --clone Qoin-Digital-Indonesia/foo staging --compose   # ENV=staging
bq --clone Qoin-Digital-Indonesia/foo v1.2.3 --compose    # ENV=production
```

**Registry idempotency di `--compose`:** tag yang di-probe mengikuti aturan Makefile — `git describe --tags --exact-match || git rev-parse --short HEAD`. Jadi kalau HEAD di tag `v1.2.3`, `bq` cek `registry/image:v1.2.3` (bukan short commit) — sama dengan tag yang di-push `make release`, sehingga early skip berfungsi benar.

### 13. Contoh full (customize secret, platform, build-arg)

```bash
bq plus-be-service staging \
    --secret id=custom,src=/path/to/secret \
    --platform linux/amd64 \
    --no-push \
    --build-arg BRANCH=custom-branch \
    --build-arg FEATURE_FLAG=on
```

---

## 🔗 Perintah Lengkap

```
bq [<repo> [<ref>]] [OPTIONS]

Subcommands (mutually exclusive):
  --init                    Buat ~/.build-q/.env + init Buildx builder
  --init --force            Recreate config
  --config                  Tampilkan konfigurasi aktif
  --init-jx                 Scaffold Makefile/compose/Dockerfile/workflow (modern secret mount)
  --init-jx --force         Overwrite file existing
  --init-legacy             Scaffold Makefile + compose.yaml pola legacy (ARG GITHUB_USER/TOKEN)
  --init-legacy --force     Overwrite file existing
  --gh-action-init          Bootstrap standar trigger-ci.yml (hapus workflow lain + set secrets)
  --init-secrets [<repo>]   Set GitHub Actions webhook secrets
  --fix-dockerfile [PATH]   Migrasi Dockerfile legacy ke secret mount (+ MAINTAINER/ENV/standalone netrc)

Build modes:
  (default)                 Build dari direktori lokal via docker buildx
  --clone <owner/repo>      Clone via `gh` CLI, lalu build
  --clone ... --clean       Hapus folder clone setelah build
  --remote                  Build via Buildx Git context (tanpa clone) — auto-fallback HTTPS bila SSH_AUTH_SOCK kosong
  --compose                 Jalankan `make build ENV=... && make release ENV=...` (bukan buildx)

Build options:
  --cicd PATH               Path cicd.json (default: cicd/cicd.json)
  --context DIR             Build context (default: .)
  -f, --dockerfile PATH     Path Dockerfile (default: Dockerfile)
  -t, --tag IMAGE:TAG       Override image tag
  --push / --no-push        Push image (default: push)
  --image-check / --rebuild Cek image di registry, skip bila ada (default: on)
  --no-image-check          Sama dengan --rebuild
  --platform PLATFORM       Target platform (default: linux/amd64)
  --build-arg KEY=VALUE     Build arg (dapat diulang)
  --secret id=ID,src=PATH   Secret build (dapat diulang; netrc otomatis)
  --gh-auth                 Inject GITHUB_USER/TOKEN dari `gh` CLI
  --token VALUE             Webhook token (dengan --init-secrets / --gh-action-init)
  --dry-run                 Preview command tanpa eksekusi
  --version                 Tampilkan versi
```

---

## 📋 Requirements

- **Python 3.7+**
- **Docker** + **Buildx plugin**
- **Git** (opsional; wajib untuk auto-detect & mode local)
- **GitHub CLI (`gh`)** — wajib untuk `--clone`, `--remote`, `--gh-auth`, `--init-secrets`, `--gh-action-init`
- **kubectl** — opsional; hanya untuk `--init-secrets` / `--gh-action-init` (fetch token otomatis dari cluster)

---

## 📄 Template Terpusat (Gist)

Sejak `v0.1.14`, template scaffolding hidup di **gist publik**, bukan hard-coded di package:

- **URL gist:** https://gist.github.com/mamatnurahmat/35cc4c36e7c7c2d236a1b5149cdbcfd9
- **Raw base:** `https://gist.githubusercontent.com/mamatnurahmat/<gist-id>/raw/<filename>`
- **6 file:** `Makefile.modern`, `compose.yaml.modern`, `Dockerfile.modern`, `Makefile.legacy`, `compose.yaml.legacy`, `trigger-ci.yml`

**Alur load:**
```
load_template(name)
   ├── 1. cache ~/.build-q/templates/<file>  (TTL 3600s, fresh?)  ─► pakai
   ├── 2. fetch dari gist raw URL                                ─► refresh cache + pakai
   ├── 3. cache lama (expired)                                   ─► pakai (dengan warning)
   └── 4. bundled snapshot di build_q/templates.py               ─► pakai (warning)
```

### Cara update template

**Update HARUS via gist**, jangan edit `build_q/templates.py`. `_BUNDLED_*` di file itu hanya snapshot offline-fallback.

```bash
# Clone gist untuk edit local
gh gist clone 35cc4c36e7c7c2d236a1b5149cdbcfd9 build-q-templates
cd build-q-templates

# Edit file yang ingin diubah (mis. Makefile.modern)
vim Makefile.modern

# Commit + push (gist adalah git repo — otomatis publish)
git commit -am "fix: adjust Makefile.modern IMAGE_TAG logic"
git push

# Verifikasi raw URL sudah kepakai
curl -s https://gist.githubusercontent.com/mamatnurahmat/35cc4c36e7c7c2d236a1b5149cdbcfd9/raw/Makefile.modern | head
```

### Force refresh cache di sisi user

Kalau user ingin ambil template terbaru sebelum TTL expired:
```bash
rm -rf ~/.build-q/templates      # hapus cache — next scaffold auto-fetch
# atau override TTL per-invocation:
BUILD_Q_GIST_TTL=0 bq --init-jx
```

### Override gist source (advanced)

Untuk fork/private mirror, override via env vars:
```bash
export BUILD_Q_GIST_USER=your-org
export BUILD_Q_GIST_ID=abcd1234...
export BUILD_Q_GIST_TTL=7200      # 2 jam
export BUILD_Q_GIST_TIMEOUT=15    # 15 detik
bq --init-jx
```

### Pipeline Tekton pakai gist yang SAMA (Opsi B strict)

Sejak `~/jenkins-x/pipeline` versi terbaru, Tekton pipeline **overlay** Makefile/compose/Dockerfile dari gist ini SEBELUM `make build`. Hasilnya BYTE-IDENTICAL dengan `bq --init-jx` — jadi tidak ada drift antara build lokal `bq --compose` dan build pipeline JX.

Alur di pipeline (step `sync-scaffold` di `.lighthouse/triggers.yaml`):
1. Early-skip: `docker manifest inspect` — kalau image sudah ada, skip semuanya.
2. Validate: `cicd/cicd.json` wajib ada (fail-fast kalau tidak).
3. Auto-detect mode: `ARG GITHUB_USER/TOKEN` di Dockerfile → legacy (Dockerfile tidak di-overlay). Else → modern (semua 3 file di-overlay).
4. Fetch dari gist raw URL, render placeholder pakai `sed s|{{KEY}}|VALUE|g` dengan 7 keys (IMAGE, PROJECT, PORT, CLUSTER, DEPLOYMENT, NODETYPE, ORG_REGISTRY) — sama persis dengan `render()` di `build_q/templates.py`.
5. `make build && make release` (fail-fast, tanpa `|| echo WARN`).

**BREAKING:** repo yang customize `Makefile` / `compose.yaml` / `Dockerfile` (modern mode) akan **di-overlay tiap build**. Custom target Makefile HILANG. Kalau perlu kustomisasi, edit di gist saja (semua repo dapat perubahan otomatis).

### Sinkronisasi bundled fallback (maintainer only)

Saat rilis `bq` baru, refresh `_BUNDLED_*` di `build_q/templates.py` agar offline user tetap dapat template terkini:
```bash
GID=35cc4c36e7c7c2d236a1b5149cdbcfd9
for f in Makefile.modern compose.yaml.modern Dockerfile.modern Makefile.legacy compose.yaml.legacy trigger-ci.yml; do
  curl -s "https://gist.githubusercontent.com/mamatnurahmat/$GID/raw/$f" > "/tmp/$f"
done
# lalu paste isi ke masing-masing _BUNDLED_* di build_q/templates.py
```

---

## 🧭 Troubleshooting

| Gejala | Kemungkinan penyebab | Solusi |
| -------- | --------------------- | -------- |
| `builder "mybuilder" not found` | Buildx belum di-bootstrap | `bq --init` |
| `authentication required` saat push | Belum login registry | `docker login` (atau `docker login <registry>`) |
| `invalid empty ssh agent socket` (mode `--remote`) | `SSH_AUTH_SOCK` kosong | Sudah auto-fallback ke HTTPS + `gh auth token`. Kalau tetap gagal → `gh auth login` |
| `gh: command not found` | GitHub CLI belum ter-install | `brew install gh && gh auth login` |
| `--init-secrets` gagal fetch token | Konteks kubectl salah / secret tidak ada | Set `JX_KUBE_CONTEXT` di `~/.build-q/.env`, atau `--token xxxx` |
| Build sukses tapi image tidak muncul di registry | Salah `REGISTRY_URL` di config | `bq --config` → verifikasi, lalu `bq --init --force` |
| `bq --version` tidak berubah setelah upgrade | pipx cache | `pipx install --force build-q` |

---

## 🚀 Development & Release

Ada **dua cara** rilis: (A) via GitHub Actions otomatis dari tag — **direkomendasikan**, dan (B) via `Makefile` lokal — cocok untuk hotfix/debug.

### A. Auto-release via GitHub Actions (rekomendasi)

Workflow `.github/workflows/pypi-release.yml` dipicu oleh push tag `v*` (atau manual via `workflow_dispatch`) dan otomatis build + upload ke PyPI.

**Flow rilis versi berikutnya:**

```bash
# 1. Bump versi (patch) — auto-update pyproject.toml + build_q/__init__.py
python3 scripts/bump_version.py                # bump patch (0.1.12 → 0.1.13)
# atau versi eksplisit:
python3 scripts/bump_version.py 0.2.0

# 2. Baca versi baru
VER=$(grep -m1 '^version = ' pyproject.toml | sed 's/.*"\(.*\)".*/\1/')
echo "Releasing v$VER"

# 3. Commit bump
git add pyproject.toml build_q/__init__.py
git commit -m "release: v$VER"

# 4. Tag + push (tag inilah yang memicu workflow)
git tag "v$VER"
git push origin main "v$VER"

# 5. Pantau workflow
gh run watch --repo mamatnurahmat/build-q --exit-status
# atau lihat di browser:
gh run list --workflow=pypi-release.yml --repo mamatnurahmat/build-q --limit 3
```

Setelah workflow selesai (± 15 detik), package tersedia di:

- <https://pypi.org/project/build-q/><versi>/
- Upgrade user: `pipx upgrade build-q`

**Apa yang dilakukan workflow (lihat `.github/workflows/pypi-release.yml`):**

1. **Checkout** repo pada commit tag.
2. **Setup Python 3.12**.
3. **Verifikasi tag** — pastikan `v<X>` cocok dengan version di `pyproject.toml` **dan** `build_q/__init__.py`. Bila mismatch, gagal (mencegah rilis "kosong").
4. **Install** `build` + `twine`.
5. **Build** sdist + wheel via `python -m build`.
6. **Validate** dengan `twine check dist/*`.
7. **Upload** via `twine upload --skip-existing` — kalau versi sudah ada di PyPI, aman skip (idempoten).
8. **Summary** dengan link PyPI.

**Autentikasi:** workflow pakai GitHub Actions secret `PYPI_API_TOKEN` (username `__token__`). Token PyPI **project-scoped** ini di-set sekali via `gh secret set`. Cara set/rotate token:

```bash
# Metode andal — pakai env-file (JANGAN pakai --body - karena pipe stdin
# rawan corrupt untuk token panjang).
TMPFILE=$(mktemp) && chmod 600 "$TMPFILE"
python3 -c "
import configparser, os
c = configparser.ConfigParser()
c.read(os.path.expanduser('~/.pypirc'))
print('PYPI_API_TOKEN=' + c['pypi']['password'])
" > "$TMPFILE"
gh secret set -f "$TMPFILE" --repo mamatnurahmat/build-q
rm -f "$TMPFILE"

# Verifikasi:
gh secret list --repo mamatnurahmat/build-q
```

**Manual trigger tanpa tag** (misal untuk re-upload versi yang sudah ada di `pyproject.toml` — `--skip-existing` akan handle):

```bash
gh workflow run pypi-release.yml --repo mamatnurahmat/build-q
```

**Cek/re-run workflow yang gagal:**

```bash
gh run list --workflow=pypi-release.yml --repo mamatnurahmat/build-q --limit 5
gh run view <RUN_ID> --repo mamatnurahmat/build-q --log-failed
gh run rerun <RUN_ID> --failed --repo mamatnurahmat/build-q
```

### B. Release manual via Makefile (lokal)

Cocok untuk hotfix cepat / troubleshoot. Baca kredensial dari `~/.pypirc` lokal.

```bash
make build             # build sdist + wheel ke dist/
make install           # install lokal (pipx editable)
make release           # bump patch, build, install, upload ke PyPI
make release V=1.0.0   # release versi spesifik
make clean             # bersihkan artefak
```

Langkah `make release`:

1. `scripts/bump_version.py` bump patch di `pyproject.toml` + `build_q/__init__.py`.
2. Build sdist + wheel via `python3 -m build` (fallback: `pipx run --spec build pyproject-build`).
3. Install lokal editable via `pipx install -e . --force`.
4. Upload ke PyPI via `pipx run twine upload --skip-existing dist/*` — kredensial dari `~/.pypirc`.

> ⚠️ **Setelah `make release`, jangan lupa commit + tag + push** supaya history Git & GitHub Actions selaras dengan PyPI:
>
> ```bash
> VER=$(grep -m1 '^version = ' pyproject.toml | sed 's/.*"\(.*\)".*/\1/')
> git add pyproject.toml build_q/__init__.py
> git commit -m "release: v$VER"
> git tag "v$VER" && git push origin main "v$VER"
> ```
>
> Tag push tetap akan memicu workflow, tapi karena `--skip-existing`, upload akan skip (idempoten).

### Release troubleshooting

| Gejala | Penyebab | Solusi |
| -------- | ---------- | -------- |
| Workflow gagal di step **Verify tag matches** | Tag `v0.1.13` tapi `pyproject.toml` masih `0.1.12` | Jalankan `python3 scripts/bump_version.py 0.1.13` dulu, commit, re-tag |
| Workflow gagal `403 Forbidden` di upload | Secret `PYPI_API_TOKEN` corrupt / kadaluwarsa | Re-set secret via metode env-file di atas |
| PyPI `400 File already exists` | Versi sudah pernah di-upload dari lokal | Sudah di-handle `--skip-existing` — workflow tetap hijau |
| Version di PyPI tidak berubah setelah tag | Tag lama di-hapus & re-push versi sama | PyPI TIDAK menerima re-upload versi yang sama — bump patch dulu |

---

## 📄 License

MIT
