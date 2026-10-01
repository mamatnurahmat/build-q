# PRD: `bq --bootstrap-k8s` — Bootstrap Manifest K8s ke GitOps

> **Version:** 1.0  
> **Last Updated:** 2026-10-01  
> **Module:** [`build_q/bootstrap.py`](file:///Users/mamatnurahmat/build-q/build_q/bootstrap.py)  
> **CLI Entry:** [`build_q/cli.py`](file:///Users/mamatnurahmat/build-q/build_q/cli.py#L299-L378)  
> **Templates:** [`scripts/gist-bootstrap-k8s/`](file:///Users/mamatnurahmat/build-q/scripts/gist-bootstrap-k8s)

---

## 1. Ringkasan

`bq --bootstrap-k8s` adalah fitur **one-shot provisioning** yang men-generate 3 manifest Kubernetes (Secret, Deployment, Service) untuk sebuah microservice, lalu mengirimnya sebagai **Pull Request** ke repo GitOps. Tujuan akhirnya: ArgoCD / Kustomize di cluster membaca PR yang sudah di-merge dan melakukan deploy otomatis.

### Contoh Perintah

```bash
bq --bootstrap-k8s plus-be-rustcreateordersnap-manager develop \
   --gitops-repo Qoin-Digital-Indonesia/gitops \
   --gitops-branch develop \
   --path-yaml cce/develop-qoin
```

### Apa yang Terjadi (Ringkas)

| Step | Aksi | Output |
|------|------|--------|
| 0 | Preflight check | Validasi `GITHUB_TOKEN`, `GH_CLI=false` |
| 1 | Derive env & namespace | `env=develop`, `namespace=develop-qoin` |
| 2 | Fetch CICD config | `cicd/cicd.json` dari repo → fallback PocketBase |
| 3 | Deteksi stack & fetch config | `.env.example` → base64 encode untuk Secret |
| 4 | Build render context | 12 variabel template (`APP`, `ENV`, `PORT`, dll) |
| 5 | Clone gitops repo | Shallow clone `--depth 1` → branch baru |
| 6 | Render 3 YAML + update kustomization | Secret + Deployment + Service |
| 7 | Commit & push | `bootstrap/{app}-{env}-{timestamp}` |
| 8 | Open PR | PR ke `gitops_branch` (deduplicated) |
| 9 | Cleanup | Hapus workdir temp |

---

## 2. Flow Diagram — End-to-End

```mermaid
flowchart TD
    START(["bq --bootstrap-k8s repo ref<br/>--gitops-repo ... --gitops-branch ... --path-yaml ..."])
    
    START --> PREFLIGHT

    subgraph STEP0["Step 0: Preflight"]
        PREFLIGHT{"GH_CLI=false?<br/>GITHUB_TOKEN ada?"}
        PREFLIGHT -->|"❌ GH_CLI=true"| FAIL_PRE["❌ Exit 1<br/>Set GH_CLI=false"]
        PREFLIGHT -->|"❌ Token kosong"| FAIL_TOKEN["❌ Exit 1<br/>GITHUB_TOKEN kosong"]
        PREFLIGHT -->|"✅ OK"| DERIVE
    end

    subgraph STEP1["Step 1: Derive Environment"]
        DERIVE["Derive env dari ref:<br/>develop → develop<br/>staging → staging<br/>main/master/vN → production"]
        DERIVE --> NS["Derive namespace:<br/>segmen terakhir --path-yaml<br/>cce/develop-qoin → develop-qoin"]
    end

    NS --> FETCH_CICD

    subgraph STEP2["Step 2: Fetch CICD Config"]
        FETCH_CICD["Coba fetch cicd/cicd.json<br/>dari source_repo@ref<br/>via GitHub REST API"]
        FETCH_CICD --> CICD_OK{"cicd.json<br/>ditemukan?"}
        CICD_OK -->|"✅ Ya"| CICD_DONE["Parse JSON → cicd dict<br/>IMAGE, PORT, DEPLOYMENT, ..."]
        CICD_OK -->|"❌ Tidak"| CICD_FALLBACK["⚠️ Fallback → PocketBase<br/>collection repo"]
        CICD_FALLBACK --> PB_FETCH["fetch_collection_records"]
        PB_FETCH --> PB_FOUND{"Record<br/>ditemukan?"}
        PB_FOUND -->|"❌"| FAIL_CICD["❌ Exit 1<br/>cicd config tidak ada"]
        PB_FOUND -->|"✅"| PB_NESTED{"record.cicd<br/>is dict?"}
        PB_NESTED -->|"Ya"| CICD_DONE
        PB_NESTED -->|"Tidak"| PB_FLAT["Ambil flat fields:<br/>image, port, deployment, ..."]
        PB_FLAT --> CICD_DONE
    end

    CICD_DONE --> DETECT_STACK

    subgraph STEP3["Step 3: Deteksi Stack dan Config File"]
        DETECT_STACK{"Hint STACK<br/>di cicd?"}
        DETECT_STACK -->|"dotnet"| PROBE_DOTNET["Probe appsettings.Env.json"]
        DETECT_STACK -->|"go/node/rust"| PROBE_DEFAULT["Probe .env.env → .env → .env.example"]
        DETECT_STACK -->|"Tidak ada"| PROBE_ALL["Probe berurutan:<br/>1. appsettings.Env.json<br/>2. .env.env<br/>3. .env<br/>4. .env.example"]
        PROBE_DOTNET --> CONFIG_RAW["config_raw bytes"]
        PROBE_DEFAULT --> CONFIG_RAW
        PROBE_ALL --> CONFIG_RAW
    end

    CONFIG_RAW --> BUILD_CTX

    subgraph STEP4["Step 4: Build Render Context"]
        BUILD_CTX["Bangun 12 variabel ctx:<br/>APP, ENV, NAMESPACE, REPLICAS,<br/>IMAGE_FULL, PORT, PROJECT,<br/>ROLE, NODEPOOL, IMAGE_PULL_SECRET,<br/>CONFIG_B64, DOTNET_ENV"]
    end

    BUILD_CTX --> CLONE_GITOPS

    subgraph STEP5["Step 5: Clone GitOps Repo"]
        CLONE_GITOPS["git clone --depth 1<br/>--branch gitops_branch<br/>gitops_repo → /tmp/bq-bootstrap-..."]
        CLONE_GITOPS --> BRANCH["git checkout -b<br/>bootstrap/app-env-timestamp"]
    end

    BRANCH --> RENDER

    subgraph STEP6["Step 6: Render YAML"]
        RENDER["Render 3 template file"]
        F1["① file-config/app-env.yaml<br/>Secret config encoded base64"]
        F2["② app_deployment.yaml<br/>Deployment mount Secret"]
        F3["③ app_services.yaml<br/>Service ClusterIP"]
        RENDER --> F1
        RENDER --> F2
        RENDER --> F3
        F1 --> KUSTOMIZE
        F2 --> KUSTOMIZE
        F3 --> KUSTOMIZE
        KUSTOMIZE["Update kustomization.yaml<br/>sisipkan entries baru di resources"]
    end

    KUSTOMIZE --> OPT_SECRET{"--apply-secret?"}

    subgraph STEP6B["Step 6b: Optional Operations"]
        OPT_SECRET -->|"Ya"| APPLY_SECRET["kubectl apply Secret<br/>skip jika sudah ada / production"]
        OPT_SECRET -->|"Tidak"| OPT_RECREATE
        APPLY_SECRET --> OPT_RECREATE
        OPT_RECREATE{"--force-recreate-deploy?"}
        OPT_RECREATE -->|"Ya"| RECREATE["Cek selector drift<br/>Delete deploy jika drift<br/>block production"]
        OPT_RECREATE -->|"Tidak"| COMMIT
        RECREATE --> COMMIT
    end

    subgraph STEP7["Step 7: Commit dan Push"]
        COMMIT["git add + status --porcelain"]
        COMMIT --> HAS_DIFF{"Ada diff?"}
        HAS_DIFF -->|"Tidak"| SKIP_PR["✅ Sudah up-to-date<br/>Exit 0 no PR needed"]
        HAS_DIFF -->|"Ya"| DO_COMMIT["git commit"]
        DO_COMMIT --> DRY_RUN{"--dry-run?"}
        DRY_RUN -->|"Ya"| DRY_DONE["🔍 Skip push + PR<br/>Exit 0"]
        DRY_RUN -->|"Tidak"| PUSH["git push origin<br/>bootstrap/app-env-ts"]
    end

    PUSH --> OPEN_PR

    subgraph STEP8["Step 8: Open PR Deduplicated"]
        OPEN_PR["Cek PR existing<br/>dengan head.ref == branch_name"]
        OPEN_PR --> PR_EXISTS{"PR sudah<br/>ada?"}
        PR_EXISTS -->|"Ya"| PR_DUP["ℹ️ PR sudah ada: URL<br/>Exit 0"]
        PR_EXISTS -->|"Tidak"| CREATE_PR["create_pull_request<br/>base=gitops_branch<br/>head=branch_name"]
        CREATE_PR --> PR_URL["✅ PR URL"]
    end

    PR_URL --> CLEANUP["🧹 Cleanup workdir<br/>skip if --keep-workdir"]
    CLEANUP --> DONE(["✅ Exit 0"])

    style START fill:#1a1a2e,stroke:#e94560,color:#fff
    style DONE fill:#0f3460,stroke:#16213e,color:#fff
    style FAIL_PRE fill:#c0392b,stroke:#e74c3c,color:#fff
    style FAIL_TOKEN fill:#c0392b,stroke:#e74c3c,color:#fff
    style FAIL_CICD fill:#c0392b,stroke:#e74c3c,color:#fff
    style SKIP_PR fill:#27ae60,stroke:#2ecc71,color:#fff
    style DRY_DONE fill:#2980b9,stroke:#3498db,color:#fff
    style PR_DUP fill:#f39c12,stroke:#e67e22,color:#fff
    style PR_URL fill:#27ae60,stroke:#2ecc71,color:#fff
```

---

## 3. Detail Per-Step

### Step 0: Preflight Check

```mermaid
flowchart LR
    A["load_config()"] --> B{"GH_CLI == false?"}
    B -->|"true = ❌"| C["bootstrap-k8s butuh jalur native REST"]
    B -->|"false = ✅"| D{"GITHUB_TOKEN?"}
    D -->|"kosong"| E["❌ Token kosong"]
    D -->|"ada"| F["✅ OK N chars"]
```

**Kenapa GH_CLI harus false?**  
`--bootstrap-k8s` menggunakan `github_api` module (native HTTP REST) untuk fetch file content, resolve commit SHA, list PR, dan create PR — bukan `gh` CLI. Ini menghindari dependency pada `gh` CLI yang mungkin belum ter-install.

**Source code:** [`_preflight()`](file:///Users/mamatnurahmat/build-q/build_q/bootstrap.py#L68-L76)

---

### Step 1: Environment & Namespace Derivation

| Git Ref | Derived `env` |
|---------|---------------|
| `develop` | `develop` |
| `staging` | `staging` |
| `main`, `master`, `v1.0.0` | `production` |
| lainnya | ref itu sendiri |

**Namespace** diambil dari segmen terakhir `--path-yaml`:
- `cce/develop-qoin` → `develop-qoin`
- `cce/production-qoin` → `production-qoin`

Bisa di-override via `--namespace` (berguna untuk cross-env).

**Source code:** [`_derive_env()`](file:///Users/mamatnurahmat/build-q/build_q/bootstrap.py#L37-L45)

---

### Step 2: Fetch CICD Config (Waterfall)

```mermaid
flowchart TD
    S["Source Repo @ ref"] --> G1["GET cicd/cicd.json"]
    G1 -->|"404"| G2["GET cicd.json root"]
    G2 -->|"404"| PB["🔄 PocketBase Fallback"]
    G1 -->|"200"| PARSE["JSON parse → cicd dict"]
    G2 -->|"200"| PARSE
    PB --> PB_AUTH["Authenticate PB API"]
    PB_AUTH --> PB_FETCH["fetch collection repo"]
    PB_FETCH --> PB_FIND["_find_repo_record"]
    PB_FIND -->|"found"| PB_CICD{"record.cicd<br/>is dict?"}
    PB_CICD -->|"Ya"| PARSE
    PB_CICD -->|"Tidak"| PB_FLAT["Build cicd dari flat fields:<br/>image, port, deployment, project"]
    PB_FLAT --> PARSE
    PB_FIND -->|"not found"| FAIL["❌ Abort"]
```

**Contoh `cicd/cicd.json`:**
```json
{
  "IMAGE": "plus-be-rustcreateordersnap-manager",
  "PORT": "2323",
  "DEPLOYMENT": "plus-be-rustcreateordersnap-manager",
  "PROJECT": "qoin",
  "NODETYPE": "back",
  "CLUSTER": "qoin",
  "STACK": "rust"
}
```

**Contoh PocketBase record (collection `repo`):**
```json
{
  "id": "zwv68x1frfcb5n4",
  "repo": "plus-be-rustcreateordersnap-manager",
  "cicd": {
    "IMAGE": "plus-be-rustcreateordersnap-manager",
    "PORT": "2323",
    "DEPLOYMENT": "plus-be-rustcreateordersnap-manager",
    "NODETYPE": "back",
    "PROJECT": "qoin",
    "STACK": "rust"
  }
}
```

**Source code:** [`bootstrap.py:L382-L425`](file:///Users/mamatnurahmat/build-q/build_q/bootstrap.py#L382-L425), [`_common.py:fetch_cicd_data()`](file:///Users/mamatnurahmat/build-q/build_q/_common.py#L85-L108)

---

### Step 3: Deteksi Stack & Fetch Config File

Stack menentukan **template** mana yang dipakai dan **config file** mana yang di-fetch dari source repo.

```mermaid
flowchart TD
    HINT{"Hint dari cicd<br/>STACK / TYPE"}
    HINT -->|"dotnet"| D["Probe: appsettings.DotnetEnv.json"]
    HINT -->|"go/node/rust/default"| E["Probe: .env.env → .env → .env.example"]
    HINT -->|"tidak ada"| F["Probe berurutan:<br/>1. appsettings.DotnetEnv.json<br/>2. .env.env<br/>3. .env<br/>4. .env.example"]
    D --> R["Return stack, path, raw_bytes"]
    E --> R
    F --> R
```

| Stack | Config File | Mount Path di Container | Secret Key |
|-------|-------------|-------------------------|------------|
| `default` | `.env` / `.env.{env}` / `.env.example` | `/app/.env` | `.env` |
| `dotnet` | `appsettings.{Env}.json` | `/app/appsettings.{Env}.json` | `appsettings.{Env}.json` |

**DotnetEnv mapping:**

| env | DotnetEnv |
|-----|-----------|
| develop | Development |
| staging | Staging |
| production | Production |

**Source code:** [`_detect_stack_and_fetch_config()`](file:///Users/mamatnurahmat/build-q/build_q/bootstrap.py#L79-L122)

---

### Step 4: Build Render Context

12 variabel yang digunakan untuk mengganti placeholder `{{KEY}}` di template:

| Key | Sumber | Contoh |
|-----|--------|--------|
| `APP` | `cicd.DEPLOYMENT` → `cicd.IMAGE` → repo name | `plus-be-rustcreateordersnap-manager` |
| `ENV` | Derived dari ref | `develop` |
| `NAMESPACE` | Segmen terakhir `--path-yaml` | `develop-qoin` |
| `DOTNET_ENV` | Mapped dari env | `Development` |
| `REPLICAS` | `--replicas` (default: 2) | `2` |
| `IMAGE_FULL` | `{registry}/{IMAGE}:{sha7}` | `loyaltolpi/plus-be-rustcreateordersnap-manager:074b14b` |
| `PORT` | `cicd.PORT` (default: 8080) | `2323` |
| `PROJECT` | `cicd.PROJECT` (default: qoin) | `qoin` |
| `ROLE` | `cicd.NODETYPE` (default: back) | `back` |
| `NODEPOOL` | `{namespace}-{suffix}` | `develop-qoin-service` |
| `IMAGE_PULL_SECRET` | `--image-pull-secret` (default: regcred) | `regcred` |
| `CONFIG_B64` | `base64(config_raw)` | `QVBQX05BTUUu...` |

**Image Tag Rule** (mirror Makefile `IMAGE_TAG`):
- Ref cocok pola tag `v\d*` → pakai ref sebagai tag (e.g. `v1.0.0`)
- Selain itu → pakai short SHA 7 char (e.g. `074b14b`)

**Nodepool suffix:**
- `NODETYPE=front` → suffix `manager`
- `NODETYPE=back` → suffix `service`

**Source code:** [`_resolve_image_tag()`](file:///Users/mamatnurahmat/build-q/build_q/bootstrap.py#L306-L315), [`run_bootstrap_k8s() Step 4`](file:///Users/mamatnurahmat/build-q/build_q/bootstrap.py#L461-L485)

---

### Step 5: Clone GitOps Repo

```bash
git clone --depth 1 --branch develop \
    https://x-access-token:{TOKEN}@github.com/Qoin-Digital-Indonesia/gitops.git \
    /tmp/bq-bootstrap-{app}-{env}-{timestamp}/gitops
```

- **Shallow clone** (`--depth 1`) untuk kecepatan
- Authentikasi via `x-access-token` (GITHUB_TOKEN)
- Workdir unik per run (timestamp-based)

---

### Step 6: Render YAML Templates

3 file YAML di-generate dari template dengan placeholder `{{KEY}}`:

#### ① Secret (`file-config/{app}-{env}.yaml`)

Template: [`secret.default.yaml`](file:///Users/mamatnurahmat/build-q/scripts/gist-bootstrap-k8s/secret.default.yaml)

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: file-config-{{APP}}-{{ENV}}
  namespace: {{NAMESPACE}}
  labels:
    app: {{APP}}
    env: {{ENV}}
type: Opaque
data:
  .env: {{CONFIG_B64}}     # ← config file di-encode base64
```

#### ② Deployment (`{app}_deployment.yaml`)

Template: [`deployment.default.yaml`](file:///Users/mamatnurahmat/build-q/scripts/gist-bootstrap-k8s/deployment.default.yaml)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{APP}}
  namespace: {{NAMESPACE}}
  labels: {app, env, project, role}
spec:
  replicas: {{REPLICAS}}
  selector:
    matchLabels: {app, env, project, role}  # ← IMMUTABLE!
  template:
    spec:
      imagePullSecrets:
        - name: {{IMAGE_PULL_SECRET}}
      nodeSelector:
        cce.cloud.com/cce-nodepool: {{NODEPOOL}}
      containers:
        - name: {{APP}}
          image: {{IMAGE_FULL}}
          ports:
            - containerPort: {{PORT}}
          volumeMounts:
            - mountPath: /app/.env      # Secret mount
              subPath: .env
      volumes:
        - name: file-config-volume
          secret:
            secretName: file-config-{{APP}}-{{ENV}}
```

#### ③ Service (`{app}_services.yaml`)

Template: [`services.yaml`](file:///Users/mamatnurahmat/build-q/scripts/gist-bootstrap-k8s/services.yaml)

```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{APP}}
  namespace: {{NAMESPACE}}
spec:
  type: ClusterIP
  selector:
    app: {{APP}}
  ports:
    - port: {{PORT}}
      targetPort: http
```

#### Kustomization Update

Setelah render, file `kustomization.yaml` di folder tujuan di-update secara **idempotent** — sisipkan entry baru di block `resources:` jika belum ada:

```yaml
resources:
  # ... existing entries ...
  - plus-be-rustcreateordersnap-manager_services.yaml     # ← ditambah
  - plus-be-rustcreateordersnap-manager_deployment.yaml    # ← ditambah
```

**Source code:** [`_render_and_write()`](file:///Users/mamatnurahmat/build-q/build_q/bootstrap.py#L318-L343), [`_update_kustomization()`](file:///Users/mamatnurahmat/build-q/build_q/bootstrap.py#L125-L182)

---

### Step 6b: Optional Operations

#### `--apply-secret` — Apply Secret ke Cluster

```mermaid
flowchart TD
    A{"env == production?"}
    A -->|"Ya"| B["🛑 BLOCK: production wajib via GitOps"]
    A -->|"Tidak"| C["kubectl get secret name -n ns"]
    C --> D{"Sudah ada?"}
    D -->|"Ya"| E["ℹ️ Skip — sudah ada"]
    D -->|"Tidak"| F["kubectl apply -f secret.yaml"]
```

> [!WARNING]
> **Production guardrail**: Secret di production HANYA boleh via PR-merge (GitOps). Tidak pernah di-apply langsung ke cluster.

**Source code:** [`_apply_secret_if_missing()`](file:///Users/mamatnurahmat/build-q/build_q/bootstrap.py#L259-L303)

#### `--force-recreate-deploy` — Selector Drift Fix

```mermaid
flowchart TD
    A{"env == production?"}
    A -->|"Ya"| B["🛑 BLOCK: production wajib manual"]
    A -->|"Tidak"| C["kubectl get deploy app<br/>-o jsonpath matchLabels"]
    C --> D{"Deployment<br/>ada?"}
    D -->|"Tidak"| E["ℹ️ Deployment belum ada — skip"]
    D -->|"Ya"| F{"Selector<br/>match?"}
    F -->|"Match"| G["✅ No drift"]
    F -->|"Drift"| H["kubectl delete deploy app<br/>ArgoCD selfHeal re-create"]
```

> [!IMPORTANT]
> Kubernetes Deployment `spec.selector` bersifat **IMMUTABLE**. Jika label schema berubah (misal 1-label → 4-label), `kubectl apply` akan **ditolak**. Solusinya: delete & re-create.

**Source code:** [`_recreate_deploy_if_selector_drift()`](file:///Users/mamatnurahmat/build-q/build_q/bootstrap.py#L185-L256)

---

### Step 7–8: Commit → Push → PR

```mermaid
sequenceDiagram
    participant BQ as bq CLI
    participant GIT as Git local
    participant GH as GitHub API

    BQ->>GIT: git add 3 files + kustomization
    BQ->>GIT: git status --porcelain
    
    alt Tidak ada diff
        GIT-->>BQ: empty
        Note over BQ: ✅ Already up-to-date, exit 0
    else Ada diff
        BQ->>GIT: git commit -m bootstrap-k8s app @ env
        
        alt --dry-run
            Note over BQ: 🔍 Skip push, exit 0
        else Normal
            BQ->>GIT: git push origin bootstrap/app-env-ts
            BQ->>GH: list_open_prs gitops_repo branch
            
            alt PR sudah ada
                GH-->>BQ: ℹ️ PR existing URL
            else PR baru
                BQ->>GH: create_pull_request base head title body
                GH-->>BQ: ✅ PR URL
            end
        end
    end
```

**PR Body** berisi metadata lengkap:
- Source repo & ref
- Namespace & env & stack
- Config file path
- Image full tag
- List of generated files

---

## 4. Output File Structure di GitOps Repo

```
Qoin-Digital-Indonesia/gitops/
└── cce/
    └── develop-qoin/
        ├── kustomization.yaml                                          ← updated
        ├── file-config/
        │   └── plus-be-rustcreateordersnap-manager-develop.yaml       ← Secret
        ├── plus-be-rustcreateordersnap-manager_deployment.yaml         ← Deployment
        └── plus-be-rustcreateordersnap-manager_services.yaml           ← Service
```

---

## 5. CLI Arguments Reference

### Required

| Argument | Deskripsi | Contoh |
|----------|-----------|--------|
| `<repo>` | Nama repo source (positional) | `plus-be-rustcreateordersnap-manager` |
| `<ref>` | Branch/tag (positional) | `develop` |
| `--gitops-repo` | Target GitOps repo | `Qoin-Digital-Indonesia/gitops` |
| `--gitops-branch` | Base branch untuk PR | `develop` |
| `--path-yaml` | Folder tujuan (segmen terakhir = namespace) | `cce/develop-qoin` |

### Optional

| Argument | Default | Deskripsi |
|----------|---------|-----------|
| `--stack` | auto-detect | Force stack: `dotnet` atau `default` |
| `--env` | dari ref | Override env: `develop`/`staging`/`production` |
| `--replicas` | `2` | Jumlah replicas Deployment |
| `--apply-secret` | `false` | Apply Secret ke cluster jika belum ada |
| `--kube-context` | current | kubectl context untuk `--apply-secret` |
| `--image-pull-secret` | `regcred` | imagePullSecret name |
| `--force-recreate-deploy` | `false` | Delete Deployment jika selector drift |
| `--namespace` | dari path-yaml | Override namespace |
| `--nodepool` | `{ns}-{suffix}` | Override cce-nodepool selector |
| `--pr-branch` | auto | Override nama branch PR |
| `--keep-workdir` | `false` | Pertahankan /tmp workdir |
| `--dry-run` | `false` | Preview tanpa push/PR |
| `--cicd` | `cicd/cicd.json` | Override path cicd config |

---

## 6. Guardrails & Safety

| Rule | Enforcement |
|------|-------------|
| **GH_CLI=false** required | Preflight check — abort jika `true` |
| **GITHUB_TOKEN** required | Preflight check — abort jika kosong |
| **Production block: apply-secret** | `--apply-secret` ditolak untuk `env=production` |
| **Production block: force-recreate** | `--force-recreate-deploy` ditolak untuk `env=production` |
| **PR deduplication** | Cek PR existing dengan `head.ref` match sebelum create |
| **No-diff skip** | Jika semua file identik, skip commit/PR (exit 0) |
| **Idempotent kustomization** | Hanya sisipkan entry yang belum ada |

---

## 7. CICD Config Source Priority

```mermaid
flowchart LR
    A["1. cicd/cicd.json<br/>GitHub repo"] -->|"404"| B["2. cicd.json<br/>repo root"]
    B -->|"404"| C["3. PocketBase<br/>collection repo<br/>nested cicd field"]
    C -->|"null"| D["4. PocketBase<br/>flat fields<br/>image, port, ..."]
    D -->|"empty"| E["❌ Abort"]
```

---

## 8. Contoh Real Execution

```
$ bq --bootstrap-k8s plus-be-rustcreateordersnap-manager develop \
     --gitops-repo Qoin-Digital-Indonesia/gitops \
     --gitops-branch develop \
     --path-yaml cce/develop-qoin

🔐 Preflight:
   ✅ GITHUB_TOKEN (40 chars)

📐 Derived: env=develop  namespace=develop-qoin (dari path-yaml)

📡 Fetch cicd config dari Qoin-Digital-Indonesia/plus-be-rustcreateordersnap-manager@develop ...
   ⚠️  cicd config tidak ada di repo (tried: cicd/cicd.json, cicd.json)
   🔄 Fallback: cek PocketBase collection 'repo' ...
   ✅ PocketBase/repo.cicd — IMAGE=plus-be-rustcreateordersnap-manager PORT=2323

🔍 Deteksi stack + fetch config file ...
   ✅ stack=default  source=.env.example  size=346B

🧩 Render context:
   APP                = plus-be-rustcreateordersnap-manager
   ENV                = develop
   NAMESPACE          = develop-qoin
   PROJECT            = qoin
   ROLE               = back
   NODEPOOL           = develop-qoin-service
   REPLICAS           = 2
   IMAGE_FULL         = loyaltolpi/plus-be-rustcreateordersnap-manager:074b14b
   PORT               = 2323
   IMAGE_PULL_SECRET  = regcred

📥 Clone gitops ...
🌿 Branch: bootstrap/plus-be-rustcreateordersnap-manager-develop-20261001-133218

📝 Render 3 file YAML:
   ✏️  cce/develop-qoin/file-config/plus-be-rustcreateordersnap-manager-develop.yaml
   ✏️  cce/develop-qoin/plus-be-rustcreateordersnap-manager_deployment.yaml
   ✏️  cce/develop-qoin/plus-be-rustcreateordersnap-manager_services.yaml

📦 Commit ...  ✅ 3 file
🚀 Push → origin/bootstrap/...
🔀 Open PR → develop
   ✅ https://github.com/Qoin-Digital-Indonesia/gitops/pull/577
🧹 Cleanup
```

---

## 9. Arsitektur & Dependencies

```mermaid
graph TB
    subgraph CLI["CLI Layer"]
        CLI_PY["cli.py<br/>argparse"]
    end

    subgraph CORE["Core Logic"]
        BOOT["bootstrap.py<br/>run_bootstrap_k8s"]
        COMMON["_common.py<br/>fetch_cicd_data"]
        TPL["templates.py<br/>load_template + render"]
        GH["github_api.py<br/>REST: contents, PR, SHA"]
        PB["pb_api.py<br/>PocketBase IDP"]
        REPO["repo.py<br/>_find_repo_record"]
        CFG["config.py<br/>load_config"]
    end

    subgraph EXTERNAL["External"]
        GITHUB["GitHub API<br/>repos/contents<br/>repos/pulls"]
        PBASE["PocketBase<br/>cicd-hw.qoin.id"]
        GIST["GitHub Gist<br/>template source"]
        K8S["K8s Cluster<br/>opt: kubectl"]
    end

    CLI_PY --> BOOT
    BOOT --> COMMON
    BOOT --> TPL
    BOOT --> GH
    BOOT --> PB
    BOOT --> REPO
    BOOT --> CFG
    COMMON --> GH
    GH --> GITHUB
    PB --> PBASE
    TPL --> GIST

    style CLI_PY fill:#2c3e50,color:#ecf0f1
    style BOOT fill:#2980b9,color:#fff
    style GITHUB fill:#24292e,color:#fff
    style PBASE fill:#6c3483,color:#fff
```

---

## 10. Error Codes

| Exit Code | Kondisi |
|-----------|---------|
| `0` | Sukses (PR created, already up-to-date, PR exists, dry-run) |
| `1` | Preflight fail, CICD config missing, stack detection fail, clone fail |
| `2` | Push fail, PR creation fail |

---

## 11. Template Source

Template YAML disimpan di **GitHub Gist** (single source of truth) dan di-cache lokal:

| Template Name | Gist File | Dipakai Untuk |
|---------------|-----------|---------------|
| `secret_default` | [`secret.default.yaml`](file:///Users/mamatnurahmat/build-q/scripts/gist-bootstrap-k8s/secret.default.yaml) | Secret stack default (Go/Node/Rust) |
| `secret_dotnet` | [`secret.dotnet.yaml`](file:///Users/mamatnurahmat/build-q/scripts/gist-bootstrap-k8s/secret.dotnet.yaml) | Secret stack dotnet |
| `deployment_default` | [`deployment.default.yaml`](file:///Users/mamatnurahmat/build-q/scripts/gist-bootstrap-k8s/deployment.default.yaml) | Deployment stack default |
| `deployment_dotnet` | [`deployment.dotnet.yaml`](file:///Users/mamatnurahmat/build-q/scripts/gist-bootstrap-k8s/deployment.dotnet.yaml) | Deployment stack dotnet |
| `services` | [`services.yaml`](file:///Users/mamatnurahmat/build-q/scripts/gist-bootstrap-k8s/services.yaml) | Service (shared) |

Cache TTL: 3600s (1 jam). Jika gist unreachable, fallback ke bundled template di [`templates.py`](file:///Users/mamatnurahmat/build-q/build_q/templates.py).
