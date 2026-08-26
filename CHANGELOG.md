# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
