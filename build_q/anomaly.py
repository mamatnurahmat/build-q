"""`bq --anomaly-scan` — MVP anomaly scanner untuk manifest K8s.

Pipeline:
    1. Parse file YAML (multi-doc) via PyYAML (lazy import).
    2. Jalankan 10 deterministic checks per resource (Deployment, Secret).
    3. Optional: panggil Jev (System One) satu kali untuk overall severity
       + prod_ready verdict berdasarkan ringkasan findings.
    4. Render laporan Rich + exit code (0=safe, 1=warn, 2=critical).

Design keputusan:
    - Deterministic rules ditulis di Python (fast, akurat, zero LLM cost).
      Hanya aspek SUBJECTIVE (overall severity, prioritas fix) dilempar
      ke Jev — model lebih bijak untuk sintesis daripada regex.
    - Scanner single-file dulu (MVP). Multi-file / repo-walk → iterasi berikut.
    - PyYAML sebagai lazy import — fail dengan pesan jelas bila belum install.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ─── Severity ordering ──────────────────────────────────────────────────────
_SEV_RANK = {"info": 0, "warn": 1, "critical": 2}
_EXIT_FROM_SEV = {"safe": 0, "info": 0, "warn": 1, "critical": 2}


@dataclass
class Finding:
    rule_name: str
    severity: str     # critical | warn | info
    category: str     # availability | security | compliance | ...
    message: str      # one-line human-readable
    fix_hint: str     # bq tool atau skill untuk perbaiki


@dataclass
class ScanResult:
    file_path: Path
    resource_kind: str
    resource_name: str
    namespace: Optional[str]
    doc_index: int = 0
    findings: list[Finding] = field(default_factory=list)
    jev_verdict: Optional[dict] = None  # {severity, prod_ready, usage}

    @property
    def worst_severity(self) -> str:
        if not self.findings:
            return "safe"
        return max(self.findings, key=lambda f: _SEV_RANK[f.severity]).severity


# ─── Helpers ────────────────────────────────────────────────────────────────

def _is_production(ns: Optional[str]) -> bool:
    return bool(ns and ns.startswith("production-"))


def _containers(doc: dict) -> list[dict]:
    return (
        doc.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
        or []
    )


def _pod_spec(doc: dict) -> dict:
    return doc.get("spec", {}).get("template", {}).get("spec", {}) or {}


# ─── Deterministic checks: Deployment ──────────────────────────────────────

def check_prod_replicas(doc: dict, ns: str) -> list[Finding]:
    if not _is_production(ns):
        return []
    replicas = doc.get("spec", {}).get("replicas", 1)
    if replicas is not None and replicas < 2:
        return [Finding(
            rule_name="prod_replicas_lt_2",
            severity="critical",
            category="availability",
            message=f"Production deployment dengan replicas={replicas} (<2) — risiko downtime saat pod evicted.",
            fix_hint="Set spec.replicas=2 atau lebih. `bq --bootstrap-k8s ... --replicas 2`.",
        )]
    return []


def check_missing_pullsecret(doc: dict, ns: str) -> list[Finding]:
    pod = _pod_spec(doc)
    secrets = pod.get("imagePullSecrets", []) or []
    names = {s.get("name") for s in secrets if isinstance(s, dict)}
    if "regcred" not in names:
        return [Finding(
            rule_name="missing_image_pullsecret",
            severity="critical",
            category="availability",
            message="imagePullSecrets tidak menyertakan `regcred` — image private akan ImagePullBackOff.",
            fix_hint="Tambah imagePullSecrets: [{name: regcred}] di pod spec.",
        )]
    return []


def check_image_latest_tag(doc: dict, ns: str) -> list[Finding]:
    out = []
    for c in _containers(doc):
        img = c.get("image", "") or ""
        tag = img.rsplit(":", 1)[1] if ":" in img.rsplit("/", 1)[-1] else ""
        if not tag or tag == "latest":
            out.append(Finding(
                rule_name="image_latest_tag",
                severity="critical",
                category="config_drift",
                message=f"Container `{c.get('name', '?')}` pakai image `{img}` tanpa tag spesifik — rollback impossible.",
                fix_hint="Pin ke tag versi (v1.2.3). `bq --gitops-set-image` untuk update.",
            ))
    return out


def check_missing_liveness(doc: dict, ns: str) -> list[Finding]:
    out = []
    for c in _containers(doc):
        if not c.get("livenessProbe"):
            out.append(Finding(
                rule_name="missing_liveness_probe",
                severity="warn",
                category="availability",
                message=f"Container `{c.get('name', '?')}` tanpa livenessProbe — crashloop silent, kubelet tidak auto-restart saat deadlock.",
                fix_hint="Tambah livenessProbe (httpGet /health:<PORT> atau exec).",
            ))
    return out


def check_missing_readiness(doc: dict, ns: str) -> list[Finding]:
    out = []
    for c in _containers(doc):
        if not c.get("readinessProbe"):
            out.append(Finding(
                rule_name="missing_readiness_probe",
                severity="warn",
                category="availability",
                message=f"Container `{c.get('name', '?')}` tanpa readinessProbe — Service forward traffic ke pod belum siap → 502/503.",
                fix_hint="Tambah readinessProbe. Mandatory untuk zero-downtime rolling update.",
            ))
    return out


def check_missing_resources_limits(doc: dict, ns: str) -> list[Finding]:
    out = []
    for c in _containers(doc):
        limits = (c.get("resources", {}) or {}).get("limits", {}) or {}
        if not limits.get("cpu") and not limits.get("memory"):
            out.append(Finding(
                rule_name="missing_resources_limits",
                severity="warn",
                category="performance",
                message=f"Container `{c.get('name', '?')}` tanpa resources.limits — risiko noisy neighbor + OOMKill tak terprediksi.",
                fix_hint="Set resources.requests + resources.limits (baseline: 100m/128Mi req, 500m/512Mi limit).",
            ))
    return out


_CRED_PATTERN = re.compile(
    r"(?:password|passwd|token|api[_-]?key|secret|credential)",
    re.IGNORECASE,
)


def check_env_hardcoded_credential(doc: dict, ns: str) -> list[Finding]:
    out = []
    for c in _containers(doc):
        for env in c.get("env", []) or []:
            name = env.get("name", "")
            value = env.get("value")
            if value is None:
                continue  # pakai valueFrom.secretKeyRef — safe
            if _CRED_PATTERN.search(name) and len(str(value)) > 4:
                out.append(Finding(
                    rule_name="env_var_hardcoded_credential",
                    severity="critical",
                    category="security",
                    message=f"Container `{c.get('name', '?')}` env `{name}` berisi value plaintext — looks like credential.",
                    fix_hint="Pindah ke Secret + valueFrom.secretKeyRef. Rotate kredensial yang bocor.",
                ))
    return out


def check_privileged(doc: dict, ns: str) -> list[Finding]:
    out = []
    for c in _containers(doc):
        sec = c.get("securityContext", {}) or {}
        if sec.get("privileged") is True:
            out.append(Finding(
                rule_name="privileged_container",
                severity="critical",
                category="security",
                message=f"Container `{c.get('name', '?')}` run privileged — akses full host, security blast radius luas.",
                fix_hint="Hapus securityContext.privileged. Pakai capabilities granular kalau butuh.",
            ))
    return out


def check_host_network(doc: dict, ns: str) -> list[Finding]:
    pod = _pod_spec(doc)
    if pod.get("hostNetwork") is True:
        return [Finding(
            rule_name="host_network_true",
            severity="critical",
            category="security",
            message="hostNetwork: true — pod share network namespace host, bypass Service abstraction.",
            fix_hint="Hapus hostNetwork kecuali DaemonSet infrastruktur (ingress/CNI).",
        )]
    return []


# ─── Deterministic checks: Secret ──────────────────────────────────────────

def check_secret_plaintext(doc: dict, ns: str) -> list[Finding]:
    # SOPS-encrypted: ada field top-level `sops` dengan metadata enkripsi.
    if "sops" in doc:
        return []
    # Sealed Secrets: kind=SealedSecret (bukan Secret)
    if doc.get("kind") == "SealedSecret":
        return []
    data = doc.get("data") or doc.get("stringData") or {}
    if data:
        return [Finding(
            rule_name="secret_plaintext_committed",
            severity="critical",
            category="security",
            message="Secret bukan SOPS/SealedSecret — plaintext/base64 ter-commit ke repo = kredensial exposed.",
            fix_hint="Encrypt dgn `bq --sops-encrypt <file>` (standar v0.1.39). Rotate kredensial yang bocor.",
        )]
    return []


CHECKS_DEPLOYMENT = [
    check_prod_replicas,
    check_missing_pullsecret,
    check_image_latest_tag,
    check_missing_liveness,
    check_missing_readiness,
    check_missing_resources_limits,
    check_env_hardcoded_credential,
    check_privileged,
    check_host_network,
]

CHECKS_SECRET = [check_secret_plaintext]


# ─── Scanner ────────────────────────────────────────────────────────────────

def _scan_doc(doc: dict, file_path: Path, idx: int) -> Optional[ScanResult]:
    if not isinstance(doc, dict):
        return None
    kind = doc.get("kind")
    if not kind:
        return None
    meta = doc.get("metadata", {}) or {}
    result = ScanResult(
        file_path=file_path,
        resource_kind=kind,
        resource_name=meta.get("name", "<noname>"),
        namespace=meta.get("namespace"),
        doc_index=idx,
    )
    if kind == "Deployment":
        for check in CHECKS_DEPLOYMENT:
            result.findings.extend(check(doc, result.namespace or ""))
    elif kind == "Secret":
        for check in CHECKS_SECRET:
            result.findings.extend(check(doc, result.namespace or ""))
    return result


def _ask_jev(result: ScanResult) -> Optional[dict]:
    """Satu Jev call untuk overall severity + prod_ready. Fail-safe."""
    try:
        from .tui import load_env, resolve_provider, jev_decide
    except ImportError:
        return None
    try:
        load_env()
        provider = resolve_provider()
    except Exception as e:
        console.print(f"[yellow]⚠️  Jev skip: {e}[/yellow]")
        return None

    state = {
        "resource_kind": result.resource_kind,
        "resource_name": result.resource_name,
        "namespace": result.namespace or "",
        "findings": [
            {"rule": f.rule_name, "severity": f.severity,
             "category": f.category, "message": f.message}
            for f in result.findings
        ],
    }
    questions = {
        "overall_severity": {
            "type": "choice",
            "instructions": "Berikan severity gabungan untuk resource K8s ini berdasarkan semua findings deterministic yang terdeteksi.",
            "criteria": {
                "safe":     "Tidak ada issue atau hanya info-level — aman deploy.",
                "warn":     "Ada issue non-blocker, perlu dibenahi dalam siklus berikutnya tapi tidak urgent.",
                "critical": "Ada issue blocker (security, availability, outage risk) — tidak boleh deploy ke production.",
            },
        },
        "prod_ready": {
            "type": "noul",
            "instructions": "Apakah resource ini AMAN di-deploy ke production?",
            "criteria": {
                "true":  "Aman di-deploy ke production, memenuhi standar Qoin.",
                "false": "Tidak aman atau tidak lengkap untuk production.",
            },
        },
    }
    try:
        return jev_decide(state, questions, provider)
    except Exception as e:
        console.print(f"[yellow]⚠️  Jev call gagal: {e}[/yellow]")
        return None


# ─── Rendering ──────────────────────────────────────────────────────────────

_SEV_STYLE = {
    "critical": "red",
    "warn":     "yellow",
    "info":     "cyan",
    "safe":     "green",
}


def _render_result(result: ScanResult) -> None:
    ns = result.namespace or "[dim]<cluster-scoped>[/dim]"
    header = (
        f"[bold]{result.resource_kind}[/bold] "
        f"[cyan]{result.resource_name}[/cyan] "
        f"ns={ns} "
        f"[dim](doc #{result.doc_index})[/dim]"
    )
    console.print(f"\n── {header} ──")

    if not result.findings:
        console.print("[green]✓ Tidak ada anomali deterministic terdeteksi.[/green]")
    else:
        tbl = Table(show_header=True, header_style="bold magenta", expand=True)
        tbl.add_column("Sev", width=10)
        tbl.add_column("Rule", width=32)
        tbl.add_column("Pesan")
        for f in sorted(result.findings, key=lambda x: -_SEV_RANK[x.severity]):
            style = _SEV_STYLE[f.severity]
            tbl.add_row(
                f"[{style}]{f.severity}[/{style}]",
                f.rule_name,
                f.message,
            )
        console.print(tbl)
        # Fix hints
        console.print("[bold]Fix hints:[/bold]")
        for f in result.findings:
            console.print(f"  • [cyan]{f.rule_name}[/cyan] → {f.fix_hint}")

    # Jev verdict
    if result.jev_verdict:
        answers = result.jev_verdict.get("answers", {})
        sev_ans = answers.get("overall_severity", {})
        prod_ans = answers.get("prod_ready", {})
        sev_choice = sev_ans.get("choice", "?")
        sev_conf = sev_ans.get("confidence", 0.0)
        prod_noul = float(prod_ans.get("noul", 0.0))
        style = _SEV_STYLE.get(sev_choice, "white")
        console.print(
            f"\n[bold]Jev verdict[/bold]: "
            f"severity=[{style}]{sev_choice}[/{style}] "
            f"(confidence {sev_conf:.0%}) · "
            f"prod_ready={prod_noul:.0%}"
        )
        usage = result.jev_verdict.get("usage") or {}
        if usage:
            parts = []
            if "input_tokens" in usage:  parts.append(f"in {usage['input_tokens']}")
            if "output_tokens" in usage: parts.append(f"out {usage['output_tokens']}")
            if "cost" in usage:          parts.append(f"${usage['cost']:.6f}")
            if parts:
                console.print(f"[dim]tokens: {' · '.join(parts)}[/dim]")


# ─── Scan one file (returns list of ScanResult, no rendering) ──────────────

def _scan_file(path: Path) -> list[ScanResult]:
    """Parse YAML (multi-doc) → list of ScanResult. Raises on parse error."""
    import yaml  # caller must ensure installed
    try:
        docs = list(yaml.safe_load_all(path.read_text()))
    except yaml.YAMLError as e:
        console.print(f"[red]❌ YAML parse error {path}: {e}[/red]")
        return []

    results: list[ScanResult] = []
    for idx, doc in enumerate(docs):
        r = _scan_doc(doc, path, idx)
        if r:
            results.append(r)
    return results


# ─── Export: CSV ───────────────────────────────────────────────────────────

def export_csv(results: list[ScanResult], out_path: Path) -> None:
    """Flatten findings → 1 row per finding. Resource tanpa finding: 1 row w/ empty rule."""
    import csv
    out_path = Path(out_path).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "file", "resource_kind", "resource_name", "namespace",
            "severity", "rule_name", "category", "message", "fix_hint",
            "jev_overall_severity", "jev_confidence", "jev_prod_ready",
        ])
        for r in results:
            jev = r.jev_verdict or {}
            answers = jev.get("answers", {}) if jev else {}
            sev_ans = answers.get("overall_severity", {}) or {}
            prod_ans = answers.get("prod_ready", {}) or {}
            jev_sev = sev_ans.get("choice", "")
            jev_conf = f"{sev_ans.get('confidence', 0.0):.4f}" if sev_ans else ""
            jev_prod = f"{float(prod_ans.get('noul', 0.0)):.4f}" if prod_ans else ""
            if not r.findings:
                w.writerow([
                    str(r.file_path), r.resource_kind, r.resource_name,
                    r.namespace or "", "safe", "", "", "", "",
                    jev_sev, jev_conf, jev_prod,
                ])
                continue
            for finding in r.findings:
                w.writerow([
                    str(r.file_path), r.resource_kind, r.resource_name,
                    r.namespace or "", finding.severity, finding.rule_name,
                    finding.category, finding.message, finding.fix_hint,
                    jev_sev, jev_conf, jev_prod,
                ])


# ─── Export: Markdown ──────────────────────────────────────────────────────

def _severity_badge(sev: str) -> str:
    return {"critical": "🔴 critical", "warn": "🟡 warn",
            "info": "🔵 info", "safe": "🟢 safe"}.get(sev, sev)


def export_markdown(
    results: list[ScanResult], out_path: Path, *,
    scan_root: Optional[Path] = None, use_jev: bool = True,
) -> None:
    """Group by file → table of findings + Jev verdict. Includes aggregate summary."""
    from collections import Counter
    import datetime

    out_path = Path(out_path).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total_resources = len(results)
    total_findings = sum(len(r.findings) for r in results)
    sev_count = Counter(f.severity for r in results for f in r.findings)
    rule_count = Counter(f.rule_name for r in results for f in r.findings)
    cat_count = Counter(f.category for r in results for f in r.findings)
    worst = max(
        (r.worst_severity for r in results),
        key=lambda s: _SEV_RANK.get(s, -1), default="safe",
    )

    lines = []
    lines.append(f"# Anomaly Scan Report")
    lines.append("")
    lines.append(f"- **Scanned**: `{scan_root or '(file)'}`")
    lines.append(f"- **Timestamp**: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- **Resources**: {total_resources}")
    lines.append(f"- **Findings**: {total_findings}")
    lines.append(f"- **Jev**: {'on' if use_jev else 'off'}")
    lines.append(f"- **Overall**: {_severity_badge(worst).upper()}")
    lines.append("")

    # Summary tables
    lines.append("## Ringkasan Severity")
    lines.append("")
    lines.append("| Severity | Count |")
    lines.append("|---|---:|")
    for sev in ("critical", "warn", "info"):
        lines.append(f"| {_severity_badge(sev)} | {sev_count.get(sev, 0)} |")
    lines.append("")

    if rule_count:
        lines.append("## Top Rules (paling sering muncul)")
        lines.append("")
        lines.append("| Rule | Count |")
        lines.append("|---|---:|")
        for rule, cnt in rule_count.most_common(10):
            lines.append(f"| `{rule}` | {cnt} |")
        lines.append("")

    if cat_count:
        lines.append("## Per Kategori")
        lines.append("")
        lines.append("| Kategori | Count |")
        lines.append("|---|---:|")
        for cat, cnt in cat_count.most_common():
            lines.append(f"| {cat} | {cnt} |")
        lines.append("")

    # Per-file details
    lines.append("## Detail Per Resource")
    lines.append("")
    for r in sorted(
        results,
        key=lambda x: (-_SEV_RANK.get(x.worst_severity, -1), str(x.file_path)),
    ):
        rel = r.file_path.name
        jev = r.jev_verdict or {}
        answers = jev.get("answers", {}) if jev else {}
        sev_ans = answers.get("overall_severity", {}) or {}
        prod_ans = answers.get("prod_ready", {}) or {}
        jev_bits = ""
        if sev_ans:
            jev_bits = (
                f" · Jev=`{sev_ans.get('choice', '?')}` "
                f"({sev_ans.get('confidence', 0.0):.0%}) "
                f"prod_ready={float(prod_ans.get('noul', 0.0)):.0%}"
            )
        header = (
            f"### `{rel}` → **{r.resource_kind}** `{r.resource_name}` "
            f"(ns=`{r.namespace or '-'}`)"
        )
        lines.append(header)
        lines.append("")
        lines.append(f"Worst: {_severity_badge(r.worst_severity)}{jev_bits}")
        lines.append("")
        if not r.findings:
            lines.append("_✓ Tidak ada anomali deterministic terdeteksi._")
            lines.append("")
            continue
        lines.append("| Sev | Rule | Pesan | Fix hint |")
        lines.append("|---|---|---|---|")
        for f in sorted(r.findings, key=lambda x: -_SEV_RANK[x.severity]):
            msg = f.message.replace("|", "\\|")
            fix = f.fix_hint.replace("|", "\\|")
            lines.append(
                f"| {_severity_badge(f.severity)} | `{f.rule_name}` | {msg} | {fix} |"
            )
        lines.append("")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─── Public entry ──────────────────────────────────────────────────────────

def run_anomaly_scan(
    target_path: str,
    *,
    use_jev: bool = True,
    glob_pattern: str = "*_deployment.yaml",
    export_csv_path: Optional[str] = None,
    export_md_path: Optional[str] = None,
) -> int:
    """Scan 1 file ATAU seluruh direktori (glob *_deployment.yaml default).

    Return exit code: 0=safe, 1=warn, 2=critical, 2=hard-error.
    """
    try:
        import yaml  # noqa: F401 — lazy, hanya validasi install
    except ImportError:
        pipx_hint = ""
        bq_bin = Path.home() / ".local" / "pipx" / "venvs" / "build-q"
        if bq_bin.exists():
            pipx_hint = (
                "   Terdeteksi install via pipx — pakai: "
                "`pipx inject build-q pyyaml`\n"
            )
        print(
            "❌ Modul `pyyaml` tidak terinstall.\n"
            + pipx_hint +
            "   Alternatif: `pip install pyyaml` (sesuaikan dgn interpreter bq).\n"
            "   (PyYAML adalah dependency opsional untuk `--anomaly-scan`).",
            file=sys.stderr,
        )
        return 2

    path = Path(target_path).expanduser()
    if not path.exists():
        print(f"❌ Path tidak ada: {path}", file=sys.stderr)
        return 2

    # ── Collect files ─────────────────────────────────────────────────────
    if path.is_dir():
        files = sorted(path.glob(glob_pattern))
        scan_root = path
        if not files:
            console.print(
                f"[yellow]⚠️  Tidak ada file match `{glob_pattern}` di {path}[/yellow]"
            )
            return 0
        console.print(
            f"[dim]🔍 Found {len(files)} file(s) match `{glob_pattern}` di {path}[/dim]"
        )
    else:
        files = [path]
        scan_root = None

    # ── Scan all ──────────────────────────────────────────────────────────
    all_results: list[ScanResult] = []
    for f in files:
        all_results.extend(_scan_file(f))

    if not all_results:
        console.print(f"[yellow]⚠️  Tidak ada resource K8s valid di scan ini[/yellow]")
        return 0

    # ── Jev (hanya untuk yg punya findings) ──────────────────────────────
    if use_jev:
        to_ask = [r for r in all_results if r.findings]
        if to_ask:
            console.print(
                f"[dim]🤖 Memanggil Jev untuk {len(to_ask)} resource dgn findings...[/dim]"
            )
        for r in to_ask:
            r.jev_verdict = _ask_jev(r)

    # ── Render ────────────────────────────────────────────────────────────
    total_findings = sum(len(r.findings) for r in all_results)
    header_subtitle = (
        f"{len(files)} file · {len(all_results)} resource · "
        f"{total_findings} findings · Jev: {'on' if use_jev else 'off'}"
    )
    console.print(Panel.fit(
        f"[bold green]bq --anomaly-scan[/bold green]  "
        f"[dim]{path}[/dim]\n{header_subtitle}",
        border_style="green",
    ))

    # Dalam mode direktori banyak file: ringkas (hanya resource yg punya findings)
    is_dir_mode = path.is_dir()
    results_to_render = (
        [r for r in all_results if r.findings] if is_dir_mode else all_results
    )
    for r in results_to_render:
        _render_result(r)

    # ── Aggregate summary ────────────────────────────────────────────────
    if is_dir_mode:
        from collections import Counter
        sev_count = Counter(f.severity for r in all_results for f in r.findings)
        rule_count = Counter(f.rule_name for r in all_results for f in r.findings)
        tbl = Table(title="\n📊 Ringkasan Agregat", show_header=True,
                    header_style="bold magenta")
        tbl.add_column("Severity")
        tbl.add_column("Jumlah", justify="right")
        for sev in ("critical", "warn", "info"):
            style = _SEV_STYLE[sev]
            tbl.add_row(f"[{style}]{sev}[/{style}]", str(sev_count.get(sev, 0)))
        console.print(tbl)

        if rule_count:
            tbl2 = Table(title="Top 5 Rules",
                         show_header=True, header_style="bold magenta")
            tbl2.add_column("Rule")
            tbl2.add_column("Count", justify="right")
            for rule, cnt in rule_count.most_common(5):
                tbl2.add_row(rule, str(cnt))
            console.print(tbl2)

    worst_sev = max(
        (r.worst_severity for r in all_results),
        key=lambda s: _SEV_RANK.get(s, -1),
        default="safe",
    )
    console.print(
        f"\n[bold]Overall:[/bold] "
        f"[{_SEV_STYLE[worst_sev]}]{worst_sev.upper()}[/{_SEV_STYLE[worst_sev]}]"
    )

    # ── Export ────────────────────────────────────────────────────────────
    if export_csv_path:
        export_csv(all_results, Path(export_csv_path))
        console.print(f"[green]📄 CSV ditulis: {export_csv_path}[/green]")
    if export_md_path:
        export_markdown(
            all_results, Path(export_md_path),
            scan_root=scan_root or path, use_jev=use_jev,
        )
        console.print(f"[green]📄 Markdown ditulis: {export_md_path}[/green]")

    return _EXIT_FROM_SEV.get(worst_sev, 2)
