"""Test `bq bin` fase 1 (PRD-bin F1) — tanpa jaringan; PB dan BIN_DIR dipalsu."""
from __future__ import annotations

import hashlib

import pytest

from build_q import bin as bqbin
from build_q.pb_api import PBAPIError


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setattr(bqbin, "BIN_DIR", d)
    monkeypatch.setattr(bqbin, "BAK_DIR", d / ".bak")
    monkeypatch.setattr(bqbin, "TMP_DIR", d / ".tmp")
    return d


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_valid_name():
    assert bqbin._valid_name("gh-clone")
    assert bqbin._valid_name("a.b_c-1")
    assert not bqbin._valid_name("../etc")
    assert not bqbin._valid_name("Gh-Clone")
    assert not bqbin._valid_name(".hidden")
    assert not bqbin._valid_name("x" * 65)


def test_scan_reports_label_and_line_not_value():
    text = "echo hi\nTOKEN=ghp_" + "A" * 36 + "\n"
    hits = bqbin._scan_secrets(text)
    assert ("token GitHub", 2) in hits or ("penugasan rahasia", 2) in hits
    assert all("A" * 10 not in label for label, _ in hits)


def test_scan_allows_shell_references():
    text = 'TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"\nTOKEN=$(gh auth token)\n'
    assert bqbin._scan_secrets(text) == []


def test_is_text():
    assert bqbin._is_text(b"#!/bin/bash\necho ok\n")
    assert not bqbin._is_text(b"\x00\x01binary")
    assert not bqbin._is_text("ÿ".encode("latin-1"))  # bukan UTF-8 valid


def test_multipart_contains_fields_and_file():
    body, ctype = bqbin._multipart({"name": "x", "sha256": "abc"}, "x", b"DATA")
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="sha256"' in body and b"abc" in body
    assert b'filename="x"' in body and b"DATA" in body


def test_check_statuses(bin_dir, monkeypatch):
    same = b"echo same\n"
    (bin_dir / "same").write_bytes(same)
    (bin_dir / "beda").write_bytes(b"lokal\n")
    (bin_dir / "hanya-lokal").write_bytes(b"x\n")
    (bin_dir / "besar").write_bytes(b"0" * (bqbin.MAX_SIZE + 1))
    remote = [
        {"name": "same", "sha256": _sha(same)},
        {"name": "beda", "sha256": _sha(b"pocketbase\n")},
        {"name": "hanya-pb", "sha256": _sha(b"y")},
        {"name": "besar", "sha256": _sha(b"z")},
    ]
    monkeypatch.setattr(bqbin, "_list_records", lambda token: remote)
    rows = {r["name"]: r for r in bqbin._compare("tok")}
    assert rows["same"]["status"] == "MATCH"
    assert rows["beda"]["status"] == "BEDA"
    assert rows["hanya-lokal"]["status"] == "HANYA DI LOKAL"
    assert rows["hanya-pb"]["status"] == "TIDAK ADA DI LOKAL"
    assert rows["besar"]["status"] == "DILEWATI (ukuran)"
    assert rows["beda"]["saran"] == "bq bin pull beda"


def test_check_is_read_only(bin_dir, monkeypatch):
    f = bin_dir / "tool"
    f.write_bytes(b"lokal\n")
    monkeypatch.setattr(bqbin, "_list_records", lambda token: [{"name": "tool", "sha256": _sha(b"pb\n")}])
    before = f.read_bytes()
    bqbin._compare("tok")
    assert f.read_bytes() == before
    assert not (bin_dir / ".bak").exists()


def test_pull_rejects_sha_mismatch_and_keeps_local(bin_dir, monkeypatch):
    f = bin_dir / "tool"
    f.write_bytes(b"lokal\n")
    rec = {"name": "tool", "sha256": _sha(b"benar\n"), "mode": "0755", "file": "tool_x.sh", "id": "r1", "collectionId": "c"}
    monkeypatch.setattr(bqbin, "_list_records", lambda token: [rec])
    monkeypatch.setattr(bqbin, "_token", lambda: "tok")
    monkeypatch.setattr(bqbin, "_download", lambda record, token: b"rusak\n")
    import argparse
    args = argparse.Namespace(all=False, names=["tool"], yes=True)
    assert bqbin.cmd_pull(args) == bqbin.EXIT_HASH
    assert f.read_bytes() == b"lokal\n"
    assert not (bin_dir / ".bak").exists()


def test_pull_replaces_with_backup_and_mode(bin_dir, monkeypatch):
    f = bin_dir / "tool"
    f.write_bytes(b"lama\n")
    data = b"#!/bin/bash\necho baru\n"
    rec = {"name": "tool", "sha256": _sha(data), "mode": "0644", "file": "tool_x.sh", "id": "r1", "collectionId": "c"}
    monkeypatch.setattr(bqbin, "_list_records", lambda token: [rec])
    monkeypatch.setattr(bqbin, "_token", lambda: "tok")
    monkeypatch.setattr(bqbin, "_download", lambda record, token: data)
    import argparse
    assert bqbin.cmd_pull(argparse.Namespace(all=False, names=["tool"], yes=True)) == 0
    assert f.read_bytes() == data
    assert (f.stat().st_mode & 0o777) == 0o644
    backups = list((bin_dir / ".bak").iterdir())
    assert len(backups) == 1 and backups[0].read_bytes() == b"lama\n"
    assert not any((bin_dir / ".tmp").iterdir())


def test_pull_refuses_invalid_remote_name(bin_dir, monkeypatch):
    rec = {"name": "../escape", "sha256": _sha(b"x"), "mode": "0755", "file": "f", "id": "r", "collectionId": "c"}
    monkeypatch.setattr(bqbin, "_list_records", lambda token: [rec])
    monkeypatch.setattr(bqbin, "_token", lambda: "tok")
    import argparse
    assert bqbin.cmd_pull(argparse.Namespace(all=False, names=["../escape"], yes=True)) != 0
    assert not (bin_dir.parent / "escape").exists()


def test_pb_unreachable_exit_code(bin_dir, monkeypatch, capsys):
    monkeypatch.setattr(bqbin, "_token", lambda: "tok")
    def boom(token):
        raise PBAPIError("jaringan: refused")
    monkeypatch.setattr(bqbin, "_list_records", boom)
    import argparse
    assert bqbin.cmd_check(argparse.Namespace(json=False)) == bqbin.EXIT_PB


def test_valid_name_rejects_trailing_newline():
    assert not bqbin._valid_name("tool\n")


def test_system_names_reserved(tmp_path, monkeypatch):
    assert bqbin._is_system_name("git")
    assert bqbin._is_system_name("bq")
    assert not bqbin._is_system_name("gh-clone")
    sysdir = tmp_path / "sys"
    sysdir.mkdir()
    (sysdir / "mytool").write_text("x")
    monkeypatch.setattr(bqbin, "SYSTEM_DIRS", (str(sysdir),))
    assert bqbin._is_system_name("mytool")


def test_push_refuses_system_name(tmp_path, monkeypatch):
    f = tmp_path / "git"
    f.write_bytes(b"#!/bin/bash\necho hi\n")
    monkeypatch.setattr(bqbin.getpass, "getpass", lambda *a: (_ for _ in ()).throw(AssertionError("prompt")))
    import argparse
    args = argparse.Namespace(file=str(f), mode=None, kind=None, version=None, notes=None, email=None, yes=True)
    assert bqbin.cmd_push(args) == 1


def test_pull_refuses_system_name_without_download(bin_dir, monkeypatch):
    rec = {"name": "curl", "sha256": _sha(b"x"), "mode": "0755", "file": "f", "id": "r", "collectionId": "c"}
    monkeypatch.setattr(bqbin, "_list_records", lambda token: [rec])
    monkeypatch.setattr(bqbin, "_token", lambda: "tok")
    def no_download(record, token):
        raise AssertionError("tidak boleh mengunduh")
    monkeypatch.setattr(bqbin, "_download", no_download)
    import argparse
    assert bqbin.cmd_pull(argparse.Namespace(all=False, names=["curl"], yes=True)) == 1
    assert not (bin_dir / "curl").exists()


def test_check_flags_system_name(bin_dir, monkeypatch):
    (bin_dir / "ssh").write_bytes(b"lokal\n")
    monkeypatch.setattr(bqbin, "_list_records", lambda token: [])
    rows = {r["name"]: r for r in bqbin._compare("tok")}
    assert rows["ssh"]["sistem"] is True


def test_require_secure_url():
    from build_q import pb_api
    assert pb_api.require_secure_url("https://cicd-hw.qoin.id/devops")
    assert pb_api.require_secure_url("http://127.0.0.1:8193")
    assert pb_api.require_secure_url("http://localhost:8193")
    for bad in ("http://cicd-hw.qoin.id/devops", "http://10.0.0.5:8090", "ftp://127.0.0.1/x"):
        with pytest.raises(PBAPIError):
            pb_api.require_secure_url(bad)


def test_bak_symlink_refused(bin_dir, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (bin_dir / "tool").write_bytes(b"lama\n")
    (bin_dir / ".bak").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        bqbin._write_atomic("tool", b"baru\n", 0o755)
    assert (bin_dir / "tool").read_bytes() == b"lama\n"
    assert list(outside.iterdir()) == []


def test_scan_new_patterns_flagged():
    samples = {
        "glpat-": "TOKEN_X=glpat-" + "A" * 20 + "\n",
        "sk_live_": "STRIPE=sk_live_" + "A" * 16 + "\n",
        "AIza": "KEY=AIza" + "A" * 35 + "\n",
        "dckr_pat_": "DOCKER=dckr_pat_" + "A" * 20 + "\n",
        "URL": "curl https://user:pass1234@host.example/x\n",
        "basic auth": "curl -u alice:secretpw http://h\n",
        "Authorization": "H='Authorization: Basic abcdEFGH1234'\n",
        "--password": "login --password=hunter22\n",
    }
    for key, text in samples.items():
        assert bqbin._scan_secrets(text), key


def test_scan_allows_non_secret_shell_forms():
    text = ('docker login --password-stdin\ncurl -u "$USER:$PASS" https://h\nmake --token $X\n'
            'URL="https://x-access-token:${TOKEN}@github.com/o/r.git"\n')
    assert bqbin._scan_secrets(text) == []


def test_download_sends_file_token(monkeypatch):
    seen = {}
    monkeypatch.setattr(bqbin.pb_api, "_http_json", lambda *a, **k: {"token": "ft123"})
    def fake_raw(method, url, token, body=None, content_type=None):
        seen["url"] = url
        return b"isi"
    monkeypatch.setattr(bqbin, "_raw", fake_raw)
    rec = {"file": "tool_x.sh", "id": "r1", "collectionId": "c1"}
    assert bqbin._download(rec, "tok") == b"isi"
    assert seen["url"].endswith("/api/files/c1/r1/tool_x.sh?token=ft123")
