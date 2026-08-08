class FreeCADMCPAddonWorkbench(Workbench):
    MenuText = "MCP Addon"
    ToolTip = "Addon for MCP Communication"

    def Initialize(self):

        commands = [
            "Start_RPC_Server",
            "Stop_RPC_Server",
            "Toggle_Auto_Start",
            "Toggle_Remote_Connections",
            "Configure_Allowed_IPs",
        ]
        self.appendToolbar("FreeCAD MCP", commands)
        self.appendMenu("FreeCAD MCP", commands)

    def Activated(self):
        pass

    def Deactivated(self):
        pass

    def ContextMenu(self, recipient):
        pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"


Gui.addWorkbench(FreeCADMCPAddonWorkbench())


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


from PySide import QtCore

QtCore.QTimer.singleShot(0, _auto_start_mcp)
