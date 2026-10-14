"""Simulasi `bq --e2e` — dunia tiruan GitHub + Tekton + ArgoCD tanpa jaringan.

Dipakai untuk:
- demo / pelatihan: `bq --e2e --simulate <skenario>` atau `/e2e --sim <skenario>` di TUI;
- self-test: `bq --e2e --simulate all` memverifikasi tiap skenario menghasilkan
  status tahap & titik putus yang diharapkan;
- unit test (`tests/test_e2e.py`) — fixture yang sama, tanpa duplikasi.

Bentuk objek tiruan mengikuti objek nyata di cluster hw-dev (PipelineRun
`webhook-<repo>-*` + results TaskRun) dan hw-pro-p (Application ArgoCD).
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from unittest import mock

from . import e2e
from .github_api import GitHubAPIError

ORG = "Qoin-Digital-Indonesia"
GITOPS_REPO = f"{ORG}/gitops"
IMAGE_ORG = "loyaltolpi"
SIM_REPO = "pay-be-demo-manager"
SIM_SHA = "c0ffee1" + "a" * 33

SIM_CONFIG: Dict[str, Any] = {
    "dockerhub": {"org": IMAGE_ORG},
    "registry": {"url": ""},
    "git": {"org": ORG},
    "webhook": {"k8s_context": "hw-dev", "k8s_namespace": "jenkins-x"},
    "argocd": {"k8s_context": "hw-pro-p", "k8s_namespace": "argocd"},
    "gitops": {"repo": GITOPS_REPO, "branch": "main", "infra": "cce", "ns_suffix": "qoin"},
}


# ─── Pembangun objek tiruan ───────────────────────────────────────────────

def pipeline_run(
    repo: str, ref: str, commit: str, *,
    reason: str = "Succeeded", ts: str = "2026-10-14T02:44:56Z",
    labels: Optional[dict] = None, suffix: str = "bf144f",
) -> dict:
    status = {"Succeeded": "True", "Failed": "False", "Running": "Unknown"}.get(reason, "True")
    name = f"webhook-{repo}-{suffix}"
    return {
        "metadata": {
            "name": name, "creationTimestamp": ts,
            "labels": {"repo": repo, "ref": ref, "trigger": "webhook", "version": "v2",
                       **(labels or {})},
            "annotations": {"commit_sha": commit[:7], "author": "dev-sim",
                            "commit_msg": "feat: simulasi"},
        },
        "status": {
            "conditions": [{"status": status, "reason": "" if reason == "Running" else reason,
                            "message": f'"step-build" exited with code 2' if reason == "Failed"
                            else "Tasks Completed"}],
            "startTime": "2026-10-14T02:44:56Z",
            "completionTime": None if reason == "Running" else "2026-10-14T02:53:37Z",
            "childReferences": [{"name": f"{name}-ci-task"}],
        },
    }


def task_run(results: Optional[dict] = None, failed_step: Optional[str] = None) -> dict:
    names = ["clone", "notify-start", "docker-login", "build", "release",
             "trigger-devsecops", "trigger-gitops", "notify-done"]
    steps, failed = [], False
    for n in names:
        if n == failed_step:
            failed = True
            steps.append({"name": n, "terminated": {"reason": "Error", "exitCode": 2}})
        elif failed:
            steps.append({"name": n, "terminated": {"reason": "Error", "exitCode": 1}})
        else:
            steps.append({"name": n, "terminated": {"reason": "Completed", "exitCode": 0}})
    return {"status": {
        "podName": "sim-ci-task-pod", "steps": steps,
        "results": [{"name": k, "value": v} for k, v in (results or {}).items()],
    }}


def argocd_app(
    path: str, rev: str, live: str, *,
    sync: str = "Synced", deploy_status: str = "Synced", health: str = "Healthy",
    auto: bool = True, overrides: Optional[List[str]] = None,
    image: str = f"{IMAGE_ORG}/{SIM_REPO}", deployment: str = SIM_REPO,
) -> dict:
    source = {"repoURL": f"https://github.com/{GITOPS_REPO}.git", "path": path,
              "targetRevision": rev}
    if overrides:
        source["kustomize"] = {"images": overrides}
    return {
        "metadata": {"name": path.rsplit("/", 1)[-1]},
        "spec": {"source": source, "syncPolicy": {"automated": {}} if auto else {}},
        "status": {
            "sync": {"status": sync}, "health": {"status": health},
            "operationState": {"phase": "Succeeded", "finishedAt": "2026-10-13T08:45:21Z",
                               "message": "successfully synced (all tasks run)"},
            "summary": {"images": [f"{image}:{live}", f"{IMAGE_ORG}/lain-be-x:1"]},
            "resources": [
                {"kind": "Service", "name": deployment, "status": "Synced"},
                {"kind": "Deployment", "name": deployment, "status": deploy_status,
                 "health": {"status": health}},
            ],
        },
    }


@dataclass
class World:
    """Keadaan GitHub + cluster tiruan untuk satu commit."""

    repo: str = f"{ORG}/{SIM_REPO}"
    ref: str = "develop"
    sha: str = SIM_SHA
    commit_ok: bool = True
    cicd: Dict[str, Any] = field(default_factory=lambda: {"IMAGE": SIM_REPO, "PROJECT": "qoin"})
    files: Dict[Tuple[str, str], str] = field(default_factory=dict)
    actions_runs: List[dict] = field(default_factory=list)
    hooks: Optional[List[dict]] = field(default_factory=list)  # None → 404 (bukan admin)
    pulls: Dict[str, dict] = field(default_factory=dict)
    runs: List[dict] = field(default_factory=list)
    taskrun: dict = field(default_factory=dict)
    apps: List[dict] = field(default_factory=list)
    kubectl_error: Optional[str] = None
    log_tail: str = ""

    @property
    def short(self) -> str:
        return self.repo.split("/")[-1]

    # --- IO tiruan (signature sama dgn build_q.e2e) ---------------------
    def gh_get(self, path: str) -> Any:
        if path.startswith(f"/repos/{self.repo}/commits/"):
            if not self.commit_ok:
                raise GitHubAPIError(f"GitHub API GET {path} → HTTP 422: No commit found")
            return {"sha": self.sha, "commit": {
                "message": "feat: simulasi e2e",
                "author": {"name": "dev-sim", "date": "2026-10-14T02:39:58Z"}}}
        if "/actions/workflows/" in path:
            return {"workflow_runs": self.actions_runs}
        if path.endswith("/hooks"):
            if self.hooks is None:
                raise GitHubAPIError("HTTP 404 Not Found")
            return self.hooks
        if "/pulls/" in path:
            return self.pulls[path.rsplit("/", 1)[-1]]
        if "/commits?" in path:
            return [{"sha": "d00d123" + "0" * 33,
                     "commit": {"message": "chore(gitops): update image [simulasi]"}}]
        raise GitHubAPIError(f"simulasi: endpoint tidak dikenal {path}")

    def gh_raw(self, repo: str, path: str, ref: str) -> str:
        if (repo, path) in self.files:
            return self.files[(repo, path)]
        raise GitHubAPIError("HTTP 404 Not Found")

    def kubectl_json(self, context: str, args: List[str]) -> dict:
        if self.kubectl_error:
            raise e2e.KubectlError(self.kubectl_error)
        if "pipelineruns" in args:
            return {"items": self.runs}
        if "taskrun" in args:
            return self.taskrun
        if "applications.argoproj.io" in args:
            return {"items": self.apps}
        raise e2e.KubectlError(f"simulasi: resource tidak dikenal {args}")

    def kubectl_logs(self, *args, **kwargs) -> str:
        return self.log_tail

    def fetch_cicd_data(self, *args) -> tuple:
        return (self.cicd, "cicd/cicd.json", b"{}") if self.cicd else ({}, None, None)


def gitops_file(repo: str = SIM_REPO, ns: str = "develop-qoin") -> str:
    return f"cce/{ns}/{repo}_deployment.yaml"


def healthy_world(ref: str = "develop", ns: str = "develop-qoin") -> World:
    """Alur sehat: Action sukses → PipelineRun sukses → YAML benar → live di ArgoCD."""
    w = World(ref=ref)
    tag = e2e.expected_tag(ref, w.sha)
    branch = e2e.gitops_branch_for("production" if ref.startswith("v") else ref)
    gfile = gitops_file(ns=ns)
    w.files = {
        (w.repo, e2e.TRIGGER_WORKFLOW): "name: CI Trigger",
        (GITOPS_REPO, gfile): f"        - image: {IMAGE_ORG}/{SIM_REPO}:{tag}\n",
    }
    w.actions_runs = [{"status": "completed", "conclusion": "success", "run_number": 42,
                       "html_url": f"https://github.com/{w.repo}/actions/runs/42"}]
    w.hooks = [{"id": 1, "config": {"url": e2e.HOOK_URL}, "active": True}]
    w.runs = [pipeline_run(SIM_REPO, ref, w.sha)]
    gitops_url = f"https://github.com/{GITOPS_REPO}/blob/{branch}/{gfile}"
    w.taskrun = task_run({"build-mode": "built", "image-tag": tag, "gitops-file": gfile,
                          "gitops-result": "pushed", "gitops-url": gitops_url})
    w.apps = [argocd_app(f"cce/{ns}", branch, tag)]
    return w


@contextmanager
def patched(world: World) -> Iterator[World]:
    """Arahkan semua IO `build_q.e2e` ke `world`."""
    with mock.patch.object(e2e, "gh_get", world.gh_get), \
            mock.patch.object(e2e, "gh_raw", world.gh_raw), \
            mock.patch.object(e2e, "kubectl_json", world.kubectl_json), \
            mock.patch.object(e2e, "kubectl_logs", world.kubectl_logs), \
            mock.patch("build_q._common.fetch_cicd_data", world.fetch_cicd_data):
        yield world


# ─── Skenario ─────────────────────────────────────────────────────────────

@dataclass
class Scenario:
    name: str
    title: str
    build: Callable[[], World]
    break_point: Optional[str]
    stages: Dict[str, str]  # status tahap yang diharapkan (subset boleh)
    verdict: str = "broken"  # broken | incomplete | healthy
    must_mention: Tuple[str, ...] = ()      # frasa wajib ada di laporan teks
    must_not_mention: Tuple[str, ...] = ()  # frasa yang TIDAK boleh muncul (saran salah)


OLD_TAG = "8594c0f"


def _stale(w: World, tag: str = OLD_TAG) -> World:
    """Commit baru belum menghasilkan image → YAML GitOps & live masih tag lama."""
    w.files[(GITOPS_REPO, gitops_file())] = f"        - image: {IMAGE_ORG}/{SIM_REPO}:{tag}\n"
    w.apps = [argocd_app("cce/develop-qoin", "develop", tag)]
    return w


def _healthy() -> World:
    return healthy_world()


def _trigger_action_failed() -> World:
    w = healthy_world()
    w.actions_runs = [{"status": "completed", "conclusion": "failure", "run_number": 43,
                       "html_url": f"https://github.com/{w.repo}/actions/runs/43"}]
    w.hooks = []
    w.runs = []
    return _stale(w)


def _trigger_missing() -> World:
    w = healthy_world()
    del w.files[(w.repo, e2e.TRIGGER_WORKFLOW)]
    w.actions_runs, w.hooks, w.runs = [], [], []
    return _stale(w)


def _pipeline_build_failed() -> World:
    w = healthy_world()
    w.runs = [pipeline_run(SIM_REPO, "develop", w.sha, reason="Failed")]
    w.taskrun = task_run(failed_step="build")
    w.log_tail = ("#12 [builder 6/7] RUN go build -o /app ./cmd\n"
                  "#12 ERROR: process \"go build\" did not complete successfully: exit code: 1\n"
                  "go: github.com/Qoin-Digital-Indonesia/qoinhubhelper@v1.3.0: "
                  "reading github.com/...: 410 Gone\n"
                  "make: *** [Makefile:21: build] Error 2")
    return _stale(w)


def _pipeline_running() -> World:
    w = healthy_world()
    w.runs = [pipeline_run(SIM_REPO, "develop", w.sha, reason="Running")]
    w.taskrun = task_run()
    return _stale(w)


def _pipeline_no_run() -> World:
    w = healthy_world()
    w.actions_runs = []
    w.runs = [pipeline_run(SIM_REPO, "develop", "1234abc" + "0" * 33, suffix="old111",
                           ts="2026-10-13T10:00:00Z")]
    return _stale(w)


def _gitops_not_found() -> World:
    w = healthy_world(ns="staging-qoin", ref="staging")
    del w.files[(GITOPS_REPO, gitops_file(ns="staging-qoin"))]
    w.taskrun = task_run({"build-mode": "built", "image-tag": SIM_SHA[:7],
                          "gitops-file": gitops_file(ns="staging-qoin"),
                          "gitops-result": "not-found"})
    w.apps = []
    return w


def _gitops_overwritten() -> World:
    w = healthy_world()
    w.files[(GITOPS_REPO, gitops_file())] = f"        - image: {IMAGE_ORG}/{SIM_REPO}:9176f76\n"
    w.apps = [argocd_app("cce/develop-qoin", "develop", "9176f76")]
    return w


def _gitops_pr_open() -> World:
    w = healthy_world(ref="v1.4.2", ns="production-qoin")
    gfile = gitops_file(ns="production-qoin")
    w.files[(GITOPS_REPO, gfile)] = f"        - image: {IMAGE_ORG}/{SIM_REPO}:v1.4.1\n"
    w.taskrun = task_run({"build-mode": "built", "image-tag": "v1.4.2", "gitops-file": gfile,
                          "gitops-result": "pr-created",
                          "gitops-url": f"https://github.com/{GITOPS_REPO}/pull/701"})
    w.pulls["701"] = {"state": "open", "merged": False}
    w.apps = [argocd_app("cce/production-qoin", "main", "v1.4.1")]
    return w


def _release_live() -> World:
    w = _gitops_pr_open()
    w.pulls["701"] = {"state": "closed", "merged": True}
    gfile = gitops_file(ns="production-qoin")
    w.files[(GITOPS_REPO, gfile)] = f"        - image: {IMAGE_ORG}/{SIM_REPO}:v1.4.2\n"
    w.apps = [argocd_app("cce/production-qoin", "main", "v1.4.2")]
    return w


def _argocd_out_of_sync() -> World:
    w = healthy_world()
    w.apps = [argocd_app("cce/develop-qoin", "develop", "8594c0f",
                         sync="OutOfSync", deploy_status="OutOfSync")]
    return w


def _argocd_kustomize_override() -> World:
    w = healthy_world()
    w.apps = [argocd_app("cce/develop-qoin", "develop", "8594c0f",
                         overrides=[f"{IMAGE_ORG}/{SIM_REPO}:8594c0f"])]
    return w


def _argocd_degraded() -> World:
    w = healthy_world()
    w.apps = [argocd_app("cce/develop-qoin", "develop", SIM_SHA[:7], health="Degraded")]
    return w


def _argocd_no_app() -> World:
    w = healthy_world()
    w.apps = []
    return w


def _fe_compose() -> World:
    w = healthy_world()
    w.repo = f"{ORG}/pay-fe-demo"
    w.cicd = {"IMAGE": "pay-fe-demo"}
    gfile = "web/develop/pay-fe-demo/docker-compose.yaml"
    w.files = {
        (w.repo, e2e.TRIGGER_WORKFLOW): "name: CI Trigger",
        (GITOPS_REPO, gfile): f"    image: {IMAGE_ORG}/pay-fe-demo:{SIM_SHA[:7]}\n",
    }
    w.runs = [pipeline_run("pay-fe-demo", "develop", w.sha)]
    w.taskrun = task_run({"build-mode": "built", "image-tag": SIM_SHA[:7],
                          "gitops-file": gfile, "gitops-result": "pushed"})
    w.apps = []
    return w


def _cluster_unreachable() -> World:
    w = healthy_world()
    w.kubectl_error = "Unable to connect to the server: dial tcp 10.2.20.121:5443: i/o timeout"
    return w


def _bad_ref() -> World:
    w = healthy_world(ref="feat/tidak-ada")
    w.commit_ok = False
    return w


SCENARIOS: Dict[str, Scenario] = {s.name: s for s in [
    Scenario("healthy", "Alur sehat: commit develop sudah live",
             _healthy, None,
             {"source": "ok", "trigger": "ok", "pipeline": "ok", "gitops": "ok", "argocd": "ok"},
             "healthy"),
    Scenario("trigger-action-failed", "GitHub Action trigger-ci gagal (secret belum diset)",
             _trigger_action_failed, "trigger",
             {"trigger": "fail", "pipeline": "fail", "gitops": "warn", "argocd": "warn"}, must_mention=("bq --init-secrets",), must_not_mention=("--gitops-set-image",)),
    Scenario("trigger-missing", "trigger-ci.yml tidak ada & webhook tidak terpasang",
             _trigger_missing, "trigger",
             {"trigger": "fail", "pipeline": "fail", "gitops": "warn"}, must_mention=("bq --pr-fix pay-be-demo-manager develop",)),
    Scenario("pipeline-build-failed", "PipelineRun gagal di step build (dependency private)",
             _pipeline_build_failed, "pipeline",
             {"trigger": "ok", "pipeline": "fail", "gitops": "warn", "argocd": "warn"}, must_mention=("step gagal: build", "410 Gone", "docker-build-fix"),
             must_not_mention=("--gitops-set-image",)),
    Scenario("pipeline-running", "PipelineRun masih berjalan",
             _pipeline_running, "pipeline", {"pipeline": "running", "gitops": "warn"}, must_not_mention=("--gitops-set-image",)),
    Scenario("pipeline-no-run", "Commit belum diproses pipeline",
             _pipeline_no_run, "pipeline", {"pipeline": "warn", "gitops": "warn"}, must_mention=("bq --cicd-trigger pay-be-demo-manager develop",)),
    Scenario("gitops-not-found", "Image ter-build tapi manifest GitOps belum di-bootstrap",
             _gitops_not_found, "gitops", {"pipeline": "ok", "gitops": "fail", "argocd": "warn"}, must_mention=("Bootstrap manifest",)),
    Scenario("gitops-overwritten", "Pipeline push tag, lalu YAML ditimpa commit lain",
             _gitops_overwritten, "gitops", {"gitops": "warn"}, must_mention=("ditimpa",)),
    Scenario("gitops-pr-open", "Rilis v1.4.2: PR GitOps ke main belum di-merge",
             _gitops_pr_open, "gitops", {"gitops": "warn"}, must_mention=("PR #701: open", "/pull/701")),
    Scenario("release-live", "Rilis v1.4.2: PR merged & live di production",
             _release_live, None,
             {"source": "ok", "trigger": "ok", "pipeline": "ok", "gitops": "ok", "argocd": "ok"},
             "healthy"),
    Scenario("argocd-outofsync", "YAML benar tapi ArgoCD belum sync (auto-sync tertahan)",
             _argocd_out_of_sync, "argocd", {"gitops": "ok", "argocd": "warn"}, must_mention=("auto-sync=on", "Auto-sync ON tapi belum menerapkan")),
    Scenario("argocd-kustomize-override", "Override kustomize di Application mengunci tag lama",
             _argocd_kustomize_override, "argocd", {"argocd": "warn"}, must_mention=("kustomize image override", "gitops-audit")),
    Scenario("argocd-degraded", "Tag sudah live tapi Application Degraded",
             _argocd_degraded, "argocd", {"argocd": "warn"}, must_mention=("Degraded", "log-inspector")),
    Scenario("argocd-no-app", "Folder GitOps belum terdaftar di ArgoCD",
             _argocd_no_app, "argocd", {"argocd": "warn"}, must_mention=("belum didaftarkan sebagai Application",)),
    Scenario("fe-compose", "Repo FE (Docker Compose) — tahap ArgoCD dilewati",
             _fe_compose, None, {"gitops": "ok", "argocd": "skip"}, "healthy"),
    Scenario("cluster-unreachable", "Cluster CI/ArgoCD tidak terjangkau (VPN/timeout)",
             _cluster_unreachable, None, {"pipeline": "skip", "argocd": "skip"}, "incomplete", must_mention=("Analisa tidak lengkap",), must_not_mention=("sehat",)),
    Scenario("bad-ref", "Ref tidak ada di GitHub",
             _bad_ref, "source", {"source": "fail"}),
]}


def simulate(name: str, *, with_logs: bool = True,
             on_stage: Optional[Callable[[str], None]] = None) -> e2e.E2EReport:
    """Jalankan `e2e.analyze` pada dunia tiruan skenario `name`."""
    scenario = SCENARIOS[name]
    world = scenario.build()
    with patched(world):
        return e2e.analyze(world.repo, world.ref, config=copy.deepcopy(SIM_CONFIG),
                           with_logs=with_logs, on_stage=on_stage)


def verify(name: str, report: e2e.E2EReport) -> List[str]:
    """Bandingkan hasil dengan harapan skenario; return daftar selisih (kosong = lulus)."""
    sc = SCENARIOS[name]
    problems = []
    got_bp = report.break_point.key if report.break_point else None
    if got_bp != sc.break_point:
        problems.append(f"titik putus {got_bp!r}, diharapkan {sc.break_point!r}")
    if report.verdict != sc.verdict:
        problems.append(f"verdict {report.verdict!r}, diharapkan {sc.verdict!r}")
    got = {s.key: s.status for s in report.stages}
    for key, want in sc.stages.items():
        if got.get(key) != want:
            problems.append(f"tahap {key}: {got.get(key)!r}, diharapkan {want!r}")
    text = e2e.render_text(report)
    problems += [f"laporan tidak menyebut {m!r}" for m in sc.must_mention if m not in text]
    problems += [f"laporan tidak boleh menyebut {m!r}" for m in sc.must_not_mention if m in text]
    return problems


def run_cli(arg: str) -> int:
    """Handler `bq --e2e --simulate <arg>`.

    - ``list`` → daftar skenario.
    - ``all``  → self-test semua skenario; exit 0 bila semua sesuai harapan.
    - ``<nama>`` → laporan lengkap satu skenario; exit code sama seperti
      `bq --e2e` sungguhan (0 sehat, 1 gagal, 2 tidak lengkap) supaya skrip
      CI yang memakai exit code bisa diuji.
    """
    if arg == "list":
        width = max(len(n) for n in SCENARIOS)
        print("Skenario simulasi `bq --e2e --simulate <nama>`:\n")
        for sc in SCENARIOS.values():
            bp = sc.break_point or "-"
            print(f"  {sc.name:<{width}}  [{sc.verdict:<10} titik putus: {bp:<8}] {sc.title}")
        return 0

    if arg == "all":
        width = max(len(n) for n in SCENARIOS)
        failed = 0
        print(f"🧪 Self-test simulasi bq --e2e — {len(SCENARIOS)} skenario\n")
        for name, sc in SCENARIOS.items():
            report = simulate(name)
            problems = verify(name, report)
            chain = " → ".join(f"{e2e.STATUS_ICON[s.status].strip()}" for s in report.stages)
            mark = "PASS" if not problems else "FAIL"
            failed += bool(problems)
            print(f"  {mark}  {name:<{width}}  {chain:<16}  {report.verdict}")
            for p in problems:
                print(f"        ↳ {p}")
        print(f"\n📊 {len(SCENARIOS) - failed}/{len(SCENARIOS)} skenario sesuai harapan")
        return 1 if failed else 0

    if arg not in SCENARIOS:
        print(f"❌ Skenario '{arg}' tidak dikenal. Lihat: bq --e2e --simulate list")
        return 2
    sc = SCENARIOS[arg]
    report = simulate(arg)
    print(f"🧪 [SIMULASI] {sc.name} — {sc.title}\n")
    print(e2e.render_text(report))
    problems = verify(arg, report)
    print("\n" + ("✅ Sesuai harapan skenario." if not problems
                  else "❌ Tidak sesuai harapan:\n" + "\n".join(f"   ↳ {p}" for p in problems)))
    return e2e.exit_code(report)
