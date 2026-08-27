# Ollama bridge (v1.0.4)

`src/freecad_mcp/ollama_bridge.py` is a tiny stdlib-and-`httpx` shim
that lets **any local LLM that speaks the Ollama `/api/chat`
endpoint** drive FreeCAD through the MCP server, without any
first-class MCP support in the LLM host.

It is the same pattern the rest of the ecosystem uses (see e.g.
`open-webui/mcpo`, `composio-mcp`, LiteLLM plugins) but kept inside
the project so we own the contract end-to-end.

## When to use it

Use this bridge when:

* Your LLM host **does not support MCP natively** but exposes an
  OpenAI-compatible `/v1/chat/completions` or Ollama
  `/api/chat` endpoint.
* You want to talk to **Ollama, LM Studio, llama.cpp-server, or any
  other OpenAI-API-shaped runtime** and let it call FreeCAD tools.
* You are scripting / embedding the bridge in a custom Python tool
  (CI, notebooks, custom agents).

Do **not** use it when:

* Your LLM host already speaks MCP natively (e.g. Claude Desktop,
  Cursor, future LM Studio with MCP server support). Use
  `claude_desktop_config.json` instead.
* You need OAuth/SSO at the MCP transport level (the bridge uses
  stdio today; HTTP+SSE is on the roadmap).

## Install

The bridge lives in the same package as the MCP server, so
`pip install -e ".[dev]"` from the repo root is enough.

The bridge also pulls `httpx`, which is already a transitive
dependency of `mcp[cli]` and `fastapi`. No new runtime deps.

## Run

Two ways.

### 1. Library call (use inside your own agent)

```python
import asyncio
from freecad_mcp.ollama_bridge import OllamaMCPBridge

answer = asyncio.run(
    OllamaMCPBridge().ask("Use list_documents, then health_check; report both.")
)
print(answer)
```

### 2. Console script (one-shot CLI)

```bash
python -m freecad_mcp.ollama_bridge \
    "List every open FreeCAD document and tell me which look empty" \
    --model qwen3.6:27b
```

## Configuration

`OllamaBridgeConfig` fields, all overridable via env or constructor:

| Field | Env | Default | Notes |
|---|---|---|---|
| `ollama_url` | `OLLAMA_HOST` | `http://127.0.0.1:11434` | Set to a remote Ollama |
| `model` | — | `qwen3.6:27b` | Must advertise `tools` capability via `/api/show` |
| `command` | — | `("mcp-freecad", "--only-text-feedback")` | Override to swap MCP server |
| `max_tool_iterations` | — | `6` | Hard cap on tool-call loops |
| `request_timeout_s` | — | `120.0` | Per Ollama POST |

## How the loop works

```
        ┌────────── Ollama /api/chat ─────────┐
        │                                     │
        │  messages += tool result            │
        │  POST model+tools+messages           │
        │  ◀────────── message ────────────     │
        │                                     │
   ┌────▼────┐        ┌──────────────┐        │
   │ Bridge  │◀──────▶│  mcp-freecad  │◀────▶ FreeCAD
   └────┬────┘        └──────────────┘   RPC XML/9875
        │  if message.tool_calls:
        │      for each call:
        │          result = mcp.call_tool(name, args)
        │          messages += {role: tool, name, content}
        │  else:
        │      return message.content
        ▼
```

The bridge never lets an exception escape its `ask()` while a tool
is being called. Errors are converted into structured
`role: tool` messages and fed back into the conversation, so the
model can usually self-correct (or at minimum, report the failure
in plain language).

The HTTP side is wrapped in `CircuitBreaker.call(fn)` so a transient
flake retries with exponential backoff; persistent failure surfaces
as a normal `httpx.HTTPError` raised from `ask()`.

## Verified

| Model | Backend | Multi-tool loop | Args-as-dict |
|---|---|---|---|
| `qwen3.6:27b` | Ollama 0.32.14 | ✅ 2-step | ✅ |
| `qwen3.6:27b` | Ollama 0.32.14 | ✅ args parsed | ✅ `dict` |

Tested live against `127.0.0.1:11434` in the gauntlet pass on 2026-08-26.
The CLI smoke is reproducible with `_bridge_smoke.py` style scripts
(see `tests/test_ollama_bridge.py` for unit coverage).

## Limitations

* **No streaming.** `stream: false` always; the loop is roundtrip-based.
* **No tool-call parallelisation.** Sequential, in declaration order.
* **No image content.** When the MCP tool returns `[ImageContent]`,
  the bridge currently emits `(empty result)` — screenshotting via
  Ollama is not yet useful. Workaround: pass `image_format="png"`
  and ask a multimodal Ollama model after sending the bytes
  out-of-band.
* **stdio transport only.** Remote MCP servers via SSE/HTTP are not
  yet wired up; pull requests welcome.
