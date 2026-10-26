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
