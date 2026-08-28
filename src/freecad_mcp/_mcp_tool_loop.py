"""Generic MCP tool-calling loop shared by the Ollama and LM Studio bridges.

The two bridges differ only in *how* they talk to the upstream LLM
(Ollama ``/api/chat`` vs. OpenAI ``/v1/chat/completions``). The
side they share is: open an MCP session, list tools, drive a loop
where each turn either answers or requests ``tool_calls`` that we
execute and feed back as ``role: tool`` messages.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession

from .circuit_breaker import CircuitBreaker

logger = logging.getLogger("FreeCADMCP.tool_loop")


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
        stripped = raw_args.strip()
        if not stripped:
            args = {}
        else:
            try:
                args = json.loads(stripped)
            except json.JSONDecodeError as exc:
                # Fail loudly so the LLM sees the parse error and self-
                # corrects in the next iteration instead of receiving a
                # silently empty ``{}`` and looping until max_iterations.
                logger.warning("tool %s: invalid arguments JSON: %s", name, exc)
                return {
                    "role": "tool",
                    "name": name,
                    "content": json.dumps(
                        {
                            "error": "invalid_arguments_json",
                            "detail": f"{type(exc).__name__}: {exc}",
                            "raw_excerpt": stripped[:200],
                        }
                    ),
                }
    else:
        args = raw_args or {}
    try:
        result = await session.call_tool(name, args)
    except Exception as exc:  # noqa: BLE001 — surface to model
        logger.warning("tool %s raised: %s", name, exc)
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


def sanitize_messages_for_llm(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize a chat message list before re-sending it to the upstream LLM.

    Different upstream backends accept slightly different shapes for
    the same logical message — most notably Ollama rejects an
    assistant message whose ``tool_calls[].function.arguments`` is a
    *string* (it must be an object), and most backends ignore but
    sometimes reject the ``thinking`` field that some models add to
    their own responses.

    The loop stores messages verbatim (``pick_message`` returns the
    raw Ollama/OpenAI ``message`` dict, including the model's
    ``thinking`` block). On the next iteration that whole message is
    appended to the body, which trips Ollama's request validator with
    a confusing 400 (``"Value looks like object, but can't find
    closing '}' symbol"``).

    This helper is intentionally permissive:

    * Strip ``thinking`` from assistant messages (the model already
      saw it; the upstream doesn't need it).
    * Convert each ``tool_calls[i].function.arguments`` from a JSON
      string to an object. Some models / older Ollama builds emit the
      string form; Ollama's current parser only accepts the object
      form.
    * Leave everything else untouched so LM Studio / OpenAI bridges
      keep working.
    """
    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            cleaned.append(msg)
            continue
        m = dict(msg)
        # Drop ``thinking`` if present — Ollama 400s on it as input.
        m.pop("thinking", None)
        # Normalize tool_calls arguments.
        if "tool_calls" in m and isinstance(m["tool_calls"], list):
            new_calls = []
            for call in m["tool_calls"]:
                if not isinstance(call, dict):
                    new_calls.append(call)
                    continue
                c = dict(call)
                fn = c.get("function")
                if isinstance(fn, dict):
                    # Shallow-copy the function dict so we don't mutate
                    # the original (and so we can rewrite ``arguments``
                    # in place without aliasing).
                    fn = dict(fn)
                    if "arguments" in fn:
                        args = fn["arguments"]
                        if isinstance(args, str):
                            try:
                                fn["arguments"] = json.loads(args) if args.strip() else {}
                            except json.JSONDecodeError:
                                # Best effort: keep the string and let
                                # the upstream model complain — that
                                # surfaces in the conversation rather
                                # than failing the whole request with a
                                # 400.
                                logger.warning(
                                    "tool call %s: arguments is not valid JSON, leaving as string",
                                    fn.get("name", "<unknown>"),
                                )
                    c["function"] = fn
                new_calls.append(c)
            m["tool_calls"] = new_calls
        cleaned.append(m)
    return cleaned
