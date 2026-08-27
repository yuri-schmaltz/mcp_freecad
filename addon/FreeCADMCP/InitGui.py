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

        # Register every command via the central helper so we have a
        # single source of truth and no surprise ``addCommand`` calls
        # during module import (which would break test runners).
        try:
            from rpc_server import rpc_server as _rpc_server

            _rpc_server.register_commands()
        except Exception as e:
            FreeCAD.Console.PrintWarning(f"[MCP] register_commands failed: {e}\n")

        # Back-compat: keep Start/Stop registered too (no toolbar slots,
        # but external scripts may still look them up).
        try:
            from rpc_server._commands import (
                StartRPCServerCommand,
                StopRPCServerCommand,
            )

            FreeCADGui.addCommand("Start_RPC_Server", StartRPCServerCommand())
            FreeCADGui.addCommand("Stop_RPC_Server", StopRPCServerCommand())
        except Exception as e:
            FreeCAD.Console.PrintWarning(
                f"[MCP] legacy Start/Stop registration failed: {e}\n"
            )

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


# Auto-start of the RPC server is now driven by ``rpc_server.py`` itself
# via :func:`_auto_start_mcp` + ``register_commands`` so there is no
# duplicate scheduling when this module is reloaded by the FreeCAD
# plugin manager.
