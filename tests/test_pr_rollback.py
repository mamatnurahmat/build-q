"""Tests untuk `bq --pr-rollback` (build_q.pr_rollback) + pemindai rahasia bersama.

Bagian integrasi memakai repo git lokal sungguhan sebagai "origin" (bare repo):
clone, revert, konflik, dan push benar-benar dijalankan. Yang dipalsukan hanya
GitHub REST (commit/compare/PR) dan cek Docker Hub.
"""
import json
import subprocess
from pathlib import Path

import pytest

from build_q import github_api, pr_fix_trace, pr_rollback, secret_scan
from build_q.pr_rollback import (
    RollbackError, build_commit_message, build_pr_body, classify_files, extract_images,
    file_kind, parse_rollback_target, rollback_slug, RollbackReport,
)

COMPOSE = "web/production/app1/docker-compose.yaml"
DEPLOY = "cce/production-qoin/svc_deployment.yaml"
SHA = "90729351395603882582763055662896febb6dd6"


# ── parse ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("target", [
    f"https://github.com/Qoin-Digital-Indonesia/gitops/commit/{SHA}",
    f"https://github.com/Qoin-Digital-Indonesia/gitops/commit/{SHA}/",
    f"https://github.com/Qoin-Digital-Indonesia/gitops/commit/{SHA}#diff-abc",
    f"github.com/Qoin-Digital-Indonesia/gitops/commit/{SHA.upper()}",
    f"Qoin-Digital-Indonesia/gitops@{SHA}",
])
def test_parse_target_forms(target):
    assert parse_rollback_target(target) == ("Qoin-Digital-Indonesia/gitops", SHA)


def test_parse_bare_sha_uses_default_repo():
    assert parse_rollback_target("9072935", "Org/gitops") == ("Org/gitops", "9072935")
    with pytest.raises(RollbackError):
        parse_rollback_target("9072935", "")


@pytest.mark.parametrize("bad", ["", "main", "https://github.com/o/r/tree/main",
                                 "https://gitlab.com/o/r/commit/9072935"])
def test_parse_invalid(bad):
    with pytest.raises(RollbackError):
        parse_rollback_target(bad, "Org/gitops")


def test_parse_pr_url_not_yet_supported():
    with pytest.raises(RollbackError, match="Fase 2"):
        parse_rollback_target("https://github.com/o/r/pull/822")


# ── allowlist & penamaan ───────────────────────────────────────────────────

@pytest.mark.parametrize("path,kind", [
    (COMPOSE, "compose"),
    ("web/develop/x/docker-compose.yml", "compose"),
    (DEPLOY, "deploy"),
    ("k8s/production-payout/pay-be-x_deployment.yaml", "deploy"),
    ("cce/production-qoin/svc_services.yaml", None),
    ("cce/production-qoin/kustomization.yaml", None),
    ("web/production/app1/nested/docker-compose.yaml", None),  # `*` tidak melewati `/`
    ("x/cce/production-qoin/svc_deployment.yaml", None),        # harus cocok penuh
    (".github/workflows/x.yml", None),
])
def test_file_kind(path, kind):
    assert file_kind(path) == kind


def test_allowlist_env_override(monkeypatch):
    monkeypatch.setenv("PR_ROLLBACK_ALLOW_DEPLOY", "apps/*/deploy.yaml")
    assert file_kind("apps/a/deploy.yaml") == "deploy"
    assert file_kind(DEPLOY) is None


def test_classify_rejects_added_and_out_of_scope():
    ok, rejected = classify_files([
        {"filename": COMPOSE, "status": "modified"},
        {"filename": DEPLOY, "status": "added"},
        {"filename": "README.md", "status": "modified"},
    ])
    assert ok == [(COMPOSE, "compose")]
    assert [p for p, _ in rejected] == [DEPLOY, "README.md"]


def test_slug():
    assert rollback_slug([(COMPOSE, "compose")]) == "app1"
    assert rollback_slug([(DEPLOY, "deploy")]) == "svc"
    assert rollback_slug([(COMPOSE, "compose"), (DEPLOY, "deploy")]) == "app1-plus1"


def test_extract_images_all_and_quoted():
    text = """services:
  a:
    image: "loyaltolpi/a:v1"  # komentar
  b:
    image: loyaltolpi/b:v2
spec:
  containers:
    - image: loyaltolpi/a:v1
"""
    assert extract_images(text) == ["loyaltolpi/a:v1", "loyaltolpi/b:v2"]


def test_check_image_skips_non_dockerhub_and_digest():
    assert pr_rollback.check_image("swr.ap-southeast-4.myhuaweicloud.com/x/y:v1", {})[0] is None
    assert pr_rollback.check_image("loyaltolpi/y@sha256:abc", {})[0] is None


# ── secret_scan ────────────────────────────────────────────────────────────

def test_secret_scan_keys_without_values():
    text = """    environment:
      PLATFORM_INIT_SECRET: VALUE-ABC-123
      RSA_PRIV_KEY: '-----BEGIN PRIVATE KEY-----\\nAAAA\\n-----END PRIVATE KEY-----'
      RSA_PUB_KEY: '-----BEGIN PUBLIC KEY-----\\nBBBB\\n-----END PUBLIC KEY-----'
      PPOB_ADDITIONAL_FEE_PUB_KEY: ''
      EMPTY_SECRET: ''
      REF_TOKEN: ${REF_TOKEN}
      DB_PASSWORD=hunter2hunter2
      BASE_URL: https://e-wallet-api.example/
"""
    hits = secret_scan.scan(text)
    keys = {h["key"] for h in hits}
    assert keys == {"PLATFORM_INIT_SECRET", "RSA_PRIV_KEY", "DB_PASSWORD"}
    rsa = next(h for h in hits if h["key"] == "RSA_PRIV_KEY")
    assert "kunci privat" in rsa["labels"] and rsa["line"] == 3
    dumped = json.dumps(hits) + "".join(secret_scan.describe(h) for h in hits)
    for value in ("VALUE-ABC-123", "AAAA", "hunter2"):
        assert value not in dumped


def test_bin_scanner_is_shared():
    from build_q import bin as bqbin
    assert bqbin.SECRET_PATTERNS is secret_scan.SECRET_PATTERNS
    assert bqbin._scan_secrets("x = ghp_" + "a" * 30) == secret_scan.scan_patterns("x = ghp_" + "a" * 30)


# ── github_api.list_open_prs: filter head wajib owner:branch ───────────────

def test_list_open_prs_adds_owner_and_filters(monkeypatch):
    seen = {}

    def fake_request(method, path, **kw):
        seen["path"] = path
        return [{"head": {"ref": "other"}, "html_url": "u1"},
                {"head": {"ref": "revert/app1-9072935"}, "html_url": "u2"}]

    monkeypatch.setattr(github_api, "_request", fake_request)
    prs = github_api.list_open_prs("Org/gitops", "revert/app1-9072935")
    assert "head=Org%3Arevert/app1-9072935" in seen["path"]
    assert [p["html_url"] for p in prs] == ["u2"]


# ── pesan commit & body PR ─────────────────────────────────────────────────

def _report(**kw):
    rep = RollbackReport(target="t", repo="Org/gitops", sha=SHA, subject="fix: ganti image",
                         base="main", branch="revert/app1-9072935")
    rep.files = [{"path": COMPOSE, "kind": "compose", "app": "app1",
                  "images_before": ["loyaltolpi/app1:v2"], "images_after": ["loyaltolpi/app1:v1"]}]
    rep.images = [{"image": "loyaltolpi/app1:v1", "ready": True, "note": "ada di Docker Hub"}]
    for k, v in kw.items():
        setattr(rep, k, v)
    return rep


def test_commit_message_has_revert_trailer():
    msg = build_commit_message(_report())
    assert msg.splitlines()[0] == "revert(web/production): app1 → loyaltolpi/app1:v1"
    assert f"This reverts commit {SHA}." in msg


def test_pr_body_lists_secret_keys_only():
    rep = _report(secrets=[{"path": COMPOSE, "line": 7, "key": "TOKEN_MAC_SECRET",
                            "labels": ["nama key sensitif"]}], hints=["redeploy host"])
    body = build_pr_body(rep)
    assert "TOKEN_MAC_SECRET" in body and "baris 7" in body
    assert "| `web/production/app1/docker-compose.yaml` | compose |" in body
    assert "redeploy host" in body


# ── Integrasi: repo git lokal sebagai origin ───────────────────────────────

def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(cwd),
                          check=True, capture_output=True, text=True).stdout.strip()


def _compose(image, extra=""):
    return f"""services:
  app1:
    image: {image}
    environment:
      BASE_URL: https://api.example/
{extra}    ports:
      - "8007:3000"
"""


class Origin:
    """Bare repo + working clone untuk menyusun histori."""

    def __init__(self, root: Path):
        self.bare = root / "origin.git"
        self.work = root / "work"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.bare), str(self.work)], check=True,
                       capture_output=True)
        _git(self.work, "checkout", "-q", "-B", "main")

    def commit(self, files: dict, msg: str) -> str:
        for path, content in files.items():
            p = self.work / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            _git(self.work, "add", path)
        _git(self.work, "commit", "-q", "-m", msg)
        _git(self.work, "push", "-q", "origin", "HEAD")
        return _git(self.work, "rev-parse", "HEAD")

    def rev(self, ref: str) -> str:
        return _git(self.bare, "rev-parse", ref)

    def show(self, ref: str, path: str) -> str:
        return _git(self.bare, "show", f"{ref}:{path}") + "\n"

    def branches(self):
        return _git(self.bare, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()

    # ── fake GitHub REST ──
    def _files(self, a: str, b: str):
        out = _git(self.bare, "diff", "--name-status", a, b)
        kinds = {"M": "modified", "A": "added", "D": "removed"}
        return [{"filename": ln.split("\t")[1], "status": kinds[ln[0]]} for ln in out.splitlines() if ln]

    def get_commit(self, repo, sha):
        full = self.rev(sha)
        parents = _git(self.bare, "rev-list", "--parents", "-n1", full).split()[1:]
        msg = _git(self.bare, "log", "-1", "--format=%B", full)
        files = self._files(parents[0], full) if len(parents) == 1 else []
        return {"sha": full, "parents": [{"sha": p} for p in parents],
                "commit": {"message": msg}, "files": files}

    def compare(self, repo, base, head):
        b, h = self.rev(base), self.rev(head)
        anc = lambda x, y: subprocess.run(["git", "merge-base", "--is-ancestor", x, y],
                                          cwd=str(self.bare)).returncode == 0
        status = "identical" if b == h else "behind" if anc(h, b) else "ahead" if anc(b, h) else "diverged"
        return {"status": status, "files": self._files(b, h)}


@pytest.fixture
def env(tmp_path, monkeypatch):
    origin = Origin(tmp_path)
    gitcfg = tmp_path / "gitconfig"
    gitcfg.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitcfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(pr_fix_trace, "LOCAL_DIR", tmp_path / "runs" / "pr-fix-runs")
    monkeypatch.setattr(pr_fix_trace.pb_api, "is_enabled", lambda: False)
    monkeypatch.setattr(pr_rollback, "_clone_url", lambda repo, token: str(origin.bare))
    monkeypatch.setattr(pr_rollback, "_requested_by", lambda: "tester@example")
    monkeypatch.setattr(github_api, "get_auth_token", lambda: "tok-secret")
    monkeypatch.setattr(github_api, "get_commit", origin.get_commit)
    monkeypatch.setattr(github_api, "compare", origin.compare)
    monkeypatch.setattr(github_api, "list_open_prs", lambda repo, head: [])
    calls = []

    def create_pr(repo, *, base, head, title, body):
        calls.append({"repo": repo, "base": base, "head": head, "title": title, "body": body})
        return {"html_url": "https://github.com/Org/gitops/pull/1"}

    monkeypatch.setattr(github_api, "create_pull_request", create_pr)
    monkeypatch.setattr(pr_rollback, "check_image", lambda image, config: (True, "ada di Docker Hub"))
    origin.pr_calls = calls
    origin.config = {"gitops": {"repo": "Org/gitops", "branch": "main"},
                     "pr_fix": {"base_workdir": str(tmp_path)}}
    origin.tmp = tmp_path
    origin.commit({COMPOSE: _compose("loyaltolpi/app1:v1"), "README.md": "x\n"}, "init")
    return origin


def _run(env, sha, **kw):
    kw.setdefault("assume_yes", True)
    report = env.tmp / "report.json"
    code = pr_rollback.run_pr_rollback(f"Org/gitops@{sha}", config=env.config,
                                       export_json=str(report), **kw)
    return code, json.loads(report.read_text())


def test_rollback_creates_pr_and_never_touches_main(env):
    bad = env.commit({COMPOSE: _compose("loyaltolpi/app1:v2")}, "fix: app1 ke v2")
    main_before = env.rev("main")
    code, rep = _run(env, bad)
    assert code == 0 and rep["status"] == "ok"
    branch = f"revert/app1-{bad[:7]}"
    assert env.rev("main") == main_before
    assert branch in env.branches()
    assert env.show(branch, COMPOSE) == _compose("loyaltolpi/app1:v1")
    msg = _git(env.bare, "log", "-1", "--format=%B", branch)
    assert f"This reverts commit {bad}." in msg
    assert msg.startswith("revert(web/production): app1 → loyaltolpi/app1:v1")
    assert rep["identical_to_parent"] is True
    assert rep["files"][0]["images_before"] == ["loyaltolpi/app1:v2"]
    (pr,) = env.pr_calls
    assert pr["base"] == "main" and pr["head"] == branch
    assert rep["pr_url"].endswith("/pull/1")


def test_dry_run_does_not_push(env):
    bad = env.commit({COMPOSE: _compose("loyaltolpi/app1:v2")}, "fix: v2")
    code, rep = _run(env, bad, dry_run=True)
    assert code == 0 and rep["status"] == "dry_run"
    assert env.branches() == ["main"] and env.pr_calls == []


def test_out_of_scope_rejected_before_clone(env):
    bad = env.commit({"README.md": "y\n"}, "docs")
    code, rep = _run(env, bad)
    assert code == 1 and rep["status"] == "failed"
    assert rep["rejected"][0]["path"] == "README.md"
    assert not list(env.tmp.glob("bq-pr-rollback-*"))


def test_added_file_rejected(env):
    bad = env.commit({DEPLOY: "image: loyaltolpi/svc:v1\n"}, "bootstrap svc")
    code, rep = _run(env, bad)
    assert code == 1 and "added" in rep["rejected"][0]["reason"]


def test_commit_not_in_base(env):
    _git(env.work, "checkout", "-q", "-b", "develop")
    bad = env.commit({COMPOSE: _compose("loyaltolpi/app1:dev")}, "dev only")
    code, rep = _run(env, bad)
    assert code == 1 and "belum ada di branch 'main'" in rep["reason"]
    code, rep = _run(env, bad, base="develop", dry_run=True)
    assert code == 0 and rep["base"] == "develop"


def test_open_pr_dedup(env, monkeypatch):
    bad = env.commit({COMPOSE: _compose("loyaltolpi/app1:v2")}, "fix: v2")
    monkeypatch.setattr(github_api, "list_open_prs",
                        lambda repo, head: [{"html_url": "https://github.com/Org/gitops/pull/9"}])
    code, rep = _run(env, bad)
    assert code == 0 and rep["status"] == "no_change" and rep["pr_url"].endswith("/9")
    assert env.pr_calls == []


def test_already_reverted_dedup(env):
    bad = env.commit({COMPOSE: _compose("loyaltolpi/app1:v2")}, "fix: v2")
    _git(env.work, "revert", "--no-edit", bad)
    _git(env.work, "push", "-q", "origin", "HEAD")
    code, rep = _run(env, bad)
    assert code == 0 and rep["status"] == "no_change" and "sudah di-rollback" in rep["reason"]


def test_conflicting_later_commit_aborts(env):
    bad = env.commit({COMPOSE: _compose("loyaltolpi/app1:v2")}, "fix: v2")
    env.commit({COMPOSE: _compose("loyaltolpi/app1:v3")}, "release v3")
    code, rep = _run(env, bad)
    assert code == 1 and "konflik" in rep["reason"]
    assert len(rep["later_commits"]) == 1 and env.pr_calls == []
    assert env.branches() == ["main"]


def test_non_conflicting_later_commit_kept(env):
    extra = "      FEATURE_FLAG: \"on\"\n"
    bad = env.commit({COMPOSE: _compose("loyaltolpi/app1:v2")}, "fix: v2")
    env.commit({COMPOSE: _compose("loyaltolpi/app1:v2").replace("    ports:", extra + "    ports:")},
               "tambah flag")
    code, rep = _run(env, bad)
    assert code == 0 and rep["identical_to_parent"] is None and len(rep["later_commits"]) == 1
    content = env.show(f"revert/app1-{bad[:7]}", COMPOSE)
    assert "loyaltolpi/app1:v1" in content and "FEATURE_FLAG" in content


def test_missing_image_blocks_unless_skipped(env, monkeypatch):
    monkeypatch.setattr(pr_rollback, "check_image", lambda image, config: (False, "TIDAK ada"))
    bad = env.commit({COMPOSE: _compose("loyaltolpi/app1:v2")}, "fix: v2")
    code, rep = _run(env, bad)
    assert code == 1 and "tidak ada di registry" in rep["reason"] and env.pr_calls == []
    code, rep = _run(env, bad, skip_image_check=True)
    assert code == 0 and rep["status"] == "ok"


def test_non_interactive_without_yes_cancels(env, monkeypatch):
    bad = env.commit({COMPOSE: _compose("loyaltolpi/app1:v2")}, "fix: v2")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code, rep = _run(env, bad, assume_yes=False)
    assert code == 3 and rep["status"] == "cancelled"
    assert env.branches() == ["main"]


def test_merge_commit_reverted_with_m1(env):
    _git(env.work, "checkout", "-q", "-b", "feat")
    env.commit({COMPOSE: _compose("loyaltolpi/app1:v2")}, "fix: v2")
    _git(env.work, "checkout", "-q", "main")
    _git(env.work, "merge", "-q", "--no-ff", "feat", "-m", "Merge pull request #1")
    _git(env.work, "push", "-q", "origin", "main")
    merge = env.rev("main")

    def get_commit(repo, sha):  # GitHub tidak memberi files utk merge → pakai compare
        c = type(env).get_commit(env, repo, sha)
        c["files"] = []
        return c

    import build_q.github_api as gh
    gh_get = gh.get_commit
    gh.get_commit = get_commit
    try:
        code, rep = _run(env, merge)
    finally:
        gh.get_commit = gh_get
    assert code == 0 and rep["merge_commit"] is True and rep["identical_to_parent"] is True
    assert env.show(f"revert/app1-{merge[:7]}", COMPOSE) == _compose("loyaltolpi/app1:v1")


def test_secret_reported_without_value_and_token_not_leaked(env):
    secret_line = "      TOKEN_MAC_SECRET: Sup3rSecretValue99\n"
    env.commit({COMPOSE: _compose("loyaltolpi/app1:v1", secret_line)}, "pakai secret")
    bad = env.commit({COMPOSE: _compose("loyaltolpi/app1:v2")}, "hapus secret + v2")
    code, rep = _run(env, bad)
    assert code == 0
    assert [s["key"] for s in rep["secrets"]] == ["TOKEN_MAC_SECRET"]
    dumped = json.dumps(rep) + env.pr_calls[0]["body"]
    assert "Sup3rSecretValue99" not in dumped
    assert "tok-secret" not in dumped
    trace = next((env.tmp / "runs" / "pr-rollback-runs").glob("*.jsonl")).read_text()
    assert "Sup3rSecretValue99" not in trace and "tok-secret" not in trace


def test_export_json_stdout_is_pure_json(env, capsys):
    bad = env.commit({COMPOSE: _compose("loyaltolpi/app1:v2")}, "fix: v2")
    code = pr_rollback.run_pr_rollback(f"Org/gitops@{bad}", config=env.config,
                                       dry_run=True, export_json="-")
    out = capsys.readouterr().out
    assert code == 0 and json.loads(out)["status"] == "dry_run"


def test_trace_written_locally(env):
    bad = env.commit({COMPOSE: _compose("loyaltolpi/app1:v2")}, "fix: v2")
    pr_rollback.run_pr_rollback(f"Org/gitops@{bad}", config=env.config, dry_run=True, run_id="rid-1")
    rows = [json.loads(ln) for ln in
            (env.tmp / "runs" / "pr-rollback-runs" / "rid-1.jsonl").read_text().splitlines()]
    steps = [r["step"] for r in rows if r["type"] == "step"]
    assert steps[:4] == ["parse", "preflight", "resolve_commit", "scope"]
    assert rows[-1]["status"] == "dry_run" and rows[-1]["report"]["branch"].startswith("revert/app1-")
    assert rows[0]["ref"] == bad


def test_redact_hides_token_in_git_errors():
    err = "fatal: unable to access 'https://x-access-token:ghs_ABC123@github.com/Org/gitops.git/': 403"
    out = pr_rollback._redact(err, "ghs_ABC123")
    assert "ghs_ABC123" not in out and "x-access-token:***@" in out
