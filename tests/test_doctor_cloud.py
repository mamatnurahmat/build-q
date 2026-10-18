"""Test `bq --doctor --cloud` — kesiapan build cloud & saran setup (tanpa jaringan)."""
from __future__ import annotations

import pytest

from build_q import cloud_build, doctor
from build_q.builder import BuildError


def test_buildx_asset_name_format(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    assert doctor._buildx_asset("v0.38.0") == "buildx-v0.38.0.darwin-arm64"
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    assert doctor._buildx_asset("v0.38.0") == "buildx-v0.38.0.linux-amd64"


def test_missing_buildx_and_creds_are_blockers(monkeypatch):
    def no_buildx(*a, **k):
        raise BuildError("tidak ada")

    def no_creds():
        raise cloud_build.CloudBuildError("kredensial tidak ada")

    monkeypatch.setattr(cloud_build, "resolve_buildx", no_buildx)
    monkeypatch.setattr(cloud_build, "load_dockerhub_creds", no_creds)
    res = doctor._check_cloud_build()
    labels = {r[0]: r for r in res}
    assert labels["Cloud: buildx resmi (driver cloud)"][1] is False
    assert labels["Cloud: buildx resmi (driver cloud)"][3] is True  # blocker
    assert labels["Cloud: kredensial Docker Hub"][1] is False
    # tanpa kredensial tidak ada login/builder check (tidak ada panggilan docker)
    assert not any(r[0] == "Cloud: login Docker Hub" for r in res)


def test_suggestions_show_setup_steps(monkeypatch, capsys):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    results = [
        ("Cloud: buildx resmi (driver cloud)", False, "x", True),
        ("Cloud: kredensial Docker Hub", False, "y", True),
        ("Cloud: builder cloud-loyaltolpi-qoin-build", False, "belum ada", False),
    ]
    doctor._print_cloud_suggestions(results)
    out = capsys.readouterr().out
    assert "buildx-v0.38.0.darwin-arm64" in out
    assert "hub.docker.com/settings/personal-access-tokens" in out
    assert "--driver cloud" in out and "loyaltolpi/qoin-build" in out


def test_no_suggestions_when_all_ok(capsys):
    doctor._print_cloud_suggestions([("Cloud: x", True, "ok", False)])
    assert capsys.readouterr().out == ""


def test_run_doctor_without_cloud_skips_section(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_check_cloud_build", lambda: pytest.fail("tidak boleh dipanggil"))
    for name in ("_check_tools", "_check_config", "_check_pb_api", "_check_github_credentials",
                 "_check_docker_login", "_check_buildx_builder", "_check_kube_context"):
        monkeypatch.setattr(doctor, name, lambda: [])
    monkeypatch.setattr(doctor, "_run_fixers", lambda sections: 0)
    doctor.run_doctor(cloud=False)
    assert "Build cloud" not in capsys.readouterr().out
