"""File templates for `bq --init-jx` scaffolding.

Placeholders use `{{NAME}}` syntax so they don't collide with shell/docker `${VAR}`
or Makefile `$(VAR)` expansions.
"""


def render(tpl: str, ctx: dict) -> str:
    """Substitute {{KEY}} placeholders in template using ctx dict."""
    result = tpl
    for key, value in ctx.items():
        result = result.replace(f"{{{{{key}}}}}", str(value))
    return result


MAKEFILE_TPL = """\
# ============================================================
# {{IMAGE}} — Makefile build / release (Backend Go)
# ============================================================
# Usage:
#   make develop                 Build image develop
#   make staging                 Build + push staging
#   make production              Build + push production
#   make build ENV=<env>         Build only
#   make release ENV=<env>       Push only
#   make run ENV=<env>           Run locally
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

export DOCKER_BUILDKIT := 1
export COMPOSE_DOCKER_CLI_BUILD := 1

.PHONY: help develop staging production build release run

## Show help
help:
	@echo ""
	@echo "$(IMAGE_NAME) — Backend Docker build/release"
	@echo "=================================================="
	@echo "  make develop            Build image develop"
	@echo "  make staging            Build + push staging"
	@echo "  make production         Build + push production"
	@echo "  make build ENV=<env>    Build image"
	@echo "  make release ENV=<env>  Push image"
	@echo "  make run ENV=<env>      Run container locally"
	@echo ""

## Full flow shortcuts
develop:
	@$(MAKE) build ENV=develop

staging:
	@$(MAKE) build ENV=staging
	@$(MAKE) release ENV=staging

production:
	@$(MAKE) build ENV=production
	@$(MAKE) release ENV=production

## Build image (BuildKit + secret mount from ~/.netrc)
build:
	@echo ">> Build $(ENV) image: $(ORG_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)"
	ORG_REGISTRY=$(ORG_REGISTRY) \\
	IMAGE_NAME=$(IMAGE_NAME) \\
	IMAGE_TAG=$(IMAGE_TAG) \\
	BUILD_ENV=$(ENV) \\
	PORT=$(PORT) \\
	PROJECT=$(PROJECT) \\
	CLUSTER=$(CLUSTER) \\
	DEPLOYMENT=$(DEPLOYMENT) \\
	NODETYPE=$(NODETYPE) \\
	docker compose build

## Push image to registry
release:
	@echo ">> Push $(ENV) image: $(ORG_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)"
	ORG_REGISTRY=$(ORG_REGISTRY) \\
	IMAGE_NAME=$(IMAGE_NAME) \\
	IMAGE_TAG=$(IMAGE_TAG) \\
	BUILD_ENV=$(ENV) \\
	docker compose push

## Run container locally
run:
	@echo ">> Run $(ENV): $(ORG_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)"
	ORG_REGISTRY=$(ORG_REGISTRY) \\
	IMAGE_NAME=$(IMAGE_NAME) \\
	IMAGE_TAG=$(IMAGE_TAG) \\
	BUILD_ENV=$(ENV) \\
	PORT=$(PORT) \\
	docker compose up
"""


COMPOSE_TPL = """\
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


DOCKERFILE_TPL = """\
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
RUN --mount=type=secret,id=netrc,target=/root/.netrc \\
    GOPRIVATE=github.com/Qoin-Digital-Indonesia/* go mod download && go mod tidy

COPY . .
COPY .env.${BRANCH} .env

RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \\
    go build -a -installsuffix cgo -o /go/bin/${PROJECT} server.go

# ------------------------------------------------------------
FROM alpine:latest
ARG BRANCH
ARG PORT
ARG PROJECT

ENV TIMEZONE=Asia/Jakarta
RUN apk --no-cache add tzdata ca-certificates && \\
    cp /usr/share/zoneinfo/${TIMEZONE} /etc/localtime && \\
    echo "${TIMEZONE}" > /etc/timezone

EXPOSE ${PORT}

COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=builder /go/bin/${PROJECT} .
COPY .env.${BRANCH} .env

RUN printf "#!/bin/sh\\n\\nwhile true; do\\n\\techo \\"[INFO] Starting Service at \\$(date)\\"\\n\\t(./${PROJECT} >> ./history.log || echo \\"[ERROR] Restarting Service at \\$(date)\\")\\ndone" > run.sh && \\
    printf "#!/bin/sh\\n./run.sh & tail -F ./history.log" > up.sh && \\
    chmod +x up.sh run.sh

CMD ["./up.sh"]
"""


MAKEFILE_LEGACY_TPL = """\
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

.PHONY: help staging production build release run down check-env netrc

## Tampilkan bantuan
help:
	@echo ""
	@echo "$(IMAGE_NAME) — generic Docker build/release (legacy netrc)"
	@echo "=========================================================="
	@echo ""
	@echo "Usage: make <target> ENV=<env>  (env: staging | production)"
	@echo ""
	@echo "  make build ENV=staging       Build image staging"
	@echo "  make build ENV=production    Build image production"
	@echo "  make release ENV=staging     Push image staging"
	@echo "  make release ENV=production  Push image production"
	@echo "  make run ENV=staging         Build + jalankan container"
	@echo "  make down                    Stop container"
	@echo "  make help                    Bantuan ini"
	@echo ""
	@echo "CI/CD: override COMPOSE_FILE"
	@echo "  make build ENV=staging COMPOSE_FILE=build.compose"
	@echo ""

## Full flow: staging (build + release)
staging:
	@$(MAKE) build ENV=staging
	@$(MAKE) release ENV=staging

## Full flow: production (build + release)
production:
	@$(MAKE) build ENV=production
	@$(MAKE) release ENV=production

## Validasi file .env.{ENV} (warning only)
check-env:
	@if [ ! -f "$(ENV_FILE)" ]; then \\
		echo ">> ⚠️  $(ENV_FILE) tidak ditemukan (pakai default Dockerfile)"; \\
	fi

## Auto-generate $HOME/.netrc bila belum ada (dipakai compose secret + Dockerfile --mount=type=secret,id=netrc)
netrc:
	@if [ -z "$(GITHUB_TOKEN)" ]; then \\
		echo ">> ❌ GITHUB_TOKEN kosong. Jalankan 'gh auth login' atau export GITHUB_TOKEN=<token>"; \\
		exit 1; \\
	fi
	@if [ ! -f "$$HOME/.netrc" ]; then \\
		echo ">> ℹ️  Generate $$HOME/.netrc dari GITHUB_USER + GITHUB_TOKEN (needed by CI runner)"; \\
		printf "machine github.com login %s password %s\\n" "$(GITHUB_USER)" "$(GITHUB_TOKEN)" > "$$HOME/.netrc" && chmod 600 "$$HOME/.netrc"; \\
	fi

## Build image Docker
build: netrc
	@echo ">> Build $(ENV) image: $(ORG_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) (compose: $(COMPOSE_FILE))"
	IMAGE_TAG=$(IMAGE_TAG) ORG_REGISTRY=$(ORG_REGISTRY) IMAGE_NAME=$(IMAGE_NAME) BUILD_ENV=$(ENV) PORT=$(PORT) \\
	GITHUB_USER=$(GITHUB_USER) GITHUB_TOKEN=$(GITHUB_TOKEN) \\
		docker compose -f $(COMPOSE_FILE) build

## Build + tag + push image ke registry
release:
	@echo ">> Push $(ENV) image: $(ORG_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)"
	@docker tag $(ORG_REGISTRY)/$(IMAGE_NAME):latest $(ORG_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) 2>/dev/null || true
	IMAGE_TAG=$(IMAGE_TAG) ORG_REGISTRY=$(ORG_REGISTRY) IMAGE_NAME=$(IMAGE_NAME) BUILD_ENV=$(ENV) \\
		docker compose -f $(COMPOSE_FILE) push

## Build + jalankan container
run: check-env
	@echo ">> Run $(ENV): $(ORG_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) on port $(PORT)"
	IMAGE_TAG=$(IMAGE_TAG) ORG_REGISTRY=$(ORG_REGISTRY) IMAGE_NAME=$(IMAGE_NAME) BUILD_ENV=$(ENV) PORT=$(PORT) \\
		docker compose -f $(COMPOSE_FILE) up -d

## Stop container
down:
	docker compose -f $(COMPOSE_FILE) down 2>/dev/null || true
"""


COMPOSE_LEGACY_TPL = """\
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


TRIGGER_CI_TPL = """\
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
          RESP=$(curl -s -w "\\n%{http_code}" -X POST "${WEBHOOK_URL}" \\
            -H "Authorization: Bearer ${WEBHOOK_TOKEN}" \\
            -H "Content-Type: application/json" \\
            -d "{\\"repo\\":\\"${REPO}\\",\\"ref\\":\\"${REF}\\"}")

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
