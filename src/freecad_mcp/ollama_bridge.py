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
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, cast

logger = logging.getLogger("FreeCADMCP.ollama_bridge")

try:
    import httpx  # type: ignore[import-not-found]

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover - exercised when httpx is absent
    httpx = None  # type: ignore[assignment]
    _HAS_HTTPX = False

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from ._mcp_tool_loop import (  # noqa: E402
    _result_to_text,
    mcp_tool_to_openai,
    run_tool_loop,
    sanitize_messages_for_llm,
)
from .circuit_breaker import CircuitBreaker  # noqa: E402

# Back-compat aliases kept so older callers still work.
_mcp_tool_to_ollama = mcp_tool_to_openai
_result_to_text = _result_to_text


def _post_json(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    """POST ``body`` as JSON to ``url`` and return the parsed response.

    Uses :mod:`httpx` when available (richer timeout/connection-pool
    semantics in the long-lived MCP server) and falls back to
    :mod:`urllib.request` so the bridge also runs in minimal
    environments — e.g. when launched from the FreeCAD dock panel
    against the system ``python3`` that doesn't have httpx installed.
    """
    # ALWAYS log the outgoing body so we can debug 4xx/5xx without a debugger.
    # Best-effort: don't let logging break the actual request.
    try:
        import os as _os
        _os.makedirs("/tmp/freecad-mcp", exist_ok=True)
        # Use a hash so we don't spam — only write if body changed since last time
        with open("/tmp/freecad-mcp/last_request_body.json", "w", encoding="utf-8") as f:
            json.dump(body, f, indent=2, ensure_ascii=False)
    except Exception:  # pragma: no cover
        pass

    if _HAS_HTTPX and httpx is not None:
        try:
            response = httpx.post(url, json=body, timeout=timeout)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            # Defensive: read the response body without depending on
            # .text/.status_code attributes that might raise in
            # some httpx edge cases.
            try:
                status = int(e.response.status_code)
            except Exception:
                status = 0
            try:
                err_body = e.response.text or ""
            except Exception:
                try:
                    err_body = e.response.content.decode("utf-8", "replace")
                except Exception:
                    err_body = f"<unreadable response: {type(e).__name__}>"
            _log_ollama_400(url, body, status, err_body)
            raise
        return cast(dict[str, Any], response.json())
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload = resp.read()
    except urllib.error.HTTPError as e:  # pragma: no cover - thin wrap
        _log_ollama_400(url, body, e.code, e.read().decode("utf-8", "replace"))
        raise RuntimeError(f"HTTP {e.code} from {url}") from e
    return cast(dict[str, Any], json.loads(payload.decode("utf-8")))


def _log_ollama_400(url: str, body: dict[str, Any], status: int, err_body: str) -> str | None:
    """Dump the failing Ollama request body + response to disk for debugging.

    Lives in ``/tmp/freecad-mcp/ollama_400_<ts>.json`` and is best-effort
    — never raises. Used by ``_post_json`` whenever Ollama 4xxs so we
    can diagnose shape mismatches without forcing the user to attach a
    debugger.

    Returns the path of the dump file (or ``None`` on failure) so the
    caller can surface it to the user.
    """
    try:
        import os as _os
        import time as _time
        ts = int(_time.time())
        out_dir = "/tmp/freecad-mcp"
        _os.makedirs(out_dir, exist_ok=True)
        path = f"{out_dir}/ollama_400_{ts}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "url": url,
                    "status": status,
                    "request": body,
                    "response": err_body,
                    "request_msg_count": len(body.get("messages", [])),
                    "request_tool_count": len(body.get("tools", [])),
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        # Print to stderr explicitly so the message reaches the user even
        # if the host FreeCAD process swallowed the logger's output.
        import sys as _sys
        msg = (
            f"[mcp] Ollama returned {status} for {url} — "
            f"dumped request body to {path} "
            f"({len(body.get('messages', []))} msgs, "
            f"{len(body.get('tools', []))} tools)\n"
        )
        try:
            _sys.stderr.write(msg)
            _sys.stderr.flush()
        except Exception:
            pass
        logger.error(
            "Ollama returned %s for %s — dumped body to %s (%d msgs, %d tools)",
            status, url, path,
            len(body.get("messages", [])),
            len(body.get("tools", [])),
        )
        return path
    except Exception:  # pragma: no cover — diagnostic only
        logger.exception("failed to dump Ollama 400 body")
        return None


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
        params = StdioServerParameters(command=self.cfg.command[0], args=list(self.cfg.command[1:]))
        return stdio_client(params)

    async def ask(self, question: str, *, model: str | None = None) -> str:
        cfg = self.cfg
        async with await self._open_mcp() as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = [mcp_tool_to_openai(t) for t in (await session.list_tools()).tools]
            chosen = model or cfg.model
            payload = {"model": chosen, "stream": False}

            async def _send(loop) -> None:
                body = dict(payload)
                # Strip ``thinking`` blocks and convert any
                # string-form ``tool_calls[].function.arguments`` back to
                # objects — Ollama's request validator rejects the
                # string form with a confusing 400.
                body["messages"] = sanitize_messages_for_llm(loop.messages)
                if tools:
                    body["tools"] = tools

                def _do() -> dict[str, Any]:
                    try:
                        return _post_json(f"{cfg.ollama_url}/api/chat", body, cfg.request_timeout_s)
                    except (httpx.HTTPStatusError, RuntimeError) as exc:
                        # If Ollama rejects the request *with tools*,
                        # retry once *without tools* so the user at
                        # least gets a text answer. This protects
                        # against mysterious shape mismatches where
                        # one of the 55 tool specs triggers a 400 we
                        # haven't diagnosed yet.
                        if "tools" in body and body["tools"]:
                            from freecad_mcp._mcp_tool_loop import sanitize_messages_for_llm as _san
                            logger.warning(
                                "Ollama 4xx with tools; retrying without tools: %s",
                                exc,
                            )
                            fallback_msgs = cast(list[dict[str, Any]], body.get("messages") or [])
                            fallback = {
                                "model": body.get("model", cfg.model),
                                "stream": False,
                                "messages": _san(fallback_msgs),
                            }
                            return _post_json(f"{cfg.ollama_url}/api/chat", fallback, cfg.request_timeout_s)
                        raise

                loop.last_reply = cast(dict[str, Any], self.breaker.call(_do))

            init: list[dict[str, Any]] = [{"role": "user", "content": question}]
            try:
                result = await run_tool_loop(
                    session,
                    init,
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
        print('usage: python -m freecad_mcp.ollama_bridge "question" [--model M]')
        sys.exit(2)
    q = sys.argv[1]
    cfg = OllamaBridgeConfig()
    if "--model" in sys.argv:
        cfg.model = sys.argv[sys.argv.index("--model") + 1]
    print(asyncio.run(OllamaMCPBridge(cfg).ask(q)))


if __name__ == "__main__":  # pragma: no cover
    main()
