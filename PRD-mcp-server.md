# PRD: `bq-mcp-server` — MCP Server for build-q DevOps Tools

> **Version:** 1.0
> **Last Updated:** 2026-10-07
> **Target build-q:** v0.1.44+
> **Status:** Draft — Menunggu Review
> **Owner:** ngen contributors

---

## 1. Ringkasan

`bq-mcp-server` adalah MCP (Model Context Protocol) server yang meng-expose kapabilitas
CLI `bq` (build-q) sebagai tools dan resources yang dapat dipanggil oleh AI assistant
(Claude Code, Claude Desktop, atau MCP client lain). Dengan ini, AI agent bisa langsung
melakukan Dockerfile scanning, K8s anomaly detection, pipeline triggering, GitOps
operations, dan SOPS encryption — tanpa user perlu hafal flag CLI.

### Kenapa MCP?

| Pendekatan | Kelebihan | Kekurangan |
|------------|-----------|------------|
| **CLI biasa** | Sudah ada, matang | AI harus parse stdout text, tidak terstruktur |
| **HTTP API (`--serve`)** | Sudah ada (v0.1.42) | Hanya untuk Jev Planner, bukan general purpose |
| **MCP Server** | Structured I/O, tool discovery, multi-client | Perlu wrapper baru |

MCP memberikan **structured input/output** yang ideal untuk AI — tidak perlu regex parsing
stdout. AI assistant bisa *discover* tools yang tersedia, memahami schema-nya, dan
memanggil dengan parameter yang tepat.

### Contoh Skenario

```
User: "scan Dockerfile di repo pay-be-topup-manager untuk security issues"

Claude Code (via MCP):
  → tool: dockerfile_scan_remote
  → args: {repo: "Qoin-Digital-Indonesia/pay-be-topup-manager", ref: "main"}
  → result: {exit_code: 1, issues: [{rule: "DF-SEC-01", severity: "warning", ...}]}

User: "cek apakah image di gitops sudah match dengan yang di cluster"

Claude Code (via MCP):
  → tool: gitops_match_check
  → args: {namespace: "staging-qoin", deployment: "pay-be-topup-manager", ...}
  → result: {match: false, live: "abc123", gitops: "v1.2.3"}
```

---

## 2. Problem Statement

Saat ini AI agent (Claude Code) berinteraksi dengan `bq` via `subprocess` / Bash tool —
harus construct command string, parse stdout text, dan handle error secara ad-hoc.
Ini menyebabkan:

1. **Fragile parsing** — output `bq` menggunakan Rich formatting (tables, panels, colors)
   yang sulit di-parse oleh AI secara konsisten.
2. **No discovery** — AI tidak tahu tools apa yang tersedia tanpa membaca source code.
3. **No structured errors** — exit code saja tidak cukup; AI perlu tahu *apa* yang gagal.
4. **Duplikasi logic** — setiap Claude Code skill yang memanggil `bq` harus menulis
   wrapper parsing sendiri.

---

## 3. Goals

- **G1:** Expose 15+ kapabilitas `bq` sebagai MCP tools dengan structured JSON I/O.
- **G2:** 100% reuse logic existing — tidak ada duplikasi business logic.
- **G3:** Support transport stdio (Claude Code/Desktop) dan SSE (web/remote).
- **G4:** Read-only tools (scan, check, doctor) bisa jalan tanpa confirmation.
- **G5:** Action tools (build, trigger, set-image) memerlukan explicit confirmation.
- **G6:** Backward compatible — `bq` CLI tetap bekerja seperti biasa.
- **G7:** Installable sebagai optional extra: `pipx install build-q[mcp]`.

---

## 4. Non-goals

- Bukan pengganti CLI `bq` — MCP server adalah **channel tambahan**.
- Tidak mengganti `bq --serve` (Jev web chat) — itu tetap berfungsi terpisah.
- Tidak expose TUI interaktif via MCP (TUI adalah experience lokal).
- Tidak implement MCP sampling (server tidak memanggil LLM).

---

## 5. Architecture

### 5.1 High-Level Diagram

```
┌─────────────────────────────────────────────────────┐
│                   MCP Clients                       │
│  ┌──────────┐  ┌───────────────┐  ┌─────────────┐  │
│  │ Claude   │  │ Claude        │  │ Other MCP   │  │
│  │ Code     │  │ Desktop       │  │ Client      │  │
│  └────┬─────┘  └──────┬────────┘  └──────┬──────┘  │
│       │ stdio         │ stdio            │ SSE     │
└───────┼───────────────┼──────────────────┼──────────┘
        │               │                 │
        ▼               ▼                 ▼
┌─────────────────────────────────────────────────────┐
│              bq-mcp-server (Python)                 │
│                                                     │
│  ┌─────────────────────────────────────────────┐    │
│  │           MCP Protocol Layer                │    │
│  │  • list_tools()     → 18 tools              │    │
│  │  • call_tool()      → dispatch + serialize  │    │
│  │  • list_resources() → 4 resources           │    │
│  │  • read_resource()  → fetch + serialize     │    │
│  │  • list_prompts()   → 3 prompts             │    │
│  │  • get_prompt()     → workflow templates     │    │
│  └─────────┬───────────────────────────────────┘    │
│            │ direct function call                    │
│  ┌─────────▼───────────────────────────────────┐    │
│  │         build_q modules (existing)          │    │
│  │                                             │    │
│  │  dockerfile_scanner.py  anomaly.py          │    │
│  │  builder.py             check.py            │    │
│  │  cicd_trigger.py        cicd_webhook.py     │    │
│  │  bootstrap.py           gitops_set_image.py │    │
│  │  is_match_image.py      set_image.py        │    │
│  │  sops.py                doctor.py           │    │
│  │  config.py              pb_api.py           │    │
│  │  tui_catalog.py                             │    │
│  └─────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────┘
```

### 5.2 Module Layout

```
build_q/
├── mcp/                          # NEW — MCP server package
│   ├── __init__.py
│   ├── server.py                 # Server entry point, tool/resource registration
│   ├── tools/                    # Tool handlers (1 file per group)
│   │   ├── __init__.py
│   │   ├── scan.py               # dockerfile_scan, dockerfile_scan_remote, k8s_anomaly_scan
│   │   ├── build.py              # docker_build, docker_build_preview, image_check
│   │   ├── cicd.py               # pipeline_trigger, pipeline_check, webhook_status
│   │   ├── gitops.py             # gitops_set_image, gitops_match_check, gitops_bootstrap
│   │   ├── k8s.py                # k8s_set_image
│   │   ├── sops.py               # sops_encrypt, sops_decrypt
│   │   └── infra.py              # build_doctor, config_show
│   ├── resources.py              # MCP resource handlers
│   ├── prompts.py                # MCP prompt templates
│   └── _capture.py               # Stdout/stderr capture utility
├── cli.py                        # existing — tambah --mcp flag
└── ...                           # existing modules unchanged
```

### 5.3 Capture Strategy

Fungsi-fungsi `bq` saat ini mencetak output ke stdout/stderr via Rich console.
MCP server perlu **menangkap** output ini dan mengembalikan sebagai structured JSON.

```python
# build_q/mcp/_capture.py

import io
import sys
from contextlib import contextmanager
from typing import Generator, Tuple

@contextmanager
def capture_output() -> Generator[Tuple[io.StringIO, io.StringIO], None, None]:
    """Capture stdout + stderr selama function call."""
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = buf_out = io.StringIO()
    sys.stderr = buf_err = io.StringIO()
    try:
        yield buf_out, buf_err
    finally:
        sys.stdout, sys.stderr = old_out, old_err
```

Alternatif: untuk modul yang menggunakan Rich Console, inject `Console(file=StringIO())`
agar output ter-capture tanpa ANSI escape codes.

---

## 6. MCP Tools Specification

### 6.1 Tool Categories

Tools dibagi 3 tier berdasarkan risk level:

| Tier | Risk | Confirmation | Contoh |
|------|------|-------------|--------|
| **T1: Read-only** | None | Tidak perlu | scan, check, doctor, match |
| **T2: Reversible Action** | Low | Optional | sops encrypt/decrypt (file lokal) |
| **T3: External Action** | Medium-High | Wajib | build+push, trigger pipeline, set-image |

### 6.2 Complete Tool Definitions

#### Group 1: Scanning & Validation (T1 — Read-only)

---

**Tool: `dockerfile_scan`**

Scan Dockerfile lokal untuk 22+ issues (security, performance, compliance).

| Field | Value |
|-------|-------|
| **Source** | `dockerfile_scanner.run_dockerfile_scan()` |
| **Risk** | T1 — read-only |

```json
{
  "name": "dockerfile_scan",
  "description": "Scan a local Dockerfile for 22+ issues covering security, performance, compliance, and build errors. Returns structured findings with severity levels.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string",
        "description": "Path to Dockerfile or directory containing Dockerfile",
        "default": "."
      },
      "use_jev": {
        "type": "boolean",
        "description": "Use Jev AI for severity synthesis (requires network)",
        "default": true
      }
    }
  }
}
```

**Output schema:**
```json
{
  "exit_code": 0,
  "summary": "2 warnings, 0 errors",
  "issues": [
    {
      "rule": "DF-SEC-01",
      "severity": "warning",
      "line": 12,
      "message": "Using sudo in RUN instruction",
      "fix_available": true
    }
  ],
  "stdout": "..."
}
```

---

**Tool: `dockerfile_scan_remote`**

Scan Dockerfile dari GitHub repo tanpa clone.

| Field | Value |
|-------|-------|
| **Source** | `dockerfile_scanner.run_dockerfile_scan_remote()` |
| **Risk** | T1 — read-only (fetches via GitHub API) |

```json
{
  "name": "dockerfile_scan_remote",
  "description": "Scan a Dockerfile from a GitHub repository without cloning. Fetches via GitHub API.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "repo": {
        "type": "string",
        "description": "GitHub repo in owner/repo format (e.g. Qoin-Digital-Indonesia/pay-be-topup-manager)"
      },
      "ref": {
        "type": "string",
        "description": "Git ref (branch or tag)",
        "default": "main"
      },
      "dockerfile_path": {
        "type": "string",
        "description": "Path to Dockerfile in repo",
        "default": "Dockerfile"
      },
      "use_jev": {
        "type": "boolean",
        "description": "Use Jev AI for severity synthesis",
        "default": true
      }
    },
    "required": ["repo"]
  }
}
```

---

**Tool: `k8s_anomaly_scan`**

Scan K8s manifest YAML untuk 10+ production anomalies.

| Field | Value |
|-------|-------|
| **Source** | `anomaly.run_anomaly_scan()` |
| **Risk** | T1 — read-only |

```json
{
  "name": "k8s_anomaly_scan",
  "description": "Scan Kubernetes deployment manifests for 10+ anomalies: low replicas, missing imagePullSecret, no resource limits, insecure securityContext, etc.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string",
        "description": "Path to YAML file or directory"
      },
      "glob_pattern": {
        "type": "string",
        "description": "File glob for directory scan",
        "default": "*_deployment.yaml"
      },
      "use_jev": {
        "type": "boolean",
        "description": "Use Jev AI for severity verdict",
        "default": true
      }
    },
    "required": ["path"]
  }
}
```

---

**Tool: `build_doctor`**

Preflight check — verifikasi tools, credentials, dan builder.

| Field | Value |
|-------|-------|
| **Source** | `doctor.run_doctor()` |
| **Risk** | T1 — read-only |

```json
{
  "name": "build_doctor",
  "description": "Run preflight checks: Docker daemon, buildx plugin, builder instance, registry credentials, GitHub token, Git config. Returns pass/fail for each check.",
  "inputSchema": {
    "type": "object",
    "properties": {}
  }
}
```

---

**Tool: `pipeline_check`**

Cek repo readiness untuk build (tanpa clone).

| Field | Value |
|-------|-------|
| **Source** | `check.run_check()` |
| **Risk** | T1 — read-only |

```json
{
  "name": "pipeline_check",
  "description": "Verify repo readiness for CI/CD: checks ref exists, cicd.json valid, build artifacts present, image status in registry, GitOps alignment.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "repo": {
        "type": "string",
        "description": "GitHub repo (owner/repo format)"
      },
      "ref": {
        "type": "string",
        "description": "Git ref (branch or tag)",
        "default": "main"
      },
      "namespace": {
        "type": "string",
        "description": "K8s namespace for rollout suggestion"
      },
      "infra": {
        "type": "string",
        "enum": ["cce", "k8s"],
        "description": "Infrastructure type"
      }
    },
    "required": ["repo"]
  }
}
```

---

**Tool: `gitops_match_check`**

Compare image di live K8s cluster vs GitOps YAML.

| Field | Value |
|-------|-------|
| **Source** | `is_match_image.run_is_match_image()` |
| **Risk** | T1 — read-only (kubectl get + GitHub API) |

```json
{
  "name": "gitops_match_check",
  "description": "Compare the image running in a Kubernetes deployment against the image declared in the GitOps repository YAML. Detects drift.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "namespace": {
        "type": "string",
        "description": "K8s namespace"
      },
      "deployment": {
        "type": "string",
        "description": "Deployment name"
      },
      "gitops_repo": {
        "type": "string",
        "description": "GitOps repo (owner/repo)"
      },
      "gitops_branch": {
        "type": "string",
        "description": "GitOps branch",
        "default": "main"
      },
      "gitops_path": {
        "type": "string",
        "description": "Path to deployment YAML in GitOps repo"
      }
    },
    "required": ["namespace", "deployment", "gitops_repo", "gitops_path"]
  }
}
```

---

**Tool: `image_check`**

Cek apakah image:tag sudah ada di Docker Hub registry.

| Field | Value |
|-------|-------|
| **Source** | `builder.py` (registry check logic) |
| **Risk** | T1 — read-only (Docker Hub API) |

```json
{
  "name": "image_check",
  "description": "Check if a Docker image:tag already exists in Docker Hub registry. Returns exists/not-exists status.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "image": {
        "type": "string",
        "description": "Full image reference (e.g. loyaltolpi/pay-be-topup-manager:v1.2.3)"
      }
    },
    "required": ["image"]
  }
}
```

---

**Tool: `webhook_status`**

Cek apakah GitHub webhook sudah terinstall di repo.

| Field | Value |
|-------|-------|
| **Source** | `cicd_webhook.run_cicd_webhook_check()` |
| **Risk** | T1 — read-only (GitHub API) |

```json
{
  "name": "webhook_status",
  "description": "Check if the CI/CD webhook (cicd-hw.qoin.id/hook) is installed on a GitHub repository. Reports webhook URL, events, and active status.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "repo": {
        "type": "string",
        "description": "GitHub repo (owner/repo format)"
      }
    },
    "required": ["repo"]
  }
}
```

---

**Tool: `config_show`**

Tampilkan konfigurasi bq saat ini.

| Field | Value |
|-------|-------|
| **Source** | `config.load_config()` |
| **Risk** | T1 — read-only |

```json
{
  "name": "config_show",
  "description": "Show current build-q configuration: builder name, registry, GitHub org, webhook URLs, GitOps settings. Sensitive values are masked.",
  "inputSchema": {
    "type": "object",
    "properties": {}
  }
}
```

---

#### Group 2: Build Operations (T3 — External Action)

---

**Tool: `docker_build`**

Build dan push Docker image.

| Field | Value |
|-------|-------|
| **Source** | `builder.run_build()` |
| **Risk** | T3 — pushes image to registry |

```json
{
  "name": "docker_build",
  "description": "Build a Docker image using buildx. Reads cicd.json for image name, port, and project. Optionally pushes to registry. DESTRUCTIVE: pushes to Docker Hub when push=true.",
  "annotations": {
    "destructiveHint": true,
    "confirmationRequired": true
  },
  "inputSchema": {
    "type": "object",
    "properties": {
      "repo": {
        "type": "string",
        "description": "Repository name or path"
      },
      "ref": {
        "type": "string",
        "description": "Git ref (branch or tag)"
      },
      "push": {
        "type": "boolean",
        "description": "Push image to registry after build",
        "default": false
      },
      "tag": {
        "type": "string",
        "description": "Override image tag"
      },
      "platform": {
        "type": "string",
        "description": "Target platform",
        "default": "linux/amd64"
      },
      "dockerfile": {
        "type": "string",
        "description": "Dockerfile path",
        "default": "Dockerfile"
      },
      "dry_run": {
        "type": "boolean",
        "description": "Preview command without executing",
        "default": false
      },
      "build_args": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Extra build arguments (KEY=VALUE format)"
      }
    },
    "required": ["repo", "ref"]
  }
}
```

---

**Tool: `docker_build_preview`**

Dry-run — preview build command tanpa eksekusi.

| Field | Value |
|-------|-------|
| **Source** | `builder.run_build(dry_run=True)` |
| **Risk** | T1 — read-only (dry-run) |

```json
{
  "name": "docker_build_preview",
  "description": "Preview the Docker buildx command that would be executed, without actually building. Safe read-only operation.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "repo": {
        "type": "string",
        "description": "Repository name or path"
      },
      "ref": {
        "type": "string",
        "description": "Git ref (branch or tag)"
      },
      "tag": {
        "type": "string",
        "description": "Override image tag"
      },
      "platform": {
        "type": "string",
        "description": "Target platform",
        "default": "linux/amd64"
      }
    },
    "required": ["repo", "ref"]
  }
}
```

---

#### Group 3: CI/CD Pipeline (T3 — External Action)

---

**Tool: `pipeline_trigger`**

Trigger Jenkins X pipeline via webhook.

| Field | Value |
|-------|-------|
| **Source** | `cicd_trigger.run_cicd_trigger()` |
| **Risk** | T3 — triggers external pipeline |

```json
{
  "name": "pipeline_trigger",
  "description": "Trigger a Jenkins X pipeline build by sending a synthetic GitHub push event to the webhook relay. EXTERNAL ACTION: starts a CI/CD build in the cluster.",
  "annotations": {
    "destructiveHint": true,
    "confirmationRequired": true
  },
  "inputSchema": {
    "type": "object",
    "properties": {
      "repo": {
        "type": "string",
        "description": "GitHub repo (owner/repo format)"
      },
      "ref": {
        "type": "string",
        "description": "Git ref to build",
        "default": "main"
      },
      "sha": {
        "type": "string",
        "description": "Override commit SHA (default: resolve from ref)"
      },
      "force": {
        "type": "boolean",
        "description": "Bypass webhook deduplication",
        "default": false
      },
      "dry_run": {
        "type": "boolean",
        "description": "Preview payload without sending",
        "default": false
      }
    },
    "required": ["repo"]
  }
}
```

---

#### Group 4: GitOps Operations (T3 — External Action)

---

**Tool: `gitops_set_image`**

Update image tag di GitOps deployment YAML.

| Field | Value |
|-------|-------|
| **Source** | `gitops_set_image.run_gitops_set_image()` |
| **Risk** | T3 — pushes commit to GitOps repo |

```json
{
  "name": "gitops_set_image",
  "description": "Update image tag in a GitOps deployment YAML file and push the change. EXTERNAL ACTION: commits and pushes to the GitOps repository.",
  "annotations": {
    "destructiveHint": true,
    "confirmationRequired": true
  },
  "inputSchema": {
    "type": "object",
    "properties": {
      "repo": {
        "type": "string",
        "description": "GitOps repo (owner/repo)"
      },
      "branch": {
        "type": "string",
        "description": "Target branch",
        "default": "main"
      },
      "path": {
        "type": "string",
        "description": "Path to deployment YAML in GitOps repo"
      },
      "image": {
        "type": "string",
        "description": "Full image reference (e.g. loyaltolpi/app:v1.2.3)"
      }
    },
    "required": ["repo", "path", "image"]
  }
}
```

---

**Tool: `gitops_bootstrap`**

Bootstrap K8s manifest (Secret + Deployment + Service) ke GitOps repo.

| Field | Value |
|-------|-------|
| **Source** | `bootstrap.run_bootstrap_k8s()` |
| **Risk** | T3 — creates PR to GitOps repo |

```json
{
  "name": "gitops_bootstrap",
  "description": "Generate Kubernetes manifests (Secret, Deployment, Service, HPA, PDB) and create a PR to the GitOps repository. EXTERNAL ACTION: creates Git branch and Pull Request.",
  "annotations": {
    "destructiveHint": true,
    "confirmationRequired": true
  },
  "inputSchema": {
    "type": "object",
    "properties": {
      "source_repo": {
        "type": "string",
        "description": "Source app repo (owner/repo)"
      },
      "ref": {
        "type": "string",
        "description": "Git ref of source repo"
      },
      "gitops_repo": {
        "type": "string",
        "description": "GitOps repo (owner/repo)"
      },
      "gitops_branch": {
        "type": "string",
        "description": "Base branch in GitOps repo",
        "default": "main"
      },
      "path_yaml": {
        "type": "string",
        "description": "Destination folder in GitOps repo (e.g. cce/staging-qoin/myapp)"
      },
      "replicas": {
        "type": "integer",
        "description": "Deployment replicas (min 2 for production)",
        "default": 2
      },
      "stack": {
        "type": "string",
        "enum": ["default", "dotnet"],
        "description": "Override stack detection"
      },
      "env": {
        "type": "string",
        "description": "Override environment (develop/staging/production)"
      },
      "apply_secret": {
        "type": "boolean",
        "description": "Apply Secret to cluster immediately",
        "default": false
      },
      "kube_context": {
        "type": "string",
        "description": "kubectl context for --apply-secret"
      },
      "namespace": {
        "type": "string",
        "description": "Override K8s namespace"
      },
      "nodepool": {
        "type": "string",
        "description": "Override CCE nodepool selector"
      }
    },
    "required": ["source_repo", "ref", "gitops_repo", "path_yaml"]
  }
}
```

---

#### Group 5: Kubernetes Direct (T3 — External Action)

---

**Tool: `k8s_set_image`**

Hot-patch image di K8s deployment (imperative rollout).

| Field | Value |
|-------|-------|
| **Source** | `set_image.run_set_image()` |
| **Risk** | T3 — modifies live K8s deployment |

```json
{
  "name": "k8s_set_image",
  "description": "Hot-patch a Kubernetes deployment with a new image tag using kubectl set image. DESTRUCTIVE: modifies live cluster state.",
  "annotations": {
    "destructiveHint": true,
    "confirmationRequired": true
  },
  "inputSchema": {
    "type": "object",
    "properties": {
      "namespace": {
        "type": "string",
        "description": "K8s namespace"
      },
      "deployment": {
        "type": "string",
        "description": "Deployment name"
      },
      "image": {
        "type": "string",
        "description": "New image reference (can be full image:tag or just short SHA)"
      },
      "container": {
        "type": "string",
        "description": "Container name (default: auto-detect)"
      }
    },
    "required": ["namespace", "deployment", "image"]
  }
}
```

---

#### Group 6: SOPS Encryption (T2 — Reversible Action)

---

**Tool: `sops_encrypt`**

Encrypt file dengan SOPS + age.

| Field | Value |
|-------|-------|
| **Source** | `sops.encrypt_file()` |
| **Risk** | T2 — modifies local file (reversible) |

```json
{
  "name": "sops_encrypt",
  "description": "Encrypt a file using SOPS with age encryption. Modifies the file in-place.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string",
        "description": "Path to file to encrypt"
      },
      "recipients": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Age public keys (default: from config)"
      }
    },
    "required": ["path"]
  }
}
```

---

**Tool: `sops_decrypt`**

Decrypt file SOPS.

| Field | Value |
|-------|-------|
| **Source** | `sops.decrypt_file()` |
| **Risk** | T2 — modifies local file if in_place=true |

```json
{
  "name": "sops_decrypt",
  "description": "Decrypt a SOPS-encrypted file. Returns plaintext content or decrypts in-place.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string",
        "description": "Path to SOPS-encrypted file"
      },
      "in_place": {
        "type": "boolean",
        "description": "Decrypt in-place (modify file) vs return content",
        "default": false
      }
    },
    "required": ["path"]
  }
}
```

---

#### Group 7: CI/CD Scaffolding (T2 — Local File Action)

---

**Tool: `dockerfile_fix`**

Auto-fix Dockerfile issues yang ditemukan scanner.

| Field | Value |
|-------|-------|
| **Source** | `dockerfile_scanner.run_dockerfile_scan(auto_fix=True)` |
| **Risk** | T2 — modifies local Dockerfile |

```json
{
  "name": "dockerfile_fix",
  "description": "Scan and auto-fix Dockerfile issues. Modifies the Dockerfile in-place with fixes for security, performance, and compliance issues.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string",
        "description": "Path to Dockerfile or directory",
        "default": "."
      },
      "use_jev": {
        "type": "boolean",
        "description": "Use Jev AI for severity synthesis",
        "default": true
      }
    }
  }
}
```

---

### 6.3 Tool Count Summary

| Tier | Count | Tools |
|------|-------|-------|
| **T1: Read-only** | 9 | `dockerfile_scan`, `dockerfile_scan_remote`, `k8s_anomaly_scan`, `build_doctor`, `pipeline_check`, `gitops_match_check`, `image_check`, `webhook_status`, `config_show` |
| **T2: Local Action** | 3 | `dockerfile_fix`, `sops_encrypt`, `sops_decrypt` |
| **T3: External Action** | 6 | `docker_build`, `docker_build_preview`, `pipeline_trigger`, `gitops_set_image`, `gitops_bootstrap`, `k8s_set_image` |
| **Total** | **18** | |

---

## 7. MCP Resources

Resources menyediakan konteks read-only yang AI bisa akses kapan saja.

### 7.1 Resource Definitions

| URI | Source | Deskripsi |
|-----|--------|-----------|
| `bq://config` | `config.load_config()` | Current bq configuration (sensitive values masked) |
| `bq://catalog` | `tui_catalog.load_catalog()` | Jev tool catalog (tools, providers, patterns) |
| `bq://doctor` | `doctor.run_doctor()` | Latest preflight check results |
| `bq://version` | `build_q.__init__` | Version string + capabilities |

### 7.2 Resource Templates (Dynamic)

| URI Template | Source | Deskripsi |
|-------------|--------|-----------|
| `bq://repo/{owner}/{name}/cicd` | `pb_api` / GitHub API | CICD config (cicd.json) for a repo |

---

## 8. MCP Prompts

Prompts menyediakan workflow templates yang AI bisa gunakan.

| Prompt Name | Deskripsi | Arguments |
|-------------|-----------|-----------|
| `deploy-new-service` | Workflow lengkap: build → bootstrap → verify | `repo`, `ref`, `env` |
| `scan-and-fix` | Scan Dockerfile + K8s manifest, fix issues | `repo`, `ref` |
| `rollout-update` | Update image: build → set-image (imperative/declarative) | `repo`, `ref`, `namespace` |

### 8.1 Contoh Prompt: `deploy-new-service`

```json
{
  "name": "deploy-new-service",
  "description": "Complete workflow to deploy a new service: build image, bootstrap K8s manifests, create GitOps PR, verify deployment.",
  "arguments": [
    {"name": "repo", "description": "Source repo (owner/repo)", "required": true},
    {"name": "ref", "description": "Git ref to build", "required": true},
    {"name": "env", "description": "Target environment", "required": true}
  ]
}
```

Prompt message yang dihasilkan:

```
Deploy new service workflow:

1. Run `build_doctor` to verify prerequisites
2. Run `pipeline_check` for {repo} at {ref}
3. Run `docker_build` with push=true
4. Run `gitops_bootstrap` to create K8s manifests
5. Verify with `gitops_match_check`

Repository: {repo}
Ref: {ref}
Environment: {env}
```

---

## 9. Implementation Plan

### Phase 1: Foundation + Read-only Tools (v0.1.44)

**Target:** 1 minggu
**Scope:** MCP server skeleton + 9 read-only tools + 4 resources

| Task | Module | Estimasi |
|------|--------|----------|
| Setup `mcp` dependency (optional extra) | `pyproject.toml` | 0.5h |
| Server entry point + stdio transport | `mcp/server.py` | 2h |
| Stdout/stderr capture utility | `mcp/_capture.py` | 1h |
| Scan tools (3): dockerfile_scan, dockerfile_scan_remote, k8s_anomaly_scan | `mcp/tools/scan.py` | 3h |
| Infra tools (2): build_doctor, config_show | `mcp/tools/infra.py` | 1h |
| Check tools (4): pipeline_check, gitops_match_check, image_check, webhook_status | `mcp/tools/cicd.py` | 2h |
| Resources (4): config, catalog, doctor, version | `mcp/resources.py` | 2h |
| CLI integration: `bq --mcp` flag | `cli.py` | 0.5h |
| Tests: unit tests untuk setiap tool handler | `tests/test_mcp_*.py` | 3h |
| Documentation: README update + config example | `README.md` | 1h |

**Deliverable:** `bq --mcp` starts stdio MCP server, Claude Code dapat scan Dockerfile
dan K8s manifest via MCP.

**Config Claude Code:**
```json
{
  "mcpServers": {
    "bq": {
      "command": "bq",
      "args": ["--mcp"]
    }
  }
}
```

### Phase 2: Action Tools (v0.1.45)

**Target:** 1 minggu
**Scope:** 6 action tools + 3 local action tools + confirmation flow

| Task | Module | Estimasi |
|------|--------|----------|
| Build tools (2): docker_build, docker_build_preview | `mcp/tools/build.py` | 2h |
| CICD tools (1): pipeline_trigger | `mcp/tools/cicd.py` | 1h |
| GitOps tools (2): gitops_set_image, gitops_bootstrap | `mcp/tools/gitops.py` | 3h |
| K8s tools (1): k8s_set_image | `mcp/tools/k8s.py` | 1h |
| SOPS tools (2): sops_encrypt, sops_decrypt | `mcp/tools/sops.py` | 1h |
| Dockerfile fix (1): dockerfile_fix | `mcp/tools/scan.py` | 1h |
| Tool annotations (destructiveHint, confirmationRequired) | semua action tools | 1h |
| Integration tests | `tests/test_mcp_actions.py` | 3h |

**Deliverable:** Full 18-tool MCP server.

### Phase 3: Prompts + SSE Transport + Polish (v0.1.46)

**Target:** 1 minggu
**Scope:** MCP prompts, SSE transport, production hardening

| Task | Module | Estimasi |
|------|--------|----------|
| MCP Prompts (3): deploy-new-service, scan-and-fix, rollout-update | `mcp/prompts.py` | 2h |
| SSE transport support | `mcp/server.py` | 2h |
| Resource templates (dynamic repo lookup) | `mcp/resources.py` | 2h |
| Error handling: structured MCP errors | semua handlers | 2h |
| Logging: structured JSON logs | `mcp/server.py` | 1h |
| Sensitive value masking (tokens, passwords) | `mcp/_capture.py` | 1h |
| Performance: lazy module imports | `mcp/tools/*.py` | 1h |
| End-to-end test: Claude Code → MCP → bq | `tests/test_mcp_e2e.py` | 3h |
| PyPI release: `build-q[mcp]` optional extra | `pyproject.toml` | 0.5h |

**Deliverable:** Production-ready MCP server dengan SSE support.

---

## 10. Dependencies

### 10.1 Python Dependencies

```toml
# pyproject.toml
[project.optional-dependencies]
mcp = ["mcp>=1.0"]
anomaly = ["pyyaml>=6.0"]
all = ["mcp>=1.0", "pyyaml>=6.0"]
```

### 10.2 Compatibility

| Requirement | Minimum | Note |
|-------------|---------|------|
| Python | 3.10+ | MCP SDK requires 3.10+, bq core tetap 3.7+ |
| `mcp` SDK | 1.0+ | PyPI: `mcp` (Anthropic official) |
| `bq` core | 0.1.43+ | Semua fungsi sudah keyword-arg (tidak argparse) |

---

## 11. Testing Strategy

### 11.1 Unit Tests

Setiap MCP tool handler di-test secara isolated:

```python
# tests/test_mcp_scan.py

import pytest
from unittest.mock import patch
from build_q.mcp.tools.scan import handle_dockerfile_scan

@pytest.mark.asyncio
async def test_dockerfile_scan_clean():
    """Dockerfile tanpa issues → exit_code 0."""
    with patch("build_q.dockerfile_scanner.run_dockerfile_scan", return_value=0):
        result = await handle_dockerfile_scan({"path": "/tmp/clean/Dockerfile"})
        assert result["exit_code"] == 0

@pytest.mark.asyncio
async def test_dockerfile_scan_with_issues():
    """Dockerfile dengan issues → exit_code 1, issues non-empty."""
    with patch("build_q.dockerfile_scanner.run_dockerfile_scan", return_value=1):
        result = await handle_dockerfile_scan({"path": "/tmp/bad/Dockerfile"})
        assert result["exit_code"] == 1
```

### 11.2 Integration Tests

Test full MCP protocol flow (client → server → tool → response):

```python
# tests/test_mcp_e2e.py

import pytest
from mcp.client import ClientSession
from mcp.client.stdio import stdio_client

@pytest.mark.asyncio
async def test_list_tools():
    """Server returns all 18 tools."""
    async with stdio_client("bq", ["--mcp"]) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 18

@pytest.mark.asyncio
async def test_call_build_doctor():
    """build_doctor returns structured result."""
    async with stdio_client("bq", ["--mcp"]) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("build_doctor", {})
            data = json.loads(result.content[0].text)
            assert "checks" in data
```

### 11.3 Test Matrix

| Test Type | Scope | Count | CI? |
|-----------|-------|-------|-----|
| Unit | Tool handlers (mocked) | 36 (2 per tool) | Yes |
| Integration | MCP protocol flow | 5 | Yes |
| E2E | Claude Code → MCP → bq | 3 | Manual |

---

## 12. Security Considerations

### 12.1 Sensitive Data

| Data | Masking Strategy |
|------|-----------------|
| `GITHUB_TOKEN` | Show first 4 chars only: `ghp_xxxx...` |
| `PB_API_PASS` | Never exposed via MCP |
| `WEBHOOK_TRIGGER_TOKEN` | Never exposed via MCP |
| SOPS age private key | Never touched by MCP server |

### 12.2 Action Authorization

- T3 tools memiliki `annotations.confirmationRequired = true`
- MCP client (Claude Code) bertanggung jawab atas confirmation UX
- MCP server **tidak** menambahkan layer confirmation sendiri (single responsibility)

### 12.3 Transport Security

- **stdio**: inherits process-level security (same user)
- **SSE**: harus dilindungi bearer token (`--serve-token`)
- **Network**: default bind `127.0.0.1` (localhost only)

---

## 13. Future Extensions

| Extension | Deskripsi | Prioritas |
|-----------|-----------|-----------|
| **MCP Sampling** | Server memanggil Jev untuk severity synthesis in-band | P2 |
| **Bulk scan** | Scan semua Dockerfile di org via GitHub API | P2 |
| **Notifications** | Push notification saat pipeline selesai | P3 |
| **Multi-cluster** | Context switching antar K8s cluster | P3 |
| **Audit log** | Log semua MCP tool calls ke file | P2 |

---

## 14. Success Metrics

| Metric | Target | Cara Ukur |
|--------|--------|-----------|
| Tool coverage | 18/18 tools callable | Integration test |
| Response time (T1 tools) | < 5 detik | Benchmark |
| Response time (T3 tools) | < 60 detik | Benchmark |
| Zero breaking changes | CLI `bq` tetap 100% kompatibel | Existing test suite |
| Adoption | Digunakan di 3+ Claude Code sessions/hari | Audit log |

---

## Appendix A: Quick Start (Post-Implementation)

### Install

```bash
# Existing bq users
pipx inject build-q mcp

# New install
pipx install 'build-q[mcp]'
```

### Configure Claude Code

```json
// .claude/settings.json atau claude_desktop_config.json
{
  "mcpServers": {
    "bq": {
      "command": "bq",
      "args": ["--mcp"],
      "env": {
        "BQ_HOME": "~/.build-q"
      }
    }
  }
}
```

### Verify

```bash
# Test MCP server starts
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{}}}' | bq --mcp

# Test via Claude Code
# User: "scan Dockerfile di repo pay-be-topup-manager"
# Claude: (calls dockerfile_scan_remote via MCP)
```

### SSE Mode (Remote)

```bash
bq --mcp --transport sse --port 3001 --token mysecret
```

---

## Appendix B: Mapping CLI Flags → MCP Tools

| CLI Flag | MCP Tool | Notes |
|----------|----------|-------|
| `bq --fix-dockerfile --scan-only` | `dockerfile_scan` | scan_only implicit |
| `bq --fix-dockerfile` | `dockerfile_fix` | auto-fix enabled |
| `bq --fix-dockerfile --remote-repo X` | `dockerfile_scan_remote` | |
| `bq --anomaly-scan` | `k8s_anomaly_scan` | |
| `bq --doctor` | `build_doctor` | |
| `bq --config` | `config_show` | |
| `bq --check` | `pipeline_check` | |
| `bq repo ref` | `docker_build` | |
| `bq --dry-run` | `docker_build_preview` | |
| `bq --cicd-trigger` | `pipeline_trigger` | |
| `bq --cicd-webhook` | `webhook_status` | |
| `bq --gitops-set-image` | `gitops_set_image` | |
| `bq --is-match-image` | `gitops_match_check` | |
| `bq --bootstrap-k8s` | `gitops_bootstrap` | |
| `bq --set-image` | `k8s_set_image` | |
| `bq --sops-encrypt` | `sops_encrypt` | |
| `bq --sops-decrypt` | `sops_decrypt` | |
| `bq` (image exists check) | `image_check` | extracted helper |
