# mcp-freecad — repo notes

## Bridge architecture (Aug 2026)

- `freecad_mcp/_mcp_tool_loop.py` — shared driver for both bridges
  - `ToolLoop` dataclass holds `messages`, `last_reply`, `iterations_used`
  - `run_tool_loop(session, init_msgs, *, pick_message, pick_tool_calls,
    pick_content, call_one_step, max_iterations)` is callback-based
  - **Important**: pickers `pick_tool_calls` and `pick_content` receive
    the **message dict** (already extracted by `pick_message`), NOT the
    full reply. Common bug when porting a new bridge: writing
    `lambda r: r.get("choices")[0].get("message", {}).get("content")`
    in `pick_content` produces empty strings. Use
    `lambda msg: msg.get("content")` instead.

## Testing async without pytest-asyncio

- Project does not depend on `pytest-asyncio` (decision from prior
  audit). Pattern: define `def _run(coro): return asyncio.run(coro)`
  helper, write tests as `def test_…(): …; res = _run(mtl.run_tool_loop(...))`.
- For HTTP serve tests use `urllib.request.urlopen` (with
  `urllib.error.HTTPError` for non-2xx) so the global `httpx.post`
  monkeypatch doesn't intercept the test client itself.

## Back-compat aliases

- `ollama_bridge._mcp_tool_to_ollama` and `_result_to_text` are
  re-exports of `_mcp_tool_loop.mcp_tool_to_openai` and
  `_result_to_text`. Keep them as module-level aliases so external
  callers don't break when refactoring.

## Run tool count after gauntlet-2

- 642/642 passed
- coverage 93.69% (≥85% required)
- src/freecad_mcp/: 18 modules, ruff + mypy clean
