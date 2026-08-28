"""In-process runner for the Ollama → MCP bridge.

Used by the dock panel when running inside a sandbox (Flatpak, Snap,
AppImage) where the host ``python3`` isn't reachable and the host
``mcp-freecad`` console script can't be spawned.

We reuse the FreeCAD-bundled Python interpreter (which already has
``FreeCAD`` importable) and run the bridge loop synchronously in the
caller's thread. The bridge imports ``mcp``, ``freecad_mcp`` and the
MCP server modules — these must be on ``sys.path`` in the sandbox.

Note: we deliberately do NOT spin up a QThread. The dock panel's
``_on_send`` is called from a Qt slot, and starting a blocking
async.run on the same thread is OK for short prompts; for long
ones the GUI will freeze until the model returns. A proper
worker-threaded implementation would need ``QApplication.exec()``
to schedule callbacks, which we don't have when ``_on_send`` is
called from a one-off macro.
"""

from __future__ import annotations

import os
import sys
import traceback


def _ensure_src_on_path() -> str | None:
    """Best-effort: prepend known package roots to ``sys.path``.

    Returns the absolute first-added directory if any, ``None`` otherwise.

    Order:
    1. ``~/.local/lib/python3.13/site-packages`` (Flatpak user-install
       location for ``pip install --user``-style drops).
    2. ``~/src`` (dev checkout).
    3. Sibling of the addon directory (legacy dev layout).
    """
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".local", "lib", "python3.13", "site-packages"),
        os.path.join(home, "src"),
        os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "src")),
        os.path.expanduser("~/.local/share/uv/tools/mcp-freecad/lib/python/site-packages"),
    ]
    added = None
    for c in candidates:
        if os.path.isdir(c) and c not in sys.path:
            sys.path.insert(0, c)
            if added is None:
                added = c
    return added


def _run_bridge_sync(prompt: str, model: str, on_log) -> tuple[str, str | None]:
    """Execute the bridge loop synchronously and return ``(answer, error)``."""
    on_log(f"[bridge] thread started (pid={os.getpid()})\n")
    added = _ensure_src_on_path()
    on_log(f"[bridge] sys.path additions: {added}\n")
    try:
        import asyncio  # noqa: E402

        # Patch ``mcp.client.stdio._create_platform_compatible_process``
        # to use a real stderr stream. Inside the FreeCAD GUI,
        # ``sys.stderr`` is a QTextEdit adapter with no ``fileno()``,
        # so the default ``stderr=errlog`` (where errlog=sys.stderr)
        # crashes anyio's subprocess spawn.
        import mcp.client.stdio as _mcp_stdio

        from freecad_mcp.ollama_bridge import OllamaBridgeConfig, OllamaMCPBridge  # noqa: E402
        _orig_cpcp = _mcp_stdio._create_platform_compatible_process

        # Capture stderr so we can show the subprocess's crash output
        # if it dies.
        sub_err_path = os.path.expanduser(
            "~/.var/app/org.freecad.FreeCAD/cache/freecad-mcp/bridge_subproc.err.log"
        )
        os.makedirs(os.path.dirname(sub_err_path), exist_ok=True)
        sub_err = open(sub_err_path, "wb")  # noqa: SIM115

        async def _patched_cpcp(command, args, env=None, errlog=None, cwd=None):
            # Forward PYTHONPATH so the subprocess can import mcp +
            # freecad_mcp from the user-site we set up above.
            import os as _os

            merged_env = dict(_os.environ)
            if env:
                merged_env.update(env)
            src_site = "/home/yuri/.local/lib/python3.13/site-packages"
            existing_pp = merged_env.get("PYTHONPATH", "")
            if src_site not in existing_pp.split(_os.pathsep):
                merged_env["PYTHONPATH"] = (
                    src_site + _os.pathsep + existing_pp
                if existing_pp
                else src_site
            )
            return await _orig_cpcp(
                command,
                args,
                env=merged_env,
                errlog=sub_err,
                cwd=cwd,
            )

        _mcp_stdio._create_platform_compatible_process = _patched_cpcp
        on_log("[bridge] imports OK (with stdio patch)\n")
    except Exception as e:
        return "", (
            "Dependências faltando no Python do sandbox: "
            f"{e!r}. Rode no host (fora do Flatpak) ou instale "
            "`mcp` e `freecad_mcp` no Python embutido do FreeCAD."
        )

    cfg = OllamaBridgeConfig(model=model)
    # Inside a Flatpak sandbox the ``mcp-freecad`` console script is
    # not on PATH; we need to spawn the embedded Python that already
    # has ``FreeCAD`` importable. ``sys.executable`` inside the FreeCAD
    # GUI is the Flatpak launcher (``/app/bin/FreeCAD``), which we can
    # pass a Python script that re-execs the embedded interpreter in
    # module mode. Simpler: detect ``/usr/bin/python3.13`` (the
    # real interpreter inside the sandbox) and use it to run
    # ``-m freecad_mcp.server`` — that subprocess also runs inside the
    # sandbox, so it has FreeCAD importable.
    sandbox_py = "/usr/bin/python3.13"
    if os.path.isfile(sandbox_py) and os.access(sandbox_py, os.X_OK):
        # Run as ``-m freecad_mcp`` (NOT ``-m freecad_mcp.server``) so that
        # ``freecad_mcp/__main__.py`` is executed and ``main()`` is invoked.
        # ``-m freecad_mcp.server`` only imports the module and exits,
        # leaving the stdio transport with no server listening.
        cfg.command = (sandbox_py, "-m", "freecad_mcp", "--only-text-feedback")
        on_log(f"[bridge] sandbox python: {sandbox_py}\n")
    on_log(f"[bridge] usando OLLAMA_HOST={cfg.ollama_url}\n")
    on_log(f"[bridge] modelo={model}\n")
    try:
        answer = asyncio.run(OllamaMCPBridge(cfg).ask(prompt, model=model))
        return answer, None
    except Exception as e:
        return "", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"


def run_bridge_in_thread(prompt: str, model: str, on_log, on_done):
    """Synchronously run the bridge and invoke ``on_done`` immediately.

    Returns a tiny stand-in object exposing ``state()`` so the dock
    panel's ``_on_send`` can treat it like a ``QProcess``. We do the
    work on the calling thread to avoid the cross-thread / event-loop
    complexity of ``QThread`` (which requires a running
    ``QApplication.exec()`` to deliver signals).
    """
    answer, err = _run_bridge_sync(prompt, model, on_log)
    on_done(answer, err)

    class _Handle:
        @staticmethod
        def state():
            return 0  # QProcess.NotRunning — already finished

    return _Handle()
