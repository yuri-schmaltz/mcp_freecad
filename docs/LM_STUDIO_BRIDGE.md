# LM Studio bridge (v1.0.4)

LM Studio (Aug-2026) doesn't speak MCP natively. To drive a FreeCAD
MCP server **from LM Studio**, run a thin HTTP proxy that:

1. Exposes the MCP tools via the **OpenAI-compatible
   `/v1/chat/completions` endpoint** (which LM Studio itself also
   speaks, but here we want the *reverse*: LM Studio as the
   *client* against our MCP-as-server).
2. Speaks MCP stdio (or SSE in future) on the upstream side to the
   actual `mcp-freecad` server.

This document describes a minimal FastAPI proxy (~120 lines) that
does exactly that, mirroring what `open-webui/mcpo` does for
Open WebUI but kept in-repo for ownership.

## Why

LM Studio's "OpenAI-compatible server" is intended to serve *its own*
models to *other* OpenAI-compatible clients. The reverse — letting
LM Studio *call* MCP tools — is what we wire here.

## Run

```bash
# 1. Start the proxy (in this repo)
python -m freecad_mcp.lmstudio_bridge --mcp mcp-freecad --port 8765

# 2. In LM Studio → Developer → OpenAI-compatible server:
#    point your local "client" (the Play tab) at http://127.0.0.1:8765/v1
#    or have a script speak OpenAI to the proxy.
```

The proxy will:

* On start: open a stdio MCP session to `mcp-freecad`, list the 18
  tools, convert to OpenAI function-calling schema.
* On each request: forward to whatever LM Studio model is configured
  on the *upstream* (you set `LMSTUDIO_BASE_URL` /
  `LMSTUDIO_MODEL`); loop on `tool_calls`; return the final answer.

## What about LM Studio as the **upstream**?

If LM Studio is *itself* serving a model on `http://127.0.0.1:1234`
(via its "Local Server" tab), this proxy can be the bridge: the
client side sees `/v1/chat/completions`, the upstream hits LM
Studio's own OpenAI-compatible endpoint, and the tools come from
mcp-freecad on the inside.

## Verified

The proxy is exercised in `tests/test_lmstudio_bridge.py`. Live
verification was scope-limited in this gauntlet pass because the
local LM Studio binary requires a GUI to start its server; the
unit tests use a fake OpenAI-compatible upstream and pass.

## See also

* [`OLLAMA_BRIDGE.md`](OLLAMA_BRIDGE.md) — the Ollama-flavoured
  equivalent.
* The **standalone web UI** (`python -m freecad_mcp.web_ui`, added
  in v1.1.0) is the simplest surface for ad-hoc interaction with
  any OpenAI-API-shaped host.
