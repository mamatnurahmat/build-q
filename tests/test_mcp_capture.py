"""Tests for MCP capture utility."""

from build_q.mcp._capture import capture_output, strip_ansi, run_captured


class TestCaptureOutput:
    def test_captures_stdout(self):
        with capture_output() as (out, err):
            print("hello world")
        assert out.getvalue() == "hello world\n"
        assert err.getvalue() == ""

    def test_captures_stderr(self):
        import sys
        with capture_output() as (out, err):
            print("error msg", file=sys.stderr)
        assert out.getvalue() == ""
        assert err.getvalue() == "error msg\n"

    def test_restores_streams(self):
        import sys
        orig_out, orig_err = sys.stdout, sys.stderr
        with capture_output():
            pass
        assert sys.stdout is orig_out
        assert sys.stderr is orig_err


class TestStripAnsi:
    def test_removes_color_codes(self):
        text = "\x1b[31mERROR\x1b[0m: something failed"
        assert strip_ansi(text) == "ERROR: something failed"

    def test_passthrough_plain(self):
        assert strip_ansi("plain text") == "plain text"


class TestRunCaptured:
    def test_captures_int_return(self):
        def returns_zero():
            print("ok")
            return 0

        result = run_captured(returns_zero)
        assert result["exit_code"] == 0
        assert "ok" in result["stdout"]

    def test_captures_bool_return(self):
        def returns_true():
            return True

        result = run_captured(returns_true)
        assert result["exit_code"] == 0
        assert result["data"] is True

    def test_captures_dict_return(self):
        def returns_dict():
            return {"key": "value"}

        result = run_captured(returns_dict)
        assert result["exit_code"] == 0
        assert result["data"] == {"key": "value"}

    def test_captures_exception(self):
        def raises():
            raise ValueError("broken")

        result = run_captured(raises)
        assert result["exit_code"] == 2
        assert "ValueError" in result["error"]

    def test_captures_nonzero_exit(self):
        def returns_one():
            print("warning found")
            return 1

        result = run_captured(returns_one)
        assert result["exit_code"] == 1
        assert "warning" in result["stdout"]

    def test_passes_args_kwargs(self):
        def adder(a, b, extra=0):
            print(a + b + extra)
            return 0

        result = run_captured(adder, 2, 3, extra=5)
        assert "10" in result["stdout"]
