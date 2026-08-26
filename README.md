[![Tests](https://img.shields.io/badge/tests-612_passed-brightgreen)](tests/)
[![Coverage](https://img.shields.io/badge/coverage-87%25-brightgreen)](tests/)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-1.12.2%2B-purple)](https://modelcontextprotocol.io)
[![Release](https://img.shields.io/badge/release-v1.0.3-blue)](CHANGELOG.md)
[![Security: TLS%20%2B%20bearer%20auth](https://img.shields.io/badge/security-TLS%20%2B%20bearer%20auth-orange)](SECURITY.md)

# FreeCAD MCP

> Drive FreeCAD from any MCP-compatible LLM (Claude Desktop, Gemini, ADK, LangChain).
> Create and edit CAD geometry, run FEM analyses with CalculiX, capture
> screenshots, and manage the parts library — all through typed,
> validated tool calls.

**Status:** v1.0.3 — hardening + observability pass. 612 tests, 87 % coverage, ruff & mypy clean.
**Origin:** originated as a fork of [neka-nat/freecad-mcp](https://github.com/neka-nat/freecad-mcp);
now an independent project under active development.

## Why this project?

Started as a fork of the excellent `neka-nat/freecad-mcp` demo. This project
turns it into a **product**:

* **Security-first defaults** — TLS + bearer auth required for remote access, hardened
  `execute_code` blocklist, path-traversal protection, refusal of dangerous network binds.
* **Production reliability** — circuit breaker in front of every RPC, exponential-backoff
  retry, JSON-structured logging, Prometheus-style metrics.
* **Operational control** — tool allow/deny list via env vars, Pydantic request validation,
  extended health check, safe-by-default system prompt.
* **Observability** — `/metrics` endpoint in Prometheus text format, latency histograms,
  failure counters, circuit state.

See [`docs/PROFESSIONALIZATION_PLAN.md`](docs/PROFESSIONALIZATION_PLAN.md) for the full
roadmap and the rationale behind every decision.

## Compatibility matrix

| Component | Supported versions | Notes |
|---|---|---|
| Python | 3.12, 3.13 | `pyproject.toml` requires `>=3.12`; CI runs on both |
| FreeCAD | 0.21, 1.0, 1.1 | Tested manually; CI does not run FreeCAD |
| OS | Windows, macOS, Linux | macOS 1.0 and 1.1 paths documented |
| MCP SDK | ≥ 1.12.2 | Required for the FastMCP tools we use |
| LLM clients | Claude Desktop, Gemini, ADK, LangChain | Any MCP-compatible host |

This repository is a FreeCAD MCP that allows you to control FreeCAD from Claude Desktop.

## Demo

### Design a flange

![demo](./assets/freecad_mcp4.gif)

### Design a toy car

![demo](./assets/make_toycar4.gif)

### Design a part from 2D drawing

#### Input 2D drawing

![input](./assets/b9-1.png)

#### Demo

![demo](./assets/from_2ddrawing.gif)

This is the conversation history.
https://claude.ai/share/7b48fd60-68ba-46fb-bb21-2fbb17399b48

## When NOT to use this

Be honest with yourself about the threat model before deploying:

* **Don't expose the RPC port to the internet.** Even with TLS + bearer auth, the
  `execute_code` tool runs arbitrary Python in the FreeCAD process. If your LLM host
  is compromised, your FreeCAD machine is too. Use a local network, a VPN, or an SSH
  tunnel.
* **Don't run FreeCAD on your primary workstation** when the LLM is untrusted. Use a
  container or VM. The `execute_code` blocklist is a guardrail, not a sandbox.
* **Don't enable `execute_code` in multi-tenant deployments.** Use the
  `FREECAD_MCP_DISABLED_TOOLS=execute_code` env var to turn it off; the structured
  tools (`create_object`, `edit_object`, `get_view`, `run_fem_analysis`, ...) cover
  95% of legitimate use cases.
* **Don't load parts from a directory you don't control.** The parts library is
  injected into the active FreeCAD document via `mergeProject`; a malicious
  `*.FCStd` file can run macro code on open.

## Production deployment checklist

Before exposing this server to anything beyond a local dev environment:

- [ ] **Sandbox.** Run FreeCAD in a container (Docker/Podman) or a dedicated VM.
- [ ] **Disable `execute_code` if not needed.** `FREECAD_MCP_DISABLED_TOOLS=execute_code`.
      Document for your users how to get it re-enabled if they need it.
- [ ] **TLS + auth for remote access.** Set `FREECAD_MCP_TLS_CERT`, `FREECAD_MCP_TLS_KEY`,
      and `FREECAD_MCP_AUTH_TOKEN` in the environment that runs FreeCAD. The server
      refuses to start with `remote_enabled=true` without all three.
- [ ] **Restrict the IP allowlist.** Edit `allowed_ips` in the FreeCAD MCP settings
      to your LLM host's subnet. `0.0.0.0/0` is explicitly rejected.
- [ ] **Scrape `/metrics`.** Point your Prometheus at the
      `health_check` tool output (or wire a sidecar to expose it on `/metrics`).
- [ ] **Structured logs.** Set `FREECAD_MCP_LOG_FORMAT=json` and ship to your
      log aggregator.
- [ ] **Review the system prompt.** v1.0.0 ships a short English fallback by
      default. If you want a custom one, place it in `docs/gabarito_ia_extracted.txt`
      and set `FREECAD_MCP_LOAD_GABARITO=1`.

## Install addon

FreeCAD Addon directory is
* Windows: `%APPDATA%\FreeCAD\Mod\`
* Mac:
  * FreeCAD 1.1: `~/Library/Application\ Support/FreeCAD/v1-1/Mod/`
  * FreeCAD 1.0: `~/Library/Application\ Support/FreeCAD/v1-0/Mod/`
* Linux:
  * Ubuntu: `~/.FreeCAD/Mod/` or `~/snap/freecad/common/Mod/` (if you install FreeCAD from snap)
  * Debian: `~/.local/share/FreeCAD/Mod`
  * Arch / CachyOS (FreeCAD 1.1 from `extra/freecad`): `~/.local/share/FreeCAD/v1-1/Mod/`

Please put `addon/FreeCADMCP` directory to the addon directory.

```bash
git clone https://github.com/yuri-schmaltz/mcp-freecad.git
cd mcp-freecad

# For Linux (Ubuntu/Debian)
cp -r addon/FreeCADMCP ~/.FreeCAD/Mod/

# For Linux (Arch/CachyOS, FreeCAD 1.1 from extra/freecad)
mkdir -p ~/.local/share/FreeCAD/v1-1/Mod/
cp -r addon/FreeCADMCP ~/.local/share/FreeCAD/v1-1/Mod/

# For macOS (FreeCAD 1.1)
cp -r addon/FreeCADMCP ~/Library/Application\ Support/FreeCAD/v1-1/Mod/
```

When you install addon, you need to restart FreeCAD.
You can select "MCP Addon" from Workbench list and use it.

![workbench_list](./assets/workbench_list.png)

And you can start RPC server by "Start RPC Server" command in "FreeCAD MCP" toolbar.

![start_rpc_server](./assets/start_rpc_server.png)

### Auto-Start RPC Server

By default, the RPC server must be started manually each time FreeCAD opens. To start it automatically:

1. Open the **FreeCAD MCP** menu (switch to the MCP Addon workbench first)
2. Check **Auto-Start Server**

The setting is saved to `freecad_mcp_settings.json` and persists across sessions. On the next FreeCAD launch, the RPC server will start automatically once the application finishes loading.

You can disable it at any time by unchecking **Auto-Start Server** in the same menu.

## Setting up Claude Desktop

Pre-installation of the [uvx](https://docs.astral.sh/uv/guides/tools/) is required.

And you need to edit Claude Desktop config file, `claude_desktop_config.json`.

For user.

```json
{
  "mcpServers": {
    "freecad": {
      "command": "uvx",
      "args": [
        "mcp-freecad"
      ]
    }
  }
}
```

If you want to save token, you can set `only_text_feedback` to `true` and use only text feedback.

```json
{
  "mcpServers": {
    "freecad": {
      "command": "uvx",
      "args": [
        "mcp-freecad",
        "--only-text-feedback"
      ]
    }
  }
}
```


For developer.
First, you need clone this repository.

```bash
git clone https://github.com/yuri-schmaltz/mcp-freecad.git
```

```json
{
  "mcpServers": {
    "freecad": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/mcp-freecad/",
        "run",
        "mcp-freecad"
      ]
    }
  }
}
```

## Remote Connections

By default the RPC server does not accept remote connections and listens on `localhost`. To control FreeCAD from another machine on your network:

### 1. Enable remote connections in FreeCAD

In the **FreeCAD MCP** toolbar:

1. Check **Remote Connections** — the RPC server will bind to `0.0.0.0` (all interfaces) on the next restart. For security reasons, it only accepts connections from the IP addresses or CIDR subnets specified in the **Allowed IPs** field. By default this is `127.0.0.1`.
2. Click **Configure Allowed IPs** and enter a comma-separated list of IP addresses or CIDR subnets that are allowed to connect, e.g.:

   ```
   192.168.1.100, 10.0.0.0/24
   ```

   `127.0.0.1` is always the default. Invalid entries are rejected with an error dialog. Restart the RPC server after changing these settings.

### 2. Point the MCP server at the remote host

Pass the `--host` flag with the IP address or hostname of the machine running FreeCAD:

```json
{
  "mcpServers": {
    "freecad": {
      "command": "uvx",
      "args": [
        "mcp-freecad",
        "--host", "192.168.1.100"
      ]
    }
  }
}
```

The `--host` value is validated on startup — it must be a valid IPv4/IPv6 address or hostname.

## Tools

* `create_document`: Create a new document in FreeCAD.
* `create_object`: Create a new object in FreeCAD.
* `edit_object`: Edit an object in FreeCAD.
* `delete_object`: Delete an object in FreeCAD.
* `execute_code`: Execute arbitrary Python code in FreeCAD.
* `insert_part_from_library`: Insert a part from the [parts library](https://github.com/FreeCAD/FreeCAD-library).
* `get_view`: Get a screenshot of the active view.
* `get_objects`: Get all objects in a document.
* `get_object`: Get an object in a document.
* `get_parts_list`: Get the list of parts in the [parts library](https://github.com/FreeCAD/FreeCAD-library).
* `list_documents`: List the names of all open documents.
* `run_fem_analysis`: Run the CalculiX solver on an existing `Fem::FemAnalysis` and return summary results (max von Mises stress, max displacement, node count, working directory). Auto-creates a `SolverCcxTools` if the analysis has none.
* `undo` / `redo`: Roll back or replay document transactions.
* `save_document`: Save a document to disk (optional explicit path).
* `export_object`: Export a single object to STL / STEP / IGES / etc.
* `get_active_view`: Inspect the active view (type, size, saveImage support).
* `health_check`: Liveness probe for monitoring (uptime, queue sizes, settings path).

## Contributors

<a href="https://github.com/yuri-schmaltz/mcp-freecad/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=yuri-schmaltz/mcp-freecad" />
</a>

Made with [contrib.rocks](https://contrib.rocks).

## Guidelines & Logging

This project now integrates directives from `docs/gabarito_ia.pdf` (extracted to `docs/gabarito_ia_extracted.txt`) and enforces them at runtime:

- The application will prefix textual responses with the mandated sentence from the gabarito. Set `FREECAD_MCP_NO_DIRECTIVE_PREFIX=1` to suppress it (saves ~10 tokens/call).
- Dangerous or "agreement trap" prompts (e.g. requests to bypass safety, or code that calls `os.system`) are detected and refused with constructive guidance. The blocklist is extensible via `FREECAD_MCP_BLOCKED_PATTERNS` (regexes). See [SECURITY.md](SECURITY.md) for the full threat model.
- Logging is configurable via the `FREECAD_MCP_LOGLEVEL` environment variable (default `INFO`).
- Logs are written to console and to `logs/freecad_mcp.log` (rotating file handler).

### Other environment variables

| Var | Default | Effect |
|---|---|---|
| `FREECAD_MCP_RPC_TIMEOUT` | `10` | XML-RPC client timeout (seconds). |
| `FREECAD_MCP_RPC_TIMEOUTS` | — | Per-op JSON timeouts, e.g. `{"create_object": 120, "run_fem_analysis": 900}`. |
| `FREECAD_MCP_KEEP_FEM_WORKDIR` | `false` | If truthy, keep the CalculiX scratch directory after each run. |
| `FREECAD_MCP_NO_DIRECTIVE_PREFIX` | `false` | Drop the audit prefix from every text response. |
| `FREECAD_MCP_MAX_INSTRUCTIONS_CHARS` | `8192` | Cap on `mcp_instructions` size with a logged warning. |
| `FREECAD_MCP_BLOCKED_PATTERNS` | — | Comma-separated regexes to extend the code blocklist. |
| `FREECAD_MCP_DISABLED_TOOLS` | — | Comma-separated tool names to disable. E.g. `execute_code` in production. |
| `FREECAD_MCP_REQUIRED_TOOLS` | — | When set, ONLY the listed tools are exposed. Mutually exclusive with `DISABLED_TOOLS`. |
| `FREECAD_MCP_LOAD_GABARITO` | `false` | Load the Portuguese `gabarito_ia.pdf` directive set as the system prompt. Off by default. |
| `FREECAD_MCP_LOG_FORMAT` | `text` | `text` (default) or `json` for log shippers. |
| `FREECAD_MCP_CB_THRESHOLD` | `3` | Consecutive RPC failures before the circuit breaker opens. |
| `FREECAD_MCP_CB_RESET_S` | `60` | Seconds to wait before the breaker half-opens for a probe. |
| `FREECAD_MCP_RETRY_MAX` | `3` | Retries with exponential backoff on transient failures while closed. |
| `FREECAD_MCP_RETRY_BASE_S` | `0.1` | Base delay for retries; doubles each attempt. |
| `FREECAD_MCP_TLS_CERT` | — | PEM certificate path. Required for remote RPC. |
| `FREECAD_MCP_TLS_KEY` | — | PEM private key path. Required for remote RPC. |
| `FREECAD_MCP_AUTH_TOKEN` | — | Shared bearer secret. Required for remote RPC. |

## Monitoring

The `health_check` MCP tool returns a JSON payload with two extra blocks
since v1.0.0:

* `circuit_breaker` — current state, consecutive failures, last error.
* `metrics` — counters, histograms, gauges from the in-process registry.

For Prometheus / Grafana / Loki, render the same registry as Prometheus
text format and expose it on a port your scraper can reach:

```python
from freecad_mcp.server_state import state  # your ServerState
from freecad_mcp.metrics import format_prometheus
# inside whatever HTTP framework you use:
@app.get("/metrics")
def metrics():
    return Response(format_prometheus(state.metrics), media_type="text/plain; version=0.0.4")
```

Scrape config:

```yaml
scrape_configs:
  - job_name: mcp-freecad
    metrics_path: /metrics
    static_configs:
      - targets: ['freecad-host:9876']  # wherever you exposed it
```

Useful alerts:

* `freecad_mcp_circuit_state == 2` for >1 minute — FreeCAD is unreachable.
* `rate(freecad_mcp_circuit_short_circuits_total[5m]) > 0` — breakers are opening repeatedly.
* `histogram_quantile(0.95, rate(freecad_mcp_tool_duration_seconds_bucket[5m])) > 30`
  — tool latency degraded.

## Running tests

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
python -m pip install -e ".[dev]"
pytest                      # ~318 tests, ~7s, no FreeCAD required
pytest -m freecad           # integration tests (need a running FreeCAD)
```

CI is configured in `.github/workflows/ci.yml` to run `pytest` (with
coverage), `ruff`, and `mypy` on every push and pull request, across
Python 3.12 / 3.13, on Ubuntu, macOS, and Windows. The FreeCAD addon's
syntax is verified against Python 3.12 separately.

## Contributing & Security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev setup, branch
naming, and PR process. See [SECURITY.md](SECURITY.md) for the threat
model and the vulnerability reporting process. The current audit and
remediation plan live in [docs/IMPROVEMENT_PLAN.md](docs/IMPROVEMENT_PLAN.md).

