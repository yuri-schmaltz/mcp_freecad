"""F4: Standalone Web UI for FreeCAD MCP.

Endpoints:
* GET /         — static HTML form (textarea + submit + response)
* POST /ask     — {prompt, model} → {response, duration_ms}
* GET  /health  — liveness probe (proxies FreeCAD health_check)
* GET  /docs    — JSON list of available MCP tools

Env vars:
* FREECAD_MCP_WEB_PORT   (default 8765)
* FREECAD_MCP_WEB_HOST   (default 127.0.0.1)
* FREECAD_MCP_OLLAMA_URL (default http://127.0.0.1:11434)
* FREECAD_MCP_WEB_MODEL  (default qwen3.6:27b)

Run: ``python -m freecad_mcp.web_ui``
"""
from __future__ import annotations

import logging
import os
import time

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel, Field
except Exception as e:
    raise ImportError(
        "FastAPI/pydantic are required for the web UI. They ship as "
        "transitive deps of mcp[cli]; install with `pip install fastapi pydantic`."
    ) from e

import httpx

from .freecad_client import FreeCADConnection
from .tool_policy import ALL_TOOL_NAMES

logger = logging.getLogger("FreeCADMCPweb")


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>FreeCAD MCP — Web UI</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 880px; margin: 2rem auto; padding: 0 1rem; color: #222; }
  h1 { font-size: 1.4rem; }
  textarea { width: 100%; min-height: 7rem; font-family: ui-monospace, monospace; font-size: 0.95rem; padding: 0.5rem; box-sizing: border-box; border: 1px solid #ccc; border-radius: 4px; }
  .row { display: flex; gap: 0.5rem; align-items: center; margin: 0.5rem 0; }
  input[type=text] { flex: 1; padding: 0.35rem; border: 1px solid #ccc; border-radius: 4px; }
  button { padding: 0.4rem 1rem; background: #2563eb; color: white; border: 0; border-radius: 4px; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: wait; }
  pre { background: #f6f8fa; padding: 0.75rem; border-radius: 4px; white-space: pre-wrap; word-wrap: break-word; font-family: ui-monospace, monospace; font-size: 0.9rem; }
  .meta { color: #666; font-size: 0.85rem; }
  .err { color: #b91c1c; }
</style>
</head>
<body>
<h1>FreeCAD MCP — Web UI</h1>
<p class="meta">Ask the local model anything about your FreeCAD project. The model can call FreeCAD tools when needed.</p>
<form id="f">
  <label for="prompt">Prompt</label>
  <textarea id="prompt" name="prompt" placeholder="e.g. Create a 10x10x10 box and save the document."></textarea>
  <div class="row">
    <label for="model">Model</label>
    <input type="text" id="model" name="model" value="__DEFAULT_MODEL__">
    <button type="submit" id="go">Ask</button>
  </div>
</form>
<h2>Response</h2>
<pre id="out">—</pre>
<p class="meta" id="meta"></p>
<script>
const form = document.getElementById('f');
const out = document.getElementById('out');
const meta = document.getElementById('meta');
const btn = document.getElementById('go');

form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const prompt = document.getElementById('prompt').value.trim();
  const model = document.getElementById('model').value.trim();
  if (!prompt) { out.textContent = '(empty prompt)'; return; }
  btn.disabled = true; out.textContent = '…thinking…'; meta.textContent = '';
  const t0 = performance.now();
  try {
    const r = await fetch('/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt, model }),
    });
    const body = await r.json();
    const dt = (performance.now() - t0) / 1000;
    if (!r.ok) {
      out.className = 'err';
      out.textContent = body.detail || JSON.stringify(body);
    } else {
      out.className = '';
      out.textContent = body.response || '(empty)';
    }
    meta.textContent = `HTTP ${r.status} · ${dt.toFixed(2)}s · model=${model}`;
  } catch (e) {
    out.className = 'err';
    out.textContent = 'Network error: ' + e;
  } finally {
    btn.disabled = false;
  }
});
</script>
</body>
</html>
"""


class AskRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=20_000)
    model: str = Field(..., min_length=1, max_length=200)


def create_web_app(
    freecad: FreeCADConnection,
    ollama_url: str = "http://127.0.0.1:11434",
    default_model: str | None = None,
) -> FastAPI:
    """Build the FastAPI app. Caller owns *freecad* lifecycle."""
    chosen_default = default_model or os.environ.get(
        "FREECAD_MCP_WEB_MODEL", "qwen3.6:27b"
    )
    app = FastAPI(
        title="FreeCAD MCP Web UI", version="1.0.0", docs_url=None, redoc_url=None
    )
    html = HTML_PAGE.replace("__DEFAULT_MODEL__", chosen_default)

    @app.get("/", response_class=HTMLResponse)
    async def root() -> HTMLResponse:
        return HTMLResponse(html)

    @app.get("/health")
    async def health() -> JSONResponse:
        try:
            res = freecad.health_check()
        except Exception as e:
            logger.warning("health check failed: %s", e)
            return JSONResponse(
                {"status": "degraded", "reason": str(e)}, status_code=503
            )
        if not isinstance(res, dict) or not res.get("success", True):
            return JSONResponse(
                {"status": "degraded", "reason": res.get("error", "unknown")},
                status_code=503,
            )
        return JSONResponse({"status": "ok", "freecad": res})

    @app.get("/docs")
    async def docs() -> JSONResponse:
        return JSONResponse(
            {"tools": sorted(ALL_TOOL_NAMES), "count": len(ALL_TOOL_NAMES)}
        )

    @app.post("/ask")
    async def ask(req: AskRequest) -> JSONResponse:
        body = {
            "model": req.model,
            "stream": False,
            "messages": [{"role": "user", "content": req.prompt}],
        }
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(f"{ollama_url}/api/chat", json=body)
                r.raise_for_status()
                payload = r.json()
        except httpx.HTTPError as e:
            logger.warning("ollama upstream error: %s", e)
            raise HTTPException(status_code=502, detail=f"Ollama error: {e}") from e
        duration_ms = (time.monotonic() - t0) * 1000.0
        message = payload.get("message") or {}
        content = message.get("content", "")
        return JSONResponse({
            "response": content,
            "model": payload.get("model", req.model),
            "duration_ms": round(duration_ms, 2),
        })

    return app


def main() -> None:
    import uvicorn

    host = os.environ.get("FREECAD_MCP_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("FREECAD_MCP_WEB_PORT", "8765"))
    ollama_url = os.environ.get(
        "FREECAD_MCP_OLLAMA_URL", "http://127.0.0.1:11434"
    )

    try:
        from .server import configure_logging

        configure_logging()
    except Exception:
        logging.basicConfig(level=logging.INFO)

    logger.info(
        "Starting FreeCAD MCP Web UI on %s:%d (ollama=%s)", host, port, ollama_url
    )
    conn = FreeCADConnection()
    app = create_web_app(conn, ollama_url=ollama_url)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
