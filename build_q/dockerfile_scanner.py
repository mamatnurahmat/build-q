"""`bq --fix-dockerfile` v2 — Dockerfile scanner + Jev-approved auto-fix.

Pipeline:
    1. Parse Dockerfile content (regex + structural analysis).
    2. Jalankan 22 deterministic checks dari dockerfile_checks.py.
    3. Optional: panggil Jev (System One) untuk:
       - overall severity synthesis
       - fix priority ranking
       - approval: safe_to_autofix (noul probability)
    4. Apply auto-fix untuk rule yang auto_fixable + Jev-approved.
    5. Render laporan Rich + optional CSV/Markdown export.

Modes:
    - Local: scan file lokal, auto-fix langsung.
    - Remote: scan via GitHub API tanpa clone (read-only).
    - PR-fix: remote scan → clone → fix → commit → push → buka PR.
      Skip PR jika hanya warning/info (tidak ada error-level findings).

Design keputusan:
    - Deterministic rules ditulis di Python (fast, akurat, zero LLM cost).
      Jev hanya untuk aspek SUBJECTIVE (overall severity, fix priority,
      apakah aman auto-fix) — model lebih bijak untuk sintesis.
    - Collection extensible via KNOWN_ISSUES list di dockerfile_checks.py
      DAN via PocketBase collection `build_q_dockerfile_rules` (future).
    - Exit code: 0=clean, 1=warning, 2=error/critical.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .dockerfile_checks import KNOWN_ISSUES, analyze_dockerfile

console = Console()

_SEV_RANK = {"info": 0, "warn": 1, "warning": 1, "error": 2, "critical": 2}
_EXIT_FROM_SEV = {"clean": 0, "info": 0, "warn": 1, "warning": 1, "error": 2, "critical": 2}
_SEV_STYLE = {
    "error": "red",
    "critical": "red",
    "warn": "yellow",
    "warning": "yellow",
    "info": "cyan",
    "clean": "green",
}


@dataclass
class DockerfileFinding:
    rule_id: str
    severity: str
    category: str
    title: str
    reason: str
    fix_hint: str
    auto_fixable: bool = False


@dataclass
class ScanResult:
    file_path: Path
    findings: list[DockerfileFinding] = field(default_factory=list)
    jev_verdict: Optional[dict] = None
    fixes_applied: list[str] = field(default_factory=list)

    @property
    def worst_severity(self) -> str:
        if not self.findings:
            return "clean"
        return max(self.findings, key=lambda f: _SEV_RANK.get(f.severity, 0)).severity

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity in ("warn", "warning"))

    @property
    def info_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "info")


def _issues_to_findings(issues: list[dict]) -> list[DockerfileFinding]:
    return [
        DockerfileFinding(
            rule_id=i["id"],
            severity=i["severity"],
            category=i.get("category", "build"),
            title=i["title"],
            reason=i["reason"],
            fix_hint=i["fix"],
            auto_fixable=i.get("auto_fixable", False),
        )
        for i in issues
    ]


# ─── Jev Integration ──────────────────────────────────────────────────────

def _ask_jev(result: ScanResult) -> Optional[dict]:
    """Satu Jev call untuk severity synthesis + fix approval. Fail-safe."""
    try:
        from .tui import load_env, resolve_provider, jev_decide
    except ImportError:
        return None
    try:
        load_env()
        provider = resolve_provider()
    except Exception as e:
        console.print(f"[yellow]  Jev skip: {e}[/yellow]")
        return None

    state = {
        "file": str(result.file_path),
        "findings_count": len(result.findings),
        "error_count": result.error_count,
        "warning_count": result.warning_count,
        "findings": [
            {
                "rule": f.rule_id,
                "severity": f.severity,
                "category": f.category,
                "title": f.title,
                "auto_fixable": f.auto_fixable,
            }
            for f in result.findings
        ],
    }
    questions = {
        "overall_severity": {
            "type": "choice",
            "instructions": (
                "Berikan severity gabungan untuk Dockerfile ini "
                "berdasarkan semua findings yang terdeteksi."
            ),
            "criteria": {
                "clean": "Tidak ada issue — Dockerfile production-ready.",
                "warn": "Ada issue non-blocker, perlu dibenahi tapi build masih bisa jalan.",
                "critical": "Ada issue blocker — build akan gagal atau ada security risk serius.",
            },
        },
        "safe_to_autofix": {
            "type": "noul",
            "instructions": (
                "Apakah AMAN menjalankan auto-fix pada Dockerfile ini? "
                "Pertimbangkan: apakah auto-fix bisa merusak build logic, "
                "apakah ada custom pattern yang harus dipreserve."
            ),
            "criteria": {
                "true": "Aman auto-fix — perubahan regex-based tidak akan merusak logic.",
                "false": "Tidak aman — ada custom pattern atau logic yang bisa rusak.",
            },
        },
        "fix_priority": {
            "type": "choice",
            "instructions": (
                "Prioritas fix mana yang harus dikerjakan PERTAMA "
                "berdasarkan impact dan urgency?"
            ),
            "criteria": {
                "security": "Fix security issue dulu (credential leak, root user).",
                "build": "Fix build-breaking issue dulu (syntax error, missing dep).",
                "performance": "Fix performance issue dulu (image size, cache).",
                "compliance": "Fix compliance issue dulu (missing EXPOSE, HEALTHCHECK).",
            },
        },
    }
    try:
        return jev_decide(state, questions, provider)
    except Exception as e:
        console.print(f"[yellow]  Jev call gagal: {e}[/yellow]")
        return None


# ─── Rendering ────────────────────────────────────────────────────────────

def _severity_badge(sev: str) -> str:
    return {
        "error": "  error",
        "critical": "  critical",
        "warn": "  warn",
        "warning": "  warn",
        "info": "  info",
        "clean": "  clean",
    }.get(sev, sev)


def _render_result(result: ScanResult) -> None:
    header = f"[bold]Dockerfile[/bold] [cyan]{result.file_path}[/cyan]"
    console.print(f"\n-- {header} --")

    if not result.findings:
        console.print("[green]  Tidak ada issue terdeteksi — Dockerfile clean.[/green]")
    else:
        tbl = Table(show_header=True, header_style="bold magenta", expand=True)
        tbl.add_column("Sev", width=10)
        tbl.add_column("Rule", width=30)
        tbl.add_column("Pesan")
        tbl.add_column("Fix?", width=5)
        for f in sorted(result.findings, key=lambda x: -_SEV_RANK.get(x.severity, 0)):
            style = _SEV_STYLE.get(f.severity, "white")
            fix_mark = "  " if f.auto_fixable else ""
            tbl.add_row(
                f"[{style}]{f.severity}[/{style}]",
                f.rule_id,
                f.title,
                fix_mark,
            )
        console.print(tbl)

        console.print("[bold]Fix hints:[/bold]")
        for f in result.findings:
            console.print(f"  * [cyan]{f.rule_id}[/cyan]  {f.fix_hint}")

    if result.jev_verdict:
        answers = result.jev_verdict.get("answers", {})
        sev_ans = answers.get("overall_severity", {})
        safe_ans = answers.get("safe_to_autofix", {})
        prio_ans = answers.get("fix_priority", {})

        sev_choice = sev_ans.get("choice", "?")
        sev_conf = sev_ans.get("confidence", 0.0)
        safe_noul = float(safe_ans.get("noul", 0.0))
        prio_choice = prio_ans.get("choice", "?")

        style = _SEV_STYLE.get(sev_choice, "white")
        console.print(
            f"\n[bold]Jev verdict[/bold]: "
            f"severity=[{style}]{sev_choice}[/{style}] "
            f"(confidence {sev_conf:.0%}) * "
            f"safe_to_autofix={safe_noul:.0%} * "
            f"fix_priority={prio_choice}"
        )
        usage = result.jev_verdict.get("usage") or {}
        if usage:
            parts = []
            if "input_tokens" in usage:
                parts.append(f"in {usage['input_tokens']}")
            if "output_tokens" in usage:
                parts.append(f"out {usage['output_tokens']}")
            if "cost" in usage:
                parts.append(f"${usage['cost']:.6f}")
            if parts:
                console.print(f"[dim]tokens: {' * '.join(parts)}[/dim]")

    if result.fixes_applied:
        console.print(f"\n[bold green]  {len(result.fixes_applied)} fix(es) applied:[/bold green]")
        for fix_desc in result.fixes_applied:
            console.print(f"   * {fix_desc}")


# ─── Export: CSV ──────────────────────────────────────────────────────────

def export_csv(results: list[ScanResult], out_path: Path) -> None:
    import csv
    import datetime
    out_path = Path(out_path).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "file", "severity", "rule_id", "category", "title",
            "auto_fixable", "fix_hint",
            "jev_overall_severity", "jev_confidence",
            "jev_safe_to_autofix", "jev_fix_priority",
            "timestamp",
        ])
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in results:
            jev = r.jev_verdict or {}
            answers = jev.get("answers", {})
            sev_ans = answers.get("overall_severity", {})
            safe_ans = answers.get("safe_to_autofix", {})
            prio_ans = answers.get("fix_priority", {})
            jev_sev = sev_ans.get("choice", "")
            jev_conf = f"{sev_ans.get('confidence', 0.0):.4f}" if sev_ans else ""
            jev_safe = f"{float(safe_ans.get('noul', 0.0)):.4f}" if safe_ans else ""
            jev_prio = prio_ans.get("choice", "")

            if not r.findings:
                w.writerow([
                    str(r.file_path), "clean", "", "", "", "", "",
                    jev_sev, jev_conf, jev_safe, jev_prio, ts,
                ])
                continue
            for finding in r.findings:
                w.writerow([
                    str(r.file_path), finding.severity, finding.rule_id,
                    finding.category, finding.title,
                    str(finding.auto_fixable), finding.fix_hint,
                    jev_sev, jev_conf, jev_safe, jev_prio, ts,
                ])


# ─── Export: Markdown ─────────────────────────────────────────────────────

def export_markdown(results: list[ScanResult], out_path: Path, *, use_jev: bool = True) -> None:
    import datetime
    from collections import Counter

    out_path = Path(out_path).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total_findings = sum(len(r.findings) for r in results)
    sev_count = Counter(f.severity for r in results for f in r.findings)
    rule_count = Counter(f.rule_id for r in results for f in r.findings)
    cat_count = Counter(f.category for r in results for f in r.findings)
    worst = max(
        (r.worst_severity for r in results),
        key=lambda s: _SEV_RANK.get(s, -1), default="clean",
    )

    lines = []
    lines.append("# Dockerfile Scan Report")
    lines.append("")
    lines.append(f"- **Files scanned**: {len(results)}")
    lines.append(f"- **Timestamp**: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- **Total findings**: {total_findings}")
    lines.append(f"- **Jev**: {'on' if use_jev else 'off'}")
    lines.append(f"- **Overall**: {_severity_badge(worst).upper()}")
    lines.append("")

    lines.append("## Severity Summary")
    lines.append("")
    lines.append("| Severity | Count |")
    lines.append("|---|---:|")
    for sev in ("error", "warning", "info"):
        lines.append(f"| {_severity_badge(sev)} | {sev_count.get(sev, 0)} |")
    lines.append("")

    if cat_count:
        lines.append("## Per Category")
        lines.append("")
        lines.append("| Category | Count |")
        lines.append("|---|---:|")
        for cat, cnt in cat_count.most_common():
            lines.append(f"| {cat} | {cnt} |")
        lines.append("")

    if rule_count:
        lines.append("## Top Rules")
        lines.append("")
        lines.append("| Rule | Count | Auto-fixable |")
        lines.append("|---|---:|---|")
        by_id = {c["id"]: c for c in KNOWN_ISSUES}
        for rule, cnt in rule_count.most_common(10):
            fixable = "Yes" if by_id.get(rule, {}).get("auto_fixable") else "No"
            lines.append(f"| `{rule}` | {cnt} | {fixable} |")
        lines.append("")

    for r in results:
        lines.append(f"### `{r.file_path}`")
        lines.append("")
        jev = r.jev_verdict or {}
        answers = jev.get("answers", {})
        if answers:
            sev_ans = answers.get("overall_severity", {})
            safe_ans = answers.get("safe_to_autofix", {})
            prio_ans = answers.get("fix_priority", {})
            lines.append(
                f"Jev: severity=`{sev_ans.get('choice', '?')}` "
                f"({sev_ans.get('confidence', 0):.0%}) | "
                f"safe_to_autofix={float(safe_ans.get('noul', 0)):.0%} | "
                f"fix_priority={prio_ans.get('choice', '?')}"
            )
            lines.append("")
        if not r.findings:
            lines.append("_  Dockerfile clean — no issues detected._")
        else:
            lines.append("| Sev | Rule | Title | Auto-fix | Fix hint |")
            lines.append("|---|---|---|---|---|")
            for f in sorted(r.findings, key=lambda x: -_SEV_RANK.get(x.severity, 0)):
                title = f.title.replace("|", "\\|")
                fix = f.fix_hint.replace("|", "\\|")
                fixable = "Yes" if f.auto_fixable else "No"
                lines.append(
                    f"| {_severity_badge(f.severity)} | `{f.rule_id}` | {title} | {fixable} | {fix} |"
                )
        lines.append("")
        if r.fixes_applied:
            lines.append("**Fixes applied:**")
            for fd in r.fixes_applied:
                lines.append(f"- {fd}")
            lines.append("")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─── Auto-fix engine ─────────────────────────────────────────────────────

def _apply_fixes(content: str, findings: list[DockerfileFinding], jev_verdict: Optional[dict]) -> tuple[str, list[str]]:
    """Apply auto-fixes for auto_fixable rules. Returns (new_content, list_of_fix_descriptions).

    Jev safety check: if jev_verdict present and safe_to_autofix < 50%,
    skip auto-fix and report.
    """
    if jev_verdict:
        answers = jev_verdict.get("answers", {})
        safe_ans = answers.get("safe_to_autofix", {})
        safe_prob = float(safe_ans.get("noul", 1.0))
        if safe_prob < 0.5:
            console.print(
                f"[yellow]  Jev safe_to_autofix={safe_prob:.0%} (<50%) "
                f"— auto-fix DILEWATI untuk keamanan.[/yellow]"
            )
            return content, []

    import re
    fixes: list[str] = []

    # from-as-lowercase: normalize `as` → `AS`
    from_as_re = re.compile(r"(?m)^([ \t]*FROM\s+\S+(?:\s+\S+)*?)\s+as\s+(\S+)")
    content, n = from_as_re.subn(r"\1 AS \2", content)
    if n:
        fixes.append(f"Normalized {n} `FROM ... AS ...` casing")

    # deprecated-maintainer → LABEL maintainer=
    maint_re = re.compile(r"(?im)^(?P<indent>[ \t]*)MAINTAINER\s+(?P<val>.+?)\s*$")
    def _maint_repl(m):
        indent = m.group("indent")
        val = m.group("val").strip().strip('"').replace('"', '\\"')
        return f'{indent}LABEL maintainer="{val}"'
    content, n = maint_re.subn(_maint_repl, content)
    if n:
        fixes.append(f"Converted {n} deprecated `MAINTAINER` to `LABEL maintainer=`")

    # env-legacy-syntax: ENV KEY value → ENV KEY=value
    env_re = re.compile(
        r"(?m)^(?P<indent>[ \t]*)ENV\s+(?P<key>[A-Z_][A-Z0-9_]*)\s+(?P<val>[^=\n][^\n]*?)\s*$"
    )
    def _env_repl(m):
        indent = m.group("indent")
        key = m.group("key")
        val = m.group("val").strip()
        if " " in val and not (val.startswith('"') and val.endswith('"')):
            val = f'"{val}"'
        return f"{indent}ENV {key}={val}"
    content, n = env_re.subn(_env_repl, content)
    if n:
        fixes.append(f"Converted {n} legacy `ENV KEY value` to `ENV KEY=value`")

    # arg-github-token-legacy: remove ARG GITHUB_*
    arg_re = re.compile(r"(?m)^[ \t]*ARG\s+GITHUB_(USER|TOKEN|PASSWORD)\s*(=[^\n]*)?\n")
    content, n = arg_re.subn("", content)
    if n:
        fixes.append(f"Removed {n} `ARG GITHUB_*` line(s)")

    # legacy-github-secrets → migrate to netrc
    legacy_secret_re = re.compile(
        r"(?m)^(?P<indent>[ \t]*)RUN\s+--mount=type=secret,id=github_(token|user)\S*\s+"
    )
    if legacy_secret_re.search(content):
        fixes.append("Detected legacy github_token/user secrets — needs manual migration to netrc")

    # netrc-secret-wrong-target → fix target
    netrc_wrong = re.compile(r"--mount=type=secret,id=netrc(?!,target=/root/\.netrc)")
    content, n = netrc_wrong.subn("--mount=type=secret,id=netrc,target=/root/.netrc", content)
    if n:
        fixes.append(f"Fixed {n} netrc secret mount target to `/root/.netrc`")

    # workdir-commented-out → uncomment
    workdir_re = re.compile(r"(?m)^([ \t]*)#\s*(WORKDIR\s+/app\s*)$")
    content, n = workdir_re.subn(r"\1\2", content)
    if n:
        fixes.append(f"Uncommented {n} `WORKDIR /app` line(s)")

    # add-instead-of-copy (simple cases only)
    add_re = re.compile(r"(?m)^([ \t]*)ADD\s+(?!https?://)(?!.*\.tar)(?!.*\.gz)(\S+\s+\S+)")
    content, n = add_re.subn(r"\1COPY \2", content)
    if n:
        fixes.append(f"Replaced {n} `ADD` with `COPY` for local files")

    # pip-no-cache
    pip_re = re.compile(r"(pip3?\s+install)\s+(?!.*--no-cache-dir)")
    content, n = pip_re.subn(r"\1 --no-cache-dir ", content)
    if n:
        fixes.append(f"Added `--no-cache-dir` to {n} pip install command(s)")

    return content, fixes


# ─── Public entry ─────────────────────────────────────────────────────────

def run_dockerfile_scan(
    target_path: str,
    *,
    use_jev: bool = True,
    auto_fix: bool = True,
    scan_only: bool = False,
    export_csv_path: Optional[str] = None,
    export_md_path: Optional[str] = None,
) -> int:
    """Scan Dockerfile(s) for issues, optionally fix with Jev approval.

    Returns exit code: 0=clean, 1=warning, 2=error.
    """
    path = Path(target_path).expanduser()
    if not path.exists():
        print(f"  Path tidak ada: {path}", file=sys.stderr)
        return 2

    # Collect files
    if path.is_dir():
        files = sorted(path.glob("**/Dockerfile*"))
        if not files:
            console.print(f"[yellow]  Tidak ada Dockerfile di {path}[/yellow]")
            return 0
        console.print(f"[dim]  Found {len(files)} Dockerfile(s) di {path}[/dim]")
    else:
        files = [path]

    all_results: list[ScanResult] = []

    for f in files:
        content = f.read_text(encoding="utf-8", errors="replace")
        issues = analyze_dockerfile(content)
        findings = _issues_to_findings(issues)
        result = ScanResult(file_path=f, findings=findings)
        all_results.append(result)

    # Jev verdict (hanya untuk yang punya findings)
    if use_jev:
        to_ask = [r for r in all_results if r.findings]
        if to_ask:
            console.print(
                f"[dim]  Memanggil Jev untuk {len(to_ask)} file dgn findings...[/dim]"
            )
        for r in to_ask:
            r.jev_verdict = _ask_jev(r)

    # Auto-fix
    if auto_fix and not scan_only:
        for r in all_results:
            if not r.findings:
                continue
            has_fixable = any(f.auto_fixable for f in r.findings)
            if not has_fixable:
                continue
            content = r.file_path.read_text(encoding="utf-8", errors="replace")
            new_content, fixes = _apply_fixes(content, r.findings, r.jev_verdict)
            if fixes and new_content != content:
                backup = r.file_path.with_suffix(r.file_path.suffix + ".bak")
                backup.write_text(content, encoding="utf-8")
                r.file_path.write_text(new_content, encoding="utf-8")
                r.fixes_applied = fixes

    # Render header
    total_findings = sum(len(r.findings) for r in all_results)
    total_fixes = sum(len(r.fixes_applied) for r in all_results)
    subtitle = (
        f"{len(files)} file * {total_findings} findings * "
        f"{total_fixes} fixes applied * Jev: {'on' if use_jev else 'off'}"
    )
    console.print(Panel.fit(
        f"[bold green]bq --fix-dockerfile[/bold green]  "
        f"[dim]{path}[/dim]\n{subtitle}",
        border_style="green",
    ))

    for r in all_results:
        _render_result(r)

    # Overall summary
    worst_sev = max(
        (r.worst_severity for r in all_results),
        key=lambda s: _SEV_RANK.get(s, -1),
        default="clean",
    )
    style = _SEV_STYLE.get(worst_sev, "white")
    console.print(
        f"\n[bold]Overall:[/bold] [{style}]{worst_sev.upper()}[/{style}]"
    )

    # Export
    if export_csv_path:
        export_csv(all_results, Path(export_csv_path))
        console.print(f"[green]  CSV ditulis: {export_csv_path}[/green]")
    if export_md_path:
        export_markdown(all_results, Path(export_md_path), use_jev=use_jev)
        console.print(f"[green]  Markdown ditulis: {export_md_path}[/green]")

    return _EXIT_FROM_SEV.get(worst_sev, 2)  # end run_dockerfile_scan_remote


# ─── Shell helper ─────────────────────────────────────────────────────────

def _sh(cmd: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


# ─── PR-fix mode (remote scan → clone → fix → commit → push → PR) ───────

def run_dockerfile_pr_fix(
    api_repo: str,
    ref: str,
    *,
    dockerfile_path: str = "Dockerfile",
    use_jev: bool = True,
    export_csv_path: Optional[str] = None,
    export_md_path: Optional[str] = None,
) -> int:
    """Scan remote Dockerfile, jika ada error-level → clone → fix → buat PR.

    Skip PR jika hanya warning/info (tidak ada error-level findings).

    Returns: 0=PR created atau clean, 1=warning only (skip PR), 2=hard error.
    """
    from . import github_api
    from .config import load_config

    config = load_config()
    gh = config["github"]
    if not gh["token"]:
        console.print("[red]  GITHUB_TOKEN kosong — dibutuhkan untuk PR.[/red]")
        return 2

    # ── Step 1: Fetch + scan (tanpa clone) ───────────────────────────────
    console.print(f"[dim]  Scan remote: {api_repo}@{ref} path={dockerfile_path}[/dim]")
    content = _fetch_remote_file(api_repo, dockerfile_path, ref)
    if content is None:
        console.print(f"[red]  File tidak ditemukan: {api_repo}:{dockerfile_path}@{ref}[/red]")
        return 2

    issues = analyze_dockerfile(content)
    findings = _issues_to_findings(issues)
    display_path = Path(f"{api_repo}@{ref}:{dockerfile_path}")
    result = ScanResult(file_path=display_path, findings=findings)

    # ── Step 2: Jev verdict ──────────────────────────────────────────────
    if use_jev and result.findings:
        console.print("[dim]  Memanggil Jev...[/dim]")
        result.jev_verdict = _ask_jev(result)

    # ── Render scan results ──────────────────────────────────────────────
    total = len(result.findings)
    console.print(Panel.fit(
        f"[bold green]bq --fix-dockerfile --pr-fix[/bold green]  "
        f"[dim]{api_repo}@{ref}[/dim]\n"
        f"1 file * {total} findings * Jev: {'on' if use_jev else 'off'}",
        border_style="blue",
    ))
    _render_result(result)

    worst_sev = result.worst_severity
    style = _SEV_STYLE.get(worst_sev, "white")
    console.print(f"\n[bold]Overall:[/bold] [{style}]{worst_sev.upper()}[/{style}]")

    # ── Export ────────────────────────────────────────────────────────────
    if export_csv_path:
        export_csv([result], Path(export_csv_path))
        console.print(f"[green]  CSV ditulis: {export_csv_path}[/green]")
    if export_md_path:
        export_markdown([result], Path(export_md_path), use_jev=use_jev)
        console.print(f"[green]  Markdown ditulis: {export_md_path}[/green]")

    # ── Step 3: Decision — skip jika hanya warning/info ──────────────────
    if not result.findings:
        console.print("\n[green]  Dockerfile clean — tidak perlu PR.[/green]")
        return 0

    has_errors = any(f.severity == "error" for f in result.findings)
    has_fixable = any(f.auto_fixable for f in result.findings)

    if not has_errors:
        console.print(
            f"\n[yellow]  Hanya warning/info ({total} findings) — PR dilewati.[/yellow]"
        )
        console.print("[dim]  PR hanya dibuat jika ada error-level findings.[/dim]")
        return 1

    if not has_fixable:
        console.print(
            f"\n[yellow]  Ada error tapi tidak ada rule auto-fixable — PR dilewati.[/yellow]"
        )
        console.print("[dim]  Fix manual diperlukan. Lihat fix hints di atas.[/dim]")
        return 2

    # ── Step 4: Clone → fix → commit → push → PR ────────────────────────
    console.print(f"\n[bold]  Ada {result.error_count} error — memulai PR fix...[/bold]")

    token = gh["token"]
    ts = time.strftime("%Y%m%d-%H%M%S")
    repo_short = api_repo.split("/")[-1]
    workdir = Path.home() / ".build-q" / "workdir" / f"dockerfile-fix-{repo_short}-{ts}"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    repo_dir = workdir / "repo"

    console.print(f"[dim]  Clone {api_repo}@{ref} ...[/dim]")
    clone_url = f"https://x-access-token:{token}@github.com/{api_repo}.git"
    try:
        _sh(["git", "clone", "--depth", "1", "--branch", ref, clone_url, str(repo_dir)])
    except subprocess.CalledProcessError as e:
        console.print(f"[red]  Clone gagal: {e.stderr}[/red]")
        shutil.rmtree(workdir, ignore_errors=True)
        return 2

    head_sha = _sh(["git", "rev-parse", "HEAD"], cwd=str(repo_dir)).stdout.strip()

    branch_name = f"fix/dockerfile-{ts}"
    _sh(["git", "checkout", "-b", branch_name], cwd=str(repo_dir))

    # Apply fixes
    target = repo_dir / dockerfile_path
    if not target.exists():
        console.print(f"[red]  {dockerfile_path} tidak ada di clone[/red]")
        shutil.rmtree(workdir, ignore_errors=True)
        return 2

    original = target.read_text(encoding="utf-8", errors="replace")
    new_content, fixes = _apply_fixes(original, result.findings, result.jev_verdict)

    if not fixes or new_content == original:
        console.print("[yellow]  Auto-fix tidak mengubah Dockerfile — PR dilewati.[/yellow]")
        shutil.rmtree(workdir, ignore_errors=True)
        return 1

    target.write_text(new_content, encoding="utf-8")
    result.fixes_applied = fixes

    console.print(f"[green]  {len(fixes)} fix(es) applied:[/green]")
    for fd in fixes:
        console.print(f"   * {fd}")

    # Commit
    _sh(["git", "add", dockerfile_path], cwd=str(repo_dir))
    fix_summary = "; ".join(fixes[:3])
    if len(fixes) > 3:
        fix_summary += f" (+{len(fixes) - 3} more)"
    commit_msg = (
        f"fix(dockerfile): auto-fix {len(fixes)} issue via bq --fix-dockerfile\n\n"
        f"Fixes: {fix_summary}\n"
        f"Base: {ref} @ {head_sha[:12]}\n"
        f"Scanner: {total} findings ({result.error_count} error, "
        f"{result.warning_count} warning, {result.info_count} info)\n"
        f"Generated with bq --fix-dockerfile --pr-fix (build-q)"
    )
    _sh(
        ["git", "-c", "user.name=build-q bot", "-c", "user.email=bq@build-q.local",
         "commit", "-m", commit_msg],
        cwd=str(repo_dir),
    )

    # Push
    console.print(f"[dim]  Push → origin/{branch_name} ...[/dim]")
    try:
        _sh(["git", "push", "origin", branch_name], cwd=str(repo_dir))
    except subprocess.CalledProcessError as e:
        console.print(f"[red]  Push gagal: {e.stderr}[/red]")
        shutil.rmtree(workdir, ignore_errors=True)
        return 2

    # Deduplicate
    try:
        existing = github_api.list_open_prs(api_repo, branch_name)
        if existing:
            url = existing[0].get("html_url", "(unknown)")
            console.print(f"[cyan]  PR sudah ada: {url}[/cyan]")
            shutil.rmtree(workdir, ignore_errors=True)
            return 0
    except github_api.GitHubAPIError:
        pass

    # PR body
    body_lines = [
        "Auto-fix Dockerfile issues terdeteksi oleh `bq --fix-dockerfile --pr-fix`.",
        "",
        f"**Base:** `{ref}` @ `{head_sha[:12]}`",
        f"**Scanner:** {total} findings "
        f"({result.error_count} error, {result.warning_count} warning, {result.info_count} info)",
        "",
        "**Fixes applied:**",
    ]
    for fd in fixes:
        body_lines.append(f"- {fd}")

    unfixed = [f for f in result.findings if not f.auto_fixable]
    if unfixed:
        body_lines += ["", "**Remaining issues (manual fix required):**"]
        for f in unfixed:
            icon = "🔴" if f.severity == "error" else "🟡" if f.severity == "warning" else "🔵"
            body_lines.append(f"- {icon} **{f.rule_id}** — {f.title}")
            body_lines.append(f"  - Fix: {f.fix_hint}")

    if result.jev_verdict:
        answers = result.jev_verdict.get("answers", {})
        sev_ans = answers.get("overall_severity", {})
        safe_ans = answers.get("safe_to_autofix", {})
        prio_ans = answers.get("fix_priority", {})
        body_lines += [
            "",
            f"**Jev verdict:** severity=`{sev_ans.get('choice', '?')}` "
            f"({sev_ans.get('confidence', 0):.0%}) | "
            f"safe_to_autofix={float(safe_ans.get('noul', 0)):.0%} | "
            f"fix_priority=`{prio_ans.get('choice', '?')}`",
        ]

    body_lines += ["", "Generated with `bq --fix-dockerfile --pr-fix` (build-q)."]

    console.print(f"[dim]  Opening PR → {ref} ...[/dim]")
    try:
        pr = github_api.create_pull_request(
            api_repo, base=ref, head=branch_name,
            title=f"fix(dockerfile): auto-fix {len(fixes)} issue ({result.error_count} error detected)",
            body="\n".join(body_lines),
        )
        console.print(f"[bold green]  PR created: {pr.get('html_url')}[/bold green]")
    except github_api.GitHubAPIError as e:
        console.print(f"[red]  Gagal buat PR: {e}[/red]")
        shutil.rmtree(workdir, ignore_errors=True)
        return 2

    shutil.rmtree(workdir, ignore_errors=True)
    return 0


# ─── Remote mode (GitHub API, no clone) ──────────────────────────────────

def _fetch_remote_file(api_repo: str, path: str, ref: str) -> Optional[str]:
    """Fetch single file from GitHub via REST API. Returns content or None."""
    try:
        from .github_api import get_contents_raw, GitHubAPIError
    except ImportError:
        console.print("[red]  github_api module not available[/red]")
        return None
    try:
        raw = get_contents_raw(api_repo, path, ref)
        return raw.decode("utf-8")
    except GitHubAPIError as e:
        console.print(f"[red]  GitHub API error: {e}[/red]")
        return None


def _list_remote_dockerfiles(api_repo: str, ref: str, root: str = "") -> list[str]:
    """List Dockerfile paths in repo root via GitHub tree API."""
    try:
        from .github_api import _request, _token, GitHubAPIError
    except ImportError:
        return []
    try:
        data = _request(
            "GET",
            f"/repos/{api_repo}/git/trees/{ref}?recursive=1",
        )
        paths = []
        for item in data.get("tree", []):
            if item.get("type") != "blob":
                continue
            p = item["path"]
            name = p.rsplit("/", 1)[-1] if "/" in p else p
            if not name.startswith("Dockerfile"):
                continue
            if root and not p.startswith(root.rstrip("/") + "/") and p != root:
                continue
            paths.append(p)
        return sorted(paths)
    except Exception:
        return []


def run_dockerfile_scan_remote(
    api_repo: str,
    ref: str,
    *,
    dockerfile_path: str = "Dockerfile",
    use_jev: bool = True,
    export_csv_path: Optional[str] = None,
    export_md_path: Optional[str] = None,
) -> int:
    """Scan Dockerfile(s) di remote GitHub repo tanpa clone.

    Fetch file content via GitHub REST API → run checks → Jev → report.
    Auto-fix TIDAK tersedia di remote mode (read-only).

    Returns exit code: 0=clean, 1=warning, 2=error.
    """
    from .config import load_config
    console.print(
        f"[dim]  Remote scan: {api_repo}@{ref} path={dockerfile_path}[/dim]"
    )

    # Determine files to scan
    file_paths: list[str] = []

    if dockerfile_path.endswith("/") or dockerfile_path == ".":
        root = "" if dockerfile_path == "." else dockerfile_path
        console.print(f"[dim]  Listing Dockerfiles in repo tree...[/dim]")
        file_paths = _list_remote_dockerfiles(api_repo, ref, root)
        if not file_paths:
            console.print(
                f"[yellow]  Tidak ada Dockerfile ditemukan di "
                f"{api_repo}@{ref}:{dockerfile_path}[/yellow]"
            )
            return 0
        console.print(f"[dim]  Found {len(file_paths)} Dockerfile(s)[/dim]")
    else:
        file_paths = [dockerfile_path]

    # Fetch & scan each file
    all_results: list[ScanResult] = []
    for fpath in file_paths:
        content = _fetch_remote_file(api_repo, fpath, ref)
        if content is None:
            console.print(f"[red]  File tidak ditemukan: {api_repo}:{fpath}@{ref}[/red]")
            continue
        issues = analyze_dockerfile(content)
        findings = _issues_to_findings(issues)
        display_path = Path(f"{api_repo}@{ref}:{fpath}")
        result = ScanResult(file_path=display_path, findings=findings)
        all_results.append(result)

    if not all_results:
        console.print("[red]  Tidak ada file berhasil di-fetch[/red]")
        return 2

    # Jev verdict
    if use_jev:
        to_ask = [r for r in all_results if r.findings]
        if to_ask:
            console.print(
                f"[dim]  Memanggil Jev untuk {len(to_ask)} file dgn findings...[/dim]"
            )
        for r in to_ask:
            r.jev_verdict = _ask_jev(r)

    # Render
    total_findings = sum(len(r.findings) for r in all_results)
    subtitle = (
        f"{len(all_results)} file * {total_findings} findings * "
        f"remote (read-only) * Jev: {'on' if use_jev else 'off'}"
    )
    console.print(Panel.fit(
        f"[bold green]bq --fix-dockerfile (remote)[/bold green]  "
        f"[dim]{api_repo}@{ref}[/dim]\n{subtitle}",
        border_style="blue",
    ))

    for r in all_results:
        _render_result(r)

    # Overall
    worst_sev = max(
        (r.worst_severity for r in all_results),
        key=lambda s: _SEV_RANK.get(s, -1),
        default="clean",
    )
    style = _SEV_STYLE.get(worst_sev, "white")
    console.print(
        f"\n[bold]Overall:[/bold] [{style}]{worst_sev.upper()}[/{style}]"
    )

    if any(r.findings for r in all_results):
        fixable = sum(
            1 for r in all_results
            for f in r.findings if f.auto_fixable
        )
        if fixable:
            console.print(
                f"\n[dim]  {fixable} issue auto-fixable. "
                f"Untuk auto-fix, clone repo lalu jalankan:[/dim]"
            )
            console.print(
                f"[dim]  bq --fix-dockerfile <path/Dockerfile>[/dim]"
            )

    # Export
    if export_csv_path:
        export_csv(all_results, Path(export_csv_path))
        console.print(f"[green]  CSV ditulis: {export_csv_path}[/green]")
    if export_md_path:
        export_markdown(all_results, Path(export_md_path), use_jev=use_jev)
        console.print(f"[green]  Markdown ditulis: {export_md_path}[/green]")

    return _EXIT_FROM_SEV.get(worst_sev, 2)
