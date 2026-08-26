"""Bridge between Ollama's HTTP /api/chat and the mcp-freecad MCP server.

Usage::

    bridge = OllamaMCPBridge()  # spawns `mcp-freecad --only-text-feedback`
    answer = asyncio.run(bridge.ask("List documents", model="qwen3.6:27b"))

Loop: each Ollama reply is parsed; if it carries ``tool_calls`` we
forward each call through MCP and feed the result back as a
``role: tool`` message, until the model produces a final answer.
``CircuitBreaker`` guards the Ollama HTTP side; tool errors are
surfaced back to the model instead of propagating, so a broken
FreeCAD never stalls the conversation.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, cast

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .circuit_breaker import CircuitBreaker


def _mcp_tool_to_ollama(tool: Any) -> dict[str, Any]:
    schema = tool.inputSchema or {}
    if schema.get("type") != "object":
        schema = {**schema, "type": "object"}
    schema.setdefault("properties", {})
    return {"type": "function",
            "function": {"name": tool.name, "description": tool.description or "",
                         "parameters": schema}}


@dataclass
class OllamaBridgeConfig:
    ollama_url: str = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    model: str = "qwen3.6:27b"
    command: tuple[str, ...] = ("mcp-freecad", "--only-text-feedback")
    max_tool_iterations: int = 6
    request_timeout_s: float = 120.0


class BridgeError(RuntimeError): ...


class OllamaMCPBridge:
    def __init__(self, config: OllamaBridgeConfig | None = None) -> None:
        self.cfg = config or OllamaBridgeConfig()
        self.breaker = CircuitBreaker()

    async def _open_mcp(self):
        params = StdioServerParameters(command=self.cfg.command[0],
                                        args=list(self.cfg.command[1:]))
        return stdio_client(params)

    async def ask(self, question: str, *, model: str | None = None) -> str:
        async with (await self._open_mcp()) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            ollama_tools = [_mcp_tool_to_ollama(t)
                            for t in (await session.list_tools()).tools]
            messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
            chosen = model or self.cfg.model
            async with httpx.AsyncClient(timeout=self.cfg.request_timeout_s) as http:
                for _ in range(self.cfg.max_tool_iterations):
                    payload: dict[str, Any] = {"model": chosen, "messages": messages, "stream": False}
                    if ollama_tools:
                        payload["tools"] = ollama_tools
                    reply = (await self._post(http, payload)).get("message", {})
                    if reply.get("tool_calls"):
                        messages.append(reply)
                        for call in reply["tool_calls"]:
                            messages.append(await self._invoke(session, call))
                        continue
                    return (reply.get("content") or "").strip()
        raise BridgeError("max_tool_iterations exceeded")

    async def _post(self, http: httpx.AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.cfg.ollama_url}/api/chat"
        return await asyncio.to_thread(self._sync_post, url, payload)

    def _sync_post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            r = httpx.post(url, json=payload, timeout=self.cfg.request_timeout_s)
            r.raise_for_status()
            return cast(dict[str, Any], r.json())
        return cast(dict[str, Any], self.breaker.call(_do))

    async def _invoke(self, session: ClientSession, call: dict[str, Any]) -> dict[str, Any]:
        fn = call.get("function", {}) or {}
        name, raw_args = fn.get("name", ""), fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError:
                args = {}
        else:
            args = raw_args or {}
        try:
            result = await session.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001 — surface back to model
            return {"role": "tool", "name": name,
                    "content": json.dumps({"error": f"{type(exc).__name__}: {exc}"})}
        return {"role": "tool", "name": name, "content": _result_to_text(result)}


def _result_to_text(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is None:
            text = json.dumps(getattr(item, "data", ""), default=str)
        parts.append(text if isinstance(text, str) else str(text))
    return "\n".join(parts) or "(empty result)"


def main() -> None:  # pragma: no cover
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m freecad_mcp.ollama_bridge \"question\" [--model M]")
        sys.exit(2)
    q = sys.argv[1]
    cfg = OllamaBridgeConfig()
    if "--model" in sys.argv:
        cfg.model = sys.argv[sys.argv.index("--model") + 1]
    print(asyncio.run(OllamaMCPBridge(cfg).ask(q)))


if __name__ == "__main__":  # pragma: no cover
    main()
