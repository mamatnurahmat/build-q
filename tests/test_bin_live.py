"""Tes hook dan aturan akses `bq bin` terhadap PocketBase lokal (PRD-bin F1.5).

Dilewati kecuali env berikut diisi. Hanya boleh ke loopback; PocketBase produksi ditolak.
    BQ_BIN_LIVE_PB_URL=http://127.0.0.1:8193
    BQ_BIN_LIVE_SU_EMAIL=<email superuser lokal>
    BQ_BIN_LIVE_SU_PASS=<password superuser lokal>
    BQ_BIN_LIVE_USER_EMAIL / BQ_BIN_LIVE_USER_PASS   (opsional: akun login biasa untuk uji akses)
Semua record uji bernama test-bin-* (huruf kecil, sesuai pola nama PocketBase) dihapus di akhir.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

PB_URL = os.getenv("BQ_BIN_LIVE_PB_URL", "").rstrip("/")
SU_EMAIL = os.getenv("BQ_BIN_LIVE_SU_EMAIL", "")
SU_PASS = os.getenv("BQ_BIN_LIVE_SU_PASS", "")

pytestmark = pytest.mark.skipif(not (PB_URL and SU_EMAIL and SU_PASS), reason="tes live PocketBase tidak diaktifkan")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _call(method, path, token=None, body=None, ctype=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(PB_URL + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _multipart(fields: dict, data: bytes) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    buf = bytearray()
    for k, v in fields.items():
        buf += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    buf += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="x.sh"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    return bytes(buf), f"multipart/form-data; boundary={b}"


@pytest.fixture(scope="module")
def su():
    assert PB_URL.startswith(("http://127.0.0.1", "http://localhost")), "tes live hanya boleh ke loopback"
    status, body = _call("POST", "/api/collections/_superusers/auth-with-password",
                         body=json.dumps({"identity": SU_EMAIL, "password": SU_PASS}).encode(),
                         ctype="application/json")
    assert status == 200, status
    token = json.loads(body)["token"]
    yield token
    status, body = _call("GET", "/api/collections/bq_bin/records?perPage=500&filter=" +
                         urllib.parse.quote('name~"test-bin-"'), token)
    for r in json.loads(body).get("items", []):
        _call("DELETE", f"/api/collections/bq_bin/records/{r['id']}", token)


def _create(su, name: str, data: bytes, claimed: str):
    body, ctype = _multipart({"name": name, "sha256": claimed, "mode": "0755", "kind": "bash"}, data)
    return _call("POST", "/api/collections/bq_bin/records", su, body, ctype)


def test_create_bad_sha_rejected_and_removed(su):
    name = "test-bin-" + uuid.uuid4().hex[:8]
    status, _ = _create(su, name, b"echo a\n", "0" * 64)
    assert status == 400
    status, body = _call("GET", "/api/collections/bq_bin/records?filter=" +
                         urllib.parse.quote(f'name="{name}"'), su)
    assert json.loads(body)["totalItems"] == 0


def test_update_bad_sha_keeps_record_and_old_content(su):
    name = "test-bin-" + uuid.uuid4().hex[:8]
    old, new = b"echo lama\n", b"echo baru\n"
    status, body = _create(su, name, old, _sha(old))
    assert status == 200, body
    rec = json.loads(body)
    body_req, ctype = _multipart({"notes": "coba", "sha256": "0" * 64, "size": str(len(new))}, new)
    status, _ = _call("PATCH", f"/api/collections/bq_bin/records/{rec['id']}", su, body_req, ctype)
    assert status == 400
    status, body = _call("GET", f"/api/collections/bq_bin/records/{rec['id']}", su)
    got = json.loads(body)
    assert got["sha256"] == _sha(old) and got["notes"] == "" and got["file"] == rec["file"]


def test_update_good_sha_replaces_content(su):
    name = "test-bin-" + uuid.uuid4().hex[:8]
    old, new = b"echo satu\n", b"echo dua\n"
    status, body = _create(su, name, old, _sha(old))
    rec = json.loads(body)
    body_req, ctype = _multipart({"sha256": _sha(new), "size": str(len(new))}, new)
    status, _ = _call("PATCH", f"/api/collections/bq_bin/records/{rec['id']}", su, body_req, ctype)
    assert status == 200
    status, body = _call("GET", f"/api/collections/bq_bin/records/{rec['id']}", su)
    got = json.loads(body)
    assert got["sha256"] == _sha(new) and got["size"] == len(new)


def test_file_download_needs_login_or_file_token(su):
    name = "test-bin-" + uuid.uuid4().hex[:8]
    data = b"echo prot\n"
    status, body = _create(su, name, data, _sha(data))
    rec = json.loads(body)
    path = f"/api/files/{rec['collectionId']}/{rec['id']}/{rec['file']}"
    status, _ = _call("GET", path)
    assert status in (401, 403, 404), status
    status, body = _call("POST", "/api/files/token", su, b"{}", "application/json")
    assert status == 200
    ftoken = json.loads(body)["token"]
    status, body = _call("GET", f"{path}?token={urllib.parse.quote(ftoken)}")
    assert status == 200 and body == data
