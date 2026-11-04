# PRD: Rollback GitOps lewat Pull Request (`bq --pr-rollback <commit_url>`)

> **Version:** 0.2 (Fase 1 terimplementasi — branch `feat/pr-rollback`)
> **Last Updated:** 2026-11-04
> **Rilis target:** v0.2.8 (Fase 1, CLI) · Fase 2 = devops-app halaman Pipeline (+ endpoint `~/jenkins-x` webhook-trigger) · Fase 3 = MCP/TUI/URL PR
> **Modul baru:** `build_q/pr_rollback.py` · `build_q/secret_scan.py` (ekstrak dari `bin.py`) · `tests/test_pr_rollback.py`
> **Modul yang disentuh:** `build_q/cli.py` · `build_q/github_api.py` · `build_q/pr_fix_trace.py` · `build_q/bin.py` · reuse `gitops_set_image._image_is_ready` dan `e2e.argocd_api_apps` / `find_argocd_app`

---

## 1. Ringkasan

Rollback di repo GitOps saat ini dikerjakan manual: buka commit, pastikan file tidak
diubah lagi setelahnya, buat branch, `git revert`, cek hasil, push, lalu buat PR. Contoh
nyata pada 2026-11-03: commit
[`9072935`](https://github.com/Qoin-Digital-Indonesia/gitops/commit/90729351395603882582763055662896febb6dd6)
mengganti image `qoincash-fe-linkage` production menjadi `wal-fe-seamless:v1.0.0-sls`, lalu
dikembalikan lewat PR #826 dengan sekitar 8 langkah manual.

`bq --pr-rollback <commit_url>` mengotomatiskan semua langkah itu menjadi **satu perintah
yang selalu berakhir di Pull Request**. Perintah ini tidak pernah push ke `main`. Cakupannya
dibatasi pada dua jenis file deploy:

| Jenis | Contoh path | Cara perubahan live setelah PR di-merge |
|---|---|---|
| **compose** (FE, Docker Compose) | `web/production/qoincash-fe-linkage/docker-compose.yaml` | redeploy di host via SSH docker compose (bukan ArgoCD) |
| **deploy yaml** (K8s, ArgoCD) | `cce/production-qoin/plus-be-x_deployment.yaml`, `k8s/production-payout/pay-be-x_deployment.yaml` | ArgoCD sync folder `<infra>/<ns>/` |

### Contoh Perintah

```bash
# Kasus utama: URL commit GitHub
bq --pr-rollback https://github.com/Qoin-Digital-Indonesia/gitops/commit/90729351395603882582763055662896febb6dd6

# Pratinjau saja (tanpa commit/push/PR), workdir dipertahankan
bq --pr-rollback <commit_url> --dry-run

# Non-interaktif (CI / MCP) — lewati konfirmasi
bq --pr-rollback <commit_url> --yes

# Bentuk pendek (repo default = GITOPS_REPO di ~/.build-q/.env)
bq --pr-rollback 9072935
bq --pr-rollback Qoin-Digital-Indonesia/gitops@9072935
```

## 2. Tujuan & Non-Tujuan

**Tujuan**

1. Rollback satu commit di repo GitOps menjadi PR siap review dalam satu perintah.
2. **Aman secara default:** hanya file compose atau deploy yaml, hanya PR (tidak pernah
   push ke base), ada konfirmasi sebelum push, dan ada pratinjau `--dry-run`.
3. Menolak lebih awal (fail fast) kasus yang sering membuat rollback justru menimbulkan insiden baru:
   image lama sudah hilang dari registry, ada commit berikutnya yang bentrok, image dikunci
   override ArgoCD, atau commit bukan bagian dari branch base.
4. Hasil yang bisa dibuktikan: isi file setelah revert diverifikasi sama dengan versi
   sebelum commit (bila tidak ada perubahan lanjutan), dan langkah-langkahnya dicatat di trace.

**Non-tujuan (Fase 1)**

- Tidak auto-merge PR dan tidak mengubah cluster atau host (tidak ada ArgoCD sync,
  `kubectl`, atau `docker compose up`). Yang diberikan hanya **petunjuk** langkah setelah merge.
- Tidak me-revert file di luar allowlist (kustomization, service, HPA, partial
  apigateway, `.github/`, dan lain-lain).
- Tidak me-revert commit yang **menambah atau menghapus** file (bootstrap atau decommission). Fase 1
  hanya untuk modifikasi (`status = modified`).
- Tidak me-revert beberapa commit atau satu rentang commit sekaligus.

## 3. Input yang Diterima

| Bentuk | Contoh | Resolusi |
|---|---|---|
| URL commit | `https://github.com/<owner>/<repo>/commit/<sha>` (boleh ada `#diff-…`, `?…`, atau `/` di akhir) | repo dan SHA diambil dari URL |
| `owner/repo@sha` | `Qoin-Digital-Indonesia/gitops@9072935` | dari string |
| SHA saja (≥ 7 hex) | `9072935` | repo = `config.gitops.repo` |
| URL PR *(Fase 2)* | `https://github.com/<owner>/<repo>/pull/822` | `GET /pulls/822` → `merge_commit_sha`, wajib `merged=true` |

Parser: `parse_rollback_target(s) -> (repo, sha)`, lalu SHA pendek diubah ke SHA penuh lewat
`github_api.get_commit_sha()` (sudah ada).

## 4. Allowlist File

Semua file di commit **wajib** cocok dengan salah satu pola di bawah dan berstatus `modified`.
Bila ada satu saja yang tidak cocok, seluruh rollback ditolak dan file penyebabnya
ditampilkan (exit 1).

| Kind | Pola (glob terhadap path repo) |
|---|---|
| `compose` | `web/*/*/docker-compose.yaml`, `web/*/*/docker-compose.yml`, `web/*/*/compose.yaml` |
| `deploy` | `cce/*/*_deployment.yaml`, `k8s/*/*_deployment.yaml` |

Pola ini bisa di-override dengan env `PR_ROLLBACK_ALLOW` (dipisah koma) bila struktur repo berubah,
tanpa perlu rilis baru.

**Slug aplikasi** (untuk nama branch dan judul PR) diambil dari path:
`web/<env>/<app>/…` → `<app>`; `<infra>/<ns>/<app>_deployment.yaml` → `<app>`.
Bila ada lebih dari satu aplikasi, slug = `<app pertama>-plus<N>`.

## 5. Alur

Setiap langkah dicatat ke trace (nama step di kolom kanan) memakai pola yang sama dengan `--pr-fix`.

| # | Langkah | Detail | Step trace |
|---|---|---|---|
| 0 | Parse input | `parse_rollback_target` → `(repo, sha)` | `parse` |
| 1 | Preflight | `GH_CLI=false`, `GITHUB_TOKEN` ada, `git` ada di PATH | `preflight` |
| 2 | Resolve commit (API) | `GET /repos/{repo}/commits/{sha}` → SHA penuh, `parents`, `files[].status/filename`, subject | `resolve_commit` |
| 3 | Cek cakupan | allowlist + status `modified` (§4). Untuk **merge commit** (`len(parents) > 1`), file diambil dari `compare/{parent1}...{sha}` | `scope` |
| 4 | Cek base | base = `--gitops-branch` atau `config.gitops.branch` (`main`). `GET compare/{base}...{sha}` wajib `behind`/`identical` (commit sudah ada di base). Bila tidak → tolak dengan petunjuk `--gitops-branch <branch>` | `base_check` |
| 5 | Dedup | (a) PR terbuka dengan head `revert/<slug>-<sha7>` → cetak URL, exit 0. (b) *dijalankan setelah clone (langkah 6)*: base sudah berisi commit `This reverts commit <sha>` → "sudah di-rollback", exit 0 | `dedup` |
| 6 | Clone | partial clone `git clone --filter=blob:none --single-branch --branch <base>` ke `PR_FIX_BASE_WORKDIR/bq-pr-rollback-<slug>-<ts>`. **Bukan** `--depth 1`, karena revert butuh commit target dan parent-nya | `clone` |
| 7 | Commit lanjutan | `git log <sha>..origin/<base> -- <files>`. Bila ada → tampilkan sebagai ⚠️ (revert tetap dicoba, perubahan lanjutan dipertahankan) | `later_commits` |
| 8 | Branch + revert | `git checkout -b revert/<slug>-<sha7>`, lalu `git revert --no-commit [-m 1] <sha>`. Bila konflik → `git revert --abort`, tampilkan daftar commit lanjutan, exit 1 | `revert` |
| 9 | Verifikasi isi | Bila tidak ada commit lanjutan: `git diff --cached --quiet <sha>^ -- <files>` **wajib** identik. Bila ada: cukup pastikan diff = kebalikan diff commit untuk hunk yang tersentuh. Diff kosong → `no_change`, exit 0 | `verify` |
| 10 | Image lama siap | Ambil semua `image:` per file (sebelum → sesudah) memakai `_IMAGE_LINE_RE` (semua match, bukan hanya yang pertama, karena compose bisa punya banyak service). Tiap image hasil revert dicek dengan `_image_is_ready()`. Bila tidak siap → **tolak** (rollback ke image yang tidak ada = outage). Bisa dilewati dengan `--skip-image-check` | `image_check` |
| 11 | Override ArgoCD *(deploy saja)* | `argocd_api_apps()` + `find_argocd_app()` (dari `e2e.py`). Bila app punya `source.kustomize.images` untuk image tersebut → ⚠️ "revert YAML tidak akan mengubah image live, hapus override (lihat `gitops-audit`)". Soft-fail bila token ArgoCD tidak ada | `argocd_override` |
| 12 | Scan secret | `secret_scan.scan(text)` pada file hasil revert. Output hanya **label + nomor baris + nama key**, tidak pernah nilainya. Default ⚠️ (tidak memblokir) dan dicantumkan di body PR | `secret_scan` |
| 13 | Pratinjau + konfirmasi | Tabel ringkas (file, kind, image sebelum → sesudah, `git diff --cached --stat`, commit lanjutan, hasil scan). Prompt `Lanjut commit + push + buat PR? [y/N]`. `--yes` melewati prompt. Non-TTY tanpa `--yes` → batal (exit 3). `--dry-run` berhenti di sini dan workdir dipertahankan | `confirm` |
| 14 | Commit | format di §6. Guard keras: tolak bila `branch == base` | `commit` |
| 15 | Push | `git push origin revert/<slug>-<sha7>` (tidak pernah ke base) | `push` |
| 16 | Buka PR | `github_api.create_pull_request(base=<base>)`, body di §6 | `open_pr` |
| 17 | Petunjuk setelah merge | compose → petunjuk redeploy host (`docker compose pull && up -d`; resolve PROHOST dari `cicd.json` → Fase 3). deploy → folder ArgoCD (+ nama app & `bq argocd app get <app>` bila token ArgoCD tersedia) | `post_merge_hint` |

### Pemetaan exit code

| Code | Arti |
|---|---|
| 0 | PR dibuat, atau tidak ada yang perlu dilakukan (PR sudah ada / sudah di-rollback / diff kosong / `--dry-run`) |
| 1 | Validasi gagal: di luar allowlist, file A/D, bukan di base, konflik, atau image tidak siap |
| 2 | Error git/GitHub: clone, push, atau API |
| 3 | Dibatalkan user di prompt konfirmasi (atau non-TTY tanpa `--yes`) |

## 6. Format Commit & PR

**Branch:** `revert/<slug>-<sha7>`, misalnya `revert/qoincash-fe-linkage-9072935` (deterministik → mudah dideduplikasi).

**Commit message:**

```
revert(<scope>): <slug> → <image hasil revert>

This reverts commit <sha penuh>.

Original: <subject commit asli>
- <file>: <image sebelum> → <image sesudah>
```

`<scope>` = direktori dua tingkat pertama, misalnya `web/production` atau `cce/production-qoin`.
Baris `This reverts commit <sha>.` wajib ada, karena dipakai untuk dedup (langkah 5b).

**Body PR** (judul = baris pertama commit):

```markdown
## Rollback
Revert <link commit> (`<subject asli>`) dibuat oleh `bq --pr-rollback`.

| File | Kind | Image sekarang | Image setelah merge |
|---|---|---|---|

- Verifikasi: isi file identik dengan `<sha7>^` ✅ | ada N commit lanjutan, dipertahankan ⚠️
- Image setelah merge ada di Docker Hub ✅

## Setelah merge
- compose: redeploy host <PROHOST> (docker compose pull && up -d)
- deploy: ArgoCD app `<app>` → sync / cek `bq argocd app get <app>`

## ⚠️ Catatan
- Secret plaintext terdeteksi: `RSA_PRIV_KEY` (baris 22), … (nama key saja)
- Override kustomize ArgoCD pada image ini: …
```

## 7. CLI Reference

| Flag | Status | Fungsi |
|---|---|---|
| `--pr-rollback TARGET` | **baru** | URL commit / `owner/repo@sha` / sha |
| `--gitops-branch BRANCH` | reuse | base PR (default `config.gitops.branch` = `main`) |
| `--pr-branch NAME` | reuse | override nama branch revert |
| `--dry-run` | reuse | berhenti setelah pratinjau, tanpa commit/push/PR |
| `--keep-workdir` | reuse | pertahankan clone di `/tmp` |
| `--yes` | **baru** | lewati prompt konfirmasi (CI/MCP) |
| `--skip-image-check` | **baru** | lewati cek image di Docker Hub (darurat, dengan peringatan merah) |
| `--export-json PATH` | reuse | laporan JSON (`RollbackReport`); `-` = stdout murni JSON, teks lain ke stderr — dipakai devops-app |
| `--run-id ID` | **baru** | ID jejak (default `rollback-<sha7>-<waktu>`); pemanggil (devops-app) membaca hasil di PB `pr_rollback_runs` |

Dispatch di `cli.py` dibuat mengikuti pola blok `if args.pr_fix:` dan memanggil
`run_pr_rollback(target, base=…, pr_branch=…, dry_run=…, keep_workdir=…, assume_yes=…, skip_image_check=…, export_json=…, run_id=…) -> int`.
Perintah ini wajib login (gate `requires_login` default), sama dengan perintah yang mengubah sistem lain.

## 8. Perubahan Kode

| File | Perubahan (Fase 1 — selesai) |
|---|---|
| `build_q/pr_rollback.py` *(baru)* | orkestrator §5: `parse_rollback_target`, `classify_files`/`file_kind` (glob per segmen), `rollback_slug`, `extract_images` (semua `image:`), `check_image`, `build_commit_message`, `build_pr_body`, `RollbackReport`, `RollbackTrace`, `PR_ROLLBACK_COLLECTIONS` + `ensure_trace_collections()`, `run_pr_rollback` |
| `build_q/secret_scan.py` *(baru)* | `SECRET_PATTERNS` + `scan_patterns` dipindah dari `bin.py` (identik) + `scan_keys` (key env/YAML `*SECRET*`, `*PASSWORD*`, `*PRIV_KEY*`, `*TOKEN*`, `*AUTH_BASIC*`, `*API_KEY*` bernilai literal; `*_PUB_KEY` dikecualikan) + `scan` / `describe` |
| `build_q/bin.py` | memakai `secret_scan` (perilaku sama; `SECRET_PATTERNS`/`_scan_secrets` tetap tersedia) |
| `build_q/github_api.py` | `get_commit()`, `compare()`; **fix** `list_open_prs()` — GitHub mengabaikan filter `head` tanpa prefix `owner:` (mengembalikan semua PR terbuka), kini `owner:branch` + saring `head.ref`. Ikut memperbaiki dedup `--pr-fix` |
| `build_q/pr_fix_trace.py` | `PrFixTrace` bisa di-subclass (atribut `LABEL`, `RUNS`, `STEPS`, `RUN_STATUSES`, `LOCAL_SUBDIR`); `ensure_collections(..., specs)` — default `--pr-fix` tidak berubah |
| `build_q/cli.py` | `--pr-rollback TARGET`, `--yes`, `--skip-image-check`, `--run-id` + dispatch + epilog |
| `tests/test_pr_rollback.py` *(baru)* | 46 test: parser, allowlist, secret scan, filter PR, pesan/body, integrasi repo git lokal (revert, konflik, merge `-m 1`, dedup, image, non-TTY, JSON stdout, jejak) |
| `README.md` | bagian `--pr-rollback` |

## 9. Guardrails & Keamanan

1. **Hanya PR.** Tidak ada jalur kode yang push ke base, dan ada guard eksplisit `branch != base`.
   Selaras dengan `.githooks/pre-push` dan aturan proteksi `main` di repo gitops.
2. **Konfirmasi manusia** sebelum push (prompt atau `--yes` eksplisit). MCP default hanya pratinjau.
3. **Allowlist ketat** (§4). File di luar daftar membatalkan seluruh rollback, tidak sebagian.
4. **Image lama wajib ada** di registry. Ini mencegah rollback yang malah membuat `ImagePullBackOff`
   atau container compose gagal start.
5. **Secret tidak pernah dicetak.** Scan hanya mengembalikan label/key/baris, dan token di URL clone
   tidak boleh muncul di log maupun trace (menyamarkan `x-access-token:…` di pesan error).
6. **Tidak menyentuh cluster/host.** Pengecekan override ArgoCD bersifat read-only dan soft-fail.
7. **Idempoten.** Nama branch deterministik + dedup PR + dedup "sudah di-revert" → aman dijalankan ulang.

## 10. Validasi

**Unit test** (`tests/test_pr_rollback.py`, tanpa jaringan: `github_api` di-monkeypatch, repo git fixture di `tmp_path`):

| Kasus | Ekspektasi |
|---|---|
| Parser: URL commit (+`#diff`, `/` di akhir), `owner/repo@sha`, sha pendek, URL tidak valid | `(repo, sha)` benar / exit 1 |
| Allowlist: compose ✅, `_deployment.yaml` ✅, `kustomization.yaml` ❌, file baru (A) ❌ | sesuai §4 |
| Revert bersih tanpa commit lanjutan | isi file == `<sha>^` |
| Commit lanjutan yang tidak bentrok | revert sukses dan perubahan lanjutan tetap ada, ⚠️ dicetak |
| Commit lanjutan yang bentrok | `revert --abort`, exit 1, daftar commit dicetak |
| Merge commit | memakai `-m 1` |
| Sudah di-revert / PR sudah terbuka | exit 0 tanpa push |
| Image tidak siap | exit 1; dengan `--skip-image-check` lanjut |
| Non-TTY tanpa `--yes` | exit 3, tidak ada push |
| `secret_scan` pada env RSA/`*_SECRET` | key dan baris terdeteksi, **nilai tidak ada** di output |
| `bin.py` setelah ekstraksi | `tests/test_bin.py` tetap hijau |

**Uji nyata (dry-run)** dengan kasus hari ini:

```bash
bq --pr-rollback https://github.com/Qoin-Digital-Indonesia/gitops/commit/90729351395603882582763055662896febb6dd6 --dry-run
```

Ekspektasi: kind `compose` · base `main` · 0 commit lanjutan · isi identik dengan `9072935^` ·
image `loyaltolpi/qoincash-fe-linkage:v2.0.2` ready · ⚠️ secret (`RSA_PRIV_KEY`,
`VOUCHER_TRANSACTION_PRIV_KEY`, `PLATFORM_INIT_SECRET`, `ACTIVATION_COOKIE_SECRET`,
`TOKEN_MAC_SECRET`, `CONNECT_TOKEN_AUTH_BASIC`) · lalu **berhenti di dedup**: selama PR #826
masih terbuka → 5a (head `revert/qoincash-fe-linkage-9072935` sama); setelah #826 di-merge → 5b
(`This reverts commit 9072935…` ada di `main`). Satu kasus ini sekaligus menguji kedua jalur dedup.

## 11. Tahapan

| Fase | Isi | Status |
|---|---|---|
| **1 — MVP CLI** | §3 (tanpa URL PR), §4, §5 langkah 0–17, §6, §7, `secret_scan.py`, jejak lokal JSONL + PB best-effort, `--export-json`/`--run-id`, unit test, README | ✅ branch `feat/pr-rollback` |
| **2 — devops-app (Pipeline)** | Tombol **Rollback PR** di halaman `/pipelines` devops-app → endpoint `POST /pr-rollback` di `~/jenkins-x` webhook-trigger (auth PB JWT + role admin/superuser) → Tekton PipelineRun `bq --pr-rollback --run-id <id> [--dry-run | --yes]` (pola sama dengan `/pr-fix`) → UI polling PB `pr_rollback_runs` untuk pratinjau lalu konfirmasi; collection `pr_rollback_runs`/`pr_rollback_steps` dibuat via `ensure_trace_collections()` | 🚧 |
| **3 — Integrasi lain** | input URL PR, tool MCP `gitops_rollback` + `tool_policy`, entri katalog TUI, resolve PROHOST compose dari `cicd.json`, `--watch` setelah merge, rollback rentang `A..B` | nanti |

## 12. Keputusan Terbuka (dengan rekomendasi)

1. **Secret plaintext pada hasil revert → blokir atau peringatan?**
   Rekomendasi: **peringatan** (tidak memblokir) + dicantumkan di body PR. Rollback biasanya
   mendesak, dan secret tersebut memang sudah ada di riwayat git. Sediakan `--strict-secrets`
   untuk memblokir.
2. **Commit lanjutan pada file yang sama → tetap revert atau tolak?**
   Rekomendasi: **tetap coba revert** (3-way, perubahan lanjutan dipertahankan) dan tolak hanya
   bila konflik. Tidak ada mode "kembalikan file ke `<sha>^` apa adanya", karena mode itu diam-diam
   ikut membatalkan rilis lain.
3. **Override ArgoCD kustomize terdeteksi → blokir?**
   Rekomendasi: **peringatan** saja di Fase 1. Perbaikan override adalah ranah `gitops-audit`
   (`--strip-overrides`), bukan rollback.
