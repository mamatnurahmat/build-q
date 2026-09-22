"""File templates for `bq --init-jx` / `bq --init-legacy` / `bq --gh-action-init`.

**Sumber kebenaran**: central gist di
https://gist.github.com/mamatnurahmat/35cc4c36e7c7c2d236a1b5149cdbcfd9
(6 file: Makefile.modern, compose.yaml.modern, Dockerfile.modern,
Makefile.legacy, compose.yaml.legacy, trigger-ci.yml).

`load_template(name)` mengambil versi terbaru dari gist raw URL, cache di
`~/.build-q/templates/*` selama TTL (default 3600 detik), dan fallback ke
konstanta `_FALLBACK_*` yang di-bundle di file ini bila gist tidak dapat
dijangkau (offline / rate-limit / block).

Placeholder pakai `{{NAME}}` (biar tidak konflik dengan `${VAR}` shell/docker
atau `${{ }}` GitHub Actions), lalu di-substitusi via `render(tpl, ctx)`.

Update / perubahan template: EDIT DI GIST (via `gh gist edit <id>`),
bukan di file ini. `_FALLBACK_*` di sini adalah snapshot untuk offline
resilience — tidak wajib sinkron persis, tapi disarankan di-refresh saat
release baru `bq` supaya user offline tetap dapat template yang wajar.
"""
from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional


GIST_USER = os.environ.get("BUILD_Q_GIST_USER", "mamatnurahmat")
GIST_ID = os.environ.get("BUILD_Q_GIST_ID", "35cc4c36e7c7c2d236a1b5149cdbcfd9")
GIST_CACHE_TTL = int(os.environ.get("BUILD_Q_GIST_TTL", "3600"))  # seconds
GIST_TIMEOUT = int(os.environ.get("BUILD_Q_GIST_TIMEOUT", "10"))  # seconds

CACHE_DIR = Path.home() / ".build-q" / "templates"


# Mapping nama logis (dipakai builder.py) → nama file di gist.
TEMPLATE_FILES: Dict[str, str] = {
    "makefile":         "Makefile.modern",
    "compose":          "compose.yaml.modern",
    "dockerfile":       "Dockerfile.modern",
    "makefile_legacy":  "Makefile.legacy",
    "compose_legacy":   "compose.yaml.legacy",
    "trigger_ci":       "trigger-ci.yml",
}


def render(tpl: str, ctx: dict) -> str:
    """Substitute {{KEY}} placeholders in template using ctx dict."""
    result = tpl
    for key, value in ctx.items():
        result = result.replace(f"{{{{{key}}}}}", str(value))
    return result


def _gist_raw_url(filename: str) -> str:
    return f"https://gist.githubusercontent.com/{GIST_USER}/{GIST_ID}/raw/{filename}"


def _cache_path(filename: str) -> Path:
    return CACHE_DIR / filename


def _fetch_from_gist(filename: str) -> Optional[str]:
    """Fetch template content from gist raw URL. Returns None on any failure."""
    url = _gist_raw_url(filename)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "build-q"})
        with urllib.request.urlopen(req, timeout=GIST_TIMEOUT) as r:
            if r.status != 200:
                return None
            return r.read().decode("utf-8")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None


def _read_cache(filename: str) -> Optional[str]:
    p = _cache_path(filename)
    if not p.exists():
        return None
    try:
        return p.read_text()
    except OSError:
        return None


def _write_cache(filename: str, content: str) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(filename).write_text(content)
    except OSError:
        pass  # cache adalah best-effort


def _cache_is_fresh(filename: str) -> bool:
    p = _cache_path(filename)
    if not p.exists():
        return False
    try:
        age = time.time() - p.stat().st_mtime
        return age < GIST_CACHE_TTL
    except OSError:
        return False


def load_template(name: str) -> str:
    """Ambil template — gist first (dengan cache TTL), fallback ke bundled.

    Alur:
      1. Bila cache masih fresh (< TTL), langsung pakai cache.
      2. Coba fetch dari gist raw URL. Bila sukses → refresh cache + return.
      3. Fallback ke cache (walau expired) bila ada.
      4. Fallback terakhir: konstanta `_FALLBACK_*` di modul ini.

    Args:
      name: kunci di `TEMPLATE_FILES` (mis. "makefile", "compose", "dockerfile",
            "makefile_legacy", "compose_legacy", "trigger_ci").

    Raises:
      KeyError bila nama tidak dikenal.
    """
    if name not in TEMPLATE_FILES:
        raise KeyError(f"Unknown template name: {name!r}")
    filename = TEMPLATE_FILES[name]

    # 1. Cache fresh → pakai langsung (hemat network round-trip).
    if _cache_is_fresh(filename):
        cached = _read_cache(filename)
        if cached is not None:
            return cached

    # 2. Coba fetch dari gist.
    remote = _fetch_from_gist(filename)
    if remote is not None:
        _write_cache(filename, remote)
        return remote

    # 3. Fallback ke cache walau expired.
    cached = _read_cache(filename)
    if cached is not None:
        print(
            f"⚠️  Gist unreachable; pakai template cache lama untuk {filename} "
            f"(cache: {_cache_path(filename)})"
        )
        return cached

    # 4. Fallback terakhir: bundled snapshot di modul ini.
    bundled = _BUNDLED_FALLBACK.get(name)
    if bundled is None:
        raise RuntimeError(
            f"Cannot load template {name!r}: gist unreachable, no cache, no bundled fallback."
        )
    print(
        f"⚠️  Gist unreachable dan cache kosong; pakai bundled fallback untuk {filename}. "
        f"Jalankan online sekali untuk sync cache dari gist."
    )
    return bundled


# ============================================================
# Bundled fallback snapshots — dipakai HANYA bila gist tidak dapat dijangkau
# dan cache kosong. Konstanta di bawah dipertahankan sebagai `_BUNDLED_*` yang
# didaftarkan di `_BUNDLED_FALLBACK` di bawah. Nama publik lama
# (`MAKEFILE_TPL`, `COMPOSE_TPL`, ...) juga dipertahankan sebagai alias untuk
# menjaga backward compatibility bila ada kode eksternal yang import langsung.
# ============================================================

_BUNDLED_MAKEFILE = """\
# ============================================================
# {{IMAGE}} — Makefile build / release (Backend Go)
# ============================================================
# Usage:
#   make develop                 Build image develop
#   make staging                 Build + push staging
#   make sandbox                 Build + push sandbox
#   make production              Build + push production
#   make build ENV=<env>         Build only  (env: develop|staging|sandbox|production)
#   make release ENV=<env>       Push only
#   make run ENV=<env>           Run locally
#
# ENV values MATCH pipeline Tekton (~/jenkins-x/pipeline/lighthouse-config/
# .lighthouse/jenkins-x/triggers.yaml) — jangan pakai nilai lain agar
# .env.<env> yang di-COPY Dockerfile konsisten dengan yang dipakai runtime JX.
#
# Image tag resolution:
#   HEAD di tag git (v1.2.3)  →  v1.2.3
#   HEAD di branch            →  <short-commit>
#   Bukan repo git            →  latest
# ============================================================

SHELL := /bin/sh

ORG_REGISTRY ?= {{ORG_REGISTRY}}
IMAGE_NAME   ?= {{IMAGE}}
IMAGE_TAG    ?= $(shell git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD 2>/dev/null || echo latest)
ENV          ?= staging
PORT         ?= {{PORT}}
PROJECT      ?= {{PROJECT}}
CLUSTER      ?= {{CLUSTER}}
DEPLOYMENT   ?= {{DEPLOYMENT}}
NODETYPE     ?= {{NODETYPE}}

# GitHub creds untuk private Go module (github.com/Qoin-Digital-Indonesia/*).
# Compose secrets: netrc mount `$HOME/.netrc` — target `netrc` di bawah
# auto-generate file itu supaya build jalan di Tekton CI (yang tidak punya
# .netrc secara default) dan di local dev tanpa perlu manual setup.
#
#   CI Tekton  : GIT_USER + GIT_TOKEN dari secret `tekton-git` (auto-inject).
#   Local dev  : fallback ke `git config user.name` + `gh auth token`.
GITHUB_USER  ?= $(or $(GIT_USER),$(shell git config user.name 2>/dev/null),qoin-bot)
GITHUB_TOKEN ?= $(or $(GIT_TOKEN),$(shell gh auth token 2>/dev/null))

export DOCKER_BUILDKIT := 1
export COMPOSE_DOCKER_CLI_BUILD := 1

.PHONY: help develop staging sandbox production build release run netrc

## Show help
help:
	@echo ""
	@echo "$(IMAGE_NAME) — Backend Docker build/release"
	@echo "=================================================="
	@echo "  make develop            Build image develop"
	@echo "  make staging            Build + push staging"
	@echo "  make sandbox            Build + push sandbox"
	@echo "  make production         Build + push production"
	@echo "  make build ENV=<env>    Build image (env: develop|staging|sandbox|production)"
	@echo "  make release ENV=<env>  Push image"
	@echo "  make run ENV=<env>      Run container locally"
	@echo ""

## Full flow shortcuts (ENV values MATCH pipeline Tekton triggers.yaml)
develop:
	@$(MAKE) build ENV=develop
	@$(MAKE) release ENV=develop

staging:
	@$(MAKE) build ENV=staging
	@$(MAKE) release ENV=staging

sandbox:
	@$(MAKE) build ENV=sandbox
	@$(MAKE) release ENV=sandbox

production:
	@$(MAKE) build ENV=production
	@$(MAKE) release ENV=production

## Auto-generate $HOME/.netrc dari GITHUB_USER + GITHUB_TOKEN.
## Dipakai compose secret + Dockerfile `RUN --mount=type=secret,id=netrc,...`.
## Wajib di CI (Tekton runner tidak punya netrc); no-op di local kalau sudah ada.
netrc:
	@if [ -z "$(GITHUB_TOKEN)" ]; then \
		echo ">> ❌ GITHUB_TOKEN kosong. CI: pastikan secret tekton-git di-mount. Local: 'gh auth login' atau export GITHUB_TOKEN=<token>"; \
		exit 1; \
	fi
	@if [ ! -f "$$HOME/.netrc" ]; then \
		echo ">> ℹ️  Generate $$HOME/.netrc dari GITHUB_USER + GITHUB_TOKEN"; \
		printf "machine github.com login %s password %s\n" "$(GITHUB_USER)" "$(GITHUB_TOKEN)" > "$$HOME/.netrc" && chmod 600 "$$HOME/.netrc"; \
	fi

## Build image (BuildKit + secret mount from ~/.netrc)
build: netrc
	@echo ">> Build $(ENV) image: $(ORG_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)"
	ORG_REGISTRY=$(ORG_REGISTRY) \
	IMAGE_NAME=$(IMAGE_NAME) \
	IMAGE_TAG=$(IMAGE_TAG) \
	BUILD_ENV=$(ENV) \
	PORT=$(PORT) \
	PROJECT=$(PROJECT) \
	CLUSTER=$(CLUSTER) \
	DEPLOYMENT=$(DEPLOYMENT) \
	NODETYPE=$(NODETYPE) \
	docker compose build

## Push image to registry
release:
	@echo ">> Push $(ENV) image: $(ORG_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)"
	ORG_REGISTRY=$(ORG_REGISTRY) \
	IMAGE_NAME=$(IMAGE_NAME) \
	IMAGE_TAG=$(IMAGE_TAG) \
	BUILD_ENV=$(ENV) \
	docker compose push

## Run container locally
run:
	@echo ">> Run $(ENV): $(ORG_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)"
	ORG_REGISTRY=$(ORG_REGISTRY) \
	IMAGE_NAME=$(IMAGE_NAME) \
	IMAGE_TAG=$(IMAGE_TAG) \
	BUILD_ENV=$(ENV) \
	PORT=$(PORT) \
	docker compose up
"""


_BUNDLED_COMPOSE = """\
# ============================================================
# compose.yaml — Docker Compose (CI/CD + local build)
# ============================================================
# Driven by Makefile targets:
#   make build ENV=<env>    →  docker compose build
#   make release ENV=<env>  →  docker compose push
#   make run ENV=<env>      →  docker compose up
#
# BuildKit secret `netrc` mounts $HOME/.netrc into the build stage
# for private Go module auth — never stored in image layers.
# ============================================================
services:
  app:
    platform: linux/amd64
    build:
      context: .
      dockerfile: Dockerfile
      args:
        BRANCH: ${BUILD_ENV:-staging}
        PROJECT: ${PROJECT:-{{PROJECT}}}
        PORT: ${PORT:-{{PORT}}}
      secrets:
        - netrc
    image: ${ORG_REGISTRY:-{{ORG_REGISTRY}}}/${IMAGE_NAME:-{{IMAGE}}}:${IMAGE_TAG:-latest}
    container_name: ${IMAGE_NAME:-{{IMAGE}}}-${BUILD_ENV:-staging}
    restart: "no"
    ports:
      - "${PORT:-{{PORT}}}:${PORT:-{{PORT}}}"

secrets:
  netrc:
    file: ${HOME}/.netrc
"""


_BUNDLED_DOCKERFILE = """\
# syntax=docker/dockerfile:1.4
# ============================================================
# Multi-stage Go build with BuildKit secret mount
# ------------------------------------------------------------
# Requires BuildKit. Build with:
#   docker buildx build --secret id=netrc,src=$HOME/.netrc ...
# or via `bq` (auto-injects the secret) or `docker compose build`.
# ============================================================

FROM golang:1.22-alpine AS builder

ARG BRANCH
ARG PORT
ARG PROJECT

RUN apk add --no-cache git gcc g++ tzdata zip ca-certificates

RUN mkdir -p /go/src/${PROJECT}
WORKDIR /go/src/${PROJECT}

COPY go.mod go.sum ./
RUN --mount=type=secret,id=netrc,target=/root/.netrc \
    GOPRIVATE=github.com/Qoin-Digital-Indonesia/* go mod download && go mod tidy

COPY . .
COPY .env.${BRANCH} .env

RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -a -installsuffix cgo -o /go/bin/${PROJECT} server.go

# ------------------------------------------------------------
FROM alpine:latest
ARG BRANCH
ARG PORT
ARG PROJECT

ENV TIMEZONE=Asia/Jakarta
RUN apk --no-cache add tzdata ca-certificates && \
    cp /usr/share/zoneinfo/${TIMEZONE} /etc/localtime && \
    echo "${TIMEZONE}" > /etc/timezone

EXPOSE ${PORT}

COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=builder /go/bin/${PROJECT} .
COPY .env.${BRANCH} .env

RUN printf "#!/bin/sh\n\nwhile true; do\n\techo \"[INFO] Starting Service at \$(date)\"\n\t(./${PROJECT} >> ./history.log || echo \"[ERROR] Restarting Service at \$(date)\")\ndone" > run.sh && \
    printf "#!/bin/sh\n./run.sh & tail -F ./history.log" > up.sh && \
    chmod +x up.sh run.sh

CMD ["./up.sh"]
"""


_BUNDLED_MAKEFILE_LEGACY = """\
# ============================================================
# {{IMAGE}} — Makefile build / release (legacy netrc via build-args)
# ============================================================
# Pola legacy: Dockerfile menerima ARG GITHUB_USER + GITHUB_TOKEN
# untuk `go mod download` repo private (Qoin-Digital-Indonesia/*).
# Local dev: token auto-diambil dari `gh auth token`.
# CI/CD    : override COMPOSE_FILE + inject GITHUB_TOKEN dari secret.
#
# Usage:
#   make build ENV=staging       Build image staging
#   make build ENV=production    Build image production
#   make release ENV=staging     Push image staging
#   make release ENV=production  Push image production
#   make staging                 Build + release staging
#   make production              Build + release production
#   make run ENV=staging         Build + jalankan container
#   make down                    Stop container
#   make help                    Daftar perintah
#
# Image tag (IMAGE_TAG):
#   HEAD tepat di tag git (v1.2.3)  →  v1.2.3
#   HEAD di branch                  →  <commit-id>
#   bukan repo git                  →  latest
#
# Compose file:
#   make build ENV=staging                              → local (compose.yaml)
#   make build ENV=staging COMPOSE_FILE=build.compose   → CI/CD
# ============================================================

SHELL := /bin/sh

ORG_REGISTRY ?= {{ORG_REGISTRY}}
IMAGE_NAME   ?= {{IMAGE}}
IMAGE_TAG    ?= $(shell git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD 2>/dev/null || echo latest)
ENV          ?= staging
PORT         ?= {{PORT}}
PROJECT      ?= {{PROJECT}}

# Compose file: default compose.yaml (local), override build.compose (CI/CD)
COMPOSE_FILE ?= compose.yaml
ENV_FILE     := .env.$(ENV)

# GitHub creds untuk go mod download private repo (github.com/Qoin-Digital-Indonesia/*)
# CI/CD JX runner : GIT_USER + GIT_TOKEN dari secret tekton-git (auto-inject)
# Local dev       : fallback ke `git config user.name` + `gh auth token`
GITHUB_USER  ?= $(or $(GIT_USER),$(shell git config user.name 2>/dev/null),qoin-bot)
GITHUB_TOKEN ?= $(or $(GIT_TOKEN),$(shell gh auth token 2>/dev/null))

# BuildKit wajib supaya `RUN --mount=type=secret,id=netrc` bekerja di compose build
export DOCKER_BUILDKIT := 1
export COMPOSE_DOCKER_CLI_BUILD := 1

.PHONY: help develop staging sandbox production build release run down check-env netrc

## Tampilkan bantuan
help:
	@echo ""
	@echo "$(IMAGE_NAME) — generic Docker build/release (legacy netrc)"
	@echo "=========================================================="
	@echo ""
	@echo "Usage: make <target> ENV=<env>  (env: develop | staging | sandbox | production)"
	@echo ""
	@echo "  make build ENV=develop       Build image develop"
	@echo "  make build ENV=staging       Build image staging"
	@echo "  make build ENV=sandbox       Build image sandbox"
	@echo "  make build ENV=production    Build image production"
	@echo "  make release ENV=<env>       Push image"
	@echo "  make run ENV=<env>           Build + jalankan container"
	@echo "  make down                    Stop container"
	@echo "  make help                    Bantuan ini"
	@echo ""
	@echo "ENV MATCH pipeline Tekton (~/jenkins-x/pipeline). Nilai selain 4"
	@echo "di atas otomatis fallback ke 'staging' oleh runtime JX."
	@echo ""
	@echo "CI/CD: override COMPOSE_FILE"
	@echo "  make build ENV=staging COMPOSE_FILE=build.compose"
	@echo ""

## Full flow: develop (build + release)
develop:
	@$(MAKE) build ENV=develop
	@$(MAKE) release ENV=develop

## Full flow: staging (build + release)
staging:
	@$(MAKE) build ENV=staging
	@$(MAKE) release ENV=staging

## Full flow: sandbox (build + release)
sandbox:
	@$(MAKE) build ENV=sandbox
	@$(MAKE) release ENV=sandbox

## Full flow: production (build + release)
production:
	@$(MAKE) build ENV=production
	@$(MAKE) release ENV=production

## Validasi file .env.{ENV} (warning only)
check-env:
	@if [ ! -f "$(ENV_FILE)" ]; then \
		echo ">> ⚠️  $(ENV_FILE) tidak ditemukan (pakai default Dockerfile)"; \
	fi

## Auto-generate $HOME/.netrc bila belum ada (dipakai compose secret + Dockerfile --mount=type=secret,id=netrc)
netrc:
	@if [ -z "$(GITHUB_TOKEN)" ]; then \
		echo ">> ❌ GITHUB_TOKEN kosong. Jalankan 'gh auth login' atau export GITHUB_TOKEN=<token>"; \
		exit 1; \
	fi
	@if [ ! -f "$$HOME/.netrc" ]; then \
		echo ">> ℹ️  Generate $$HOME/.netrc dari GITHUB_USER + GITHUB_TOKEN (needed by CI runner)"; \
		printf "machine github.com login %s password %s\n" "$(GITHUB_USER)" "$(GITHUB_TOKEN)" > "$$HOME/.netrc" && chmod 600 "$$HOME/.netrc"; \
	fi

## Build image Docker
build: netrc
	@echo ">> Build $(ENV) image: $(ORG_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) (compose: $(COMPOSE_FILE))"
	IMAGE_TAG=$(IMAGE_TAG) ORG_REGISTRY=$(ORG_REGISTRY) IMAGE_NAME=$(IMAGE_NAME) BUILD_ENV=$(ENV) PORT=$(PORT) \
	GITHUB_USER=$(GITHUB_USER) GITHUB_TOKEN=$(GITHUB_TOKEN) \
		docker compose -f $(COMPOSE_FILE) build

## Build + tag + push image ke registry
release:
	@echo ">> Push $(ENV) image: $(ORG_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)"
	@docker tag $(ORG_REGISTRY)/$(IMAGE_NAME):latest $(ORG_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) 2>/dev/null || true
	IMAGE_TAG=$(IMAGE_TAG) ORG_REGISTRY=$(ORG_REGISTRY) IMAGE_NAME=$(IMAGE_NAME) BUILD_ENV=$(ENV) \
		docker compose -f $(COMPOSE_FILE) push

## Build + jalankan container
run: check-env
	@echo ">> Run $(ENV): $(ORG_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) on port $(PORT)"
	IMAGE_TAG=$(IMAGE_TAG) ORG_REGISTRY=$(ORG_REGISTRY) IMAGE_NAME=$(IMAGE_NAME) BUILD_ENV=$(ENV) PORT=$(PORT) \
		docker compose -f $(COMPOSE_FILE) up -d

## Stop container
down:
	docker compose -f $(COMPOSE_FILE) down 2>/dev/null || true
"""


_BUNDLED_COMPOSE_LEGACY = """\
# ============================================================
# compose.yaml — Docker Compose (kompatibel legacy + modern)
# ============================================================
# Dipakai oleh Makefile legacy:
#   make build ENV=staging  /  make build ENV=production
#
# Mendukung DUA pola Dockerfile sekaligus:
#   - Legacy: pakai ARG GITHUB_USER/GITHUB_TOKEN (echo ke ~/.netrc)
#   - Modern: pakai `RUN --mount=type=secret,id=netrc` (hasil bq --fix-dockerfile)
# Keduanya bisa jalan bersamaan tanpa perubahan file lain.
#
# Variable (di-export oleh Makefile):
#   BUILD_ENV     → environment (staging | production)
#   IMAGE_NAME    → nama image
#   IMAGE_TAG     → tag image
#   ORG_REGISTRY  → Docker Hub registry
#   PORT          → port host
#   GITHUB_USER   → user untuk netrc auth (pola legacy)
#   GITHUB_TOKEN  → token untuk netrc auth (pola legacy)
# ============================================================
services:
  app:
    platform: linux/amd64
    build:
      context: .
      dockerfile: Dockerfile
      args:
        BUILD_ENV: ${BUILD_ENV:-production}
        BRANCH: ${BUILD_ENV:-staging}
        PROJECT: ${PROJECT_NAME:-{{PROJECT}}}
        GITHUB_USER: ${GIT_USER:-${GITHUB_USER:-qoin-bot}}
        GITHUB_TOKEN: ${GIT_TOKEN:-${GITHUB_TOKEN:-}}
      secrets:
        - netrc

    image: ${ORG_REGISTRY:-{{ORG_REGISTRY}}}/${IMAGE_NAME:-{{IMAGE}}}:${IMAGE_TAG:-latest}
    container_name: ${IMAGE_NAME:-{{IMAGE}}}-${BUILD_ENV:-staging}
    restart: "no"
    ports:
      - "${PORT:-{{PORT}}}:{{PORT}}"

secrets:
  netrc:
    file: ${HOME}/.netrc
"""


_BUNDLED_TRIGGER_CI = """\
# ====================================================================
# CI Trigger — GitHub Actions → Webhook Trigger (Jenkins X)
# ====================================================================
# Setup: add 2 repo secrets in GitHub:
#   Settings → Secrets and variables → Actions
#     WEBHOOK_TRIGGER_URL   = https://cicd-hw.qoin.id/trigger
#     WEBHOOK_TRIGGER_TOKEN = (from cluster: kubectl get secret webhook-trigger-token -n jenkins-x)
# ====================================================================
name: CI Trigger

on:
  push:
    branches:
      - develop
      - development
      - staging
      - sandbox
      - master
      - main
    tags:
      - 'v[0-9]+.[0-9]+.[0-9]+*'

jobs:
  trigger:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger Jenkins X Pipeline
        env:
          REPO: ${{ github.event.repository.name }}
          REF: ${{ github.ref_name }}
          EVENT: ${{ github.event_name }}
          DELETED: ${{ github.event.deleted }}
          WEBHOOK_URL: ${{ secrets.WEBHOOK_TRIGGER_URL }}
          WEBHOOK_TOKEN: ${{ secrets.WEBHOOK_TRIGGER_TOKEN }}
        run: |
          set -e

          echo "╔════════════════════════════════════════════╗"
          echo "║  🚀 GitHub Actions → Jenkins X Pipeline   ║"
          echo "╠════════════════════════════════════════════╣"
          echo "║  Repo   : ${REPO}"
          echo "║  Ref    : ${REF}"
          echo "║  Event  : ${EVENT}"
          echo "╚════════════════════════════════════════════╝"

          if [ "${DELETED}" = "true" ]; then
            echo "⏭️  Skip: branch/tag deleted"
            exit 0
          fi

          if [ -z "${WEBHOOK_URL}" ] || [ -z "${WEBHOOK_TOKEN}" ]; then
            echo "❌ ERROR: WEBHOOK_TRIGGER_URL or WEBHOOK_TRIGGER_TOKEN not set."
            echo "   Setup: Settings → Secrets and variables → Actions → New repository secret"
            exit 1
          fi

          echo "==> POST ${WEBHOOK_URL}"
          RESP=$(curl -s -w "\n%{http_code}" -X POST "${WEBHOOK_URL}" \
            -H "Authorization: Bearer ${WEBHOOK_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "{\"repo\":\"${REPO}\",\"ref\":\"${REF}\"}")

          HTTP_CODE=$(echo "$RESP" | tail -1)
          BODY=$(echo "$RESP" | sed '$d')
          echo "   HTTP ${HTTP_CODE}: ${BODY}"

          if [ "${HTTP_CODE}" = "201" ]; then
            PIPELINE=$(echo "${BODY}" | grep -o '"pipelinerun":"[^"]*"' | cut -d'"' -f4 || echo "unknown")
            VISUALIZER=$(echo "${BODY}" | grep -o '"visualizer":"[^"]*"' | cut -d'"' -f4 || echo "")
            echo "✅ PipelineRun: ${PIPELINE}"
            if [ -n "${VISUALIZER}" ]; then
              echo "🔗 ${VISUALIZER}"
            else
              echo "🔗 https://cicd-hw.qoin.id/visualizer/"
            fi
          else
            echo "❌ Trigger failed"
            exit 1
          fi
"""


# ============================================================
# Registry fallback — dipakai load_template() sebagai layer terakhir bila
# gist + cache tidak tersedia.
# ============================================================
_BUNDLED_FALLBACK: Dict[str, str] = {
    "makefile":        _BUNDLED_MAKEFILE,
    "compose":         _BUNDLED_COMPOSE,
    "dockerfile":      _BUNDLED_DOCKERFILE,
    "makefile_legacy": _BUNDLED_MAKEFILE_LEGACY,
    "compose_legacy":  _BUNDLED_COMPOSE_LEGACY,
    "trigger_ci":      _BUNDLED_TRIGGER_CI,
}


# ============================================================
# Backward-compat aliases — modul lama import langsung, tapi baru
# sebaiknya panggil `load_template(name)`.
# ============================================================
MAKEFILE_TPL         = _BUNDLED_MAKEFILE
COMPOSE_TPL          = _BUNDLED_COMPOSE
DOCKERFILE_TPL       = _BUNDLED_DOCKERFILE
MAKEFILE_LEGACY_TPL  = _BUNDLED_MAKEFILE_LEGACY
COMPOSE_LEGACY_TPL   = _BUNDLED_COMPOSE_LEGACY
TRIGGER_CI_TPL       = _BUNDLED_TRIGGER_CI
