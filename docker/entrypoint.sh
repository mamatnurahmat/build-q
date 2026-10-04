#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────
# build-q container entrypoint (via tini PID 1).
#
# Stateless-friendly: semua pre-flight step skip graceful jika resource
# tidak ada. Container bisa jalan TANPA mount apapun — cukup env vars.
#
#   (tanpa argumen) | serve   → bq --serve (Jev web chat)
#   <command> [args...]       → exec langsung
# ─────────────────────────────────────────────────────────────────────────
set -eu

HOST_MOUNT_DIR="${BQ_HOST_MOUNT_DIR:-/run/host}"

log()  { printf '[entrypoint] %s\n' "$*" >&2; }
warn() { printf '[entrypoint] WARNING: %s\n' "$*" >&2; }

is_true() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

# ─── 1. Docker client config (skip jika tidak di-mount) ─────────────────
setup_docker_config() {
    mkdir -p /root/.docker
    host_cfg="${HOST_MOUNT_DIR}/docker/config.json"
    [ -f "$host_cfg" ] || return 0

    if jq 'del(.credsStore, .credHelpers, .currentContext)' "$host_cfg" \
        > /root/.docker/config.json.tmp 2>/dev/null; then
        mv /root/.docker/config.json.tmp /root/.docker/config.json
        chmod 600 /root/.docker/config.json
        log "docker config disalin dari host"
    else
        rm -f /root/.docker/config.json.tmp
    fi
}

docker_login() {
    if [ -n "${DOCKERHUB_USER:-}" ] && [ -n "${DOCKERHUB_TOKEN:-}" ]; then
        if printf '%s' "$DOCKERHUB_TOKEN" | docker login \
            --username "$DOCKERHUB_USER" --password-stdin \
            ${DOCKER_LOGIN_REGISTRY:+"$DOCKER_LOGIN_REGISTRY"} >/dev/null 2>&1; then
            log "docker login OK (${DOCKER_LOGIN_REGISTRY:-docker.io})"
        else
            warn "docker login gagal"
        fi
    fi
}

# ─── 2. SSH keys (skip jika tidak di-mount) ─────────────────────────────
setup_ssh() {
    host_ssh="${HOST_MOUNT_DIR}/ssh"
    [ -d "$host_ssh" ] || return 0

    mkdir -p /root/.ssh
    cp -R "$host_ssh"/. /root/.ssh/ 2>/dev/null || true
    chown -R root:root /root/.ssh
    chmod 700 /root/.ssh
    find /root/.ssh -type f -exec chmod 600 {} +
    find /root/.ssh -type f -name '*.pub' -exec chmod 644 {} +

    if [ -f /root/.ssh/config ] && ! grep -qi '^IgnoreUnknown' /root/.ssh/config; then
        { printf 'IgnoreUnknown UseKeychain,AddKeysToAgent\n\n'; cat /root/.ssh/config; } \
            > /root/.ssh/config.tmp && mv /root/.ssh/config.tmp /root/.ssh/config
        chmod 600 /root/.ssh/config
    fi
    log "ssh keys disalin dari host"
}

# ─── 3. Docker socket (info only, non-fatal) ────────────────────────────
check_docker_socket() {
    sock="${DOCKER_HOST:-unix:///var/run/docker.sock}"
    sock="${sock#unix://}"
    if [ ! -S "$sock" ]; then
        log "docker socket tidak ada — mode stateless (remote scan, serve) tetap jalan"
        return 0
    fi
    if docker info >/dev/null 2>&1; then
        log "docker socket OK"
    else
        warn "docker socket ada tapi daemon tidak merespon"
    fi
}

# ─── Pre-flight (semua non-fatal) ───────────────────────────────────────
setup_docker_config
setup_ssh
check_docker_socket
docker_login

# ─── Command dispatch ────────────────────────────────────────────────────
if [ "$#" -eq 0 ] || [ "$1" = "serve" ]; then
    [ "$#" -gt 0 ] && shift
    port="${BQ_SERVE_PORT:-8888}"
    host="${BQ_SERVE_HOST:-0.0.0.0}"

    set -- bq --serve "$port" --serve-host "$host" "$@"

    if [ -n "${BQ_SERVE_TOKEN:-}" ]; then
        set -- "$@" --serve-token "$BQ_SERVE_TOKEN"
    elif [ "$host" != "127.0.0.1" ] && [ "$host" != "localhost" ]; then
        warn "BQ_SERVE_TOKEN kosong & bind ${host} — /api/* TANPA auth!"
    fi

    if is_true "${BQ_SERVE_NO_EXEC:-}"; then
        set -- "$@" --serve-no-exec
    fi

    log "starting: bq --serve ${port} --serve-host ${host}"
fi

exec "$@"
