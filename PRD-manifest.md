# PRD: Manifest GitOps — Cek, Saran, Template Standar & Uji (`bq` Manifest Suite)

> **Version:** 1.0
> **Last Updated:** 2026-10-22
> **Rilis:** tahap Manifest `--e2e` (v0.2.2) · `--manifest-check` (v0.2.3) · `--manifest-init`, `--manifest-template`, `--manifest-verify` (v0.2.4)
> **Modul:** `build_q/manifest_check.py` (rule, cek, generator, template) · `build_q/manifest_scan.py` (`--manifest-check`) · `build_q/manifest_init.py` (`--manifest-init`, `--manifest-verify`) · `build_q/manifest_template.py` (`--manifest-template`) · `build_q/e2e.py` (tahap Manifest, IO, pemilihan sample) · `build_q/kube_contexts.py` (mapping cluster)
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
3. Manifest sudah ada → hasil `check()` + saran perbaikan (tidak pernah menimpa).
4. Belum ada → template PB → sample tetangga → generator; kerangka Secret hanya untuk Secret yang
   belum ada di cluster; baris `resources` kustomization atau `kustomization.yaml` baru.
5. Output: teks, `--out-dir` (struktur path GitOps), `--export-md`.

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

## 6. Standar yang Ditegakkan

| Aspek | Standar | Sumber |
|---|---|---|
| Label pod | `app` = nama, `env` = prefix namespace, `project` = akhiran namespace, `role` = `front` (manager/apigateway) / `back` | data GitOps `main` (production-qoin 47/50, production-ngenwal 71/71) |
| Selector | `matchLabels` ⊆ label pod; selector lama dipertahankan (immutable) | Kubernetes |
| Replicas | production 2, lainnya 1 | pdb-production-standards |
| Secret config | `file-config-<app>` atau `file-config-<app>-<env>`, satu mount (`/app/.env` / `appsettings.<env>.json`) | skill file-config-setup |
| Pull secret | `regcred` untuk image `loyaltolpi/…` (boleh via patch kustomization) | k8s-manifest-fix |
| Nodepool CCE | `<prefix>-manager` / `<prefix>-service` per namespace (`params.by_namespace`) | NODEPOOL |
| Service | selector memilih pod; `targetPort` bernama harus ada; port klien mengikuti konvensi namespace | Kubernetes + data GitOps |
| Kustomization | file deployment/service terdaftar di `resources` | Kustomize |

## 7. Data di PocketBase

| Collection | Isi | Kunci upsert | Akses |
|---|---|---|---|
| `build_q_anomaly_rules` | 53 rule (12 dipakai Manifest Suite): severity, active, `params` | `rule_name` | baca login, tulis superuser |
| `build_q_manifest_templates` | template Deployment + Service per `infra × namespace × app_kind` | `name` | baca login, tulis superuser |
| `kube_cx`, `namespace` | mapping cluster ↔ namespace (+ `server` untuk destination ArgoCD) | `name` / `name+kube_cx` | lihat catatan keamanan |

Sumber kebenaran rule: `scripts/anomaly-rules-seed.json`; template: hasil `--manifest-template`
(seed lokal). Semua lewat `bq --tui-sync <seed>`.

## 8. CLI Reference

| Flag | Keterangan |
|---|---|
| `--e2e <repo> <ref>` | rantai e2e termasuk tahap Manifest; `--no-jev` untuk tanpa Jev |
| `--manifest-check <ns>` | `--export`, `--export-md PATH`, `--export-json PATH`, `--gitops-branch`, `--gitops-repo`, `--infra` |
| `--manifest-init <repo> <ns>` | `--out-dir DIR`, `--export-md PATH`, `--infra`, `--gitops-branch`, `--gitops-repo`; `<env>` saja tetap diterima |
| `--manifest-template <ns,…\|all>` | `--export-json PATH` (default `~/.build-q/manifest-templates-seed.json`), `--gitops-branch` (default main), `--infra` |
| `--manifest-verify <ns>` | `--export-md`, `--export-json`, `--gitops-branch` (default main), `--infra` |
| `--tui-sync <seed>` | upsert seed ke PB (rule, template, mapping) |
| `--tui-pull` | refresh katalog, mapping `kube_cx`, rule manifest |

Exit code: `--manifest-check` 0/1/2 (OK/FAIL/SKIP) · `--manifest-init` 1 bila manifest yang ada
punya temuan critical · `--manifest-verify` 1 bila ada diff · `--manifest-template` 2 bila tak ada template.

TUI: `/e2e REPO REF`, `/manifest-check NS [BRANCH]` (ekspor otomatis ke `~/.build-q/reports/`).
Web: tahap Manifest di 🔭 E2E, tombol 🧩 Namespace (unduh `.md`/`.json`).

## 9. Guardrails & Keamanan

- Read-only: hanya GitHub API GET, `kubectl get … -o name`, dan baca PB. Tidak ada apply/commit/PR.
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
| Unit & simulasi | 484 test; 23 skenario `--e2e --simulate` |
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
- Data khas aplikasi di `--manifest-init` nyata terbatas pada `cicd.json` (port tunggal `PORT`,
  `STACK`); port tambahan (mis. gRPC) & path mount dari `WORKDIR` Dockerfile belum dibaca.

**Rencana**

1. Baca `WORKDIR`/port tambahan dari Dockerfile & `cicd.json` untuk `--manifest-init`.
2. Saran HPA + PDB untuk production (pdb-production-standards).
3. `--manifest-init` di TUI & web; `--manifest-verify` terjadwal (CI) dengan ambang 0 diff.
4. Rule `nodepool_mismatch_cce` sebagai temuan.
5. Hardening akses PB `kube_cx`/`namespace`.
