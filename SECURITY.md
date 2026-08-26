# Security

`mcp-freecad` is a bridge between an LLM (which can be tricked by a
hostile prompt) and FreeCAD, which is a fully programmable Python
environment that can reach the host filesystem, network, and OS
services. This document describes the threat model and the safeguards
in place; if you find a vulnerability please follow the reporting
process at the bottom.

## Threat model

The MCP server runs as a stdio process spawned by the LLM host
(Claude Desktop, etc.). It forwards every tool call to FreeCAD over
XML-RPC. The trust assumptions:

- **The LLM is partially trusted.** It may be steered by a user prompt
  that contains adversarial instructions, but most of its inputs are
  not adversarial on average. `check_prompt_conflict` rejects a small
  set of agreement-trap phrases.
- **The user is not trusted.** Anything the user types into the chat
  becomes part of the prompt. `check_code_conflict` rejects the
  most dangerous Python builtins (`eval`, `exec`, `os.system`,
  `os.popen`, `subprocess.<call>`, bare `import subprocess`, `rm -rf /`,
  `shutdown`, `reboot`, plus since 0.4.0: `compile`, `breakpoint`,
  `__import__`, `globals`/`locals`, `socket.*`, `urllib.*`, `httpx.*`,
  `requests.*`, `ftplib.*`, `smtplib.*`, `ctypes.*`, `cffi`,
  `pickle.*`, `marshal.*`, `shelve.*`).
- **The FreeCAD host is not necessarily sandboxed.** The RPC server
  runs in the FreeCAD GUI process with full Python access. `parts_library`
  enforces strict path-traversal protection because it can open
  arbitrary files inside the parts library directory.
- **The network is partially trusted.** The XML-RPC server binds to
  `localhost` by default. Remote connections are opt-in via a
  setting; the allowlist refuses `0.0.0.0/0` and `::/0` because those
  would expose the RPC server (and therefore `execute_code`) to the
  whole internet. **Since 0.4.0 the server refuses to start with
  `remote_enabled=true` unless TLS and a bearer token are configured.**
- **The XML-RPC transport supports TLS natively since 0.3.0** via
  `FREECAD_MCP_TLS_CERT` / `FREECAD_MCP_TLS_KEY`; bearer-token
  authentication via `FREECAD_MCP_AUTH_TOKEN` is enforced with
  constant-time compare. A reverse proxy is no longer required.

## Safeguards in 0.2.0

- **Code-level blocklist** (`src/freecad_mcp/guidelines.py`):
  `check_code_conflict` runs on every value passed to `execute_code`
  and refuses to forward anything matching the dangerous builtins.
  Extend the list at runtime via `FREECAD_MCP_BLOCKED_PATTERNS`.
- **Path traversal** (`addon/FreeCADMCP/rpc_server/parts_library.py`):
  `_safe_resolve` rejects empty, absolute, `..`-bearing, and
  symlink-escape inputs.
- **Remote IP allowlist** (`validate_allowed_ips`): refuses
  `0.0.0.0/0` and `::/0` with an explicit error.
- **XML-RPC timeout** (`freecad_client._TimeoutTransport`): the
  client will not block forever if FreeCAD hangs.
- **Cooperative cancellation** (`FreeCADRPC.cancel_request`): a
  long-running task can be marked for cancellation before the GUI
  worker pops it.
- **Per-operation timeouts** (`FreeCADRPC.PER_OPERATION_TIMEOUTS`):
  each tool has a sensible default; the queue wait is bounded so a
  single stuck task does not block the whole server.
- **Thread-safe lifecycle** (`start_rpc_server` / `stop_rpc_server`):
  cannot create two concurrent servers, and `server_close()` releases
  the listening socket on shutdown.
- **Optional TLS** (0.3.0): set `FREECAD_MCP_TLS_CERT` and
  `FREECAD_MCP_TLS_KEY` to PEM paths; the server wraps every
  accepted socket in TLS (TLS 1.2 minimum). Refuses to start with
  half-configured cert/key.
- **Optional bearer-token auth** (0.3.0): set `FREECAD_MCP_AUTH_TOKEN`
  to a shared secret; every request must carry a matching
  `Authorization: Bearer <token>` header. Validation is constant-time
  (`hmac.compare_digest`). Strongly recommended whenever TLS is on.

## What is **not** in scope

- The `execute_code` tool **intentionally** runs arbitrary Python
  in-process via `exec(code, globals())`. The code executes in the
  FreeCAD GUI process with **full access to the host filesystem,
  network, OS services, and every loaded Python module** — there is
  no sandbox, no RestrictedPython, no whitelist of "safe" snippets.
  The blocklist in `src/freecad_mcp/guidelines.py` is a guardrail
  that catches the most dangerous builtins (`eval`, `exec`,
  `subprocess.*`, network libraries, `pickle`/`marshal`/`ctypes`,
  etc.), but it is **not** a sandbox: a sophisticated prompt can
  still construct a payload the regex does not match (e.g. string
  assembly, indirect imports, attribute traversal).
  If you cannot trust the LLM (or the user behind it) to that extent,
  do not enable `execute_code`. Run FreeCAD inside a container or VM
  (see "Production deployment checklist" in the README), and set
  `FREECAD_MCP_DISABLED_TOOLS=execute_code` to turn it off entirely
  in deployments that don't need it.
- The `open` family of operations on the XML-RPC server is not
  authenticated **at the application layer** when `remote_enabled`
  is off (default = localhost). With `remote_enabled=true` plus
  `FREECAD_MCP_AUTH_TOKEN` set, the server enforces
  `Authorization: Bearer <token>` on every request; missing or
  mismatched tokens are rejected.
- The `mcp_instructions` system prompt is included in every MCP
  call to the LLM. Treat it as public — do not put secrets in
  `docs/gabarito_ia_extracted.txt`. As of 0.4.0 the file is
  opt-in via `FREECAD_MCP_LOAD_GABARITO=1`; the default is a
  short English placeholder.

## Reporting a vulnerability

**Do not open a public GitHub issue for security bugs.** Email
`yuri.schmaltz@gmail.com` (or DM the maintainer on the platform where
you first found the project) with:

- A description of the bug and the impact you can demonstrate.
- A minimal reproduction (FreeCAD version, OS, the LLM prompt that
  triggers the issue).
- Optionally, a suggested fix.

We will respond within 7 days. Please give us a reasonable window
(typically 90 days) to publish a fix before disclosing publicly.