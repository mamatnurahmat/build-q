"""`bq --manifest-check <namespace>` — uji tahap Manifest untuk SEMUA Deployment di satu
folder namespace GitOps, dengan laporan yang bisa diekspor (Markdown / JSON).

Read-only. Sumber:
  - manifest : GitHub API repo GitOps `<branch>:<infra>/<namespace>/` — branch default
               mengikuti ENV namespace (develop/staging/sandbox → branch yang sama,
               lainnya → GITOPS_BRANCH/main), sama dengan tahap GitOps `bq --e2e`.
  - Secret   : `kubectl get secrets -o name` SEKALI per namespace (isi tidak dibaca).
               Context: destination Application ArgoCD → mapping `kube_cx` → kandidat
               namespace ambigu dgn Deployment terbanyak. Tak terjangkau → indikasi repo.
  - rule     : PocketBase `build_q_anomaly_rules` (sama dgn tahap Manifest `bq --e2e`).

Pemeriksaan per Deployment identik dengan tahap Manifest (`manifest_check.check`),
dijalankan paralel. ContextVar credential (mode web, per-user) ikut ke tiap thread.
"""
from __future__ import annotations

import collections
import contextvars
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import __version__

STATUS_ICON = {"ok": "✅", "fail": "❌", "skip": "⏭️"}
STATUS_LABEL = {"ok": "OK", "fail": "FAIL", "skip": "SKIP"}
_NAMESPACE_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


class ManifestScanError(RuntimeError):
    """Namespace/folder tidak valid atau tidak ada di repo GitOps."""


@dataclass
class AppResult:
    app: str
    status: str  # ok | fail | skip
    deploy_path: str
    service_path: str = ""
    findings: List[dict] = field(default_factory=list)
    secrets: Dict[str, str] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    suggestions: List[dict] = field(default_factory=list)
    error: str = ""

    @property
    def critical(self) -> List[str]:
        return list(dict.fromkeys(f["rule_name"] for f in self.findings if f["severity"] == "critical"))

    @property
    def advisory(self) -> List[str]:
        return list(dict.fromkeys(f["rule_name"] for f in self.findings if f["severity"] != "critical"))

    @property
    def unknown_secrets(self) -> List[str]:
        return [n for n, s in self.secrets.items() if s in ("repo", "tidak-diketahui")]


@dataclass
class ScanReport:
    namespace: str
    folder: str
    gitops_repo: str
    branch: str
    secret_source: str = ""
    rules_source: str = ""
    rules_count: int = 0
    sample_source: str = ""
    generated_at: str = ""
    version: str = __version__
    details: List[str] = field(default_factory=list)
    items: List[AppResult] = field(default_factory=list)

    @property
    def totals(self) -> Dict[str, int]:
        c = collections.Counter(i.status for i in self.items)
        return {"ok": c["ok"], "fail": c["fail"], "skip": c["skip"], "total": len(self.items)}

    @property
    def rule_counts(self) -> List[dict]:
        c = collections.Counter((f["rule_name"], f["severity"]) for i in self.items for f in i.findings)
        return [{"rule_name": r, "severity": s, "count": n} for (r, s), n in c.most_common()]

    @property
    def exit_code(self) -> int:
        """0 semua OK, 1 ada FAIL, 2 ada SKIP (secret belum terverifikasi / error)."""
        t = self.totals
        return 1 if t["fail"] else (2 if t["skip"] else 0)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(totals=self.totals, rule_counts=self.rule_counts, exit_code=self.exit_code)
        return d


# ─── Cluster: destination ArgoCD & Secret per namespace ──────────────────

def _argocd_destination(repo: str, folder: str, branch: str, config: dict, details: List[str]) -> dict:
    from . import e2e

    try:
        apps = e2e.kubectl_json(config["argocd"]["k8s_context"],
                                ["-n", config["argocd"]["k8s_namespace"], "get",
                                 "applications.argoproj.io"]).get("items", [])
    except e2e.KubectlError:
        try:
            apps = e2e.argocd_api_apps(config)
        except Exception:
            details.append("Application ArgoCD tidak terbaca — context dari mapping kube_cx")
            return {}
    app = e2e.find_argocd_app(apps, repo, folder, branch)
    if app is None:
        details.append(f"tidak ada Application ArgoCD untuk {folder}@{branch} — context dari mapping kube_cx")
        return {}
    dest = (app.get("spec") or {}).get("destination") or {}
    details.append(f"Application ArgoCD {app['metadata']['name']} → destination "
                   f"{dest.get('server') or dest.get('name') or '?'}")
    return dest


def _namespace_secrets(ns: str, apps: List[str], dest: dict, config: dict, infra: str,
                       details: List[str]) -> Tuple[Optional[set], str]:
    """(nama Secret di namespace | None, keterangan sumber)."""
    from . import e2e, kube_contexts

    ctx = kube_contexts.context_for_destination(dest, config["argocd"]["k8s_context"])
    if ctx:
        candidates, why = [ctx], "destination ArgoCD"
    elif infra != "cce":
        details.append(f"infra {infra}/ (SLS) tidak ada di mapping kube_cx — secret dicek dari repo GitOps")
        return None, "repo GitOps (infra SLS)"
    else:
        ctx, why = kube_contexts.resolve(ns)
        candidates = [ctx] if ctx else kube_contexts.candidates(ns)
    if not candidates:
        return None, f"repo GitOps ({why})"
    chosen = candidates[0]
    if len(candidates) > 1:
        # Namespace ambigu → cluster yang menjalankan Deployment folder ini paling banyak.
        best, best_n = "", 0
        for cand in candidates:
            try:
                n = len(set(e2e.kubectl_names(cand, ns, "deployments")) & set(apps))
            except e2e.KubectlError as e:
                details.append(f"cluster {cand} tidak terjangkau ({str(e)[:80]})")
                continue
            details.append(f"{cand}/{ns}: {n} dari {len(apps)} Deployment ada di sini")
            if n > best_n:
                best, best_n = cand, n
        if not best:
            return None, "repo GitOps (cluster kandidat tidak bisa dipastikan)"
        chosen, why = best, f"namespace ambigu → cluster dgn {best_n} Deployment"
    try:
        names = set(e2e.kubectl_names(chosen, ns, "secrets"))
    except e2e.KubectlError as e:
        details.append(f"cluster {chosen} tidak terjangkau ({str(e)[:80]})")
        return None, f"repo GitOps (cluster {chosen} tidak terjangkau)"
    return names, f"cluster {chosen}/{ns} ({len(names)} secret; {why})"


# ─── Scan ────────────────────────────────────────────────────────────────

def scan(
    namespace: str,
    *,
    branch: Optional[str] = None,
    infra: str = "cce",
    gitops_repo: Optional[str] = None,
    config: Optional[dict] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    workers: int = 8,
) -> ScanReport:
    """Uji semua `*_deployment.yaml` di `<infra>/<namespace>/` repo GitOps. Read-only."""
    from . import e2e, manifest_check as mc
    from .config import load_config

    folder_ns = (namespace or "").strip().strip("/")
    if not _NAMESPACE_RE.fullmatch(folder_ns):
        raise ManifestScanError(f"namespace tidak valid: {namespace!r} (contoh: develop-qoin)")
    if infra not in ("cce", "k8s"):
        raise ManifestScanError(f"infra tidak valid: {infra!r} (cce | k8s)")
    config = config or load_config()
    repo = gitops_repo or config["gitops"]["repo"]
    branch = branch or e2e.gitops_branch_for(mc.env_from_namespace(folder_ns), config["gitops"]["branch"])
    folder = f"{infra}/{folder_ns}"

    rules, rules_src = mc.load_rules()
    src = mc.sample_source(rules)
    rep = ScanReport(
        namespace=folder_ns, folder=folder, gitops_repo=repo, branch=branch,
        rules_source=rules_src, rules_count=len(rules),
        sample_source=f"{src['branch']}:{src['infra']}/{folder_ns}",
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    files = e2e.gh_list_dir(repo, folder, branch)
    apps = sorted(f.removesuffix("_deployment.yaml") for f in files if f.endswith("_deployment.yaml"))
    if not apps:
        raise ManifestScanError(f"tidak ada *_deployment.yaml di {repo}@{branch}:{folder}/")

    kz, note = e2e._fetch_kustomization(repo, folder, branch)
    if note:
        rep.details.append(note)
    ns = (kz or {}).get("namespace") or folder_ns
    rep.namespace = ns
    if ns != folder_ns:
        rep.details.append(f"namespace efektif: {ns} (folder {folder_ns})")
    dest = _argocd_destination(repo, folder, branch, config, rep.details)
    secrets, rep.secret_source = _namespace_secrets(ns, apps, dest, config, infra, rep.details)
    in_repo = None
    if secrets is None:
        in_repo = set(files) | set(e2e.gh_list_dir(repo, f"{folder}/file-config", branch))
    suffixes = (rules.get("service_missing") or mc.DEFAULT_RULES["service_missing"])["params"]["suffixes"]
    sample_cache: dict = {}
    lock, done = threading.Lock(), [0]

    def check_app(app: str) -> AppResult:
        path = f"{folder}/{app}_deployment.yaml"
        try:
            docs = mc.parse_yaml(e2e.gh_raw(repo, path, branch))
            deploy = mc.find_kind(docs, "Deployment")
            if deploy is None:
                return AppResult(app, "skip", path, error="file tanpa kind Deployment")
            service, spath = mc.find_kind(docs, "Service"), path
            if service is None:
                service, spath = e2e._fetch_service(repo, path, branch, suffixes)
            app_ns = (kz or {}).get("namespace") or (deploy.get("metadata") or {}).get("namespace") or ns
            sample = None
            if service is None and mc._container_ports(deploy):
                sample = e2e._manifest_sample(repo, folder, app, rules, cache=sample_cache)
            same_ns = app_ns == ns
            res = mc.check(rules, deploy=deploy, service=service, app=app, ns=app_ns,
                           deploy_path=path, service_path=spath,
                           secrets_in_cluster=secrets if same_ns else None,
                           secrets_in_repo=in_repo if same_ns else None,
                           kustomization=kz, sample=sample)
            critical = any(f.severity == "critical" for f in res.findings)
            unknown = any(s in ("repo", "tidak-diketahui") for s in res.secrets.values())
            return AppResult(
                app, "fail" if critical else ("skip" if unknown else "ok"), path, spath,
                [vars(f) for f in res.findings], dict(res.secrets), list(res.notes), list(res.suggestions),
            )
        except Exception as e:  # satu aplikasi bermasalah tidak menghentikan scan
            return AppResult(app, "skip", path, error=str(e)[:200])
        finally:
            with lock:
                done[0] += 1
                n = done[0]
            if on_progress:
                on_progress(n, len(apps), app)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(apps)))) as pool:
        futures = [pool.submit(contextvars.copy_context().run, check_app, app) for app in apps]
        rep.items = [f.result() for f in futures]
    return rep


# ─── Render & export ──────────────────────────────────────────────────────

def _status(item: AppResult) -> str:
    return f"{STATUS_ICON[item.status]} {STATUS_LABEL[item.status]}"


def render_text(rep: ScanReport) -> str:
    """Laporan plain-text ringkas (CLI)."""
    t = rep.totals
    lines = [f"🧩 Manifest check: namespace {rep.namespace} · {rep.gitops_repo}@{rep.branch}:{rep.folder}/",
             f"   secret: {rep.secret_source} · rule {rep.rules_count} ({rep.rules_source}) · "
             f"sample {rep.sample_source}"]
    lines += [f"   • {d}" for d in rep.details]
    lines += ["", f"Hasil: ✅ {t['ok']} OK · ❌ {t['fail']} FAIL · ⏭️  {t['skip']} SKIP (total {t['total']})"]
    if rep.rule_counts:
        lines += ["", "Temuan per rule:"]
        lines += [f"  {r['count']:4d}  {r['rule_name']} [{r['severity']}]" for r in rep.rule_counts]
    groups = [
        ("❌ FAIL", [i for i in rep.items if i.status == "fail"],
         lambda i: ", ".join(i.critical) + (f"  (+ saran: {', '.join(i.advisory)})" if i.advisory else "")),
        ("⏭️  SKIP", [i for i in rep.items if i.status == "skip"],
         lambda i: i.error or f"secret belum bisa dipastikan: {', '.join(i.unknown_secrets)}"),
        ("✅ OK dengan saran standar", [i for i in rep.items if i.status == "ok" and i.advisory],
         lambda i: ", ".join(i.advisory)),
    ]
    for title, items, describe in groups:
        if items:
            lines += ["", f"{title}:"] + [f"  - {i.app}: {describe(i)}" for i in items]
    return "\n".join(lines)


def _md_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(rep: ScanReport) -> str:
    """Laporan Markdown lengkap: ringkasan, temuan per rule, tabel, detail + saran YAML."""
    t = rep.totals
    md = [f"# Laporan Manifest — namespace `{rep.namespace}`", "",
          "| | |", "|---|---|",
          f"| Repo GitOps | `{rep.gitops_repo}@{rep.branch}:{rep.folder}/` |",
          f"| Dibuat | {rep.generated_at} · bq {rep.version} |",
          f"| Secret | {_md_cell(rep.secret_source)} |",
          f"| Rule | {rep.rules_count} dari `build_q_anomaly_rules` ({rep.rules_source}) |",
          f"| Sample YAML | `{rep.sample_source}` |", ""]
    if rep.details:
        md += ["**Catatan analisa**", ""] + [f"- {_md_cell(d)}" for d in rep.details] + [""]
    md += ["## Ringkasan", "", "| Status | Jumlah |", "|---|---|",
           f"| ✅ OK | {t['ok']} |", f"| ❌ FAIL | {t['fail']} |", f"| ⏭️ SKIP | {t['skip']} |",
           f"| **Total** | **{t['total']}** |", ""]
    if rep.rule_counts:
        md += ["### Temuan per rule", "", "| Rule | Severity | Jumlah |", "|---|---|---|"]
        md += [f"| `{r['rule_name']}` | {r['severity']} | {r['count']} |" for r in rep.rule_counts] + [""]
    md += ["## Daftar Deployment", "", "| # | Deployment | Status | Critical | Saran (warn) |",
           "|---|---|---|---|---|"]
    for n, i in enumerate(rep.items, 1):
        md.append(f"| {n} | `{i.app}` | {_status(i)} | {', '.join(i.critical) or '—'} | "
                  f"{', '.join(i.advisory) or '—'} |")
    detailed = [i for i in rep.items if i.findings or i.unknown_secrets or i.error or i.notes]
    if detailed:
        md += ["", "## Detail"]
    for i in detailed:
        md += ["", f"### {_status(i)} `{i.app}`", "", f"- Deployment: `{i.deploy_path}`"]
        if i.service_path:
            md.append(f"- Service: `{i.service_path}`")
        if i.error:
            md.append(f"- ⚠️ error: {_md_cell(i.error)}")
        for f in i.findings:
            md.append(f"- **[{f['severity']}] {f['rule_name']}** — {_md_cell(f['message'])}  \n"
                      f"  💡 {_md_cell(f['fix_hint'])}")
        md += [f"- secret `{n}`: {s}" for n, s in i.secrets.items()]
        md += [f"- {_md_cell(n)}" for n in i.notes]
        for sug in i.suggestions:
            md += ["", f"<details><summary>📝 Saran YAML — {sug['title']} (<code>{sug['path']}</code>)</summary>",
                   "", "```yaml", sug["yaml"].rstrip(), "```", "", "</details>"]
    md += ["", "---", "", "_Read-only: isi Secret tidak pernah dibaca. Saran YAML tidak diterapkan otomatis — "
           "ajukan lewat PR ke repo GitOps. Kerangka Secret jangan di-commit plaintext._", ""]
    return "\n".join(md)


def render_json(rep: ScanReport) -> str:
    return json.dumps(rep.to_dict(), indent=2, ensure_ascii=False) + "\n"


def default_report_path(rep: ScanReport, ext: str, directory: Optional[Path] = None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return (directory or Path.cwd()) / f"manifest-{rep.namespace}-{stamp}.{ext}"


def export(rep: ScanReport, *, md_path: Optional[str] = None, json_path: Optional[str] = None) -> List[Path]:
    """Tulis laporan Markdown dan/atau JSON; return path yang ditulis."""
    written = []
    for target, render in ((md_path, render_markdown), (json_path, render_json)):
        if not target:
            continue
        path = Path(target).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(rep), encoding="utf-8")
        written.append(path)
    return written
