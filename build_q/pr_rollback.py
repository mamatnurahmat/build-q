"""`bq --pr-rollback <commit_url>` — rollback satu commit GitOps lewat Pull Request.

Cakupan Fase 1 (PRD-pr-rollback.md): hanya file **compose** (`web/<env>/<app>/docker-compose.yaml`)
dan **deploy yaml** (`cce|k8s/<ns>/<app>_deployment.yaml`) yang dimodifikasi. Hasil akhirnya
SELALU Pull Request — tidak pernah push ke branch base.

Alur: parse → preflight → resolve_commit → scope → base_check → dedup → clone →
later_commits → revert → verify → image_check → argocd_override → secret_scan →
confirm → commit → push → open_pr → post_merge_hint. Setiap langkah dicatat ke
`~/.build-q/pr-rollback-runs/<run_id>.jsonl` (+ PocketBase best-effort).

Exit code:
  0 = PR dibuat, atau tidak ada yang perlu dilakukan (PR sudah ada / sudah di-rollback / dry-run)
  1 = validasi gagal (di luar allowlist, bukan di base, konflik, image tidak siap, ...)
  2 = error git / GitHub API
  3 = dibatalkan user (atau non-interaktif tanpa --yes)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import github_api, secret_scan
from .pr_fix_trace import PrFixTrace, STEP_STATUSES

EXIT_OK, EXIT_INVALID, EXIT_GIT, EXIT_CANCELLED = 0, 1, 2, 3

DEFAULT_ALLOW: Dict[str, Tuple[str, ...]] = {
    "compose": (
        "web/*/*/docker-compose.yaml", "web/*/*/docker-compose.yml",
        "web/*/*/compose.yaml", "web/*/*/compose.yml",
    ),
    "deploy": ("cce/*/*_deployment.yaml", "k8s/*/*_deployment.yaml"),
}

RUN_STATUSES = ["running", "ok", "no_change", "dry_run", "failed", "cancelled"]
RUNS_COLLECTION = "pr_rollback_runs"
STEPS_COLLECTION = "pr_rollback_steps"

# Schema PocketBase untuk jejak --pr-rollback (dibuat sekali oleh superuser lewat
# `ensure_trace_collections()`); rules kosong = hanya superuser, sama seperti pr_fix_*.
PR_ROLLBACK_COLLECTIONS: Dict[str, Dict[str, Any]] = {
    RUNS_COLLECTION: {
        "name": RUNS_COLLECTION,
        "type": "base",
        "listRule": None, "viewRule": None, "createRule": None, "updateRule": None, "deleteRule": None,
        "fields": [
            {"name": "run_id", "type": "text", "required": True},
            {"name": "repo", "type": "text", "required": True},
            {"name": "ref", "type": "text", "required": True},
            {"name": "status", "type": "select", "values": RUN_STATUSES, "maxSelect": 1, "required": True},
            {"name": "base", "type": "text"},
            {"name": "branch", "type": "text"},
            {"name": "pr_url", "type": "url"},
            {"name": "requested_by", "type": "text"},
            {"name": "report", "type": "json"},
            {"name": "started_at", "type": "date"},
            {"name": "finished_at", "type": "date"},
        ],
        "indexes": [f"CREATE UNIQUE INDEX idx_{RUNS_COLLECTION}_run_id ON {RUNS_COLLECTION} (run_id)"],
    },
    STEPS_COLLECTION: {
        "name": STEPS_COLLECTION,
        "type": "base",
        "listRule": None, "viewRule": None, "createRule": None, "updateRule": None, "deleteRule": None,
        "fields": [
            {"name": "run_id", "type": "text", "required": True},
            {"name": "seq", "type": "number", "required": True},
            {"name": "step", "type": "text", "required": True},
            {"name": "status", "type": "select", "values": STEP_STATUSES, "maxSelect": 1, "required": True},
            {"name": "detail", "type": "json"},
            {"name": "at", "type": "date"},
        ],
        "indexes": [f"CREATE INDEX idx_{STEPS_COLLECTION}_run ON {STEPS_COLLECTION} (run_id)"],
    },
}


class RollbackTrace(PrFixTrace):
    LABEL = "pr-rollback"
    RUNS = RUNS_COLLECTION
    STEPS = STEPS_COLLECTION
    RUN_STATUSES = RUN_STATUSES
    LOCAL_SUBDIR = "pr-rollback-runs"


def ensure_trace_collections() -> Dict[str, str]:
    """Buat collection pr_rollback_runs/steps di PocketBase (butuh PB_API_USER/PASS superuser)."""
    from . import pb_api
    from .pr_fix_trace import ensure_collections, write_token

    base = pb_api._base_url().rstrip("/")
    return ensure_collections(base, write_token(base), PR_ROLLBACK_COLLECTIONS)


class RollbackError(Exception):
    def __init__(self, msg: str, code: int = EXIT_INVALID, *, status: str = "failed") -> None:
        super().__init__(msg)
        self.code = code
        self.status = status


# ── Parse target ───────────────────────────────────────────────────────────

_SHA = r"[0-9a-fA-F]{7,40}"
_COMMIT_URL_RE = re.compile(
    rf"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/commit/({_SHA})(?:[/?#].*)?$"
)
_PR_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/[\w.-]+/[\w.-]+/pull/\d+")
_REPO_AT_RE = re.compile(rf"^([\w.-]+)/([\w.-]+)@({_SHA})$")
_BARE_SHA_RE = re.compile(rf"^{_SHA}$")


def _strip_git(name: str) -> str:
    return name[:-4] if name.endswith(".git") else name


def parse_rollback_target(target: str, default_repo: str = "") -> Tuple[str, str]:
    """URL commit / `owner/repo@sha` / sha → `(owner/repo, sha lowercase)`."""
    t = (target or "").strip()
    m = _COMMIT_URL_RE.match(t)
    if m:
        return f"{m.group(1)}/{_strip_git(m.group(2))}", m.group(3).lower()
    if _PR_URL_RE.match(t):
        raise RollbackError(
            "URL PR belum didukung (Fase 2) — pakai URL commit, mis. merge commit PR tersebut."
        )
    m = _REPO_AT_RE.match(t)
    if m:
        return f"{m.group(1)}/{_strip_git(m.group(2))}", m.group(3).lower()
    if _BARE_SHA_RE.match(t):
        if not default_repo:
            raise RollbackError("SHA tanpa repo — set GITOPS_REPO atau pakai format owner/repo@sha.")
        return default_repo, t.lower()
    raise RollbackError(
        f"Target tidak dikenali: {target!r}. Contoh: https://github.com/<owner>/<repo>/commit/<sha>"
    )


# ── Allowlist & penamaan ───────────────────────────────────────────────────

def allow_patterns() -> Dict[str, Tuple[str, ...]]:
    """Pola allowlist; override per kind via env PR_ROLLBACK_ALLOW_COMPOSE / _DEPLOY (dipisah koma)."""
    out: Dict[str, Tuple[str, ...]] = {}
    for kind, default in DEFAULT_ALLOW.items():
        env = os.getenv(f"PR_ROLLBACK_ALLOW_{kind.upper()}", "").strip()
        out[kind] = tuple(p.strip() for p in env.split(",") if p.strip()) if env else default
    return out


def _glob_match(path: str, pattern: str) -> bool:
    """Glob per segmen path (`*` tidak melewati `/`), harus cocok penuh."""
    ps, gs = path.split("/"), pattern.split("/")
    return len(ps) == len(gs) and all(fnmatchcase(a, b) for a, b in zip(ps, gs, strict=True))


def file_kind(path: str, patterns: Optional[Dict[str, Tuple[str, ...]]] = None) -> Optional[str]:
    for kind, pats in (patterns or allow_patterns()).items():
        if any(_glob_match(path, p) for p in pats):
            return kind
    return None


def classify_files(
    files: List[Dict[str, Any]], patterns: Optional[Dict[str, Tuple[str, ...]]] = None,
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """→ (diizinkan [(path, kind)], ditolak [(path, alasan)])."""
    allowed: List[Tuple[str, str]] = []
    rejected: List[Tuple[str, str]] = []
    for f in files:
        path, status = f.get("filename", ""), f.get("status", "")
        kind = file_kind(path, patterns)
        if kind is None:
            rejected.append((path, "di luar allowlist compose / deploy yaml"))
        elif status != "modified":
            rejected.append((path, f"status '{status}' — Fase 1 hanya untuk file yang dimodifikasi"))
        else:
            allowed.append((path, kind))
    return allowed, rejected


def app_name(path: str, kind: str) -> str:
    parts = path.split("/")
    if kind == "compose" and len(parts) >= 3:
        return parts[2]
    name = parts[-1]
    for suffix in ("_deployment.yaml", "_deployment.yml"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def rollback_slug(items: List[Tuple[str, str]]) -> str:
    apps = list(dict.fromkeys(app_name(p, k) for p, k in items))
    slug = apps[0] if len(apps) == 1 else f"{apps[0]}-plus{len(apps) - 1}"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", slug).strip("-") or "gitops"


def commit_scope(paths: List[str]) -> str:
    scopes = list(dict.fromkeys("/".join(p.split("/")[:2]) for p in paths))
    return scopes[0] if len(scopes) == 1 else "gitops"


# ── Image ──────────────────────────────────────────────────────────────────

_IMAGE_RE = re.compile(r"""^[ \t]*(?:-[ \t]+)?image:[ \t]*["']?([^\s"'#]+)["']?[ \t]*(?:#.*)?$""", re.M)


def extract_images(text: str) -> List[str]:
    """Semua nilai `image:` (unik, urut kemunculan) — compose bisa punya banyak service."""
    return list(dict.fromkeys(_IMAGE_RE.findall(text)))


def _image_name(image: str) -> str:
    """Nama image tanpa tag/digest (port registry dipertahankan)."""
    name = image.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    return name.rsplit(":", 1)[0] if ":" in last else name


def check_image(image: str, config: Dict[str, Any]) -> Tuple[Optional[bool], str]:
    """(True/False/None, catatan). None = tidak bisa dicek (bukan Docker Hub / digest)."""
    if "@" in image:
        return None, "image ber-digest — tidak dicek"
    first = image.split("/", 1)[0]
    ref = image
    if "/" in image and ("." in first or ":" in first or first == "localhost"):
        if first not in ("docker.io", "index.docker.io", "registry-1.docker.io"):
            return None, f"registry {first} bukan Docker Hub — tidak dicek"
        ref = image.split("/", 1)[1]
    if ":" not in ref.rsplit("/", 1)[-1]:
        ref = f"{ref}:latest"
    from .gitops_set_image import _image_is_ready

    dh = config.get("dockerhub") or {}
    ready = _image_is_ready(
        ref,
        dockerhub_user=dh.get("user") or os.getenv("DOCKERHUB_USER"),
        dockerhub_password=dh.get("token") or os.getenv("DOCKERHUB_PASSWORD"),
    )
    return ready, "ada di Docker Hub" if ready else "TIDAK ada di Docker Hub"


def _override_names(app: Dict[str, Any]) -> List[str]:
    spec = app.get("spec") or {}
    out: List[str] = []
    for src in spec.get("sources") or [spec.get("source") or {}]:
        for entry in ((src.get("kustomize") or {}).get("images") or []):
            out.append(str(entry))
    return out


# ── Laporan ────────────────────────────────────────────────────────────────

@dataclass
class RollbackReport:
    target: str
    repo: str = ""
    sha: str = ""
    subject: str = ""
    base: str = ""
    branch: str = ""
    merge_commit: bool = False
    status: str = "running"
    reason: str = ""
    files: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, str]] = field(default_factory=list)
    later_commits: List[str] = field(default_factory=list)
    identical_to_parent: Optional[bool] = None
    images: List[Dict[str, Any]] = field(default_factory=list)
    argocd: List[Dict[str, Any]] = field(default_factory=list)
    secrets: List[Dict[str, Any]] = field(default_factory=list)
    diffstat: str = ""
    pr_url: str = ""
    hints: List[str] = field(default_factory=list)

    @property
    def sha7(self) -> str:
        return self.sha[:7]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_commit_message(rep: RollbackReport) -> str:
    paths = [f["path"] for f in rep.files]
    scope = commit_scope(paths)
    slug = rollback_slug([(f["path"], f["kind"]) for f in rep.files])
    restored = [i["image"] for i in rep.images]
    title = f"revert({scope}): {slug} → {restored[0]}" if restored else f"revert({scope}): {rep.subject}"[:100]
    lines = [title, "", f"This reverts commit {rep.sha}.", "", f"Original: {rep.subject}"]
    for f in rep.files:
        before, after = ", ".join(f["images_before"]) or "-", ", ".join(f["images_after"]) or "-"
        lines.append(f"- {f['path']}: {before} → {after}" if before != after else f"- {f['path']}")
    return "\n".join(lines) + "\n"


def build_pr_body(rep: RollbackReport) -> str:
    url = f"https://github.com/{rep.repo}/commit/{rep.sha}"
    out = [
        "## Rollback",
        f"Revert [`{rep.sha7}`]({url}) (`{rep.subject}`) — dibuat oleh `bq --pr-rollback`.",
        "",
        "| File | Kind | Image sekarang | Image setelah merge |",
        "|---|---|---|---|",
    ]
    for f in rep.files:
        before = "<br>".join(f"`{i}`" for i in f["images_before"]) or "-"
        after = "<br>".join(f"`{i}`" for i in f["images_after"]) or "-"
        out.append(f"| `{f['path']}` | {f['kind']} | {before} | {after} |")
    out.append("")
    if rep.identical_to_parent:
        out.append(f"- Verifikasi: isi file identik dengan `{rep.sha7}^` ✅")
    elif rep.later_commits:
        out.append(f"- ⚠️ Ada {len(rep.later_commits)} commit lanjutan pada file yang sama (dipertahankan):")
        out += [f"  - `{c}`" for c in rep.later_commits]
    for img in rep.images:
        mark = {True: "✅", False: "❌", None: "➖"}[img["ready"]]
        out.append(f"- Image `{img['image']}`: {img['note']} {mark}")
    if rep.merge_commit:
        out.append("- Commit asal adalah merge commit — di-revert terhadap parent pertama (`-m 1`).")
    out += ["", "## Setelah merge"] + [f"- {h}" for h in rep.hints]
    notes: List[str] = []
    if rep.secrets:
        notes.append("Secret plaintext terdeteksi pada hasil revert (nilai tidak ditampilkan) — "
                     "sebaiknya dipindah ke SOPS / `.env` host:")
        notes += [f"  - `{s['path']}` {secret_scan.describe(s)}" for s in rep.secrets]
    for a in rep.argocd:
        if a.get("overrides"):
            notes.append(f"ArgoCD app `{a['app']}` punya override kustomize image "
                         f"({', '.join(a['overrides'])}) — revert YAML tidak mengubah image live "
                         "sampai override dihapus (lihat gitops-audit).")
    if notes:
        out += ["", "## ⚠️ Catatan"] + [n if n.startswith("  ") else f"- {n}" for n in notes]
    out += ["", "🤖 Generated with `bq --pr-rollback`"]
    return "\n".join(out) + "\n"


# ── Utilitas git ───────────────────────────────────────────────────────────

_TOKEN_IN_URL_RE = re.compile(r"(x-access-token:)[^@\s]+(@)")


def _redact(text: str, token: str = "") -> str:
    text = _TOKEN_IN_URL_RE.sub(r"\1***\2", text or "")
    return text.replace(token, "***") if token else text


def _clone_url(repo: str, token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{repo}.git"


class _Git:
    def __init__(self, cwd: Path, token: str) -> None:
        self.cwd = cwd
        self.token = token
        self.env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def run(self, *args: str, check: bool = True, input: Optional[str] = None,
            cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
        p = subprocess.run(["git", *args], cwd=str(cwd or self.cwd), capture_output=True,
                           text=True, env=self.env, input=input)
        if check and p.returncode != 0:
            msg = _redact((p.stderr or p.stdout).strip(), self.token)
            raise RollbackError(f"git {args[0]} gagal: {msg}", EXIT_GIT)
        return p

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.strip()


_OUT = {"stream": None}  # None → sys.stdout; stderr saat --export-json - (stdout = JSON murni)


def _say(msg: str = "") -> None:
    print(msg, file=_OUT["stream"] or sys.stdout, flush=True)


def _warn(msg: str) -> None:
    _say(f"   ⚠️  {msg}")


def _api_error(e: github_api.GitHubAPIError, what: str) -> RollbackError:
    msg = str(e)
    code = EXIT_INVALID if re.search(r"HTTP (404|422)\b", msg) else EXIT_GIT
    return RollbackError(f"{what}: {msg[:300]}", code)


def _requested_by() -> str:
    who = os.getenv("BQ_REQUESTED_BY", "").strip()
    if who:
        return who
    try:
        from . import pb_api
        return (((pb_api.read_auth() or {}).get("record") or {}).get("email")) or ""
    except Exception:
        return ""


# ── Main ───────────────────────────────────────────────────────────────────

def run_pr_rollback(
    target: str,
    *,
    base: Optional[str] = None,
    pr_branch: Optional[str] = None,
    dry_run: bool = False,
    keep_workdir: bool = False,
    assume_yes: bool = False,
    skip_image_check: bool = False,
    export_json: Optional[str] = None,
    run_id: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> int:
    if config is None:
        from .config import load_config
        config = load_config()
    rep = RollbackReport(target=target)
    _OUT["stream"] = sys.stderr if export_json == "-" else None

    _say("")
    _say("╔══════════════════════════════════════════════════════════════╗")
    _say("║ bq --pr-rollback: rollback commit GitOps lewat Pull Request  ║")
    _say("╚══════════════════════════════════════════════════════════════╝")

    try:
        rep.repo, rep.sha = parse_rollback_target(target, (config.get("gitops") or {}).get("repo", ""))
    except RollbackError as e:
        print(f"❌ {e}", file=sys.stderr)
        rep.status, rep.reason = "failed", str(e)
        _export(rep, export_json)
        return e.code

    run_id = run_id or f"rollback-{rep.sha[:7]}-{time.strftime('%Y%m%d-%H%M%S')}"
    trace = RollbackTrace(rep.repo, rep.sha, run_id)
    trace.set(requested_by=_requested_by())
    trace.step("parse", "ok", repo=rep.repo, sha=rep.sha, target=target)
    _say(f" Target : {rep.repo}@{rep.sha[:12]}")
    _say(f" Jejak  : {trace.local_path}")

    code = EXIT_OK
    try:
        code = _run(rep, trace, config, base=base, pr_branch=pr_branch, dry_run=dry_run,
                    assume_yes=assume_yes, skip_image_check=skip_image_check)
    except RollbackError as e:
        code = e.code
        rep.status, rep.reason = e.status, str(e)
        print(f"\n❌ {e}", file=sys.stderr)
    except Exception as e:  # jangan biarkan trace menggantung
        code = EXIT_GIT
        rep.status, rep.reason = "failed", f"{type(e).__name__}: {e}"[:500]
        trace.step("unhandled", "fail", error=rep.reason)
        print(f"\n❌ Error tak terduga: {rep.reason}", file=sys.stderr)
    finally:
        if rep.status == "running":
            rep.status = "ok"
        trace.set(base=rep.base, branch=rep.branch, pr_url=rep.pr_url, report=rep.to_dict())
        trace.finish(rep.status)
        _export(rep, export_json)

    wd = _WORKDIRS.pop(run_id, None)
    if wd is not None:
        if keep_workdir or rep.status == "dry_run":
            _say(f"\n📂 Workdir dipertahankan: {wd}")
        else:
            shutil.rmtree(wd, ignore_errors=True)
    return code


_WORKDIRS: Dict[str, Path] = {}


def _export(rep: RollbackReport, path: Optional[str]) -> None:
    if not path:
        return
    data = json.dumps(rep.to_dict(), indent=2, ensure_ascii=False)
    if path == "-":
        print(data)
        return
    Path(path).write_text(data + "\n", encoding="utf-8")
    _say(f"🧾 Laporan JSON: {path}")


def _fail(trace: RollbackTrace, step: str, msg: str, code: int = EXIT_INVALID, **detail: Any) -> RollbackError:
    trace.step(step, "fail", error=msg[:500], **detail)
    return RollbackError(msg, code)


def _run(
    rep: RollbackReport,
    trace: RollbackTrace,
    config: Dict[str, Any],
    *,
    base: Optional[str],
    pr_branch: Optional[str],
    dry_run: bool,
    assume_yes: bool,
    skip_image_check: bool,
) -> int:
    # ── 1. preflight ───────────────────────────────────────────────
    try:
        token = github_api.get_auth_token()
    except github_api.GitHubAPIError as e:
        raise _fail(trace, "preflight", f"GITHUB_TOKEN tidak tersedia: {e}") from e
    if not shutil.which("git"):
        raise _fail(trace, "preflight", "git tidak ditemukan di PATH", EXIT_GIT)
    trace.step("preflight", "ok")

    # ── 2. resolve commit ──────────────────────────────────────────
    _say("\nℹ️  Resolve commit ...")
    try:
        commit = github_api.get_commit(rep.repo, rep.sha)
    except github_api.GitHubAPIError as e:
        err = _api_error(e, f"Commit {rep.sha} tidak bisa dibaca di {rep.repo}")
        raise _fail(trace, "resolve_commit", str(err), err.code) from e
    rep.sha = commit.get("sha") or rep.sha
    rep.subject = ((commit.get("commit") or {}).get("message") or "").splitlines()[0] if commit.get("commit") else ""
    parents = [p.get("sha") for p in commit.get("parents") or []]
    if not parents:
        raise _fail(trace, "resolve_commit", "Commit root (tanpa parent) tidak bisa di-rollback.")
    rep.merge_commit = len(parents) > 1
    files = commit.get("files") or []
    if rep.merge_commit:
        try:
            files = github_api.compare(rep.repo, parents[0], rep.sha).get("files") or []
        except github_api.GitHubAPIError as e:
            err = _api_error(e, "Gagal membaca diff merge commit")
            raise _fail(trace, "resolve_commit", str(err), err.code) from e
    if len(files) >= 300:
        raise _fail(trace, "resolve_commit", f"Commit mengubah {len(files)} file — terlalu besar untuk rollback otomatis.")
    trace.step("resolve_commit", "ok", sha=rep.sha, subject=rep.subject, merge=rep.merge_commit, files=len(files))
    _say(f"   {rep.sha[:12]} {rep.subject}")
    if rep.merge_commit:
        _say("   ↪ merge commit — revert terhadap parent pertama (-m 1)")

    # ── 3. scope ───────────────────────────────────────────────────
    allowed, rejected = classify_files(files)
    rep.rejected = [{"path": p, "reason": r} for p, r in rejected]
    if rejected or not allowed:
        msg = "Commit di luar cakupan --pr-rollback (hanya compose / deploy yaml yang dimodifikasi):"
        detail = "\n".join(f"   - {p}: {r}" for p, r in rejected) or "   - commit tidak mengubah file"
        raise _fail(trace, "scope", f"{msg}\n{detail}", rejected=rep.rejected)
    rep.files = [{"path": p, "kind": k, "app": app_name(p, k), "images_before": [], "images_after": []}
                 for p, k in allowed]
    trace.step("scope", "ok", files=[f"{k}:{p}" for p, k in allowed])
    for p, k in allowed:
        _say(f"   ✅ {k:8} {p}")

    # ── 4. base ────────────────────────────────────────────────────
    rep.base = base or (config.get("gitops") or {}).get("branch") or "main"
    try:
        status = github_api.compare(rep.repo, rep.base, rep.sha).get("status")
    except github_api.GitHubAPIError as e:
        err = _api_error(e, f"Gagal membandingkan {rep.base}...{rep.sha[:7]}")
        raise _fail(trace, "base_check", str(err), err.code) from e
    if status not in ("behind", "identical"):
        raise _fail(trace, "base_check",
                    f"Commit {rep.sha7} belum ada di branch '{rep.base}' (status: {status}). "
                    "Pakai --gitops-branch <branch> yang memuat commit ini.", status=status)
    trace.step("base_check", "ok", base=rep.base, status=status)

    # ── 5a. dedup PR terbuka ───────────────────────────────────────
    rep.branch = pr_branch or f"revert/{rollback_slug(allowed)}-{rep.sha7}"
    if rep.branch == rep.base:
        raise _fail(trace, "dedup", f"Branch revert tidak boleh sama dengan base ({rep.base}).")
    try:
        existing = github_api.list_open_prs(rep.repo, rep.branch)
    except github_api.GitHubAPIError as e:
        existing = []
        _warn(f"cek PR terbuka gagal ({str(e)[:120]}) — lanjut")
    if existing:
        rep.pr_url = existing[0].get("html_url", "")
        rep.status, rep.reason = "no_change", "PR rollback untuk commit ini sudah terbuka"
        trace.step("dedup", "skip", reason=rep.reason, url=rep.pr_url)
        _say(f"\nℹ️  {rep.reason}: {rep.pr_url}")
        return EXIT_OK

    # ── 6. clone (partial, histori lengkap) ────────────────────────
    workdir = Path(tempfile.mkdtemp(
        prefix=f"bq-pr-rollback-{rep.sha7}-",
        dir=(config.get("pr_fix") or {}).get("base_workdir") or None,
    ))
    _WORKDIRS[trace.run_id] = workdir
    repo_dir = workdir / "repo"
    git = _Git(repo_dir, token)
    _say(f"\n📥 Clone {rep.repo}@{rep.base} (partial) ...")
    git.run("clone", "--quiet", "--filter=blob:none", "--single-branch", "--branch", rep.base,
            _clone_url(rep.repo, token), str(repo_dir), cwd=workdir)
    if git.run("cat-file", "-e", f"{rep.sha}^{{commit}}", check=False).returncode != 0:
        raise _fail(trace, "clone", f"Commit {rep.sha7} tidak ada di hasil clone {rep.base}.", EXIT_GIT)
    head = git.out("rev-parse", "--short=12", "HEAD")
    trace.step("clone", "ok", head=head)

    # ── 5b. dedup sudah di-revert ──────────────────────────────────
    reverted = git.out("log", "-1", "--format=%h %s", "-F", f"--grep=This reverts commit {rep.sha}", "HEAD")
    if reverted:
        rep.status, rep.reason = "no_change", f"sudah di-rollback di {rep.base} oleh {reverted}"
        trace.step("dedup", "skip", reason=rep.reason)
        _say(f"\nℹ️  Commit ini {rep.reason}")
        return EXIT_OK
    trace.step("dedup", "ok")

    # ── 7. commit lanjutan ─────────────────────────────────────────
    paths = [f["path"] for f in rep.files]
    later = git.out("log", "--format=%h %s", f"{rep.sha}..HEAD", "--", *paths)
    rep.later_commits = [ln for ln in later.splitlines() if ln.strip()]
    if rep.later_commits:
        _warn(f"{len(rep.later_commits)} commit lanjutan mengubah file yang sama:")
        for c in rep.later_commits:
            _say(f"      {c}")
    trace.step("later_commits", "info" if rep.later_commits else "ok", commits=rep.later_commits)

    # ── 8. branch + revert ─────────────────────────────────────────
    git.run("checkout", "-q", "-b", rep.branch)
    revert = ["revert", "--no-commit"] + (["-m", "1"] if rep.merge_commit else []) + [rep.sha]
    p = git.run(*revert, check=False)
    if p.returncode != 0:
        git.run("revert", "--abort", check=False)
        msg = (f"Revert {rep.sha7} konflik dengan perubahan berikutnya — rollback otomatis dibatalkan.\n"
               + "\n".join(f"   - {c}" for c in rep.later_commits))
        raise _fail(trace, "revert", msg, conflict=_redact(p.stderr.strip())[:500])
    trace.step("revert", "ok", branch=rep.branch)

    # ── 9. verifikasi ──────────────────────────────────────────────
    if git.run("diff", "--cached", "--quiet", check=False).returncode == 0:
        rep.status, rep.reason = "no_change", "revert tidak menghasilkan perubahan (isi sudah sama)"
        trace.step("verify", "skip", reason=rep.reason)
        _say(f"\nℹ️  {rep.reason}")
        return EXIT_OK
    if not rep.later_commits:
        same = git.run("diff", "--cached", "--quiet", f"{rep.sha}^1", "--", *paths, check=False).returncode == 0
        rep.identical_to_parent = same
        if not same:
            raise _fail(trace, "verify", f"Verifikasi gagal: hasil revert berbeda dari {rep.sha7}^.")
    rep.diffstat = git.out("diff", "--cached", "--stat")
    trace.step("verify", "ok", identical_to_parent=rep.identical_to_parent)

    texts: Dict[str, str] = {}
    for f in rep.files:
        before = git.run("show", f"HEAD:{f['path']}").stdout
        after = (repo_dir / f["path"]).read_text(encoding="utf-8")
        texts[f["path"]] = after
        f["images_before"], f["images_after"] = extract_images(before), extract_images(after)

    # ── 10. image hasil revert harus ada ───────────────────────────
    restored = list(dict.fromkeys(i for f in rep.files for i in f["images_after"] if i not in f["images_before"]))
    missing: List[str] = []
    for image in restored:
        ready, note = check_image(image, config)
        rep.images.append({"image": image, "ready": ready, "note": note})
        if ready is False:
            missing.append(image)
    if missing and not skip_image_check:
        raise _fail(trace, "image_check",
                    "Image hasil rollback tidak ada di registry — rollback akan gagal start:\n"
                    + "\n".join(f"   - {i}" for i in missing)
                    + "\n   (darurat: --skip-image-check)", images=rep.images)
    trace.step("image_check", "info" if missing else "ok", images=rep.images, skipped=bool(missing))
    if missing:
        _warn("--skip-image-check: image TIDAK ada di registry, tetap dilanjutkan: " + ", ".join(missing))

    # ── 11. override ArgoCD (deploy saja, read-only, soft-fail) ────
    deploy_files = [f for f in rep.files if f["kind"] == "deploy"]
    if deploy_files:
        try:
            from .e2e import argocd_api_apps, find_argocd_app

            apps = argocd_api_apps(config)
            for f in deploy_files:
                folder = f["path"].rsplit("/", 1)[0]
                app = find_argocd_app(apps, rep.repo, folder, rep.base)
                if not app:
                    rep.argocd.append({"path": f["path"], "app": None, "overrides": []})
                    continue
                names = {_image_name(i) for i in f["images_after"]}
                hits = [o for o in _override_names(app) if _image_name(o.split("=", 1)[0]) in names]
                rep.argocd.append({"path": f["path"], "app": (app.get("metadata") or {}).get("name"),
                                   "overrides": hits})
            trace.step("argocd_override", "info" if any(a["overrides"] for a in rep.argocd) else "ok",
                       apps=rep.argocd)
        except Exception as e:
            trace.step("argocd_override", "skip", reason=f"{type(e).__name__}: {e}"[:300])
            _warn(f"cek override ArgoCD dilewati: {str(e)[:120]}")

    # ── 12. scan secret (tanpa nilai) ──────────────────────────────
    for path, text in texts.items():
        for hit in secret_scan.scan(text):
            rep.secrets.append({"path": path, **hit})
    trace.step("secret_scan", "info" if rep.secrets else "ok",
               hits=[{"path": s["path"], "line": s["line"], "key": s["key"]} for s in rep.secrets])

    # ── hints setelah merge ────────────────────────────────────────
    for f in rep.files:
        if f["kind"] == "compose":
            rep.hints.append(f"compose `{f['path']}`: redeploy di host aplikasi `{f['app']}` "
                             "(docker compose pull && docker compose up -d) — tidak lewat ArgoCD.")
        else:
            app = next((a["app"] for a in rep.argocd if a["path"] == f["path"] and a.get("app")), None)
            folder = f["path"].rsplit("/", 1)[0]
            rep.hints.append(f"deploy `{f['path']}`: ArgoCD sync folder `{folder}`"
                             + (f" (app `{app}`, cek: `bq argocd app get {app}`)." if app else "."))

    # ── 13. pratinjau + konfirmasi ─────────────────────────────────
    _preview(rep)
    if dry_run:
        rep.status, rep.reason = "dry_run", "pratinjau saja (--dry-run)"
        trace.step("confirm", "skip", reason="dry_run")
        _say("\n🔍 Dry-run: berhenti sebelum commit/push/PR.")
        return EXIT_OK
    if not assume_yes:
        if not sys.stdin.isatty():
            trace.step("confirm", "skip", reason="non-interaktif tanpa --yes")
            raise RollbackError("Non-interaktif tanpa --yes — dibatalkan (tidak ada yang di-push).",
                                EXIT_CANCELLED, status="cancelled")
        answer = input("\nLanjut commit + push + buat PR? [y/N] ").strip().lower()
        if answer not in ("y", "ya", "yes"):
            trace.step("confirm", "skip", reason="dibatalkan user")
            raise RollbackError("Dibatalkan — tidak ada yang di-push.", EXIT_CANCELLED, status="cancelled")
    trace.step("confirm", "ok", assume_yes=assume_yes)

    # ── 14. commit ─────────────────────────────────────────────────
    ident: List[str] = []
    if not git.run("config", "user.email", check=False).stdout.strip():
        ident = ["-c", "user.name=build-q bot", "-c", "user.email=bq@build-q.local"]
    git.run(*ident, "commit", "-q", "-F", "-", input=build_commit_message(rep))
    commit_sha = git.out("rev-parse", "HEAD")
    trace.step("commit", "ok", sha=commit_sha[:12])

    # ── 15. push (tidak pernah ke base) ────────────────────────────
    if rep.branch == rep.base:
        raise _fail(trace, "push", "Guard: menolak push ke branch base.")
    _say(f"\n⬆️  Push {rep.branch} ...")
    p = git.run("push", "-q", "origin", f"HEAD:refs/heads/{rep.branch}", check=False)
    if p.returncode != 0:
        msg = _redact(p.stderr.strip(), token)
        raise _fail(trace, "push", f"Push gagal: {msg[:300]}\n   (branch sudah ada di remote? pakai --pr-branch <nama lain>)",
                    EXIT_GIT)
    trace.step("push", "ok", branch=rep.branch)

    # ── 16. PR ─────────────────────────────────────────────────────
    title = build_commit_message(rep).splitlines()[0]
    try:
        pr = github_api.create_pull_request(rep.repo, base=rep.base, head=rep.branch,
                                            title=title, body=build_pr_body(rep))
    except github_api.GitHubAPIError as e:
        raise _fail(trace, "open_pr", f"Gagal membuat PR: {str(e)[:300]}", EXIT_GIT) from e
    rep.pr_url = pr.get("html_url", "")
    trace.step("open_pr", "ok", url=rep.pr_url)
    rep.status = "ok"
    _say(f"\n✅ PR rollback: {rep.pr_url}")
    _say("\nSetelah merge:")
    for h in rep.hints:
        _say(f"   - {h}")
    trace.step("post_merge_hint", "info", hints=rep.hints)
    return EXIT_OK


def _preview(rep: RollbackReport) -> None:
    _say("\n── Pratinjau rollback ─────────────────────────────────────────")
    _say(f" Commit : {rep.sha7} {rep.subject}")
    _say(f" PR     : {rep.branch} → {rep.base}")
    for f in rep.files:
        _say(f" • {f['path']} ({f['kind']})")
        before, after = f["images_before"], f["images_after"]
        if before != after:
            _say(f"     image: {', '.join(before) or '-'}  →  {', '.join(after) or '-'}")
    if rep.diffstat:
        _say("\n" + "\n".join(f"   {ln}" for ln in rep.diffstat.splitlines()))
    if rep.identical_to_parent:
        _say(f"\n ✅ Isi file identik dengan {rep.sha7}^ (versi sebelum commit)")
    for img in rep.images:
        mark = {True: "✅", False: "❌", None: "➖"}[img["ready"]]
        _say(f" {mark} {img['image']} — {img['note']}")
    for a in rep.argocd:
        if a.get("overrides"):
            _warn(f"ArgoCD `{a['app']}` override kustomize image: {', '.join(a['overrides'])} "
                  "— revert YAML tidak mengubah image live.")
    if rep.secrets:
        _warn("Secret plaintext pada hasil revert (nilai tidak ditampilkan):")
        for s in rep.secrets:
            _say(f"      {s['path']} {secret_scan.describe(s)}")
