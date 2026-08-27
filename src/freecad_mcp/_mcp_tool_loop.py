"""Generic MCP tool-calling loop shared by the Ollama and LM Studio bridges.

The two bridges differ only in *how* they talk to the upstream LLM
(Ollama ``/api/chat`` vs. OpenAI ``/v1/chat/completions``). The
side they share is: open an MCP session, list tools, drive a loop
where each turn either answers or requests ``tool_calls`` that we
execute and feed back as ``role: tool`` messages.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession

from .circuit_breaker import CircuitBreaker


def mcp_tool_to_openai(tool: Any) -> dict[str, Any]:
    """Convert MCP ``ToolDescription`` → OpenAI ``{"type":"function",...}``."""
    schema = tool.inputSchema or {}
    if schema.get("type") != "object":
        schema = {**schema, "type": "object"}
    schema.setdefault("properties", {})
    return {"type": "function",
            "function": {"name": tool.name, "description": tool.description or "",
                         "parameters": schema}}


@dataclass
class ToolLoop:
    """State machine for one assistant turn."""
    messages: list[dict[str, Any]]
    last_reply: dict[str, Any]
    iterations_used: int = 0


# The minimal ``chat`` shape the bridges need: takes messages (and
# maybe tools), returns a parsed dict at minimum with a "message" /
# "choices" key. Each bridge adapts its backend into this signature.
PostCallFn = Callable[[ToolLoop], Awaitable[None]]


@dataclass
class LoopResult:
    content: str
    iterations: int
    messages: list[dict[str, Any]]


async def run_tool_loop(
    session: ClientSession,
    init_messages: list[dict[str, Any]],
    *,
    pick_message: Callable[[dict[str, Any]], dict[str, Any]],
    pick_tool_calls: Callable[[dict[str, Any]], list[dict[str, Any]]],
    pick_content: Callable[[dict[str, Any]], str],
    call_one_step: Callable[[ToolLoop], Awaitable[None]],
    max_iterations: int = 6,
) -> LoopResult:
    """Drive the model until it returns a final answer.

    * ``pick_message`` / ``pick_tool_calls`` / ``pick_content``
      abstract the differences between Ollama's ``{"message":{...}}``
      response and OpenAI's ``{"choices":[{"message":{...}}]}``.
    * ``call_one_step`` performs one upstream LLM call (mutates
      ``loop.messages`` and ``loop.last_reply``).
    * Tool calls are dispatched through ``session.call_tool``; their
      results are appended as ``role: tool`` messages.
    """
    loop = ToolLoop(messages=list(init_messages), last_reply={})
    breaker = CircuitBreaker()  # local; bridges may share their own
    for _ in range(max_iterations):
        await call_one_step(loop)
        loop.iterations_used += 1
        reply = pick_message(loop.last_reply)
        calls = pick_tool_calls(reply)
        if calls:
            loop.messages.append(reply)
            for call in calls:
                loop.messages.append(await _dispatch_tool(session, call, breaker))
            continue
        return LoopResult(content=pick_content(reply).strip(),
                         iterations=loop.iterations_used,
                         messages=loop.messages)
    raise RuntimeError("max_tool_iterations exceeded")


async def _dispatch_tool(session: ClientSession, call: dict[str, Any],
                        breaker: CircuitBreaker) -> dict[str, Any]:
    name = call.get("name") or call.get("function", {}).get("name", "")
    raw_args = (call.get("arguments")
                if "arguments" in call
                else call.get("function", {}).get("arguments"))
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            args = {}
    else:
        args = raw_args or {}
    try:
        result = await session.call_tool(name, args)
    except Exception as exc:  # noqa: BLE001 — surface to model
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
