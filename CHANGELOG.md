# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.8] — 2026-08-28

### Fixed — Ollama 4xx no longer hard-fails the dispatch

Two complementary hardening changes that make the dock panel's
Ollama dispatch *robust* against transient shape mismatches between
the FastMCP-emitted tool specs and whatever Ollama build the user
happens to run:

- **Diagnostic dump, hardened** — `_post_json` now writes
  `/tmp/freecad-mcp/last_request_body.json` **before every**
  upstream call, so even a successful dispatch leaves a forensic
  snapshot. On 4xx, the same helper that wrote the body also
  dumps the failing request + Ollama response to
  `/tmp/freecad-mcp/ollama_400_<unix_ts>.json`. The 4xx branch
  defensively wraps `e.response.status_code` /
  `e.response.text` in try/except so the dump never gets bypassed
  by an unexpected httpx edge case (e.g. `e.response` being `None`).
- **Automatic fallback to no-tools retry** — when `_post_json`
  raises on the *first* call with `tools`, the bridge retries the
  same request with the `tools` field stripped. The model still
  answers (text-only), the user gets a usable response, and the
  diagnostic dump captures the failing body for root-cause
  analysis. The fallback only fires when the request body
  originally had `tools` — a 4xx that happens after a tool result
  was already attached still propagates so real errors aren't
  hidden.

### Added — tests

- 3 new tests in `tests/test_ollama_no_tools_fallback.py`:
  - `test_fallback_returns_no_tools_response_when_400` — verify
    the retry path.
  - `test_post_json_dumps_body_even_when_call_raises` — verify the
    last_request_body.json dump happens even on 4xx.
  - `test_send_retries_without_tools_on_400` — verify the
    bridge-level retry strips `tools` and returns the second
    call's response.

### Verified

- 971 pytest tests pass (3 new + previous 968), ruff + mypy clean.
- Headless smoke gauntlet inside the Flatpak sandbox completes the
  full bridge roundtrip without 4xx.

## [1.1.7] — 2026-08-28

### Fixed — Ollama 400 Bad Request on tool-call loop

- **`_mcp_tool_loop.sanitize_messages_for_llm`** (new) — normalizes
  the chat message list before each upstream call. Two problems were
  tripping Ollama's request validator with
  `HTTP 400 "Value looks like object, but can't find closing '}' symbol"`:
  - **`tool_calls[].function.arguments`** — Ollama's current parser
    only accepts the **object** form (`{"x":1}`); some models and
    older Ollama builds emit the **string** form (`'{"x":1}'`).
    Sanitizer converts the string form back to an object.
  - **`thinking`** — the model adds a `thinking` block to its own
    replies; re-sending that verbatim makes Ollama 400. Sanitizer
    strips it.
  - LM Studio and OpenAI-compatible backends keep working: the
    helper only rewrites the fields that are problematic for *any*
    backend, leaving the rest untouched.
- Both bridges (`ollama_bridge`, `lmstudio_bridge`) now call
  `sanitize_messages_for_llm(loop.messages)` before each upstream
  call, so the fix applies to the whole tool-call loop, not just the
  first iteration.

### Added — tests

- 9 new tests in `tests/test_sanitize_messages.py` covering: stripping
  `thinking`, converting string arguments to objects, leaving object
  arguments alone, empty-string arguments, passthrough for
  user/tool messages, handling messages without `tool_calls`, leaving
  malformed JSON untouched (best-effort), normalizing multiple
  `tool_calls`, and verifying the input list is never mutated.

### Verified

- 966 pytest tests pass (1 pre-existing LM-Studio test deselected).
- `ruff check src/ addon/ tests/` clean.
- `mypy src/freecad_mcp/` clean.
- Headless smoke gauntlet inside the Flatpak sandbox goes end-to-end
  with the model now calling tools (server logs show
  `Processing request of type CallToolRequest`) instead of 400-ing on
  the second iteration.

## [1.1.6] — 2026-08-28

### Fixed — Flatpak sandbox in-process bridge

- **`_in_process_bridge.py`** (new) — runs the Ollama → MCP bridge
  inside the FreeCAD process when the dock panel detects a sandbox
  (`/.flatpak-info`, `SNAP_NAME`, or `APPIMAGE`). This avoids the
  previous "spawn host python" path that failed because the
  Flatpak-bundled host `python3` symlinks to a path the Flatpak
  cannot `X_OK`. The bridge reuses `/usr/bin/python3.13` (the
  real interpreter inside the sandbox) and patches
  `mcp.client.stdio._create_platform_compatible_process` so the
  subprocess's `stderr` is a real file (the FreeCAD GUI's
  `sys.stderr` is a QTextEdit adapter with no `fileno()`).
- **`freecad_mcp/__main__.py`** (new) — entry point for
  `python -m freecad_mcp`, forwards to `freecad_mcp.server.main`.
  This is what the in-process bridge spawns (previously it spawned
  `python -m freecad_mcp.server`, which only imports the module and
  exits, leaving the stdio transport with no server listening —
  the root cause of the `McpError: Connection closed` log in 1.1.5).
- **`_panel.py`** — added `_is_sandboxed()`, `_start_in_process()`,
  `_build_subprocess_env()`, and `_find_venv_python()` methods. The
  panel now picks between the subprocess (host dev) and in-process
  (sandbox) execution paths automatically. The fallback for
  `_resolve_repo_root` was rewritten to use `in self.__dict__` so it
  is robust to Qt subclasses that may define `__getattr__`.
- **Forwarding `PYTHONPATH` to the MCP subprocess** so the in-process
  bridge can locate `mcp` + `freecad_mcp` inside the Flatpak sandbox
  without requiring a system-wide `pip install`.

### Verified

- Headless smoke gauntlet (`/tmp/fc-headless/headless_smoke.py`)
  inside the Flatpak sandbox goes end-to-end:
  workbench activated → RPC server up → panel obtained → sandbox
  detected → in-process bridge starts → MCP session initialized
  (`Processing request of type ListToolsRequest`) → Ollama answers
  with tool-aware guidance.
- 957 pytest tests pass (1 pre-existing LM-Studio test deselected).
- `ruff check src/ addon/ tests/` clean.
- `mypy src/freecad_mcp/` clean.

## [1.1.5] — 2026-08-28

### Fixed

- **`ollama_bridge` urllib fallback** — when launched from the dock
  panel against a Python that doesn't have `httpx` installed (the
  common Flatpak + system-`python3` case), the bridge crashed with
  `ModuleNotFoundError: No module named 'httpx'`. The single
  `httpx.post` call site is now wrapped in a `_post_json(url, body,
  timeout)` helper that uses `httpx` when importable and falls back
  to `urllib.request` otherwise. No behavior change for callers
  that already have httpx.

### Added — tests

- 2 new tests in `test_ollama_bridge.py` exercising both the httpx
  and urllib code paths with tiny in-process HTTP servers.

## [1.1.4] — 2026-08-28

### Added — Ollama model picker

- **`addon/FreeCADMCP/rpc_server/_ollama_models.py`** — new helper
  that talks to Ollama's `/api/tags` endpoint over plain
  `urllib.request` (no extra deps, runs inside the FreeCAD PySide
  runtime). Exposes `OllamaModelInfo` (name, family, parameter
  size, quantization level, capabilities, digest) and
  `list_ollama_models(url=None, timeout=3.0)` returning an
  `OllamaListResult` with structured error info.
- **Dock panel model picker** — `QLineEdit` swapped for an
  editable `QComboBox` populated from `/api/tags` on startup
  and via a `⟳` refresh button next to the field. Each entry
  shows `name — family — size — quantization — capabilities` in
  the dropdown while `userData` keeps the bare model name
  used in the `--model` flag.
- **Ollama host field** — new `OLLAMA_HOST` row above the
  prompt; edit + `editingFinished` triggers a refresh. Empty
  falls back to the `OLLAMA_HOST` env var, then
  `http://127.0.0.1:11434`.
- **Selection preservation** — refresh repopulates the combo
  but restores the previous selection if it's still installed.

### Added — tests

- `tests/test_ollama_models.py` — 10 tests covering happy
  path, OLLAMA_HOST env var, TCP unreachable, HTTP 502,
  malformed JSON, non-list payload, malformed entries, empty
  host.
- `tests/test_panel_model_picker.py` — 7 tests covering
  combo population, previous-selection preservation,
  "ghost" model fallback, Ollama-down/HTTP-error logging,
  signals blocking during refresh, env-only host.

## [1.1.3] — 2026-08-28

### Fixed

- **Ollama bridge dispatch** — when the system Python did not have
  `freecad_mcp` installed (the common Flatpak install case), the
  dock panel emitted `ModuleNotFoundError: No module named 'freecad_mcp'`
  the moment you pressed **Enviar**. The dispatch now resolves the
  repo root via three strategies in order:
  1. `$FREECAD_MCP_REPO_ROOT` env var.
  2. `~/.config/freecad-mcp/repo-root` plain-text config file.
  3. Walk up from the addon module looking for `pyproject.toml`.

  When a repo with a `src/` layout is detected, the subprocess is
  launched with `PYTHONPATH=src/` via `env`, so the system Python
  can import `freecad_mcp` without `pip install`.

### Added — tests

- `tests/test_panel_dispatch.py` — 5 tests covering env var,
  config file, dot-venv fast path, and the "nothing available"
  error path.

## [1.1.2] — 2026-08-27

**Theme: 5 new feature suites from competitive analysis.** 25 new
MCP tools, 5 new addon modules. Brings the project to **53 total
MCP tools** and **843 tests passing**.

### Added — Inspection & Measurement suite (7 tools)

Per competitive analysis, no other FreeCAD MCP server combines
FEM with a full inspection/measurement API. New tools:

- **`list_faces(doc, obj, type_filter?, limit?)`** — per-face type
  / normal / centroid / area. `type_filter` is a case-insensitive
  substring ("Cylinder", "Plane", "Sphere", ...).
- **`measure(doc, obj, properties?)`** — volume, area, bbox, COM,
  length, edge/face/vertex count.
- **`measure_distance(doc, obj_a, obj_b)`** — `BRepExtrema` with
  bbox fallback.
- **`geometric_verification(doc, obj, handedness_tol?)`** —
  emptiness, OCCT validity, inertia-matrix handedness, normal
  consistency.
- **`analyze_shape(doc, obj)`** — counts of each surface type
  (Plane/Cylinder/Cone/Sphere/Torus/B-Spline).
- **`spatial_query(doc, obj_a, obj_b, mode?, clearance_tol?)`** —
  `interference` / `clearance` / `containment` modes.
- **`sketch_diagnostics(doc, sketch)`** — DOF, conflicts,
  redundancies, fully_constrained flag.
- **`recompute_diff(doc, obj, expected_volume?)`** — before/after
  metrics with optional volume delta.

### Added — Multi-instance management (5 tools)

Discovery at `~/.cache/freecad-mcp/instances/<uuid>.json`:

- **`list_freecad_instances(max_age_seconds?)`** — live instances
  with UUID, label, PID, host, port, started_at, latency.
- **`spawn_freecad_instance(label?, host?, port?, ...)`** —
  register a new instance and mark active.
- **`select_freecad_instance(uuid)`** — switch active target with
  TCP probe + latency measurement.
- **`stop_freecad_instance(uuid)`** — unregister from discovery.
- **`instance_status(uuid?)`** — health + latency.

Each FreeCAD session auto-registers on `start_rpc_server()` so the
MCP server can find it.

### Added — Async execute + job management (5 tools)

Long-running ops no longer block the MCP client:

- **`execute_code_async(code, label?)`** — submit to background
  worker, returns `job_id` immediately.
- **`poll_job(job_id)`** — status / result / error / traceback.
- **`list_jobs(include_terminal?)`** — all known jobs.
- **`cancel_job(job_id)`** — cooperative cancellation.
- Job state persisted to `~/.cache/freecad-mcp/jobs/<id>.json`
  so jobs survive MCP-server restarts.

### Added — Live API introspection (2 tools)

Reduces LLM errors by validating callables before invocation:

- **`api_introspect(path)`** — `inspect.Signature` + docstring of
  any `Part.makeBox`, `FreeCAD.Vector`, etc.
- **`api_search(query, modules_filter?, limit?)`** — fuzzy/regex
  search across `FreeCAD`, `FreeCADGui`, `Part`, `Mesh`, `Path`,
  `Fem`, `Arch`, `Spreadsheet`, `Draft`, `TechDraw`, `Sketcher`,
  `math`, `os`.

### Added — CAM / Path toolpath (6 tools)

Closes the design→manufacturing loop. Uses the `Path` workbench
when available; otherwise returns `{"success": False, "reason":
"Path workbench not available"}`:

- **`cam_create_tool(doc, name, type, diameter, length, material)`**
  — `EndMill`, `BallEndMill`, `Drill`, `CounterSink`, etc.
- **`cam_create_tool_controller(doc, name, tool, spindle, feed,
  feed_v)`** — binds a tool to spindle/feed rates.
- **`cam_create_job(doc, name, base_shape?, tool_controller?,
  stock_x/y/z?)`** — `Path::Job` with stock extents.
- **`cam_add_operation(doc, job, op_type, name, ...)`** —
  `profile`, `pocket`, `adaptive`, `drilling`, `face`.
- **`cam_post_process(doc, job, post_processor?, output_path?)`**
  — emits G-code via `linuxcnc` / `grbl` / `marlin` / `smoothie`
  / `haas` / `mach3_mach4` / `toshiba`.
- **`cam_simulate_toolpath(doc, job, max_segments?)`** —
  downsampled backplot ready for client-side rendering.

### Added — server-side modules

- **`addon/FreeCADMCP/rpc_server/inspection.py`** — list_faces,
  measure, geometric_verification, analyze_shape, spatial_query,
  recompute_diff, sketch_diagnostics.
- **`addon/FreeCADMCP/rpc_server/multi_instance.py`** —
  register_instance, list_instances, select_instance, set_active,
  discovery directory at `~/.cache/freecad-mcp/instances/`.
- **`addon/FreeCADMCP/rpc_server/job_runner.py`** —
  `JobRunner` (single-worker `ThreadPoolExecutor`), JSON-backed
  job persistence, cooperative cancel.
- **`addon/FreeCADMCP/rpc_server/api_introspect.py`** —
  `inspect.signature` + docstring extraction with regex/substring
  search.
- **`addon/FreeCADMCP/rpc_server/cam_ops.py`** — Path workbench
  wrappers (tool, controller, job, ops, post-process, simulate).

### Added — tests

66 new unit tests across:
- `tests/test_multi_instance.py` — discovery, register, prune,
  probe, active UUID.
- `tests/test_api_introspect.py` — signature, class metadata,
  substring/regex search, default module set.
- `tests/test_job_runner.py` — submit / poll / cancel / error,
  persistence, truncation, custom runner, lost-job recovery.
- `tests/test_inspection.py` — face listing, measure,
  geometric_verification (mirrored & right-handed), analyze_shape,
  spatial_query (interference/clearance/containment), recompute_diff,
  sketch_diagnostics (DOF/conflicts/redundancies).
- `tests/test_cam_ops.py` — tool creation, op validation,
  simulation backplot, missing-doc/missing-job error paths.

### Changed

- `PER_OPERATION_TIMEOUTS` extended with 25 new entries (5-600s).
- `ALL_TOOL_NAMES` extended to 53 tools.
- `start_rpc_server()` auto-registers the instance in the discovery
  cache so external tools can find it.
- `conftest.py`, `test_rpc_server_status.py`,
  `test_rpc_server_object_gui.py` synthetic loaders updated to
  include the 5 new submodules.

## [1.1.0] — 2026-08-27

**Theme: Observability & reproducibility.** Six new MCP tools, four
new MCP resources, four new in-process modules, and five new
operator-facing upgrades to the FreeCAD dock panel. **776 tests
passing** (up from 692), ruff & mypy clean.

### Added — server-side modules

- **`freecad_mcp.streaming`** — `OutputBuffer` + `ProgressDebouncer`
  + `stream_output(ctx)`. Captures every line the FreeCAD `execute_code`
  response contains (after stripping the `Output: ` and
  ``
  `Python code execution scheduled.` prefixes) and reports progress
  via the FastMCP `Context.report_progress` coroutine so MCP host
  UIs see incremental updates. Thread-safe under `threading.Lock`.
- **`freecad_mcp.replay`** — `SessionRecorder` (thread-safe under
  `threading.Lock`) appends every dispatched tool call to a JSON file
  with atomic `tmp + fsync + os.replace`. Replays land in
  `~/.config/FreeCAD/mcp-freecad/replays/<session_id>.json`. The
  `get_replay(session_id, format="replay")` tool can replay against
  the live connection; destructive tools are skipped by default
  (`dry_run=True`) and require `allow_destructive=True` to actually
  run. Export formats: `json`, `markdown`, `replay`.
- **`freecad_mcp.profiler`** — `PerformanceProfiler` ring buffer
  (default 1 000 entries, thread-safe under `RLock`) with per-tool
  percentile stats (`count`, `mean_ms`, `p50_ms`, `p95_ms`,
  `p99_ms`, `max_ms`) and a collapsed-stack flamegraph export
  (`tool_name count duration_ms`) ready for Brendan Gregg's
  `flamegraph.pl`. Singleton accessor `get_profiler()` and a
  decorator `_profile_decorator` (re-exported as `profile_tool`
  in `server.py`) wrap every tool call. Slow calls above
  `FREECAD_MCP_SLOW_THRESHOLD_MS` (default 500) are logged at INFO.
- **`freecad_mcp.diff`** — `DocumentDiff` dataclass with
  `objects_added`, `objects_removed`, `objects_modified`,
  `objects_unchanged` plus per-property diffs (`properties_added`,
  `properties_removed`, `properties_modified`). Renders as a
  Markdown summary or a full JSON tree via `as_dict()`.
- **`freecad_mcp.workflows`** — `Workflow` + `WorkflowStep` +
  `WorkflowRegistry`. Three built-ins
  (`create-box-with-save`, `safe-execute`, `duplicate-object`),
  JSON persistence to `~/.config/FreeCAD/mcp-freecad/workflows.json`,
  two-pass template substitution (`{var}` user vars first, then
  `{prev.X}` dotted-path references to the previous step's return
  value), `optional=True` steps that skip on raised exception,
  and a guidelines pre-check for `execute_code`.
- **`freecad_mcp.web_ui`** — `create_web_app(freecad, ollama_url,
  default_model)` FastAPI factory with `GET /`, `POST /ask`,
  `GET /health`, `GET /docs`. Launch with `python -m freecad_mcp.web_ui`
  (port `FREECAD_MCP_WEB_PORT`, default 8765). Opt-in deps:
  `pip install fastapi httpx`.

### Added — MCP tools (6)

* `diff_documents(doc_a, doc_b)` — structured diff between two open
  FreeCAD documents.
* `list_workflows()` — return every registered workflow
  (built-in + user).
* `run_workflow(name, args)` — execute a registered workflow against
  the live connection.
* `get_profiler_stats()` — per-tool percentile stats + flamegraph
  export from the in-process profiler.
* `list_replays()` — list every recorded session replay on disk.
* `get_replay(session_id, format, dry_run)` — fetch a replay as JSON,
  Markdown, or re-execute it (`dry_run=True` by default).

### Added — MCP resources (4)

Read-only data surfaces the MCP client can attach without invoking a
write tool:

* `freecad://server/policy` — the currently effective tool policy
  (enabled set, denylist, elevated-tool gate, all known tools).
* `freecad://server/metrics` — Prometheus-style snapshot of every
  counter, gauge, and histogram in the registry.
* `freecad://server/profiler` — same payload as `get_profiler_stats`.
* `freecad://server/replay-dir` — the on-disk directory used to
  persist session replays.

### Added — FreeCAD dock panel (addon side)

* **Prompt template gallery** — `_prompt_templates.py` with five
  built-ins (`Listar documentos`, `Health check`, `Caixa 10x10x10`,
  `Auditoria FEM`, `Diff de documentos`), a `QComboBox` in the dock
  to insert them into the prompt field, and persistence to
  `~/.config/FreeCAD/mcp-freecad/prompt_templates.json`.
* **Dark / light / auto theme** — `cycle_theme()` toggles between
  the three. `auto` follows the FreeCAD main window's palette
  luminance. Override with `FREECAD_MCP_PANEL_THEME`.
* **Resizable log view** — the log `QPlainTextEdit` is now
  `Expanding`-policy so it fills the dock when the user resizes it.

### Changed

- **`ALL_TOOL_NAMES` extended** in `tool_policy.py` with the six new
  tools. `test_all_tool_names_known` now asserts the 24-tool set.
- **`freecad_mcp.server.execute_code`** wraps the operation in an
  `OutputBuffer` and calls `_observe_tool_call` / `_finalize_tool_call`
  so every code execution is recorded by the `SessionRecorder` even
  on exceptions.
- **`ProfileEntry`** uses `dataclass` (was `dict`) and `PerformanceProfiler`
  uses `collections.deque(maxlen=N)` for O(1) ring-buffer eviction.

### Tests

- **84 new tests** across seven new files: `test_streaming.py`,
  `test_replay.py`, `test_profiler.py`, `test_diff.py`,
  `test_workflows.py`, `test_web_ui.py`, `test_prompt_templates.py`.
  Coverage of the new modules is 100 %. Total: **776 tests passing in
  ~12s**, ruff 0 errors, mypy 0 errors (24 source files).
- The shared PySide/FreeCAD stub in `tests/conftest.py` grew
  `QComboBox` and `QSizePolicy` so the dock panel can be instantiated
  in unit tests.

### Bug fixes caught during integration

- **`_lookup_replay_method` was eagerly evaluating
  `connection.create_document`**, raising `AttributeError` on test
  stubs missing one of the 18 methods. Fixed with `getattr(connection,
  tool_name, None)`.
- **Built-in workflow templates used the MCP tool argument shape**
  (`obj_type` / `obj_name` / `obj_properties`) but `WorkflowRegistry`
  calls `connection.create_object(obj_data=…)`. Fixed by switching
  the templates to the `obj_data` dict format.
- **`SessionRecorder` was missing `__len__`** — `len(rec)` raised
  `TypeError`. Added.
- **mypy rejected `WorkflowRegistry.list`** because the method name
  shadowed the built-in `list` type. Fixed by annotating as
  `builtins.list[str]` instead of `list[str]`.
- **mypy rejected the new MCP tool return types** (`list[TextContent]`)
  because `text_response` returns `list[TextContent | ImageContent]`.
  Fixed by importing `ToolResponse` and using it as the return type.

## [1.0.4 (Unreleased prior entry)]

**Theme: Wider LLM support.** Allows the MCP server to be driven
by Ollama (and any OpenAI-API-shaped runtime) without first-class
MCP support, plus a headless-render fallback for the FreeCAD
addon. 38 new tests, 4 new dispatch tests, 93.69 % coverage
(up from 86.68 %).

### Added

- **`freecad_mcp.ollama_bridge`** — new module: 60-statement
  bridge (≤100 lines requirement met) that opens a stdio MCP
  session to `mcp-freecad`, converts MCP `ToolDescription` to
  Ollama / OpenAI function-calling schema, and runs a tool-call
  loop. `CircuitBreaker` guards the HTTP transport; tool errors
  are surfaced back to the model instead of propagating. Verified
  end-to-end with `qwen3.6:27b` on Ollama 0.32.14 — two-step
  loops with dict-argument parsing and error-envelope
  roundtripping both work. See `docs/OLLAMA_BRIDGE.md`.
- **`freecad_mcp.lmstudio_bridge`** — new module: 103-statement
  bridge for **any** OpenAI-API-shaped LLM host (LM Studio Local
  Server, llama.cpp `--server`, vLLM HTTP backend, etc). Exposes
  two surfaces: an in-process `LMStudioMCPBridge.ask()` (mirrors
  the Ollama bridge) **and** a stdlib `ThreadingHTTPServer` that
  proxies `/v1/chat/completions` to the MCP session — letting
  OpenAI-shaped clients drive FreeCAD without modifying the
  upstream LLM. Zero new runtime deps. See
  `docs/LM_STUDIO_BRIDGE.md`.
- **`freecad_mcp._mcp_tool_loop`** — new shared driver module:
  abstracts the tool-call loop (callback picker functions +
  `ToolLoop` state machine + `_dispatch_tool`) so the two
  bridges share the same loop logic and behave identically on
  errors / overflow / max-iteration. 56 statements.
- **`docs/OLLAMA_BRIDGE.md`** + **`docs/LM_STUDIO_BRIDGE.md`** —
  two new guides covering the OpenAI-API-shaped bridge pattern
  for non-MCP LLM hosts.
- **15 new tests** covering the shared loop driver, the LM
  Studio in-process ask path, and the HTTP proxy end-to-end
  (live `ThreadingHTTPServer` + POST `/v1/chat/completions` +
  404 + 400 JSON-parse error).
- **`pytest-asyncio` (≥0.23) added to dev deps** + `asyncio_mode
  = auto` in `pytest.ini`. The 9 bridge/loop tests that used to
  wrap every coroutine in `asyncio.run(...)` are now plain
  `async def test_*` functions, which read closer to the code
  they exercise and run faster (no per-test event-loop
  spin-up in the harness wrapper).

### Fixed

- **`_flush_gui_events` is now headless-safe.** When run under
  `flatpak run --command=python3` (or any interpreter without
  an active Qt event loop), the previous call to
  `QtCore.QThread.msleep` would block indefinitely. The helper
  now detects missing `QApplication.instance()` and `updateGui`
  failures and falls back to plain `time.sleep`. The MCP probe
  from the FreeCAD Flatpak gauntlet can now complete a
  `system.listMethods` round-trip without the GUI helper hanging.
  4 new tests in `tests/test_dispatch_module.py` cover
  updateGui-raises, missing app instance, processEvents failure,
  and msleep failure paths.

### Documentation

- **README** — added the Flatpak install path
  (`~/.var/app/org.freecad.FreeCAD/data/FreeCAD/v1-1/Mod/`)
  with its `cp` command, alongside the existing Ubuntu / Debian /
  Arch / macOS paths.

## [1.0.3] — 2026-08-26

**Theme: Hardening & observability.** Audit gauntlet result — every
item from the tier plan delivered. 401 tests (up from 318), 65 %
coverage (up from 61 %), ruff & mypy clean.

### Bug Fixes

- **`add_screenshot_if_available` hardcoded `mimeType="image/png"`**
  even when the screenshot bytes were JPEG/WebP, breaking strict MCP
  clients. The helper now takes an explicit `image_format` argument
  and reports `image/jpeg`, `image/webp`, or `image/png` correctly.
  Fixes get_view / get_active_view responses that transcoded to
  non-PNG formats.
- **`get_active_screenshot` ran two XML-RPC calls** (a
  `_SCREENSHOT_SUPPORT_CHECK` probe via `execute_code`, then the
  actual capture). The race between them could return blank
  screenshots; the happy path paid double latency. The client now
  uses one round-trip and the RPC's structured `{"success": False,
  "reason": ...}` response.
- **Maintenance email in SECURITY.md** still pointed at the upstream
  fork's author (`nekanat.stock@gmail.com`). Updated to
  `yuri.schmaltz@gmail.com`.
- **`schemas._check_type` was a silent no-op** for unknown FreeCAD
  TypeId prefixes. Now logs a warning so LLM typos surface at the
  MCP layer instead of failing with a vague `Fault` downstream.

### Added

- **`FREECAD_MCP_LOAD_GABARITO` flips to ON by default when the
  system locale is PT-BR** (`LANG=pt*`, `LC_ALL=pt*`, `LC_MESSAGES=pt*`).
  Portuguese-speaking operators running `uvx mcp-freecad` get the
  directive set without extra configuration. English speakers keep
  the short English fallback. Explicit env vars always win.
- **New `get_active_screenshot_with_status`** returns a structured
  `{"success", "screenshot", "format"}` or `{"success": False,
  "reason": "rpc_error"|"no_capture"|"view_unsupported"|...}` so
  callers can distinguish "view wrong" from "RPC dead". The legacy
  `get_active_screenshot` (returning b64 or `None`) is preserved for
  back-compat.
- **`RequestTracker.cancel_all_pending()` and `invalidate_cache()`**
  — bulk-flush helpers for shutdown paths and "underlying state has
  changed, drop cached idempotent answers" scenarios. Exposed as RPC
  methods (`cancel_all_pending_requests`, `invalidate_idempotency_cache`)
  and through the `FreeCADConnection` client.
- **`pydantic>=2.0` declared** in `requirements.txt` and
  `pyproject.toml` (was a transitive install).
- **`pytest --strict-markers`** so typos in `@pytest.mark.X` fail
  the suite instead of being silently ignored.
- **`@pytest.mark.slow` applied** to network-bound tests; can be
  skipped via `pytest -m "not slow"` for a fast feedback loop.

### Refactor

- **`validate_allowed_ips` extracted** to a pure
  `_ip_allowlist.py` module (testable without FreeCAD). The
  `rpc_server.py` wrapper is now a thin re-export of
  `parse_allowlist` / `parse_allowlist_to_networks`. 26 new unit
  tests cover every edge case (malformed-list rejection, wildcard
  refusal, IPv6, empty input, etc.).
- **`SCREENSHOT_SUPPORT_CHECK` removed** — the probe snippet is no
  longer needed because `get_active_screenshot` returns structured
  failure reasons directly.

### Tests

- 83 new tests across `test_ip_allowlist.py`,
  `test_rpc_server_methods.py`, `test_freecad_client.py`,
  `test_responses.py`, `test_metrics.py`, `test_request_tracking.py`,
  and `test_server_module.py`. Total: **401 tests, 65 % coverage**.
- Coverage of `rpc_server.py` improved from 32 % to 39 % via
  direct tests of `_tracked_call`, `health_check`,
  `cancel_all_pending_requests`, `invalidate_idempotency_cache`,
  `undo`/`redo`/`save_document`/`export_object`,
  `_timeout_for` precedence, and `get_active_view`.

### Security

- SECURITY.md now explicitly documents that `execute_code` runs
  `exec(code, globals())` — there is **no sandbox**, the blocklist
  is a guardrail, not a containment boundary. Operators should run
  FreeCAD in a container or VM and disable `execute_code` in
  multi-tenant deployments.

## [1.0.2](https://github.com/yuri-schmaltz/mcp_freecad/compare/v1.0.1...v1.0.2) (2026-08-08)


### Build System

* **ci:** set include-v-in-tag: true to match repo tag convention ([ab78c3c](https://github.com/yuri-schmaltz/mcp_freecad/commit/ab78c3c56ba150c99abd6e0efb6bd8c8ae4382e7))

## [1.0.1](https://github.com/yuri-schmaltz/mcp_freecad/compare/v1.0.0...1.0.1) (2026-08-07)


### Bug Fixes

* **ci:** repair commitlint config and mypy no-any-return ([f321edf](https://github.com/yuri-schmaltz/mcp_freecad/commit/f321edf67e833535aa78272bcbb2ba31909cd98c))
* **deps:** cap mcp&lt;2 to prevent mcp 2.x install breakage ([ec64d68](https://github.com/yuri-schmaltz/mcp_freecad/commit/ec64d680484dcaa70cd4ef23d23ad5e02d97c8da))


### Tests

* skip POSIX-only tests on Windows + relax connect-refused bound ([8858e91](https://github.com/yuri-schmaltz/mcp_freecad/commit/8858e910bd01e003675c6c54847bf17ed4232755))


### Build System

* **ci:** add packages wrapper to release-please v17 config ([4e51d8c](https://github.com/yuri-schmaltz/mcp_freecad/commit/4e51d8c255d2daeab9133ba5d1c84b002bb0f4af))

## [Unreleased]

### Added
- (none yet)

## [1.0.0] — 2026-07-15

**Theme: Cut Oficial.** The project is now an independent package under
`yuri-schmaltz/mcp-freecad` and ships under the `mcp-freecad` PyPI name.

### Breaking changes

- **Distribution name:** `freecad-mcp` → `mcp-freecad` (PyPI, `uvx`,
  `pip install`). Update your `claude_desktop_config.json`
  `mcpServers` entry from `uvx freecad-mcp` to `uvx mcp-freecad`.
- **Console-script entry point:** `freecad-mcp` → `mcp-freecad`.
  Update shell aliases, systemd units, or CI scripts accordingly.
- **Configuration directory:** the addon now writes
  `freecad_mcp_settings.json` under a directory named `mcp-freecad`
  (e.g. `~/.config/mcp-freecad/` on Linux) instead of `freecad-mcp`.
  Existing pre-1.0.0 installs that already have a `freecad-mcp`
  directory will continue to have it read and written — the upgrade
  is non-destructive.
- **Project URL & author in `pyproject.toml`:** all `project.urls`
  now point at `yuri-schmaltz/mcp-freecad`; the author and
  maintainer is `Yuri Schmaltz <yuri.schmaltz@gmail.com>`.

### Added

- Backward-compat path: the addon still honours a pre-existing
  `~/.config/freecad-mcp/` directory so users upgrading from 0.x keep
  their settings.
- New test `test_resolve_uses_legacy_dir_when_present` locks in the
  backward-compat behaviour.
- Explicit `[tool.hatch.build.targets.wheel] packages = ["src/freecad_mcp"]`
  so the wheel builds cleanly with the new distribution name (the
  Python module name `freecad_mcp` is unchanged).
- `Project-URL` entries for Changelog and Security in
  `pyproject.toml`.

### Removed

- `examples/` directory (`adk/agent.py`, `langchain/react.py`,
  `cantilever_fem.py`, `hello_freecad.py`). The example scripts
  required manual path edits and shipped out-of-date install
  instructions. The README covers every supported integration.

### Fixed

- Hatchling build target was previously relying on the implicit
  `src/<package_name>` heuristic; now declared explicitly so the
  wheel always builds regardless of distribution-name changes.
- Invalid classifier `Topic :: Scientific/Engineering :: Computer
  Aided Design (CAD)` removed (it was rejected by current
  packaging versions); replaced with valid alternatives.

### Migration guide

1. Replace `uvx freecad-mcp` with `uvx mcp-freecad` in your
   Claude Desktop (or any MCP host) `mcpServers` config.
2. If you pinned the package, replace `freecad-mcp>=0.4.0` with
   `mcp-freecad>=1.0.0` in your requirements.
3. Your existing `freecad_mcp_settings.json` (which lives in
   `~/.config/freecad-mcp/` on Linux) keeps working — the addon
   will keep reading and writing to that directory until you
   delete it. New installs create `~/.config/mcp-freecad/`.

## [0.4.0] — 2026-07-10

**Theme: from demo to product.** Every Tier 1 (security & reliability
blocker) and Tier 2 (production-grade) item from
`docs/PROFESSIONALIZATION_PLAN.md` is delivered in this release.

### Security (Tier 1)

- **Code blocklist extended** (`src/freecad_mcp/guidelines.py`).
  `execute_code` now also refuses `compile()`, `breakpoint()`,
  `__import__()`, `globals()`, `locals()`, `getattr(__builtins__)`,
  `socket.*`, `urllib.*`, `httpx.*`, `requests.*`, `ftplib.*`,
  `smtplib.*`, `ctypes.*`, `cffi`, `pickle.*`, `marshal.*`,
  `shelve.*`, and the corresponding `import` statements.
  Operators can extend the list at runtime via
  `FREECAD_MCP_BLOCKED_PATTERNS`. New
  `scan_dangerous_tokens()` helper returns the full set of matches
  for log analysis.
- **Tool allow/deny policy** (`src/freecad_mcp/tool_policy.py`).
  Operators can disable dangerous tools via
  `FREECAD_MCP_DISABLED_TOOLS=execute_code` or run in whitelist
  mode via `FREECAD_MCP_REQUIRED_TOOLS=...`. Disabled tools are
  removed from the MCP tool list and answer with a clear error
  when called by name. Misconfiguration (typos, conflicting env
  vars) refuses to start the server.
- **Remote-connections security gate**
  (`addon/.../rpc_server/_security_gate.py`). The RPC server now
  refuses to bind on a non-loopback address without
  `FREECAD_MCP_TLS_CERT` AND `FREECAD_MCP_TLS_KEY` AND
  `FREECAD_MCP_AUTH_TOKEN`. The `ToggleRemoteConnectionsCommand`
  menu item shows a dialog with the same gate and refuses to
  persist the setting if TLS+auth are not configured.
- **Gabarito opt-in.** The Portuguese `gabarito_ia.pdf` directive
  set is no longer loaded by default. Operators who need the
  previous behaviour set `FREECAD_MCP_LOAD_GABARITO=1`. The
  legacy `FREECAD_MCP_NO_DIRECTIVE_PREFIX=1` knob is honoured as
  a force-off override for back-compat.

### Reliability (Tier 1)

- **Circuit breaker** (`src/freecad_mcp/circuit_breaker.py`).
  Every RPC method in `FreeCADConnection` now flows through a
  three-state breaker (closed → open → half_open). Transient
  failures (connection refused, timeout, OS-level errors,
  `xmlrpc.client.ProtocolError`) trigger exponential-backoff
  retry; non-transient errors (`xmlrpc.client.Fault`) propagate
  immediately. The breaker exposes its state via
  `FreeCADConnection.breaker_metrics()` and feeds the
  `health_check` MCP tool. Knobs:
  `FREECAD_MCP_CB_THRESHOLD` (default 3),
  `FREECAD_MCP_CB_RESET_S` (default 60),
  `FREECAD_MCP_RETRY_MAX` (default 3),
  `FREECAD_MCP_RETRY_BASE_S` (default 0.1).

### Production hardening (Tier 2)

- **Pydantic request validation** (`src/freecad_mcp/schemas.py`).
  `create_object` and `edit_object` validate their parameters
  with Pydantic models before reaching FreeCAD. Typos in field
  names (`obj_propertie`) fail loudly; Fem:: types other than
  `Fem::AnalysisPython` refuse to be created without an
  `analysis_name` container.
- **Prometheus-style metrics** (`src/freecad_mcp/metrics.py`).
  In-process registry of counters, histograms, and gauges
  (`freecad_mcp_tool_calls_total`,
  `freecad_mcp_tool_duration_seconds`,
  `freecad_mcp_validation_failures_total`,
  `freecad_mcp_circuit_state`,
  `freecad_mcp_circuit_short_circuits_total`,
  `freecad_mcp_uptime_seconds`). Exposed in `health_check`
  output as JSON and rendered in Prometheus text format via
  `format_prometheus()`. No `prometheus_client` dependency.
- **Structured JSON logging**
  (`src/freecad_mcp/json_logging.py`). `FREECAD_MCP_LOG_FORMAT=json`
  switches the formatter to a single-line JSON shape suitable for
  log shippers. Default remains the human-readable text format.
- **Smoke test suite** (`tests/test_smoke_imports.py`). A
  dedicated test file asserts every public module imports
  cleanly, catching refactor regressions at near-zero cost.
- **Test markers hardened.** `pytest.ini` ships with explicit
  `freecad` and `slow` markers and `-m "not freecad"` by
  default; the `addopts` line in the original 0.3.0 release is
  now part of the committed config.

### Docs

- **`docs/PROFESSIONALIZATION_PLAN.md`** — the full roadmap that
  motivated this release: diagnosis, tier-by-tier scope, criteria
  for "professional", and explicit non-goals.
- **README** — new badges, compatibility matrix, "When NOT to
  use" honesty section, "Production deployment checklist",
  "Monitoring" section with Prometheus scrape config, expanded
  env-var reference.

### Tests

- 195 → **304** tests (added: 12 tool_policy + 3 tool_guard +
  28 guidelines + 14 schemas + 11 metrics + 8 logging +
  9 security_gate + 13 circuit_breaker + 13 smoke imports + 3
  responses updates). All passing in ~7s.
- Coverage: 54% → **63%** total. Critical modules ≥ 80%
  (`guidelines` 99%, `metrics` 98%, `tool_policy` 100%,
  `security_gate` 100%, `circuit_breaker` 89%,
  `operations/core` 91%).
- `ruff check`: clean on `src/` and `tests/`.
- `mypy`: clean on `src/` (15 source files).

### Breaking changes (call out)

- `FREECAD_MCP_NO_DIRECTIVE_PREFIX=1` is now the legacy
  force-OFF knob; the new canonical opt-in is
  `FREECAD_MCP_LOAD_GABARITO=1`. The default for both is
  "no prefix on responses" (was: "always-on Portuguese prefix").
- New dependency: `pydantic>=2.0`.
- The `health_check` tool now returns a `metrics` block by
  default; consumers that parse the response should treat the
  shape as a superset of 0.3.0.
- The RPC server refuses to start with `remote_enabled=true`
  unless TLS+auth are configured; deployments that relied on the
  "enable then realise and add TLS later" flow must set the env
  vars first.

## [0.3.0] — 2026-07-02

### Added
- **TLS support for the XML-RPC server**: set `FREECAD_MCP_TLS_CERT`
  and `FREECAD_MCP_TLS_KEY` to PEM paths; the server will then wrap
  every accepted socket in TLS (TLS 1.2 minimum). Falls back to plain
  HTTP if either env var is missing or invalid (logged loudly).
- **Bearer-token auth**: set `FREECAD_MCP_AUTH_TOKEN` to a shared
  secret; every XML-RPC request must then carry a matching
  `Authorization: Bearer <token>` header. Validation uses
  `hmac.compare_digest` (constant-time).
- **Screenshot in JPEG / WebP**: `get_view` now accepts
  `image_format="jpeg"` (or `"webp"`). FreeCAD's `saveImage` still
  produces PNG internally; the new `_transcode_screenshot` helper
  uses Pillow to convert. If Pillow is not installed, the call
  returns a clear error and the request does not crash.
- **Payload compression for large exports**: new
  `FreeCADConnection.export_object_bytes` returns the exported file
  as a gzipped base64 string when the result is larger than
  `FREECAD_MCP_GZIP_MIN` (default 64 KB). Highly compressible payloads
  shrink by 100x+; tests assert the wire size for all-zeros input.

### Tests
- 177 → 191 (+14): TLS context construction, bad cert fallback,
  bearer-token matching, case-insensitive header, hmac constant-time,
  Pillow transcoding for JPEG and WebP, compression threshold.

## [0.2.0] — 2026-07-02

### Added

#### Security & stability
- **Path traversal protection** in `parts_library.insert_part_from_library`:
  rejects empty, absolute, `..`-bearing, and symlink-escape inputs. Closes C1.
- **XML-RPC timeout** via `_TimeoutTransport` in `freecad_client.py`,
  configurable through `FREECAD_MCP_RPC_TIMEOUT` (default 10s). Closes
  the DoS window in C2 and the A1 hang scenario.
- **Per-operation RPC timeouts** with env-var override
  (`FREECAD_MCP_RPC_TIMEOUTS` JSON). Default: create/edit 30-60s, FEM 600s.
- **Guidelines re-scoped**: `check_code_conflict` (regex with word
  boundaries) applies only to executable strings; `check_prompt_conflict`
  handles agreement-trap phrases in free-form prompts;
  `check_path_conflict` rejects absolute/traversal paths. Operators can
  extend the blocklist via `FREECAD_MCP_BLOCKED_PATTERNS`. Closes C3
  and C4.
- **IP-filter wildcard rejection**: `0.0.0.0/0` and `::/0` are now
  refused by `validate_allowed_ips` with an explicit error. Closes M7.
- **Idempotency + cooperative cancellation**: every tracked RPC method
  now accepts an optional `request_id`. Repeated calls with the same id
  return the cached response; `cancel_request(id)` short-circuits a
  queued task before it runs (FIFO eviction, default capacity 256).
- **FEM workdir cleanup**: CalculiX scratch directories are removed
  after every run via a new `_fem_workdir` helper module. Opt out with
  `FREECAD_MCP_KEEP_FEM_WORKDIR=1` for post-mortem inspection.
- **Thread-safe lifecycle**: `start_rpc_server` / `stop_rpc_server` are
  now wrapped in an `RLock`. `stop` also calls `server_close()` so the
  listening socket is released immediately.

#### Robustness
- `process_gui_tasks` guarantees reschedule under any exception.
- `execute_code` isolates `output_buffer` per request.
- `get_active_screenshot` collapses the view-check and capture into a
  single GUI task (no race between the two steps).
- `parts_library.get_parts_list` invalidates its cache based on
  `(latest_mtime, count)` so newly-dropped FCStd files are visible
  without restart.
- `_get_settings_path` walks a fallback chain (FreeCAD user dir →
  `$XDG_CONFIG_HOME` → `$HOME/.config` → `$HOME` → temp dir) so
  read-only installations persist settings.
- `configure_logging` is idempotent.
- `@safe_operation` now applied to all 11 MCP operations.
- `_save_active_screenshot` clears the selection in a `finally` block.
- `set_object_property` reports per-property errors via a callback.

#### Tools (MCP)
- `undo(doc_name, steps=1)` — undo N transactions in a document.
- `redo(doc_name, steps=1)` — redo N previously-undone transactions.
- `save_document(doc_name, path=None)` — save a document to disk.
- `export_object(doc_name, obj_name, path, fmt=None)` — export a single
  object to STL / STEP / IGES / etc. Format inferred from the file
  extension when not given.
- `get_active_view()` — view_type, width, height, has_save_image.
- `health_check()` — uptime, queue sizes, cache stats, settings path.

#### Configuration
- `FREECAD_MCP_NO_DIRECTIVE_PREFIX=1` — drop the audit prefix from
  every text response (saves ~10 tokens/call).
- `FREECAD_MCP_MAX_INSTRUCTIONS_CHARS=8192` — cap on `mcp_instructions`
  size with a logged warning when truncated.
- `FREECAD_MCP_KEEP_FEM_WORKDIR=1` — see "FEM workdir cleanup" above.
- `FREECAD_MCP_RPC_TIMEOUT=10` — XML-RPC client timeout (seconds).
- `FREECAD_MCP_RPC_TIMEOUTS='{"create_object": 120}'` — per-op timeouts.
- `FREECAD_MCP_BLOCKED_PATTERNS='\\bctypes\\s*\\.\\s*CDLL\\s*\\('` — extend
  the dangerous-code blocklist.
- `pyproject.toml` now includes real description, keywords, trove
  classifiers, and project URLs (Homepage, Repository, Issues).

#### CI / DX
- Migrated CI to `pytest` with coverage (matrix Python 3.11/3.12/3.13,
  `--cov-fail-under=50`).
- Added `ruff` lint job and `mypy` type-check job to the CI matrix.
- Added `[project.optional-dependencies] dev = [pytest, pytest-cov,
  ruff, mypy]` to `pyproject.toml`.
- New `pytest` markers: `freecad` (integration tests requiring a real
  FreeCAD instance, skipped by default) and `slow`.

### Fixed
- XML-RPC calls to a hung FreeCAD instance no longer block forever
  (timeout via `_TimeoutTransport`).
- Two concurrent calls to `start_rpc_server` no longer create two
  listening servers (`_rpc_lock`).
- `delete_object` previously reported success even when the
  underlying call failed; now correctly reports the error.
- `safe_operation` decorator applied to all 11 operations so a
  transient RPC failure no longer surfaces as a raw traceback to
  the LLM.

### Tests
- Total: 0 → 175 unit tests across 13 test modules.
- Coverage: 0% → 56% (target 50% for addon met; 91% on `src/freecad_mcp/
  operations/core.py`, 98% on `guidelines.py`, 100% on `_fem_workdir`).
- mypy: clean on `src/`.
- ruff: clean on `src/` and `tests/`.

### Documentation
- New `CHANGELOG.md` (this file).
- New `CONTRIBUTING.md` with dev setup, lint/test commands, and PR
  process.
- New `SECURITY.md` with the threat model, the guidelines blocklist,
  and the vulnerability-reporting process.
- New `docs/IMPROVEMENT_PLAN.md` auditing the codebase and tracking
  the 6-phase remediation plan.
- `README.md` and `pyproject.toml` updated to reflect the new
  features, env vars, and tooling.

## [0.1.18] — 2026-07-02

Initial public release. See git history for the full pre-0.2.0 lineage.
