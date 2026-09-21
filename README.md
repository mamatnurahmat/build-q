# build-q (bq) 🚀

**build-q** (dibaca *bq*) adalah CLI Python zero-dependency untuk operasi `docker buildx` lokal & remote yang selaras dengan pipeline CI/CD (Jenkins X). Satu perintah pendek `bq` menggantikan `docker buildx build` yang panjang, dengan auto-detect Git, `cicd/cicd.json`, secret `netrc`, resource limit, dan idempotency check terhadap registry.

Selain itu, `bq` menyediakan scaffolding CI/CD (`--init-jx`), setup secrets GitHub Actions (`--init-secrets`), migrasi Dockerfile legacy ke pola `--mount=type=secret` (`--fix-dockerfile`), dan auto-init Docker Buildx builder (`--init`).

---

## 🛠 Fitur

### Build & Push
- **Zero-dependency**: hanya Python 3.7+ standard library.
- **Git auto-detection**: nama repo & ref (branch/tag/short-SHA) dari Git.
- **Mode sumber kode**: build lokal, `--clone` (via `gh` CLI), atau `--remote` (Buildx Git context).
- **Registry idempotency**: cek image di registry sebelum build → skip bila sudah ada (bypass `--rebuild`).
- **Smart branch build-arg**: `BRANCH=production` untuk tag `v*`, else `BRANCH=develop`.
- **Default netrc secret**: `--secret id=netrc,src=$HOME/.netrc` otomatis.
- **Resource limit default**: memory & CPU aman untuk laptop.
- **CI/CD alignment**: baca `PORT/PORT2/PROJECT/IMAGE` dari `cicd/cicd.json`.
- **Dry run**: pratinjau command tanpa eksekusi.
- **GitHub org shorthand**: set `GITHUB_ORG=...` di config → cukup `bq <repo>` bukan `bq <owner>/<repo>`.

### Setup & Automation
- **`--init`**: buat file config `~/.build-q/.env` **dan** auto-create Docker Buildx builder (bootstrap).
- **`--init-jx`**: scaffold `Makefile` + `compose.yaml` + `Dockerfile` (modern secret mount) + `.github/workflows/trigger-ci.yml` dari `cicd/cicd.json`. Auto-panggil `--init-secrets` bila git remote GitHub terdeteksi.
- **`--init-secrets`**: set GitHub Actions secrets (`WEBHOOK_TRIGGER_URL`, `WEBHOOK_TRIGGER_TOKEN`) — auto-fetch token dari k8s secret `webhook-trigger-token` di namespace `jenkins-x`, auto-detect target repo dari `git remote`.
- **`--fix-dockerfile`**: migrasi Dockerfile legacy — `FROM ... as ...` → `AS`, hapus `ARG GITHUB_USER/TOKEN`, ubah `RUN echo "machine github.com..." > ~/.netrc && ...` → `RUN --mount=type=secret,id=netrc,target=/root/.netrc \ ...`. Simpan backup ke `Dockerfile.bak`.
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

---

## ⚙️ Konfigurasi Awal (sekali saja)

```bash
bq --init         # buat ~/.build-q/.env + create Docker buildx builder
bq --init --force # recreate config; builder tetap aman (cek dulu, buat bila tidak ada)
bq --config       # tampilkan konfigurasi aktif
```

### `~/.build-q/.env`

| Variable | Default | Keterangan |
|----------|---------|-----------|
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

### 6. Bootstrap CI/CD service baru (Jenkins X)
```bash
mkdir my-new-service && cd my-new-service
cat > cicd/cicd.json <<EOF   # buat dulu manual
{"IMAGE":"my-new-service","PROJECT":"qoinplus","PORT":"8080"}
EOF
bq --init-jx                 # scaffold Makefile/compose/Dockerfile/workflow
git init && git remote add origin git@github.com:Qoin-Digital-Indonesia/my-new-service.git
bq --init-secrets            # set webhook secrets di GitHub (auto-detect repo)
git add . && git commit -m "chore: bootstrap CI/CD"
git push -u origin main      # trigger Jenkins X pipeline via GitHub Actions
```

### 7. Migrasi Dockerfile legacy (ARG-based netrc → secret mount)
```bash
cd my-old-service
bq --fix-dockerfile                # migrasi ./Dockerfile (backup ke .bak)
bq --fix-dockerfile path/to/Dockerfile
bq --no-push --rebuild             # test build hasil migrasi
```

### 8. Workaround Dockerfile legacy tanpa migrasi (--gh-auth)
```bash
bq plus-be-service staging --remote --rebuild --gh-auth
# → inject GITHUB_USER dari `gh api user` + GITHUB_TOKEN dari `gh auth token`
```

### 9. Setup secrets Jenkins X untuk repo existing
```bash
cd existing-repo
bq --init-secrets                                        # auto-detect dari git
bq --init-secrets Qoin-Digital-Indonesia/foo-service     # eksplisit
bq --init-secrets foo-service --token xxxx               # skip kubectl
```

### 10. Contoh full (customize secret, platform, build-arg)
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
  --init-jx                 Scaffold Makefile/compose/Dockerfile/workflow
  --init-jx --force         Overwrite file existing
  --init-secrets [<repo>]   Set GitHub Actions webhook secrets
  --fix-dockerfile [PATH]   Migrasi Dockerfile legacy ke secret mount

Build modes:
  (default)                 Build dari direktori lokal
  --clone <owner/repo>      Clone via `gh` CLI, lalu build
  --clone ... --clean       Hapus folder clone setelah build
  --remote                  Build via Buildx Git context (tanpa clone)

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
  --token VALUE             Webhook token (dengan --init-secrets)
  --dry-run                 Preview command tanpa eksekusi
  --version                 Tampilkan versi
```

---

## 📋 Requirements

- **Python 3.7+**
- **Docker** + **Buildx plugin**
- **Git** (opsional; wajib untuk auto-detect & mode local)
- **GitHub CLI (`gh`)** — wajib untuk `--clone`, `--remote`, `--gh-auth`, `--init-secrets`
- **kubectl** — opsional; hanya untuk `--init-secrets` (fetch token otomatis dari cluster)

---

## 🚀 Development & Release

Proyek pakai `Makefile` untuk build & release.

```bash
make build         # build sdist + wheel ke dist/
make install       # install local (pipx editable)
make release       # bump patch, build, install, upload ke PyPI
make release V=1.0.0   # release versi spesifik
make clean         # bersihkan artefak
```

Release process (`make release`):
1. `scripts/bump_version.py` bump patch version di `pyproject.toml` & `build_q/__init__.py`.
2. Build sdist + wheel via `python3 -m build` (fallback: `pipx run --spec build pyproject-build`).
3. Install lokal editable via `pipx`.
4. Upload ke PyPI via `pipx run twine`. Kredensial dibaca dari `~/.pypirc`.

---

## 📄 License

MIT
