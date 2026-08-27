"""FreeCAD toolbar / menu commands for the MCP addon.

Extracted from ``rpc_server`` so the command classes are
unit-testable without booting FreeCAD. Each command class implements
the FreeCAD ``Gui::Command`` protocol (GetResources / Activated /
IsActive) and delegates the actual work to :mod:`_dispatch` and
:mod:`_settings`.

The five commands are:

* :class:`StartRPCServerCommand` / :class:`StopRPCServerCommand` \u2014
  start/stop the XML-RPC server.
* :class:`ToggleRemoteConnectionsCommand` \u2014 opt-in to a non-loopback
  bind. Refused unless TLS + auth env vars are configured.
* :class:`ConfigureAllowedIPsCommand` \u2014 dialog to edit the IP allowlist.
* :class:`ToggleAutoStartCommand` \u2014 persist ``auto_start_rpc`` so the
  server comes up on the next FreeCAD launch.
"""
from __future__ import annotations

from ._dispatch import rpc_server_instance
from ._settings import load_settings, save_settings


class StartRPCServerCommand:
    def GetResources(self):
        return {"MenuText": "Start RPC Server", "ToolTip": "Start RPC Server"}

    def Activated(self):
        # Local import to break the cycle: _commands is imported by
        # rpc_server, which exposes start_rpc_server.
        from . import rpc_server

        msg = rpc_server.start_rpc_server()
        if hasattr(__import__("FreeCAD"), "Console"):
            __import__("FreeCAD").Console.PrintMessage(msg + "\n")

    def IsActive(self):
        return True


class StopRPCServerCommand:
    def GetResources(self):
        return {"MenuText": "Stop RPC Server", "ToolTip": "Stop RPC Server"}

    def Activated(self):
        from . import rpc_server

        msg = rpc_server.stop_rpc_server()
        if hasattr(__import__("FreeCAD"), "Console"):
            __import__("FreeCAD").Console.PrintMessage(msg + "\n")

    def IsActive(self):
        return True


class ToggleRemoteConnectionsCommand:
    def GetResources(self):
        return {
            "MenuText": "Remote Connections",
            "ToolTip": "Enable or disable remote connections for the RPC server.",
            "Checkable": True,
        }

    def Activated(self, checked=0):
        # T1.5 — refuse to enable remote access unless TLS + auth are
        # already configured. Showing a dialog is friendlier than
        # silently failing at start time.
        from ._security_gate import can_start_remote_server

        FreeCAD = __import__("FreeCAD", fromlist=["Console"])
        try:
            from PySide import QtWidgets
        except Exception:
            QtWidgets = None  # type: ignore[assignment]

        if checked:
            allowed, missing = can_start_remote_server("0.0.0.0", __import__("os").environ)
            if not allowed:
                if QtWidgets is not None:
                    QtWidgets.QMessageBox.critical(
                        None,
                        "Remote Connections Disabled",
                        "Remote connections require TLS and a bearer token, otherwise "
                        "anyone on the network can call execute_code (= arbitrary "
                        "Python in the FreeCAD process).\n\n"
                        f"Missing environment variables: {', '.join(missing)}.\n\n"
                        "Set them in your environment (and the FreeCAD process "
                        "environment) and try again. See SECURITY.md for details.",
                    )
                FreeCAD.Console.PrintError(
                    f"MCP RPC: refused to enable remote connections; missing "
                    f"env vars: {', '.join(missing)}.\n"
                )
                return
        settings = load_settings()
        settings["remote_enabled"] = bool(checked)
        save_settings(settings)

        if settings["remote_enabled"]:
            allowed_ips = settings.get("allowed_ips", "127.0.0.1")
            FreeCAD.Console.PrintMessage(
                f"Remote connections enabled. Allowed IPs: {allowed_ips}\n"
            )
        else:
            FreeCAD.Console.PrintMessage("Remote connections disabled.\n")

        if rpc_server_instance:
            FreeCAD.Console.PrintMessage(
                "Restart the RPC server for changes to take effect.\n"
            )

    def IsActive(self):
        return True


class ConfigureAllowedIPsCommand:
    def GetResources(self):
        return {
            "MenuText": "Configure Allowed IPs",
            "ToolTip": "Set which IP addresses or subnets are allowed to connect to the RPC server.",
        }

    def Activated(self):
        from . import rpc_server  # for validate_allowed_ips

        try:
            from PySide import QtWidgets
        except Exception:
            QtWidgets = None  # type: ignore[assignment]

        FreeCAD = __import__("FreeCAD", fromlist=["Console"])
        settings = load_settings()
        current_ips = settings.get("allowed_ips", "127.0.0.1")

        if QtWidgets is None:
            FreeCAD.Console.PrintError(
                "MCP RPC: PySide not available; cannot show the Allowed IPs dialog.\n"
            )
            return

        text, ok = QtWidgets.QInputDialog.getText(
            None,
            "Allowed IP Addresses",
            "Enter allowed IP addresses or subnets (comma-separated):\n"
            "Examples: 127.0.0.1, 192.168.1.0/24, 10.0.0.5",
            QtWidgets.QLineEdit.Normal,
            current_ips,
        )
        if ok and text.strip():
            valid, errors = rpc_server.validate_allowed_ips(text.strip())
            if errors:
                QtWidgets.QMessageBox.warning(
                    None,
                    "Invalid IP Configuration",
                    "The following errors were found:\n\n"
                    + "\n".join(f"\u2022 {e}" for e in errors)
                    + ("\n\nOnly valid entries will be saved."
                       if valid else "\n\nNo valid entries found. Settings not changed."),
                )
            if not valid:
                FreeCAD.Console.PrintWarning("Allowed IPs not changed \u2014 no valid entries.\n")
                return
            normalised = ", ".join(valid)
            settings["allowed_ips"] = normalised
            save_settings(settings)
            FreeCAD.Console.PrintMessage(
                f"Allowed IPs updated to: {normalised}\n"
            )
            if rpc_server_instance:
                FreeCAD.Console.PrintMessage(
                    "Restart the RPC server for changes to take effect.\n"
                )
        else:
            FreeCAD.Console.PrintMessage("Allowed IPs not changed.\n")

    def IsActive(self):
        return True


class ToggleAutoStartCommand:
    def GetResources(self):
        return {
            "MenuText": "Auto-Start Server",
            "ToolTip": "Automatically start the RPC server when FreeCAD launches.",
            "Checkable": True,
        }

    def Activated(self, checked=0):
        # T1.5 — refuse to enable remote access unless TLS + auth are
        # already configured. Showing a dialog is friendlier than
        # silently failing at start time.
        from ._security_gate import can_start_remote_server

        FreeCAD = __import__("FreeCAD", fromlist=["Console"])
        try:
            from PySide import QtWidgets
        except Exception:
            QtWidgets = None  # type: ignore[assignment]

        if checked:
            allowed, missing = can_start_remote_server("0.0.0.0", __import__("os").environ)
            if not allowed:
                if QtWidgets is not None:
                    QtWidgets.QMessageBox.critical(
                        None,
                        "Remote Connections Disabled",
                        "Remote connections require TLS and a bearer token, otherwise "
                        "anyone on the network can call execute_code (= arbitrary "
                        "Python in the FreeCAD process).\n\n"
                        f"Missing environment variables: {', '.join(missing)}.\n\n"
                        "Set them in your environment (and the FreeCAD process "
                        "environment) and try again. See SECURITY.md for details.",
                    )
                FreeCAD.Console.PrintError(
                    f"MCP RPC: refused to enable remote connections; missing "
                    f"env vars: {', '.join(missing)}.\n"
                )
                return
        settings = load_settings()
        settings["auto_start_rpc"] = bool(checked)
        save_settings(settings)

        if settings["auto_start_rpc"]:
            FreeCAD.Console.PrintMessage(
                "MCP RPC server will start automatically on next FreeCAD launch.\n"
            )
        else:
            FreeCAD.Console.PrintMessage(
                "MCP RPC server auto-start disabled.\n"
            )

    def IsActive(self):
        return True


def _sync_toggle_states():
    """Sync checkable menu items with saved settings on startup.

    FreeCAD may construct the menu before the settings file has been
    read; we retry for a few seconds before giving up. This is the
    same pattern as the original ``rpc_server`` module, extracted here
    so the menu code lives in one place.
    """
    try:
        import FreeCADGui
    except Exception:
        return
    try:
        from PySide import QtCore, QtWidgets
    except Exception:
        return

    try:
        settings = load_settings()
        main_window = FreeCADGui.getMainWindow()
        toggle_map = {
            "Remote Connections": settings.get("remote_enabled", False),
            "Auto-Start Server": settings.get("auto_start_rpc", False),
        }
        found = 0
        for action in main_window.findChildren(QtWidgets.QAction):
            if action.text() in toggle_map:
                action.setChecked(toggle_map[action.text()])
                found += 1
                if found == len(toggle_map):
                    return
    except Exception:
        pass
    # Retry if menu not ready yet
    QtCore.QTimer.singleShot(2000, _sync_toggle_states)


__all__ = [
    "StartRPCServerCommand",
    "StopRPCServerCommand",
    "ToggleRemoteConnectionsCommand",
    "ConfigureAllowedIPsCommand",
    "ToggleAutoStartCommand",
    "ToggleRPCServerCommand",
    "_sync_toggle_states",
]


class ToggleRPCServerCommand:
    """Single toolbar button that starts OR stops the RPC server.

    Replaces the two separate Start/Stop buttons in the workbench
    toolbar. The button text and icon colour are recomputed on
    ``Activated`` and on a 1 s Qt timer so the UI stays in sync with
    the actual server state even when the server is stopped from
    outside (auto-start failed, crash, etc.).
    """

    POLL_INTERVAL_MS = 1000

    def GetResources(self):
        return {
            "MenuText": "Toggle RPC Server",
            "ToolTip": "Start or stop the FreeCAD MCP RPC server.",
        }

    def _refresh_label(self):
        try:
            from . import rpc_server

            running = rpc_server.is_rpc_server_running()
        except Exception:
            running = False
        try:
            import FreeCADGui
            from PySide import QtCore, QtWidgets

            action = FreeCADGui.getMainWindow().findChild(
                QtWidgets.QAction, "ToggleRPCServer"
            )
            if action is not None:
                action.setText(
                    "Stop RPC Server" if running else "Start RPC Server"
                )
        except Exception:
            pass

    def Activated(self):
        from . import rpc_server

        msg = rpc_server.toggle_rpc_server()
        try:
            __import__("FreeCAD").Console.PrintMessage(f"[MCP] {msg}\n")
        except Exception:
            pass
        self._refresh_label()
        # Notify the dock panel if it is open.
        try:
            from ._panel import notify_status_change

            notify_status_change()
        except Exception:
            pass

    def IsActive(self):
        return True

    def start_refresh_timer(self):
        """Called once after the workbench is Initialized."""
        try:
            from PySide import QtCore
        except Exception:
            return
        QtCore.QTimer.singleShot(self.POLL_INTERVAL_MS, self._tick)

    def _tick(self):
        self._refresh_label()
        try:
            from PySide import QtCore
        except Exception:
            return
        QtCore.QTimer.singleShot(self.POLL_INTERVAL_MS, self._tick)
