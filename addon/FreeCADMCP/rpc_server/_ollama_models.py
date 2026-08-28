"""Lightweight helpers for talking to the Ollama HTTP API.

Right now the only thing the dock panel needs is a model lister, but
this module is structured so additional endpoints (``/api/ps``,
``/api/show``, ...) can be added later without bloating
``_panel.py``.

The helpers are intentionally pure-Python + ``urllib`` (no extra deps
beyond the Python stdlib) so they can be imported inside FreeCAD's
PySide runtime as well as plain pytest.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
_LIST_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class OllamaModelInfo:
    """One entry from ``GET /api/tags``.

    ``details`` is preserved as the raw dict so future fields added by
    newer Ollama versions don't silently get dropped.
    """

    name: str
    size: int = 0
    parameter_size: str = ""
    quantization_level: str = ""
    family: str = ""
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    modified_at: str = ""
    digest: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> OllamaModelInfo:
        details = raw.get("details") or {}
        caps = details.get("capabilities") or raw.get("capabilities") or []
        if not isinstance(caps, list):
            caps = [str(caps)]
        return cls(
            name=str(raw.get("name") or raw.get("model") or ""),
            size=int(raw.get("size") or 0),
            parameter_size=str(details.get("parameter_size") or ""),
            quantization_level=str(details.get("quantization_level") or ""),
            family=str(details.get("family") or ""),
            capabilities=tuple(str(c) for c in caps),
            modified_at=str(raw.get("modified_at") or ""),
            digest=str(raw.get("digest") or ""),
            details=dict(details),
        )

    def display(self) -> str:
        """Human-readable label for a combobox."""
        bits = [self.name]
        if self.parameter_size:
            bits.append(self.parameter_size)
        if self.quantization_level:
            bits.append(self.quantization_level)
        if self.capabilities:
            bits.append(",".join(self.capabilities))
        return " — ".join(bits)


@dataclass(frozen=True)
class OllamaListResult:
    """Outcome of :func:`list_ollama_models`."""

    models: tuple[OllamaModelInfo, ...]
    url: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(m.name for m in self.models)


def _strip_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _parse_host_port(url: str) -> tuple[str, int]:
    """Extract ``(host, port)`` from an http URL. Defaults to 11434."""
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port if parsed.port is not None else 11434
    return host, port


def _ollama_reachable(url: str, timeout: float) -> bool:
    """TCP probe so we fail fast without a full HTTP request."""
    try:
        host, port = _parse_host_port(url)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def list_ollama_models(
    url: str | None = None,
    *,
    timeout: float = _LIST_TIMEOUT_S,
) -> OllamaListResult:
    """Return ``OllamaListResult`` for ``GET {url}/api/tags``.

    The function never raises; connection errors, timeouts and HTTP
    failures are captured in ``result.error`` so the panel can show
    a friendly log line instead of crashing.
    """
    candidate = url if url is not None else os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_URL)
    base = _strip_url(candidate or "")
    if not base:
        return OllamaListResult(models=(), url="", error="OLLAMA_HOST vazio")

    if not _ollama_reachable(base, timeout):
        return OllamaListResult(
            models=(),
            url=base,
            error=f"Ollama não está respondendo em {base}",
        )

    endpoint = f"{base}/api/tags"
    req = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload = resp.read()
    except urllib.error.HTTPError as e:
        return OllamaListResult(models=(), url=base, error=f"HTTP {e.code} em {endpoint}")
    except urllib.error.URLError as e:
        return OllamaListResult(models=(), url=base, error=f"URLError: {e.reason}")
    except (OSError, TimeoutError) as e:
        return OllamaListResult(models=(), url=base, error=f"{type(e).__name__}: {e}")

    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return OllamaListResult(models=(), url=base, error=f"JSON inválido: {e}")

    raw_models = body.get("models") or []
    if not isinstance(raw_models, list):
        return OllamaListResult(models=(), url=base, error="resposta sem lista 'models'")

    out: list[OllamaModelInfo] = []
    for entry in raw_models:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(OllamaModelInfo.from_api(entry))
        except (TypeError, ValueError):
            continue

    return OllamaListResult(models=tuple(out), url=base)
