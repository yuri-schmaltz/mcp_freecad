"""FreeCAD RPC server orchestrator.

This module is the public entry point of the addon. After the v0.4.0
refactor it is intentionally thin: the heavy lifting is split into:

* :mod:`.parts_library`     \u2014 path-traversal-safe parts library.
* :mod:`.serialize`         \u2014 defensive FreeCAD object serializer.
* :mod:`._fem_workdir`      \u2014 CalculiX scratch-dir cleanup.
* :mod:`._request_tracking` \u2014 idempotency + cooperative cancellation.
* :mod:`._security_gate`    \u2014 non-loopback bind gate (TLS + auth).
* :mod:`._settings`         \u2014 JSON settings persistence with fallbacks.
* :mod:`._dispatch`         \u2014 GUI-thread queue + RPC server lifecycle.
* :mod:`._screenshot`       \u2014 PNG \u2192 JPEG/WebP transcoding helper.
* :mod:`._commands`         \u2014 FreeCAD toolbar/menu command classes.

The file you are reading is responsible only for:

1. The :class:`Object` dataclass and :func:`set_object_property` helper
   used by the GUI-thread create/edit methods.
2. The :class:`FreeCADRPC` class \u2014 the actual method implementations
   that the XML-RPC server dispatches to.
3. :func:`start_rpc_server` / :func:`stop_rpc_server` (lifecycle).
4. :func:`validate_allowed_ips` (the IP allowlist parser).
5. Wiring up the FreeCAD workbench on import.

The file is large because :class:`FreeCADRPC` is large; that class is
the FreeCAD-side equivalent of the MCP tool surface and has to mirror
every operation the client can call.
"""
from __future__ import annotations

import base64
import contextlib
import hmac
import http.client
import io
import ipaddress
import os
import queue
import ssl
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from xmlrpc.server import SimpleXMLRPCServer

from PySide import QtCore

try:
    import FreeCAD
    import FreeCADGui
    import ObjectsFem
except Exception:
    # Module loaded outside FreeCAD during unit tests; the test
    # harness injects FreeCAD stubs as needed.
    FreeCAD = None  # type: ignore[assignment]
    FreeCADGui = None  # type: ignore[assignment]
    ObjectsFem = None  # type: ignore[assignment]

from ._dispatch import (
    _DISPATCH_SHUTDOWN,
    _SERVER_START_TIME,
    _resolve_screenshot_size,
    _rpc_lock,
    process_gui_tasks,
    rpc_request_queue,
    rpc_response_queue,
    rpc_server_instance,
    rpc_server_thread,
)
from ._fem_workdir import keep_fem_workdir as _keep_fem_workdir
from ._fem_workdir import safe_rmtree as _safe_rmtree
from ._request_tracking import get_default_tracker as _get_tracker
from ._screenshot import transcode_to_format  # noqa: F401  (re-exported as _transcode_screenshot below)
from ._security_gate import can_start_remote_server, format_refusal_message
from ._settings import (
    _ensure_dir,  # noqa: F401  (re-exported for back-compat with v0.3.x)
    _get_settings_path,
    _resolve_settings_dir,  # noqa: F401  (re-exported for back-compat with v0.3.x)
    _writable_dir,  # noqa: F401  (re-exported for back-compat with v0.3.x)
    load_settings,
    save_settings,  # noqa: F401  (re-exported for back-compat; external callers do rpc_mod.save_settings)
)
from .parts_library import get_parts_list, insert_part_from_library
from .mesh_to_solid import mesh_import, mesh_simplify, mesh_to_solid
from .step_metadata import step_extract_metadata
from .bom import bom_export
from .fem_post_process import fem_post_process
from .serialize import serialize_object

# Backward-compat alias (legacy name in v0.3.x and earlier).
_transcode_screenshot = transcode_to_format

# NOTE: ``_commands`` is imported lazily at workbench-wiring time (see
# below) to break the circular import:
#   rpc_server → _commands → rpc_server (for start/stop)
# ``_sync_toggle_states`` is also pulled in there for the same reason.

# IP allowlist validation lives in :mod:`._ip_allowlist` (extracted
# in v1.0.3 so the logic is testable without FreeCAD). The wrappers
# below preserve the public surface that ``ConfigureAllowedIPsCommand``
# and the test suite rely on.
from ._ip_allowlist import (  # noqa: E402  — after the local imports above
    parse_allowlist,
    parse_allowlist_to_networks,
)


def validate_allowed_ips(allowed_ips_str: str) -> tuple[list[str], list[str]]:
    """Back-compat wrapper around :func:`._ip_allowlist.parse_allowlist`.

    Validates a comma-separated string of IP addresses/subnets and
    returns ``(valid, errors)``. See :mod:`._ip_allowlist` for the
    detailed rules (malformed-list rejection, no ``0.0.0.0/0``, etc.).
    """
    return parse_allowlist(allowed_ips_str)


def _parse_allowed_ips(allowed_ips_str: str) -> list:
    """Back-compat wrapper that also emits FreeCAD console warnings."""
    def _warn(msg: str) -> None:
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintWarning(f"MCP RPC: {msg}, skipping\n")
    return list(parse_allowlist_to_networks(allowed_ips_str, on_warning=_warn))


# --- Bearer-token auth + IP-filtered + TLS XML-RPC server -----------------

def _get_auth_token() -> str | None:
    """Read the auth token from the environment, if any.

    The token is a shared secret. The client must send the
    ``Authorization: Bearer <token>`` header on every request. The
    match is constant-time to avoid leaking the token length via
    timing side-channels.
    """
    tok = os.environ.get("FREECAD_MCP_AUTH_TOKEN", "").strip()
    return tok or None


class _BearerAuthHandler:
    """Mixin-like helper that authenticates the ``Authorization`` header.

    The XML-RPC server decodes HTTP requests in
    ``SimpleXMLRPCServer.parse_request``; we hook in via
    ``SimpleXMLRPCServer.setup`` to wrap the request socket and read
    the headers ourselves before handing off.
    """

    _AUTH_SCHEME = "Bearer "

    def _check_auth(self, headers: str) -> bool:
        expected = _get_auth_token()
        if expected is None:
            # No token configured: auth disabled, allow the request.
            return True
        # Find the Authorization header (case-insensitive).
        auth_value: str | None = None
        for line in headers.splitlines():
            if ":" not in line:
                continue
            name, _, value = line.partition(":")
            if name.strip().lower() == "authorization":
                auth_value = value.strip()
                break
        if auth_value is None or not auth_value.startswith(self._AUTH_SCHEME):
            return False
        presented = auth_value[len(self._AUTH_SCHEME):]
        # Constant-time compare to avoid timing side-channels.
        return hmac.compare_digest(presented, expected)


class FilteredXMLRPCServer(SimpleXMLRPCServer, _BearerAuthHandler):
    """XML-RPC server that filters connections by allowed IP, optional TLS, and optional bearer-token auth.

    Configuration is read from the environment on instantiation:

    * ``allowed_ips_str`` (constructor arg) \u2014 comma-separated list of
      CIDR subnets / IP addresses that may connect.
    * ``FREECAD_MCP_TLS_CERT`` / ``FREECAD_MCP_TLS_KEY`` \u2014 paths to PEM
      certificate and private key. If both are set, the server wraps
      every accepted socket in TLS via ``ssl.wrap_socket``.
    * ``FREECAD_MCP_AUTH_TOKEN`` \u2014 shared secret. If set, every request
      must carry a matching ``Authorization: Bearer <token>`` header.
      The check is constant-time.
    """

    def __init__(self, addr, allowed_ips_str="127.0.0.1", tls_cert=None, tls_key=None, **kwargs):
        self._allowed_networks = _parse_allowed_ips(allowed_ips_str)
        self._tls_cert = tls_cert
        self._tls_key = tls_key
        self._ssl_context: ssl.SSLContext | None = None
        if tls_cert and tls_key:
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
                # Reasonable defaults: refuse ancient protocols.
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                self._ssl_context = ctx
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintMessage(
                        f"MCP RPC: TLS enabled (cert={tls_cert})\n"
                    )
            except Exception as e:
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintError(
                        f"MCP RPC: failed to load TLS cert/key ({type(e).__name__}: {e}); "
                        "falling back to plain HTTP. DO NOT enable remote connections.\n"
                    )
        super().__init__(addr, **kwargs)
        if _get_auth_token() is not None and FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage("MCP RPC: bearer-token auth enabled\n")

    # Cap concurrent TLS handshakes / accept() calls. Prevents a
    # Slowloris-style flood from pinning the server thread.
    _ACCEPT_TIMEOUT_S = 5.0
    _AUTH_HEADER_TIMEOUT_S = 5.0
    _MAX_HEADER_LINES = 64

    def get_request(self):
        """Accept a connection and optionally wrap it in TLS."""
        # Apply a per-accept timeout so a stalled client cannot pin the
        # server thread indefinitely.
        try:
            sock, addr = super().get_request()
        except OSError as e:
            # Timeout or connection reset — log and let the caller retry.
            if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                FreeCAD.Console.PrintWarning(
                    f"MCP RPC: accept() error: {type(e).__name__}: {e}\n"
                )
            raise
        with contextlib.suppress(OSError):
            sock.settimeout(self._ACCEPT_TIMEOUT_S)
        if self._ssl_context is not None:
            try:
                sock = self._ssl_context.wrap_socket(sock, server_side=True)
            except Exception as e:
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintWarning(
                        f"MCP RPC: TLS handshake from {addr} failed: {type(e).__name__}: {e}\n"
                    )
                with contextlib.suppress(Exception):
                    sock.close()
                # Re-raise so SimpleXMLRPCServer drops the connection.
                raise
        return sock, addr

    def verify_request(self, request, client_address):
        client_ip = client_address[0]
        try:
            addr = ipaddress.ip_address(client_ip)
            for network in self._allowed_networks:
                if addr in network:
                    return True
        except ValueError:
            pass
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintWarning(
                f"MCP RPC: Rejected connection from {client_ip}\n"
            )
        return False

    def parse_request(self) -> bool:
        """Parse an HTTP request, optionally enforcing bearer-token auth.

        This override exists to support ``FREECAD_MCP_AUTH_TOKEN``-gated
        RPC traffic without re-introducing the legacy H3 double-read bug.

        Background (CPython 3.12 stdlib):
            ``BaseHTTPRequestHandler.handle_one_request`` reads the
            request line from the canonical request file via
            ``self.raw_requestline = self.rfile.readline(65537)`` and
            then calls ``self.parse_request()``. The default
            ``parse_request`` uses that buffered ``raw_requestline`` and
            then calls ``http.client.parse_headers(self.rfile, ...)`` to
            consume the header block from the same ``rfile``. After it
            returns, ``self.rfile`` is positioned at the start of the
            request body and ``self.headers`` is populated.

            Reference: CPython 3.12 ``Lib/http/server.py``,
            ``BaseHTTPRequestHandler.parse_request`` and
            ``handle_one_request``.

        Auth-disabled path:
            Delegate straight to ``super().parse_request()`` so any
            future CPython hardening (stricter request-line validation,
            HTTP/2 rejection, …) is inherited for free.

        Auth-enabled path:
            Reimplement the stdlib ``parse_request`` inline so the
            headers are read **exactly once** off ``self.rfile`` before
            being validated. We use ``http.client.parse_headers`` (the
            same helper the stdlib uses) which consumes the bytes from
            ``self.rfile`` once. We then validate the ``Authorization``
            header against the configured bearer token. If auth fails
            we send a 401 response and return ``False``. If auth
            succeeds we complete the rest of the stdlib parse logic
            and return ``True`` with ``self.rfile`` positioned at the
            body — exactly as the upstream contract requires.

            No second ``rfile.readline()`` or ``sock.makefile()`` is
            ever performed in this branch.
        """
        if _get_auth_token() is None:
            return super().parse_request()
        return self._parse_request_with_auth()

    def _parse_request_with_auth(self) -> bool:
        """Auth-enabled replacement for ``BaseHTTPRequestHandler.parse_request``.

        Mirrors CPython 3.12 ``Lib/http/server.py`` step-for-step, with
        one addition: after the header block is parsed but before any
        user-visible ``send_error`` call, we validate the bearer
        token.

        Socket-read count: **1** — performed by ``handle_one_request``'s
        initial ``self.rfile.readline(65537)``. This method does NOT
        call ``self.rfile.readline`` or ``sock.makefile``. The only
        rfile consumption is the single
        ``http.client.parse_headers(self.rfile, ...)`` call.
        """
        # ------------------------------------------------------------------
        # Phase 1 — request line. ``self.raw_requestline`` was already
        # buffered by ``handle_one_request``. No socket read here.
        # ------------------------------------------------------------------
        self.command = None
        self.request_version = self.default_request_version
        self.close_connection = True

        requestline = str(self.raw_requestline, "iso-8859-1")
        requestline = requestline.rstrip("\r\n")
        self.requestline = requestline
        words = requestline.split()
        if len(words) == 0:
            return False

        if len(words) >= 3:
            version = words[-1]
            try:
                if not version.startswith("HTTP/"):
                    raise ValueError
                base_version_number = version.split("/", 1)[1]
                version_number = base_version_number.split(".")
                if len(version_number) != 2:
                    raise ValueError
                if any(not component.isdigit() for component in version_number):
                    raise ValueError("non digit in http version")
                if any(len(component) > 10 for component in version_number):
                    raise ValueError("unreasonable length http version")
                version_number = int(version_number[0]), int(version_number[1])
            except (ValueError, IndexError):
                self.send_error(
                    HTTPStatus.BAD_REQUEST,
                    "Bad request version (%r)" % version,  # noqa: UP031 (stdlib fidelity)
                )
                return False
            if version_number >= (1, 1) and self.protocol_version >= "HTTP/1.1":
                self.close_connection = False
            if version_number >= (2, 0):
                self.send_error(
                    HTTPStatus.HTTP_VERSION_NOT_SUPPORTED,
                    "Invalid HTTP version (%s)" % base_version_number,  # noqa: UP031 (stdlib fidelity)
                )
                return False
            self.request_version = version

        if not 2 <= len(words) <= 3:
            self.send_error(
                HTTPStatus.BAD_REQUEST,
                "Bad request syntax (%r)" % requestline,  # noqa: UP031 (stdlib fidelity)
            )
            return False
        command, path = words[:2]
        if len(words) == 2:
            self.close_connection = True
            if command != "GET":
                self.send_error(
                    HTTPStatus.BAD_REQUEST,
                    "Bad HTTP/0.9 request type (%r)" % command,  # noqa: UP031 (stdlib fidelity)
                )
                return False
        self.command, self.path = command, path

        # gh-87389: protect against open-redirect attacks via "//path".
        if self.path.startswith("//"):
            self.path = "/" + self.path.lstrip("/")

        # ------------------------------------------------------------------
        # Phase 2 — headers. This is the ONLY socket read for the auth
        # branch: ``http.client.parse_headers`` consumes the header
        # block off ``self.rfile`` once.
        # ------------------------------------------------------------------
        try:
            self.headers = http.client.parse_headers(
                self.rfile, _class=self.MessageClass,
            )
        except http.client.LineTooLong as err:
            self.send_error(
                HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE,
                "Line too long",
                str(err),
            )
            return False
        except http.client.HTTPException as err:
            self.send_error(
                HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE,
                "Too many headers",
                str(err),
            )
            return False

        # ------------------------------------------------------------------
        # Phase 2.5 — bearer-token gate. Runs AFTER ``parse_headers``
        # so ``self.headers`` is fully populated, but BEFORE we touch
        # Connection / Expect so a failed auth returns False cheaply.
        # We never re-read the socket.
        # ------------------------------------------------------------------
        auth_value = self.headers.get("Authorization")
        headers_text = f"Authorization: {auth_value}" if auth_value else ""
        if not self._check_auth(headers_text):
            if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                FreeCAD.Console.PrintWarning(
                    f"MCP RPC: rejected request with bad/missing bearer "
                    f"token from {self.client_address[0] if self.client_address else '?'}\n"
                )
            self.send_error(
                HTTPStatus.UNAUTHORIZED,
                "Unauthorized",
                "Missing or invalid bearer token.",
            )
            return False

        # ------------------------------------------------------------------
        # Phase 3 — Connection / Expect directives (identical to stdlib).
        # ------------------------------------------------------------------
        conntype = self.headers.get("Connection", "")
        if conntype.lower() == "close":
            self.close_connection = True
        elif (
            conntype.lower() == "keep-alive"
            and self.protocol_version >= "HTTP/1.1"
        ):
            self.close_connection = False
        expect = self.headers.get("Expect", "")
        expect_ok = (
            expect.lower() != "100-continue"
            or self.protocol_version < "HTTP/1.1"
            or self.request_version < "HTTP/1.1"
            or self.handle_expect_100()
        )
        return expect_ok


# --- Object helper + per-property setter ----------------------------------

@dataclass
class Object:
    name: str
    type: str | None = None
    analysis: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)


def set_object_property(
    doc: FreeCAD.Document,
    obj: FreeCAD.DocumentObject,
    properties: dict[str, Any],
    on_warning: Callable[[str, str], None] | None = None,
) -> list[tuple[str, str]]:
    """Apply *properties* to *obj* and return a list of per-property errors.

    Each error is returned as ``(property_name, error_message)`` so the
    caller (typically the MCP layer) can decide whether to surface the
    partial failure. The function never raises for per-property
    failures (one bad property does not poison the rest) but
    propagates the document-level exceptions because those indicate a
    broken state we cannot continue from.

    ``on_warning`` is an optional callback invoked as
    ``on_warning(property, message)`` for each individual failure.
    """
    errors: list[tuple[str, str]] = []
    for prop, val in properties.items():
        try:
            if prop in obj.PropertiesList:
                if prop == "Placement" and isinstance(val, dict):
                    if "Base" in val:
                        pos = val["Base"]
                    elif "Position" in val:
                        pos = val["Position"]
                    else:
                        pos = {}
                    rot = val.get("Rotation", {})
                    placement = FreeCAD.Placement(
                        FreeCAD.Vector(
                            pos.get("x", 0),
                            pos.get("y", 0),
                            pos.get("z", 0),
                        ),
                        FreeCAD.Rotation(
                            FreeCAD.Vector(
                                rot.get("Axis", {}).get("x", 0),
                                rot.get("Axis", {}).get("y", 0),
                                rot.get("Axis", {}).get("z", 1),
                            ),
                            rot.get("Angle", 0),
                        ),
                    )
                    setattr(obj, prop, placement)

                elif isinstance(getattr(obj, prop), FreeCAD.Vector) and isinstance(
                    val, dict
                ):
                    vector = FreeCAD.Vector(
                        val.get("x", 0), val.get("y", 0), val.get("z", 0)
                    )
                    setattr(obj, prop, vector)

                elif prop in ["Base", "Tool", "Source", "Profile"] and isinstance(
                    val, str
                ):
                    ref_obj = doc.getObject(val)
                    if ref_obj:
                        setattr(obj, prop, ref_obj)
                    else:
                        raise ValueError(f"Referenced object '{val}' not found.")

                elif prop == "References" and isinstance(val, list):
                    refs = []
                    for ref_name, face in val:
                        ref_obj = doc.getObject(ref_name)
                        if ref_obj:
                            refs.append((ref_obj, face))
                        else:
                            raise ValueError(f"Referenced object '{ref_name}' not found.")
                    setattr(obj, prop, refs)

                else:
                    setattr(obj, prop, val)
            elif prop == "ShapeColor" and isinstance(val, (list, tuple)):
                setattr(obj.ViewObject, prop, (float(val[0]), float(val[1]), float(val[2]), float(val[3])))
            elif prop == "ViewObject" and isinstance(val, dict):
                for k, v in val.items():
                    if k == "ShapeColor":
                        setattr(obj.ViewObject, k, (float(v[0]), float(v[1]), float(v[2]), float(v[3])))
                    else:
                        setattr(obj.ViewObject, k, v)
            else:
                setattr(obj, prop, val)

        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            errors.append((prop, msg))
            if on_warning is not None:
                on_warning(prop, msg)
            else:
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintError(f"Property '{prop}' assignment error: {e}\n")
    return errors


# --- FreeCADRPC class ------------------------------------------------------

class FreeCADRPC:
    """RPC server for FreeCAD.

    The per-operation default timeouts are tuned for typical CAD work and
    can be overridden at three levels (lowest priority first):

    1. The ``TIMEOUT`` class constant (10s \u2014 kept for back-compat).
    2. The ``FREECAD_MCP_DEFAULT_RPC_TIMEOUT`` env var (single number,
       applies to every operation that does not declare its own).
    3. The ``FREECAD_MCP_RPC_TIMEOUTS`` env var (JSON object keyed by
       operation name, e.g. ``{"create_object": 120, "run_fem_analysis":
       900}``).
    4. The per-call ``timeout`` argument the MCP client passes through
       (highest priority; falls back to 1/2/3).

    A short timeout on a slow operation will produce
    ``{"success": false, "error": "no response within Xs ..."}`` which
    the caller can recognise and retry with a longer deadline.
    """

    TIMEOUT = 10  # backwards-compat default; see PER_OPERATION_TIMEOUTS.

    PER_OPERATION_TIMEOUTS: dict[str, float] = {
        "create_document": 30.0,
        "create_object": 60.0,         # mesh generation can be slow
        "edit_object": 60.0,
        "delete_object": 30.0,
        "execute_code": 30.0,
        "insert_part_from_library": 30.0,
        "run_fem_analysis": 600.0,
        "get_active_screenshot": 30.0,
        "mesh_import": 30.0,
        "mesh_simplify": 60.0,
        "mesh_to_solid": 300.0,
        "step_extract_metadata": 5.0,
        "bom_export": 60.0,
        "fem_post_process": 60.0,
        "cancel_request": 5.0,
    }

    def __init__(self) -> None:
        # Apply env overrides. Errors are non-fatal \u2014 we keep the
        # in-code defaults if the env vars are malformed.
        import copy
        import json as _json

        # IMPORTANT: copy the class-level dict per instance so env
        # overrides applied at instance time do not leak back into the
        # class attribute (and contaminate other instances constructed
        # later).
        self.PER_OPERATION_TIMEOUTS = copy.deepcopy(self.PER_OPERATION_TIMEOUTS)

        default = os.environ.get("FREECAD_MCP_DEFAULT_RPC_TIMEOUT")
        if default:
            try:
                self.TIMEOUT = float(default)
            except (TypeError, ValueError):
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintWarning(
                        f"MCP RPC: invalid FREECAD_MCP_DEFAULT_RPC_TIMEOUT={default!r}, "
                        "ignoring.\n"
                    )

        per_op_raw = os.environ.get("FREECAD_MCP_RPC_TIMEOUTS")
        if per_op_raw:
            try:
                parsed = _json.loads(per_op_raw)
                if isinstance(parsed, dict):
                    for op, value in parsed.items():
                        try:
                            self.PER_OPERATION_TIMEOUTS[str(op)] = float(value)
                        except (TypeError, ValueError):
                            if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                                FreeCAD.Console.PrintWarning(
                                    f"MCP RPC: invalid timeout for op {op!r}: {value!r}, ignoring.\n"
                                )
            except _json.JSONDecodeError as e:
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintWarning(
                        f"MCP RPC: invalid FREECAD_MCP_RPC_TIMEOUTS JSON: {e}, ignoring.\n"
                    )

    def _timeout_for(self, operation: str, override: float | None = None) -> float:
        """Resolve the effective timeout for *operation*."""
        if override is not None:
            try:
                v = float(override)
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
        return float(self.PER_OPERATION_TIMEOUTS.get(operation, self.TIMEOUT))

    def ping(self):
        return True

    def health_check(self) -> dict[str, Any]:
        import time as _time
        now = _time.time()
        tracker = _get_tracker()
        with _rpc_lock:
            running = rpc_server_instance is not None
        return {
            "success": True,
            "uptime_seconds": round(now - _SERVER_START_TIME, 3),
            "rpc_server_running": running,
            "request_queue_size": rpc_request_queue.qsize(),
            "response_queue_size": rpc_response_queue.qsize(),
            "cached_responses": len(tracker.cached_ids()),
            "pending_cancellations": len(tracker.pending_cancellations()),
            "settings_dir": _get_settings_path(),
        }

    def undo(self, doc_name: str, steps: int = 1) -> dict[str, Any]:
        def task():
            doc = FreeCAD.getDocument(doc_name)
            if not doc:
                return {"success": False, "error": f"Document '{doc_name}' not found."}
            for _ in range(max(1, int(steps))):
                doc.undo()
            doc.recompute()
            return {"success": True, "undone_steps": max(1, int(steps))}
        return self._tracked_call(None, task, self._timeout_for("create_document"))

    def redo(self, doc_name: str, steps: int = 1) -> dict[str, Any]:
        def task():
            doc = FreeCAD.getDocument(doc_name)
            if not doc:
                return {"success": False, "error": f"Document '{doc_name}' not found."}
            for _ in range(max(1, int(steps))):
                doc.redo()
            doc.recompute()
            return {"success": True, "redone_steps": max(1, int(steps))}
        return self._tracked_call(None, task, self._timeout_for("create_document"))

    def save_document(self, doc_name: str, path: str | None = None) -> dict[str, Any]:
        def task():
            doc = FreeCAD.getDocument(doc_name)
            if not doc:
                return {"success": False, "error": f"Document '{doc_name}' not found."}
            try:
                if path:
                    doc.saveAs(path)
                    return {"success": True, "path": path}
                doc.save()
                return {"success": True, "path": doc.FileName or "<unsaved>"}
            except Exception as e:
                return {"success": False, "error": f"{type(e).__name__}: {e}"}
        return self._tracked_call(None, task, self._timeout_for("create_document"))

    def export_object(self, doc_name: str, obj_name: str, path: str, fmt: str | None = None) -> dict[str, Any]:
        def task():
            doc = FreeCAD.getDocument(doc_name)
            if not doc:
                return {"success": False, "error": f"Document '{doc_name}' not found."}
            obj = doc.getObject(obj_name)
            if not obj:
                return {"success": False, "error": f"Object '{obj_name}' not found."}
            try:
                import os as _os
                effective_fmt = fmt or _os.path.splitext(path)[1].lstrip(".").lower() or "stl"
                if effective_fmt == "stl":
                    import MeshPart
                    mesh = MeshPart.meshFromShape(obj.Shape)
                    mesh.write(path)
                else:
                    doc.exportPart = getattr(doc, "exportPart", None)
                    from FreeCAD import export as fc_export  # type: ignore
                    fc_export([obj], path)
                return {"success": True, "path": path, "format": effective_fmt}
            except Exception as e:
                return {"success": False, "error": f"{type(e).__name__}: {e}"}
        return self._tracked_call(None, task, self._timeout_for("create_object"))

    def get_active_view(self) -> dict[str, Any]:
        # Local import keeps the import graph flat and avoids loading
        # _dispatch until the user actually probes the view.
        from ._dispatch import _get_view_size

        def task():
            try:
                view = FreeCADGui.ActiveDocument.ActiveView
            except Exception as e:
                return {"success": False, "error": f"{type(e).__name__}: {e}"}
            if view is None:
                return {"success": False, "error": "no active view"}
            try:
                width, height = _get_view_size(view)
            except Exception:
                width = height = None
            return {
                "success": True,
                "view_type": type(view).__name__,
                "width": width,
                "height": height,
                "has_save_image": hasattr(view, "saveImage"),
            }
        return self._tracked_call(None, task, self._timeout_for("get_active_screenshot"))

    def cancel_request(self, request_id: str) -> dict[str, Any]:
        if not request_id or not isinstance(request_id, str):
            return {"success": False, "error": "request_id must be a non-empty string"}
        tracker = _get_tracker()
        cancelled = tracker.cancel(request_id)
        return {"success": True, "request_id": request_id, "cancelled": cancelled}

    def cancel_all_pending_requests(self) -> dict[str, Any]:
        """Bulk-flush every pending cancellation flag.

        Useful for "abort the current session" semantics: any task that
        has not yet started picking up its cancellation flag will see
        it cleared (so a re-dispatch is *not* short-circuited), but
        the bookkeeping is reset. The cached-response set is left
        alone — see :meth:`invalidate_idempotency_cache` for that.

        Returns ``{"success": True, "flushed": <int>}``.
        """
        tracker = _get_tracker()
        n = tracker.cancel_all_pending()
        return {"success": True, "flushed": n}

    def invalidate_idempotency_cache(self) -> dict[str, Any]:
        """Drop every cached response so future calls re-execute.

        Use this when the underlying state (FreeCAD document, file on
        disk) has changed and stale idempotent answers would mislead
        the LLM.

        Returns ``{"success": True, "dropped": <int>}``.
        """
        tracker = _get_tracker()
        n = tracker.invalidate_cache()
        return {"success": True, "dropped": n}

    def _tracked_call(
        self,
        request_id: str | None,
        task_factory: Callable[[], Any],
        timeout: float,
    ) -> Any:
        """Submit *task_factory* to the GUI queue with idempotency + cancel.

        See :mod:`._request_tracking` for the cancellation / caching
        contract.
        """
        tracker = _get_tracker()

        if request_id is not None:
            cached = tracker.get_cached(request_id)
            if cached is not None:
                return cached
            if tracker.consume_cancel(request_id):
                return {"success": False, "error": "request cancelled before execution", "request_id": request_id, "cancelled": True}

        def task():
            if request_id is not None and tracker.consume_cancel(request_id):
                return {"success": False, "error": "request cancelled before execution", "request_id": request_id, "cancelled": True}
            try:
                result = task_factory()
            except Exception as e:
                result = {"success": False, "error": f"{type(e).__name__}: {e}"}
            if request_id is not None:
                tracker.cache_response(request_id, result)
            return result

        rpc_request_queue.put(task)
        try:
            return rpc_response_queue.get(timeout=timeout)
        except queue.Empty:
            return {"success": False, "error": f"no response within {timeout}s (still queued or running on GUI thread)"}

    def create_document(self, name="New_Document", request_id: str | None = None, timeout: float | None = None):
        def task():
            ok = self._create_document_gui(name)
            return {"success": ok is True, "document_name": name if ok is True else None, "error": None if ok is True else ok}
        return self._tracked_call(request_id, task, self._timeout_for("create_document", timeout))

    def create_object(self, doc_name, obj_data: dict[str, Any], request_id: str | None = None, timeout: float | None = None):
        obj = Object(
            name=obj_data.get("Name", "New_Object"),
            type=obj_data["Type"],
            analysis=obj_data.get("Analysis"),
            properties=obj_data.get("Properties", {}),
        )

        def task():
            ok = self._create_object_gui(doc_name, obj)
            return {"success": ok is True, "object_name": obj.name if ok is True else None, "error": None if ok is True else ok}
        return self._tracked_call(request_id, task, self._timeout_for("create_object", timeout))

    def edit_object(self, doc_name: str, obj_name: str, properties: dict[str, Any], request_id: str | None = None, timeout: float | None = None) -> dict[str, Any]:
        obj = Object(
            name=obj_name,
            properties=properties.get("Properties", {}),
        )

        def task():
            ok = self._edit_object_gui(doc_name, obj)
            return {"success": ok is True, "object_name": obj.name if ok is True else None, "error": None if ok is True else ok}
        return self._tracked_call(request_id, task, self._timeout_for("edit_object", timeout))

    def delete_object(self, doc_name: str, obj_name: str, request_id: str | None = None, timeout: float | None = None):
        def task():
            ok = self._delete_object_gui(doc_name, obj_name)
            return {"success": ok is True, "object_name": obj_name if ok is True else None, "error": None if ok is True else ok}
        return self._tracked_call(request_id, task, self._timeout_for("delete_object", timeout))

    def mesh_import(self, path: str, doc_name: str | None = None, label: str | None = None) -> dict[str, Any]:
        def task():
            try:
                return mesh_import(path=path, doc_name=doc_name, label=label)
            except Exception as e:
                return {"success": False, "reason": f"{type(e).__name__}: {e}"}

        return self._tracked_call(None, task, self._timeout_for("mesh_import"))

    def mesh_simplify(self, doc_name: str, mesh_name: str, target_faces: int = 5000) -> dict[str, Any]:
        def task():
            try:
                return mesh_simplify(doc_name=doc_name, mesh_name=mesh_name, target_faces=target_faces)
            except Exception as e:
                return {"success": False, "reason": f"{type(e).__name__}: {e}"}

        return self._tracked_call(None, task, self._timeout_for("mesh_simplify"))

    def mesh_to_solid(
        self,
        doc_name: str,
        mesh_name: str,
        new_name: str | None = None,
        repair: bool = True,
        sew_tolerance: float = 1e-3,
        max_triangles_before_simplify: int = 50_000,
        target_faces_after_simplify: int = 5000,
    ) -> dict[str, Any]:
        def task():
            try:
                return mesh_to_solid(
                    doc_name=doc_name,
                    mesh_name=mesh_name,
                    new_name=new_name,
                    repair=repair,
                    sew_tolerance=sew_tolerance,
                    max_triangles_before_simplify=max_triangles_before_simplify,
                    target_faces_after_simplify=target_faces_after_simplify,
                )
            except Exception as e:
                return {"success": False, "reason": f"{type(e).__name__}: {e}"}

        return self._tracked_call(None, task, self._timeout_for("mesh_to_solid", 120.0))

    def step_extract_metadata(self, path: str) -> dict[str, Any]:
        def task():
            try:
                return step_extract_metadata(path=path)
            except Exception as e:
                return {"success": False, "reason": f"{type(e).__name__}: {e}"}

        return self._tracked_call(None, task, self._timeout_for("step_extract_metadata"))

    def bom_export(
        self,
        doc_name: str,
        fmt: str = "json",
        include_extras: bool = False,
        group_by_type: bool = True,
    ) -> dict[str, Any]:
        def task():
            try:
                return bom_export(
                    doc_name=doc_name,
                    fmt=fmt,
                    include_extras=include_extras,
                    group_by_type=group_by_type,
                )
            except Exception as e:
                return {"success": False, "reason": f"{type(e).__name__}: {e}"}

        return self._tracked_call(None, task, self._timeout_for("bom_export", 60.0))

    def fem_post_process(self, path: str) -> dict[str, Any]:
        def task():
            try:
                return fem_post_process(path=path)
            except Exception as e:
                return {"success": False, "reason": f"{type(e).__name__}: {e}"}

        return self._tracked_call(None, task, self._timeout_for("fem_post_process", 60.0))

    def run_fem_analysis(self, doc_name: str, analysis_name: str, timeout: int = 600, request_id: str | None = None) -> dict[str, Any]:
        try:
            timeout_s = int(timeout)
        except (TypeError, ValueError):
            return {"success": False, "error": f"invalid timeout: {timeout!r}"}

        def task():
            return self._run_fem_analysis_gui(doc_name, analysis_name)
        return self._tracked_call(request_id, task, self._timeout_for("run_fem_analysis", timeout_s))

    def execute_code(self, code: str, request_id: str | None = None, timeout: float | None = None) -> dict[str, Any]:
        def task():
            buf = io.StringIO()
            # Use a per-call globals dict so user scripts cannot leak
            # names into the rpc_server module namespace (where they
            # would persist between RPC calls and possibly collide with
            # unrelated sessions). Builtins stay reachable so plain
            # ``print()`` / ``range()`` work as expected.
            exec_globals: dict[str, Any] = {
                "__builtins__": __builtins__,
                "__name__": "__mcp_execute_code__",
                "FreeCAD": FreeCAD,
                "FreeCADGui": FreeCADGui,
            }
            try:
                with contextlib.redirect_stdout(buf):
                    exec(code, exec_globals)
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintMessage("Python code executed successfully.\n")
                return {
                    "success": True,
                    "message": "Python code execution scheduled. \nOutput: " + buf.getvalue(),
                }
            except Exception as e:
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintError(
                        f"Error executing Python code: {e}\n"
                    )
                return {
                    "success": False,
                    "error": f"Error executing Python code: {e}\n",
                    "output": buf.getvalue(),
                }
        return self._tracked_call(request_id, task, self._timeout_for("execute_code", timeout))

    def get_objects(self, doc_name):
        doc = FreeCAD.getDocument(doc_name)
        if doc:
            return [serialize_object(obj) for obj in doc.Objects]
        return []

    def get_object(self, doc_name, obj_name):
        doc = FreeCAD.getDocument(doc_name)
        if doc:
            obj = doc.getObject(obj_name)
            if obj:
                return serialize_object(obj)
            return None
        return None

    def insert_part_from_library(self, relative_path, request_id: str | None = None, timeout: float | None = None):
        def task():
            ok = self._insert_part_from_library(relative_path)
            return {"success": ok is True, "message": "Part inserted from library." if ok is True else None, "error": None if ok is True else ok}
        return self._tracked_call(request_id, task, self._timeout_for("insert_part_from_library", timeout))

    def list_documents(self):
        return list(FreeCAD.listDocuments().keys())

    def get_parts_list(self):
        return get_parts_list()

    def get_active_screenshot(
        self,
        view_name: str = "Isometric",
        width: int | None = None,
        height: int | None = None,
        focus_object: str | None = None,
        timeout: float | None = None,
        image_format: str = "png",
    ) -> str:
        """Get a screenshot of the active view.

        Returns a base64-encoded string of the screenshot or None if a
        screenshot cannot be captured (e.g., when in TechDraw or
        Spreadsheet view).

        ``image_format`` is one of ``png`` (default), ``jpeg``/``jpg``,
        or ``webp``. PNG is what FreeCAD's ``saveImage`` produces
        natively; JPEG and WebP are produced by transcoding the PNG
        with Pillow if available, or by emitting a clear error if it
        is not.
        """
        import os as _os
        fmt = (image_format or "png").lower()
        if fmt not in ("png", "jpeg", "jpg", "webp"):
            return None
        fd, tmp_path = tempfile.mkstemp(suffix=".png")
        _os.close(fd)

        def task():
            try:
                active_view = FreeCADGui.ActiveDocument.ActiveView
                if active_view is None:
                    if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                        FreeCAD.Console.PrintWarning("No active view available\n")
                    return {"success": False, "reason": "no_active_view"}

                if not hasattr(active_view, 'saveImage'):
                    if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                        FreeCAD.Console.PrintWarning("Current view does not support screenshots\n")
                    return {"success": False, "reason": "view_unsupported"}

                ok = self._save_active_screenshot(tmp_path, view_name, width, height, focus_object)
                if ok is not True:
                    return {"success": False, "reason": "capture_failed", "detail": ok}
                return {"success": True}
            except Exception as e:
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintError(f"Screenshot task raised {type(e).__name__}: {e}\n")
                return {"success": False, "reason": "exception", "detail": f"{type(e).__name__}: {e}"}

        rpc_request_queue.put(task)
        try:
            res = rpc_response_queue.get(timeout=self._timeout_for("get_active_screenshot", timeout))
        except queue.Empty:
            if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                FreeCAD.Console.PrintWarning("Screenshot capture timed out\n")
            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)
            return None

        if not isinstance(res, dict) or not res.get("success"):
            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)
            return None

        try:
            with open(tmp_path, "rb") as image_file:
                image_bytes = image_file.read()
            if fmt == "png":
                encoded = base64.b64encode(image_bytes).decode("utf-8")
            else:
                encoded = transcode_to_format(image_bytes, fmt)
                if encoded is None:
                    if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                        FreeCAD.Console.PrintWarning(
                            "MCP RPC: Pillow not installed; cannot transcode to "
                            f"{fmt}. Install with 'pip install Pillow' or pass "
                            "image_format='png'.\n"
                        )
                    return None
        finally:
            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)
        return encoded

    def _create_document_gui(self, name):
        doc = FreeCAD.newDocument(name)
        doc.recompute()
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(f"Document '{name}' created via RPC.\n")
        return True

    def _create_object_gui(self, doc_name, obj: Object):
        doc = FreeCAD.getDocument(doc_name)
        if doc:
            try:
                if obj.type == "Fem::FemMeshGmsh" and obj.analysis:
                    from femmesh.gmshtools import GmshTools
                    res = getattr(doc, obj.analysis).addObject(ObjectsFem.makeMeshGmsh(doc, obj.name))[0]
                    geom_attr = "Shape" if hasattr(res, "Shape") else ("Part" if hasattr(res, "Part") else None)
                    legacy_to_new = {
                        "Part": geom_attr,
                        "ElementSizeMax": "CharacteristicLengthMax",
                        "ElementSizeMin": "CharacteristicLengthMin",
                    }
                    geom_key = "Part" if "Part" in obj.properties else ("Shape" if "Shape" in obj.properties else None)
                    if geom_key is None:
                        raise ValueError("'Part' (or 'Shape') property not found in properties.")
                    target_obj = doc.getObject(obj.properties[geom_key])
                    if target_obj is None:
                        raise ValueError(f"Referenced object '{obj.properties[geom_key]}' not found.")
                    if geom_attr is None:
                        raise ValueError("Mesh object has neither 'Shape' nor 'Part' property.")
                    setattr(res, geom_attr, target_obj)
                    del obj.properties[geom_key]

                    for param, value in obj.properties.items():
                        target_param = legacy_to_new.get(param, param)
                        if target_param and hasattr(res, target_param):
                            setattr(res, target_param, value)
                    doc.recompute()

                    gmsh_tools = GmshTools(res)
                    gmsh_tools.create_mesh()
                    if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                        FreeCAD.Console.PrintMessage(
                            f"FEM Mesh '{res.Name}' generated successfully in '{doc_name}'.\n"
                        )
                elif obj.type.startswith("Fem::"):
                    fem_make_methods = {
                        "MaterialCommon": ObjectsFem.makeMaterialSolid,
                        "AnalysisPython": ObjectsFem.makeAnalysis,
                    }
                    obj_type_short = obj.type.split("::")[1]
                    method_name = "make" + obj_type_short
                    make_method = fem_make_methods.get(obj_type_short, getattr(ObjectsFem, method_name, None))

                    if callable(make_method):
                        res = make_method(doc, obj.name)
                        prop_errors = set_object_property(doc, res, obj.properties)
                        if prop_errors and FreeCAD is not None and hasattr(FreeCAD, "Console"):
                            FreeCAD.Console.PrintWarning(
                                f"FEM object '{res.Name}' created with {len(prop_errors)} property warning(s): "
                                f"{prop_errors}\n"
                            )
                        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                            FreeCAD.Console.PrintMessage(
                                f"FEM object '{res.Name}' created with '{method_name}'.\n"
                            )
                    else:
                        raise ValueError(f"No creation method '{method_name}' found in ObjectsFem.")
                    if obj.type != "Fem::AnalysisPython" and obj.analysis:
                        getattr(doc, obj.analysis).addObject(res)
                else:
                    res = doc.addObject(obj.type, obj.name)
                    prop_errors = set_object_property(doc, res, obj.properties)
                    if prop_errors and FreeCAD is not None and hasattr(FreeCAD, "Console"):
                        FreeCAD.Console.PrintWarning(
                            f"Object '{res.Name}' created with {len(prop_errors)} property warning(s): "
                            f"{prop_errors}\n"
                        )
                    if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                        FreeCAD.Console.PrintMessage(
                            f"{res.TypeId} '{res.Name}' added to '{doc_name}' via RPC.\n"
                        )

                doc.recompute()
                return True
            except Exception as e:
                return str(e)
        else:
            if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                FreeCAD.Console.PrintError(f"Document '{doc_name}' not found.\n")
            return f"Document '{doc_name}' not found.\n"

    def _edit_object_gui(self, doc_name: str, obj: Object):
        doc = FreeCAD.getDocument(doc_name)
        if not doc:
            if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                FreeCAD.Console.PrintError(f"Document '{doc_name}' not found.\n")
            return f"Document '{doc_name}' not found.\n"

        obj_ins = doc.getObject(obj.name)
        if not obj_ins:
            if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                FreeCAD.Console.PrintError(f"Object '{obj.name}' not found in document '{doc_name}'.\n")
            return f"Object '{obj.name}' not found in document '{doc_name}'.\n"

        try:
            if hasattr(obj_ins, "References") and "References" in obj.properties:
                refs = []
                for ref_name, face in obj.properties["References"]:
                    ref_obj = doc.getObject(ref_name)
                    if ref_obj:
                        refs.append((ref_obj, face))
                    else:
                        raise ValueError(f"Referenced object '{ref_name}' not found.")
                obj_ins.References = refs
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintMessage(
                        f"References updated for '{obj.name}' in '{doc_name}'.\n"
                    )
                del obj.properties["References"]
            prop_errors = set_object_property(doc, obj_ins, obj.properties)
            if prop_errors and FreeCAD is not None and hasattr(FreeCAD, "Console"):
                FreeCAD.Console.PrintWarning(
                    f"Object '{obj.name}' edited with {len(prop_errors)} property warning(s): "
                    f"{prop_errors}\n"
                )
            doc.recompute()
            if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                FreeCAD.Console.PrintMessage(f"Object '{obj.name}' updated via RPC.\n")
            return True
        except Exception as e:
            return str(e)

    def _run_fem_analysis_gui(self, doc_name: str, analysis_name: str):
        work_dir = None
        try:
            doc = FreeCAD.getDocument(doc_name)
            if not doc:
                return {"success": False, "error": f"Document '{doc_name}' not found."}
            analysis = doc.getObject(analysis_name)
            if analysis is None:
                return {"success": False, "error": f"Analysis '{analysis_name}' not found."}
            if analysis.TypeId not in ("Fem::FemAnalysis", "Fem::FemAnalysisPython"):
                return {"success": False, "error": f"'{analysis_name}' is not a FEM analysis (TypeId={analysis.TypeId})."}

            solver = None
            for member in analysis.Group:
                tid = getattr(member, "TypeId", "")
                if "SolverCcx" in tid or "SolverCalculix" in tid:
                    solver = member
                    break
            if solver is None:
                solver_factory = (
                    getattr(ObjectsFem, "makeSolverCalculiXCcxTools", None)
                    or getattr(ObjectsFem, "makeSolverCalculixCcxTools", None)
                )
                if solver_factory is None:
                    return {"success": False, "error": "ObjectsFem has no Calculix solver factory."}
                solver = solver_factory(doc, "CalculiX")
                analysis.addObject(solver)

            from femtools import ccxtools

            fea = ccxtools.FemToolsCcx(analysis=analysis, solver=solver)
            fea.update_objects()

            import tempfile as _tempfile
            work_dir = _tempfile.mkdtemp(prefix="freecad_mcp_fem_")
            fea.setup_working_dir(work_dir)
            fea.setup_ccx()

            prereq_msg = fea.check_prerequisites()
            if prereq_msg:
                return {"success": False, "error": f"Prerequisites failed: {prereq_msg}", "working_dir": work_dir}

            fea.purge_results()
            fea.run()
            fea.load_results()

            result_obj = None
            for member in analysis.Group:
                if "Result" in getattr(member, "TypeId", "") and hasattr(member, "vonMises"):
                    result_obj = member
                    break
            if result_obj is None:
                return {"success": False, "error": "Solver ran but no result object was produced.", "working_dir": work_dir}

            vm = list(getattr(result_obj, "vonMises", None) or [])
            disp = list(getattr(result_obj, "DisplacementLengths", None) or [])
            doc.recompute()

            return {
                "success": True,
                "result_object": result_obj.Name,
                "node_count": len(vm),
                "max_von_mises_MPa": max(vm) if vm else None,
                "min_von_mises_MPa": min(vm) if vm else None,
                "max_displacement_mm": max(disp) if disp else None,
                "working_dir": work_dir,
            }
        except Exception as e:
            import traceback
            return {
                "success": False,
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
                "working_dir": work_dir,
            }
        finally:
            if work_dir is not None and not _keep_fem_workdir():
                _safe_rmtree(
                    work_dir,
                    on_warning=lambda msg: FreeCAD.Console.PrintWarning(
                        f"MCP RPC: {msg}\n"
                    ) if FreeCAD is not None and hasattr(FreeCAD, "Console") else None,
                )

    def _delete_object_gui(self, doc_name: str, obj_name: str):
        doc = FreeCAD.getDocument(doc_name)
        if not doc:
            if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                FreeCAD.Console.PrintError(f"Document '{doc_name}' not found.\n")
            return f"Document '{doc_name}' not found.\n"

        try:
            doc.removeObject(obj_name)
            doc.recompute()
            if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                FreeCAD.Console.PrintMessage(f"Object '{obj_name}' deleted via RPC.\n")
            return True
        except Exception as e:
            return str(e)

    def _insert_part_from_library(self, relative_path):
        try:
            insert_part_from_library(relative_path)
            return True
        except Exception as e:
            return str(e)

    def _save_active_screenshot(
        self,
        save_path: str,
        view_name: str = "Isometric",
        width: int | None = None,
        height: int | None = None,
        focus_object: str | None = None,
    ):
        try:
            from ._dispatch import _flush_gui_events
            view = FreeCADGui.ActiveDocument.ActiveView
            if not hasattr(view, 'saveImage'):
                return "Current view does not support screenshots"

            if view_name == "Isometric":
                view.viewIsometric()
            elif view_name == "Front":
                view.viewFront()
            elif view_name == "Top":
                view.viewTop()
            elif view_name == "Right":
                view.viewRight()
            elif view_name == "Back":
                view.viewBack()
            elif view_name == "Left":
                view.viewLeft()
            elif view_name == "Bottom":
                view.viewBottom()
            elif view_name == "Dimetric":
                view.viewDimetric()
            elif view_name == "Trimetric":
                view.viewTrimetric()
            else:
                raise ValueError(f"Invalid view name: {view_name}")

            focused_selection = False
            try:
                if focus_object:
                    doc = FreeCAD.ActiveDocument
                    obj = doc.getObject(focus_object) if doc else None
                    if obj:
                        FreeCADGui.Selection.clearSelection()
                        FreeCADGui.Selection.addSelection(obj)
                        FreeCADGui.SendMsgToActiveView("ViewSelection")
                        focused_selection = True
                        _flush_gui_events()
                    else:
                        view.fitAll()
                else:
                    view.fitAll()

                _flush_gui_events()
                width, height = _resolve_screenshot_size(view, width, height)
                view.saveImage(save_path, width, height, "Current")
                return True
            finally:
                if focused_selection:
                    try:
                        FreeCADGui.Selection.clearSelection()
                        _flush_gui_events(delay_ms=0)
                    except Exception as e:
                        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                            FreeCAD.Console.PrintWarning(
                                f"MCP RPC: failed to clear selection after screenshot: {e}\n"
                            )
        except Exception as e:
            return str(e)


# --- Local thin aliases (kept here so FreeCADRPC.health_check can call them
# without a circular import) ---



# --- Lifecycle: start / stop the XML-RPC server -----------------------------

def start_rpc_server(port=9875):
    with _rpc_lock:
        global rpc_server_thread, rpc_server_instance

        if rpc_server_instance:
            return "RPC Server already running."

        settings = load_settings()
        remote_enabled = settings.get("remote_enabled", False)
        allowed_ips = settings.get("allowed_ips", "127.0.0.1")

        host = "0.0.0.0" if remote_enabled else "localhost"

        # T1.5 — refuse to bind on a non-loopback address without both TLS
        # and a bearer-token configured. Without these two, the RPC server
        # (which exposes ``execute_code`` = arbitrary Python in the
        # FreeCAD process) is reachable by anyone on the network. Loopback
        # binds are still allowed without TLS (e.g. for local dev).
        tls_cert = os.environ.get("FREECAD_MCP_TLS_CERT")
        tls_key = os.environ.get("FREECAD_MCP_TLS_KEY")
        if host != "localhost":
            allowed, missing = can_start_remote_server(host, os.environ)
            if not allowed:
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintError(
                        "MCP RPC: refusing to start with remote_enabled=True. "
                        f"Missing required env vars: {', '.join(missing)}. "
                        "Set FREECAD_MCP_TLS_CERT, FREECAD_MCP_TLS_KEY, and "
                        "FREECAD_MCP_AUTH_TOKEN before enabling remote access. "
                        "See SECURITY.md for the threat model.\n"
                    )
                return format_refusal_message(missing)

        # Build the server + handler under a try/except so a failure
        # between bind() and Thread.start() cannot leave the module in
        # a zombie state (rpc_server_instance pointing at a non-running
        # server that the next caller would treat as "already running").
        try:
            server = FilteredXMLRPCServer(
                (host, port),
                allowed_ips_str=allowed_ips,
                tls_cert=tls_cert,
                tls_key=tls_key,
                allow_none=True,
                logRequests=False,
            )
            server.register_instance(FreeCADRPC())
        except Exception as exc:
            if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                FreeCAD.Console.PrintError(
                    f"MCP RPC: failed to construct server on {host}:{port}: "
                    f"{type(exc).__name__}: {exc}\n"
                )
            return f"MCP RPC: failed to construct server: {exc}"

        rpc_server_instance = server

        def server_loop():
            if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                FreeCAD.Console.PrintMessage(f"RPC Server started at {host}:{port}\n")
                if remote_enabled:
                    FreeCAD.Console.PrintMessage(f"Remote connections enabled. Allowed IPs: {allowed_ips}\n")
            rpc_server_instance.serve_forever()

        try:
            rpc_server_thread = threading.Thread(target=server_loop, daemon=True)
            rpc_server_thread.start()
        except Exception as exc:
            # Thread spawn failed — release the half-built server so the
            # user can retry without "already running" blocking them.
            with contextlib.suppress(Exception):
                server.server_close()
            rpc_server_instance = None
            rpc_server_thread = None
            if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                FreeCAD.Console.PrintError(
                    f"MCP RPC: failed to spawn server thread: {type(exc).__name__}: {exc}\n"
                )
            return f"MCP RPC: failed to spawn server thread: {exc}"

        QtCore.QTimer.singleShot(500, process_gui_tasks)

        msg = f"RPC Server started at {host}:{port}."
        if remote_enabled:
            msg += f" Allowed IPs: {allowed_ips}"
        return msg


def stop_rpc_server():
    with _rpc_lock:
        global rpc_server_instance, rpc_server_thread

        if rpc_server_instance:
            try:
                rpc_request_queue.put(_DISPATCH_SHUTDOWN)
            except Exception as e:
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintWarning(f"MCP RPC: failed to post shutdown sentinel: {e}\n")
            rpc_server_instance.shutdown()
            rpc_server_thread.join()
            try:
                rpc_server_instance.server_close()
            except Exception as e:
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintWarning(f"MCP RPC: server_close raised {type(e).__name__}: {e}\n")
            rpc_server_instance = None
            rpc_server_thread = None
            if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                FreeCAD.Console.PrintMessage("RPC Server stopped.\n")
            return "RPC Server stopped."

        return "RPC Server was not running."


# --- Status helpers (used by the toolbar toggle + dock panel) ---------------


def is_rpc_server_running() -> bool:
    """Return ``True`` when the XML-RPC server thread is alive.

    Cheap and side-effect free. Used by the dock panel / toolbar
    toggle to colour the indicator and decide whether the next click
    should stop or (re)start the server.
    """
    return rpc_server_instance is not None


def get_rpc_status() -> dict[str, Any]:
    """Return a snapshot of the RPC server state for the UI.

    Keys: ``running`` (bool), ``host`` (str|None), ``port`` (int|None),
    ``remote_enabled`` (bool), ``allowed_ips`` (str|None),
    ``auto_start`` (bool), ``pid`` (int|None).
    """
    settings = load_settings()
    running = is_rpc_server_running()
    host: str | None = None
    port: int | None = None
    if running and rpc_server_instance is not None:
        try:
            server_addr = getattr(rpc_server_instance, "server_address", None)
            if server_addr:
                host, port = server_addr[0], server_addr[1]
        except Exception:
            host, port = None, None
    return {
        "running": running,
        "host": host,
        "port": port,
        "remote_enabled": bool(settings.get("remote_enabled", False)),
        "allowed_ips": settings.get("allowed_ips", "127.0.0.1"),
        "auto_start": bool(settings.get("auto_start_rpc", False)),
        "pid": rpc_server_thread.ident if rpc_server_thread else None,
    }


def toggle_rpc_server(port: int = 9875) -> str:
    """Start the server if it is stopped, otherwise stop it.

    Convenience wrapper used by the single-button toolbar toggle and
    the dock panel — keeps the decision logic in one place so the UI
    stays a thin view.
    """
    if is_rpc_server_running():
        return stop_rpc_server()
    return start_rpc_server(port=port)


# Re-import here to break the cycle: _commands references this module
# for ``start_rpc_server`` / ``stop_rpc_server``, but the toolbar
# ``Toggle_RPC_Server`` command is registered by ``InitGui.py`` via
# :func:`register_commands` (so test runners that import this module
# without FreeCADGui do not blow up at import time).
from ._commands import (  # noqa: E402  (intentional late import)
    ConfigureAllowedIPsCommand,
    ToggleAutoStartCommand,
    ToggleRemoteConnectionsCommand,
    ToggleRPCServerCommand,
    _sync_toggle_states,
)


def register_commands() -> None:
    """Register every FreeCAD command this addon defines.

    Called by ``InitGui.py`` after the workbench is Initialized, so the
    FreeCADGui namespace is guaranteed to be live. Safe to call more
    than once; ``addCommand`` overwrites by name in FreeCAD.
    """
    if FreeCADGui is None:
        return
    try:
        FreeCADGui.addCommand("Toggle_RPC_Server", ToggleRPCServerCommand())
        FreeCADGui.addCommand("Toggle_Auto_Start", ToggleAutoStartCommand())
        FreeCADGui.addCommand(
            "Toggle_Remote_Connections", ToggleRemoteConnectionsCommand()
        )
        FreeCADGui.addCommand(
            "Configure_Allowed_IPs", ConfigureAllowedIPsCommand()
        )
    except Exception as exc:  # pragma: no cover - depends on FreeCAD GUI
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintWarning(
                f"MCP RPC: register_commands() raised {type(exc).__name__}: {exc}\n"
            )


def _auto_start_mcp():
    """Bring up the RPC server at boot when ``auto_start_rpc`` is set.

    Bounded retry with exponential backoff (1s, 2s, 4s, max 5 attempts)
    so transient races with the user clicking the toolbar don't leave
    the server silently dead until next FreeCAD restart.
    """
    try:
        settings = load_settings()
        if not settings.get("auto_start_rpc", False):
            return

        for attempt in range(1, 6):
            try:
                msg = start_rpc_server()
            except Exception as e:
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintWarning(
                        f"[MCP] Auto-start attempt {attempt} failed: "
                        f"{type(e).__name__}: {e}\n"
                    )
                if attempt < 5 and QtCore is not None:
                    QtCore.QTimer.singleShot(2 ** (attempt - 1) * 1000, _auto_start_mcp)
                return

            if "started" in msg.lower() or "already running" in msg.lower():
                if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                    FreeCAD.Console.PrintMessage(f"[MCP] Auto-start: {msg}\n")
                return
            # Server refused to start (TLS/auth missing, port busy, …)
            if FreeCAD is not None and hasattr(FreeCAD, "Console"):
                FreeCAD.Console.PrintWarning(
                    f"[MCP] Auto-start attempt {attempt} refused: {msg}\n"
                )
            if attempt < 5 and QtCore is not None:
                QtCore.QTimer.singleShot(2 ** (attempt - 1) * 1000, _auto_start_mcp)
            return
    except Exception as e:
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintWarning(f"[MCP] Auto-start failed: {e}\n")


QtCore.QTimer.singleShot(0, _auto_start_mcp)
QtCore.QTimer.singleShot(2000, _sync_toggle_states)
