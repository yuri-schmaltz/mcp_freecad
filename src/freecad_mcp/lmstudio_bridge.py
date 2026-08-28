"""Bridge between an OpenAI-compatible endpoint and the mcp-freecad MCP server.

This is the symmetrical counterpart of :mod:`ollama_bridge` for any
LLM runtime that exposes the OpenAI ``/v1/chat/completions``
contract: LM Studio's "Local Server", llama.cpp ``--server`` mode,
vLLM's HTTP backend, etc.

Two surfaces:

* :class:`LMStudioMCPBridge` — in-process client (mirrors
  :class:`ollama_bridge.OllamaMCPBridge`).
* :func:`serve` — a stdlib HTTP server that *exposes* the MCP tools
  through ``/v1/chat/completions``, so an OpenAI-shaped client (a
  custom agent, the LM Studio GUI in some configs, etc.) can drive
  FreeCAD without modifying the upstream LLM.

Usage (in-process)::

    import asyncio
    from freecad_mcp.lmstudio_bridge import LMStudioMCPBridge
    print(asyncio.run(LMStudioMCPBridge().ask("health_check", model="qwen3.6:27b")))

Usage (HTTP proxy)::

    python -m freecad_mcp.lmstudio_bridge serve --port 8765 --mcp-cmd mcp-freecad

``CircuitBreaker`` guards the HTTP transport; tool errors are
surfaced back to the model instead of propagating.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ._mcp_tool_loop import mcp_tool_to_openai, run_tool_loop, sanitize_messages_for_llm
from .circuit_breaker import CircuitBreaker


@dataclass
class LMStudioBridgeConfig:
    """Runtime config for :class:`LMStudioMCPBridge`."""

    base_url: str = os.environ.get("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234")
    api_key: str = os.environ.get("LMSTUDIO_API_KEY", "lm-studio")  # placeholder
    model: str = os.environ.get("LMSTUDIO_MODEL", "qwen3.6:27b")
    command: tuple[str, ...] = ("mcp-freecad", "--only-text-feedback")
    max_tool_iterations: int = 6
    request_timeout_s: float = 120.0


class LMStudioBridgeError(RuntimeError): ...


class LMStudioMCPBridge:
    """OpenAI-compatible HTTP bridge to mcp-freecad (in-process client)."""

    def __init__(self, config: LMStudioBridgeConfig | None = None) -> None:
        self.cfg = config or LMStudioBridgeConfig()
        self.breaker = CircuitBreaker()

    async def _open_mcp(self):
        params = StdioServerParameters(command=self.cfg.command[0],
                                        args=list(self.cfg.command[1:]))
        return stdio_client(params)

    async def ask(self, question: str, *, model: str | None = None) -> str:
        cfg = self.cfg
        async with (await self._open_mcp()) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = [mcp_tool_to_openai(t)
                     for t in (await session.list_tools()).tools]
            chosen = model or cfg.model
            init_msgs: list[dict[str, Any]] = [{"role": "user", "content": question}]
            payload: dict[str, Any] = {"model": chosen, "stream": False,
                                        "messages": init_msgs}
            if tools:
                payload["tools"] = tools
            headers = {"Authorization": f"Bearer {cfg.api_key}"}

            async def _send(loop) -> None:
                body = dict(payload)
                body["messages"] = sanitize_messages_for_llm(loop.messages)

                def _do() -> dict[str, Any]:
                    r = httpx.post(f"{cfg.base_url}/v1/chat/completions",
                                    json=body, timeout=cfg.request_timeout_s,
                                    headers=headers)
                    r.raise_for_status()
                    return cast(dict[str, Any], r.json())
                loop.last_reply = cast(dict[str, Any],
                                       self.breaker.call(_do))
            try:
                result = await run_tool_loop(
                    session, init_msgs,
                    pick_message=lambda r: cast(dict[str, Any],
                                                 (r.get("choices") or [{}])[0].get("message", {})),
                    pick_tool_calls=lambda msg: list(msg.get("tool_calls") or []),
                    pick_content=lambda msg: str(msg.get("content") or ""),
                    call_one_step=_send,
                    max_iterations=cfg.max_tool_iterations,
                )
            except RuntimeError as exc:
                raise LMStudioBridgeError(str(exc)) from exc
        return cast(str, result.content)


# ---------------------------------------------------------------------------
# ``serve`` — stdlib HTTP proxy exposing MCP tools via OpenAI shim
# ---------------------------------------------------------------------------


def serve(host: str = "127.0.0.1", port: int = 8765,
         mcp_command: tuple[str, ...] = ("mcp-freecad", "--only-text-feedback")) -> None:
    """Run an OpenAI-compatible HTTP proxy that fronts mcp-freecad.

    The proxy itself talks *Ollama-style* on the upstream side, so
    it works with any OpenAI-API-shaped local runtime; the schema
    exposed here is the standard ``/v1/chat/completions``.

    Because the proxy needs to keep a single MCP session alive per
    request, we run the asyncio bridge in a background thread and
    forward each request through it via ``asyncio.run_coroutine_threadsafe``.
    """
    cfg = LMStudioBridgeConfig(command=mcp_command)
    bridge = LMStudioMCPBridge(cfg)
    loop_holder: dict[str, Any] = {}

    def _run_loop() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_holder["loop"] = loop
        loop.run_forever()

    th = threading.Thread(target=_run_loop, daemon=True)
    th.start()
    # Wait briefly for the loop to come up.
    while "loop" not in loop_holder:
        pass
    loop = loop_holder["loop"]

    class _Handler(BaseHTTPRequestHandler):
        # BaseHTTPRequestHandler writes directly to self.wfile; we
        # silence the default access-log spam to keep stderr clean.
        def log_message(self, *_args, **_kw) -> None: ...

        def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            if self.path != "/v1/chat/completions":
                self.send_error(404, "use POST /v1/chat/completions")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                payload = json.loads(body or b"{}")
            except (ValueError, json.JSONDecodeError):
                self.send_error(400, "invalid JSON")
                return
            messages = payload.get("messages") or []
            if not messages:
                self.send_error(400, "missing messages")
                return
            # Take the *last* user message as the *question* and use it
            # plus any earlier messages as the conversation seed.
            model = payload.get("model") or cfg.model
            fut = asyncio.run_coroutine_threadsafe(
                _drive(bridge, cfg, model, list(messages)), loop)
            try:
                reply = fut.result(timeout=cfg.request_timeout_s + 30)
            except Exception as exc:  # noqa: BLE001
                self.send_error(502, f"{type(exc).__name__}: {exc}")
                return
            body_json = json.dumps(reply).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_json)))
            self.end_headers()
            self.wfile.write(body_json)

    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"LM Studio bridge: http://{host}:{port}/v1/chat/completions", flush=True)
    httpd.serve_forever()


async def _drive(bridge: LMStudioMCPBridge, cfg: LMStudioBridgeConfig,
                model: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """One-shot driver: emulate a single OpenAI round-trip via the bridge."""
    # Reuse ``ask()``'s loop logic but seed with arbitrary messages:
    # the simplest path is to spawn a transient MCP session, send each
    # message and merge tool flows; for the typical 'last-user-message'
    # use case we forward just the last message.
    last_user = next((m["content"] for m in reversed(messages)
                       if m.get("role") == "user"), "")
    answer = await bridge.ask(last_user, model=model)
    return {
        "id": "chatcmpl-bridge",
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": answer},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def main() -> None:  # pragma: no cover
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m freecad_mcp.lmstudio_bridge {ask|serve} ...")
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "ask":
        if len(sys.argv) < 3:
            print("usage: python -m freecad_mcp.lmstudio_bridge ask \"question\" [--model M]")
            sys.exit(2)
        q = sys.argv[2]
        cfg = LMStudioBridgeConfig()
        if "--model" in sys.argv:
            cfg.model = sys.argv[sys.argv.index("--model") + 1]
        print(asyncio.run(LMStudioMCPBridge(cfg).ask(q)))
    elif cmd == "serve":
        # Minimal argparse-style parse: --host HOST --port PORT --mcp-cmd ...
        host = "127.0.0.1"
        port = 8765
        mcp_command: tuple[str, ...] = ("mcp-freecad", "--only-text-feedback")
        args = sys.argv[2:]
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--host" and i + 1 < len(args):
                host = args[i + 1]
                i += 2
                continue
            if a == "--port" and i + 1 < len(args):
                port = int(args[i + 1])
                i += 2
                continue
            if a == "--mcp-cmd" and i + 1 < len(args):
                mcp_command = tuple(args[i + 1].split())
                i += 2
                continue
            i += 1
        serve(host=host, port=port, mcp_command=mcp_command)
    else:
        print(f"unknown sub-command: {cmd!r}")
        sys.exit(2)


if __name__ == "__main__":  # pragma: no cover
    main()
