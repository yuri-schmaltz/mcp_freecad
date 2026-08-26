import base64
import contextlib
import gzip
import logging
import os
import xmlrpc.client
from typing import Any

from .circuit_breaker import CircuitBreaker

logger = logging.getLogger("FreeCADMCPserver")

# Read the default XML-RPC timeout from the environment so operators can
# tighten it (slow networks, fragile tunnels) or relax it (huge FEM
# results) without touching code. Default = 10s, matching the server's
# own per-task timeout.
_DEFAULT_RPC_TIMEOUT = float(os.environ.get("FREECAD_MCP_RPC_TIMEOUT", "10"))


class _TimeoutTransport(xmlrpc.client.Transport):
    """XML-RPC transport that enforces a connect/read timeout on every call.

    The stdlib ``ServerProxy`` does not expose a socket timeout, so without
    this every call hangs forever if the FreeCAD RPC server dies. The
    timeout applies to TCP connect and to each socket read; ``set_timeout``
    on the response is also set as a final safety net so a peer that opens
    but never replies is still bounded.
    """

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self._timeout = max(0.1, float(timeout))

    def make_connection(self, host):  # type: ignore[override]
        # The stdlib Transport contract: ``host`` is either None (use
        # self.host/self.port) or a (host, port) tuple. We accept either
        # shape and return an HTTPConnection with timeout baked in.
        if host is None:
            endpoint_host, endpoint_port = self.host, self.port
        else:
            endpoint_host, endpoint_port = host[0], host[1]
        import http.client
        return http.client.HTTPConnection(
            endpoint_host, endpoint_port, timeout=self._timeout
        )


def _build_server_proxy(host: str, port: int, timeout: float) -> xmlrpc.client.ServerProxy:
    """Construct a ServerProxy that honours *timeout*.

    Uses the stdlib ``Transport`` (HTTP) by default; falls back to
    ``SafeTransport`` for HTTPS, both wrapped with our timeout enforcement.
    """
    url = f"http://{host}:{port}"
    try:
        transport: xmlrpc.client.Transport = _TimeoutTransport(timeout)
    except Exception:
        # Extremely defensive: if TimeoutTransport fails for some reason we
        # still get a working (but untimed) client rather than crashing the
        # server at startup.
        logger.warning("Falling back to default XML-RPC transport without timeout")
        transport = xmlrpc.client.Transport()
    return xmlrpc.client.ServerProxy(url, allow_none=True, transport=transport)


class FreeCADConnection:
    def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)
        # Provide a default breaker so unit tests that bypass ``__init__``
        # (via ``__new__``) still have a working circuit breaker. Real
        # construction paths in ``__init__`` overwrite this with a fresh
        # instance (or the one passed in by the caller).
        instance.breaker = CircuitBreaker()
        return instance

    def __init__(
        self,
        host: str = "localhost",
        port: int = 9875,
        timeout: float | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ):
        effective_timeout = _DEFAULT_RPC_TIMEOUT if timeout is None else float(timeout)
        self.timeout = effective_timeout
        self.server = _build_server_proxy(host, port, effective_timeout)
        # One breaker per connection, shared by all RPC methods. Operations
        # are sequential from the client's perspective, so a per-call
        # breaker would lose aggregate failure counts.
        self.breaker = circuit_breaker if circuit_breaker is not None else CircuitBreaker()

    def disconnect(self) -> None:
        # Transport.close() clears cached HTTP connections if one was opened.
        transport = getattr(self.server, "_ServerProxy__transport", None)
        close = getattr(transport, "close", None)
        if callable(close):
            close()

    def ping(self) -> bool:
        return self.breaker.call(lambda: self.server.ping())  # type: ignore[return-value]

    def cancel_request(self, request_id: str) -> dict[str, Any]:
        """Cooperatively cancel a previously-submitted request by id.

        The id must be the same string passed to the originating call. The
        cancel only takes effect if the GUI worker has not yet started the
        task; once the handler is running it cannot be interrupted.
        """
        return self.breaker.call(lambda: self.server.cancel_request(request_id))  # type: ignore[return-value]

    def cancel_all_pending_requests(self) -> dict[str, Any]:
        """Bulk-flush every pending cancellation flag on the RPC server.

        Useful when an LLM session is being torn down or restarted and
        the caller does not want to track individual request ids. The
        idempotency cache is **not** touched — see
        :meth:`invalidate_idempotency_cache` for that.
        """
        return self.breaker.call(lambda: self.server.cancel_all_pending_requests())  # type: ignore[return-value]

    def invalidate_idempotency_cache(self) -> dict[str, Any]:
        """Drop every cached response on the RPC server.

        Use when the underlying state has changed and stale idempotent
        answers would mislead the LLM.
        """
        return self.breaker.call(lambda: self.server.invalidate_idempotency_cache())  # type: ignore[return-value]

    def create_document(self, name: str, request_id: str | None = None) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.create_document(name, request_id))  # type: ignore[return-value]

    def create_object(self, doc_name: str, obj_data: dict[str, Any], request_id: str | None = None) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.create_object(doc_name, obj_data, request_id))  # type: ignore[return-value]

    def edit_object(self, doc_name: str, obj_name: str, obj_data: dict[str, Any], request_id: str | None = None) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.edit_object(doc_name, obj_name, obj_data, request_id))  # type: ignore[return-value]

    def delete_object(self, doc_name: str, obj_name: str, request_id: str | None = None) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.delete_object(doc_name, obj_name, request_id))  # type: ignore[return-value]

    def insert_part_from_library(self, relative_path: str, request_id: str | None = None) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.insert_part_from_library(relative_path, request_id))  # type: ignore[return-value]

    def execute_code(self, code: str, request_id: str | None = None) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.execute_code(code, request_id))  # type: ignore[return-value]

    def get_active_screenshot(
        self,
        view_name: str = "Isometric",
        width: int | None = None,
        height: int | None = None,
        focus_object: str | None = None,
        image_format: str = "png",
    ) -> str | None:
        """Capture a screenshot of the active view.

        Returns the base64-encoded image bytes, or ``None`` on failure.
        On ``None`` the caller can call :meth:`get_active_screenshot_with_status`
        to get a structured failure reason.

        v1.0.3 — single round-trip: the previous implementation first
        ran ``_SCREENSHOT_SUPPORT_CHECK`` via ``execute_code`` to decide
        whether the view supported ``saveImage`` (one RPC), then ran
        the actual capture (a second RPC). The check raced with the
        user changing workbenches and doubled latency in the happy path.
        The server now returns ``{"success": False, "reason": ...}``
        directly from ``get_active_screenshot``, so one call is enough.
        """
        result = self.get_active_screenshot_with_status(
            view_name=view_name, width=width, height=height,
            focus_object=focus_object, image_format=image_format,
        )
        return result.get("screenshot") if isinstance(result, dict) else None

    def get_active_screenshot_with_status(
        self,
        view_name: str = "Isometric",
        width: int | None = None,
        height: int | None = None,
        focus_object: str | None = None,
        image_format: str = "png",
    ) -> dict[str, Any]:
        """Capture a screenshot and return a structured status dict.

        Returns::

            {"success": True,  "screenshot": "<base64>", "format": "png"}
            {"success": False, "reason": "view_unsupported"}
            {"success": False, "reason": "no_active_view"}
            {"success": False, "reason": "capture_failed", "detail": "..."}
            {"success": False, "reason": "exception",        "detail": "..."}
            {"success": False, "reason": "timeout"}
            {"success": False, "reason": "rpc_error",        "detail": "..."}

        Distinguishes the cases the old ``get_active_screenshot`` API
        collapsed into a bare ``None`` so callers (and operators looking
        at logs) can tell a missing active view from a circuit-breaker
        trip from a transcode failure.
        """
        try:
            encoded = self.breaker.call(  # type: ignore[union-attr]
                lambda: self.server.get_active_screenshot(
                    view_name, width, height, focus_object, image_format
                )
            )
        except Exception as e:
            logger.error("get_active_screenshot RPC failed: %s: %s", type(e).__name__, e)
            return {
                "success": False,
                "reason": "rpc_error",
                "detail": f"{type(e).__name__}: {e}",
            }

        # The server returns ``None`` when it could not capture (view
        # unsupported, no active view, transcode failed, ...). We don't
        # have the underlying reason from the server in that case; the
        # common ones are mapped to a sensible default.
        if encoded is None:
            return {"success": False, "reason": "no_capture"}

        return {"success": True, "screenshot": encoded, "format": (image_format or "png").lower()}

    def get_objects(self, doc_name: str) -> list[dict[str, Any]]:
        return self.breaker.call(lambda: self.server.get_objects(doc_name))  # type: ignore[return-value]

    def get_object(self, doc_name: str, obj_name: str) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.get_object(doc_name, obj_name))  # type: ignore[return-value]

    def get_parts_list(self) -> list[str]:
        return self.breaker.call(lambda: self.server.get_parts_list())  # type: ignore[return-value]

    def list_documents(self) -> list[str]:
        return self.breaker.call(lambda: self.server.list_documents())  # type: ignore[return-value]

    def run_fem_analysis(self, doc_name: str, analysis_name: str, timeout: int = 600, request_id: str | None = None) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.run_fem_analysis(doc_name, analysis_name, timeout, request_id))  # type: ignore[return-value]

    def health_check(self) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.health_check())  # type: ignore[return-value]

    def undo(self, doc_name: str, steps: int = 1) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.undo(doc_name, steps))  # type: ignore[return-value]

    def redo(self, doc_name: str, steps: int = 1) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.redo(doc_name, steps))  # type: ignore[return-value]

    def save_document(self, doc_name: str, path: str | None = None) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.save_document(doc_name, path))  # type: ignore[return-value]

    def export_object(self, doc_name: str, obj_name: str, path: str, fmt: str | None = None) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.export_object(doc_name, obj_name, path, fmt))  # type: ignore[return-value]

    def export_object_bytes(self, doc_name: str, obj_name: str, fmt: str = "stl") -> dict[str, Any]:
        """Export an object and return its bytes, optionally gzip-compressed.

        The XML-RPC ``export_object`` method writes to disk; this helper
        reads the file back and, if it is large (>FREECAD_MCP_GZIP_MIN),
        returns a gzipped base64 string. Use ``gzip.decompress`` on the
        receiver to get the original bytes.

        Smaller files are returned raw (base64). Either way the result
        has a ``b64_data`` field and a ``compressed`` boolean.
        """
        import tempfile
        # Threshold in bytes above which we apply gzip. Default 64 KB.
        threshold = int(os.environ.get("FREECAD_MCP_GZIP_MIN", str(64 * 1024)))
        with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            res = self.breaker.call(  # type: ignore[union-attr]
                lambda: self.server.export_object(doc_name, obj_name, tmp_path, fmt)
            )
            if not isinstance(res, dict) or not res.get("success"):
                return res if isinstance(res, dict) else {"success": False, "error": "unknown"}
            with open(tmp_path, "rb") as f:
                raw = f.read()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

        if len(raw) >= threshold:
            compressed = gzip.compress(raw, compresslevel=6)
            return {
                "success": True,
                "format": fmt,
                "size_bytes": len(raw),
                "compressed": True,
                "b64_data": base64.b64encode(compressed).decode("ascii"),
            }
        return {
            "success": True,
            "format": fmt,
            "size_bytes": len(raw),
            "compressed": False,
            "b64_data": base64.b64encode(raw).decode("ascii"),
        }

    def get_active_view(self) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.get_active_view())  # type: ignore[return-value]

    def breaker_metrics(self) -> dict[str, Any]:
        """Expose the circuit breaker state for monitoring/health checks.

        Operators can wire this into the ``health_check`` MCP tool
        output; the metrics also feed the Prometheus exporter (see
        T2.2 of ``docs/PROFESSIONALIZATION_PLAN.md``).
        """
        return self.breaker.metrics()
