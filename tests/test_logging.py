"""Unit tests for the JSON log formatter and the log format selector."""
import io
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freecad_mcp.json_logging import JsonLogFormatter  # noqa: E402


def _capture_log(formatter, msg="hello", extra=None, level=logging.INFO, exc_info=None):
    """Run a single record through *formatter* and return the parsed JSON."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(formatter)
    logger = logging.getLogger("test_json_logging")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.log(level, msg, extra=extra or {}, exc_info=exc_info)
    raw = buf.getvalue().strip()
    return json.loads(raw)


def test_json_formatter_basic_fields():
    rec = _capture_log(JsonLogFormatter(), msg="hello")
    assert rec["msg"] == "hello"
    assert rec["level"] == "INFO"
    assert rec["logger"] == "test_json_logging"
    assert "ts" in rec
    # Timestamp is ISO 8601 with milliseconds and 'Z'.
    assert rec["ts"].endswith("Z")
    assert "T" in rec["ts"]


def test_json_formatter_merges_extras():
    rec = _capture_log(
        JsonLogFormatter(), msg="opened", extra={"port": 9875, "host": "localhost"}
    )
    assert rec["port"] == 9875
    assert rec["host"] == "localhost"
    assert rec["msg"] == "opened"


def test_json_formatter_args_interpolation():
    """logger.warning('port %d', 9875) should render as 'port 9875'."""
    rec = _capture_log(JsonLogFormatter(), msg="port %d", extra={"__interp": 9875})
    # The formatter uses getMessage() which applies % interpolation.
    assert rec["msg"] == "port %d"
    # Real interpolation test:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("test_json_logging_args")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.warning("port %d open", 9875)
    parsed = json.loads(buf.getvalue().strip())
    assert parsed["msg"] == "port 9875 open"


def test_json_formatter_exception_renders_as_string():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        rec = _capture_log(JsonLogFormatter(), msg="failed", exc_info=True)
    assert "exc_info" in rec
    assert "RuntimeError" in rec["exc_info"]
    assert "boom" in rec["exc_info"]


def test_json_formatter_handles_non_json_extras():
    """Non-serialisable extras must be coerced to repr, not crash."""
    class Opaque:
        def __repr__(self):
            return "<opaque>"

    rec = _capture_log(JsonLogFormatter(), msg="x", extra={"obj": Opaque()})
    assert rec["obj"] == "<opaque>"


def test_json_formatter_handles_list_and_tuple_extras():
    """Lists and tuples are recursed element-by-element."""
    rec = _capture_log(JsonLogFormatter(), msg="x", extra={
        "items_list": [1, 2, "three"],
        "items_tuple": (1, 2, "three"),
    })
    assert rec["items_list"] == [1, 2, "three"]
    assert rec["items_tuple"] == [1, 2, "three"]


def test_json_formatter_handles_dict_extras():
    """MutableMapping extras are coerced to {str(k): ...}."""
    from collections import UserDict
    rec = _capture_log(JsonLogFormatter(), msg="x", extra={
        "meta": UserDict({"a": 1, "b": "two"}),
    })
    assert rec["meta"] == {"a": 1, "b": "two"}


def test_json_formatter_extras_dont_shadow_standard_fields():
    """User-supplied extras that conflict with standard fields are ignored
    so the structured fields stay authoritative."""
    rec = _capture_log(JsonLogFormatter(), msg="hello", extra={"level": "FAKE"})
    # Standard level (INFO/whatever) wins.
    assert rec["level"] != "FAKE"


def test_json_formatter_does_not_leak_logging_internals():
    """Internal LogRecord attributes (e.g. ``args``, ``levelno``) must
    not appear in the output unless explicitly added by the caller.
    """
    rec = _capture_log(JsonLogFormatter(), msg="x")
    assert "args" not in rec
    assert "levelno" not in rec
    assert "pathname" not in rec
    assert "module" not in rec


def test_log_format_text_default(monkeypatch):
    """configure_logging uses the text formatter unless LOG_FORMAT=json."""
    monkeypatch.setenv("FREECAD_MCP_LOG_FORMAT", "")
    monkeypatch.setenv("FREECAD_MCP_LOGLEVEL", "INFO")
    # Reload the server module to re-run configure_logging.
    import importlib
    import freecad_mcp.server as srv
    importlib.reload(srv)
    root = logging.getLogger()
    for handler in root.handlers:
        if handler.formatter is not None:
            assert not isinstance(handler.formatter, JsonLogFormatter)
    # Cleanup
    root._freecad_mcp_configured = False


def test_log_format_json(monkeypatch):
    """configure_logging uses JsonLogFormatter when LOG_FORMAT=json."""
    monkeypatch.setenv("FREECAD_MCP_LOG_FORMAT", "json")
    monkeypatch.setenv("FREECAD_MCP_LOGLEVEL", "INFO")
    import importlib
    import freecad_mcp.server as srv
    importlib.reload(srv)
    root = logging.getLogger()
    has_json = any(
        isinstance(h.formatter, JsonLogFormatter) for h in root.handlers
    )
    assert has_json, "no JSON formatter found on any handler"
    # Cleanup
    root._freecad_mcp_configured = False


if __name__ == "__main__":
    print("Run with pytest; direct invocation is not supported.")
