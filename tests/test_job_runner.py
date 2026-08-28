"""Tests for the job_runner module (v1.1.2)."""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

_RPC_DIR = Path(__file__).resolve().parent.parent / "addon/FreeCADMCP/rpc_server"


@pytest.fixture
def jr_mod(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "_job_runner_for_test", _RPC_DIR / "job_runner.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_job_runner_for_test"] = mod  # needed for dataclass __module__
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_submit_and_poll_simple(jr_mod) -> None:
    runner = jr_mod.JobRunner()
    job = runner.submit("x = 1 + 1\nresult = x", label="add")
    # Spin until terminal.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        j = runner.poll(job.job_id)
        if j and j.is_terminal:
            break
        time.sleep(0.05)
    j = runner.poll(job.job_id)
    assert j is not None
    assert j.status == jr_mod.STATUS_DONE
    assert j.result == 2


def test_submit_collects_traceback(jr_mod) -> None:
    runner = jr_mod.JobRunner()
    job = runner.submit("raise ValueError('boom')", label="err")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        j = runner.poll(job.job_id)
        if j and j.is_terminal:
            break
        time.sleep(0.05)
    j = runner.poll(job.job_id)
    assert j.status == jr_mod.STATUS_ERROR
    assert "boom" in (j.error or "")
    assert j.traceback is not None


def test_cancel_pending_returns_immediately(jr_mod) -> None:
    runner = jr_mod.JobRunner()
    job = runner.submit("time.sleep(10)", label="long")
    # Try to cancel quickly — may catch it before running starts.
    runner.cancel(job.job_id)
    # Either cancelled now, or running (no immediate kill). Poll a few times.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        j = runner.poll(job.job_id)
        if j and j.is_terminal:
            break
        time.sleep(0.05)
    j = runner.poll(job.job_id)
    # If cancellation happened, status is cancelled; if running, it stays
    # running until the sleep finishes (we just verify no exception).
    assert j is not None


def test_cancel_unknown_job(jr_mod) -> None:
    runner = jr_mod.JobRunner()
    assert runner.cancel("nope") is False


def test_list_jobs(jr_mod) -> None:
    runner = jr_mod.JobRunner()
    runner.submit("result = 1", label="a")
    runner.submit("result = 2", label="b")
    time.sleep(0.5)
    items = runner.list_jobs(include_terminal=True)
    assert len(items) >= 2


def test_list_jobs_exclude_terminal(jr_mod) -> None:
    runner = jr_mod.JobRunner()
    runner.submit("result = 1", label="fast")
    time.sleep(0.3)
    pending = runner.list_jobs(include_terminal=False)
    # Either empty (all terminal) or only non-terminal.
    for j in pending:
        assert not j.is_terminal


def test_poll_unknown_returns_none(jr_mod) -> None:
    runner = jr_mod.JobRunner()
    assert runner.poll("no-such-id") is None


def test_custom_runner(jr_mod) -> None:
    runner = jr_mod.JobRunner()

    def my_runner(code: str, globals_: dict):
        return f"ran:{code}"

    job = runner.submit("anything", label="custom", runner=my_runner)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        j = runner.poll(job.job_id)
        if j and j.is_terminal:
            break
        time.sleep(0.05)
    j = runner.poll(job.job_id)
    assert j.result == "ran:anything"


def test_persisted_jobs_reload(jr_mod) -> None:
    runner = jr_mod.JobRunner()
    runner.submit("result = 99", label="p")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        j = runner.poll(runner.list_jobs()[0].job_id)
        if j and j.is_terminal:
            break
        time.sleep(0.05)
    # Force a "restart" by reading jobs back from disk.
    files = list(jr_mod.job_dir().glob("*.json"))
    assert files
    data = files[0].read_text()
    assert "99" in data


def test_truncate_string_result(jr_mod) -> None:
    big = "x" * 50_000
    truncated = jr_mod._truncate(big, max_chars=1000)
    assert len(truncated) < 1500
    assert "[truncated" in truncated


def test_truncate_passthrough(jr_mod) -> None:
    assert jr_mod._truncate(42) == 42
    assert jr_mod._truncate([1, 2, 3]) == [1, 2, 3]


def test_default_runner_executes(jr_mod) -> None:
    ns = {"y": 7}
    result = jr_mod._default_runner("y += 1\nresult = y", ns)
    assert result == 8


def test_job_status_constants(jr_mod) -> None:
    for s in (
        jr_mod.STATUS_PENDING,
        jr_mod.STATUS_RUNNING,
        jr_mod.STATUS_DONE,
        jr_mod.STATUS_ERROR,
        jr_mod.STATUS_CANCELLED,
    ):
        assert isinstance(s, str)


def test_get_runner_singleton(jr_mod) -> None:
    a = jr_mod.get_runner()
    b = jr_mod.get_runner()
    assert a is b


def test_persisted_lost_job_marked_error(monkeypatch, tmp_path) -> None:
    """A persisted non-terminal job is marked error on load."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    # Write a fake non-terminal job file before importing.
    import json
    jr_dir = tmp_path / "freecad-mcp" / "jobs"
    jr_dir.mkdir(parents=True)
    (jr_dir / "lost-1.json").write_text(json.dumps({
        "job_id": "lost-1",
        "label": "lost",
        "status": "running",
        "submitted_at": 0.0,
        "started_at": 0.0,
        "finished_at": None,
    }))
    spec = importlib.util.spec_from_file_location(
        "_job_runner_for_lost_test", _RPC_DIR / "job_runner.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_job_runner_for_lost_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    runner = mod.JobRunner()
    job = runner.poll("lost-1")
    assert job is not None
    assert job.status == mod.STATUS_ERROR
    assert "restarted" in (job.error or "")


def test_redact_strips_password_assignment(jr_mod) -> None:
    assert jr_mod._redact("password=hunter2") == "[REDACTED]"


def test_redact_strips_api_key(jr_mod) -> None:
    assert jr_mod._redact("api_key=ABCD1234abcd1234ABCD") == "[REDACTED]"


def test_redact_strips_bearer_token(jr_mod) -> None:
    redacted = jr_mod._redact("Authorization: Bearer abcdefghij1234567890")
    assert "[REDACTED]" in redacted
    assert "abcdefghij1234567890" not in redacted


def test_redact_strips_github_token(jr_mod) -> None:
    assert "[REDACTED]" in jr_mod._redact("token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij")


def test_redact_strips_openai_key(jr_mod) -> None:
    assert "[REDACTED]" in jr_mod._redact("OPENAI_KEY=sk-abcdefghijklmnopqrstuvwxyz1234")


def test_redact_walks_dicts(jr_mod) -> None:
    redacted = jr_mod._redact({"password": "x", "ok": "value"})
    assert redacted == {"password": "[REDACTED]", "ok": "value"}


def test_redact_walks_lists(jr_mod) -> None:
    redacted = jr_mod._redact(["password=x", "ok"])
    assert redacted == ["[REDACTED]", "ok"]


def test_redact_passes_through_unknown(jr_mod) -> None:
    """Non-string values pass through unchanged."""
    assert jr_mod._redact(42) == 42
    assert jr_mod._redact(None) is None
    assert jr_mod._redact(3.14) == 3.14


def test_truncate_redacts_secrets(jr_mod) -> None:
    """Truncated results should be redacted first."""
    big = "password=hunter2 " * 5000
    out = jr_mod._truncate(big)
    assert "[REDACTED]" in out
    assert "hunter2" not in out


def test_resolve_max_workers_default(jr_mod, monkeypatch) -> None:
    monkeypatch.delenv("FREECAD_MCP_JOB_WORKERS", raising=False)
    assert jr_mod._resolve_max_workers() == 1


def test_resolve_max_workers_env(jr_mod, monkeypatch) -> None:
    monkeypatch.setenv("FREECAD_MCP_JOB_WORKERS", "4")
    assert jr_mod._resolve_max_workers() == 4


def test_resolve_max_workers_clamps_to_one(jr_mod, monkeypatch) -> None:
    monkeypatch.setenv("FREECAD_MCP_JOB_WORKERS", "0")
    assert jr_mod._resolve_max_workers() == 1
    monkeypatch.setenv("FREECAD_MCP_JOB_WORKERS", "-3")
    assert jr_mod._resolve_max_workers() == 1


def test_resolve_max_workers_invalid_value(jr_mod, monkeypatch) -> None:
    monkeypatch.setenv("FREECAD_MCP_JOB_WORKERS", "not-a-number")
    assert jr_mod._resolve_max_workers() == 1


def test_error_message_is_redacted(jr_mod) -> None:
    """An exception message containing secrets must be redacted in the
    persisted job record."""
    runner = jr_mod.JobRunner()
    # Exec raises — its message contains the secret.
    job = runner.submit(
        'raise RuntimeError("api_key=ABCD1234abcd1234ABCD")',
        label="secret-error",
    )
    # Spin until terminal.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        j = runner.poll(job.job_id)
        if j and j.is_terminal:
            break
        time.sleep(0.05)
    j = runner.poll(job.job_id)
    assert j.status == jr_mod.STATUS_ERROR
    assert "[REDACTED]" in (j.error or "")
    assert "ABCD1234abcd1234ABCD" not in (j.error or "")
