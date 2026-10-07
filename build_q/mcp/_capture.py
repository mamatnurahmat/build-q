"""Capture stdout/stderr from bq functions for structured MCP responses."""

from __future__ import annotations

import io
import logging
import sys
import time
from contextlib import contextmanager
from typing import Generator

logger = logging.getLogger("bq-mcp")


@contextmanager
def capture_output() -> Generator[tuple[io.StringIO, io.StringIO], None, None]:
    """Redirect stdout + stderr to StringIO buffers during a function call."""
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = buf_out = io.StringIO()
    sys.stderr = buf_err = io.StringIO()
    try:
        yield buf_out, buf_err
    finally:
        sys.stdout, sys.stderr = old_out, old_err


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from Rich-formatted output."""
    import re
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)


def run_captured(func, *args, **kwargs) -> dict:
    """Run a bq function, capture its output, and return structured result.

    Returns dict with keys: exit_code, stdout, stderr, error (if exception).
    For functions returning bool, exit_code is 0 (True) or 1 (False).
    For functions returning dict, the dict is included as 'data'.
    """
    func_name = getattr(func, "__name__", str(func))
    t0 = time.monotonic()
    result: dict = {"exit_code": 0, "stdout": "", "stderr": ""}

    try:
        with capture_output() as (out, err):
            ret = func(*args, **kwargs)

        result["stdout"] = strip_ansi(out.getvalue())
        result["stderr"] = strip_ansi(err.getvalue())

        if isinstance(ret, bool):
            result["exit_code"] = 0 if ret else 1
            result["data"] = ret
        elif isinstance(ret, int):
            result["exit_code"] = ret
        elif isinstance(ret, dict):
            result["exit_code"] = 0
            result["data"] = ret
        elif isinstance(ret, tuple):
            result["exit_code"] = 0
            result["data"] = list(ret)
        else:
            result["data"] = ret

    except Exception as exc:
        result["exit_code"] = 2
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["error_type"] = type(exc).__name__
        try:
            result["stdout"] = strip_ansi(out.getvalue())
            result["stderr"] = strip_ansi(err.getvalue())
        except UnboundLocalError:
            pass

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    result["elapsed_ms"] = elapsed_ms
    logger.info(
        "tool=%s exit_code=%d elapsed_ms=%d",
        func_name, result["exit_code"], elapsed_ms,
    )

    return result
