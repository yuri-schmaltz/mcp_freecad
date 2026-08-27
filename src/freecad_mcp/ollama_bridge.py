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
import os
from dataclasses import dataclass
from typing import Any, cast

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ._mcp_tool_loop import _result_to_text, mcp_tool_to_openai, run_tool_loop
from .circuit_breaker import CircuitBreaker

# Back-compat aliases kept so older callers still work.
_mcp_tool_to_ollama = mcp_tool_to_openai
_result_to_text = _result_to_text


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
        cfg = self.cfg
        async with (await self._open_mcp()) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = [mcp_tool_to_openai(t)
                     for t in (await session.list_tools()).tools]
            chosen = model or cfg.model
            payload = {"model": chosen, "stream": False}

            async def _send(loop) -> None:
                body = dict(payload)
                body["messages"] = loop.messages
                if tools:
                    body["tools"] = tools

                def _do() -> dict[str, Any]:
                    r = httpx.post(f"{cfg.ollama_url}/api/chat", json=body,
                                    timeout=cfg.request_timeout_s)
                    r.raise_for_status()
                    return cast(dict[str, Any], r.json())
                loop.last_reply = cast(dict[str, Any],
                                       self.breaker.call(_do))
            init: list[dict[str, Any]] = [{"role": "user", "content": question}]
            try:
                result = await run_tool_loop(
                    session, init,
                    pick_message=lambda r: cast(dict[str, Any], r.get("message", {})),
                    pick_tool_calls=lambda r: list(r.get("tool_calls") or []),
                    pick_content=lambda r: str(r.get("content") or ""),
                    call_one_step=_send,
                    max_iterations=cfg.max_tool_iterations,
                )
            except RuntimeError as exc:
                raise BridgeError(str(exc)) from exc
        return cast(str, result.content)


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
