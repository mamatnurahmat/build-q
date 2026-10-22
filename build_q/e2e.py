"""Analisa end-to-end CI/CD satu commit: trigger → pipeline → GitOps → ArgoCD.

Alur Qoin yang ditelusuri (read-only, tidak mengubah apa pun):

  1. Source   — ref → SHA, ENV & tag image (aturan sama dgn Tekton, lihat
                `builder._env_from_ref`), cicd.json.
  2. Trigger  — GitHub Actions `trigger-ci.yml` untuk SHA tsb
                (POST cicd-hw.qoin.id/trigger) + status webhook `/hook`.
  3. Pipeline — PipelineRun Tekton `webhook-<repo>-*` di JX_KUBE_CONTEXT
                (label repo/ref, anotasi commit_sha) + results TaskRun
                (build-mode, image-tag, gitops-file, gitops-result, gitops-url).
  4. GitOps   — tag image di YAML GitOps pada branch ENV (develop/staging/
                sandbox → push langsung; tag v* → PR ke main).
  5. Manifest — Deployment ↔ Service ↔ Secret di folder GitOps: selector
                cocok label pod, targetPort ada, Secret yang dirujuk sudah ada
                di namespace (context dari mapping `kube_cx`). Rule standar
                dari PB `build_q_anomaly_rules`; bila belum sesuai → saran YAML.
                Opsional: Jev menilai kesiapan deploy & prioritas perbaikan.
  6. ArgoCD   — Application yang memantau folder file GitOps di branch tsb:
                sync/health + image live (`status.summary.images`). Dibaca
                via kubectl; bila cluster tak terjangkau, fallback ke REST
                API ArgoCD (ARGOCD_API_URL + token SSO).

Hasil: `E2EReport` berisi status per tahap dan *titik putus* (tahap pertama
yang gagal) beserta saran tindak lanjut.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from . import github_api
from .audit import redact

OK, WARN, FAIL, SKIP, RUNNING = "ok", "warn", "fail", "skip", "running"
STATUS_ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌", SKIP: "⏭️ ", RUNNING: "⏳"}

TRIGGER_WORKFLOW = ".github/workflows/trigger-ci.yml"
HOOK_URL = "https://cicd-hw.qoin.id/hook"
DIRECT_PUSH_ENVS = ("develop", "staging", "sandbox")
_KUBECTL_TIMEOUT = 25


@dataclass
class Stage:
    key: str
    title: str
    status: str = SKIP
    summary: str = ""
    details: List[str] = field(default_factory=list)
    hints: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)
    applicable: bool = True  # False = SKIP karena memang tidak relevan (mis. FE/Compose)


@dataclass
class E2EReport:
    repo: str
    ref: str
    sha: str = ""
    env: str = ""
    expected_tag: str = ""
    image: str = ""
    stages: List[Stage] = field(default_factory=list)

    @property
    def break_point(self) -> Optional[Stage]:
        """Tahap pertama yang FAIL; bila tidak ada, tahap pertama yang WARN/RUNNING."""
        for wanted in ((FAIL,), (WARN, RUNNING)):
            for st in self.stages:
                if st.status in wanted:
                    return st
        return None

    @property
    def unchecked(self) -> List[Stage]:
        """Tahap relevan yang tidak bisa diperiksa (akses/cluster tidak terjangkau)."""
        return [s for s in self.stages if s.status == SKIP and s.applicable]

    @property
    def verdict(self) -> str:
        """broken | incomplete | healthy."""
        if self.break_point:
            return "broken"
        return "incomplete" if self.unchecked else "healthy"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        bp = self.break_point
        d["break_point"] = bp.key if bp else None
        d["verdict"] = self.verdict
        d["unchecked"] = [s.key for s in self.unchecked]
        return d


# ─── IO helpers (di-mock di test) ─────────────────────────────────────────

class KubectlError(RuntimeError):
    pass


# KUBECONFIG per-konteks — dipakai `bq --serve` (kubeconfig milik user dari
# PocketBase). None = kubeconfig lokal (CLI); "" = user tidak punya kubeconfig.
_KUBECONFIG: ContextVar[Optional[str]] = ContextVar("e2e_kubeconfig", default=None)


@contextmanager
def use_credentials(*, github_token: str, kubeconfig: str) -> Iterator[None]:
    """Jalankan analisa hanya dengan credential ini — tanpa fallback env/file lokal."""
    reset = _KUBECONFIG.set(kubeconfig or "")
    try:
        with github_api.use_token(github_token):
            yield
    finally:
        _KUBECONFIG.reset(reset)


def _kubectl_env() -> Optional[Dict[str, str]]:
    kubeconfig = _KUBECONFIG.get()
    if kubeconfig is None:
        return None
    if not kubeconfig:
        raise KubectlError("kubeconfig tidak ada di credential PocketBase Anda (KUBECONFIG_*_B64)")
    # Env minimal: jangan wariskan credential proses server ke kubectl.
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
    return {**{k: os.environ[k] for k in keep if k in os.environ}, "KUBECONFIG": kubeconfig}


def kubectl_json(context: str, args: List[str]) -> dict:
    cmd = ["kubectl"] + (["--context", context] if context else []) + args + ["-o", "json"]
    env = _kubectl_env()
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=_KUBECTL_TIMEOUT, env=env)
    except FileNotFoundError as e:
        raise KubectlError("kubectl tidak ditemukan di PATH") from e
    except subprocess.TimeoutExpired as e:
        raise KubectlError(f"kubectl timeout ({_KUBECTL_TIMEOUT}s) — context {context}") from e
    if res.returncode != 0:
        raise KubectlError(res.stderr.strip().splitlines()[-1] if res.stderr.strip() else "kubectl gagal")
    return json.loads(res.stdout or "{}")


def kubectl_names(context: str, ns: str, kind: str) -> List[str]:
    """`kubectl get <kind> -o name` — hanya nama (isi Secret tidak pernah dibaca)."""
    cmd = ["kubectl", "--context", context, "-n", ns, "get", kind, "-o", "name",
           f"--request-timeout={_KUBECTL_TIMEOUT}s"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_KUBECTL_TIMEOUT + 5,
                           env=_kubectl_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise KubectlError(f"kubectl {context}: {e}") from e
    if r.returncode != 0:
        raise KubectlError(f"kubectl {context}: {r.stderr.strip()[:200]}")
    return [ln.split("/", 1)[-1] for ln in r.stdout.split()]


def gh_list_dir(repo: str, path: str, ref: str) -> List[str]:
    """Nama file di folder repo (GitHub contents API); [] bila folder tidak ada."""
    try:
        items = gh_get(f"/repos/{repo}/contents/{quote(path)}?ref={quote(ref)}") or []
    except github_api.GitHubAPIError:
        return []
    return [i.get("name", "") for i in items if isinstance(i, dict)]


def kubectl_logs(context: str, ns: str, pod: str, container: str, tail: int = 15) -> str:
    cmd = ["kubectl", "--context", context, "-n", ns, "logs", pod, "-c", container, f"--tail={tail}"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=_KUBECTL_TIMEOUT,
                             env=_kubectl_env())
    except (FileNotFoundError, subprocess.TimeoutExpired, KubectlError):
        return ""
    return redact(res.stdout) if res.returncode == 0 else ""


class ArgocdApiError(RuntimeError):
    def __init__(self, msg: str, *, auth: bool = False):
        super().__init__(msg)
        self.auth = auth  # token tidak ada / kedaluwarsa → bisa login ulang


def _argocd_token(config: dict) -> str:
    acfg = config.get("argocd") or {}
    if acfg.get("token"):
        return acfg["token"]
    auth_file = os.path.expanduser(acfg.get("auth_file") or "")
    if not auth_file or not os.path.isfile(auth_file):
        return ""
    try:
        with open(auth_file, encoding="utf-8") as f:
            return str(json.load(f).get("ARGOCD_TOKEN") or "")
    except (OSError, ValueError):
        return ""


def argocd_api_apps(config: dict) -> List[dict]:
    """GET /api/v1/applications — bentuk item sama dgn Application k8s."""
    acfg = config.get("argocd") or {}
    base = (acfg.get("api_url") or "").rstrip("/")
    if not base:
        raise ArgocdApiError("ARGOCD_API_URL kosong")
    token = _argocd_token(config)
    if not token:
        raise ArgocdApiError(f"token ArgoCD tidak ada (ARGOCD_TOKEN / {acfg.get('auth_file')})", auth=True)
    req = Request(f"{base}/api/v1/applications",
                  headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=_KUBECTL_TIMEOUT) as res:
            return (json.loads(res.read() or b"{}") or {}).get("items") or []
    except HTTPError as e:
        if e.code in (401, 403):
            raise ArgocdApiError(f"HTTP {e.code} — token SSO ArgoCD tidak valid/kedaluwarsa", auth=True) from e
        raise ArgocdApiError(f"HTTP {e.code} {e.reason}") from e
    except (URLError, TimeoutError, OSError) as e:
        raise ArgocdApiError(f"tidak bisa menghubungi {base}: {getattr(e, 'reason', e)}") from e
    except ValueError as e:
        raise ArgocdApiError("respons ArgoCD API bukan JSON") from e


def argocd_token_login(config: dict) -> bool:
    """Jalankan `argocd-app token-login` (SSO via browser) — hanya di terminal
    interaktif & ARGOCD_AUTO_LOGIN aktif. Return True bila login sukses."""
    import shutil
    import sys

    if not (config.get("argocd") or {}).get("auto_login", True) or not sys.stdin.isatty():
        return False
    exe = shutil.which("argocd-app")
    if not exe:
        return False
    print("🔐 Token ArgoCD kedaluwarsa — menjalankan `argocd-app token-login` ...", file=sys.stderr)
    try:
        # Output diteruskan ke terminal: user perlu melihat URL login SSO.
        return subprocess.run([exe, "token-login"], timeout=300).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def gh_get(path: str) -> Any:
    return github_api._request("GET", path)


def gh_raw(repo: str, path: str, ref: str) -> str:
    return github_api.get_contents_raw(repo, path, ref).decode("utf-8", errors="replace")


# ─── Pure helpers ─────────────────────────────────────────────────────────

def expected_tag(ref: str, sha: str) -> str:
    """Tag image yang dibuat pipeline: basename tag v*, selain itu short SHA (7)."""
    if ref.startswith(("v", "refs/tags/v")):
        return posixpath.basename(ref)
    return sha[:7]


def gitops_branch_for(env: str, default_branch: str = "main") -> str:
    return env if env in DIRECT_PUSH_ENVS else default_branch


def image_tag(image_ref: Optional[str]) -> str:
    if not image_ref or ":" not in image_ref.rsplit("/", 1)[-1]:
        return ""
    return image_ref.rsplit(":", 1)[-1]


def same_commit(a: str, b: str) -> bool:
    a, b = (a or "").lower(), (b or "").lower()
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def _ts(obj: dict) -> str:
    return (obj.get("metadata") or {}).get("creationTimestamp", "")


def _condition(obj: dict) -> tuple[str, str, str]:
    conds = (obj.get("status") or {}).get("conditions") or [{}]
    c = conds[0]
    return c.get("status", ""), c.get("reason", ""), c.get("message", "")


def _duration(status: dict) -> str:
    from datetime import datetime

    start, end = status.get("startTime"), status.get("completionTime")
    if not start or not end:
        return ""
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        secs = int((datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds())
    except ValueError:
        return ""
    return f"{secs // 60}m{secs % 60:02d}s"


def select_build_run(runs: List[dict], ref: str, sha: str) -> tuple[Optional[dict], bool]:
    """Pilih PipelineRun build untuk ref+SHA. Return (run, cocok_commit).

    Run `devsecops`/`pr-fix` (punya label `pipeline`) diabaikan. Bila tidak ada
    run untuk SHA ini, kembalikan run terbaru utk ref tsb dengan flag False.
    """
    builds = [
        r for r in runs
        if (r.get("metadata", {}).get("labels") or {}).get("ref") == ref
        and "pipeline" not in (r.get("metadata", {}).get("labels") or {})
    ]
    builds.sort(key=_ts, reverse=True)
    for r in builds:
        ann = r.get("metadata", {}).get("annotations") or {}
        if same_commit(ann.get("commit_sha", ""), sha):
            return r, True
    return (builds[0], False) if builds else (None, False)


def find_argocd_app(apps: List[dict], gitops_repo: str, path: str, branch: str) -> Optional[dict]:
    repo_name = gitops_repo.rsplit("/", 1)[-1]
    for app in apps:
        spec = app.get("spec") or {}
        sources = spec.get("sources") or [spec.get("source") or {}]
        for src in sources:
            url = (src.get("repoURL") or "").rstrip("/")
            if url.endswith(".git"):
                url = url[:-4]
            if not url.endswith("/" + repo_name):
                continue
            rev = src.get("targetRevision") or "HEAD"
            if src.get("path", "").strip("/") == path.strip("/") and (
                rev == branch or (rev == "HEAD" and branch == "main")
            ):
                return app
    return None


# ─── Stages ───────────────────────────────────────────────────────────────

def _stage_source(rep: E2EReport, config: dict) -> tuple[Stage, dict]:
    from ._common import fetch_cicd_data
    from .builder import _env_from_ref

    st = Stage("source", "Source")
    try:
        commit = gh_get(f"/repos/{rep.repo}/commits/{quote(rep.ref, safe='')}")
    except github_api.GitHubAPIError as e:
        st.status, st.summary = FAIL, f"ref '{rep.ref}' tidak bisa diakses"
        st.details.append(str(e)[:200])
        return st, {}
    rep.sha = commit["sha"]
    msg = ((commit.get("commit") or {}).get("message") or "").splitlines()[0][:80]
    author = ((commit.get("commit") or {}).get("author") or {})
    rep.env = _env_from_ref(rep.ref, rep.repo)
    rep.expected_tag = expected_tag(rep.ref, rep.sha)

    cicd, cicd_path, _ = fetch_cicd_data(rep.repo, rep.ref, "cicd/cicd.json")
    image_name = (cicd or {}).get("IMAGE") or rep.repo.split("/")[-1]
    org = config["dockerhub"]["org"] or config["registry"]["url"] or "loyaltolpi"
    rep.image = f"{org}/{image_name}"

    st.status = OK
    st.summary = f"{rep.sha[:7]} → ENV={rep.env}, image {rep.image}:{rep.expected_tag}"
    st.details += [
        f"commit: {msg} — {author.get('name', '?')} ({author.get('date', '?')})",
        f"cicd.json: {cicd_path or 'TIDAK ditemukan'}",
    ]
    if not cicd_path:
        st.status = WARN
        st.hints.append("Pipeline butuh cicd.json — jalankan `bq --init-jx` / `bq --pr-fix`.")
    return st, cicd or {}


def _stage_trigger(rep: E2EReport) -> Stage:
    st = Stage("trigger", "Trigger (hook)")
    try:
        gh_raw(rep.repo, TRIGGER_WORKFLOW, rep.ref)
        has_workflow = True
    except github_api.GitHubAPIError:
        has_workflow = False
    st.data["workflow"] = has_workflow

    run = None
    if has_workflow:
        try:
            runs = gh_get(
                f"/repos/{rep.repo}/actions/workflows/trigger-ci.yml/runs"
                f"?head_sha={rep.sha}&per_page=5"
            ).get("workflow_runs") or []
            run = runs[0] if runs else None
        except github_api.GitHubAPIError as e:
            st.details.append(f"Actions API: {str(e)[:120]}")

    hook = None
    try:
        hooks = gh_get(f"/repos/{rep.repo}/hooks") or []
        hook = next((h for h in hooks if (h.get("config") or {}).get("url") == HOOK_URL), None)
        st.data["hook"] = {"installed": bool(hook), "active": bool(hook and hook.get("active"))}
        st.details.append(
            f"webhook {HOOK_URL}: "
            + ("aktif" if hook and hook.get("active") else "non-aktif" if hook else "tidak terpasang")
        )
    except github_api.GitHubAPIError:
        st.details.append("webhook: tidak bisa dicek (butuh akses admin repo)")

    if run:
        concl = run.get("conclusion") or run.get("status")
        st.data["actions_run"] = {"conclusion": concl, "url": run.get("html_url")}
        st.details.insert(0, f"Actions run #{run.get('run_number')}: {concl} — {run.get('html_url')}")
        if run.get("status") != "completed":
            st.status, st.summary = RUNNING, "GitHub Action trigger-ci sedang berjalan"
        elif concl == "success":
            st.status, st.summary = OK, "trigger-ci.yml sukses POST ke /trigger"
        else:
            st.status, st.summary = FAIL, f"trigger-ci.yml {concl}"
            st.hints.append("Cek log Action; umumnya secret WEBHOOK_TRIGGER_URL/TOKEN belum diset "
                            "→ `bq --init-secrets`.")
    elif hook and hook.get("active"):
        st.status, st.summary = OK, "dipicu via webhook /hook (tanpa GitHub Action)"
    elif not has_workflow:
        st.status, st.summary = FAIL, f"{TRIGGER_WORKFLOW} tidak ada di {rep.ref} dan webhook tidak aktif"
        st.hints.append(f"Pasang trigger: `bq --pr-fix {rep.repo.split('/')[-1]} {rep.ref}` "
                        "atau `bq --cicd-webhook <repo>`.")
    else:
        st.status, st.summary = WARN, "tidak ada run trigger-ci untuk commit ini"
        st.hints.append("Branch mungkin tidak termasuk `on.push` trigger-ci.yml, atau run sudah "
                        f"kedaluwarsa. Picu ulang: `bq --cicd-trigger {rep.repo.split('/')[-1]} {rep.ref}`.")
    return st


def _stage_pipeline(rep: E2EReport, config: dict, *, with_logs: bool) -> Stage:
    st = Stage("pipeline", "Pipeline (Tekton)")
    ctx = config["webhook"]["k8s_context"]
    ns = config["webhook"]["k8s_namespace"]
    short = rep.repo.split("/")[-1]
    try:
        runs = kubectl_json(ctx, ["-n", ns, "get", "pipelineruns", "-l", f"repo={short}"]).get("items", [])
    except KubectlError as e:
        st.status, st.summary = SKIP, f"tidak bisa baca PipelineRun ({ctx}/{ns})"
        st.details.append(str(e))
        return st

    run, exact = select_build_run(runs, rep.ref, rep.sha)
    if run is None:
        st.status, st.summary = FAIL, f"tidak ada PipelineRun build untuk {short}@{rep.ref}"
        st.hints.append("Trigger tidak sampai ke cluster (atau run sudah di-prune). "
                        f"Coba `bq --cicd-trigger {short} {rep.ref}`.")
        return st

    name = run["metadata"]["name"]
    ann = run["metadata"].get("annotations") or {}
    cond_status, reason, message = _condition(run)
    st.data.update({"pipelinerun": name, "reason": reason, "exact_commit": exact})
    st.details.append(
        f"{name}: {reason or 'Pending'} {_duration(run.get('status') or {})} "
        f"(commit {ann.get('commit_sha', '?')}, oleh {ann.get('author', '?')})"
    )

    results: Dict[str, str] = {}
    child = ((run.get("status") or {}).get("childReferences") or [{}])[0].get("name")
    if child:
        try:
            tr = kubectl_json(ctx, ["-n", ns, "get", "taskrun", child])
            trs = tr.get("status") or {}
            results = {r["name"]: r.get("value", "") for r in trs.get("results") or []}
            failed = next(
                (s for s in trs.get("steps") or []
                 if (s.get("terminated") or {}).get("exitCode", 0) != 0),
                None,
            )
            if failed:
                step = failed["name"]
                st.data["failed_step"] = step
                st.details.append(f"step gagal: {step} (exit {failed['terminated'].get('exitCode')})")
                pod = trs.get("podName", "")
                if with_logs and pod:
                    tail = kubectl_logs(ctx, ns, pod, f"step-{step}")
                    if tail:
                        st.data["log_tail"] = tail
                st.hints.append(f"Log lengkap: kubectl --context {ctx} -n {ns} logs {pod} -c step-{step}")
        except KubectlError as e:
            st.details.append(f"TaskRun {child}: {e}")
    st.data["results"] = results
    if results:
        st.details.append("results: " + ", ".join(f"{k}={v}" for k, v in results.items() if v))

    if cond_status == "Unknown" or not reason:
        st.status, st.summary = RUNNING, f"{name} masih berjalan"
    elif cond_status == "False":
        st.status, st.summary = FAIL, f"{name} {reason}: {message[:120]}"
        if st.data.get("failed_step") == "build":
            st.hints.append("Error build image — lihat skill docker-build-fix / `bq --fix-dockerfile`.")
    else:
        st.status = OK
        st.summary = f"{name} Succeeded — image {results.get('build-mode', 'built')}"
        built_tag = results.get("image-tag")
        if built_tag and built_tag != rep.expected_tag:
            st.status = WARN
            st.details.append(f"image-tag {built_tag} ≠ tag yang diharapkan {rep.expected_tag}")

    if not exact:
        if st.status == OK:
            st.status = WARN
        st.summary = f"run untuk commit {rep.sha[:7]} tidak ditemukan; terakhir: {st.summary}"
        st.hints.append("Commit ini belum diproses pipeline (atau run sudah di-prune). "
                        f"Picu ulang: `bq --cicd-trigger {short} {rep.ref}`.")
    return st


def _gitops_path(rep: E2EReport, cicd: dict, config: dict, results: dict) -> str:
    if results.get("gitops-file"):
        return results["gitops-file"]
    from .builder import _is_fe_repo
    from .rollout import compute_rollout

    short = rep.repo.split("/")[-1]
    if _is_fe_repo(short):
        return f"web/{rep.env}/{short}/docker-compose.yaml"
    r = compute_rollout(
        cicd=cicd, repo=short, ref=rep.ref,
        image_tag=f"{rep.image}:{rep.expected_tag}", config=config,
    )
    return r["gitops_path"]


def _stage_gitops(rep: E2EReport, cicd: dict, config: dict, pipeline: Stage) -> Stage:
    st = Stage("gitops", "GitOps")
    gitops_repo = config["gitops"]["repo"]
    results = pipeline.data.get("results") or {}
    path = _gitops_path(rep, cicd, config, results)
    branch = gitops_branch_for(rep.env, config["gitops"]["branch"])
    st.data.update({"repo": gitops_repo, "path": path, "branch": branch})
    st.details.append(f"{gitops_repo}@{branch}:{path}")

    gres = results.get("gitops-result", "")
    if gres:
        st.details.append(f"pipeline gitops-result: {gres} {results.get('gitops-url', '')}".rstrip())

    pr_url = results.get("gitops-url", "")
    m = re.search(r"/pull/(\d+)", pr_url)
    if m:
        try:
            pr = gh_get(f"/repos/{gitops_repo}/pulls/{m.group(1)}")
            state = "merged" if pr.get("merged") else pr.get("state", "?")
            st.data["pr"] = {"number": int(m.group(1)), "state": state, "url": pr_url}
            st.details.append(f"PR #{m.group(1)}: {state}")
            if state != "merged":
                st.status = WARN if state == "open" else FAIL
                st.summary = f"PR GitOps #{m.group(1)} {state} — belum masuk {branch}"
                st.hints.append(f"Review & merge {pr_url} agar ArgoCD men-deploy {rep.expected_tag}.")
                return st
        except github_api.GitHubAPIError as e:
            st.details.append(f"PR #{m.group(1)}: {str(e)[:100]}")

    try:
        text = gh_raw(gitops_repo, path, branch)
    except github_api.GitHubAPIError:
        st.data["missing"] = True
        st.status, st.summary = FAIL, f"file GitOps tidak ada di {branch}"
        st.hints.append("Bootstrap manifest dulu (skill gitops-cce-bootstrap / gitops-k8s-bootstrap / "
                        "gitops-web-bootstrap, atau `bq --bootstrap-k8s`).")
        return st

    from .gitops_set_image import _extract_first_image

    st.data["_yaml"] = text  # dipakai tahap manifest; dibuang sebelum laporan
    current = _extract_first_image(text) or ""
    tag = image_tag(current)
    st.data["image"] = current
    st.details.append(f"image di YAML: {current or '?'}")
    try:
        commits = gh_get(
            f"/repos/{gitops_repo}/commits?sha={quote(branch)}&path={quote(path)}&per_page=1"
        ) or []
        if commits:
            c = commits[0]
            st.details.append(
                f"commit terakhir: {c['sha'][:7]} {((c.get('commit') or {}).get('message') or '').splitlines()[0][:70]}"
            )
    except github_api.GitHubAPIError:
        pass

    if tag == rep.expected_tag:
        st.status, st.summary = OK, f"YAML sudah {rep.expected_tag}"
    else:
        st.status = WARN
        st.summary = f"YAML berisi {tag or '?'}, bukan {rep.expected_tag}"
        if gres == "pushed":
            st.hints.append("Pipeline melaporkan `pushed` tapi YAML sudah berubah lagi — kemungkinan "
                            "ditimpa commit/pipeline lain; cek riwayat commit file ini.")
        elif gres == "not-found":
            st.status = FAIL
            st.hints.append("Pipeline tidak menemukan file deployment — bootstrap manifest dulu.")
        elif pipeline.status != OK:
            # Jangan sarankan set-image: image tag ini belum tentu pernah ter-build.
            st.hints.append(f"YAML belum memuat {rep.expected_tag} karena pipeline belum menghasilkan "
                            "image — selesaikan tahap Pipeline dulu.")
        else:
            st.hints.append(f"Update via GitOps: `bq --gitops-set-image {gitops_repo} {branch} {path} "
                            f"{rep.image}:{rep.expected_tag}` (atau PR).")
    return st


def _manifest_jev(findings: list, *, app: str, ns: str) -> Optional[dict]:
    """Satu Jev call: kesiapan deploy + rule yang harus dibereskan dulu. Fail-safe."""
    try:
        from .tui import jev_decide, load_env, resolve_provider

        load_env()
        provider = resolve_provider()
        resp = jev_decide(
            {"deployment": app, "namespace": ns,
             "findings": [{"rule": f.rule_name, "severity": f.severity, "message": f.message}
                          for f in findings]},
            {
                "deploy_ready": {
                    "type": "noul",
                    "instructions": "Apakah pasangan Deployment/Service/Secret ini siap melayani traffic?",
                    "criteria": {"true": "Selector cocok, secret tersedia — pod bisa jalan & menerima traffic.",
                                 "false": "Ada masalah yang membuat pod gagal jalan atau traffic tidak sampai."},
                },
                "fix_first": {
                    "type": "choice",
                    "instructions": "Rule mana yang paling perlu dibereskan lebih dulu?",
                    "criteria": {f.rule_name: f.message for f in findings},
                },
            },
            provider,
        )
    except Exception as e:  # provider/key/network — Jev hanya pelengkap
        return {"error": str(e)[:120]}
    answers = resp.get("answers") or {}
    try:
        ready = float((answers.get("deploy_ready") or {}).get("noul", 0.0))
    except (TypeError, ValueError):
        ready = 0.0
    return {"deploy_ready": ready, "fix_first": (answers.get("fix_first") or {}).get("choice", "")}


def _fetch_service(repo: str, path: str, branch: str, suffixes: List[str]) -> tuple[Optional[dict], str]:
    """Service pasangan `<app>_deployment.yaml` (suffix pertama yang ada); path default bila tidak ada."""
    from . import manifest_check as mc

    for suffix in suffixes:
        candidate = path.replace("_deployment.yaml", suffix)
        try:
            service = mc.find_kind(mc.parse_yaml(gh_raw(repo, candidate, branch)), "Service")
        except Exception:  # 404 / YAML rusak → anggap belum ada
            continue
        if service is not None:
            return service, candidate
    return None, path.replace("_deployment.yaml", suffixes[0])


def _fetch_kustomization(repo: str, folder: str, branch: str) -> tuple[Optional[dict], str]:
    from . import manifest_check as mc

    try:
        return mc.parse_kustomization(gh_raw(repo, f"{folder}/kustomization.yaml", branch)), ""
    except github_api.GitHubAPIError:
        return None, f"{folder}/kustomization.yaml tidak ada"
    except Exception as e:  # YAML rusak
        return None, f"kustomization.yaml tidak bisa dibaca: {str(e)[:80]}"


def _manifest_sample(repo: str, folder: str, app: str, rules: dict,
                     cache: Optional[dict] = None) -> Optional[dict]:
    """Sample Deployment+Service tetangga dari GitOps `<branch>:<infra>/<namespace>` (default main:cce).

    Kandidat diurutkan (role/project sama dulu); yang dipakai harus punya Service,
    terdaftar di kustomization, dan lolos rule critical — agar kesalahan tidak ikut tersalin.
    `cache` (dict) dipakai ulang lintas aplikasi saat scan satu namespace.
    """
    from . import manifest_check as mc

    memo = cache if cache is not None else {}

    def cached(key, fn):
        if key not in memo:
            memo[key] = fn()
        return memo[key]

    src = mc.sample_source(rules)
    sample_folder = f"{src['infra']}/{folder.split('/', 1)[-1]}"
    files = cached(("ls", sample_folder), lambda: gh_list_dir(repo, sample_folder, src["branch"]))
    if not files:
        return None
    kz = cached(("kz", sample_folder),
                lambda: _fetch_kustomization(repo, sample_folder, src["branch"])[0])
    suffixes = (rules.get("service_missing") or mc.DEFAULT_RULES["service_missing"])["params"]["suffixes"]

    def validate(cand: str) -> Optional[dict]:
        dpath = f"{sample_folder}/{cand}_deployment.yaml"
        try:
            deploy = mc.find_kind(mc.parse_yaml(gh_raw(repo, dpath, src["branch"])), "Deployment")
        except Exception:
            return None
        service, spath = _fetch_service(repo, dpath, src["branch"], suffixes)
        if deploy is None or service is None:
            return None
        ns = (kz or {}).get("namespace") or (deploy.get("metadata") or {}).get("namespace") or ""
        res = mc.check(rules, deploy=deploy, service=service, app=cand, ns=ns, deploy_path=dpath,
                       service_path=spath, kustomization=kz)
        if res.worst == "critical":
            return None
        return {"app": cand, "deploy": deploy, "service": service, "path": f"{src['branch']}:{dpath}"}

    for cand in mc.rank_samples(files, app)[: int(src.get("max_candidates", 5))]:
        sample = cached(("sample", sample_folder, cand), lambda c=cand: validate(c))
        if sample:
            return sample
    return None

def _secret_scope(st: Stage, *, ns: str, app: str, dest: dict, config: dict,
                  infra: str = "cce") -> Optional[set]:
    """Nama Secret di namespace target, atau None bila cluster tak bisa dipastikan.

    Context: destination Application ArgoCD (pasti) → mapping `kube_cx` per namespace
    → kandidat namespace ambigu (cluster yang menjalankan Deployment ini). Mapping
    `kube_cx` hanya berisi cluster CCE (hw-*), jadi tidak dipakai untuk path `k8s/` (SLS).
    """
    from . import kube_contexts

    ctx = kube_contexts.context_for_destination(dest, config["argocd"]["k8s_context"])
    if ctx:
        candidates, why = [ctx], f"destination ArgoCD {dest.get('server') or dest.get('name')}"
    elif infra != "cce":
        st.details.append(f"infra {infra}/ (SLS) tidak ada di mapping kube_cx — secret dicek dari repo GitOps")
        return None
    else:
        ctx, why = kube_contexts.resolve(ns)
        candidates = [ctx] if ctx else kube_contexts.candidates(ns)
    for cand in candidates:
        try:
            if len(candidates) > 1 and app not in kubectl_names(cand, ns, "deployments"):
                st.details.append(f"{cand}/{ns}: Deployment {app} tidak ada di sini")
                continue
            names = set(kubectl_names(cand, ns, "secrets"))
            st.details.append(f"secret dicek di cluster {cand}/{ns} ({why})")
            return names
        except KubectlError as e:
            st.details.append(f"cluster {cand} tidak terjangkau ({str(e)[:80]})")
    st.details.append("secret dicek dari repo GitOps"
                      + (f" ({why})" if not candidates else " (cluster tidak bisa dipastikan)"))
    return None


def _stage_manifest(rep: E2EReport, cicd: dict, gitops: Stage, argocd: Stage, config: dict, *,
                    use_jev: bool) -> Stage:
    from . import manifest_check as mc

    st = Stage("manifest", "Manifest (deploy↔svc↔secret)")
    path = gitops.data.get("path", "")
    text = gitops.data.pop("_yaml", None)
    if not path or path.startswith("web/"):
        st.applicable = not path.startswith("web/")
        st.summary = ("FE Docker Compose — tidak ada Deployment/Service K8s" if path
                      else "dilewati (path GitOps tidak diketahui)")
        return st
    try:
        rules, src = mc.load_rules()
        docs = mc.parse_yaml(text) if text else []
    except ImportError:
        st.summary = "dilewati — pyyaml belum terpasang (pip install pyyaml)"
        return st
    except Exception as e:  # YAML rusak
        st.status, st.summary = FAIL, f"YAML deployment tidak valid: {str(e)[:100]}"
        return st
    st.details.append(f"rule standar: {len(rules)} dari {mc.COLLECTION} ({src})")
    if text is None and not gitops.data.get("missing"):
        st.summary = "dilewati — YAML GitOps belum dibaca (selesaikan tahap GitOps dulu)"
        return st

    repo, branch = gitops.data.get("repo", ""), gitops.data.get("branch", "main")
    folder = posixpath.dirname(path)
    infra, folder_ns = folder.split("/", 1)[0], folder.split("/", 1)[-1]
    app = cicd.get("DEPLOYMENT") or posixpath.basename(path).removesuffix("_deployment.yaml")
    stack = mc.detect_stack(None, cicd)
    deploy = mc.find_kind(docs, "Deployment")
    kz, kz_note = _fetch_kustomization(repo, folder, branch)
    if kz_note:
        st.details.append(kz_note)
    dest = argocd.data.get("destination") or {}
    # namespace: di kustomization menimpa metadata.namespace file.
    ns = ((kz or {}).get("namespace") or ((deploy or {}).get("metadata") or {}).get("namespace")
          or dest.get("namespace") or folder_ns)
    if ns != folder_ns:
        st.details.append(f"namespace efektif: {ns} (folder {folder_ns})")
    suffixes = (rules.get("service_missing") or mc.DEFAULT_RULES["service_missing"])["params"]["suffixes"]

    if deploy is None:
        port = int(cicd["PORT"]) if str(cicd.get("PORT", "")).isdigit() else 80
        image = f"{rep.image}:{rep.expected_tag}"
        sample = _manifest_sample(repo, folder, app, rules)
        if sample:
            fixed = mc.adapt_sample_deployment(rules, sample, app=app, ns=ns, image=image, port=port,
                                               stack=stack, infra=infra, kz=kz)
            service, origin = mc.adapt_sample_service(sample, fixed), f"dari sample {sample['path']}"
        else:
            fixed = mc.suggest_deployment(rules, app=app, ns=ns, image=image, port=port, stack=stack,
                                          infra=infra)
            service, origin = mc.suggest_service(fixed), "generator standar (sample tidak ditemukan)"
        st.status, st.summary = FAIL, f"Deployment {app} belum ada — saran YAML {origin}"
        st.data["suggestions"] = [
            {"title": f"Deployment baru ({origin})", "path": path, "yaml": mc.dump_yaml(fixed)},
            {"title": f"Service baru ({origin})", "path": path.replace("_deployment.yaml", suffixes[0]),
             "yaml": mc.dump_yaml(service)},
        ] + [mc._secret_suggestion(rules, name=n, app=app, ns=ns, stack=stack, infra=infra)
             for n in mc.referenced_secrets(fixed)]
        st.hints.append("Simpan YAML di atas ke repo GitOps + daftarkan kedua file di resources "
                        "kustomization.yaml (atau `bq --bootstrap-k8s`).")
        return st

    service = mc.find_kind(docs, "Service")
    service_path = path
    if service is None:
        service, service_path = _fetch_service(repo, path, branch, suffixes)
    sample = None
    if service is None and any(c.get("ports") for c in (deploy.get("spec") or {}).get("template", {})
                               .get("spec", {}).get("containers") or []):
        sample = _manifest_sample(repo, folder, app, rules)

    in_cluster = _secret_scope(st, ns=ns, app=app, dest=dest, config=config, infra=infra)
    in_repo = None
    if in_cluster is None:
        in_repo = set(gh_list_dir(repo, folder, branch)) | set(gh_list_dir(repo, f"{folder}/file-config", branch))

    res = mc.check(rules, deploy=deploy, service=service, app=app, ns=ns, deploy_path=path,
                   service_path=service_path, cicd=cicd, secrets_in_cluster=in_cluster,
                   secrets_in_repo=in_repo, kustomization=kz, sample=sample)
    st.details.append(f"Service: {service_path}" + ("" if service else " (TIDAK ada)"))
    for name, state in res.secrets.items():
        st.details.append(f"secret {name}: {state}")
    st.details += res.notes
    for f in res.findings:
        st.details.append(f"[{f.severity}] {f.rule_name}: {f.message}")
        if f.fix_hint not in st.hints:
            st.hints.append(f.fix_hint)
    st.data["findings"] = [vars(f) for f in res.findings]
    st.data["suggestions"] = res.suggestions

    critical = [f.rule_name for f in res.findings if f.severity == "critical"]
    advisory = [f.rule_name for f in res.findings if f.severity != "critical"]
    unknown = [n for n, s in res.secrets.items() if s in ("repo", "tidak-diketahui")]
    if critical:
        st.status = FAIL
        st.summary = f"{len(critical)} masalah — " + ", ".join(dict.fromkeys(critical))
    elif unknown:
        # Selector sudah dicek, tapi secret belum terverifikasi → analisa tidak lengkap.
        st.summary = f"selector cocok; secret belum bisa dipastikan di cluster: {', '.join(unknown)}"
    else:
        st.status, st.summary = OK, "selector Deployment↔Service cocok, secret tersedia"
    if advisory and not critical:
        # Saran standar (warn) tidak memutus deploy → bukan titik putus.
        st.summary += f" · saran standar: {', '.join(dict.fromkeys(advisory))}"

    if use_jev and res.findings:
        verdict = _manifest_jev(res.findings, app=app, ns=ns)
        st.data["jev"] = verdict
        if verdict and "error" not in verdict:
            st.details.append(f"Jev: siap melayani traffic {verdict['deploy_ready']:.0%}"
                              + (f", bereskan dulu: {verdict['fix_first']}" if verdict["fix_first"] else ""))
        elif verdict:
            st.details.append(f"Jev dilewati: {verdict['error']}")
    return st


def _stage_argocd(rep: E2EReport, config: dict, gitops: Stage) -> Stage:
    st = Stage("argocd", "ArgoCD (deploy)")
    path = gitops.data.get("path", "")
    if not path:
        st.summary = "dilewati (path GitOps tidak diketahui)"
        return st
    if path.startswith("web/"):
        st.summary = "FE di-deploy via Docker Compose — tidak dipantau ArgoCD"
        st.applicable = False
        return st

    ctx = config["argocd"]["k8s_context"]
    ns = config["argocd"]["k8s_namespace"]
    try:
        apps = kubectl_json(ctx, ["-n", ns, "get", "applications.argoproj.io"]).get("items", [])
        st.data["via"] = "kubectl"
    except KubectlError as e:
        api_url = (config["argocd"].get("api_url") or "").rstrip("/")
        try:
            try:
                apps = argocd_api_apps(config)
            except ArgocdApiError as first:
                if not (first.auth and argocd_token_login(config)):
                    raise
                # Token baru ditulis ke auth file; env ARGOCD_TOKEN lama diabaikan.
                apps = argocd_api_apps({**config, "argocd": {**config["argocd"], "token": ""}})
                st.details.append("token ArgoCD diperbarui via `argocd-app token-login`")
        except ArgocdApiError as api_err:
            st.summary = f"tidak bisa baca Application ArgoCD ({ctx}/{ns} maupun API)"
            st.details.append(f"kubectl: {e}")
            st.details.append(f"ArgoCD API {api_url}: {api_err}")
            if api_err.auth:
                st.hints.append("Perbarui token SSO ArgoCD: `argocd-app token-login` "
                                "(atau set ARGOCD_TOKEN), lalu ulangi.")
            return st
        st.data["via"] = "api"
        ctx = api_url
        st.details.append(f"kubectl {config['argocd']['k8s_context']} gagal ({e}) — dibaca via ArgoCD API {api_url}")

    folder = posixpath.dirname(path)
    branch = gitops.data.get("branch", "main")
    app = find_argocd_app(apps, gitops.data.get("repo", ""), folder, branch)
    if app is None:
        st.status, st.summary = WARN, f"tidak ada Application untuk {folder}@{branch} di {ctx}"
        if folder.startswith("k8s/"):
            st.hints.append("Infra SLS (k8s/) tidak dikelola ArgoCD di context ini — set "
                            "ARGOCD_KUBE_CONTEXT ke cluster ArgoCD yang benar, atau cek deploy manual.")
        else:
            st.hints.append("Folder GitOps belum didaftarkan sebagai Application ArgoCD.")
        return st

    name = app["metadata"]["name"]
    spec, status = app.get("spec") or {}, app.get("status") or {}
    st.data["destination"] = spec.get("destination") or {}
    sync = (status.get("sync") or {}).get("status", "?")
    health = (status.get("health") or {}).get("status", "?")
    # `automated: {}` (tanpa prune/selfHeal) tetap berarti auto-sync AKTIF.
    auto = (spec.get("syncPolicy") or {}).get("automated") is not None
    op = status.get("operationState") or {}
    st.data.update({"app": name, "sync": sync, "health": health, "auto_sync": auto})
    st.details.append(f"Application {name}: sync={sync} health={health} auto-sync={'on' if auto else 'off'}")
    if op:
        st.details.append(f"operasi terakhir: {op.get('phase')} {op.get('finishedAt', '')} {op.get('message', '')[:80]}".rstrip())

    for cond in status.get("conditions") or []:
        st.details.append(f"condition {cond.get('type')}: {cond.get('message', '')[:120]}")

    image_name = rep.image.rsplit("/", 1)[-1]
    live = next(
        (i for i in (status.get("summary") or {}).get("images") or []
         if i.rsplit(":", 1)[0].endswith("/" + image_name) or i.rsplit(":", 1)[0] == image_name),
        "",
    )
    live_tag = image_tag(live)
    st.data["live_image"] = live
    st.details.append(f"image live: {live or 'tidak ditemukan di summary'}")

    deploy = next(
        (r for r in status.get("resources") or []
         if r.get("kind") == "Deployment" and r.get("name") == (gitops.data.get("deployment") or image_name)),
        None,
    ) or next((r for r in status.get("resources") or []
               if r.get("kind") == "Deployment" and image_name in r.get("name", "")), None)
    if deploy:
        st.details.append(f"Deployment {deploy['name']}: {deploy.get('status')} "
                          f"{(deploy.get('health') or {}).get('status', '')}".rstrip())

    overrides = [
        i for src in (spec.get("sources") or [spec.get("source") or {}])
        for i in ((src.get("kustomize") or {}).get("images") or [])
        if image_name in i
    ]
    if overrides:
        st.details.append(f"kustomize image override: {', '.join(overrides)}")
        st.hints.append("Override image di Application mengunci tag — hapus (skill gitops-audit).")

    gitops_tag = image_tag(gitops.data.get("image"))
    if live_tag == rep.expected_tag:
        st.status, st.summary = OK, f"{rep.expected_tag} sudah live ({health})"
    elif deploy and deploy.get("status") == "OutOfSync":
        st.status = WARN
        st.summary = f"live {live_tag or '?'}, GitOps {gitops_tag or '?'} — Deployment OutOfSync"
        if auto:
            st.hints.append(
                f"Auto-sync ON tapi belum menerapkan perubahan (operasi terakhir "
                f"{op.get('finishedAt', '?')}) — cek sync window / error comparison, "
                f"lalu refresh/sync Application {name} (skill argocd-app)."
            )
        else:
            st.hints.append(f"Auto-sync OFF — sync manual Application {name} (skill argocd-app).")
    else:
        st.status = WARN
        st.summary = f"live {live_tag or '?'}, diharapkan {rep.expected_tag}"
    if health not in ("Healthy", "?") and st.status == OK:
        st.status = WARN
        st.hints.append(f"Application {health} — cek pod (skill log-inspector).")
    return st


# ─── Entry point ──────────────────────────────────────────────────────────

def analyze(
    repo: str,
    ref: str,
    *,
    config: Optional[dict] = None,
    with_logs: bool = True,
    on_stage: Optional[Callable[[str], None]] = None,
    use_jev: bool = False,
) -> E2EReport:
    """Telusuri satu commit dari trigger sampai ArgoCD. Read-only.

    `use_jev`: tahap manifest meminta penilaian Jev bila ada findings.
    """
    from .config import load_config

    config = config or load_config()
    repo = github_api.normalize_repo(repo)
    if "/" not in repo:  # shorthand → GITHUB_ORG, fallback owner repo GitOps
        org = config["git"]["org"] or config["gitops"]["repo"].split("/")[0]
        repo = f"{org}/{repo}"
    rep = E2EReport(repo=repo, ref=ref)

    def step(title: str) -> None:
        if on_stage:
            on_stage(title)

    step("source")
    source, cicd = _stage_source(rep, config)
    rep.stages.append(source)
    if source.status == FAIL:
        return rep

    step("trigger")
    rep.stages.append(_stage_trigger(rep))
    step("pipeline")
    pipeline = _stage_pipeline(rep, config, with_logs=with_logs)
    rep.stages.append(pipeline)
    step("gitops")
    gitops = _stage_gitops(rep, cicd, config, pipeline)
    gitops.data["deployment"] = cicd.get("DEPLOYMENT") or cicd.get("IMAGE")
    rep.stages.append(gitops)
    # ArgoCD dibaca dulu: destination Application = context paling pasti untuk cek Secret.
    step("argocd")
    argocd = _stage_argocd(rep, config, gitops)
    step("manifest")
    rep.stages.append(_stage_manifest(rep, cicd, gitops, argocd, config, use_jev=use_jev))
    gitops.data.pop("_yaml", None)
    rep.stages.append(argocd)
    return rep


def verdict_line(rep: E2EReport) -> str:
    bp = rep.break_point
    if bp:
        return f"🎯 Titik putus: {bp.title} — {bp.summary}"
    if rep.unchecked:
        names = ", ".join(s.title for s in rep.unchecked)
        argocd = next((s for s in rep.stages if s.key == "argocd"), None)
        tail = (f"ArgoCD mengonfirmasi {rep.expected_tag} sudah live." if argocd and argocd.status == OK
                else "Belum bisa dipastikan live.")
        return f"❔ Analisa tidak lengkap — tidak bisa memeriksa: {names}. {tail}"
    return "🎉 Alur end-to-end sehat."


def exit_code(rep: E2EReport) -> int:
    """0 = sehat, 1 = ada tahap FAIL, 2 = tidak lengkap / hanya WARN."""
    bp = rep.break_point
    if bp and bp.status == FAIL:
        return 1
    return 0 if rep.verdict == "healthy" else 2


def render_text(rep: E2EReport) -> str:
    """Laporan plain-text (CLI / non-TUI)."""
    lines = [f"🔭 E2E CI/CD: {rep.repo} @ {rep.ref}"
             + (f" ({rep.sha[:7]}, ENV={rep.env}, tag {rep.expected_tag})" if rep.sha else ""), ""]
    lines.append("   " + " → ".join(f"{STATUS_ICON[s.status]} {s.title}" for s in rep.stages))
    for s in rep.stages:
        lines += ["", f"{STATUS_ICON[s.status]} {s.title}: {s.summary}"]
        lines += [f"     • {d}" for d in s.details]
        if s.data.get("log_tail"):
            lines.append("     log:")
            lines += [f"       {ln}" for ln in s.data["log_tail"].rstrip().splitlines()]
        lines += [f"     💡 {h}" for h in s.hints]
        for sug in s.data.get("suggestions") or []:
            lines += ["", f"     📝 Saran YAML — {sug['title']}", f"        # {sug['path']}"]
            lines += [f"        {ln}" for ln in sug["yaml"].rstrip().splitlines()]
    bp = rep.break_point
    lines += ["", verdict_line(rep)]
    return "\n".join(lines)
