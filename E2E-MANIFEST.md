# Tahap Manifest `bq --e2e` — Deployment ↔ Service ↔ Secret + Saran YAML

> Sejak **v0.2.2**. Read-only: tidak ada perintah yang mengubah cluster maupun repo.

`bq --e2e <repo> <ref>` menelusuri satu commit dari trigger sampai ArgoCD. Tahap
**Manifest** memeriksa apakah manifest aplikasi di repo GitOps sudah benar-benar bisa
melayani traffic:

- selector Deployment cocok dengan label pod, dan selector Service cocok dengan pod;
- `targetPort` Service mengarah ke port yang ada;
- Secret yang dirujuk pod **sudah ada** di namespace tujuan;
- file manifest ikut di-apply oleh kustomization/ArgoCD.

Bila ada yang belum sesuai, tahap ini memberi **saran YAML** yang benar. Manifest baru
disalin dari aplikasi tetangga di repo GitOps `main:cce/<namespace>`, sehingga mengikuti
konvensi nyata di folder tersebut. Semua rule dan nilai standarnya disimpan terpusat di
PocketBase.

---

## Daftar isi

1. [Cara pakai](#1-cara-pakai)
2. [Posisi tahap di alur e2e](#2-posisi-tahap-di-alur-e2e)
3. [Yang diperiksa](#3-yang-diperiksa)
4. [Evaluasi kustomization](#4-evaluasi-kustomization)
5. [Cek Secret di cluster](#5-cek-secret-di-cluster)
6. [Status tahap & titik putus](#6-status-tahap--titik-putus)
7. [Saran YAML](#7-saran-yaml)
8. [Penilaian Jev](#8-penilaian-jev)
9. [Rule terpusat di PocketBase](#9-rule-terpusat-di-pocketbase)
10. [Mapping kube context (`kube_cx`)](#10-mapping-kube-context-kube_cx)
11. [Format data (web & integrasi)](#11-format-data-web--integrasi)
12. [Simulasi](#12-simulasi)
13. [Uji satu namespace — `bq --manifest-check`](#13-uji-satu-namespace--bq---manifest-check)
14. [Troubleshooting](#14-troubleshooting)
15. [Batasan](#15-batasan)

---

## 1. Cara pakai

```bash
bq --e2e pay-be-topup-manager develop           # CLI — Jev aktif bila ada temuan
bq --e2e pay-be-topup-manager develop --no-jev  # tanpa Jev (offline, tanpa biaya LLM)
bq --e2e --simulate manifest-selector-mismatch  # contoh laporan tanpa cluster/jaringan
```

- **TUI** (`bq --tui`): `/e2e REPO REF`. Setiap saran YAML tampil sebagai panel
  ber-highlight.
- **Web** (`bq --serve PORT`): tombol **🔭 E2E** atau `/e2e REPO REF`. Tahap *Manifest*
  ada di rantai tahap, dan saran YAML tampil di kartu tahapnya. Credential (GitHub
  token, kubeconfig) diambil dari akun PocketBase pengguna yang login, bukan dari server.

Yang dibutuhkan: `bq auth login`, `GITHUB_TOKEN` untuk membaca repo source dan GitOps,
serta (opsional) kubeconfig yang memuat context `hw-*` agar Secret bisa dicek di
cluster. Tanpa akses cluster, pemeriksaan tetap berjalan dari repo, dan hasilnya ditandai
"belum bisa dipastikan".

## 2. Posisi tahap di alur e2e

```
Source → Trigger → Pipeline → GitOps → Manifest → ArgoCD
                                 │         ▲          │
                                 │         └──────────┘  destination Application
                                 └─ YAML deployment      (context cluster paling pasti)
```

- Tahap ini memakai YAML deployment yang sudah dibaca tahap **GitOps** (branch sesuai
  ENV: `develop`/`staging`/`sandbox`, atau `main` untuk tag `v*`).
- Tahap **ArgoCD** dibaca lebih dulu (tetapi tampil sesudah Manifest) supaya
  `spec.destination` Application bisa dipakai untuk memilih cluster tempat Secret dicek.
- Dilewati untuk aplikasi FE (`web/…`, Docker Compose). Bila tahap GitOps belum
  membaca YAML (mis. PR GitOps masih open), tahap ini juga dilewati.

## 3. Yang diperiksa

Rule dengan implementasi pemeriksaan. Severity bisa diubah di PocketBase (lihat §9).

| Rule | Severity default | Yang diperiksa | Saran YAML |
|---|---|---|---|
| `manifest_not_in_kustomization` | critical | File `<app>_deployment.yaml` / `_services.yaml` ada di folder, tetapi tidak tercantum di `resources:` kustomization.yaml, sehingga tidak pernah di-apply ArgoCD | — (tambahkan nama file ke `resources`) |
| `deploy_selector_template_mismatch` | critical | `spec.selector.matchLabels` tidak termuat di `template.metadata.labels` | Deployment |
| `missing_standard_labels` | warn | Label pod tidak memuat `app`, `env`, `project`, `role` | Deployment (selector lama dipertahankan) |
| `missing_image_pullsecret` | critical | Image privat (`loyaltolpi/…`) tanpa `imagePullSecrets: regcred` **setelah** patch kustomization | Deployment |
| `missing_file_config_volume` | warn | Backend (`-be-`) tanpa volume Secret `file-config-<app>` maupun `file-config-<app>-<env>`. Config kemungkinan masih *baked* di image (pola lama) | Deployment (+ kerangka Secret bila belum ada) |
| `secret_missing` | critical | Secret yang **dirujuk** pod (volume, `envFrom.secretRef`, `env.valueFrom.secretKeyRef`, `regcred`) tidak ada di namespace | kerangka Secret |
| `service_missing` | critical | Deployment membuka `containerPort` tetapi tidak punya Service (`<app>_services.yaml` / `_service.yaml`, atau Service di file yang sama) | Service baru dari sample |
| `service_namespace_mismatch` | critical | Namespace Service ≠ Deployment (hanya bila kustomization tidak menetapkan `namespace:`) | Service |
| `service_selector_no_match` | critical | `Service.spec.selector` tidak cocok dengan label pod sehingga endpoints kosong | Service |
| `service_port_targetport_mismatch` | critical / warn | `targetPort` **bernama** yang tidak ada di `containerPort` → critical (endpoint kosong). `targetPort` **angka** yang tidak dideklarasikan → warn saja, karena tetap jalan bila aplikasi listen di port itu | Service atau Deployment (lihat §7.2) |

Dua rule lain hanya dipakai `params`-nya dan tidak diperiksa sebagai temuan:

| Rule | Dipakai untuk |
|---|---|
| `nodepool_mismatch_cce` | `params.by_namespace` → nodepool CCE (`<prefix>-manager` / `<prefix>-service`) di saran Deployment |
| `manifest_sample_source` | sumber sample YAML (`params.branch`, `params.infra`, `params.max_candidates`) |

Catatan pemeriksaan:

- Deployment tanpa `containerPort` (worker) tidak wajib punya Service; laporan hanya
  memberi catatan.
- Image publik (mis. `nginx:stable`, `istio/proxyv2`) tidak wajib `regcred`.
- Hanya Secret yang **benar-benar dirujuk** pod yang dicek keberadaannya. Aplikasi
  dengan config baked tidak dilaporkan `secret_missing`.
- Nama Secret config dengan akhiran env (`file-config-<app>-<env>`, mis.
  `file-config-plus-be-rusttransaction-module-develop`) dikenali sebagai Secret config.
  Bila Secret seperti itu sudah ada di cluster tetapi belum di-mount, saran mount memakai
  nama yang sudah ada, dan kerangka Secret tidak disarankan lagi.

## 4. Evaluasi kustomization

Manifest dinilai seperti hasil akhir `kustomize build` di folder `cce/<ns>/` atau `k8s/<ns>/`:

- **Namespace efektif** = `namespace:` di kustomization.yaml, lalu `metadata.namespace`
  Deployment, lalu `destination.namespace` Application ArgoCD, lalu nama folder. Contoh nyata:
  folder `cce/staging-ngenwal` di-deploy ke namespace **`production-ngenwal`**, dan laporan
  menulis `namespace efektif: production-ngenwal (folder staging-ngenwal)`.
- **Patch JSON6902** di `patches:` / `patchesJson6902:` diterapkan sebelum pemeriksaan.
  Operasi yang didukung: `add`, `replace`, `remove` (path JSON pointer, index list, `-`).
  Target dicocokkan dengan `kind`, `name` (regex), dan `labelSelector` (`k=v,…`). Contoh
  nyata: semua folder `cce/production-*` menyuntikkan `imagePullSecrets: regcred` lewat
  patch, sehingga Deployment tanpa `regcred` di file mentah tidak dilaporkan.
- **`resources:`**: file deployment/service harus terdaftar di sana.
- Patch *strategic merge* dan patch dari file (`path:`) **tidak** dievaluasi. Laporan
  mencatatnya di detail tahap.

## 5. Cek Secret di cluster

Urutan pemilihan context cluster:

1. **`spec.destination` Application ArgoCD**: `server` dicocokkan dengan field `server` di
   PocketBase `kube_cx`, atau `name` dengan nama/cluster `kube_cx`.
   `https://kubernetes.default.svc` / `in-cluster` = cluster ArgoCD (`ARGOCD_KUBE_CONTEXT`,
   default `hw-pro-p`).
2. **Mapping namespace → context** dari PocketBase (`namespace` → `kube_cx`), dengan fallback
   prefix `develop-*`/`sandbox-*` → `hw-dev` dan `staging-*` → `hw-pro-q`.
3. **Namespace ambigu** (ada di >1 context, mis. `develop-payout` di `hw-dev` & `hw-pro-p`):
   tiap kandidat dicoba, lalu dipakai cluster yang benar-benar menjalankan Deployment tersebut.
4. Path **`k8s/` (SLS)** tidak memakai mapping `kube_cx` (isinya hanya cluster CCE `hw-*`).
   Kecuali destination ArgoCD-nya dikenal, Secret dicek dari repo.

Perintah yang dijalankan (read-only, **isi Secret tidak pernah dibaca**):

```bash
kubectl --context <ctx> -n <ns> get secrets -o name
kubectl --context <ctx> -n <ns> get deployments -o name   # hanya untuk namespace ambigu
```

Bila cluster tidak terjangkau, keberadaan Secret dicari di repo GitOps (folder aplikasi dan
subfolder `file-config/`): file bernama `[file-config-]<app>[-<env>][_secret|_secret_sops].yaml`,
misalnya `file-config-<app>_secret_sops.yaml` atau `file-config/<app>-develop.yaml`.
Pencocokannya persis; file lain yang sekadar mengandung nama aplikasi (mis.
`<app>_deployment.yaml`) tidak dihitung.

| Status secret | Arti |
|---|---|
| `ada` | ada di namespace (cluster) |
| `tidak-ada` | tidak ada di namespace → `secret_missing` (critical) |
| `repo` | cluster tak terjangkau; ada file secret di repo GitOps (indikasi) |
| `tidak-diketahui` | cluster tak terjangkau dan tidak ada indikasi di repo |

## 6. Status tahap & titik putus

| Kondisi | Status tahap | Dampak laporan |
|---|---|---|
| Ada temuan **critical** | ❌ FAIL | titik putus (bila tidak ada tahap gagal sebelumnya); exit code 1 |
| Tidak ada critical, tetapi secret `repo`/`tidak-diketahui` | ⏭️ SKIP | verdict *incomplete* ("Analisa tidak lengkap"); exit code 2 |
| Hanya temuan **warn** | ✅ OK | ringkasan diberi akhiran `· saran standar: <rule>`; bukan titik putus |
| Tidak ada temuan | ✅ OK | "selector Deployment↔Service cocok, secret tersedia" |
| Deployment belum ada di GitOps | ❌ FAIL | saran Deployment + Service + Secret lengkap |

Contoh laporan CLI:

```
❌ Manifest (deploy↔svc↔secret): 1 masalah — service_port_targetport_mismatch
     • rule standar: 12 dari build_q_anomaly_rules (pb)
     • secret dicek di cluster hw-dev/develop-saas (mapping develop-saas → hw-dev)
     • Service: cce/develop-saas/saas-be-loyalty-module_services.yaml
     • secret file-config-saas-be-loyalty-module: ada
     • secret regcred: ada
     • [critical] service_port_targetport_mismatch: targetPort bernama `grpc` tidak ada di containerPort (http) — endpoint kosong
     • Jev: siap melayani traffic 5%, bereskan dulu: service_port_targetport_mismatch
     💡 Arahkan targetPort ke nama/nomor containerPort (standar: name http).

     📝 Saran YAML — Service (selector/namespace/targetPort disesuaikan dgn Deployment)
        # cce/develop-saas/saas-be-loyalty-module_services.yaml
        ...
          - name: grpc
            port: 9541
            protocol: TCP
            targetPort: 9541
```

## 7. Saran YAML

Setiap saran berisi judul, path tujuan di repo GitOps, dan YAML lengkapnya. Saran
**tidak pernah diterapkan otomatis**: simpan ke repo GitOps lewat PR seperti biasa.

### 7.1 Manifest baru — dari sample GitOps `main:cce/<namespace>`

Dipakai saat Deployment belum ada di GitOps, atau Deployment ada tetapi Service-nya belum ada.

**Memilih sample** (`params` rule `manifest_sample_source`, default `branch=main`,
`infra=cce`, `max_candidates=5`):

1. Daftar `*_deployment.yaml` di `<branch>:<infra>/<folder namespace>`.
2. Kandidat diurutkan: akhiran sama (`-manager`/`-module`/…), lalu project sama
   (`pay`/`ngenwal`/…), lalu jenis sama (`be`/`fe`), lalu panjang nama terdekat.
3. Sample dipakai bila punya Service, terdaftar di kustomization, dan **lolos semua rule
   critical**, agar kesalahan tidak ikut tersalin.

**Penyesuaian sample** ke aplikasi target:

- Nama aplikasi sample diganti nama target di seluruh manifest (nama, container,
  `secretName file-config-<app>`, `<app>.dll`, …).
- Dibuang: `status`, `uid`/`resourceVersion`/`creationTimestamp`/…, annotation
  `argocd.argoproj.io/*`, `meta.helm.sh/*`, `deployment.kubernetes.io/*`,
  `kubectl.kubernetes.io/*`, label `app.kubernetes.io/managed-by: Helm`, label
  **`app.kubernetes.io/instance`** (label tracking ArgoCD; bila tersalin, resource baru
  bisa dianggap milik Application lain), dan `clusterIP`/`clusterIPs` Service.
- Label standar target: `app`, `project` (prefix nama), `role` (`front` untuk
  `-manager`/`-apigateway`, selain itu `back`), dan `env` mengikuti sample.
- Image target `<org>/<image>:<tag>` dari tahap Source, `containerPort` dari `PORT`
  cicd.json (default 80).
- Stack berbeda dengan sample: `.NET` → `args: [dotnet, <app>.dll, --environment=<env>]` +
  mount `/app/appsettings.<env>.json`; Go → args dihapus + mount `/app/.env`.
- Role berbeda dengan sample: nodepool CCE diganti (`…-manager` ↔ `…-service`).
  Target `k8s/` (SLS): nodepool CCE dibuang, jadi set `nodetype` secara manual.
- `regcred` hanya ditambahkan bila tidak disuntikkan patch kustomization folder tujuan.
- Konvensi sample lain dipertahankan (mis. `tz-config` `/etc/localtime`, `strategy`,
  `resources`, `replicas`, `progressDeadlineSeconds`).
- **Service** baru: struktur sample (`type`, `sessionAffinity`, `ipFamilies`, …), selector =
  `matchLabels` Deployment, ports dari `containerPort` target (`targetPort` = nama port).

Bila tidak ada sample yang memenuhi syarat, dipakai **generator standar**:
`replicas` 2 (production) / 1, `RollingUpdate maxSurge 1 maxUnavailable 0`,
resources `30m/100Mi` → `500m/256Mi`, port `http`, `regcred`, volume `file-config-<app>`
(`defaultMode 420`), dan nodepool dari `params.by_namespace`.

### 7.2 Perbaikan file yang sudah ada

- **Diff minimal**: saran dibuat dari isi file mentah di GitOps, dan urutan key aslinya
  dipertahankan. Field hasil patch kustomization tidak ditambahkan ke file.
- **Selector immutable**: `matchLabels` lama dipertahankan selama masih termuat di label pod.
  Label standar ditambahkan ke metadata dan template saja. Bila `matchLabels` memang harus
  berubah, Deployment perlu dihapus lalu dibuat ulang (disebut di hint).
- **`targetPort` bernama yang tidak ada**:
  - nomor `port` Service dideklarasikan sebagai `containerPort` → `targetPort` diubah ke
    nomor itu (contoh nyata `saas-be-loyalty-module`: `grpc 9541 → targetPort: 9541`);
  - tidak dideklarasikan → Service **tidak diubah**, tetapi saran Deployment menambah
    `containerPort` bernama tersebut, dengan catatan untuk memastikan nomornya.
- **`targetPort` angka yang tidak dideklarasikan**: hanya warn, tanpa saran perubahan.
- `port` Service (yang dipakai klien) tidak pernah diubah.

### 7.3 Kerangka Secret

```yaml
apiVersion: v1
kind: Secret
type: Opaque
metadata:
  name: file-config-<app>
  namespace: <ns>
  labels: {app: <app>, env: <env>}
stringData:
  .env: <isi dari .env.<env>>          # .NET: appsettings.<env>.json
```

Isi dari file config aplikasi, apply ke namespace tujuan, dan **jangan commit plaintext**
(enkripsi SOPS `*_secret_sops.yaml`; lihat skill `secret-from-file` / `file-config-setup`).

## 8. Penilaian Jev

Bila tahap Manifest punya temuan, Jev (System One) dipanggil **satu kali**:

- **Dikirim**: nama deployment, namespace, dan daftar temuan (`rule`, `severity`, `message`).
  YAML dan isi Secret tidak dikirim.
- **Ditanya**: `deploy_ready` (seberapa siap pasangan Deployment/Service/Secret melayani
  traffic) dan `fix_first` (rule yang paling perlu dibereskan dulu).
- **Hasil**: baris detail `Jev: siap melayani traffic N%, bereskan dulu: <rule>` dan
  `stages[].data.jev`.
- **Kapan aktif**: TUI `/e2e` dan web selalu aktif; CLI aktif kecuali memakai `--no-jev`.
  Provider sama dengan Jev planner (default `typesafe`, key `TYPESAFE_API_KEY`).
- **Fail-safe**: bila key/provider/jaringan bermasalah, laporan tetap lengkap dengan catatan
  `Jev dilewati: …`. Status tahap selalu ditentukan oleh rule deterministik, bukan oleh Jev.

## 9. Rule terpusat di PocketBase

Collection **`build_q_anomaly_rules`** (PocketBase devops `https://cicd-hw.qoin.id/devops`):

| Field | Tipe | Keterangan |
|---|---|---|
| `rule_name` | text, unik | kunci rule |
| `resource_type` | text | Deployment / Service / Secret / Kustomization / Template |
| `severity` | select | `critical` \| `warn` \| `info` |
| `category` | text | availability, network, compliance, config_drift, … |
| `description`, `detect_hint`, `fix_hint` | text | penjelasan & saran (fix_hint tampil sebagai 💡) |
| `source` | text | asal rule (skill/fitur) |
| `params` | json | nilai standar (lihat tabel di bawah) |
| `active` | bool | `false` → rule dimatikan |
| `order` | number | urutan |

Akses: baca = pengguna login (`bq auth login`), tulis = superuser.

**Pemuatan**: PocketBase → cache `~/.build-q/.anomaly-rules-cache.json` (TTL
`BUILD_Q_TUI_CACHE_TTL`, default 1 jam) → default bawaan bila PB tidak terjangkau dan cache
kosong. Baris `rule standar: N dari build_q_anomaly_rules (pb|cache|stale-cache|default)`
menunjukkan sumbernya. Refresh manual: `bq --tui-pull` atau `/refresh` di TUI.

**Penggabungan**: record PB menimpa `severity`, `category`, `description`, `fix_hint`.
`params` digabung per key (key PB menang), dan `active: false` membuang rule. Record PB
untuk rule tanpa implementasi pemeriksaan diabaikan oleh tahap ini.

**Mengubah rule**: edit `scripts/anomaly-rules-seed.json`, lalu:

```bash
bq --tui-sync scripts/anomaly-rules-seed.json   # upsert by rule_name + bersihkan cache
```

**`params` yang dipakai**:

| Rule | Params (default) |
|---|---|
| `service_missing` | `suffixes: ["_services.yaml", "_service.yaml"]` |
| `missing_standard_labels` | `required_labels: [app, env, project, role]` |
| `missing_image_pullsecret` | `name: regcred`, `image_prefixes: ["loyaltolpi/", "docker.io/loyaltolpi/"]` |
| `missing_file_config_volume` | `applies_to: ["-be-"]`, `secret_prefix: file-config-`, `volume_name: file-config-volume`, `mount_go: /app/.env`, `key_go: .env`, `mount_dotnet: /app/appsettings.{env}.json`, `key_dotnet: appsettings.{env}.json` |
| `nodepool_mismatch_cce` | `label: cce.cloud.com/cce-nodepool`, `by_namespace: {develop-qoin: develop-qoin, develop-ngenwal: develop-qoincash, production-payout: production-payout-nodepool, production-qoin: production-nodepool, production-ngenwal: production-ue-nodepool, …}` |
| `manifest_sample_source` | `branch: main`, `infra: cce`, `max_candidates: 5` |

Contoh: agar sample diambil dari branch lain, ubah `params.branch` rule
`manifest_sample_source` di seed, lalu jalankan `bq --tui-sync`.

## 10. Mapping kube context (`kube_cx`)

Mapping context ↔ namespace juga terpusat di PocketBase (sejak v0.2.1):

| Collection | Field |
|---|---|
| `kube_cx` | `name` (hw-dev, hw-pro-p, hw-pro-q, hw-pro-u), `cluster`, `server`, `environment`, `note` |
| `namespace` | `name`, `description`, `kube_cx` (relation) |

Mapping ini dipakai di tiga tempat:

- **Tahap Manifest**: memilih cluster untuk cek Secret (§5). Field `server` dicocokkan
  dengan `destination.server` ArgoCD.
- **Jev planner** (`--tui` / `--serve`): param `kube_context` terisi otomatis dari namespace.
  Bila ambigu, Jev memilih di antara kandidatnya saja.
- **MCP** resource `bq://kube-contexts` → `{source, contexts, namespaces}`.

Cache: `~/.build-q/.kube-cx-cache.json` (format v2 dengan `server`; cache format lama
otomatis diambil ulang). Refresh manual: `bq --tui-pull` atau `/refresh` di TUI. Data diubah
lewat bagian `kube_cx` / `namespace` di `scripts/build-q-seed.json` + `bq --tui-sync`.

## 11. Format data (web & integrasi)

Laporan `E2EReport.to_dict()` (payload SSE `report` di web) untuk tahap ini:

```jsonc
{
  "key": "manifest",
  "title": "Manifest (deploy↔svc↔secret)",
  "status": "ok | fail | skip",
  "summary": "...",
  "details": ["rule standar: 12 dari build_q_anomaly_rules (pb)", "secret file-config-x: ada", "..."],
  "hints": ["..."],
  "applicable": true,
  "data": {
    "findings": [{"rule_name": "service_missing", "severity": "critical", "category": "network",
                  "message": "...", "fix_hint": "..."}],
    "suggestions": [{"title": "Service baru (dari sample main:cce/...)",
                     "path": "cce/develop-qoin/x_services.yaml", "yaml": "apiVersion: v1\n..."}],
    "jev": {"deploy_ready": 0.05, "fix_first": "service_missing"}
  }
}
```

Event SSE `stage` mengirim `{"name": "manifest"}` saat tahap ini berjalan (urutan event
`argocd` lalu `manifest`, karena destination ArgoCD dibaca lebih dulu).

## 12. Simulasi

`bq --e2e --simulate` menjalankan 23 skenario tanpa jaringan. Lima di antaranya untuk tahap ini:

| Skenario | Titik putus | Verdict |
|---|---|---|
| `manifest-selector-mismatch` | manifest | broken (saran Service `role` dibetulkan) |
| `manifest-service-missing` | manifest | broken (Service baru dari sample tetangga) |
| `manifest-not-in-kustomization` | manifest | broken |
| `manifest-secret-missing` | manifest | broken (kerangka Secret) |
| `manifest-baked-config` | — | healthy (saran standar, bukan titik putus) |

Skenario `gitops-not-found` menunjukkan saran Deployment + Service + Secret lengkap dari sample.

## 13. Uji satu namespace — `bq --manifest-check`

> Sejak **v0.2.3**.

Pemeriksaan yang sama dengan tahap Manifest, dijalankan sekaligus untuk **semua
Deployment** di folder `<infra>/<namespace>/` repo GitOps, tanpa tahap pipeline/ArgoCD
yang memang per commit. Read-only.

```bash
bq --manifest-check develop-qoin                        # ringkasan di terminal
bq --manifest-check develop-qoin --export               # + manifest-develop-qoin-<waktu>.md & .json
bq --manifest-check production-qoin --export-md laporan.md --export-json laporan.json
bq --manifest-check staging-ngenwal --gitops-branch staging   # override branch GitOps
bq --manifest-check production-saas --infra k8s          # folder k8s/ (SLS)
```

- **TUI**: `/manifest-check NAMESPACE [BRANCH]` menampilkan tabel Deployment bermasalah, dan
  laporan `.md` + `.json` otomatis diekspor ke `~/.build-q/reports/`.
- **Web**: tombol **🧩 Namespace** atau `/manifest-check NAMESPACE [BRANCH]`. Progres tampil
  langsung, tiap Deployment punya kartu detail + saran YAML, dan tersedia tombol
  **⬇️ Laporan .md** / **⬇️ .json**. Credential diambil dari akun PocketBase pengguna
  (`POST /api/manifest-check` → SSE `/api/manifest-check/stream/<id>`: `progress`, `report`,
  `error`, `done`).

Cara kerja:

| Aspek | Perilaku |
|---|---|
| Branch | Mengikuti ENV namespace seperti tahap GitOps: `develop-*`/`staging-*`/`sandbox-*` → branch yang sama, lainnya → `GITOPS_BRANCH` (default `main`). Override: `--gitops-branch` |
| Repo | `GITOPS_REPO` (override `--gitops-repo`), dibaca lewat GitHub API dengan 8 permintaan paralel |
| Namespace | `namespace:` kustomization folder (mis. folder `staging-ngenwal` → `production-ngenwal`) |
| Cluster | Destination Application ArgoCD untuk folder tsb → mapping `kube_cx` → untuk namespace ambigu, cluster yang menjalankan Deployment folder ini paling banyak |
| Secret | `kubectl get secrets -o name` **sekali** per namespace; isi tidak dibaca |
| Sample | Sama dengan §7.1, di-cache lintas aplikasi |
| Status per Deployment | Sama dengan §6: ✅ OK (boleh dengan saran warn) · ❌ FAIL (ada critical) · ⏭️ SKIP (Secret belum terverifikasi / error baca) |

Exit code: `0` semua OK · `1` ada FAIL · `2` ada SKIP (tanpa FAIL). Cocok untuk CI, mis.
`bq --manifest-check production-qoin --export-json hasil.json || echo "perlu ditinjau"`.

**Isi laporan Markdown**: tabel identitas (repo/branch/folder, waktu, versi bq, sumber Secret,
rule, sample), catatan analisa (pemilihan cluster, namespace efektif), ringkasan status,
temuan per rule, daftar semua Deployment (status, critical, saran), lalu detail per
Deployment bermasalah: temuan + 💡 saran, status Secret, catatan, dan saran YAML (bagian
`<details>` yang bisa dilipat).

**Isi laporan JSON** (`ScanReport.to_dict()`):

```jsonc
{
  "namespace": "develop-qoin", "folder": "cce/develop-qoin",
  "gitops_repo": "Qoin-Digital-Indonesia/gitops", "branch": "develop",
  "secret_source": "cluster hw-dev/develop-qoin (48 secret; destination ArgoCD)",
  "rules_source": "pb", "rules_count": 12, "sample_source": "main:cce/develop-qoin",
  "generated_at": "2026-10-22T19:48:03+07:00", "version": "<versi bq>",
  "details": ["Application ArgoCD develop-qoin → destination https://10.2.20.121:5443"],
  "totals": {"ok": 48, "fail": 0, "skip": 0, "total": 48},
  "rule_counts": [{"rule_name": "missing_standard_labels", "severity": "warn", "count": 1}],
  "exit_code": 0,
  "items": [{"app": "plus-apigateway", "status": "ok",
             "deploy_path": "cce/develop-qoin/plus-apigateway_deployment.yaml",
             "service_path": "cce/develop-qoin/plus-apigateway_services.yaml",
             "findings": [{"rule_name": "missing_standard_labels", "severity": "warn", "...": "..."}],
             "secrets": {"regcred": "ada"}, "notes": [], "suggestions": [{"title": "...", "path": "...", "yaml": "..."}],
             "error": ""}]
}
```

Contoh nyata `develop-qoin` (branch `develop`): 48 Deployment diperiksa dalam ±8 detik, 48 ✅ OK,
dengan satu saran standar (label `env`/`project` pada `plus-apigateway`). Cluster dipilih dari
destination Application ArgoCD `develop-qoin` (`hw-dev`).

## 14. Troubleshooting

| Gejala | Penyebab & solusi |
|---|---|
| `selector cocok; secret belum bisa dipastikan di cluster` | Cluster tidak terjangkau (VPN) atau kubeconfig tanpa context `hw-*`. Hubungkan VPN, atau pakai kubeconfig read-only: `bq auth get KUBECONFIG_READONLY_HW_B64 > ~/.kube/config-readonly-hw` lalu `export KUBECONFIG=~/.kube/config-readonly-hw`. Di web, kubeconfig diambil dari credential PocketBase pengguna |
| `context untuk <ns> tidak diketahui` | Namespace belum ada di PB `namespace`. Tambahkan di `scripts/build-q-seed.json` → `bq --tui-sync` |
| `namespace efektif: X (folder Y)` | Normal. kustomization menetapkan `namespace:` berbeda dari nama folder |
| Saran memakai `generator standar (sample tidak ditemukan)` | Folder `main:cce/<ns>` tidak ada atau tidak ada tetangga yang lolos rule critical. Cek `params` rule `manifest_sample_source` |
| `patch strategic-merge tidak dievaluasi` | Patch kustomization jenis ini dilewati. Bila memengaruhi label/secret/regcred, periksa manual |
| `rule standar: … (default)` | PB/collection tidak terjangkau. Rule bawaan dipakai; cek `bq --doctor` / `bq auth status` |
| Temuan yang memang disengaja (mis. label non-standar untuk komponen infra) | Turunkan `severity` atau set `active: false` di seed → `bq --tui-sync` |
| Jev tidak muncul | Tidak ada temuan, memakai `--no-jev`, atau `TYPESAFE_API_KEY` kosong (detail: `Jev dilewati: …`) |

## 15. Batasan

- Hanya Deployment dan Service **pertama** di file yang dinilai.
- Patch *strategic merge* dan patch dari file (`path:`) tidak dievaluasi.
- Isi Secret tidak dibaca, sehingga kecocokan key (mis. `appsettings.<env>.json` vs
  `DOTNET_ENVIRONMENT`) tidak diperiksa. Gunakan skill `secret-from-file` / `file-config-setup`.
- Nodepool hanya dipakai untuk saran; Deployment dengan nodepool salah belum dilaporkan.
- `imagePullSecrets` pada ServiceAccount tidak dibaca, sehingga Deployment yang mendapat
  `regcred` dari ServiceAccount bisa dilaporkan `missing_image_pullsecret`. Matikan atau
  sesuaikan rule bila pola ini dipakai.
- Path `k8s/` (SLS): Secret dicek dari repo kecuali destination ArgoCD dikenal di `kube_cx`.
  Sample tetap diambil dari `cce`, dengan nodepool CCE dibuang.
