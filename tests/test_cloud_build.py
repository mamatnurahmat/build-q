"""Test `bq --cloud` (build_q/cloud_build.py) — tanpa jaringan & tanpa Docker sungguhan.

buildx dan docker dipalsukan dengan skrip bash di PATH, sehingga yang diuji nyata:
urutan login → buat builder → build, kredensial lewat stdin, dan argumen yang dikirim.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from build_q import cloud_build
from build_q.builder import BuildError

DH_USER = "newrahmat"
DH_TOKEN = "dckr_pat_TESTONLY_0123456789abcdef"
GH_TOKEN = "ghp_TESTONLY_gh_token_value"


def _exe(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def fake_config():
    return {
        "builder": {"name": "x", "memory": "4g", "cpu_period": "100000", "cpu_quota": "200000"},
        "registry": {"url": "loyaltolpi"},
        "gitops": {"repo": "", "branch": "main", "infra": "cce", "ns_suffix": "qoin"},
    }


@pytest.fixture
def stubs(monkeypatch, tmp_path, fake_config):
    """buildx & docker palsu di PATH; PB, config, dan registry dimatikan."""
    log = tmp_path / "log"
    log.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    buildx = bin_dir / "buildx"
    _exe(buildx, f"""
case "$1" in
  create)
    if [ "$2" = "--help" ]; then echo '      --driver string  Driver to use (available: "cloud", "docker-container")'; exit 0; fi
    echo "buildx $*" >> "{log}/calls"; exit 0 ;;
  inspect) echo "buildx $*" >> "{log}/calls"; exit 1 ;;
  build)   echo "buildx $*" >> "{log}/calls"; echo "$DOCKER_CONFIG" > "{log}/build_cfg"; exit 0 ;;
esac
exit 1
""")
    docker = bin_dir / "docker"
    _exe(docker, f"""
case "$1" in
  login) cat > "{log}/login_stdin"; echo "docker $*" >> "{log}/calls"; echo "$DOCKER_CONFIG" > "{log}/login_cfg"; exit 0 ;;
  logout) echo "docker $*" >> "{log}/calls"; exit 0 ;;
  manifest) echo "docker $*" >> "{log}/calls"; exit ${{MANIFEST_RC:-1}} ;;
esac
exit 1
""")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(cloud_build, "load_config", lambda: fake_config)
    monkeypatch.setattr(cloud_build, "load_dockerhub_creds", lambda: (DH_USER, DH_TOKEN))
    monkeypatch.setattr(cloud_build, "_github_token", lambda: GH_TOKEN)
    monkeypatch.setattr(cloud_build._builder, "check_image_exists", lambda tag: False)
    monkeypatch.setenv("MANIFEST_RC", "1")  # default: image belum ada di Docker Hub
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_USER", "ghuser")
    monkeypatch.setattr(cloud_build, "CONFIG_DIR", tmp_path / "cfg")
    return {"log": log, "buildx": str(buildx), "bin": bin_dir}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "svc"
    r.mkdir()
    (r / "Dockerfile").write_text("FROM scratch\n")
    monkeypatch.chdir(r)
    return r


# ── helper murni ───────────────────────────────────────────────────────────

def test_endpoint_and_local_name():
    assert cloud_build.endpoint_to_local_name("loyaltolpi/qoin-build") == "cloud-loyaltolpi-qoin-build"
    assert cloud_build.resolve_endpoint("a/b") == "a/b"
    with pytest.raises(BuildError, match="<org>/<builder>"):
        cloud_build.resolve_endpoint("tanpa-slash")


def test_to_buildx_argv_replaces_prefix_and_hides_gh_values():
    cmd = ["docker", "buildx", "build", "--builder", "b",
           "--build-arg", "GITHUB_TOKEN=ghp_SECRET", "--build-arg", "PORT=8080",
           "--secret", "id=netrc,src=/Users/x/.netrc", "-t", "img", "."]
    out = cloud_build.to_buildx_argv(cmd, "/path/buildx")
    assert out[:2] == ["/path/buildx", "build"]
    assert "ghp_SECRET" not in out
    i = out.index("--build-arg")
    assert out[i + 1] == "GITHUB_TOKEN"
    assert "PORT=8080" in out
    assert any(x == "id=netrc,src=/Users/x/.netrc" for x in out)  # build lokal → secret file tetap dipakai


def test_to_buildx_argv_rejects_unknown_format():
    with pytest.raises(BuildError, match="tidak dikenali"):
        cloud_build.to_buildx_argv(["podman", "build"], "/b")


def test_resolve_buildx_requires_cloud_driver(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_build, "CONFIG_DIR", tmp_path / "none")
    monkeypatch.delenv("BQ_BUILDX_BIN", raising=False)
    bad = tmp_path / "buildx-bawaan"
    _exe(bad, 'echo \'Driver (available: "docker-container")\'; exit 0\n')
    with pytest.raises(BuildError, match="driver cloud tidak ditemukan"):
        cloud_build.resolve_buildx(str(bad))


def test_resolve_buildx_accepts_official(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_build, "CONFIG_DIR", tmp_path / "none")
    good = tmp_path / "buildx-resmi"
    _exe(good, 'echo \'Driver (available: "cloud", "docker-container")\'; exit 0\n')
    assert cloud_build.resolve_buildx(str(good)) == str(good)


# ── alur build (buildx & docker palsu) ─────────────────────────────────────

def _calls(stubs):
    f = stubs["log"] / "calls"
    return f.read_text().splitlines() if f.exists() else []


def test_full_flow_login_create_build_cleanup(repo, stubs):
    rc = cloud_build.run_cloud_build("svc", "main", cicd_dict={"IMAGE": "svc"},
                                     buildx_bin=stubs["buildx"], image_check=False)
    assert rc == 0
    log = stubs["log"]

    # 1) token Docker lewat stdin login, bukan argv
    assert (log / "login_stdin").read_text() == DH_TOKEN
    calls = _calls(stubs)
    assert all(DH_TOKEN not in c and GH_TOKEN not in c for c in calls)
    assert f"docker login -u {DH_USER} --password-stdin" in calls

    # 2) builder cloud dibuat karena inspect gagal, dengan endpoint org/name
    assert any(c == "buildx create --driver cloud --name cloud-loyaltolpi-qoin-build loyaltolpi/qoin-build"
               for c in calls)

    # 3) build: builder cloud, push, token GitHub tidak di argv
    build = [c for c in calls if c.startswith("buildx build")][0]
    assert build.startswith("buildx build --builder cloud-loyaltolpi-qoin-build")
    assert "build build" not in build
    assert "--push" in build
    assert "--build-arg GITHUB_TOKEN" in build
    assert GH_TOKEN not in build

    # 4) DOCKER_CONFIG sementara dipakai lalu dihapus; logout dipanggil
    tmp_cfg = (log / "build_cfg").read_text().strip()
    assert tmp_cfg and not Path(tmp_cfg).exists()
    assert "docker logout" in calls


def test_no_push_flag_propagates(repo, stubs):
    cloud_build.run_cloud_build("svc", "main", cicd_dict={"IMAGE": "svc"},
                                buildx_bin=stubs["buildx"], image_check=False, push=False)
    build = [c for c in _calls(stubs) if c.startswith("buildx build")][0]
    assert "--push" not in build


def test_existing_builder_not_recreated(repo, stubs):
    _exe(Path(stubs["buildx"]), f"""
case "$1" in
  create) if [ "$2" = "--help" ]; then echo '"cloud"'; exit 0; fi; echo "buildx $*" >> "{stubs['log']}/calls"; exit 0 ;;
  inspect) exit 0 ;;
  build) echo "buildx $*" >> "{stubs['log']}/calls"; exit 0 ;;
esac
exit 1
""")
    cloud_build.run_cloud_build("svc", "main", cicd_dict={"IMAGE": "svc"},
                                buildx_bin=stubs["buildx"], image_check=False)
    assert not any(c.startswith("buildx create") for c in _calls(stubs))


def test_dry_run_touches_nothing_and_hides_credentials(repo, stubs, capsys, monkeypatch):
    def boom():
        raise AssertionError("tidak boleh membaca kredensial pada dry-run")
    monkeypatch.setattr(cloud_build, "load_dockerhub_creds", boom)
    rc = cloud_build.run_cloud_build("svc", "main", cicd_dict={"IMAGE": "svc"},
                                     image_check=False, dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "--builder cloud-loyaltolpi-qoin-build" in out
    assert DH_TOKEN not in out and GH_TOKEN not in out
    assert not (stubs["log"] / "calls").exists()


def test_missing_dockerhub_creds_is_clear_error(repo, stubs, monkeypatch):
    def boom():
        raise cloud_build.CloudBuildError("kredensial Docker Hub tidak ditemukan (x)")
    monkeypatch.setattr(cloud_build, "load_dockerhub_creds", boom)
    with pytest.raises(BuildError, match="kredensial Docker Hub"):
        cloud_build.run_cloud_build("svc", "main", cicd_dict={"IMAGE": "svc"},
                                    buildx_bin=stubs["buildx"], image_check=False)


def test_login_failure_stops_before_build(repo, stubs):
    _exe(stubs["bin"] / "docker", 'case "$1" in login) cat >/dev/null; echo denied >&2; exit 1;; logout) exit 0;; esac\nexit 1\n')
    with pytest.raises(BuildError, match="docker login Docker Hub gagal"):
        cloud_build.run_cloud_build("svc", "main", cicd_dict={"IMAGE": "svc"},
                                    buildx_bin=stubs["buildx"], image_check=False)
    assert not any(c.startswith("buildx build") for c in _calls(stubs))


# ── skip / --rebuild / context remote ──────────────────────────────────────

def _builds(stubs):
    return [c for c in _calls(stubs) if c.startswith("buildx build")]


def test_skip_when_image_ready_on_hub(repo, stubs, monkeypatch, capsys):
    monkeypatch.setenv("MANIFEST_RC", "0")
    rc = cloud_build.run_cloud_build("svc", "main", cicd_dict={"IMAGE": "svc"},
                                     buildx_bin=stubs["buildx"])
    assert rc == 0
    assert _builds(stubs) == []  # tidak ada build
    assert any(c.startswith("docker manifest inspect") for c in _calls(stubs))  # cek pakai kredensial PB
    assert "Skipping build" in capsys.readouterr().out


def test_builds_when_image_missing(repo, stubs, monkeypatch):
    monkeypatch.setenv("MANIFEST_RC", "1")
    assert cloud_build.run_cloud_build("svc", "main", cicd_dict={"IMAGE": "svc"},
                                       buildx_bin=stubs["buildx"]) == 0
    assert len(_builds(stubs)) == 1


def test_rebuild_bypasses_check(repo, stubs, monkeypatch):
    monkeypatch.setenv("MANIFEST_RC", "0")  # image ada, tapi --rebuild
    assert cloud_build.run_cloud_build("svc", "main", cicd_dict={"IMAGE": "svc"},
                                       buildx_bin=stubs["buildx"], image_check=False) == 0
    assert len(_builds(stubs)) == 1
    assert not any(c.startswith("docker manifest") for c in _calls(stubs))


def test_remote_context_and_secrets_pass_through(repo, stubs):
    ctx = "https://github.com/Qoin-Digital-Indonesia/svc.git#sandbox"
    cloud_build.run_cloud_build("svc", "sandbox", cicd_dict={"IMAGE": "svc"}, buildx_bin=stubs["buildx"],
                                image_check=False, context=ctx,
                                secrets=["id=GIT_AUTH_TOKEN,env=GIT_AUTH_TOKEN"])
    build = _builds(stubs)[0]
    assert build.endswith(ctx)
    assert "id=GIT_AUTH_TOKEN,env=GIT_AUTH_TOKEN" in build
