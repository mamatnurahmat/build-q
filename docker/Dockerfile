# syntax=docker/dockerfile:1.7
#
# build-q (bq) — image lengkap dgn Jev TUI + web chat (--serve) + Dockerfile
# scanner + anomaly scanner + bootstrap-k8s + GitOps tooling.
# Docker-outside-of-Docker via /var/run/docker.sock mount (opsional).
#
# Build:
#   docker build -t newrahmat/jev:0.1.43 docker/
#
# Run stateless (tanpa compose, tanpa mount):
#   docker run --rm -e TYPESAFE_API_KEY=sk-ts-xxx -e GITHUB_TOKEN=ghp_xxx \
#     newrahmat/jev:0.1.43 bq --fix-dockerfile Dockerfile \
#       --remote-repo owner/repo --remote-branch main
#
# Run serve mode:
#   docker run -d -p 8888:8888 -e TYPESAFE_API_KEY=sk-ts-xxx \
#     newrahmat/jev:0.1.43
#
# Run dengan Docker daemon host (DooD):
#   docker run -v /var/run/docker.sock:/var/run/docker.sock \
#     newrahmat/jev:0.1.43

ARG PYTHON_VERSION=3.13

# ─── Stage 1: downloader (cacheable) ───────────────────────────────────────
FROM debian:bookworm-slim AS downloader

ARG DOCKER_VERSION=27.4.1
ARG BUILDX_VERSION=v0.19.1
ARG COMPOSE_VERSION=v2.32.1
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
    \
    curl -fsSL "https://download.docker.com/linux/static/stable/${arch_u}/docker-${DOCKER_VERSION}.tgz" -o docker.tgz; \
    tar -xzf docker.tgz; mv docker/docker /dl/docker-cli; rm -rf docker docker.tgz; \
    \
    curl -fsSL "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-${arch_deb}" -o /dl/docker-buildx; \
    chmod +x /dl/docker-buildx; \
    \
    curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${arch_u}" -o /dl/docker-compose; \
    chmod +x /dl/docker-compose; \
    \
    curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch_deb}/kubectl" -o /dl/kubectl; \
    chmod +x /dl/kubectl; \
    \
    curl -fsSL "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.${arch_deb}" -o /dl/sops; \
    chmod +x /dl/sops; \
    \
    curl -fsSL "https://github.com/FiloSottile/age/releases/download/${AGE_VERSION}/age-${AGE_VERSION}-linux-${arch_deb}.tar.gz" -o /tmp/age.tgz; \
    mkdir -p /tmp/age-ext && tar -xzf /tmp/age.tgz -C /tmp/age-ext --strip-components=1; \
    mv /tmp/age-ext/age /dl/age; mv /tmp/age-ext/age-keygen /dl/age-keygen; \
    rm -rf /tmp/age.tgz /tmp/age-ext; \
    \
    curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch_deb}.tar.gz" -o /tmp/gh.tgz; \
    mkdir -p /tmp/gh-ext && tar -xzf /tmp/gh.tgz -C /tmp/gh-ext --strip-components=1; \
    mv /tmp/gh-ext/bin/gh /dl/gh; rm -rf /tmp/gh.tgz /tmp/gh-ext


# ─── Stage 2: runtime ──────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG BUILD_Q_VERSION=0.1.43

LABEL org.opencontainers.image.title="build-q (Jev)" \
      org.opencontainers.image.description="bq CLI — Dockerfile scanner, Jev web chat, DevOps tooling" \
      org.opencontainers.image.version="${BUILD_Q_VERSION}" \
      org.opencontainers.image.source="https://github.com/mamatnurahmat/build-q" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update && apt-get install -y --no-install-recommends \
      git tini curl ca-certificates openssh-client jq make \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

COPY --from=downloader /dl/docker-cli      /usr/local/bin/docker
COPY --from=downloader /dl/docker-buildx   /usr/libexec/docker/cli-plugins/docker-buildx
COPY --from=downloader /dl/docker-compose  /usr/libexec/docker/cli-plugins/docker-compose
COPY --from=downloader /dl/kubectl         /usr/local/bin/kubectl
COPY --from=downloader /dl/sops            /usr/local/bin/sops
COPY --from=downloader /dl/age             /usr/local/bin/age
COPY --from=downloader /dl/age-keygen      /usr/local/bin/age-keygen
COPY --from=downloader /dl/gh              /usr/local/bin/gh

RUN set -eux; \
    docker --version; docker buildx version; docker compose version; \
    kubectl version --client --output=yaml | head -5; \
    sops --version; age --version; gh --version | head -1; git --version

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir "build-q[anomaly]==${BUILD_Q_VERSION}" && \
    bq --version

RUN git config --system --add safe.directory '*' && \
    printf 'Host *\n    StrictHostKeyChecking accept-new\n' \
      > /etc/ssh/ssh_config.d/10-build-q.conf

RUN mkdir -p /root/.build-q /root/.kube /root/.docker /root/.config/sops/age \
             /run/host /workspace
WORKDIR /workspace

COPY --chmod=0755 entrypoint.sh  /usr/local/bin/entrypoint.sh
COPY --chmod=0755 healthcheck.sh /usr/local/bin/healthcheck.sh

ENV JEV_PROVIDER=typesafe \
    PB_API=false \
    GH_CLI=false \
    BUILD_Q_TUI_CACHE_TTL=3600 \
    BQ_SERVE_PORT=8888 \
    BQ_SERVE_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["/usr/local/bin/healthcheck.sh"]

ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
