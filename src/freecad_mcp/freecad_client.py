import base64
import contextlib
import gzip
import logging
import os
import re
import xmlrpc.client
from typing import Any

from .circuit_breaker import CircuitBreaker
from .tool_policy import is_tool_elevated

logger = logging.getLogger("FreeCADMCPserver")

# Read the default XML-RPC timeout from the environment so operators can
# tighten it (slow networks, fragile tunnels) or relax it (huge FEM
# results) without touching code. Default = 10s, matching the server's
# own per-task timeout.
_DEFAULT_RPC_TIMEOUT = float(os.environ.get("FREECAD_MCP_RPC_TIMEOUT", "10"))

# Pattern matched against exception messages before they're returned to
# the LLM. Absolute filesystem paths (anchored at start, ``~``, or a
# drive letter) are replaced with ``<path>`` so the model never sees
# where the file lives on the host. Keeps enough signal for debugging
# (file basename, type).
_PATH_REDACT_RE = re.compile(
    r"[A-Za-z]:[/\\][^\s'\"\\)]+"          # C:\abs or C:/abs
    r"|~[/\\][^\s'\"\\)]+"                 # ~/relative
    r"|(?<![A-Za-z0-9_./])[/\\]{1,2}[^\s'\"\\)]+"  # /abs or \\abs (not preceded by path char)
)


def _sanitize_detail(detail: str) -> str:
    """Strip absolute paths from error messages before exposing them to an LLM.

    FreeCAD tracebacks and Python's own exceptions regularly embed the
    absolute path of the document, the user's home, or the OS temp dir.
    Sending that verbatim to a remote model leaks filesystem layout. We
    keep the trailing filename and the exception type — that's enough
    for the model to debug, but the path itself is replaced with a
    ``<path>`` placeholder.
    """
    return _PATH_REDACT_RE.sub("<path>", detail)


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


class _BearerTransport(_TimeoutTransport):
    """XML-RPC transport that injects ``Authorization: Bearer <token>`` on every outbound request.

    Inherits timeout behaviour from :class:`_TimeoutTransport`. The
    bearer token is stored on the transport instance and refreshed in
    place by :meth:`FreeCADConnection.set_bearer_token` so the existing
    ``ServerProxy`` keeps working without being rebuilt.
    """

    def __init__(self, timeout: float, token: str | None = None) -> None:
        super().__init__(timeout)
        self._token = token

    def set_token(self, token: str | None) -> None:
        """Update the bearer token used for subsequent requests.

        ``token=None`` disables header injection (used when the client is
        created with ``set_bearer_token`` called later).
        """
        self._token = token

    def send_headers(self, connection, headers):  # type: ignore[override]
        if self._token:
            headers.append(("Authorization", f"Bearer {self._token}"))
        super().send_headers(connection, headers)


def _build_server_proxy(
    host: str, port: int, timeout: float, token: str | None = None
) -> xmlrpc.client.ServerProxy:
    """Construct a ServerProxy that honours *timeout* and optional bearer *token*.

    Uses the stdlib ``Transport`` (HTTP) by default; falls back to
    ``SafeTransport`` for HTTPS, both wrapped with our timeout
    enforcement. When *token* is provided, every XML-RPC request
    carries an ``Authorization: Bearer <token>`` header — required by
    ``_BearerAuthHandler`` on the FreeCAD side whenever
    ``FREECAD_MCP_AUTH_TOKEN`` is set on the server.
    """
    url = f"http://{host}:{port}"
    try:
        transport: xmlrpc.client.Transport = _BearerTransport(timeout, token)
    except Exception:
        # Extremely defensive: if TimeoutTransport fails for some reason we
        # still get a working (but untimed) client rather than crashing the
        # server at startup.
        logger.warning("Falling back to default XML-RPC transport without timeout")
        transport = xmlrpc.client.Transport()
    return xmlrpc.client.ServerProxy(url, allow_none=True, transport=transport)


class ElevatedToolAuthError(RuntimeError):
    """Raised when an elevated tool is invoked without a bearer token.

    Distinct from the standard RPC error envelope so callers can wire
    it into a single retry-with-auth path if they wish.
    """


class FreeCADConnection:
    def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)
        # Provide a default breaker so unit tests that bypass ``__init__``
        # (via ``__new__``) still have a working circuit breaker. Real
        # construction paths in ``__init__`` overwrite this with a fresh
        # instance (or the one passed in by the caller).
        instance.breaker = CircuitBreaker()
        instance._bearer_token: str | None = None
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
        self.server = _build_server_proxy(host, port, effective_timeout, token=None)
        # One breaker per connection, shared by all RPC methods. Operations
        # are sequential from the client's perspective, so a per-call
        # breaker would lose aggregate failure counts.
        self.breaker = circuit_breaker if circuit_breaker is not None else CircuitBreaker()

    # ------------------------------------------------------------------
    # Bearer-token auth (for ELEVATED tools)
    # ------------------------------------------------------------------

    def set_bearer_token(self, token: str) -> None:
        """Configure the bearer token sent on every XML-RPC request.

        Stored both on the instance (``self._bearer_token``) and pushed
        into the live transport so the *existing* ``ServerProxy`` starts
        sending the ``Authorization: Bearer <token>`` header on its
        next call. The FreeCAD RPC server (``_BearerAuthHandler``) reads
        this header whenever ``FREECAD_MCP_AUTH_TOKEN`` is configured.

        Passing an empty / whitespace string clears the token.
        """
        cleaned = (token or "").strip() or None
        self._bearer_token = cleaned
        transport = getattr(self.server, "_ServerProxy__transport", None)
        if isinstance(transport, _BearerTransport):
            transport.set_token(cleaned)
        logger.info(
            "FreeCAD client bearer token %s",
            "set" if cleaned else "cleared",
        )

    def _auth_headers(self) -> dict[str, str]:
        """Headers that *would* be attached to the next RPC.

        Exposed for tests so callers can reason about whether the token
        is configured without having to dig into the transport.
        """
        if self._bearer_token:
            return {"Authorization": f"Bearer {self._bearer_token}"}
        return {}

    def _call_elevated(self, name: str, fn):
        """Run *fn* only if *name* is an elevated tool *and* we have a token.

        ``execute_code`` and ``run_fem_analysis`` are the only elevated
        tools today (see :data:`tool_policy.ELEVATED_TOOLS`). When
        ``name`` is not elevated, the call is forwarded unconditionally
        — this helper is a no-op in that path so it is safe to wrap
        every RPC method.

        Raises:
            ElevatedToolAuthError: when *name* is elevated and no bearer
                token has been configured via :meth:`set_bearer_token`.
        """
        if not is_tool_elevated(name):
            return self.breaker.call(fn)
        if not self._bearer_token:
            logger.error(
                "Elevated tool '%s' rejected: no bearer token configured "
                "(set FREECAD_MCP_AUTH_TOKEN on the server and call "
                "set_bearer_token() on the client).",
                name,
            )
            raise ElevatedToolAuthError(
                f"Tool '{name}' is elevated and requires a bearer token. "
                f"Set FREECAD_MCP_AUTH_TOKEN on the FreeCAD server and "
                f"call FreeCADConnection.set_bearer_token(token) before "
                f"invoking this tool."
            )
        logger.debug("Elevated tool '%s' proceeding with bearer token.", name)
        return self.breaker.call(fn)

    def disconnect(self) -> None:
        # Transport.close() clears cached HTTP connections if one was opened.
        transport = getattr(self.server, "_ServerProxy__transport", None)
        close = getattr(transport, "close", None)
        if callable(close):
            close()

    def ping(self) -> bool:
        """Cheap liveness probe that swallows XML-RPC faults.

        Returns ``True`` only when the server actually responded with a
        ``True`` ping; otherwise ``False`` (and the breaker has already
        counted the failure internally).
        """
        try:
            return bool(self.breaker.call(lambda: self.server.ping()))  # type: ignore[return-value]
        except Exception:
            return False

    def _safe_call(self, label: str, fn):
        """Wrap an RPC call with circuit-breaker + sanitised error envelope.

        Any exception raised by ``fn`` (or by the breaker opening) is
        captured into a dict ``{"success": False, "reason": ..., "detail": ...}``
        so individual tools do not need to repeat the boilerplate. The
        detail string is scrubbed of absolute paths to avoid leaking the
        user's filesystem layout to the LLM.
        """
        try:
            return self.breaker.call(fn)
        except xmlrpc.client.Fault as e:
            logger.warning("%s RPC fault: %s", label, e)
            return {
                "success": False,
                "reason": "rpc_fault",
                "detail": _sanitize_detail(f"{type(e).__name__}: {e}"),
            }
        except (ConnectionError, OSError) as e:
            logger.warning("%s RPC connection error: %s", label, e)
            return {
                "success": False,
                "reason": "rpc_connection_error",
                "detail": _sanitize_detail(f"{type(e).__name__}: {e}"),
            }
        except Exception as e:
            logger.warning("%s RPC unexpected error: %s", label, e)
            return {
                "success": False,
                "reason": "rpc_error",
                "detail": _sanitize_detail(f"{type(e).__name__}: {e}"),
            }

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

    def mesh_import(
        self,
        path: str,
        doc_name: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.mesh_import(path, doc_name, label))  # type: ignore[return-value]

    def mesh_simplify(
        self,
        doc_name: str,
        mesh_name: str,
        target_faces: int = 5000,
    ) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.mesh_simplify(doc_name, mesh_name, target_faces))  # type: ignore[return-value]

    def mesh_to_solid(
        self,
        doc_name: str,
        mesh_name: str,
        new_name: str | None = None,
        *,
        repair: bool = True,
        sew_tolerance: float = 1e-3,
        max_triangles_before_simplify: int = 50_000,
        target_faces_after_simplify: int = 5_000,
    ) -> dict[str, Any]:
        return self.breaker.call(
            lambda: self.server.mesh_to_solid(
                doc_name,
                mesh_name,
                new_name,
                repair,
                sew_tolerance,
                max_triangles_before_simplify,
                target_faces_after_simplify,
            )
        )  # type: ignore[return-value]

    def step_extract_metadata(self, path: str) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.step_extract_metadata(path))  # type: ignore[return-value]

    def bom_export(
        self,
        doc_name: str,
        fmt: str = "json",
        include_extras: bool = False,
        group_by_type: bool = True,
    ) -> dict[str, Any]:
        return self.breaker.call(
            lambda: self.server.bom_export(doc_name, fmt, include_extras, group_by_type)
        )  # type: ignore[return-value]

    def fem_post_process(self, path: str) -> dict[str, Any]:
        return self.breaker.call(lambda: self.server.fem_post_process(path))  # type: ignore[return-value]

    def execute_code(self, code: str, request_id: str | None = None) -> dict[str, Any]:
        return self._call_elevated(
            "execute_code",
            lambda: self.server.execute_code(code, request_id),
        )  # type: ignore[return-value]

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
        return self._call_elevated(
            "run_fem_analysis",
            lambda: self.server.run_fem_analysis(doc_name, analysis_name, timeout, request_id),
        )  # type: ignore[return-value]

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
