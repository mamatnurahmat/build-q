"""Jev Agent Planner — TUI interaktif yang memilih tool build-q via Jev.

Fitur:
- Provider switchable: `openrouter` atau `typesafe` (via .env atau perintah TUI).
- Credential per-provider dibaca dari .env, bisa dipersist (`/save`) atau di-swap
  runtime (`/provider openrouter`).
- Top-3 kandidat tool ditampilkan dengan **score kecocokan** + **contoh perintah
  lengkap** (parameter di-extract dari pesan user via regex).
- Konfirmasi eksekusi dengan penanda RISKY untuk perintah destruktif.

Butuh: `rich`, `requests`. Tidak butuh `typesafe-sdk` — panggilan lewat HTTP langsung.
"""
from __future__ import annotations

import contextlib
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import requests
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .config import ENV_FILE, load_config

# ---------------------------------------------------------------------------
# .env handling
# ---------------------------------------------------------------------------


def _candidate_env_paths() -> list[Path]:
    """Cari .env di cwd lalu di direktori parent (utk kasus run dari examples/)."""
    here = Path.cwd()
    script_dir = Path(__file__).resolve().parent
    return [
        here / ".env",
        script_dir / ".env",
        script_dir.parent / ".env",
    ]


def load_env() -> Path | None:
    """Load centralized config, with legacy project .env fallback."""
    if ENV_FILE.exists():
        load_config()
        return ENV_FILE

    for p in _candidate_env_paths():
        if p.exists():
            for raw in p.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                k, _, v = line.partition("=")
                v = v.strip().strip('"').strip("'")
                os.environ.setdefault(k.strip(), v)
            return p

    load_config()
    return ENV_FILE if ENV_FILE.exists() else None


def save_env(updates: dict, path: Path | None = None) -> Path:
    """Update or append KEY=VALUE lines. Bikin file baru kalau belum ada."""
    if path is None:
        path = ENV_FILE
        path.parent.mkdir(parents=True, exist_ok=True)

    existing = path.read_text().splitlines() if path.exists() else []
    keys_seen: set[str] = set()
    new_lines: list[str] = []
    for raw in existing:
        stripped = raw.strip()
        head = stripped[len("export "):] if stripped.startswith("export ") else stripped
        if "=" in head and not head.startswith("#"):
            k = head.split("=", 1)[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}")
                keys_seen.add(k)
                continue
        new_lines.append(raw)
    for k, v in updates.items():
        if k not in keys_seen:
            new_lines.append(f"{k}={v}")
    path.write_text("\n".join(new_lines) + "\n")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

@dataclass
class Provider:
    name: str
    url: str
    model: str
    key_env: str


PROVIDERS: dict[str, Provider] = {
    "openrouter": Provider(
        name="openrouter",
        url="https://openrouter.ai/api/alpha/decisions",
        model="typesafe/jev-1.13",
        key_env="OPENROUTER_API_KEY",
    ),
    "typesafe": Provider(
        name="typesafe",
        url="https://api.typesafe.ai/v1/systemone",
        model="jev-latest",
        key_env="TYPESAFE_API_KEY",
    ),
}


def resolve_provider(name: str | None = None) -> Provider:
    """Ambil provider aktif; url/model bisa dioverride via env (mis. TYPESAFE_URL)."""
    name = (name or os.environ.get("JEV_PROVIDER", "typesafe")).lower()
    if name not in PROVIDERS:
        raise RuntimeError(f"Provider '{name}' tidak dikenal. Pilih: {list(PROVIDERS)}")
    p = PROVIDERS[name]
    return Provider(
        name=p.name,
        url=os.environ.get(f"{p.name.upper()}_URL", p.url),
        model=os.environ.get(f"{p.name.upper()}_MODEL", p.model),
        key_env=p.key_env,
    )


def ensure_key(provider: Provider) -> str:
    key = os.environ.get(provider.key_env, "").strip()
    if not key:
        raise RuntimeError(
            f"{provider.key_env} kosong. Isi di .env atau jalankan /set {provider.key_env} <value>."
        )
    return key


# ---------------------------------------------------------------------------
# Jev decision call (HTTP, universal untuk kedua provider)
# ---------------------------------------------------------------------------

def jev_decide(state: dict, questions: dict, provider: Provider) -> dict:
    key = ensure_key(provider)
    r = requests.post(
        provider.url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": provider.model, "state": state, "questions": questions},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Tool catalog
# ---------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    # ─── bq: BUILD image ───────────────────────────────────────────────────
    "bq_build_local": {
        "desc": "EKSEKUSI build image container secara lokal via Docker buildx untuk repo & branch/tag. Ini perintah default untuk membangun image (bukan setup). Push ke registry.",
        "template": "bq {repo} {ref} --local",
        "params": {
            "repo": {"type": "text", "hint": "nama repo (mis. pay-be-topup-manager, saas-apigateway)"},
            "ref": {"type": "text", "hint": "branch atau tag (develop, staging, main, v1.2.3)"},
        },
    },
    "bq_build_no_push": {
        "desc": "Build image lokal tanpa push ke Docker Hub — hanya untuk test build sukses / dry validation.",
        "template": "bq {repo} {ref} --local --no-push",
        "params": {
            "repo": {"type": "text", "hint": "nama repo"},
            "ref": {"type": "text", "hint": "branch atau tag"},
        },
    },
    "bq_build_compose": {
        "desc": "Build & release image via docker-compose (make build/release) untuk aplikasi web/legacy compose.",
        "template": "bq {repo} {ref} --compose",
        "params": {
            "repo": {"type": "text", "hint": "nama repo"},
            "ref": {"type": "text", "hint": "branch/tag"},
        },
    },
    "bq_build_dry_run": {
        "desc": "Simulasi build tanpa eksekusi — hanya print perintah docker build yang akan dijalankan.",
        "template": "bq {repo} {ref} --local --dry-run",
        "params": {
            "repo": {"type": "text", "hint": "nama repo"},
            "ref": {"type": "text", "hint": "branch/tag"},
        },
    },
    "bq_clone_build": {
        "desc": "Clone repo dari GitHub lalu build image (berguna untuk build repo yang belum ada di lokal).",
        "template": "bq --clone {owner_repo} {ref} --local --clean",
        "params": {
            "owner_repo": {"type": "text", "hint": "owner/repo GitHub (mis. Qoin-Digital-Indonesia/pay-be-x)"},
            "ref": {"type": "text", "hint": "branch/tag"},
        },
    },
    "bq_build_remote": {
        "desc": "Build image remote dari git langsung via buildx (tanpa clone lokal). Butuh registry access.",
        "template": "bq {repo} {ref} --remote",
        "params": {
            "repo": {"type": "text", "hint": "nama repo"},
            "ref": {"type": "text", "hint": "branch/tag"},
        },
    },

    # ─── bq: CONFIG & PREFLIGHT ─────────────────────────────────────────────
    "bq_doctor": {
        "desc": "Preflight check kesiapan tools (git, docker, buildx, kubectl) & credentials (GITHUB_TOKEN, docker login, kube-context). Exit 0 kalau siap.",
        "template": "bq --doctor",
        "params": {},
    },
    "bq_config_show": {
        "desc": "Tampilkan konfigurasi build-q yang aktif dari ~/.build-q/.env.",
        "template": "bq --config",
        "params": {},
    },
    "bq_init_config": {
        "desc": "Initialize file config ~/.build-q/.env pertama kali (interactive setup).",
        "template": "bq --init",
        "params": {},
    },
    "bq_check": {
        "desc": "Cek konsistensi cicd.json, Makefile, Dockerfile, compose.yaml — validasi konfigurasi CI/CD project.",
        "template": "bq --check",
        "params": {},
    },

    # ─── bq: INIT / SCAFFOLD ────────────────────────────────────────────────
    "bq_init_jx": {
        "desc": "SETUP AWAL project baru: scaffold Makefile + compose.yaml + Dockerfile + pipeline Jenkins X dari template. Dijalankan sekali di project baru, BUKAN untuk build reguler.",
        "template": "bq --init-jx --stack {stack}",
        "params": {
            "stack": {"type": "choice", "options": ["dotnet", "default"]},
        },
    },
    "bq_init_legacy": {
        "desc": "Scaffold ulang konfigurasi CI/CD legacy (untuk repo lama yang belum pakai jx-init).",
        "template": "bq --init-legacy",
        "params": {},
    },
    "bq_init_secrets": {
        "desc": "Setup GitHub Actions secrets (registry credentials + gitops token) untuk repo aktif.",
        "template": "bq --init-secrets",
        "params": {},
    },
    "bq_gh_action_init": {
        "desc": "Install/update file .github/workflows/trigger-ci.yml agar push ke branch memicu build CI/CD.",
        "template": "bq --gh-action-init",
        "params": {},
    },
    "bq_fix_dockerfile": {
        "desc": "Migrate Dockerfile legacy (ARG netrc, FROM lowercase) ke pola buildx-native yang benar.",
        "template": "bq --fix-dockerfile {path}",
        "params": {
            "path": {"type": "text", "default": "./Dockerfile", "hint": "path Dockerfile (default: ./Dockerfile)"},
        },
    },

    # ─── bq: CI/CD TRIGGER & FIX ────────────────────────────────────────────
    "bq_cicd_trigger": {
        "desc": "Trigger CI/CD pipeline lewat webhook (mulai build remote di CI runner).",
        "template": "bq --cicd-trigger",
        "params": {},
    },
    "bq_cicd_webhook": {
        "desc": "Register/verifikasi webhook GitHub → CI backend untuk repo aktif.",
        "template": "bq --cicd-webhook",
        "params": {},
    },
    "bq_pr_fix": {
        "desc": "Regen konfigurasi CI/CD (jx-init) dan buat PR fix ke repo & ref tertentu — berguna kalau pipeline JX gagal.",
        "template": "bq --pr-fix {repo} {ref}",
        "params": {
            "repo": {"type": "text", "hint": "nama repo (mis. plus-be-webview2-manager)"},
            "ref": {"type": "text", "hint": "branch atau tag (develop, staging, main, v1.2.3)"},
        },
    },

    # ─── bq: K8s BOOTSTRAP & GITOPS ─────────────────────────────────────────
    "bq_bootstrap_k8s": {
        "desc": "Bootstrap deployment ke Kubernetes: generate manifest deployment.yaml + service.yaml, apply, push ke repo GitOps.",
        "template": "bq --bootstrap-k8s --gitops-repo {gitops_repo} --gitops-branch {branch} --path-yaml {path_yaml} --env {env} --replicas {replicas} --kube-context {kube_context} --namespace {namespace}",
        "params": {
            "gitops_repo": {"type": "text", "default": "gitops", "hint": "repo gitops (default: gitops)"},
            "branch": {"type": "text", "default": "main", "hint": "branch gitops (default: main)"},
            "path_prefix": {"type": "choice", "options": ["cce", "k8s"], "default": "cce"},
            "path_yaml": {
                "type": "text",
                "default_template": "{path_prefix}/{namespace}/{deployment}_deployment.yaml",
                "hint": "path YAML (auto: <prefix>/<ns>/<deployment>_deployment.yaml)",
            },
            "env": {"type": "choice", "options": ["develop", "staging", "production"]},
            "replicas": {"type": "text", "default": "2", "hint": "jumlah replica (default 2)"},
            "kube_context": {"type": "text", "hint": "kube context (mis. hw-pro-p)"},
            "namespace": {"type": "text", "hint": "namespace K8s"},
            "deployment": {"type": "text", "hint": "nama deployment (utk derive path_yaml)"},
        },
    },
    "bq_set_image": {
        "desc": "Hot-patch image K8s deployment via `kubectl set image` + wait rollout. Mengubah state cluster langsung.",
        "template": "bq --set-image {namespace} {deployment} {image}",
        "params": {
            "namespace": {"type": "text", "hint": "namespace K8s"},
            "deployment": {"type": "text", "hint": "nama deployment"},
            "image": {
                "type": "text",
                "default_template": "loyaltolpi/{deployment}:{tag}",
                "hint": "image lengkap (auto: loyaltolpi/<deployment>:<tag> bila tag ada di pesan)",
            },
            "tag": {"type": "text", "hint": "tag versi (utk derive image, opsional)"},
        },
    },
    "bq_gitops_set_image": {
        "desc": "Update image tag di file YAML repo GitOps + commit + push ke branch (tanpa PR). Pre-flight cek registry.",
        "template": "bq --gitops-set-image {gitops_repo} {branch} {path_yaml} {image}",
        "params": {
            "gitops_repo": {"type": "text", "default": "gitops", "hint": "repo gitops (default: gitops)"},
            "branch": {"type": "text", "default": "main", "hint": "target branch (default: main)"},
            "path_prefix": {"type": "choice", "options": ["cce", "k8s"], "default": "cce"},
            "path_yaml": {
                "type": "text",
                "default_template": "{path_prefix}/{namespace}/{deployment}_deployment.yaml",
                "hint": "path YAML (auto: <prefix>/<ns>/<deployment>_deployment.yaml)",
            },
            "image": {
                "type": "text",
                "default_template": "loyaltolpi/{deployment}:{tag}",
                "hint": "image lengkap (auto: loyaltolpi/<deployment>:<tag>)",
            },
            "namespace": {"type": "text", "hint": "namespace (utk derive path_yaml)"},
            "deployment": {"type": "text", "hint": "nama deployment (utk derive path_yaml + image)"},
            "tag": {"type": "text", "hint": "tag versi"},
        },
    },
    "bq_is_match_image": {
        "desc": "Bandingkan image container deployment K8s (live) dengan image di file YAML GitOps. Read-only, sarankan fix bila mismatch.",
        "template": "bq --is-match-image {namespace} {deployment} {gitops_repo} {branch} {path_yaml}",
        "params": {
            "namespace": {"type": "text", "hint": "namespace K8s (mis. production-ngenwal)"},
            "deployment": {"type": "text", "hint": "nama deployment"},
            "gitops_repo": {"type": "text", "default": "gitops", "hint": "repo gitops (default: gitops)"},
            "branch": {"type": "text", "default": "main", "hint": "branch (default: main)"},
            "path_prefix": {"type": "choice", "options": ["cce", "k8s"], "default": "cce"},
            "path_yaml": {
                "type": "text",
                "default_template": "{path_prefix}/{namespace}/{deployment}_deployment.yaml",
                "hint": "path YAML (auto: <prefix>/<ns>/<deployment>_deployment.yaml)",
            },
        },
    },

    # ─── Companion (non-bq) ─────────────────────────────────────────────────
    "drift_checker_worker": {
        "desc": "Jalankan worker version drift check (GitHub vs GitOps YAML vs cluster runtime) untuk satu namespace.",
        "template": "python3 -m drift_checker.worker {namespace}",
        "params": {"namespace": {"type": "text", "hint": "namespace target (mis. production-payout)"}},
    },
}

RISKY_TOOLS = {
    "bq_build_local", "bq_build_compose", "bq_build_remote", "bq_clone_build",
    "bq_init_config", "bq_init_jx", "bq_init_legacy", "bq_init_secrets",
    "bq_gh_action_init", "bq_fix_dockerfile",
    "bq_cicd_trigger", "bq_cicd_webhook", "bq_pr_fix",
    "bq_bootstrap_k8s", "bq_set_image", "bq_gitops_set_image",
}

TEXT_PATTERNS = {
    # v1.2.3 tag semver
    "tag": r"v\d+\.\d+\.\d+",
    # image: loyaltolpi/xxx:v1.2.3 atau loyaltolpi/xxx:sha
    "image": r"loyaltolpi/[\w.-]+:[\w.-]+",
    # nama repo Qoin: harus punya segmen -be-/-fe-/-mono- ATAU berakhir -apigateway
    # (mencegah false match "payout" dari "production-payout")
    "repo": r"(?:pay|plus|ngenwal|ngendigid|qoinhub|saas|dana|reward|kyc|admin)-(?:be|fe|mono)-[\w-]+|[a-z]+-apigateway",
    # owner/repo GitHub
    "owner_repo": r"[\w-]+/[\w.-]+",
    # branch/tag umum
    "ref": r"v\d+\.\d+\.\d+|develop|staging|main|master|production",
    # K8s namespace
    "namespace": r"(?:develop|staging|production)-[\w-]+|jenkins-x|kube-system|default",
    # nama deployment (sama pola dengan repo)
    "deployment": r"(?:pay|plus|ngenwal|ngendigid|qoinhub|saas|dana|reward|kyc|admin)-(?:be|fe|mono)-[\w-]+|[a-z]+-apigateway",
    # kube context (mis. hw-pro-p, sls-pro-q)
    "kube_context": r"(?:hw|sls)-(?:dev|pro|stg)-[a-z]",
    # ArgoCD app / pod
    "app": r"[\w-]+-app|[\w-]+-argocd",
    "pod": r"[\w-]+-[\w-]{5,}-[\w]{5}",
    # path YAML gitops
    "path_yaml": r"(?:cce|k8s)/[\w-]+/[\w.-]+\.yaml",
    "partial_file": r"[\w./-]+\.json",
    # Dockerfile path
    "path": r"(?:\./)?[\w./-]*Dockerfile[\w.-]*",
    # replicas: angka 1-9
    "replicas": r"\b[1-9]\b",
    # stack choice
    "stack": r"\bdotnet\b|\bdefault\b",
    # env choice
    "env": r"\b(?:develop|staging|production)\b",
    # branch
    "branch": r"\b(?:develop|staging|main|master)\b",
}


# ---------------------------------------------------------------------------
# Decision + parameter extraction
# ---------------------------------------------------------------------------

def extract_params_from_text(user_msg: str, tool_name: str) -> dict:
    """Isi param text (regex) dan choice (word-match); apply default_template dari param lain."""
    meta = TOOLS[tool_name]
    out: dict = {}

    # Pass 1: regex-based & choice-based extraction
    for pname, pspec in meta["params"].items():
        if pspec["type"] == "text":
            pat = TEXT_PATTERNS.get(pname)
            m = re.search(pat, user_msg) if pat else None
            if m:
                out[pname] = m.group(0)
            elif "default" in pspec:
                out[pname] = pspec["default"]
            else:
                out[pname] = f"<{pname}>"
        elif pspec["type"] == "choice":
            options = pspec.get("options", [])
            matched = next(
                (o for o in options if re.search(rf"\b{re.escape(o)}\b", user_msg, re.I)),
                None,
            )
            if matched:
                out[pname] = matched
            elif "default" in pspec:
                out[pname] = pspec["default"]
            else:
                out[pname] = f"<{pname}>"

    # Pass 2: apply default_template for params still placeholder,
    # referencing other params that got filled.
    for pname, pspec in meta["params"].items():
        tmpl = pspec.get("default_template")
        if not tmpl:
            continue
        val = out.get(pname, "")
        if not str(val).startswith("<"):
            continue  # already filled
        try:
            candidate = tmpl.format(**out)
        except (KeyError, IndexError):
            continue
        if "<" in candidate:
            continue  # required upstream param still missing
        out[pname] = candidate
    return out


def preview_command(tool_name: str, params: dict) -> str:
    return TOOLS[tool_name]["template"].format(**params)


def pick_tool(user_msg: str, provider: Provider) -> dict:
    criteria = {name: meta["desc"] for name, meta in TOOLS.items()}
    criteria["none"] = "Tidak ada tool yang cocok untuk permintaan ini."
    questions = {
        "tool": {
            "type": "choice",
            "instructions": "Pilih 1 tool build-q paling cocok untuk memenuhi request user.",
            "criteria": criteria,
        },
        "risky": {
            "type": "noul",
            "instructions": "Apakah tool ini bersifat destruktif / mengubah state?",
            "criteria": {
                "true": "Tool mengubah state cluster/repo (apply, sync, delete, create PR, push).",
                "false": "Tool hanya read-only (list, cek status, ambil info).",
            },
        },
    }
    return jev_decide({"user_request": user_msg}, questions, provider)


def fill_missing_choice_params(user_msg: str, tool_name: str, provider: Provider) -> dict:
    """Untuk param type=choice yang belum ke-extract regex, minta Jev pilih valuenya."""
    meta = TOOLS[tool_name]
    already = extract_params_from_text(user_msg, tool_name)
    choice_qs = {}
    for pname, pspec in meta["params"].items():
        if pspec["type"] != "choice":
            continue
        if not str(already.get(pname, "")).startswith("<"):
            continue
        choice_qs[pname] = {
            "type": "choice",
            "instructions": f"Pilih nilai param '{pname}' sesuai request user.",
            "criteria": {opt: f"{pname} = {opt}" for opt in pspec["options"]},
        }
    if not choice_qs:
        return {}
    resp = jev_decide({"user_request": user_msg, "tool": tool_name}, choice_qs, provider)
    return {k: v["choice"] for k, v in resp.get("answers", {}).items()}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

console = Console()


def score_bar(prob: float, width: int = 20) -> str:
    n = int(round(prob * width))
    return "█" * n + "░" * (width - n)


def render_top_suggestions(user_msg: str, tool_answer: dict) -> str:
    """Tampilkan top-3 kandidat tool dengan score bar + preview perintah."""
    probs: dict[str, float] = tool_answer.get("probabilities", {})
    top = sorted(probs.items(), key=lambda x: -x[1])[:3]

    tbl = Table(
        title="Top-3 Kandidat Perintah (skor kecocokan)",
        show_header=True,
        header_style="bold cyan",
        expand=True,
    )
    tbl.add_column("#", width=3, justify="right")
    tbl.add_column("Tool", width=22)
    tbl.add_column("Skor", width=32)
    tbl.add_column("Contoh Perintah")

    winner = top[0][0] if top else ""
    for i, (name, p) in enumerate(top, 1):
        if name == "none":
            cmd_preview = "[dim]—[/dim]"
        else:
            params = extract_params_from_text(user_msg, name)
            cmd_preview = f"[green]{preview_command(name, params)}[/green]"
        name_cell = f"[bold]{name}[/bold]" if name == winner else name
        tbl.add_row(
            str(i),
            name_cell,
            f"{score_bar(p)} {p:.2%}",
            cmd_preview,
        )
    console.print(tbl)
    return winner


def render_verdict(tool_answer: dict, risky_answer: dict) -> tuple[str, bool]:
    tool = tool_answer["choice"]
    conf = tool_answer.get("confidence", 0.0)
    noul = float(risky_answer.get("noul", 0.0)) if risky_answer else 0.0
    risky = noul >= 0.5 or tool in RISKY_TOOLS
    tag = "[red]RISKY[/red]" if risky else "[green]safe[/green]"
    console.print(
        f"→ Pilihan Jev: [bold green]{tool}[/bold green] "
        f"(confidence {conf:.2%}) {tag} (destructive noul={noul:.2f})"
    )
    return tool, risky


def render_usage(resp: dict) -> None:
    usage = resp.get("usage") or {}
    if not usage:
        return
    parts = []
    if "input_tokens" in usage:
        parts.append(f"in {usage['input_tokens']}")
    if "output_tokens" in usage:
        parts.append(f"out {usage['output_tokens']}")
    if "cost" in usage:
        parts.append(f"${usage['cost']:.6f}")
    if parts:
        console.print(f"[dim]tokens: {' · '.join(parts)}[/dim]")


def show_tools() -> None:
    tbl = Table(title="Katalog Tool", show_header=True, header_style="bold magenta")
    tbl.add_column("Tool")
    tbl.add_column("Deskripsi")
    tbl.add_column("Template", style="dim")
    for name, meta in TOOLS.items():
        tbl.add_row(name, meta["desc"], meta["template"])
    console.print(tbl)


def show_provider(provider: Provider) -> None:
    key = os.environ.get(provider.key_env, "")
    masked = (key[:8] + "…" + key[-4:]) if len(key) > 12 else ("<empty>" if not key else "<short>")
    tbl = Table(title="Provider aktif", show_header=False, box=None)
    tbl.add_column("k", style="cyan")
    tbl.add_column("v")
    tbl.add_row("provider", provider.name)
    tbl.add_row("url", provider.url)
    tbl.add_row("model", provider.model)
    tbl.add_row("key", masked)
    console.print(tbl)


def show_help() -> None:
    console.print(Panel.fit(
        "[bold]Perintah TUI[/bold]\n"
        "  [cyan]/tools[/cyan]                    lihat katalog tool\n"
        "  [cyan]/provider[/cyan]                 tampilkan provider aktif\n"
        "  [cyan]/provider openrouter[/cyan]      switch ke OpenRouter\n"
        "  [cyan]/provider typesafe[/cyan]        switch ke TypeSafe direct\n"
        "  [cyan]/set KEY VALUE[/cyan]            set env var (mis. OPENROUTER_API_KEY sk-...)\n"
        "  [cyan]/save[/cyan]                     persist JEV_PROVIDER + kunci saat ini ke .env\n"
        "  [cyan]/help[/cyan]                     tampilkan bantuan ini\n"
        "  [cyan]/quit[/cyan]                     keluar",
        border_style="green",
    ))


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def handle_slash(cmd: str, provider_ref: list[Provider]) -> bool:
    """Return True kalau command dikenali (tidak diteruskan ke Jev)."""
    parts = cmd.split()
    head = parts[0]

    if head in ("/quit", "/exit", "/q"):
        raise SystemExit(0)
    if head == "/help":
        show_help()
        return True
    if head == "/tools":
        show_tools()
        return True
    if head == "/provider":
        if len(parts) == 1:
            show_provider(provider_ref[0])
        else:
            try:
                provider_ref[0] = resolve_provider(parts[1])
                os.environ["JEV_PROVIDER"] = provider_ref[0].name
                console.print(f"[green]switched → {provider_ref[0].name}[/green]")
                show_provider(provider_ref[0])
            except Exception as e:
                console.print(f"[red]{e}[/red]")
        return True
    if head == "/set":
        if len(parts) < 3:
            console.print("[red]usage: /set KEY VALUE[/red]")
        else:
            k, v = parts[1], " ".join(parts[2:])
            os.environ[k] = v
            console.print(f"[green]{k} set (in-memory)[/green]")
        return True
    if head == "/save":
        p = provider_ref[0]
        updates = {"JEV_PROVIDER": p.name}
        for env_key in (p.key_env, f"{p.name.upper()}_URL", f"{p.name.upper()}_MODEL"):
            if os.environ.get(env_key):
                updates[env_key] = os.environ[env_key]
        path = save_env(updates)
        console.print(f"[green]persisted → {path}[/green]")
        return True
    if head.startswith("/"):
        console.print(f"[yellow]perintah tidak dikenal: {head} (ketik /help)[/yellow]")
        return True
    return False


def repl() -> None:
    loaded = load_env()
    if loaded:
        console.print(f"[dim].env loaded from {loaded}[/dim]")
    try:
        current = resolve_provider()
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    provider_ref = [current]

    console.print(Panel.fit(
        "[bold green]Jev Agent Planner[/bold green]  "
        f"[dim]provider={current.name} model={current.model}[/dim]\n"
        "Ketik request natural (ID/EN). Jev akan usulkan top-3 perintah + skor kecocokan.\n"
        "[cyan]/help[/cyan] daftar perintah TUI",
        border_style="green",
    ))

    while True:
        try:
            msg = Prompt.ask("\n[bold blue]you[/bold blue]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nSampai jumpa!")
            return
        if not msg:
            continue
        if msg.startswith("/") and handle_slash(msg, provider_ref):
            continue

        provider = provider_ref[0]
        try:
            with console.status(f"[cyan]Jev ({provider.name}) sedang memutuskan...[/cyan]"):
                resp = pick_tool(msg, provider)
        except Exception as e:
            console.print(f"[red]Jev error:[/red] {e}")
            continue

        answers = resp.get("answers", {})
        tool_ans = answers.get("tool", {})
        risky_ans = answers.get("risky", {})

        render_top_suggestions(msg, tool_ans)
        render_usage(resp)
        tool, risky = render_verdict(tool_ans, risky_ans)

        if tool == "none":
            console.print("[yellow]Tidak ada tool yang cocok. Coba lebih spesifik.[/yellow]")
            continue

        params = extract_params_from_text(msg, tool)
        try:
            choice_params = fill_missing_choice_params(msg, tool, provider)
            params.update(choice_params)
        except Exception as e:
            console.print(f"[red]fill-param error:[/red] {e}")
            continue

        meta = TOOLS[tool]

        # Param "sumber" = yang direferensi default_template param lain.
        # Prompt dulu supaya default_template bisa auto-fill dependent params.
        referenced: set[str] = set()
        for pspec in meta["params"].values():
            tmpl = pspec.get("default_template")
            if tmpl:
                referenced.update(re.findall(r"\{(\w+)\}", tmpl))

        def _prompt(pname: str, pspec: dict, params: dict = params) -> None:
            val = params.get(pname, "")
            if val and not str(val).startswith("<"):
                return
            hint = pspec.get("hint", "")
            default = pspec.get("default", "")
            params[pname] = Prompt.ask(
                f"  [cyan]param[/cyan] [bold]{pname}[/bold] [dim]({hint})[/dim]",
                default=default or None,
            )

        def _reapply_templates(meta: dict = meta, params: dict = params) -> None:
            for pname, pspec in meta["params"].items():
                tmpl = pspec.get("default_template")
                if not tmpl:
                    continue
                if not str(params.get(pname, "")).startswith("<"):
                    continue
                try:
                    candidate = tmpl.format(**params)
                except (KeyError, IndexError):
                    continue
                if "<" in candidate:
                    continue
                params[pname] = candidate

        # Round 1: prompt source params
        for pname, pspec in meta["params"].items():
            if pname in referenced:
                _prompt(pname, pspec)
        _reapply_templates()

        # Round 2: prompt sisanya (path_yaml/image ideally sudah auto-derived)
        for pname, pspec in meta["params"].items():
            if pname not in referenced:
                _prompt(pname, pspec)

        cmd = preview_command(tool, params)
        console.print(Panel(cmd, title="[magenta]Perintah Final[/magenta]", border_style="magenta"))

        default_yes = not risky
        prompt_label = "Jalankan?" + (" [red](RISKY)[/red]" if risky else "")
        if Confirm.ask(prompt_label, default=default_yes):
            console.print(Panel(cmd, title="[yellow]Menjalankan[/yellow]", border_style="yellow"))
            try:
                subprocess.run(shlex.split(cmd), check=False)
            except KeyboardInterrupt:
                console.print("[red]dibatalkan[/red]")
        else:
            console.print("[dim]dilewati[/dim]")


if __name__ == "__main__":
    repl()
