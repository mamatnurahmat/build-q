# Jev Web Chat — Docker Setup

Deploy **`bq --serve 8888`** (Jev Agent Planner web chat) sebagai container Docker dgn **Docker-outside-of-Docker** (DooD) — container punya **akses FULL** ke Docker daemon host via mount `/var/run/docker.sock`.

Mode ini cocok untuk:
- Tim DevOps share Jev lewat satu URL internal (no local install per laptop)
- CI runner yang perlu jalanin `bq --remote` build, `bq --bootstrap-k8s`, dll
- Jump host / bastion yang jadi gateway tunggal ke infra

## Isi folder

| File | Fungsi |
|---|---|
| `Dockerfile` | Multi-stage build: downloader (docker/kubectl/sops/age/gh) + runtime (python 3.13 + build-q) |
| `compose.yaml` | Service definition dgn volumes, env, port, healthcheck |
| `.env.example` | Template semua env var yg dibutuhkan |
| `.dockerignore` | Minimize build context |

## Quick Start

```bash
cd docker/
cp .env.example .env

# Edit .env — minimum isi:
#   TYPESAFE_API_KEY   (atau OPENROUTER_API_KEY)
#   PB_API_USER + PB_API_PASS  (atau GITHUB_TOKEN manual)
#   BQ_SERVE_TOKEN     (recommend saat expose network)
vi .env

docker compose up -d --build
docker compose logs -f jev-chat

# Verifikasi
curl http://localhost:8888/api/health

# Buka browser (localhost)
open http://localhost:8888

# Dari laptop lain di LAN (butuh BQ_SERVE_TOKEN)
open "http://10.1.2.3:8888/?token=<BQ_SERVE_TOKEN>"
```

## Tools yang disertakan (semua versi latest)

| Tool | Versi default | Fungsi |
|---|---|---|
| `python` | 3.13-slim-bookworm | Runtime |
| `build-q` | 0.1.42 (dari PyPI) | Utama + anomaly extras (pyyaml) |
| `docker` (client) | 27.4.1 | Talk to host daemon via sock |
| `docker buildx` | latest | Build image |
| `docker compose` | latest | `bq --compose` |
| `kubectl` | v1.32.0 | `bq --set-image`, `bq --apply-secret` |
| `sops` | v3.9.1 | `bq --sops-encrypt/decrypt` |
| `age` + `age-keygen` | v1.2.0 | SOPS encryption key |
| `gh` | 2.63.2 | Opt-in (`GH_CLI=true`) |
| `git` | debian stable | Clone, push, PR |
| `tini` | debian stable | PID 1 (reap zombies, handle SIGTERM) |

Override versi via build-arg:
```bash
docker build \
  --build-arg BUILD_Q_VERSION=0.1.42 \
  --build-arg DOCKER_VERSION=28.0.0 \
  --build-arg KUBECTL_VERSION=v1.33.0 \
  -t build-q:custom -f docker/Dockerfile .
```

## Volumes (persistent + sock mount)

| Mount | Mode | Fungsi |
|---|---|---|
| `/var/run/docker.sock` | RW | **DooD** — container pakai Docker daemon host (akses FULL) |
| `~/.kube/config` → `/root/.kube/config` | RO | kubectl context dari host |
| `~/.config/sops` → `/root/.config/sops` | RO | SOPS age keys |
| `~/.ssh` → `/root/.ssh` | RO | SSH key untuk git clone SSH |
| `~/.docker/config.json` → `/root/.docker/config.json` | RO | Registry auth sinkron dgn host |
| named `jev-chat-config` → `/root/.build-q` | RW | TUI cache, PB cache, audit log |
| named `jev-chat-workspace` → `/workspace` | RW | git clone, gitops operations |

## Env Vars (singkat — lengkap di `.env.example`)

### WAJIB (minimum)
```bash
TYPESAFE_API_KEY=sk-ts-...      # atau OPENROUTER_API_KEY
PB_API_USER=admin@qoin.id       # jika PB_API=true
PB_API_PASS=<password>
```

### Recommend untuk LAN exposure
```bash
BQ_SERVE_TOKEN=$(openssl rand -hex 24)  # Bearer auth
```

### Demo / screencast
```bash
BQ_SERVE_NO_EXEC=true    # read-only, tolak /api/execute
```

## Operations

```bash
# Status
docker compose ps
docker compose logs -f jev-chat
docker compose top jev-chat

# Restart (reload env setelah edit .env)
docker compose up -d --force-recreate

# Shell masuk ke container
docker compose exec jev-chat sh

# Check cache + audit log
docker compose exec jev-chat ls -la /root/.build-q
docker compose exec jev-chat tail -20 /root/.build-q/.serve-audit.log

# Update ke versi baru
# 1. edit .env — ubah BUILD_Q_VERSION=0.1.43
# 2. rebuild
docker compose build --no-cache
docker compose up -d

# Stop + hapus (data persistent di named volumes TETAP ada)
docker compose down

# Full cleanup (termasuk volumes)
docker compose down -v
```

## Security Checklist

Sebelum expose ke network, pastikan:

- [ ] `BQ_SERVE_TOKEN` di-set (bukan empty) → auth wajib
- [ ] Firewall / Security Group hanya izinkan dari IP tim DevOps
- [ ] `GITHUB_TOKEN` punya scope minimal yang dibutuhkan (bukan full access)
- [ ] `PB_API_PASS` di-rotate berkala
- [ ] Audit log reviewed berkala: `docker compose exec jev-chat tail /root/.build-q/.serve-audit.log`
- [ ] Kalau production: ganti ke image dgn user non-root + readonly rootfs + resource limits
- [ ] Backup `jev-chat-workspace` volume (bisa berisi gitops repo in-progress)

## Troubleshooting

### Container start tapi `/api/health` fail
```bash
docker compose logs jev-chat | tail -30
# Cek: provider API key diset? PB_API_USER valid?
```

### `bq --remote` build fail di container
```bash
# Verifikasi socket mount
docker compose exec jev-chat docker ps
# Harus list container host (bukan error)

# Verifikasi buildx builder
docker compose exec jev-chat docker buildx ls
```

### `kubectl` error unauthorized
```bash
# Mount kubeconfig biasanya butuh adjust karena path cert file berbeda.
# Cek:
docker compose exec jev-chat kubectl config view
# Perbaiki: pakai `KUBECONFIG_HOST=/path/to/alt/kubeconfig` di .env
```

### SSE streaming terputus di proxy
Behind nginx/traefik, pastikan:
- `proxy_buffering off;`
- `proxy_read_timeout 300s;` (atau lebih untuk long build)
- Header `X-Accel-Buffering: no` sudah di-set server (sudah default)

## Deployment Patterns

### A. Dev laptop (localhost only)
```yaml
# compose.yaml override
services:
  jev-chat:
    ports:
      - "127.0.0.1:8888:8888"
```

### B. Shared tim (LAN internal)
Set `BQ_SERVE_TOKEN` + firewall rules. Share token via Slack DM/1Password.

### C. Behind traefik (opsional)
```yaml
# tambah labels ke compose.yaml
labels:
  traefik.enable: "true"
  traefik.http.routers.jev.rule: "Host(`jev.internal.qoin.id`)"
  traefik.http.routers.jev.tls: "true"
  traefik.http.services.jev.loadbalancer.server.port: "8888"
```

### D. CI runner (ephemeral)
```bash
# One-shot: scan + exit
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v $(pwd)/gitops:/workspace:ro \
  -e PB_API=true -e PB_API_USER=... -e PB_API_PASS=... \
  build-q:0.1.42 \
  bq --anomaly-scan /workspace/cce/production-qoin --export-md /tmp/report.md
```

## Related

- [`PRD-chat.md`](../PRD-chat.md) — PRD lengkap `bq --serve`
- [`PRD-tui.md`](../PRD-tui.md) — PRD terminal mode `bq --tui`
- [`RELEASE_NOTES/v0.1.42.md`](../RELEASE_NOTES/v0.1.42.md) — fitur rilis ini
