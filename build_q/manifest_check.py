"""Cek pasangan Deployment ↔ Service ↔ Secret di GitOps + saran YAML standar.

Dipakai tahap `manifest` di `bq --e2e` (CLI, TUI, web). Read-only.

Rule & nilai standar (label wajib, prefix secret file-config, mount path,
nodepool CCE per namespace, nama imagePullSecret) dimuat dari PocketBase
collection `build_q_anomaly_rules` (field `params` JSON). Rule non-aktif di
PB dilewati. Urutan sumber: PB → cache `~/.build-q/.anomaly-rules-cache.json`
→ `DEFAULT_RULES` di bawah (PB down + cache kosong).

Yang dinilai adalah hasil akhir kustomization folder: `namespace:` menimpa
metadata.namespace, `patches` JSON6902 (mis. inject imagePullSecrets) diterapkan
dulu, dan file harus terdaftar di `resources`.

Severity `critical` = memutus deploy/traffic (titik putus e2e); `warn` = saran
standar (tidak memutus — mis. config masih baked di image).

Pemeriksaan deterministik di Python; Jev (opsional, di e2e) hanya menilai
kesiapan deploy & prioritas perbaikan dari findings.
"""
from __future__ import annotations

import copy
import json
import posixpath
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import pb_api
from .config import CONFIG_DIR
from .tui_catalog import _ttl_seconds


COLLECTION = "build_q_anomaly_rules"
CACHE_FILE = CONFIG_DIR / ".anomaly-rules-cache.json"

# Snapshot rule e2e manifest (sinkron dgn scripts/anomaly-rules-seed.json).
DEFAULT_RULES: Dict[str, dict] = {
    "service_missing": {
        "severity": "critical", "category": "network",
        "description": "Deployment yang membuka containerPort tanpa Service pasangan di folder GitOps — pod tidak bisa diakses lewat DNS service.",
        "fix_hint": "Tambahkan <deployment>_services.yaml (selector = label pod) dan daftarkan di kustomization.yaml.",
        "params": {"suffixes": ["_services.yaml", "_service.yaml"]},
    },
    "manifest_not_in_kustomization": {
        "severity": "critical", "category": "config_drift",
        "description": "File Deployment/Service ada di folder tapi tidak terdaftar di resources kustomization.yaml — tidak pernah di-apply ArgoCD.",
        "fix_hint": "Tambahkan nama file ke `resources:` di kustomization.yaml folder tsb.",
        "params": {},
    },
    "deploy_selector_template_mismatch": {
        "severity": "critical", "category": "network",
        "description": "spec.selector.matchLabels Deployment tidak termuat di template.metadata.labels — Deployment ditolak API server.",
        "fix_hint": "Samakan matchLabels dengan label pod (selector immutable: hapus & buat ulang Deployment bila mengubahnya).",
        "params": {},
    },
    "missing_standard_labels": {
        "severity": "warn", "category": "compliance",
        "description": "Label pod tidak memuat label standar Qoin (app, env, project, role).",
        "fix_hint": "Tambahkan label standar di metadata & template Deployment (selector lama dipertahankan).",
        "params": {"required_labels": ["app", "env", "project", "role"]},
    },
    "service_selector_no_match": {
        "severity": "critical", "category": "network",
        "description": "Service.spec.selector tidak match label pod Deployment — endpoints kosong, traffic drop.",
        "fix_hint": "Samakan Service.spec.selector dengan spec.selector.matchLabels Deployment.",
        "params": {},
    },
    "service_namespace_mismatch": {
        "severity": "critical", "category": "network",
        "description": "Namespace Service berbeda dengan namespace Deployment (tanpa namespace: di kustomization) — Service tidak pernah menemukan pod.",
        "fix_hint": "Samakan metadata.namespace Service dengan Deployment, atau set `namespace:` di kustomization.yaml.",
        "params": {},
    },
    "service_port_targetport_mismatch": {
        "severity": "critical", "category": "network",
        "description": "Service.ports[].targetPort bernama tidak ada di containerPort Deployment (endpoint kosong). targetPort angka yang tidak dideklarasikan → warn.",
        "fix_hint": "Arahkan targetPort ke nama/nomor containerPort (standar: name http).",
        "params": {},
    },
    "missing_file_config_volume": {
        "severity": "warn", "category": "compliance",
        "description": "Deployment backend tanpa volume Secret file-config-<app> — config kemungkinan masih baked di image (pola lama).",
        "fix_hint": "Pindahkan config ke Secret file-config-<app> lalu mount ke /app/.env (Go) atau /app/appsettings.{env}.json (.NET) — skill file-config-setup. Buat Secret-nya DULU sebelum menambah mount.",
        "params": {
            "applies_to": ["-be-"],
            "secret_prefix": "file-config-",
            "volume_name": "file-config-volume",
            "mount_go": "/app/.env",
            "key_go": ".env",
            "mount_dotnet": "/app/appsettings.{env}.json",
            "key_dotnet": "appsettings.{env}.json",
        },
    },
    "secret_missing": {
        "severity": "critical", "category": "availability",
        "description": "Secret yang dirujuk Deployment belum ada di namespace — pod CreateContainerConfigError / volume gagal mount.",
        "fix_hint": "Buat Secret dari file config (skill secret-from-file) lalu apply ke namespace tujuan.",
        "params": {},
    },
    "missing_image_pullsecret": {
        "severity": "critical", "category": "availability",
        "description": "Deployment dengan image registry privat tanpa imagePullSecrets: regcred (setelah patch kustomization) — ImagePullBackOff.",
        "fix_hint": "Tambah imagePullSecrets: [{name: regcred}] (atau patch kustomization yang meng-inject-nya).",
        "params": {"name": "regcred", "image_prefixes": ["loyaltolpi/", "docker.io/loyaltolpi/"]},
    },
    "appsettings_env_mismatch": {
        "severity": "critical", "category": "config_drift",
        "description": "Aplikasi .NET berjalan dgn environment X (args --environment / env ASPNETCORE_ENVIRONMENT) tapi file yang di-mount appsettings.Y.json — .NET hanya membaca appsettings.<X>.json, config Secret diabaikan.",
        "fix_hint": "Samakan: mount appsettings.<env runtime>.json (key Secret yg sama), atau ubah env runtime ke env file yang di-mount.",
        "params": {"env_vars": ["ASPNETCORE_ENVIRONMENT", "DOTNET_ENVIRONMENT"]},
    },
    "config_mount_path_mismatch": {
        "severity": "critical", "category": "config_drift",
        "description": "File config Secret di-mount di direktori yang tidak dibaca aplikasi — config diabaikan. Aplikasi membaca dari direktori kerja image (WORKDIR stage final, default /) atau tujuan `COPY … .env` di Dockerfile (Go: godotenv.Load() = relatif CWD).",
        "fix_hint": "Pindahkan mountPath ke <WORKDIR>/<file> sesuai Dockerfile (mis. /.env bila tanpa WORKDIR, /app/.env bila WORKDIR /app).",
        "params": {"dockerfiles": ["Dockerfile", "build.Dockerfile", "Dockerfile.production"]},
    },
    "config_format_mismatch": {
        "severity": "critical", "category": "config_drift",
        "description": "Format file config tidak sesuai bahasa: .NET membaca appsettings.<env>.json (kecuali memakai DotNetEnv), bahasa lain membaca .env.",
        "fix_hint": "Mount appsettings.<env runtime>.json untuk .NET (atau tambahkan DotNetEnv), .env untuk Go/Rust/Node/Python.",
        "params": {"dotnet_env_packages": ["DotNetEnv", "dotenv.net"]},
    },
    "dotnet_env_mode": {
        "severity": "info", "category": "template",
        "description": "Pola source .NET → cara environment diberikan: kode yg membaca env var langsung (GetEnvironmentVariable) butuh env ASPNETCORE_ENVIRONMENT; generic/minimal host lain cukup args --environment.",
        "fix_hint": "Tambah pola di params.env_patterns bila ada cara baca env lain.",
        "params": {"env_var": "ASPNETCORE_ENVIRONMENT", "default_mode": "args",
                   "source_files": ["Program.cs", "Startup.cs"],
                   "env_patterns": [r"GetEnvironmentVariable\(\s*\"(ASPNETCORE|DOTNET)_ENVIRONMENT\"\s*\)"],
                   # Nilai env var dipakai dalam LOGIKA → wajib mode env. Hanya untuk AddJsonFile
                   # (redundan dgn host default yg membaca --environment) → args cukup.
                   "logic_patterns": [r"Equals\(\s*{var}\b", r"\b{var}\s*[!=]=", r"[!=]=\s*{var}\b",
                                      r"switch\s*\(\s*{var}\b",
                                      r"\b{var}\??\.(Equals|ToLower|ToUpper|StartsWith|Contains)\b"],
                   "inline_logic_patterns": [r"GetEnvironmentVariable\(\s*\"(ASPNETCORE|DOTNET)_ENVIRONMENT\"\s*\)"
                                             r"\s*(\?\?[^;]*)?([!=]=|\??\.(Equals|ToLower|ToUpper))"]},
    },
    "manifest_sample_source": {
        "severity": "info", "category": "template",
        "description": "Sumber sample YAML untuk saran manifest baru: Deployment+Service tetangga terdekat di repo GitOps <branch>:<infra>/<namespace>.",
        "fix_hint": "Ubah params.branch / params.infra di PB untuk mengganti sumber sample.",
        "params": {"branch": "main", "infra": "cce", "max_candidates": 5},
    },
    "nodepool_mismatch_cce": {
        "severity": "critical", "category": "availability",
        "description": "CCE deployment nodeSelector cce.cloud.com/cce-nodepool tidak sesuai pool namespace — pod Pending.",
        "fix_hint": "Pakai pool <prefix>-manager (manager/apigateway) atau <prefix>-service (module/workflow).",
        "params": {
            "label": "cce.cloud.com/cce-nodepool",
            "by_namespace": {
                "develop-qoin": "develop-qoin", "develop-payout": "develop-qoin",
                "develop-qoinhub": "develop-qoin", "develop-saas": "develop-qoin",
                "develop-ngenwal": "develop-qoincash", "sandbox-qoin": "sandbox-qoin",
                "production-payout": "production-payout-nodepool",
                "production-qoin": "production-nodepool", "staging-qoin": "production-nodepool",
                "production-ngenwal": "production-ue-nodepool", "production-saas": "production-ue-nodepool",
                "staging-ngenwal": "production-ue-nodepool",
            },
        },
    },
}

_SEV_RANK = {"info": 0, "warn": 1, "critical": 2}


# ─── Load rules (PB → cache → default) ────────────────────────────────────

def _read_cache() -> Optional[dict]:
    try:
        return json.loads(CACHE_FILE.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return None


def _merge(records: List[dict]) -> Dict[str, dict]:
    """Record PB menimpa default per rule; `active=false` → rule dibuang."""
    rules = copy.deepcopy(DEFAULT_RULES)
    for r in records:
        name = r.get("rule_name")
        if not name or name not in rules:
            continue  # rule PB tanpa implementasi check di sini — abaikan
        if r.get("active") is False:
            rules.pop(name)
            continue
        base = rules[name]
        for k in ("severity", "category", "description", "fix_hint"):
            if r.get(k):
                base[k] = r[k]
        if isinstance(r.get("params"), dict):
            base["params"] = {**base["params"], **r["params"]}
    return rules


def load_rules(*, force_refresh: bool = False) -> Tuple[Dict[str, dict], str]:
    """Return ({rule_name: rule}, source) — source: pb|cache|stale-cache|default."""
    cache = _read_cache()
    ttl = _ttl_seconds()
    if (cache and not force_refresh and ttl > 0
            and time.time() - cache.get("fetched_at", 0) < ttl):
        return _merge(cache.get("records", [])), "cache"
    try:
        records = pb_api.fetch_collection_records(COLLECTION, per_page=500)
    except Exception as e:  # PB down / collection belum ada → jangan matikan e2e
        if cache:
            print(f"⚠️  {COLLECTION}: {e} — pakai cache lama", file=sys.stderr)
            return _merge(cache.get("records", [])), "stale-cache"
        return _merge([]), "default"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({"fetched_at": int(time.time()), "records": records}, indent=2))
    try:
        CACHE_FILE.chmod(0o600)
    except OSError:
        pass
    return _merge(records), "pb"


# ─── Helpers manifest ─────────────────────────────────────────────────────

@dataclass
class Finding:
    rule_name: str
    severity: str
    category: str
    message: str
    fix_hint: str


@dataclass
class ManifestResult:
    findings: List[Finding] = field(default_factory=list)
    suggestions: List[dict] = field(default_factory=list)  # {title, path, yaml}
    secrets: Dict[str, str] = field(default_factory=dict)  # nama → ada|tidak-ada|repo|tidak-diketahui
    notes: List[str] = field(default_factory=list)

    @property
    def worst(self) -> str:
        if not self.findings:
            return "ok"
        return max(self.findings, key=lambda f: _SEV_RANK.get(f.severity, 0)).severity


def parse_yaml(text: str) -> List[dict]:
    import yaml  # pyyaml — dependency inti sejak v0.2.2

    return [d for d in yaml.safe_load_all(text or "") if isinstance(d, dict)]


def dump_yaml(doc: dict) -> str:
    import yaml

    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)


_KEY_ORDER = {
    "doc": ("apiVersion", "kind", "type", "metadata", "spec", "data", "stringData"),
    "metadata": ("name", "namespace", "labels", "annotations"),
    "spec": ("replicas", "revisionHistoryLimit", "progressDeadlineSeconds", "selector", "strategy",
             "template", "type"),
    "template": ("metadata", "spec"),
}


def _ordered(d: dict, order: Tuple[str, ...]) -> dict:
    return {**{k: d[k] for k in order if k in d}, **{k: v for k, v in d.items() if k not in order}}


def tidy(doc: dict) -> dict:
    """Urutan key konvensional (apiVersion, kind, metadata, spec{replicas, selector, …, template})."""
    out = _ordered(doc, _KEY_ORDER["doc"])
    if isinstance(out.get("metadata"), dict):
        out["metadata"] = _ordered(out["metadata"], _KEY_ORDER["metadata"])
    spec = out.get("spec")
    if isinstance(spec, dict):
        out["spec"] = spec = _ordered(spec, _KEY_ORDER["spec"])
        if isinstance(spec.get("template"), dict):
            spec["template"] = _ordered(spec["template"], _KEY_ORDER["template"])
    return out


def find_kind(docs: List[dict], kind: str) -> Optional[dict]:
    return next((d for d in docs if d.get("kind") == kind), None)


def env_from_namespace(ns: str) -> str:
    return (ns or "").split("-", 1)[0] or "develop"


def role_for(app: str) -> str:
    return "front" if app.endswith(("-manager", "-apigateway")) else "back"


def detect_stack(deploy: Optional[dict], cicd: Optional[dict] = None) -> str:
    stack = str((cicd or {}).get("STACK", "")).lower()
    if "dotnet" in stack or stack == "net":
        return "dotnet"
    for c in _containers(deploy or {}):
        words = [str(w) for w in (c.get("args") or []) + (c.get("command") or [])]
        if any("dotnet" in w or w.endswith(".dll") for w in words):
            return "dotnet"
        if any("appsettings" in str(m.get("subPath", "")) + str(m.get("mountPath", ""))
               for m in c.get("volumeMounts") or []):
            return "dotnet"
        if any(e.get("name") in ("ASPNETCORE_ENVIRONMENT", "DOTNET_ENVIRONMENT") for e in c.get("env") or []):
            return "dotnet"
    return "go"


def _pod(doc: dict) -> dict:
    return ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}


def _containers(doc: dict) -> List[dict]:
    return _pod(doc).get("containers") or []


def _pod_labels(doc: dict) -> dict:
    return (((doc.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels") or {}


def _match_labels(doc: dict) -> dict:
    return ((doc.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}


def _subset(a: dict, b: dict) -> bool:
    return all(b.get(k) == v for k, v in a.items())


def _container_ports(doc: dict) -> List[dict]:
    return [p for c in _containers(doc) for p in c.get("ports") or []]


ENV_VARS = ("ASPNETCORE_ENVIRONMENT", "DOTNET_ENVIRONMENT")


def runtime_env(container: dict) -> Tuple[str, str]:
    """(mode, env) runtime .NET dari container: mode args | env | args+env | '' (default)."""
    arg = next((str(a).split("=", 1)[1] for a in container.get("args") or []
                if str(a).startswith("--environment=")), "")
    var = next((str(e.get("value", "")) for e in container.get("env") or [] if e.get("name") in ENV_VARS), "")
    mode = ("args" if arg else "") + ("+env" if var and arg else ("env" if var else ""))
    return mode, arg or var


def apply_env_mode(container: dict, mode: str, app: str, env_name: str, env_var: str = ENV_VARS[0]) -> None:
    """Cara .NET menerima environment: `args` (dotnet <app>.dll --environment=X), `env`
    (ASPNETCORE_ENVIRONMENT=X), `args+env` (keduanya), atau `none` (tanpa keduanya, default image)."""
    others = [e for e in container.get("env") or [] if e.get("name") not in ENV_VARS]
    if mode in ("args", "args+env"):
        container["args"] = ["dotnet", f"{app}.dll", f"--environment={env_name}"]
    else:
        container.pop("args", None)
    env = others + ([{"name": env_var, "value": env_name}] if mode in ("env", "args+env") else [])
    if env:
        container["env"] = env
    else:
        container.pop("env", None)


def referenced_secrets(deploy: dict) -> List[str]:
    """Nama Secret yang dirujuk pod: volume, envFrom, env.valueFrom (urut, unik)."""
    pod = _pod(deploy)
    names: List[str] = []
    for v in pod.get("volumes") or []:
        names.append(((v.get("secret") or {}).get("secretName")) or "")
    for c in _containers(deploy):
        for ef in c.get("envFrom") or []:
            names.append((ef.get("secretRef") or {}).get("name") or "")
        for e in c.get("env") or []:
            names.append((((e.get("valueFrom") or {}).get("secretKeyRef")) or {}).get("name") or "")
    return list(dict.fromkeys(n for n in names if n))


def _applies(rule: dict, app: str) -> bool:
    pats = rule["params"].get("applies_to") or []
    return not pats or any(p in app for p in pats)


def secret_in_repo(name: str, env: str, files: Set[str]) -> bool:
    """Indikasi Secret ada di repo GitOps: `<name>_secret*.yaml` atau `file-config/<app>-<env>.yaml`."""
    stem = re.escape(name.removeprefix("file-config-"))
    pat = re.compile(rf"(?:file-config-)?{stem}(?:-{re.escape(env)})?(?:_secret(?:_sops)?)?\.ya?ml")
    return any(pat.fullmatch(f.rsplit("/", 1)[-1]) for f in files)


# ─── Kustomization ───────────────────────────────────────────────────────

def parse_kustomization(text: str) -> dict:
    """{namespace, resources (None bila tak ada key), patches} dari kustomization.yaml."""
    import yaml

    data = yaml.safe_load(text or "") or {}
    resources = data.get("resources")
    return {
        "namespace": data.get("namespace") or "",
        "resources": None if resources is None else [str(r).removeprefix("./") for r in resources],
        "patches": list(data.get("patches") or []) + list(data.get("patchesJson6902") or []),
    }


def _json_patch(doc: dict, op: dict) -> None:
    """Terapkan satu operasi JSON6902 (add/replace/remove) secara longgar."""
    parts = [p.replace("~1", "/").replace("~0", "~") for p in str(op["path"]).lstrip("/").split("/")]
    cur = doc
    for i, key in enumerate(parts[:-1]):
        if isinstance(cur, list):
            cur = cur[int(key)]
            continue
        if cur.get(key) is None:
            if op["op"] != "add":
                return
            cur[key] = [] if parts[i + 1].isdigit() or parts[i + 1] == "-" else {}
        cur = cur[key]
    last, kind = parts[-1], op["op"]
    value = copy.deepcopy(op.get("value"))
    if isinstance(cur, list):
        if last == "-":
            if kind == "add":
                cur.append(value)
            return
        idx = int(last)
        if kind == "add":
            cur.insert(idx, value)
        elif kind == "replace":
            cur[idx] = value
        elif kind == "remove":
            del cur[idx]
    elif kind in ("add", "replace"):
        cur[last] = value
    elif kind == "remove":
        cur.pop(last, None)


def _target_matches(target: dict, doc: dict, kind: str, name: str) -> bool:
    if target.get("kind") and target["kind"] != kind:
        return False
    if target.get("name") and not re.fullmatch(str(target["name"]), name or ""):
        return False
    selector = target.get("labelSelector")
    if selector:
        labels = (doc.get("metadata") or {}).get("labels") or {}
        for term in str(selector).split(","):
            k, _, v = term.partition("=")
            if labels.get(k.strip()) != v.strip():
                return False
    return True


def apply_patches(doc: Optional[dict], kz: Optional[dict], kind: str) -> Tuple[Optional[dict], List[str]]:
    """Doc hasil patch JSON6902 kustomization + catatan patch yg tidak dievaluasi."""
    if not doc or not kz or not kz.get("patches"):
        return doc, []
    import yaml

    out, notes = copy.deepcopy(doc), []
    name = (doc.get("metadata") or {}).get("name", "")
    for p in kz["patches"]:
        if not _target_matches(p.get("target") or {}, out, kind, name):
            continue
        body = p.get("patch")
        if not isinstance(body, str):
            notes.append(f"patch file {p.get('path', '?')} tidak dievaluasi")
            continue
        try:
            ops = yaml.safe_load(body)
            if not isinstance(ops, list):
                notes.append("patch strategic-merge tidak dievaluasi")
                continue
            for op in ops:
                _json_patch(out, op)
        except Exception as e:  # patch aneh → nilai dari file mentah saja
            notes.append(f"patch kustomization gagal dievaluasi: {str(e)[:80]}")
    return out, notes


# ─── Generator YAML standar ──────────────────────────────────────────────

def standard_labels(app: str, ns: str) -> dict:
    # project = akhiran namespace (`<env>-<PROJECT>`, sama dgn cicd.json PROJECT) — konvensi
    # dominan di GitOps main (production-qoin 47/50, production-ngenwal 71/71, …).
    project = ns.split("-", 1)[1] if "-" in ns else app.split("-", 1)[0]
    return {"app": app, "env": env_from_namespace(ns), "project": project, "role": role_for(app)}


def node_selector(rules: Dict[str, dict], app: str, ns: str, ctx: Optional[str] = None) -> Tuple[str, str]:
    """(label, nilai) nodeSelector standar: mapping PB per namespace × cluster (label bisa
    `nodetype` mis. production-payout), cadangan params rule nodepool_mismatch_cce."""
    try:
        from . import kube_contexts

        label, value = kube_contexts.node_selector_for(ns, role_for(app), ctx)
    except Exception:
        label, value = "", ""
    if value:
        return label, value
    pool = nodepool_for(rules, app, ns, ctx)
    cce_label = (rules.get("nodepool_mismatch_cce") or {}).get("params", {}).get("label") or ""
    return (cce_label, pool) if pool and cce_label else ("", "")


def nodepool_for(rules: Dict[str, dict], app: str, ns: str, ctx: Optional[str] = None) -> str:
    """Nodepool CCE: mapping PB `namespace.nodepool_front/back` per cluster (kube_contexts),
    cadangan `params.by_namespace` rule nodepool_mismatch_cce."""
    try:
        from . import kube_contexts

        pool = kube_contexts.nodepool_for(ns, role_for(app), ctx)
    except Exception:  # PB/cache tak tersedia → pakai params rule
        pool = ""
    if pool:
        return pool
    rule = rules.get("nodepool_mismatch_cce")
    prefix = ((rule or {}).get("params", {}).get("by_namespace") or {}).get(ns, "")
    if not prefix:
        return ""
    return f"{prefix}-{'manager' if role_for(app) == 'front' else 'service'}"


def suggest_deployment(
    rules: Dict[str, dict], *, app: str, ns: str, image: str, port: int = 80,
    stack: str = "go", infra: str = "cce", base: Optional[dict] = None,
    add_pull_secret: bool = True, add_file_config: bool = True,
    file_config_secret: Optional[str] = None,
) -> dict:
    """Deployment standar Qoin; `base` (YAML lama) dipertahankan bila ada.

    `file_config_secret`: nama Secret config yang sudah ada (mis. `file-config-<app>-develop`);
    default `<secret_prefix><app>`.
    """
    env = env_from_namespace(ns)
    labels = {**standard_labels(app, ns), **(_pod_labels(base) if base else {})}
    doc = copy.deepcopy(base) if base else {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": app, "namespace": ns},
        "spec": {
            "replicas": 2 if env == "production" else 1,
            "strategy": {"type": "RollingUpdate",
                         "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
            "template": {"spec": {"containers": [{
                "name": app, "image": image, "imagePullPolicy": "IfNotPresent",
                "ports": [{"containerPort": port, "name": "http", "protocol": "TCP"}],
                "resources": {"requests": {"cpu": "30m", "memory": "100Mi"},
                              "limits": {"cpu": "500m", "memory": "256Mi"}},
            }]}},
        },
    }
    if stack == "dotnet" and not base:
        doc["spec"]["template"]["spec"]["containers"][0]["args"] = [
            "dotnet", f"{app}.dll", f"--environment={env}"]
    doc.setdefault("metadata", {})["labels"] = {**(doc["metadata"].get("labels") or {}), **labels}
    doc["metadata"]["namespace"] = ns
    spec = doc.setdefault("spec", {})
    # selector immutable — pertahankan matchLabels lama selama masih termuat di label pod.
    if not (_match_labels(doc) and _subset(_match_labels(doc), labels)):
        spec["selector"] = {"matchLabels": dict(labels)}
    spec.setdefault("template", {}).setdefault("metadata", {})["labels"] = dict(labels)
    pod = spec["template"].setdefault("spec", {})

    pull = (rules.get("missing_image_pullsecret") or {}).get("params", {}).get("name")
    if add_pull_secret and pull and pull not in {s.get("name") for s in pod.get("imagePullSecrets") or []}:
        pod.setdefault("imagePullSecrets", []).append({"name": pull})

    fc = rules.get("missing_file_config_volume")
    if add_file_config and fc and _applies(fc, app):
        p = fc["params"]
        secret = file_config_secret or f"{p['secret_prefix']}{app}"
        key = p["key_dotnet" if stack == "dotnet" else "key_go"].format(env=env)
        mount = p["mount_dotnet" if stack == "dotnet" else "mount_go"].format(env=env)
        vols = pod.setdefault("volumes", [])
        # Volume config yg sudah ada (nama volume apa pun, mis. file-config-<app>-<env>) dipakai apa adanya.
        has_fc = any(str((v.get("secret") or {}).get("secretName", "")).startswith(f"{p['secret_prefix']}{app}")
                     for v in vols)
        if not has_fc:
            vols.append({"name": p["volume_name"], "secret": {"secretName": secret, "defaultMode": 420}})
            c0 = pod.setdefault("containers", [{}])[0]
            mounts = c0.setdefault("volumeMounts", [])
            if not any(m.get("name") == p["volume_name"] for m in mounts):
                mounts.append({"name": p["volume_name"], "mountPath": mount, "subPath": key})

    if infra == "cce":
        label, value = node_selector(rules, app, ns)
        if value:
            selector = pod.setdefault("nodeSelector", {})
            cce_label = (rules.get("nodepool_mismatch_cce") or {}).get("params", {}).get("label")
            if label != cce_label:
                selector.pop(cce_label, None)  # konvensi lain (mis. nodetype) → buang label nodepool CCE
            selector[label] = value
    return doc if base else tidy(doc)


def suggest_service(deploy: dict, base: Optional[dict] = None,
                    port_fixes: Optional[Dict[str, int]] = None) -> dict:
    """Service yang selector & port-nya cocok dgn Deployment; `base` = Service lama.

    Pada `base`, port Service dipertahankan; hanya targetPort di `port_fixes`
    ({nama port | nomor port: targetPort baru}) yang diubah.
    """
    meta = deploy.get("metadata") or {}
    selector = _match_labels(deploy) or _pod_labels(deploy)
    if base:
        doc = copy.deepcopy(base)
        doc.setdefault("metadata", {})["namespace"] = meta.get("namespace")
        doc["metadata"]["labels"] = {**(doc["metadata"].get("labels") or {}), **selector}
        doc.setdefault("spec", {})["selector"] = dict(selector)
        for sp in doc["spec"].get("ports") or []:
            key = sp.get("name") or str(sp.get("port"))
            if port_fixes and key in port_fixes:
                sp["targetPort"] = port_fixes[key]
        if not doc["spec"].get("ports"):
            doc["spec"]["ports"] = suggest_service(deploy)["spec"]["ports"]
        return doc
    ports = []
    for p in _container_ports(deploy):
        name = p.get("name") or f"p{p.get('containerPort')}"
        ports.append({"name": name, "port": p.get("containerPort"), "protocol": p.get("protocol", "TCP"),
                      "targetPort": p.get("name") or p.get("containerPort")})
    return tidy({
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": meta.get("name"), "namespace": meta.get("namespace"),
                     "labels": dict(selector)},
        "spec": {"type": "ClusterIP", "selector": dict(selector),
                 "ports": ports or [{"name": "http", "port": 80, "protocol": "TCP", "targetPort": "http"}]},
    })


def suggest_secret(rules: Dict[str, dict], *, name: str, app: str, ns: str, stack: str,
                   env_name: Optional[str] = None) -> dict:
    """Kerangka Secret file-config (nilai diisi dari file config — JANGAN commit plaintext).

    Key: `.env` (semua bahasa) atau `appsettings.<env runtime>.json` (.NET)."""
    env = env_name or env_from_namespace(ns)
    fc = (rules.get("missing_file_config_volume") or DEFAULT_RULES["missing_file_config_volume"])["params"]
    key = fc["key_dotnet" if stack == "dotnet" else "key_go"].format(env=env)
    return tidy({
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": name, "namespace": ns, "labels": {"app": app, "env": env}},
        "stringData": {key: f"<isi dari {'appsettings.' + env + '.json' if stack == 'dotnet' else '.env.' + env}>"},
    })


def _secret_suggestion(rules: Dict[str, dict], *, name: str, app: str, ns: str, stack: str,
                       infra: str, env_name: Optional[str] = None) -> dict:
    return {
        "title": f"Secret {name} (isi dari file config, apply — jangan commit plaintext)",
        "path": f"{infra}/{ns}/{name}_secret.yaml",
        "yaml": dump_yaml(suggest_secret(rules, name=name, app=app, ns=ns, stack=stack, env_name=env_name)),
    }


# ─── Template standar terpusat (PocketBase `build_q_manifest_templates`) ────

TEMPLATE_COLLECTION = "build_q_manifest_templates"
TEMPLATE_CACHE = CONFIG_DIR / ".manifest-templates-cache.json"
TEMPLATE_APP = "__app__"  # placeholder nama aplikasi di template (diganti saat adaptasi)


def app_kind(app: str) -> str:
    """Jenis aplikasi dari akhiran nama: manager | module | apigateway | workflow | …"""
    return app.rsplit("-", 1)[-1]


def load_templates(*, force_refresh: bool = False) -> Tuple[List[dict], str]:
    """Template aktif dari PB → cache → [] (source: pb|cache|stale-cache|none)."""
    try:
        cache = json.loads(TEMPLATE_CACHE.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        cache = None
    ttl = _ttl_seconds()
    if (cache and not force_refresh and ttl > 0
            and time.time() - cache.get("fetched_at", 0) < ttl):
        return cache.get("records", []), "cache"
    try:
        records = pb_api.fetch_collection_records(TEMPLATE_COLLECTION, filter_="active=true", per_page=500)
    except Exception:
        return (cache.get("records", []), "stale-cache") if cache else ([], "none")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATE_CACHE.write_text(json.dumps({"fetched_at": int(time.time()), "records": records}))
    try:
        TEMPLATE_CACHE.chmod(0o600)
    except OSError:
        pass
    return records, "pb"


def find_template(templates: List[dict], *, infra: str, namespace: str, app: str,
                  exclude_source_app: str = "") -> Optional[dict]:
    """Template paling spesifik (namespace persis > `*`) untuk jenis aplikasi tsb → bentuk `sample`."""
    kind = app_kind(app)
    matches = [t for t in templates
               if t.get("active", True) and t.get("infra", "cce") == infra
               and t.get("app_kind") in (kind, "*") and t.get("namespace") in (namespace, "*")
               and (not exclude_source_app or t.get("source_app") != exclude_source_app)
               and isinstance(t.get("deployment"), dict) and isinstance(t.get("service"), dict)]
    if not matches:
        return None
    t = min(matches, key=lambda t: (t.get("namespace") == "*", t.get("app_kind") == "*", t.get("order") or 0))
    return {"app": TEMPLATE_APP, "deploy": t["deployment"], "service": t["service"],
            "path": f"template {t.get('name')}", "standard": True, "template": t.get("name")}


def make_template(deploy: dict, service: dict, *, app: str, infra: str, namespace: str,
                  source: str) -> dict:
    """Record template dari manifest standar: field runtime dibuang, nama app → placeholder."""
    return {
        "name": f"{infra}-{namespace}-{app_kind(app)}",
        "infra": infra, "namespace": namespace, "app_kind": app_kind(app),
        "stack": detect_stack(deploy),
        "deployment": _renamed(deploy, app, TEMPLATE_APP),
        "service": _renamed(service, app, TEMPLATE_APP),
        "source": source, "source_app": app,
        "description": f"Template standar {app_kind(app)} di {infra}/{namespace} (dari {source})",
        "active": True, "order": 0,
    }


# ─── Sample YAML dari GitOps (default main:cce/<namespace>) ──────────────

SAMPLE_RULE = "manifest_sample_source"
_RUNTIME_ANNOTATIONS = ("argocd.argoproj.io/", "meta.helm.sh/", "deployment.kubernetes.io/",
                        "kubectl.kubernetes.io/")


def sample_source(rules: Dict[str, dict]) -> dict:
    return {**DEFAULT_RULES[SAMPLE_RULE]["params"], **((rules.get(SAMPLE_RULE) or {}).get("params") or {})}


def rank_samples(files: List[str], app: str) -> List[str]:
    """Nama app kandidat sample dari daftar file folder: role (suffix) & project sama dulu."""
    apps = [f.removesuffix("_deployment.yaml") for f in files if f.endswith("_deployment.yaml")]
    suffix, parts = app.rsplit("-", 1)[-1], app.split("-")

    def score(a: str) -> tuple:
        other = a.split("-")
        return (a.rsplit("-", 1)[-1] == suffix, other[:1] == parts[:1], other[1:2] == parts[1:2],
                -abs(len(a) - len(app)))

    return sorted((a for a in apps if a != app), key=score, reverse=True)


def strip_runtime(doc: dict) -> dict:
    """Buang field runtime/Helm/ArgoCD dari manifest sample."""
    out = copy.deepcopy(doc)
    out.pop("status", None)
    meta = out.setdefault("metadata", {})
    for k in ("uid", "resourceVersion", "creationTimestamp", "generation", "managedFields", "selfLink"):
        meta.pop(k, None)
    ann = {k: v for k, v in (meta.get("annotations") or {}).items() if not k.startswith(_RUNTIME_ANNOTATIONS)}
    if ann:
        meta["annotations"] = ann
    else:
        meta.pop("annotations", None)
    labels = meta.get("labels") or {}
    if labels.get("app.kubernetes.io/managed-by") == "Helm":
        labels.pop("app.kubernetes.io/managed-by")
    # Label tracking ArgoCD — bila tersalin, resource baru dianggap milik Application lain.
    labels.pop("app.kubernetes.io/instance", None)
    spec = out.get("spec") or {}
    for k in ("clusterIP", "clusterIPs"):
        spec.pop(k, None)
    return out


def _renamed(doc: dict, old: str, new: str) -> dict:
    return parse_yaml(dump_yaml(strip_runtime(doc)).replace(old, new))[0]


def adapt_sample_deployment(
    rules: Dict[str, dict], sample: dict, *, app: str, ns: str, image: str,
    port: Optional[int] = None, stack: str = "go", infra: str = "cce", kz: Optional[dict] = None,
    ports: Optional[List[dict]] = None, config_mount_dir: Optional[str] = None,
    config_mount_paths: Optional[List[str]] = None, dotnet_args: Optional[bool] = None,
    config_secret: Optional[bool] = None, env_mode: Optional[str] = None,
) -> dict:
    """Deployment baru dari sample tetangga: ganti nama/label/image/port/stack, lalu standar rule.

    `ports` (daftar containerPort lengkap, mis. http + grpc) menimpa `port`;
    `config_mount_dir` = direktori kerja aplikasi (WORKDIR Dockerfile) untuk mount file config.
    """
    s_app = sample["app"]
    doc = _renamed(sample["deploy"], s_app, app)
    s_labels = _pod_labels(doc)
    env = env_from_namespace(ns)  # selalu standar: env/replicas sample bisa menyimpang
    labels = {**s_labels, **standard_labels(app, ns)}
    doc["metadata"].update({"name": app, "namespace": ns})
    doc.setdefault("spec", {})["replicas"] = 2 if env == "production" else 1
    doc["metadata"]["labels"] = {**(doc["metadata"].get("labels") or {}), **labels}
    keys = [k for k in (_match_labels(doc) or labels) if k in labels]
    doc.setdefault("spec", {})["selector"] = {"matchLabels": {k: labels[k] for k in keys}}
    doc["spec"].setdefault("template", {}).setdefault("metadata", {})["labels"] = labels
    pod = _pod(doc)
    c0 = (pod.get("containers") or [{}])[0]
    c0.update({"name": app, "image": image})
    if ports:
        c0["ports"] = [{k: v for k, v in (("containerPort", p.get("containerPort")), ("name", p.get("name")),
                                          ("protocol", p.get("protocol", "TCP"))) if v is not None}
                       for p in ports]
    elif port:
        first = dict((c0.get("ports") or [{}])[0])
        first.update({"containerPort": port})
        first.setdefault("name", "http")
        first.setdefault("protocol", "TCP")
        c0["ports"] = [first]

    fc = (rules.get("missing_file_config_volume") or DEFAULT_RULES["missing_file_config_volume"])["params"]
    fc_vols = {v.get("name") for v in pod.get("volumes") or []
               if str((v.get("secret") or {}).get("secretName", "")).startswith(fc["secret_prefix"])}
    fc_mounts = [m for m in c0.get("volumeMounts") or [] if m.get("name") in fc_vols]
    # Nama Secret config dinormalkan: sample `file-config-<s>-staging` di production → `file-config-<app>`.
    for v in pod.get("volumes") or []:
        name = str((v.get("secret") or {}).get("secretName", ""))
        if name.startswith(fc["secret_prefix"]) and name not in (f"{fc['secret_prefix']}{app}",
                                                                   f"{fc['secret_prefix']}{app}-{env}"):
            v["secret"]["secretName"] = f"{fc['secret_prefix']}{app}"
    if stack != detect_stack(sample["deploy"]):
        if stack == "dotnet":
            c0["args"] = ["dotnet", f"{app}.dll", f"--environment={env}"]
        else:
            c0.pop("args", None)
            c0["env"] = [e for e in c0.get("env") or [] if e.get("name") not in ENV_VARS]
            if not c0["env"]:
                c0.pop("env")
        for m in fc_mounts:
            m["mountPath"] = fc[f"mount_{stack}"].format(env=env)
            m["subPath"] = fc[f"key_{stack}"].format(env=env)
    if fc_mounts and (config_mount_paths or config_mount_dir):
        # Satu mount config per path (sample bisa punya 2 mount config — jangan diwariskan).
        first = fc_mounts[0]
        key = first.get("subPath") or posixpath.basename(first.get("mountPath", ""))
        paths = config_mount_paths or [f"{config_mount_dir.rstrip('/')}/{key}"]
        others = [m for m in c0.get("volumeMounts") or [] if m not in fc_mounts]
        c0["volumeMounts"] = others + [{**first, "mountPath": path, "subPath": posixpath.basename(path)}
                                       for path in paths]
    if config_secret is False:
        # Aplikasi memilih config baked di image → tanpa volume/mount Secret config.
        pod["volumes"] = [v for v in pod.get("volumes") or [] if v.get("name") not in fc_vols]
        c0["volumeMounts"] = [m for m in c0.get("volumeMounts") or [] if m.get("name") not in fc_vols]
        for key in ("volumes",):
            if not pod[key]:
                pod.pop(key)
        if not c0["volumeMounts"]:
            c0.pop("volumeMounts")
    if stack == "dotnet":
        # Env runtime mengikuti konvensi sample (mis. develop-ngenwal = production), lalu mode dari source.
        s_mode, s_env = runtime_env(c0)
        env_name = s_env or env
        if env_mode or not s_mode:
            apply_env_mode(c0, env_mode or "args", app, env_name)
        # appsettings yang di-mount HARUS sama dgn env runtime (kalau beda, .NET mengabaikannya).
        for m in fc_mounts:
            if "appsettings" in str(m.get("subPath", "")) + str(m.get("mountPath", "")):
                key = f"appsettings.{env_name}.json"
                m["mountPath"] = f"{posixpath.dirname(m.get('mountPath') or '/app/x')}/{key}"
                m["subPath"] = key
    if stack == "dotnet" and dotnet_args is not None and env_mode is None:
        if dotnet_args:
            c0["args"] = ["dotnet", f"{app}.dll", f"--environment={env}"]
        else:
            c0.pop("args", None)
    label = (rules.get("nodepool_mismatch_cce") or {}).get("params", {}).get("label")
    if infra != "cce":
        # Sample CCE untuk target SLS (k8s/): nodepool CCE tidak berlaku di sana.
        (pod.get("nodeSelector") or {}).pop(label, None)
        if not pod.get("nodeSelector"):
            pod.pop("nodeSelector", None)
    elif role_for(app) != role_for(s_app):
        # nodeSelector role sample tidak berlaku → diganti standar (suggest_deployment di bawah).
        for key in ("nodetype", label):
            (pod.get("nodeSelector") or {}).pop(key, None)

    pull_params = (rules.get("missing_image_pullsecret") or {}).get("params", {})
    pull = pull_params.get("name")
    prefixes = tuple(pull_params.get("image_prefixes") or ())
    if prefixes and not image.startswith(prefixes):
        # Image publik (mis. nginx) tidak butuh regcred dari sample.
        pod["imagePullSecrets"] = [s for s in pod.get("imagePullSecrets") or [] if s.get("name") != pull]
        if not pod["imagePullSecrets"]:
            pod.pop("imagePullSecrets")
    patched, _ = apply_patches(doc, kz, "Deployment")
    injected = pull in {s.get("name") for s in _pod(patched).get("imagePullSecrets") or []} or (
        bool(prefixes) and not image.startswith(prefixes))
    return tidy(suggest_deployment(rules, app=app, ns=ns, image=image, stack=stack, infra=infra, base=doc,
                                   add_pull_secret=not injected, add_file_config=config_secret is not False))


def adapt_sample_service(sample: dict, deploy: dict, *, service_type: Optional[str] = None,
                         exposed_ports: Optional[List[int]] = None) -> dict:
    """Service baru dari sample tetangga: selector & port mengikuti Deployment target.

    Konvensi "port klien tetap" sample (mis. Service port 80 → container 8810) diikuti.
    `service_type` / `exposed_ports` = keputusan aplikasi (NodePort, port yang diekspos).
    """
    app = (deploy.get("metadata") or {}).get("name", "")
    doc = _renamed(sample["service"], sample["app"], app)
    ports = suggest_service(deploy)["spec"]["ports"]
    if exposed_ports:
        ports = [p for p in ports if p["port"] in exposed_ports] or ports
    s_ports = (sample["service"].get("spec") or {}).get("ports") or []
    s_cports = {str(p.get("containerPort")) for p in _container_ports(sample["deploy"])}
    s_named = {p.get("name"): p.get("containerPort") for p in _container_ports(sample["deploy"]) if p.get("name")}
    if len(s_ports) == 1 and ports:
        tp = s_ports[0].get("targetPort", s_ports[0].get("port"))
        target = s_named.get(tp, tp) if isinstance(tp, str) and not tp.isdigit() else tp
        if str(s_ports[0].get("port")) != str(target) and str(target) in s_cports:
            ports[0] = {**ports[0], "port": s_ports[0].get("port")}  # port klien tetap (mis. 80)
    doc.setdefault("spec", {})["ports"] = ports
    if service_type:
        doc["spec"]["type"] = service_type
    doc["metadata"]["name"] = app
    return tidy(suggest_service(deploy, base=doc))


# ─── Pemeriksaan ─────────────────────────────────────────────────────────

def check(
    rules: Dict[str, dict], *, deploy: dict, service: Optional[dict], app: str, ns: str,
    deploy_path: str, service_path: str, cicd: Optional[dict] = None,
    secrets_in_cluster: Optional[Set[str]] = None, secrets_in_repo: Optional[Set[str]] = None,
    kustomization: Optional[dict] = None, sample: Optional[dict] = None,
) -> ManifestResult:
    """Cek Deployment ↔ Service ↔ Secret pada hasil akhir kustomization.

    `ns` = namespace efektif; `secrets_in_cluster` None = cluster tak terjangkau;
    `sample` = {app, deploy, service, path} tetangga (dasar saran Service baru).
    """
    res = ManifestResult()

    def add(rule_name: str, message: str, severity: Optional[str] = None) -> None:
        r = rules.get(rule_name)
        if r:
            res.findings.append(Finding(rule_name, severity or r["severity"], r["category"],
                                        message, r["fix_hint"]))

    stack = detect_stack(deploy, cicd)
    infra = deploy_path.split("/", 1)[0]
    env = env_from_namespace(ns)
    kz = kustomization or {}

    # File harus terdaftar di kustomization agar ikut di-apply ArgoCD.
    if kz.get("resources") is not None:
        files = [deploy_path] + ([service_path] if service is not None and service_path != deploy_path else [])
        for f in files:
            if f.rsplit("/", 1)[-1] not in kz["resources"]:
                add("manifest_not_in_kustomization", f"{f.rsplit('/', 1)[-1]} tidak ada di resources kustomization.yaml")

    raw_deploy, raw_service = deploy, service
    deploy, notes = apply_patches(deploy, kz, "Deployment")
    res.notes += notes
    if service is not None:
        service, notes = apply_patches(service, kz, "Service")
        res.notes += notes

    pod_labels = _pod_labels(deploy)
    match = _match_labels(deploy)
    fix_deploy = False

    if match and not _subset(match, pod_labels):
        add("deploy_selector_template_mismatch",
            f"matchLabels {match} tidak termuat di label pod {pod_labels}")
        fix_deploy = True
    req = (rules.get("missing_standard_labels") or {}).get("params", {}).get("required_labels") or []
    missing = [k for k in req if k not in pod_labels]
    if missing:
        add("missing_standard_labels", f"label pod tanpa {', '.join(missing)}")
        fix_deploy = True

    pull_rule = rules.get("missing_image_pullsecret")
    pull = (pull_rule or {}).get("params", {}).get("name")
    pulls = {s.get("name") for s in _pod(deploy).get("imagePullSecrets") or []}
    prefixes = tuple((pull_rule or {}).get("params", {}).get("image_prefixes") or ())
    images = [c.get("image") or "" for c in _containers(deploy)]
    private = not prefixes or any(i.startswith(prefixes) for i in images)
    if pull and private and pull not in pulls:
        add("missing_image_pullsecret", f"imagePullSecrets tanpa {pull} (image {', '.join(images)})")
        fix_deploy = True

    if stack == "dotnet" and rules.get("appsettings_env_mismatch"):
        mode, run_env = runtime_env(_containers(deploy)[0])
        for m in _containers(deploy)[0].get("volumeMounts") or []:
            hit = re.search(r"appsettings\.([^/]+)\.json$", str(m.get("subPath") or m.get("mountPath") or ""))
            if hit and run_env and hit.group(1) != run_env:
                add("appsettings_env_mismatch",
                    f"environment runtime `{run_env}` ({mode}) tapi yang di-mount {hit.group(0)} — "
                    f".NET membaca appsettings.{run_env}.json, config Secret diabaikan")

    fc = rules.get("missing_file_config_volume")
    refs = referenced_secrets(deploy)
    fc_missing, fc_existing = "", ""
    if fc and _applies(fc, app):
        want = f"{fc['params']['secret_prefix']}{app}"
        variants = [want, f"{want}-{env}"]
        if not any(v in refs for v in variants):
            # Secret config bisa sudah ada dgn nama lain (mis. file-config-<app>-develop) tapi tak di-mount.
            if secrets_in_cluster is not None:
                fc_existing = next((v for v in variants if v in secrets_in_cluster), "")
            msg = f"tidak ada volume Secret {want} — config kemungkinan baked di image"
            if fc_existing:
                msg += f"; Secret {fc_existing} sudah ada di namespace tetapi tidak di-mount"
            elif secrets_in_repo:
                files = sorted(f for f in secrets_in_repo
                               if any(secret_in_repo(v, env, {f}) for v in variants))
                if files:
                    res.notes.append(f"di repo ada {', '.join(files)} — pastikan nama Secret-nya "
                                     f"(mis. {variants[1]}) sebelum menerapkan saran mount")
            add("missing_file_config_volume", msg)
            fix_deploy = True
            fc_missing = fc_existing or want

    # Keberadaan Secret yang DIRUJUK: cluster (pasti) → repo (indikasi) → tidak diketahui.
    for name in refs + ([pull] if pull and pull in pulls else []):
        if secrets_in_cluster is not None:
            state = "ada" if name in secrets_in_cluster else "tidak-ada"
        elif secrets_in_repo and secret_in_repo(name, env, secrets_in_repo):
            state = "repo"
        else:
            state = "tidak-diketahui"
        res.secrets[name] = state
        if state != "tidak-ada":
            continue
        if name == pull:
            add("secret_missing", f"imagePullSecret {name} belum ada di namespace {ns} — salin dari "
                                  f"namespace lain / `kubectl create secret docker-registry {name}`")
        else:
            add("secret_missing", f"Secret {name} belum ada di namespace {ns}")
            res.suggestions.append(_secret_suggestion(rules, name=name, app=app, ns=ns, stack=stack, infra=infra,
                                                      env_name=runtime_env(_containers(deploy)[0])[1] or None))
    if fc_missing and not fc_existing:
        res.suggestions.append(_secret_suggestion(rules, name=fc_missing, app=app, ns=ns, stack=stack, infra=infra))

    fixed_deploy = deploy
    if fix_deploy:
        image = next((i for i in images if i), "")
        fixed_deploy = suggest_deployment(
            rules, app=app, ns=ns, image=image, stack=stack, infra=infra, base=raw_deploy,
            add_pull_secret=pull not in pulls, add_file_config=bool(fc_missing),
            file_config_secret=fc_missing or None,
        )
    add_ports: List[dict] = []  # containerPort bernama yang belum dideklarasikan

    def deployment_suggestion() -> None:
        if not (fix_deploy or add_ports):
            return
        doc = copy.deepcopy(fixed_deploy if fix_deploy else raw_deploy)
        if add_ports:
            c0 = _pod(doc)["containers"][0]
            c0["ports"] = list(c0.get("ports") or []) + add_ports
        res.suggestions.insert(0, {"title": "Deployment (selector/label/port/secret sesuai standar)",
                                   "path": deploy_path, "yaml": dump_yaml(doc)})

    ports = _container_ports(deploy)
    if service is None:
        deployment_suggestion()
        if not ports:
            res.notes.append("Deployment tanpa containerPort (worker) — Service tidak wajib")
            return res
        add("service_missing", f"{service_path} tidak ada")
        if sample:
            svc, title = adapt_sample_service(sample, fixed_deploy), f"Service baru (dari sample {sample['path']})"
        else:
            svc, title = suggest_service(fixed_deploy), "Service baru (selector = label pod Deployment)"
        res.suggestions.append({"title": title, "path": service_path, "yaml": dump_yaml(svc)})
        return res

    fix_svc = False
    svc_ns = (service.get("metadata") or {}).get("namespace")
    dep_ns = (deploy.get("metadata") or {}).get("namespace")
    if not kz.get("namespace") and svc_ns and dep_ns and svc_ns != dep_ns:
        add("service_namespace_mismatch", f"Service di {svc_ns}, Deployment di {dep_ns}")
        fix_svc = True
    selector = (service.get("spec") or {}).get("selector") or {}
    if not selector or not _subset(selector, pod_labels):
        add("service_selector_no_match", f"selector Service {selector} ≠ label pod {pod_labels}")
        fix_svc = True
    names = {str(p.get("name")) for p in ports if p.get("name")}
    numbers = {str(p.get("containerPort")) for p in ports}
    port_fixes: Dict[str, int] = {}  # nama/nomor port Service → targetPort angka
    for sp in (service.get("spec") or {}).get("ports") or []:
        tp = str(sp.get("targetPort", sp.get("port")))
        sport = str(sp.get("port"))
        if tp.isdigit():
            # Port angka tetap jalan bila aplikasi listen di sana; containerPort hanya informasi.
            if numbers and tp not in numbers:
                add("service_port_targetport_mismatch",
                    f"targetPort {tp} tidak dideklarasikan sebagai containerPort ({', '.join(sorted(numbers))})"
                    " — pastikan aplikasi listen di port ini", severity="warn")
            continue
        if tp in names:
            continue
        add("service_port_targetport_mismatch",
            f"targetPort bernama `{tp}` tidak ada di containerPort "
            f"({', '.join(sorted(names)) or 'tanpa nama'}) — endpoint kosong")
        if sport in numbers:
            # Nomor port Service memang dideklarasikan container → arahkan ke nomor itu.
            port_fixes[sp.get("name") or sport] = int(sport)
            fix_svc = True
        elif sport.isdigit():
            # Tidak bisa ditebak dari Service → deklarasikan port bernama itu di Deployment.
            add_ports.append({"containerPort": int(sport), "name": tp, "protocol": sp.get("protocol", "TCP")})
    deployment_suggestion()
    if fix_svc:
        res.suggestions.append({"title": "Service (selector/namespace/targetPort disesuaikan dgn Deployment)",
                                "path": service_path,
                                "yaml": dump_yaml(suggest_service(fixed_deploy, raw_service, port_fixes=port_fixes))})
    if add_ports:
        res.notes.append("port " + ", ".join(f"{p['name']}={p['containerPort']}" for p in add_ports)
                         + " ditambahkan di saran Deployment — pastikan nomornya sama dgn port aplikasi")
    return res
