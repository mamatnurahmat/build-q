# syntax=docker/dockerfile:1.7
#
# build-q (bq) — image lengkap dgn Jev TUI + web chat (--serve) + anomaly
# scanner + bootstrap-k8s + GitOps tooling. Zero daemon-in-container
# (Docker-outside-of-Docker via /var/run/docker.sock mount).
#
# Build:
#   docker build -t build-q:latest -f docker/Dockerfile .
#   # atau pin versi:
#   docker build --build-arg BUILD_Q_VERSION=0.1.42 -t build-q:0.1.42 -f docker/Dockerfile .

ARG PYTHON_VERSION=3.13

# ─── Stage 1: downloader (pisah agar cacheable) ─────────────────────────────
FROM debian:bookworm-slim AS downloader

ARG DOCKER_VERSION=27.4.1
ARG KUBECTL_VERSION=v1.32.0
ARG SOPS_VERSION=v3.9.1
ARG AGE_VERSION=v1.2.0
ARG GH_VERSION=2.63.2

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates tar \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /dl
RUN set -eux; \
    arch_u=$(uname -m); \
    case "$arch_u" in \
      x86_64)  arch_deb=amd64 ;; \
      aarch64) arch_deb=arm64 ;; \
      *) echo "unsupported arch: $arch_u"; exit 1 ;; \
    esac; \
    echo "ARCH=$arch_deb" > /dl/_arch; \
    \
    # Docker CLI (client only — kita pakai host daemon via socket) \
    curl -fsSL "https://download.docker.com/linux/static/stable/${arch_u}/docker-${DOCKER_VERSION}.tgz" -o docker.tgz; \
    tar -xzf docker.tgz; \
    mv docker/docker /dl/docker-cli; \
    rm -rf docker docker.tgz; \
    \
    # Docker buildx plugin \
    curl -fsSL "https://github.com/docker/buildx/releases/latest/download/buildx-$(uname -s | tr '[:upper:]' '[:lower:]')-${arch_deb}" -o /dl/docker-buildx; \
    chmod +x /dl/docker-buildx; \
    \
    # Docker compose plugin \
    curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s | tr '[:upper:]' '[:lower:]')-${arch_u}" -o /dl/docker-compose; \
    chmod +x /dl/docker-compose; \
    \
    # kubectl \
    curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch_deb}/kubectl" -o /dl/kubectl; \
    chmod +x /dl/kubectl; \
    \
    # sops \
    curl -fsSL "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.${arch_deb}" -o /dl/sops; \
    chmod +x /dl/sops; \
    \
    # age + age-keygen (sops encryption keys) \
    curl -fsSL "https://github.com/FiloSottile/age/releases/download/${AGE_VERSION}/age-${AGE_VERSION}-linux-${arch_deb}.tar.gz" -o age.tgz; \
    tar -xzf age.tgz; \
    mv age/age /dl/age; \
    mv age/age-keygen /dl/age-keygen; \
    rm -rf age age.tgz; \
    \
    # gh CLI (optional; dipakai bila GH_CLI=true) \
    curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch_deb}.tar.gz" -o gh.tgz; \
    tar -xzf gh.tgz; \
    mv gh_${GH_VERSION}_linux_${arch_deb}/bin/gh /dl/gh; \
    rm -rf gh_${GH_VERSION}_linux_${arch_deb} gh.tgz


# ─── Stage 2: runtime ───────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG BUILD_Q_VERSION=0.1.42

# System deps minimal: git (clone), tini (PID 1), curl (healthcheck),
# ca-certificates, openssh-client (git SSH), jq (debugging)
RUN apt-get update && apt-get install -y --no-install-recommends \
      git tini curl ca-certificates openssh-client jq \
      make \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Copy binaries dari downloader stage
COPY --from=downloader /dl/docker-cli      /usr/local/bin/docker
COPY --from=downloader /dl/docker-buildx   /usr/libexec/docker/cli-plugins/docker-buildx
COPY --from=downloader /dl/docker-compose  /usr/libexec/docker/cli-plugins/docker-compose
COPY --from=downloader /dl/kubectl         /usr/local/bin/kubectl
COPY --from=downloader /dl/sops            /usr/local/bin/sops
COPY --from=downloader /dl/age             /usr/local/bin/age
COPY --from=downloader /dl/age-keygen      /usr/local/bin/age-keygen
COPY --from=downloader /dl/gh              /usr/local/bin/gh

# Verifikasi
RUN set -eux; \
    docker --version; \
    docker buildx version; \
    docker compose version; \
    kubectl version --client --output=yaml | head -5; \
    sops --version; \
    age --version; \
    gh --version | head -1; \
    git --version

# Install build-q + optional anomaly extras (pyyaml)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir "build-q[anomaly]==${BUILD_Q_VERSION}"

# Workspace & config dirs
RUN mkdir -p /root/.build-q /root/.kube /root/.config/sops/age /workspace
WORKDIR /workspace

# Env defaults (bisa di-override via compose/.env)
ENV JEV_PROVIDER=typesafe \
    PB_API=false \
    GH_CLI=false \
    BUILD_Q_TUI_CACHE_TTL=3600 \
    PYTHONUNBUFFERED=1

EXPOSE 8888

# Healthcheck untuk --serve mode
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:8888/api/health >/dev/null || exit 1

# Entry via tini (reap zombies + handle signal), command via shell untuk
# conditional flag expansion dari env (BQ_SERVE_TOKEN, BQ_SERVE_NO_EXEC).
ENTRYPOINT ["tini", "--"]
CMD ["sh", "-c", "bq --serve 8888 --serve-host 0.0.0.0 ${BQ_SERVE_TOKEN:+--serve-token \"$BQ_SERVE_TOKEN\"} ${BQ_SERVE_NO_EXEC:+--serve-no-exec}"]
