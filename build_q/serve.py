"""`bq --serve PORT` — Jev Agent Planner sebagai web chat (M1 MVP).

Arsitektur:
- `http.server.ThreadingHTTPServer` (stdlib, zero new deps).
- Reuse 100% logic di `tui.py` (pick_tool, extract_params, preview_command).
- Routes JSON API + inline HTML SPA dari `serve_assets.py`.
- Default bind 127.0.0.1 — exposure `--serve-host 0.0.0.0` butuh warning.
- RISKY commands tolak execute kalau `confirm` tidak eksplisit.
- Audit log append-only ke `~/.build-q/.serve-audit.log`.

M1 scope (sinkronus):
  - GET  /                → inline SPA
  - GET  /api/health      → version + provider + cache age
  - GET  /api/catalog     → tools/providers/patterns ringkas
  - POST /api/decide      → {user_msg} → pilih tool + extract params
  - POST /api/execute     → {cmd, confirm, risky} → run subprocess sync
  - POST /api/provider    → {name} → switch provider runtime

M2 (next): async subprocess + SSE stream.
"""
from __future__ import annotations

import datetime
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import CONFIG_DIR

AUDIT_LOG = CONFIG_DIR / ".serve-audit.log"

# ─── Rate limit per-IP (M5) ─────────────────────────────────────────────────
_RATE_WINDOW = 60.0
_RATE_MAX = 120  # 120 req/menit per IP (API) + lebih longgar untuk /
_rate_hits: dict = {}
_rate_lock = threading.Lock()


def _rate_limit_ok(ip: str, path: str) -> bool:
    """True kalau request lolos rate limit. Reset otomatis per window 60s."""
    now = time.time()
    with _rate_lock:
        bucket = _rate_hits.setdefault(ip, [])
        # purge entries di luar window
        bucket[:] = [t for t in bucket if now - t < _RATE_WINDOW]
        max_hits = _RATE_MAX if path.startswith("/api/") else _RATE_MAX * 3
        if len(bucket) >= max_hits:
            return False
        bucket.append(now)
    return True


# ─── Job store untuk async execute + SSE stream (M2) ───────────────────────
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL_SECONDS = 600  # auto-purge job state 10 menit setelah done


def _reap_old_jobs() -> None:
    now = time.time()
    with _JOBS_LOCK:
        stale = [jid for jid, j in _JOBS.items()
                 if j.get("done_at") and (now - j["done_at"]) > _JOB_TTL_SECONDS]
        for jid in stale:
            _JOBS.pop(jid, None)


def _spawn_job(cmd: str) -> str:
    """Spawn subprocess async; register job; return job_id."""
    _reap_old_jobs()
    job_id = uuid.uuid4().hex[:12]
    q: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=10_000)
    try:
        proc = subprocess.Popen(
            shlex.split(cmd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except FileNotFoundError as e:
        q.put(f"ERR: executable not found: {e}")
        q.put(None)
        with _JOBS_LOCK:
            _JOBS[job_id] = {
                "cmd": cmd, "proc": None, "q": q,
                "returncode": 127, "done_at": time.time(),
            }
        return job_id

    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "cmd": cmd, "proc": proc, "q": q,
            "returncode": None, "done_at": None, "started_at": time.time(),
        }

    def _reader(p: subprocess.Popen, qq: "queue.Queue[Optional[str]]", jid: str) -> None:
        try:
            for line in p.stdout:  # type: ignore[union-attr]
                qq.put(line.rstrip("\n"))
            p.wait()
        except Exception as e:
            qq.put(f"ERR: reader: {e}")
        finally:
            qq.put(None)  # sentinel: EOF
            with _JOBS_LOCK:
                if jid in _JOBS:
                    _JOBS[jid]["returncode"] = p.returncode
                    _JOBS[jid]["done_at"] = time.time()

    threading.Thread(target=_reader, args=(proc, q, job_id), daemon=True).start()
    return job_id


# ─── Helpers ────────────────────────────────────────────────────────────────

def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler: BaseHTTPRequestHandler, html: str) -> None:
    body = html.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if not length:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def _audit(entry: dict) -> None:
    entry["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # never block response on audit failure


def _mask_key(k: str) -> str:
    if not k:
        return ""
    if len(k) > 12:
        return f"{k[:6]}…{k[-4:]}"
    return "<short>"


# ─── Handler factory ────────────────────────────────────────────────────────

def make_handler(config: dict) -> type:
    """Factory agar handler dapat capture config (allow_exec, host, dll)."""

    class _Handler(BaseHTTPRequestHandler):
        # Hush default access log line — pakai audit kita sendiri
        def log_message(self, fmt, *args):
            pass

        # ── Pre-guards (dipanggil manual di do_GET/do_POST) ──────────────────
        def _check_guards(self, path: str) -> bool:
            """Return False bila ditolak (sudah tulis response). Else True."""
            ip = self.client_address[0] if self.client_address else "unknown"
            # Rate limit
            if not _rate_limit_ok(ip, path):
                _json_response(self, 429, {"error": "rate limit exceeded"})
                return False
            # Token auth untuk /api/* (bila dikonfigurasi)
            token = config.get("token")
            if token and path.startswith("/api/"):
                auth = self.headers.get("Authorization", "")
                sent = auth.replace("Bearer ", "", 1).strip() if auth.startswith("Bearer ") else ""
                if not sent:
                    # Fallback: baca dari query string (?token=X) — untuk EventSource
                    qs = parse_qs(urlparse(self.path).query)
                    sent = (qs.get("token", [""])[0] or "").strip()
                if sent != token:
                    _audit({"event": "auth_fail", "path": path, "ip": ip})
                    _json_response(self, 401, {"error": "unauthorized (Bearer token required)"})
                    return False
            return True

        # ── Routing ─────────────────────────────────────────────────────────
        def do_GET(self):
            parsed = urlparse(self.path)
            route = parsed.path
            if not self._check_guards(route):
                return
            if route == "/":
                return self._serve_html()
            if route == "/api/health":
                return self._api_health()
            if route == "/api/catalog":
                return self._api_catalog()
            if route.startswith("/api/stream/"):
                job_id = route.split("/api/stream/", 1)[1].strip("/")
                return self._api_stream(job_id)
            return _json_response(self, 404, {"error": f"not found: {route}"})

        def do_POST(self):
            parsed = urlparse(self.path)
            route = parsed.path
            if not self._check_guards(route):
                return
            body = _read_json_body(self)
            if route == "/api/decide":
                return self._api_decide(body)
            if route == "/api/execute":
                return self._api_execute(body)
            if route == "/api/execute-stream":
                return self._api_execute_stream(body)
            if route == "/api/provider":
                return self._api_provider(body)
            return _json_response(self, 404, {"error": f"not found: {route}"})

        # ── Routes ──────────────────────────────────────────────────────────
        def _serve_html(self):
            from .serve_assets import INDEX_HTML
            _html_response(self, INDEX_HTML)

        def _api_health(self):
            from . import tui, tui_catalog
            try:
                provider = tui.resolve_provider()
                prov_info = {
                    "name": provider.name,
                    "model": provider.model,
                    "url": provider.url,
                    "key_env": provider.key_env,
                    "key": _mask_key(os.environ.get(provider.key_env, "")),
                }
            except Exception as e:
                prov_info = {"error": str(e)}
            _json_response(self, 200, {
                "version": __version__,
                "provider": prov_info,
                "cache_age_seconds": tui_catalog.cache_age_seconds(),
                "allow_exec": config.get("allow_exec", True),
                "host": config.get("host", "127.0.0.1"),
                "auth_required": bool(config.get("token")),
            })

        def _api_catalog(self):
            from . import tui
            cat = tui.catalog()
            # Ringkas — tidak kirim probabilities, patterns regex, dll
            tools = {
                name: {
                    "desc": meta.get("desc", ""),
                    "template": meta.get("template", ""),
                    "params": meta.get("params", {}),
                    "risky": name in cat["risky"],
                    "category": meta.get("_category", ""),
                }
                for name, meta in cat["tools"].items()
            }
            providers = {
                name: {
                    "name": p["name"],
                    "model": p["model"],
                    "url": p["url"],
                    "is_default": p.get("is_default", False),
                }
                for name, p in cat["providers"].items()
            }
            _json_response(self, 200, {
                "tools": tools,
                "providers": providers,
                "default_provider": cat["default_provider"],
                "n_tools": len(tools),
                "n_providers": len(providers),
                "n_patterns": len(cat["patterns"]),
            })

        def _api_decide(self, body: dict):
            from . import tui
            user_msg = (body.get("user_msg") or "").strip()
            if not user_msg:
                return _json_response(self, 400, {"error": "user_msg required"})
            try:
                provider = tui.resolve_provider(body.get("provider"))
                resp = tui.pick_tool(user_msg, provider)
            except Exception as e:
                return _json_response(self, 500, {"error": str(e)})

            answers = resp.get("answers", {}) or {}
            tool_ans = answers.get("tool", {}) or {}
            risky_ans = answers.get("risky", {}) or {}
            tool = tool_ans.get("choice", "none")
            probs = tool_ans.get("probabilities", {}) or {}
            top3 = sorted(probs.items(), key=lambda x: -x[1])[:3]

            # Extract params + fill via Jev untuk tool terpilih
            params = {}
            preview = ""
            if tool and tool != "none":
                params = tui.extract_params_from_text(user_msg, tool)
                try:
                    filled = tui.fill_missing_choice_params(user_msg, tool, provider)
                    params.update(filled)
                except Exception as e:
                    # non-fatal — frontend bisa minta manual
                    _audit({"event": "fill_param_error", "tool": tool, "error": str(e)})
                try:
                    preview = tui.preview_command(tool, params)
                except Exception as e:
                    preview = f"<preview error: {e}>"

            noul = float(risky_ans.get("noul", 0.0) or 0.0)
            risky = noul >= 0.5 or tool in tui.RISKY_TOOLS()
            _audit({
                "event": "decide",
                "user_msg": user_msg[:200],
                "tool": tool,
                "confidence": tool_ans.get("confidence"),
                "risky": risky,
            })
            _json_response(self, 200, {
                "user_msg": user_msg,
                "tool": tool,
                "confidence": tool_ans.get("confidence", 0.0),
                "top3": [{"name": n, "prob": p} for n, p in top3],
                "params": params,
                "preview": preview,
                "risky": risky,
                "risky_noul": noul,
                "usage": resp.get("usage") or {},
                "params_schema": (
                    tui.TOOLS().get(tool, {}).get("params", {})
                    if tool and tool != "none" else {}
                ),
            })

        def _api_execute(self, body: dict):
            if not config.get("allow_exec", True):
                return _json_response(self, 403, {
                    "error": "execute disabled (--serve-no-exec)",
                })
            cmd = (body.get("cmd") or "").strip()
            if not cmd:
                return _json_response(self, 400, {"error": "cmd required"})
            risky = bool(body.get("risky"))
            confirm = bool(body.get("confirm"))
            if risky and not confirm:
                return _json_response(self, 400, {
                    "error": "RISKY command requires confirm=true in body",
                })
            # Guard: hanya izinkan command yang mulai dgn `bq ` atau `python3 -m `
            # (sesuai pattern katalog TUI) — menolak arbitrary shell.
            if not (cmd.startswith("bq ") or cmd.startswith("python3 -m ")):
                return _json_response(self, 400, {
                    "error": "cmd harus diawali `bq ` atau `python3 -m ` (safety guard)",
                })

            job_id = uuid.uuid4().hex[:12]
            t0 = time.time()
            try:
                proc = subprocess.run(
                    shlex.split(cmd),
                    capture_output=True, text=True, timeout=180,
                )
                elapsed = time.time() - t0
                _audit({
                    "event": "execute", "job_id": job_id, "cmd": cmd,
                    "exit": proc.returncode, "elapsed_s": round(elapsed, 2),
                    "risky": risky,
                })
                _json_response(self, 200, {
                    "job_id": job_id,
                    "cmd": cmd,
                    "exit": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "elapsed_s": round(elapsed, 2),
                })
            except subprocess.TimeoutExpired as e:
                _audit({
                    "event": "execute_timeout", "job_id": job_id, "cmd": cmd,
                })
                _json_response(self, 504, {
                    "job_id": job_id, "error": f"timeout after {e.timeout}s",
                })
            except FileNotFoundError as e:
                _json_response(self, 500, {
                    "job_id": job_id, "error": f"executable not found: {e}",
                })
            except Exception as e:
                _audit({
                    "event": "execute_error", "job_id": job_id, "error": str(e),
                })
                _json_response(self, 500, {
                    "job_id": job_id, "error": str(e),
                })

        def _api_execute_stream(self, body: dict):
            """Spawn command async; return job_id untuk di-stream via SSE."""
            if not config.get("allow_exec", True):
                return _json_response(self, 403, {
                    "error": "execute disabled (--serve-no-exec)",
                })
            cmd = (body.get("cmd") or "").strip()
            if not cmd:
                return _json_response(self, 400, {"error": "cmd required"})
            risky = bool(body.get("risky"))
            confirm = bool(body.get("confirm"))
            if risky and not confirm:
                return _json_response(self, 400, {
                    "error": "RISKY command requires confirm=true in body",
                })
            if not (cmd.startswith("bq ") or cmd.startswith("python3 -m ")):
                return _json_response(self, 400, {
                    "error": "cmd harus diawali `bq ` atau `python3 -m `",
                })

            job_id = _spawn_job(cmd)
            _audit({
                "event": "execute_stream_start", "job_id": job_id,
                "cmd": cmd, "risky": risky,
            })
            _json_response(self, 200, {
                "job_id": job_id,
                "cmd": cmd,
                "stream_url": f"/api/stream/{job_id}",
            })

        def _api_stream(self, job_id: str):
            """Server-Sent Events — baris output real-time, event `done` di akhir."""
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
            if not job:
                return _json_response(self, 404, {
                    "error": f"job not found: {job_id}",
                })
            q = job["q"]
            # SSE headers — hindari Content-Length, aktifkan chunked
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")  # disable nginx buf
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            start = time.time()
            try:
                # Initial retry hint
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
                while True:
                    try:
                        line = q.get(timeout=30)
                    except queue.Empty:
                        # heartbeat agar koneksi tidak diputus proxy
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    if line is None:
                        # Sentinel: EOF — kirim event done
                        with _JOBS_LOCK:
                            rc = _JOBS.get(job_id, {}).get("returncode")
                        payload = json.dumps({
                            "exit": rc, "elapsed_s": round(time.time() - start, 2),
                        })
                        self.wfile.write(
                            f"event: done\ndata: {payload}\n\n".encode()
                        )
                        self.wfile.flush()
                        _audit({
                            "event": "execute_stream_done", "job_id": job_id,
                            "exit": rc,
                        })
                        return
                    payload = json.dumps({"line": line})
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # Client disconnect — hentikan subprocess bila masih jalan
                proc = job.get("proc")
                if proc and proc.poll() is None:
                    proc.terminate()
                _audit({
                    "event": "execute_stream_abort", "job_id": job_id,
                })

        def _api_provider(self, body: dict):
            from . import tui
            name = (body.get("name") or "").strip().lower()
            if not name:
                return _json_response(self, 400, {"error": "name required"})
            try:
                provider = tui.resolve_provider(name)
            except Exception as e:
                return _json_response(self, 400, {"error": str(e)})
            os.environ["JEV_PROVIDER"] = provider.name
            _audit({"event": "switch_provider", "provider": provider.name})
            _json_response(self, 200, {
                "ok": True,
                "provider": {
                    "name": provider.name,
                    "model": provider.model,
                    "url": provider.url,
                    "key": _mask_key(os.environ.get(provider.key_env, "")),
                },
            })

    return _Handler


# ─── Entry ──────────────────────────────────────────────────────────────────

def run_serve(
    port: int,
    *,
    host: str = "127.0.0.1",
    allow_exec: bool = True,
    token: Optional[str] = None,
) -> int:
    """Start HTTP server. Block until Ctrl+C."""
    # Hydrate env dulu (biar provider resolve bisa jalan)
    from .tui import load_env
    load_env()

    config = {
        "host": host,
        "port": port,
        "allow_exec": allow_exec,
        "token": token,
    }
    handler_cls = make_handler(config)
    server = ThreadingHTTPServer((host, port), handler_cls)

    url = f"http://{host}:{port}"
    print(f"🚀 Jev web chat → {url}")
    print(f"   version    : {__version__}")
    print(f"   host       : {host}")
    print(f"   exec mode  : {'enabled' if allow_exec else 'disabled (--serve-no-exec)'}")
    print(f"   auth       : {'Bearer token required' if token else 'none (localhost trust)'}")
    print(f"   rate limit : {_RATE_MAX} req/menit per IP (API)")
    print(f"   audit log  : {AUDIT_LOG}")
    if host == "0.0.0.0":
        print("")
        print("⚠️  Bind 0.0.0.0 — server exposed ke seluruh network!")
        print("    Pastikan firewall / VPN membatasi akses, dan RISKY commands")
        print("    tetap butuh confirm manual dari UI.")
        if not token:
            print("    SANGAT disarankan tambah --serve-token untuk auth!")
    print(f"\n   Ctrl+C untuk stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
    finally:
        server.server_close()
    return 0
