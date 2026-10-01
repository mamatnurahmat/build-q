"""Jev Agent Planner — TUI interaktif yang memilih tool build-q via Jev.

Fitur:
- Katalog tool, provider, dan regex pattern **100% dari PocketBase**
  (collection `build_q_tools`, `build_q_providers`, `build_q_patterns`).
  Cache lokal `~/.build-q/.tui-cache.json` TTL 1 jam. Fallback bootstrap
  minimum bila PB down + cache kosong.
- Provider switchable runtime (`/provider openrouter`).
- Top-3 kandidat tool ditampilkan dengan score + preview perintah.
- Konfirmasi eksekusi dengan tag RISKY untuk perintah destruktif.

Butuh: `rich`, `requests`.
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
from . import tui_catalog

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
# Catalog accessors (PocketBase-backed, cached)
# ---------------------------------------------------------------------------

@dataclass
class Provider:
    name: str
    url: str
    model: str
    key_env: str


_catalog_cache: dict | None = None


def catalog(*, force_refresh: bool = False) -> dict:
    """Lazy-load katalog dari PB (cache per-process + file cache)."""
    global _catalog_cache
    if _catalog_cache is None or force_refresh:
        _catalog_cache = tui_catalog.load_catalog(force_refresh=force_refresh)
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


def preview_command(tool_name: str, params: dict) -> str:
    return TOOLS()[tool_name]["template"].format(**params)


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
    return jev_decide({"user_request": user_msg}, questions, provider)


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
    risky = noul >= 0.5 or tool in RISKY_TOOLS()
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
    for name, meta in TOOLS().items():
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

        meta = TOOLS()[tool]

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
