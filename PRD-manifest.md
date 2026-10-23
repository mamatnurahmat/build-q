# PRD: Manifest GitOps — Cek, Saran, Template Standar & Uji (`bq` Manifest Suite)

> **Version:** 1.1
> **Last Updated:** 2026-10-23
> **Rilis:** tahap Manifest `--e2e` (v0.2.2) · `--manifest-check` (v0.2.3) · `--manifest-init`, `--manifest-template`, `--manifest-verify` (v0.2.4) · collection `nodepool` (v0.2.5) · `--manifest-init --pr`, analisa source (bahasa, mode env .NET, path & format file config) (v0.2.6)
> **Modul:** `build_q/manifest_check.py` (rule, cek, generator, template) · `build_q/manifest_scan.py` (`--manifest-check`) · `build_q/manifest_init.py` (`--manifest-init`, `--manifest-verify`) · `build_q/manifest_template.py` (`--manifest-template`) · `build_q/manifest_pr.py` (`--manifest-init --pr`) · `build_q/e2e.py` (tahap Manifest, IO, pemilihan sample) · `build_q/kube_contexts.py` (mapping cluster)
> **Dokumentasi pengguna:** [E2E-MANIFEST.md](./E2E-MANIFEST.md)

---

## 1. Ringkasan

Deploy di Qoin mengikuti GitOps: pipeline memperbarui image di repo GitOps, lalu ArgoCD
menyinkronkan folder `<infra>/<namespace>/` ke cluster. Banyak insiden justru bukan di
pipeline, melainkan di **manifest**: selector Service tidak cocok dengan pod, `targetPort`
bernama yang tidak ada, Secret config belum dibuat, file tidak terdaftar di kustomization,
atau aplikasi baru disalin dari tetangga yang ternyata tidak standar.

Manifest Suite menjawab tiga kebutuhan, semuanya **read-only** terhadap GitOps dan cluster:

| Kebutuhan | Perintah | Hasil |
|---|---|---|
| "Commit saya sudah live tapi kenapa tidak jalan?" | `bq --e2e <repo> <ref>` | tahap **Manifest** di rantai e2e + saran YAML |
| "Apakah satu namespace sehat?" | `bq --manifest-check <namespace>` | laporan semua Deployment (Markdown/JSON) |
| "YAML yang benar untuk aplikasi ini di namespace X?" | `bq --manifest-init <repo> <namespace>` | Deployment + Service + Secret + kustomization sesuai konsep GitOps |
| "Apakah generator konsisten & tidak merusak core?" | `bq --manifest-verify <namespace>` | uji leave-one-out terhadap manifest nyata |
| "Template standar terpusat" | `bq --manifest-template <ns…>` | template per namespace × jenis aplikasi di PocketBase |
| "Ajukan saran itu ke GitOps" | `bq --manifest-init <repo> <ns> --pr` | branch + PR (Deployment/Service, kustomization +resources, tanpa Secret) |

### Contoh Perintah

```bash
bq --e2e saas-be-loyalty-module develop
bq --manifest-check develop-qoin --export
bq --manifest-init pay-be-topup-manager production-payout
bq --manifest-init pay-be-topup-manager sandbox-payout --out-dir ./gitops-draft
bq --manifest-template all && bq --tui-sync ~/.build-q/manifest-templates-seed.json
bq --manifest-verify production-qoin --export-md verify.md
```

## 2. Tujuan & Non-Tujuan

**Tujuan**

1. Mendeteksi manifest yang memutus deploy/traffic sebelum atau sesudah rilis, dengan temuan
   palsu serendah mungkin (dinilai pada hasil akhir kustomization, bukan file mentah).
2. Memberi saran YAML yang **langsung bisa dipakai** dan mengikuti konvensi nyata repo GitOps.
3. Menjamin saran untuk aplikasi baru **konsisten** (template terpusat) dan **tidak mengubah
   core** manifest yang sudah berjalan (uji leave-one-out).
4. Semua standar (rule, severity, params, template) terpusat di PocketBase, bisa diubah tanpa rilis.

**Non-tujuan**

- Tidak meng-apply, commit, atau membuat PR otomatis — saran diajukan manusia lewat PR.
- Tidak membaca isi Secret (hanya nama).
- Tidak menggantikan `--bootstrap-k8s` (yang membuat PR); Manifest Suite fokus cek & saran.

## 3. Pengguna & Skenario

| Persona | Skenario |
|---|---|
| Developer | commit sudah ter-build tapi endpoint 503 → `--e2e` menunjuk Service tanpa endpoint + saran |
| DevOps | audit rutin satu namespace sebelum cutover → `--manifest-check --export`, kirim laporan |
| DevOps | onboarding aplikasi baru ke namespace → `--manifest-init … --out-dir`, ajukan PR |
| Maintainer `bq` | mengubah generator → `--manifest-verify` di namespace production, target 0 diff |

## 4. Arsitektur

```
                 ┌──────────────── PocketBase devops ────────────────┐
                 │ build_q_anomaly_rules   build_q_manifest_templates │
                 │ kube_cx + namespace (mapping cluster)              │
                 └───────────────┬───────────────────────────────────┘
                                 │ (cache ~/.build-q/*.json, TTL 1 jam)
 GitHub API (repo source,        ▼
 repo GitOps) ──────────► manifest_check ◄──── kubectl get … -o name (nama Secret/Deployment)
                          │  check()  generator  template  sample
        ┌─────────────────┼──────────────┬────────────────┬─────────────────┐
        ▼                 ▼              ▼                ▼                 ▼
   e2e (tahap       manifest_scan   manifest_init   manifest_template  manifest_init
   Manifest)        (--manifest-    (--manifest-    (--manifest-       .verify_folder
                     check)          init)           template)          (--manifest-verify)
```

Komponen kunci:

| Komponen | Tanggung jawab |
|---|---|
| `manifest_check.check()` | 10 pemeriksaan deterministik pada Deployment/Service hasil kustomization |
| `apply_patches()` | evaluasi patch JSON6902 kustomization (add/replace/remove, target kind/name/labelSelector) |
| `suggest_*` / `adapt_sample_*` | generator standar & adaptasi template/sample ke aplikasi target |
| `load_rules()` / `load_templates()` | PB → cache → default bawaan |
| `e2e._manifest_sample()` | sumber saran: template PB → sample tetangga → (pemanggil) generator |
| `kube_contexts` | context cluster: destination ArgoCD → mapping namespace → kandidat ambigu |

## 5. Alur Per Perintah

### 5.1 Tahap Manifest di `bq --e2e`

1. Baca YAML deployment dari tahap GitOps (branch ENV) dan `kustomization.yaml` folder.
2. Namespace efektif = `namespace:` kustomization → `metadata.namespace` → destination ArgoCD → nama folder.
3. Tahap ArgoCD dibaca lebih dulu; `spec.destination` dipetakan ke context `kube_cx` (field `server`).
4. `kubectl get secrets -o name` di namespace tsb (fallback: indikasi file di repo).
5. `check()` → temuan + saran YAML; Jev menilai kesiapan bila ada temuan (TUI/web; CLI `--no-jev`).
6. Status: critical → ❌ titik putus; Secret belum terverifikasi → ⏭️ (incomplete); warn → ✅ + saran.

### 5.2 `bq --manifest-check <namespace>`

1. Daftar `*_deployment.yaml` di `<branch>:<infra>/<namespace>/` (branch mengikuti ENV namespace).
2. Satu kali pemilihan cluster + satu kali `kubectl get secrets` per namespace.
3. `check()` per Deployment secara paralel (8 worker, ContextVar credential ikut ke thread).
4. Render teks / Markdown / JSON; exit 0 OK · 1 FAIL · 2 SKIP. Tersedia di CLI, TUI, web.

### 5.3 `bq --manifest-init <repo> <namespace>`

1. Env = prefix namespace. Ref source: develop/staging/sandbox → branch sama; production → tag
   `v*` semver tertinggi. Branch/tag belum ada → branch default repo (dengan catatan).
2. `cicd.json` (IMAGE, DEPLOYMENT, PROJECT, PORT, STACK) → path via `compute_rollout`
   (aturan yang sama dgn saran rollout pipeline); namespace argumen menimpa PROJECT.
3. Analisa source aplikasi (§5.6): bahasa → format file config, Dockerfile → path file config,
   source .NET → mode environment.
4. Manifest sudah ada → hasil `check()` + temuan berbasis source (`config_mount_path_mismatch`,
   `config_format_mismatch`) + saran perbaikan (tidak pernah menimpa).
5. Belum ada → template PB → sample tetangga → generator, lalu diterapkan hasil analisa source
   (path mount config, mode env .NET, key Secret); kerangka Secret hanya untuk Secret yang belum ada
   di cluster; baris `resources` kustomization atau `kustomization.yaml` baru.
6. Output: teks, `--out-dir` (struktur path GitOps), `--export-md`.
7. `--pr` (v0.2.6): satu commit di branch `manifest/<app>-<folder>-<waktu>` lewat GitHub Git Data
   API lalu PR ke branch env. `kustomization.yaml` hanya ditambah baris `resources` (divalidasi:
   key lain identik, kalau tidak → batal). Secret tidak pernah di-commit (dibuang + pengaman
   `kind: Secret`). `--pr --dry-run` = pratinjau.

### 5.4 `bq --manifest-template <ns,…|all>`

1. Per namespace × jenis aplikasi (akhiran nama), kumpulkan kandidat.
2. Tolak yang tidak standar: label (`project` = akhiran namespace), replicas, nama Secret config,
   mount config ganda, Service/kustomization, temuan critical.
3. Dahulukan kandidat dengan **konvensi port Service mayoritas** (port klien tetap vs = container).
4. Nama aplikasi → placeholder `__app__`; seed lokal `~/.build-q/manifest-templates-seed.json`
   (tidak di repo); `bq --tui-sync` → PB.

### 5.5 `bq --manifest-verify <namespace>`

Leave-one-out pada `main:<infra>/<namespace>/`: tiap Deployment di-generate dari template/sample
lain (template dari dirinya sendiri dikecualikan); data khas aplikasi (port, path mount config,
args dotnet, config baked, type & port Service yang diekspos) diberikan sebagai input; field
inti dibandingkan setelah patch kustomization; perbedaan diklasifikasikan:

| Status | Kondisi |
|---|---|
| match | core identik |
| match-nonstandard | beda hanya di field di mana manifest asli menyimpang dari standar |
| diff | generator berbeda dari core standar → **regresi** (exit 1) |
| unique | tidak ada aplikasi sejenis |
| skip | manifest asli tidak lengkap / gagal dibaca |

### 5.6 Analisa source aplikasi (v0.2.6)

Data khas aplikasi dibaca dari repo source pada ref env (production: tag `v*`), bukan ditebak:

**a. Bahasa → format file config.** Berlapis, dengan bukti di output: `STACK`/`TYPE` cicd.json →
file penanda root (`*.csproj`/`*.sln`/`global.json`/`appsettings*.json` = .NET, `go.mod` = Go,
`Cargo.toml` = Rust, `package.json` = Node, `requirements.txt`/`pyproject.toml` = Python) → base
image Dockerfile (`mcr.microsoft.com/dotnet`, `golang`, `rust`, `node`, `python`, `krakend`) →
GitHub Languages API → default Go. **.NET → `appsettings.<env runtime>.json`; bahasa lain → `.env`.**

**b. Path file config ← Dockerfile.** Stage final: tujuan `COPY … .env` → selain itu `WORKDIR`
(diwarisi lewat alias stage; **tanpa WORKDIR = `/`**; `$VAR` = tidak diketahui → path template).
Go `godotenv.Load()` membaca `.env` relatif direktori kerja.

| Referensi | Dockerfile (stage final) | Pembacaan config | mountPath |
|---|---|---|---|
| `plus-be-appmerchant-manager` (Go) | `FROM alpine`, tanpa WORKDIR, `COPY .env.${BRANCH} .env` | `godotenv.Load()` | `/.env` |
| `ngenwal-be-saasaltoservice-manager` (.NET) | `FROM base`, `WORKDIR /app` | generic host → appsettings | `/app/…` (di GitOps `.env` → format salah) |

**c. Mode environment .NET ← `Program.cs`/`Startup.cs`** (dicari juga di subfolder):

| Pola kode | Manifest | Referensi |
|---|---|---|
| nilai `GetEnvironmentVariable("ASPNETCORE_\|DOTNET_ENVIRONMENT")` dipakai dlm **logika** (`Equals`, `==`, `switch`, …) | env `ASPNETCORE_ENVIRONMENT=<env>` | `ngenwal-be-transactionconverter-manager` (`string.Equals(environment, "develop")`) |
| env var hanya utk `AddJsonFile`/log, atau tidak dibaca | args `dotnet <app>.dll --environment=<env>` | `ngenwal-be-saasidentityserver4client-manager`; 11 aplikasi `ConfigureHostConfiguration` di production-ngenwal |

Env var yang hanya dipakai `AddJsonFile` redundan: `Host.CreateDefaultBuilder(args)` sudah memuat
`appsettings.<--environment>.json`. Nama env mengikuti konvensi template folder, dan mount + key
Secret selalu `appsettings.<env runtime>.json`. Pola regex di params rule `dotnet_env_mode` (PB).

**d. Temuan berbasis source** (manifest yang sudah ada): `config_mount_path_mismatch` (mount ≠
direktori yang dibaca; disertai saran Deployment terkoreksi) dan `config_format_mismatch` (.NET
me-mount `.env` tanpa DotNetEnv/dotenv.net di `*.csproj`, atau non-.NET me-mount `appsettings`).
Rule berbasis manifest saja: `appsettings_env_mismatch` (env runtime ≠ appsettings yang di-mount).

## 6. Standar yang Ditegakkan

| Aspek | Standar | Sumber |
|---|---|---|
| Label pod | `app` = nama, `env` = prefix namespace, `project` = akhiran namespace, `role` = `front` (manager/apigateway) / `back` | data GitOps `main` (production-qoin 47/50, production-ngenwal 71/71) |
| Selector | `matchLabels` ⊆ label pod; selector lama dipertahankan (immutable) | Kubernetes |
| Replicas | production 2, lainnya 1 | pdb-production-standards |
| Secret config | `file-config-<app>` atau `file-config-<app>-<env>`, satu mount; key `.env` (Go/Rust/Node/Python) atau `appsettings.<env runtime>.json` (.NET) | skill file-config-setup |
| Path file config | direktori mount = tujuan `COPY … .env` / WORKDIR stage final Dockerfile (default `/`); Go `godotenv.Load()` relatif CWD | survei 199 Deployment (±94% cocok) |
| Environment .NET | mode dari source: `GetEnvironmentVariable("ASPNETCORE_ENVIRONMENT")` → env var; generic host → args `--environment`; env runtime = appsettings yang di-mount | analisa transactionconverter vs identityserver4client |
| Pull secret | `regcred` untuk image `loyaltolpi/…` (boleh via patch kustomization) | k8s-manifest-fix |
| Nodepool | collection PB `nodepool` per cluster × namespace × role (`label`, `value`); selalu nama pool presisi `cce-nodepool` (`nodetype` di hw-pro-p tidak presisi) | node cluster + GitOps |
| Service | selector memilih pod; `targetPort` bernama harus ada; port klien mengikuti konvensi namespace | Kubernetes + data GitOps |
| Kustomization | file deployment/service terdaftar di `resources` | Kustomize |

## 7. Data di PocketBase

| Collection | Isi | Kunci upsert | Akses |
|---|---|---|---|
| `build_q_anomaly_rules` | 57 rule (16 dipakai Manifest Suite): severity, active, `params` (termasuk pola `dotnet_env_mode`) | `rule_name` | baca login, tulis superuser |
| `build_q_manifest_templates` | template Deployment + Service per `infra × namespace × app_kind` | `name` | baca login, tulis superuser |
| `kube_cx`, `namespace` | mapping cluster ↔ namespace (+ `server` destination ArgoCD, `nodepools` inventaris) | `name` / `name+kube_cx` | lihat catatan keamanan |
| `nodepool` | nodeSelector standar per cluster × namespace × role, nama `<cluster>/<namespace>/<role>` (24 record) | `kube_cx+namespace+role` | baca login, tulis superuser |

Sumber kebenaran rule: `scripts/anomaly-rules-seed.json`; template: hasil `--manifest-template`
(seed lokal). Semua lewat `bq --tui-sync <seed>`.

## 8. CLI Reference

| Flag | Keterangan |
|---|---|
| `--e2e <repo> <ref>` | rantai e2e termasuk tahap Manifest; `--no-jev` untuk tanpa Jev |
| `--manifest-check <ns>` | `--export`, `--export-md PATH`, `--export-json PATH`, `--gitops-branch`, `--gitops-repo`, `--infra` |
| `--manifest-init <repo> <ns>` | `--out-dir DIR`, `--export-md PATH`, `--pr` (+ `--dry-run`), `--infra`, `--gitops-branch`, `--gitops-repo`; `<env>` saja tetap diterima |
| `--manifest-template <ns,…\|all>` | `--export-json PATH` (default `~/.build-q/manifest-templates-seed.json`), `--gitops-branch` (default main), `--infra` |
| `--manifest-verify <ns>` | `--export-md`, `--export-json`, `--gitops-branch` (default main), `--infra` |
| `--tui-sync <seed>` | upsert seed ke PB (rule, template, mapping) |
| `--tui-pull` | refresh katalog, mapping `kube_cx`, rule manifest |

Exit code: `--manifest-check` 0/1/2 (OK/FAIL/SKIP) · `--manifest-init` 1 bila manifest yang ada
punya temuan critical · `--manifest-verify` 1 bila ada diff · `--manifest-template` 2 bila tak ada template.

TUI: `/e2e REPO REF`, `/manifest-check NS [BRANCH]` (ekspor otomatis ke `~/.build-q/reports/`).
Web: tahap Manifest di 🔭 E2E, tombol 🧩 Namespace (unduh `.md`/`.json`).

## 9. Guardrails & Keamanan

- Read-only terhadap cluster: hanya `kubectl get … -o name`. Satu-satunya penulisan adalah
  `--manifest-init --pr` (eksplisit): branch + PR baru di repo GitOps, tanpa apply dan tanpa
  push ke branch tujuan; Secret tidak pernah ikut.
- Isi Secret tidak pernah dibaca; kerangka Secret berisi placeholder dan diberi peringatan jangan
  commit plaintext.
- Saran perbaikan file lama: diff minimal, urutan key asli, selector immutable dipertahankan,
  `port` Service tidak diubah, `targetPort` angka tidak ditebak.
- Label tracking ArgoCD (`app.kubernetes.io/instance`) & annotation Helm/ArgoCD tidak pernah
  tersalin dari sample/template.
- Mode web: credential (GitHub token, kubeconfig) per pengguna dari PocketBase, tanpa fallback
  ke credential server; lock simulasi mencegah dunia tiruan bocor ke analisa nyata.
- Template & seed berisi struktur infra internal → disimpan di PB / file lokal, **tidak** di repo.

## 10. Validasi

| Uji | Hasil |
|---|---|
| Unit & simulasi | 524 test; 23 skenario `--e2e --simulate` |
| Pola mode env .NET vs source (production-ngenwal, 50 aplikasi, tag ter-deploy) | 36 cocok · 14 beda konvensi tapi jalan · **0 berisiko** (setelah aturan "env var dipakai dlm logika") |
| Pola path file config vs Dockerfile (199 Deployment, 4 namespace) | ±94% cocok (.NET 71/74, Go 95/101, Node 7/7); sisanya manifest salah mount (dikonfirmasi ke kode) |
| `--manifest-check sandbox-qoin` | 11 aplikasi `appsettings_env_mismatch` (`--environment=sandbox` + `appsettings.production.json`) |
| `--manifest-init … --pr --dry-run` | kustomization hanya +2 baris resources, Secret tidak ikut |
| Scan 773 manifest (cce/ + k8s/) | temuan critical palsu ±300 → 19 asli setelah evaluasi kustomization |
| `--manifest-check develop-qoin` | 48 Deployment ±8 detik, 48 OK |
| `--manifest-verify` 4 namespace production (208 Deployment) | **0 diff**; match 78, match-nonstandard 116, unik 11, skip 3 |
| E2E nyata | `saas-be-loyalty-module` (targetPort grpc tanpa port bernama), `ngenwal-be-saasrecurringtransaction-module` (Service hilang), Jev nyata |

## 11. Batasan & Rencana

**Batasan**

- Patch kustomization strategic-merge / `path:` belum dievaluasi.
- Hanya Deployment & Service pertama per file; HPA/PDB belum dibuat oleh `--manifest-init`.
- Nodepool belum diperiksa sebagai temuan (hanya dipakai di saran).
- `imagePullSecrets` di ServiceAccount tidak dibaca.
- Path `k8s/` (SLS) tidak ada di mapping `kube_cx` → Secret dicek dari repo.
- Analisa source (bahasa, Dockerfile, mode env, DotNetEnv) hanya di `--manifest-init` (per aplikasi);
  `--manifest-check` / tahap Manifest `--e2e` belum menjalankan `config_mount_path_mismatch` /
  `config_format_mismatch`.
- Dockerfile dibaca dari root repo (`Dockerfile`, `build.Dockerfile`, `Dockerfile.production`);
  Go yang memuat `.env` dengan path absolut di kode (`godotenv.Load("/x/.env")`) belum dibaca.
- Port tambahan (mis. gRPC) belum dibaca (hanya `PORT` cicd.json).

**Rencana**

1. Baca port tambahan (gRPC) dari Dockerfile `EXPOSE` / `cicd.json` untuk `--manifest-init`.
2. Jalankan rule berbasis source di `--manifest-check` (Dockerfile per aplikasi, paralel).
3. PR perbaikan GitOps untuk temuan nyata: 11 `appsettings_env_mismatch` sandbox-qoin, mount path
   (`ngenwal-be-saasaccountlinkage-manager`, `plus-be-switchrintisservice-manager`,
   `ngenwal-be-messaging-manager`), format (`ngenwal-be-saasaltoservice-manager`, `pay-be-va*`),
   `nodetype` production-payout.
4. Saran HPA + PDB untuk production (pdb-production-standards).
5. `--manifest-init` di TUI & web; `--manifest-verify` terjadwal (CI) dengan ambang 0 diff.
6. Rule `nodepool_mismatch_cce` sebagai temuan.
7. Hardening akses PB `kube_cx`/`namespace`.
