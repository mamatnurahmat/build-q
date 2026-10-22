"""`bq --manifest-template <namespace,...>` — bangun template standar Deployment + Service per
namespace × jenis aplikasi dari manifest GitOps yang LOLOS standar, untuk PocketBase
`build_q_manifest_templates` (dipakai `--manifest-init` / saran Manifest sebelum sample tetangga).

Kriteria manifest yang boleh jadi template (semua harus terpenuhi):
  - label pod = label standar (app, env = prefix namespace, project = akhiran namespace, role)
  - replicas standar (production 2, lainnya 1)
  - Secret config bernama standar (`file-config-<app>` / `-<env>`) dengan tepat satu mount
  - punya Service, terdaftar di kustomization, tanpa temuan critical tahap Manifest

Seed ditulis ke file lokal (default `~/.build-q/manifest-templates-seed.json`, di luar repo karena
berisi struktur infra internal) lalu di-sync: `bq --tui-sync <file>`. Read-only terhadap GitOps.
"""
from __future__ import annotations

import collections
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import CONFIG_DIR

DEFAULT_SEED = CONFIG_DIR / "manifest-templates-seed.json"


def _standard_reason(rules: dict, deploy: dict, app: str, ns: str) -> str:
    """'' bila manifest memenuhi standar template; selain itu alasan penolakan."""
    from . import manifest_check as mc

    labels = mc._pod_labels(deploy)
    bad = {k: labels.get(k) for k, v in mc.standard_labels(app, ns).items() if labels.get(k) != v}
    if bad:
        return f"label tidak standar {bad}"
    env = mc.env_from_namespace(ns)
    if (deploy.get("spec") or {}).get("replicas", 1) != (2 if env == "production" else 1):
        return f"replicas {(deploy.get('spec') or {}).get('replicas')}"
    prefix = rules["missing_file_config_volume"]["params"]["secret_prefix"]
    fc = {v.get("name"): v["secret"].get("secretName") for v in mc._pod(deploy).get("volumes") or []
          if str((v.get("secret") or {}).get("secretName", "")).startswith(prefix)}
    if any(n not in (f"{prefix}{app}", f"{prefix}{app}-{env}") for n in fc.values()):
        return f"nama Secret config tidak standar {sorted(fc.values())}"
    mounts = [m for m in mc._containers(deploy)[0].get("volumeMounts") or [] if m.get("name") in fc]
    if len(mounts) > 1:
        return "mount config ganda"
    return ""


def build(namespace: str, *, branch: str = "main", infra: str = "cce",
          config: Optional[dict] = None) -> Tuple[List[dict], List[dict]]:
    """Return (records template, laporan per jenis aplikasi)."""
    from . import e2e, manifest_check as mc
    from .config import load_config

    config = config or load_config()
    repo, folder = config["gitops"]["repo"], f"{infra}/{namespace}"
    rules, _ = mc.load_rules()
    suffixes = rules["service_missing"]["params"]["suffixes"]
    files = e2e.gh_list_dir(repo, folder, branch)
    kz, _ = e2e._fetch_kustomization(repo, folder, branch)
    ns = (kz or {}).get("namespace") or namespace
    apps = sorted(f.removesuffix("_deployment.yaml") for f in files if f.endswith("_deployment.yaml"))
    by_kind: Dict[str, List[str]] = collections.defaultdict(list)
    for app in apps:
        by_kind[mc.app_kind(app)].append(app)

    def port_style(deploy: dict, service: dict) -> str:
        """Konvensi port Service: `fixed:<port>` (port klien tetap) atau `same` (= containerPort)."""
        ports = (service.get("spec") or {}).get("ports") or []
        named = {p.get("name"): p.get("containerPort") for p in mc._container_ports(deploy) if p.get("name")}
        if len(ports) == 1:
            tp = ports[0].get("targetPort", ports[0].get("port"))
            target = named.get(tp, tp) if isinstance(tp, str) and not str(tp).isdigit() else tp
            if str(ports[0].get("port")) != str(target):
                return f"fixed:{ports[0].get('port')}"
        return "same"

    records, report = [], []
    for kind, candidates in sorted(by_kind.items()):
        rejected, fetched = [], {}
        for app in candidates:
            path = f"{folder}/{app}_deployment.yaml"
            try:
                deploy = mc.find_kind(mc.parse_yaml(e2e.gh_raw(repo, path, branch)), "Deployment")
                service, spath = e2e._fetch_service(repo, path, branch, suffixes)
            except Exception as e:
                rejected.append(f"{app}: gagal dibaca ({str(e)[:60]})")
                continue
            fetched[app] = (deploy, service, spath)
        # Konvensi port Service mayoritas di jenis ini → kandidat sekonvensi didahulukan.
        styles = collections.Counter(port_style(d, s) for d, s, _ in fetched.values() if d and s)
        majority = styles.most_common(1)[0][0] if styles else "same"
        ordered = sorted(fetched, key=lambda a: (fetched[a][0] is None or fetched[a][1] is None or
                                                 port_style(fetched[a][0], fetched[a][1]) != majority, a))
        for app in ordered:
            path = f"{folder}/{app}_deployment.yaml"
            deploy, service, spath = fetched[app]
            if deploy is None or service is None:
                rejected.append(f"{app}: Deployment/Service tidak lengkap")
                continue
            reason = _standard_reason(rules, deploy, app, ns)
            if not reason:
                res = mc.check(rules, deploy=deploy, service=service, app=app, ns=ns,
                               deploy_path=path, service_path=spath, kustomization=kz)
                crit = [f.rule_name for f in res.findings if f.severity == "critical"]
                reason = f"temuan critical {crit}" if crit else ""
            if reason:
                rejected.append(f"{app}: {reason}")
                continue
            # Template = manifest efektif TANPA patch kustomization (patch tetap berlaku saat apply).
            records.append(mc.make_template(deploy, service, app=app, infra=infra, namespace=namespace,
                                            source=f"{branch}:{path}"))
            report.append({"kind": kind, "template": records[-1]["name"], "source_app": app,
                           "candidates": len(candidates), "rejected": rejected})
            break
        else:
            report.append({"kind": kind, "template": None, "source_app": None,
                           "candidates": len(candidates), "rejected": rejected})
    return records, report


def write_seed(records: List[dict], path: Optional[str] = None) -> Path:
    target = Path(path).expanduser() if path else DEFAULT_SEED
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"build_q_manifest_templates": records}, indent=2, ensure_ascii=False) + "\n")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def render_report(namespace: str, report: List[dict]) -> str:
    lines = [f"🧩 Template standar {namespace}:"]
    for r in report:
        if r["template"]:
            lines.append(f"  ✅ {r['kind']:<12} → {r['template']} (dari {r['source_app']}; "
                         f"{len(r['rejected'])}/{r['candidates']} kandidat ditolak)")
        else:
            lines.append(f"  ⚠️  {r['kind']:<12} → tidak ada kandidat standar ({r['candidates']} kandidat)")
            lines += [f"       - {x}" for x in r["rejected"][:3]]
    return "\n".join(lines)
