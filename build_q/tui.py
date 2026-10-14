"""Jev Agent Planner — TUI interaktif yang memilih tool build-q via Jev.

Fitur:
- Katalog tool, provider, dan regex pattern **100% dari PocketBase**
  (collection `build_q_tools`, `build_q_providers`, `build_q_patterns`).
  Cache lokal `~/.build-q/.tui-cache.json` TTL 1 jam. Fallback bootstrap
  minimum bila PB down + cache kosong.
- Provider switchable runtime (`/provider openrouter`).
- Top-3 kandidat tool ditampilkan dengan score + preview perintah.
- Konfirmasi eksekusi dengan tag RISKY untuk perintah destruktif. Penilaian
  risiko memakai `build_q.tool_policy` — sama dengan tier tool MCP `bq --mcp`.
- Audit log JSON-lines di `~/.build-q/.tui-audit.log` (format sama dengan MCP).

Butuh: `rich`, `requests`.
"""
from __future__ import annotations

import atexit
import contextlib
import logging
import os
import re
import shlex
import string
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .audit import redact, setup_audit_logger
from .config import CONFIG_DIR, ENV_FILE, load_config
from .tool_policy import command_policy
from . import tui_catalog

audit_log = logging.getLogger("bq-tui")
HISTORY_FILE = CONFIG_DIR / ".tui-history"

# ---------------------------------------------------------------------------
# .env handling
# ---------------------------------------------------------------------------


def _candidate_env_paths() -> list[Path]:
    """Lokasi .env legacy di samping source build-q (instalasi editable).

    Sengaja TIDAK membaca `.env` di cwd: menjalankan `bq --tui` di dalam repo
    aplikasi tidak boleh ikut memuat env/secret aplikasi tersebut.
    """
    script_dir = Path(__file__).resolve().parent
    return [
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
# Catalog accessors (PocketBase-backed, cached)
# ---------------------------------------------------------------------------

@dataclass
class Provider:
    name: str
    url: str
    model: str
    key_env: str


_catalog_cache: dict | None = None
_catalog_loaded_at = 0.0


def catalog(*, force_refresh: bool = False) -> dict:
    """Lazy-load katalog dari PB (cache per-process + file cache).

    Cache per-process ikut TTL file cache, jadi sesi panjang tetap
    mendapat katalog terbaru tanpa restart.
    """
    global _catalog_cache, _catalog_loaded_at
    ttl = tui_catalog._ttl_seconds()
    expired = ttl > 0 and (time.monotonic() - _catalog_loaded_at) >= ttl
    if _catalog_cache is None or force_refresh or expired:
        _catalog_cache = tui_catalog.load_catalog(force_refresh=force_refresh)
        _catalog_loaded_at = time.monotonic()
    return _catalog_cache


def TOOLS() -> dict:
    return catalog()["tools"]


def PROVIDERS() -> dict:
    return catalog()["providers"]


def RISKY_TOOLS() -> set:
    return catalog()["risky"]


def TEXT_PATTERNS() -> dict:
    return catalog()["patterns"]


def resolve_provider(name: str | None = None) -> Provider:
    """Ambil provider aktif; url/model bisa dioverride via env (mis. TYPESAFE_URL)."""
    providers = PROVIDERS()
    if not providers:
        raise RuntimeError(
            "Belum ada provider di PocketBase collection `build_q_providers`. "
            "Jalankan `bq --tui-sync scripts/build-q-seed.json`."
        )
    default = catalog()["default_provider"] or next(iter(providers))
    name = (name or os.environ.get("JEV_PROVIDER") or default).lower()
    if name not in providers:
        raise RuntimeError(
            f"Provider '{name}' tidak dikenal. Pilih: {list(providers)}"
        )
    p = providers[name]
    return Provider(
        name=p["name"],
        url=os.environ.get(f"{p['name'].upper()}_URL", p["url"]),
        model=os.environ.get(f"{p['name'].upper()}_MODEL", p["model"]),
        key_env=p["key_env"],
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
# Tool catalog / regex patterns — SEMUA dari PocketBase (collection
# `build_q_tools`, `build_q_patterns`, `build_q_providers`). Gunakan
# accessor di atas: `TOOLS()`, `PROVIDERS()`, `RISKY_TOOLS()`,
# `TEXT_PATTERNS()` — JANGAN deklarasikan konstanta hardcoded di sini.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Decision + parameter extraction
# ---------------------------------------------------------------------------

def extract_params_from_text(user_msg: str, tool_name: str) -> dict:
    """Isi param text (regex) dan choice (word-match); apply default_template dari param lain."""
    meta = TOOLS()[tool_name]
    patterns = TEXT_PATTERNS()
    out: dict = {}

    # Pass 1: regex-based & choice-based extraction
    for pname, pspec in meta["params"].items():
        if pspec["type"] == "text":
            pat = patterns.get(pname)
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


class _PlaceholderDict(dict):
    """Param yang tidak ada di dict dirender sebagai `<nama>`, bukan KeyError."""

    def __missing__(self, key: str) -> str:
        return f"<{key}>"


def preview_command(tool_name: str, params: dict) -> str:
    template = TOOLS()[tool_name]["template"]
    try:
        return string.Formatter().vformat(template, (), _PlaceholderDict(params))
    except (ValueError, IndexError, AttributeError) as e:
        raise ValueError(f"template tool '{tool_name}' tidak valid: {template!r} ({e})") from e


@dataclass
class Decision:
    tool: str
    confidence: float = 0.0
    probabilities: dict = field(default_factory=dict)
    noul: float = 0.0


def parse_decision(resp: dict) -> Decision:
    """Validasi respons Jev; raise ValueError bila bentuknya tidak sesuai."""
    answers = resp.get("answers") if isinstance(resp, dict) else None
    if not isinstance(answers, dict):
        raise ValueError("respons Jev tidak berisi `answers`")
    tool_ans = answers.get("tool") or {}
    tool = tool_ans.get("choice") if isinstance(tool_ans, dict) else None
    if not isinstance(tool, str) or not tool:
        raise ValueError("respons Jev tidak berisi `answers.tool.choice`")
    if tool != "none" and tool not in TOOLS():
        raise ValueError(
            f"Jev memilih tool '{tool}' yang tidak ada di katalog — coba /refresh"
        )
    risky_ans = answers.get("risky") or {}
    try:
        noul = float(risky_ans.get("noul", 0.0) or 0.0)
    except (TypeError, ValueError, AttributeError):
        noul = 0.0
    probs = tool_ans.get("probabilities") or {}
    return Decision(
        tool=tool,
        confidence=float(tool_ans.get("confidence", 0.0) or 0.0),
        probabilities=probs if isinstance(probs, dict) else {},
        noul=noul,
    )


def assess_risk(tool: str, noul: float, cmd: str | None = None) -> tuple[bool, list[str]]:
    """Gabungan sinyal risiko (OR): flag katalog, penilaian Jev, tier tool MCP.

    Tier MCP hanya bisa menaikkan risiko, tidak pernah menurunkannya.
    """
    reasons: list[str] = []
    if tool in RISKY_TOOLS():
        reasons.append("katalog: risky")
    if noul >= 0.5:
        reasons.append(f"Jev: destructive noul={noul:.2f}")
    if cmd:
        mcp_tool, policy = command_policy(cmd)
        if policy and policy.risky:
            reasons.append(f"MCP: {mcp_tool} (tier {policy.tier})")
    return bool(reasons), reasons


def pick_tool(user_msg: str, provider: Provider) -> dict:
    criteria = {name: meta["desc"] for name, meta in TOOLS().items()}
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
    return jev_decide({"user_request": redact(user_msg)}, questions, provider)


def fill_missing_choice_params(user_msg: str, tool_name: str, provider: Provider) -> dict:
    """Untuk param type=choice yang belum ke-extract regex, minta Jev pilih valuenya."""
    meta = TOOLS()[tool_name]
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
    resp = jev_decide({"user_request": redact(user_msg), "tool": tool_name}, choice_qs, provider)
    out: dict = {}
    for k, v in (resp.get("answers") or {}).items():
        choice = v.get("choice") if isinstance(v, dict) else None
        if k in choice_qs and choice in meta["params"][k].get("options", []):
            out[k] = choice
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

console = Console()


def score_bar(prob: float, width: int = 20) -> str:
    n = int(round(prob * width))
    return "█" * n + "░" * (width - n)


def render_top_suggestions(user_msg: str, decision: Decision) -> str:
    """Tampilkan top-3 kandidat tool dengan score bar + preview perintah."""
    probs = {k: float(v) for k, v in decision.probabilities.items() if k == "none" or k in TOOLS()}
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


def render_verdict(decision: Decision) -> bool:
    risky, _ = assess_risk(decision.tool, decision.noul)
    tag = "[red]RISKY[/red]" if risky else "[green]safe[/green]"
    console.print(
        f"→ Pilihan Jev: [bold green]{decision.tool}[/bold green] "
        f"(confidence {decision.confidence:.2%}) {tag} "
        f"(destructive noul={decision.noul:.2f})"
    )
    return risky


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
    for name, meta in TOOLS().items():
        tbl.add_row(name, meta["desc"], meta["template"])
    console.print(tbl)


def _mask(value: str) -> str:
    if not value:
        return "<empty>"
    return (value[:8] + "…" + value[-4:]) if len(value) > 12 else "<short>"


def show_provider(provider: Provider) -> None:
    masked = _mask(os.environ.get(provider.key_env, ""))
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
        "  [cyan]/set KEY[/cyan]                  set env var, value diketik tersembunyi\n"
        "  [cyan]/set KEY VALUE[/cyan]            set env var langsung (terlihat di layar)\n"
        "  [cyan]/refresh[/cyan]                  ambil ulang katalog dari PocketBase\n"
        "  [cyan]/e2e REPO REF[/cyan]             analisa CI/CD end-to-end: hook → pipeline → GitOps → ArgoCD\n"
        "  [cyan]/e2e --sim [SKENARIO][/cyan]     simulasi e2e tanpa cluster (tanpa nama: daftar skenario)\n"
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
        if len(parts) < 2:
            console.print("[red]usage: /set KEY [VALUE][/red]")
        else:
            k = parts[1]
            v = " ".join(parts[2:]) if len(parts) > 2 else Prompt.ask(
                f"  value {k}", password=True,
            )
            os.environ[k] = v
            console.print(f"[green]{k} set (in-memory, {_mask(v)})[/green]")
        return True
    if head == "/e2e":
        if len(parts) >= 2 and parts[1] == "--sim":
            if len(parts) == 2:
                show_e2e_scenarios()
            else:
                run_e2e_sim(parts[2])
        elif len(parts) < 3:
            console.print("[red]usage: /e2e REPO REF[/red]  (mis. /e2e pay-be-topup-manager develop)"
                          "  ·  [red]/e2e --sim [SKENARIO][/red]")
        else:
            run_e2e(parts[1], parts[2])
        return True
    if head == "/refresh":
        try:
            cat = catalog(force_refresh=True)
            console.print(
                f"[green]katalog di-refresh ({cat['source']}): "
                f"{len(cat['tools'])} tools[/green]"
            )
        except Exception as e:
            console.print(f"[red]refresh gagal:[/red] {e}")
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


_E2E_STYLE = {
    "ok": "green", "warn": "yellow", "fail": "red", "skip": "dim", "running": "cyan",
}


def render_e2e(report) -> None:
    """Render laporan `build_q.e2e.E2EReport` sebagai rantai + panel per tahap."""
    from .e2e import STATUS_ICON

    head = f"[bold]{escape(report.repo)}[/bold] @ [bold]{escape(report.ref)}[/bold]"
    if report.sha:
        head += escape(f"  commit {report.sha[:7]} · ENV={report.env} · "
                       f"image {report.image}:{report.expected_tag}")
    console.print(Panel.fit(head, title="[magenta]E2E CI/CD[/magenta]", border_style="magenta"))

    bp = report.break_point
    chain = "  →  ".join(
        f"[{_E2E_STYLE[s.status]}]{STATUS_ICON[s.status]} "
        f"{'[u]' if s is bp else ''}{s.title}{'[/u]' if s is bp else ''}[/{_E2E_STYLE[s.status]}]"
        for s in report.stages
    )
    console.print(chain)

    for s in report.stages:
        body = [f"[bold]{escape(s.summary)}[/bold]"] + [f"• {escape(d)}" for d in s.details]
        if s.data.get("log_tail"):
            body.append("[dim]" + escape(s.data["log_tail"].rstrip()) + "[/dim]")
        body += [f"💡 {escape(h)}" for h in s.hints]
        console.print(Panel(
            "\n".join(body),
            title=f"{STATUS_ICON[s.status]} {s.title}",
            title_align="left",
            border_style=_E2E_STYLE[s.status],
        ))

    if bp:
        console.print(
            f"🎯 [bold]Titik putus:[/bold] [{_E2E_STYLE[bp.status]}]{bp.title}[/] — {escape(bp.summary)}"
        )
    elif report.unchecked:
        names = ", ".join(s.title for s in report.unchecked)
        console.print(f"[yellow]❔ Analisa tidak lengkap — tidak bisa memeriksa: {escape(names)}. "
                      "Belum bisa dipastikan live.[/yellow]")
    else:
        console.print("[green]🎉 Alur end-to-end sehat — commit ini sudah live.[/green]")


def show_e2e_scenarios() -> None:
    from .e2e_sim import SCENARIOS

    tbl = Table(title="Skenario simulasi /e2e --sim", header_style="bold magenta")
    tbl.add_column("Skenario", style="cyan", no_wrap=True)
    tbl.add_column("Titik putus")
    tbl.add_column("Verdict")
    tbl.add_column("Keterangan")
    for sc in SCENARIOS.values():
        tbl.add_row(sc.name, sc.break_point or "-", sc.verdict, sc.title)
    console.print(tbl)


def run_e2e_sim(name: str) -> None:
    from .e2e_sim import SCENARIOS, simulate, verify

    if name not in SCENARIOS:
        console.print(f"[red]skenario '{escape(name)}' tidak dikenal[/red] — ketik /e2e --sim")
        return
    console.print(f"[magenta]🧪 SIMULASI[/magenta] {name} — {escape(SCENARIOS[name].title)}")
    report = simulate(name)
    render_e2e(report)
    problems = verify(name, report)
    if problems:
        console.print("[red]Tidak sesuai harapan skenario:[/red] " + escape("; ".join(problems)))
    _audit("e2e_sim", scenario=name, verdict=report.verdict, ok=not problems)


def run_e2e(repo: str, ref: str) -> None:
    from . import e2e

    t0 = time.monotonic()
    with console.status("[cyan]E2E: source…[/cyan]") as status:
        report = e2e.analyze(
            repo, ref,
            on_stage=lambda name: status.update(f"[cyan]E2E: {name}…[/cyan]"),
        )
    render_e2e(report)
    bp = report.break_point
    _audit(
        "e2e", repo=report.repo, ref=ref, sha=report.sha[:7],
        stages={s.key: s.status for s in report.stages},
        break_point=bp.key if bp else None, verdict=report.verdict,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )


def _audit(event: str, msg: str = "", **fields) -> None:
    audit_log.info(msg or event, extra={"audit": {"source": "tui", "event": event, **fields}})


def _setup_history() -> None:
    """Riwayat input (panah atas/bawah) via readline, disimpan antar sesi."""
    try:
        import readline
    except ImportError:  # Windows tanpa pyreadline
        return
    with contextlib.suppress(OSError):
        readline.read_history_file(HISTORY_FILE)
    readline.set_history_length(1000)

    def _save() -> None:
        with contextlib.suppress(OSError):
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            readline.write_history_file(HISTORY_FILE)
            HISTORY_FILE.chmod(0o600)

    atexit.register(_save)


def _collect_params(msg: str, tool: str, provider: Provider) -> dict:
    """Isi param: regex → Jev (choice) → prompt user, dengan default_template."""
    params = extract_params_from_text(msg, tool)
    params.update(fill_missing_choice_params(msg, tool, provider))
    meta = TOOLS()[tool]

    # Param "sumber" = yang direferensi default_template param lain.
    # Prompt dulu supaya default_template bisa auto-fill dependent params.
    referenced: set[str] = set()
    for pspec in meta["params"].values():
        tmpl = pspec.get("default_template")
        if tmpl:
            referenced.update(re.findall(r"\{(\w+)\}", tmpl))

    def _prompt(pname: str, pspec: dict) -> None:
        val = params.get(pname, "")
        if val and not str(val).startswith("<"):
            return
        hint = pspec.get("hint", "")
        default = pspec.get("default", "")
        params[pname] = Prompt.ask(
            f"  [cyan]param[/cyan] [bold]{pname}[/bold] [dim]({hint})[/dim]",
            default=default or None,
        )

    def _reapply_templates() -> None:
        for pname, pspec in meta["params"].items():
            tmpl = pspec.get("default_template")
            if not tmpl or not str(params.get(pname, "")).startswith("<"):
                continue
            try:
                candidate = tmpl.format(**params)
            except (KeyError, IndexError, ValueError, AttributeError):
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
    return params


def run_turn(msg: str, provider: Provider) -> None:
    """Satu giliran REPL: pilih tool → isi param → konfirmasi → eksekusi."""
    with console.status(f"[cyan]Jev ({provider.name}) sedang memutuskan...[/cyan]"):
        resp = pick_tool(msg, provider)
    decision = parse_decision(resp)

    render_top_suggestions(msg, decision)
    render_usage(resp)
    render_verdict(decision)
    _audit(
        "decide", tool=decision.tool, confidence=decision.confidence,
        noul=decision.noul, provider=provider.name,
    )

    if decision.tool == "none":
        console.print("[yellow]Tidak ada tool yang cocok. Coba lebih spesifik.[/yellow]")
        return

    params = _collect_params(msg, decision.tool, provider)
    cmd = preview_command(decision.tool, params)
    risky, reasons = assess_risk(decision.tool, decision.noul, cmd)
    mcp_tool, _ = command_policy(cmd)

    console.print(Panel(cmd, title="[magenta]Perintah Final[/magenta]", border_style="magenta"))
    if mcp_tool:
        console.print(f"[dim]padanan MCP: mcp__bq__{mcp_tool}[/dim]")
    if risky:
        console.print(f"[red]RISKY[/red] [dim]({'; '.join(reasons)})[/dim]")
    if "<" in cmd and ">" in cmd:
        console.print("[yellow]Masih ada param kosong (<...>) — periksa sebelum menjalankan.[/yellow]")

    prompt_label = "Jalankan?" + (" [red](RISKY)[/red]" if risky else "")
    audit_fields = {
        "tool": decision.tool, "mcp_tool": mcp_tool, "risky": risky,
        "cmd": redact(cmd),
    }
    if not Confirm.ask(prompt_label, default=not risky):
        console.print("[dim]dilewati[/dim]")
        _audit("skip", **audit_fields)
        return

    console.print(Panel(cmd, title="[yellow]Menjalankan[/yellow]", border_style="yellow"))
    t0 = time.monotonic()
    try:
        rc = subprocess.run(shlex.split(cmd), check=False).returncode
    except KeyboardInterrupt:
        console.print("[red]dibatalkan[/red]")
        rc = 130
    except OSError as e:
        console.print(f"[red]gagal menjalankan:[/red] {e}")
        rc = 127
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    if rc != 0:
        console.print(f"[red]exit code {rc}[/red]")
    _audit("execute", exit_code=rc, elapsed_ms=elapsed_ms, **audit_fields)


def repl() -> None:
    loaded = load_env()
    if loaded:
        console.print(f"[dim].env loaded from {loaded}[/dim]")
    setup_audit_logger(audit_log, ".tui-audit.log")
    _setup_history()
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
        "[dim]Teks request dikirim ke provider Jev; token/secret yang dikenali disamarkan.[/dim]\n"
        "[cyan]/help[/cyan] daftar perintah TUI",
        border_style="green",
    ))
    _audit("start", provider=current.name)

    while True:
        try:
            msg = Prompt.ask("\n[bold blue]you[/bold blue]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nSampai jumpa!")
            return
        if not msg:
            continue
        try:
            if msg.startswith("/") and handle_slash(msg, provider_ref):
                continue
            run_turn(msg, provider_ref[0])
        except KeyboardInterrupt:
            console.print("\n[red]dibatalkan[/red]")
        except Exception as e:  # satu giliran gagal tidak boleh mematikan sesi
            console.print(f"[red]error:[/red] {e}")
            _audit("error", error=f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    repl()
