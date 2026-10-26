"""`bq bin` — sinkron `~/.build-q/bin` dengan collection PocketBase `bq_bin` (fase 1, PRD-bin F1).

Fase 1 hanya memindahkan berkas. `bq` TIDAK mengeksekusi isi tool; tool dijalankan
langsung lewat PATH oleh pemakainya.

    bq bin ls                        daftar tool di PocketBase
    bq bin check [--json]            bandingkan lokal dengan PocketBase (tidak mengubah berkas)
    bq bin pull <name> [--yes]       unduh satu tool (backup, ganti atomik)
    bq bin pull --all [--yes]        unduh semua tool yang berbeda
    bq bin push <file> [--mode 0755] [--kind bash|python|binary] [--version V] [--notes T] [--email E] [--yes]
                                     unggah (hanya superuser; pola rahasia ditolak)

Exit: check 0 = semua cocok, 1 = ada beda, 2 = PocketBase tidak terjangkau.
      pull/push 122 = sha256 tidak cocok (tidak ada berkas yang diganti).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

from . import pb_api
from .pb_api import PBAPIError

COLLECTION = "bq_bin"
MAX_SIZE = 512 * 1024
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MODES = ("0644", "0755")
KINDS = ("bash", "python", "binary")
EXIT_OK, EXIT_DIFF, EXIT_PB, EXIT_HASH = 0, 1, 2, 122

BIN_DIR = pb_api.CONFIG_DIR / "bin"
BAK_DIR = BIN_DIR / ".bak"
TMP_DIR = BIN_DIR / ".tmp"

# Pola rahasia untuk `push`. Hanya label yang dicetak, tidak pernah nilainya.
SECRET_PATTERNS = [
    ("token GitHub", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("token Slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("kunci akses AWS", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("kunci privat", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("penugasan rahasia", re.compile(
        r"^\s*(?:export\s+)?[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASS|APIKEY|API_KEY|KEY)[A-Za-z0-9_]*"
        r"\s*=\s*['\"]?[^\s'\"$]{8,}",
        re.M | re.I,
    )),
]


# ── helper ────────────────────────────────────────────────────────────────

def _base() -> str:
    return pb_api._base_url()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name))


def _raw(method: str, url: str, token: str, body: Optional[bytes] = None,
         content_type: Optional[str] = None) -> bytes:
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", errors="replace")).get("message") or e.reason
        except Exception:  # noqa: BLE001 — pesan galat server hanya informasi
            msg = e.reason
        raise PBAPIError(f"HTTP {e.code}: {msg}") from e
    except urllib.error.URLError as e:
        raise PBAPIError(f"jaringan: {e.reason}") from e


def _token() -> str:
    return pb_api.get_token(_base())


def _list_records(token: str) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        url = f"{_base()}/api/collections/{COLLECTION}/records?perPage=500&page={page}"
        data = pb_api._http_json("GET", url, headers={"Authorization": f"Bearer {token}"})
        items.extend(data.get("items", []))
        if page >= int(data.get("totalPages") or 1):
            return items
        page += 1


def _find_record(token: str, name: str) -> Optional[dict]:
    flt = urllib.parse.quote(f'name="{name}"', safe="")
    url = f"{_base()}/api/collections/{COLLECTION}/records?perPage=1&filter={flt}"
    data = pb_api._http_json("GET", url, headers={"Authorization": f"Bearer {token}"})
    items = data.get("items") or []
    return items[0] if items else None


def _download(record: dict, token: str) -> bytes:
    fname = urllib.parse.quote(record["file"])
    url = f"{_base()}/api/files/{record['collectionId']}/{record['id']}/{fname}"
    return _raw("GET", url, token)


def _local_files() -> dict[str, Path]:
    """Berkas biasa di BIN_DIR. Direktori tersembunyi (.bak, .tmp) dan berkas titik diabaikan."""
    out: dict[str, Path] = {}
    if not BIN_DIR.is_dir():
        return out
    for p in sorted(BIN_DIR.iterdir()):
        if p.name.startswith(".") or p.is_symlink() or not p.is_file():
            continue
        out[p.name] = p
    return out


def _is_text(data: bytes) -> bool:
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _scan_secrets(text: str) -> list[tuple[str, int]]:
    """Kembalikan (label pola, nomor baris). Nilai rahasia tidak pernah dikembalikan."""
    hits: list[tuple[str, int]] = []
    for label, rx in SECRET_PATTERNS:
        for m in rx.finditer(text):
            hits.append((label, text.count("\n", 0, m.start()) + 1))
    return hits


def _multipart(fields: dict[str, str], filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    buf = bytearray()
    for key, value in fields.items():
        buf += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
    buf += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    buf += data + b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


def _superuser_token(email: str, password: str) -> str:
    resp = pb_api._http_json("POST", f"{_base()}/api/collections/_superusers/auth-with-password",
                             body={"identity": email, "password": password})
    if not resp.get("token"):
        raise PBAPIError("login superuser gagal: token tidak ada")
    return resp["token"]


def _err(msg: str) -> None:
    print(f"[bq-bin] {msg}", file=sys.stderr)


# ── ls ────────────────────────────────────────────────────────────────────

def cmd_ls(args: argparse.Namespace) -> int:
    try:
        records = _list_records(_token())
    except PBAPIError as e:
        _err(f"PocketBase tidak terjangkau atau login gagal: {e}")
        return EXIT_PB
    local = _local_files()
    records.sort(key=lambda r: r.get("name", ""))
    if not records:
        print("Belum ada tool di PocketBase (collection bq_bin kosong).")
        return EXIT_OK
    print(f"{'NAMA':<28} {'KIND':<8} {'UKURAN':>8}  {'SHA':<8}  {'VERSI':<10} LOKAL")
    for r in records:
        name = r.get("name", "")
        print(f"{name:<28} {r.get('kind') or '-':<8} {r.get('size') or 0:>8}  "
              f"{(r.get('sha256') or '')[:8]:<8}  {r.get('version') or '-':<10} "
              f"{'ya' if name in local else 'tidak'}")
    return EXIT_OK


# ── check ─────────────────────────────────────────────────────────────────

def _compare(token: str) -> list[dict]:
    remote = {r["name"]: r for r in _list_records(token) if r.get("name") and _valid_name(r["name"])}
    local = _local_files()
    rows: list[dict] = []
    for name in sorted(set(remote) | set(local)):
        lp = local.get(name)
        rec = remote.get(name)
        if lp is not None and lp.stat().st_size > MAX_SIZE:
            status, lsha = "DILEWATI (ukuran)", None
        elif lp is not None:
            lsha = _sha256(lp.read_bytes())
            if rec is None:
                status = "HANYA DI LOKAL"
            else:
                status = "MATCH" if lsha == rec.get("sha256") else "BEDA"
        else:
            lsha = None
            status = "TIDAK ADA DI LOKAL"
        rsha = rec.get("sha256") if rec else None
        if status in ("BEDA", "TIDAK ADA DI LOKAL"):
            saran = f"bq bin pull {name}"
        elif status == "HANYA DI LOKAL":
            saran = f"bq bin push {lp}"
        else:
            saran = "-"
        rows.append({"name": name, "status": status, "sha_lokal": lsha, "sha_pb": rsha, "saran": saran})
    return rows


def cmd_check(args: argparse.Namespace) -> int:
    try:
        rows = _compare(_token())
    except PBAPIError as e:
        _err(f"PocketBase tidak terjangkau atau login gagal: {e}")
        return EXIT_PB
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        if not rows:
            print("Tidak ada tool (PocketBase kosong, BIN_DIR kosong).")
        else:
            print(f"{'NAMA':<28} {'SHA LOKAL':<10} {'SHA PB':<10} {'STATUS':<20} SARAN")
            for r in rows:
                print(f"{r['name']:<28} {(r['sha_lokal'] or '-')[:8]:<10} {(r['sha_pb'] or '-')[:8]:<10} "
                      f"{r['status']:<20} {r['saran']}")
    bad = {"BEDA", "TIDAK ADA DI LOKAL", "HANYA DI LOKAL"}
    return EXIT_DIFF if any(r["status"] in bad for r in rows) else EXIT_OK


# ── pull ──────────────────────────────────────────────────────────────────

def _show_diff(name: str, local: Optional[Path], data: bytes) -> None:
    if local is None:
        print(f"  {name}: baru (tidak ada di lokal), {len(data)} byte")
        return
    old = local.read_bytes()
    if old == data:
        return
    if not (_is_text(old) and _is_text(data)):
        print(f"  {name}: biner atau bukan teks, diff tidak ditampilkan ({len(old)} -> {len(data)} byte)")
        return
    diff = difflib.unified_diff(
        old.decode("utf-8").splitlines(keepends=True),
        data.decode("utf-8").splitlines(keepends=True),
        fromfile=f"lokal/{name}", tofile=f"pocketbase/{name}",
    )
    sys.stdout.writelines(diff)


def _write_atomic(name: str, data: bytes, mode: int) -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = TMP_DIR / f"{name}.{uuid.uuid4().hex}"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        dest = BIN_DIR / name
        if dest.exists():
            BAK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            shutil.copy2(dest, BAK_DIR / f"{name}.{stamp}")
        os.replace(tmp, BIN_DIR / name)
    finally:
        if tmp.exists():
            tmp.unlink()


def cmd_pull(args: argparse.Namespace) -> int:
    if not args.all and not args.names:
        _err("sebutkan nama tool atau pakai --all")
        return 1
    try:
        token = _token()
        remote = {r["name"]: r for r in _list_records(token) if r.get("name")}
    except PBAPIError as e:
        _err(f"PocketBase tidak terjangkau atau login gagal: {e}")
        return EXIT_PB
    names = sorted(remote) if args.all else args.names
    local = _local_files()
    plan: list[str] = []
    for name in names:
        if name not in remote:
            _err(f"'{name}' tidak ada di PocketBase")
            return 1
        if not _valid_name(name):
            _err(f"nama '{name}' di PocketBase tidak valid; ditolak")
            return EXIT_HASH
        lp = local.get(name)
        if lp is not None and _sha256(lp.read_bytes()) == remote[name].get("sha256"):
            continue  # sudah MATCH
        plan.append(name)
    if not plan:
        print("Semua tool sudah sama dengan PocketBase. Tidak ada yang diubah.")
        return EXIT_OK

    # Unduh dan verifikasi dulu semua; baru tampilkan diff dan ganti.
    staged: dict[str, bytes] = {}
    for name in plan:
        rec = remote[name]
        data = _download(rec, token)
        if _sha256(data) != rec.get("sha256"):
            _err(f"sha256 '{name}' tidak cocok dengan PocketBase; tidak ada berkas yang diganti")
            return EXIT_HASH
        if len(data) > MAX_SIZE:
            _err(f"'{name}' lebih besar dari 512 KB; ditolak")
            return EXIT_HASH
        staged[name] = data

    print("Perubahan:")
    for name in plan:
        _show_diff(name, local.get(name), staged[name])

    for name in plan:
        if not args.yes:
            ans = input(f"Ganti '{name}' dengan versi PocketBase? [y/N] ").strip().lower()
            if ans not in ("y", "ya"):
                print(f"  dilewati: {name}")
                continue
        mode = int(remote[name].get("mode") or "0755", 8)
        _write_atomic(name, staged[name], mode)
        print(f"  diganti: {name} (sha {_sha256(staged[name])[:8]}, mode {oct(mode)[2:]})")
    return EXIT_OK


# ── push ──────────────────────────────────────────────────────────────────

def _guess_kind(data: bytes) -> str:
    first = data.split(b"\n", 1)[0].decode("utf-8", errors="replace")
    if "python" in first:
        return "python"
    return "bash"


def cmd_push(args: argparse.Namespace) -> int:
    src = Path(args.file).expanduser().resolve()
    if not src.is_file():
        _err(f"berkas tidak ada: {src}")
        return 1
    name = src.name
    if not _valid_name(name):
        _err(f"nama '{name}' tidak sesuai pola {NAME_RE.pattern}")
        return 1
    data = src.read_bytes()
    if len(data) > MAX_SIZE:
        _err(f"berkas lebih besar dari 512 KB ({len(data)} byte); ditolak")
        return 1
    if not _is_text(data):
        _err("berkas bukan teks; fase 1 hanya menerima skrip teks")
        return 1
    hits = _scan_secrets(data.decode("utf-8"))
    if hits:
        _err("ditolak: pola rahasia ditemukan (nilai tidak ditampilkan):")
        for label, line in hits:
            _err(f"  baris {line}: {label}")
        return 1
    mode = args.mode or "0755"
    if mode not in MODES:
        _err(f"mode harus salah satu dari {', '.join(MODES)}")
        return 1
    kind = args.kind or _guess_kind(data)
    if kind not in KINDS:
        _err(f"kind harus salah satu dari {', '.join(KINDS)}")
        return 1
    sha = _sha256(data)

    email = args.email or input("Email superuser PocketBase: ").strip()
    password = getpass.getpass("Password superuser: ")
    try:
        token = _superuser_token(email, password)
        existing = _find_record(token, name)
    except PBAPIError as e:
        _err(f"login superuser gagal atau PocketBase tidak terjangkau: {e}")
        return EXIT_PB

    action = "perbarui" if existing else "buat baru"
    print(f"Unggah '{name}' ({action}): {len(data)} byte, sha {sha[:8]}, mode {mode}, kind {kind}")
    if not args.yes:
        ans = input("Lanjut? [y/N] ").strip().lower()
        if ans not in ("y", "ya"):
            print("Dibatalkan.")
            return 0

    fields = {"name": name, "sha256": sha, "size": str(len(data)), "mode": mode, "kind": kind,
              "version": args.version or "", "notes": args.notes or ""}
    body, ctype = _multipart(fields, name, data)
    try:
        if existing:
            url = f"{_base()}/api/collections/{COLLECTION}/records/{existing['id']}"
            resp = json.loads(_raw("PATCH", url, token, body, ctype))
        else:
            url = f"{_base()}/api/collections/{COLLECTION}/records"
            resp = json.loads(_raw("POST", url, token, body, ctype))
    except PBAPIError as e:
        _err(f"unggah ditolak: {e}")
        return EXIT_PB
    if resp.get("sha256") != sha:
        _err("PocketBase menyimpan sha256 yang berbeda; periksa hook bq_bin")
        return EXIT_HASH
    print(f"Berhasil: '{name}' tersimpan di PocketBase (sha {sha[:8]}).")
    return EXIT_OK


# ── entry ─────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bq bin", description="Sinkron ~/.build-q/bin dengan PocketBase (bq_bin)")
    p.add_argument("--pb-url", help="override URL PocketBase (mis. http://127.0.0.1:8193 untuk tes lokal)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ls", help="daftar tool di PocketBase")
    c = sub.add_parser("check", help="bandingkan lokal dengan PocketBase (tidak mengubah berkas)")
    c.add_argument("--json", action="store_true")
    pl = sub.add_parser("pull", help="unduh tool dari PocketBase")
    pl.add_argument("names", nargs="*")
    pl.add_argument("--all", action="store_true")
    pl.add_argument("--yes", action="store_true", help="tanpa konfirmasi")
    ps = sub.add_parser("push", help="unggah tool ke PocketBase (superuser)")
    ps.add_argument("file")
    ps.add_argument("--mode", choices=MODES)
    ps.add_argument("--kind", choices=KINDS)
    ps.add_argument("--version")
    ps.add_argument("--notes")
    ps.add_argument("--email", help="email superuser (password diminta interaktif)")
    ps.add_argument("--yes", action="store_true", help="tanpa konfirmasi")
    return p


def cli_main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.pb_url:
        os.environ["PB_API_URL"] = args.pb_url.rstrip("/")
    handlers = {"ls": cmd_ls, "check": cmd_check, "pull": cmd_pull, "push": cmd_push}
    return handlers[args.cmd](args)
