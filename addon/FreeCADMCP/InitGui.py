class FreeCADMCPAddonWorkbench(Workbench):
    MenuText = "MCP Addon"
    ToolTip = "Addon for MCP Communication"

    def Initialize(self):
        # Single toggle replaces the legacy Start / Stop pair.
        commands = [
            "Toggle_RPC_Server",
            "Toggle_Auto_Start",
            "Toggle_Remote_Connections",
            "Configure_Allowed_IPs",
        ]
        self.appendToolbar("FreeCAD MCP", commands)
        self.appendMenu("FreeCAD MCP", commands)

        # Keep the toolbar/menu label in sync with the actual state.
        try:
            from rpc_server._commands import ToggleRPCServerCommand

            self._toggle_cmd = ToggleRPCServerCommand()
            self._toggle_cmd.start_refresh_timer()
        except Exception as e:
            FreeCAD.Console.PrintWarning(f"[MCP] refresh timer setup failed: {e}\n")

    def Activated(self):
        # Open the MCP dock so the user immediately sees status + prompt.
        from rpc_server._panel import show_panel

        try:
            show_panel()
        except Exception as e:
            FreeCAD.Console.PrintWarning(f"[MCP] show_panel failed: {e}\n")

    def Deactivated(self):
        pass

    def ContextMenu(self, recipient):
        pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"


FreeCADGui.addWorkbench(FreeCADMCPAddonWorkbench())


# Back-compat: keep the original Start/Stop commands registered so external
# scripts that look them up still work, but no longer surface them in the
# toolbar (the Toggle button does both jobs).
try:
    from rpc_server._commands import (
        StartRPCServerCommand,
        StopRPCServerCommand,
    )

    FreeCADGui.addCommand("Start_RPC_Server", StartRPCServerCommand())
    FreeCADGui.addCommand("Stop_RPC_Server", StopRPCServerCommand())
except Exception:
    pass


def _auto_start_mcp():
    try:
        from rpc_server import rpc_server

        settings = rpc_server.load_settings()
        if not settings.get("auto_start_rpc", False):
            return

        msg = rpc_server.start_rpc_server()
        FreeCAD.Console.PrintMessage(f"[MCP] Auto-start: {msg}\n")
    except Exception as e:
        FreeCAD.Console.PrintWarning(f"[MCP] Auto-start failed: {e}\n")


if QtCore is not None:
    QtCore.QTimer.singleShot(0, _auto_start_mcp)
