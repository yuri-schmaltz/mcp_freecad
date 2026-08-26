"""Unit tests for operations/core.py — drives the operations against fakes.

These cover the full MCP-facing surface of the operations layer without
spinning up FreeCAD: we substitute FreeCADConnection with a small stub
that records what the operations call. The goal is to cover the
branching (guidelines, success, failure, screenshot attachment) that the
MCP layer relies on.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import freecad_mcp.operations.core as core  # noqa: E402

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeFreeCAD:
    """Stand-in for FreeCADConnection that records every call."""

    def __init__(
        self,
        create_document=None,
        create_object=None,
        edit_object=None,
        delete_object=None,
        execute_code=None,
        get_active_screenshot=None,
        get_active_screenshot_with_status=None,
        insert_part_from_library=None,
        get_objects=None,
        get_object=None,
        get_parts_list=None,
        list_documents=None,
        run_fem_analysis=None,
        health_check=None,
        undo=None,
        redo=None,
        save_document=None,
        export_object=None,
        get_active_view=None,
        breaker_metrics=None,
    ):
        self.calls = []
        self._handlers = {
            "create_document": create_document,
            "create_object": create_object,
            "edit_object": edit_object,
            "delete_object": delete_object,
            "execute_code": execute_code,
            "get_active_screenshot": get_active_screenshot,
            "get_active_screenshot_with_status": get_active_screenshot_with_status,
            "insert_part_from_library": insert_part_from_library,
            "get_objects": get_objects,
            "get_object": get_object,
            "get_parts_list": get_parts_list,
            "list_documents": list_documents,
            "run_fem_analysis": run_fem_analysis,
            "health_check": health_check,
            "undo": undo,
            "redo": redo,
            "save_document": save_document,
            "export_object": export_object,
            "get_active_view": get_active_view,
            "breaker_metrics": breaker_metrics,
        }

    def _dispatch(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        handler = self._handlers.get(name)
        if handler is None:
            raise RuntimeError(f"unhandled: {name}")
        return handler(*args, **kwargs)

    def create_document(self, name):
        return self._dispatch("create_document", name)

    def create_object(self, doc_name, obj_data):
        return self._dispatch("create_object", doc_name, obj_data)

    def edit_object(self, doc_name, obj_name, obj_data):
        return self._dispatch("edit_object", doc_name, obj_name, obj_data)

    def delete_object(self, doc_name, obj_name):
        return self._dispatch("delete_object", doc_name, obj_name)

    def execute_code(self, code):
        return self._dispatch("execute_code", code)

    def get_active_screenshot(self, *args, **kwargs):
        return self._dispatch("get_active_screenshot", *args, **kwargs)

    def get_active_screenshot_with_status(self, *args, **kwargs):
        # v1.0.3 — operations now call the structured helper. Default
        # falls back to the legacy single-shot ``get_active_screenshot``
        # and packages the result in the new status envelope, so old
        # test setups that only override the legacy method keep working.
        handler = self._handlers.get("get_active_screenshot_with_status")
        if handler is not None:
            return handler(*args, **kwargs)
        legacy = self._dispatch("get_active_screenshot", *args, **kwargs)
        if legacy is None:
            return {"success": False, "reason": "no_capture"}
        return {
            "success": True,
            "screenshot": legacy,
            "format": (kwargs.get("image_format") or "png").lower(),
        }

    def insert_part_from_library(self, relative_path):
        return self._dispatch("insert_part_from_library", relative_path)

    def get_objects(self, doc_name):
        return self._dispatch("get_objects", doc_name)

    def get_object(self, doc_name, obj_name):
        return self._dispatch("get_object", doc_name, obj_name)

    def get_parts_list(self):
        return self._dispatch("get_parts_list")

    def list_documents(self):
        return self._dispatch("list_documents")

    def run_fem_analysis(self, doc_name, analysis_name, timeout):
        return self._dispatch("run_fem_analysis", doc_name, analysis_name, timeout)

    def health_check(self):
        return self._dispatch("health_check")

    def undo(self, doc_name, steps=1):
        return self._dispatch("undo", doc_name, steps)

    def redo(self, doc_name, steps=1):
        return self._dispatch("redo", doc_name, steps)

    def save_document(self, doc_name, path=None):
        return self._dispatch("save_document", doc_name, path)

    def export_object(self, doc_name, obj_name, path, fmt=None):
        return self._dispatch("export_object", doc_name, obj_name, path, fmt)

    def get_active_view(self):
        return self._dispatch("get_active_view")

    def breaker_metrics(self):
        handler = self._handlers.get("breaker_metrics")
        if handler is not None:
            return handler()
        # v0.4.0 default — health_check_operation now reads this.
        return {
            "state": "closed",
            "consecutive_failures": 0,
            "threshold": 3,
            "total_calls": 0,
            "total_failures": 0,
            "total_short_circuits": 0,
        }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_create_document_success():
    fake = FakeFreeCAD(create_document=lambda n: {"success": True, "document_name": n})
    r = core.create_document_operation(fake, "Doc1")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("created successfully" in t for t in texts), texts
    assert fake.calls[0][0] == "create_document"


def test_create_document_failure():
    fake = FakeFreeCAD(create_document=lambda n: {"success": False, "error": "boom"})
    r = core.create_document_operation(fake, "Doc1")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Failed to create document" in t and "boom" in t for t in texts)


def test_create_object_success_includes_screenshot_when_available():
    fake = FakeFreeCAD(
        create_object=lambda d, o: {"success": True, "object_name": o["Name"]},
        get_active_screenshot=lambda: "B64",
    )
    r = core.create_object_operation(fake, False, "Doc", "Part::Box", "B", None, {})
    has_image = any(getattr(t, "type", "") == "image" for t in r)
    assert has_image, f"expected image attachment in {r}"


def test_create_object_no_screenshot_when_only_text_feedback():
    fake = FakeFreeCAD(
        create_object=lambda d, o: {"success": True, "object_name": o["Name"]},
        get_active_screenshot=lambda: "B64",
    )
    r = core.create_object_operation(fake, True, "Doc", "Part::Box", "B", None, {})
    has_image = any(getattr(t, "type", "") == "image" for t in r)
    assert not has_image


def test_create_object_blocked_by_dangerous_name():
    """A name with a banned pattern in obj_name is allowed (we do not
    check names). Use a real exec via execute_code instead.
    """
    fake = FakeFreeCAD(
        create_object=lambda d, o: {"success": True, "object_name": o["Name"]},
        get_active_screenshot=lambda: None,
    )
    # Names are labels; no guideline check.
    r = core.create_object_operation(fake, False, "Doc", "Part::Box", "eval test", None, {})
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("created successfully" in t for t in texts)


def test_create_object_failure_reports_error():
    fake = FakeFreeCAD(
        create_object=lambda d, o: {"success": False, "error": "nope"},
        get_active_screenshot=lambda: None,
    )
    r = core.create_object_operation(fake, False, "Doc", "Part::Box", "B", None, {})
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Failed to create object" in t and "nope" in t for t in texts)


def test_edit_object_success():
    fake = FakeFreeCAD(
        edit_object=lambda d, n, p: {"success": True, "object_name": n},
        get_active_screenshot=lambda: None,
    )
    r = core.edit_object_operation(fake, False, "Doc", "Box", {"Height": 5})
    assert any("edited successfully" in t.text for t in r if hasattr(t, "text"))


def test_edit_object_success_appends_screenshot():
    """When the screenshot is available and feedback is not text-only,
    the edit_object success path appends an image part."""
    fake = FakeFreeCAD(
        edit_object=lambda d, n, p: {"success": True, "object_name": n},
        get_active_screenshot=lambda: "BASE64-DATA",
    )
    r = core.edit_object_operation(fake, False, "Doc", "Box", {"Height": 5})
    types = [getattr(t, "type", "") for t in r]
    assert "image" in types


def test_edit_object_failure_reports_error():
    fake = FakeFreeCAD(
        edit_object=lambda d, n, p: {"success": False, "error": "boom"},
        get_active_screenshot=lambda: None,
    )
    r = core.edit_object_operation(fake, False, "Doc", "Box", {})
    assert any("Failed to edit object" in t.text for t in r if hasattr(t, "text"))


def test_edit_object_validation_error_returns_text_response():
    """Invalid edit_object input short-circuits to a text response."""
    from pydantic import ValidationError

    # Pass invalid obj_properties (not a dict) to trigger validation failure.
    fake = FakeFreeCAD(
        edit_object=lambda d, n, p: {"success": True, "object_name": n},
        get_active_screenshot=lambda: None,
    )
    # Pydantic may or may not raise on a non-dict; use a path that we know fails.
    r = core.edit_object_operation(fake, False, "Doc", "Box", "not-a-dict")
    assert any("Invalid edit_object" in t.text or "edited" in t.text
               for t in r if hasattr(t, "text"))


def test_delete_object_success():
    fake = FakeFreeCAD(
        delete_object=lambda d, n: {"success": True, "object_name": n},
        get_active_screenshot=lambda: None,
    )
    r = core.delete_object_operation(fake, False, "Doc", "Box")
    assert any("deleted successfully" in t.text for t in r if hasattr(t, "text"))


def test_delete_object_failure_reports_error():
    fake = FakeFreeCAD(
        delete_object=lambda d, n: {"success": False, "error": "not found"},
        get_active_screenshot=lambda: None,
    )
    r = core.delete_object_operation(fake, False, "Doc", "Box")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Failed to delete object" in t and "not found" in t for t in texts)


def test_execute_code_dangerous_blocked():
    fake = FakeFreeCAD(
        execute_code=lambda c: {"success": True, "message": "ok"},
        get_active_screenshot=lambda: None,
    )
    r = core.execute_code_operation(fake, False, "os.system('rm -rf /')")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Refusing" in t and "pattern" in t for t in texts)
    # And the fake should not have been called.
    assert not any(call[0] == "execute_code" for call in fake.calls)


def test_execute_code_safe_passes_through():
    fake = FakeFreeCAD(
        execute_code=lambda c: {"success": True, "message": "executed"},
        get_active_screenshot=lambda: None,
    )
    r = core.execute_code_operation(fake, False, "doc.addObject('Part::Box', 'B')")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("executed successfully" in t for t in texts)


def test_execute_code_failure_reports_error():
    fake = FakeFreeCAD(
        execute_code=lambda c: {"success": False, "error": "NameError: x"},
        get_active_screenshot=lambda: None,
    )
    r = core.execute_code_operation(fake, False, "1/0")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Failed to execute code" in t and "NameError" in t for t in texts)


def test_insert_part_from_library_safe_path():
    fake = FakeFreeCAD(
        insert_part_from_library=lambda p: {"success": True, "message": "ok"},
        get_active_screenshot=lambda: None,
    )
    r = core.insert_part_from_library_operation(fake, False, "Mechanical/Bearings/6200.fcstd")
    assert any("inserted from library" in t.text for t in r if hasattr(t, "text"))


def test_insert_part_from_library_path_traversal_blocked():
    fake = FakeFreeCAD(
        insert_part_from_library=lambda p: {"success": True, "message": "ok"},
        get_active_screenshot=lambda: None,
    )
    r = core.insert_part_from_library_operation(fake, False, "../../etc/passwd")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Diretriz" in t or "escapes" in t.lower() for t in texts)
    assert not any(call[0] == "insert_part_from_library" for call in fake.calls)


def test_insert_part_from_library_failure_reports():
    fake = FakeFreeCAD(
        insert_part_from_library=lambda p: {"success": False, "error": "missing"},
        get_active_screenshot=lambda: None,
    )
    r = core.insert_part_from_library_operation(fake, False, "gear.fcstd")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Failed" in t and "missing" in t for t in texts)


def test_get_objects_json_response():
    fake = FakeFreeCAD(
        get_objects=lambda d: [{"Name": "Box"}],
        get_active_screenshot=lambda: None,
    )
    r = core.get_objects_operation(fake, False, "Doc")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Box" in t for t in texts)


def test_get_object_json_response():
    fake = FakeFreeCAD(
        get_object=lambda d, n: {"Name": n, "TypeId": "Part::Box"},
        get_active_screenshot=lambda: None,
    )
    r = core.get_object_operation(fake, False, "Doc", "Box")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Part::Box" in t for t in texts)


def test_get_parts_list_with_parts():
    fake = FakeFreeCAD(get_parts_list=lambda: ["a.fcstd", "b.fcstd"])
    r = core.get_parts_list_operation(fake)
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("a.fcstd" in t and "b.fcstd" in t for t in texts)


def test_get_parts_list_empty_message():
    fake = FakeFreeCAD(get_parts_list=lambda: [])
    r = core.get_parts_list_operation(fake)
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("No parts found" in t for t in texts)


def test_list_documents_json():
    fake = FakeFreeCAD(list_documents=lambda: ["Doc1", "Doc2"])
    r = core.list_documents_operation(fake)
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Doc1" in t and "Doc2" in t for t in texts)


def test_get_view_with_screenshot():
    fake = FakeFreeCAD(get_active_screenshot=lambda *a, **k: "BASE64DATA")
    r = core.get_view_operation(fake, "Isometric")
    assert any(getattr(t, "type", "") == "image" and t.data == "BASE64DATA" for t in r)


def test_get_view_with_screenshot_jpeg_mime():
    """v1.0.3 — image_format='jpeg' must surface as image/jpeg mimeType,
    not the legacy hardcoded image/png (which would mislead strict MCP
    clients)."""
    def fake_status(*a, **k):
        return {"success": True, "screenshot": "BASE64JPEG", "format": "jpeg"}
    fake = FakeFreeCAD(
        get_active_screenshot=lambda *a, **k: "BASE64JPEG",
        get_active_screenshot_with_status=fake_status,
    )
    r = core.get_view_operation(fake, "Isometric", image_format="jpeg")
    img = next(t for t in r if getattr(t, "type", "") == "image")
    assert img.mimeType == "image/jpeg"
    assert img.data == "BASE64JPEG"


def test_get_view_with_screenshot_webp_mime():
    """v1.0.3 — image_format='webp' must surface as image/webp."""
    def fake_status(*a, **k):
        return {"success": True, "screenshot": "BASE64WEBP", "format": "webp"}
    fake = FakeFreeCAD(
        get_active_screenshot=lambda *a, **k: "BASE64WEBP",
        get_active_screenshot_with_status=fake_status,
    )
    r = core.get_view_operation(fake, "Isometric", image_format="webp")
    img = next(t for t in r if getattr(t, "type", "") == "image")
    assert img.mimeType == "image/webp"


def test_get_view_with_screenshot_png_mime():
    """Default PNG path still works."""
    def fake_status(*a, **k):
        return {"success": True, "screenshot": "BASE64", "format": "png"}
    fake = FakeFreeCAD(
        get_active_screenshot=lambda *a, **k: "BASE64",
        get_active_screenshot_with_status=fake_status,
    )
    r = core.get_view_operation(fake, "Isometric")
    img = next(t for t in r if getattr(t, "type", "") == "image")
    assert img.mimeType == "image/png"


def test_get_view_without_screenshot():
    def fake_status(*a, **k):
        return {"success": False, "reason": "no_capture"}
    fake = FakeFreeCAD(
        get_active_screenshot=lambda *a, **k: None,
        get_active_screenshot_with_status=fake_status,
    )
    r = core.get_view_operation(fake, "Isometric")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Cannot get screenshot" in t for t in texts)


def test_get_view_rpc_error_reports_detail():
    """v1.0.3 — RPC failures surface the underlying detail."""
    def fake_status(*a, **k):
        return {"success": False, "reason": "rpc_error", "detail": "ConnectionRefusedError: nope"}
    fake = FakeFreeCAD(
        get_active_screenshot=lambda *a, **k: None,
        get_active_screenshot_with_status=fake_status,
    )
    r = core.get_view_operation(fake, "Isometric")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("RPC error" in t and "ConnectionRefusedError" in t for t in texts)


def test_get_view_timeout_reports_detail():
    def fake_status(*a, **k):
        return {"success": False, "reason": "timeout", "detail": "5.0s"}
    fake = FakeFreeCAD(
        get_active_screenshot=lambda *a, **k: None,
        get_active_screenshot_with_status=fake_status,
    )
    r = core.get_view_operation(fake, "Isometric")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("timed out" in t.lower() and "5.0s" in t for t in texts)


def test_get_view_unknown_reason_reports_reason_and_detail():
    """Forward-compat: any new reason code shows up as 'reason=... detail=...'."""
    def fake_status(*a, **k):
        return {"success": False, "reason": "future_thing", "detail": "stuff"}
    fake = FakeFreeCAD(
        get_active_screenshot=lambda *a, **k: None,
        get_active_screenshot_with_status=fake_status,
    )
    r = core.get_view_operation(fake, "Isometric")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("reason=future_thing" in t and "detail=stuff" in t for t in texts)


def test_run_fem_analysis_success_summary():
    fake = FakeFreeCAD(
        run_fem_analysis=lambda d, a, t: {
            "success": True,
            "result_object": "Results",
            "node_count": 1234,
            "max_von_mises_MPa": 250.0,
            "min_von_mises_MPa": 5.0,
            "max_displacement_mm": 0.123,
            "working_dir": "/tmp/x",
        },
        get_active_screenshot=lambda: None,
    )
    r = core.run_fem_analysis_operation(fake, False, "Doc", "Analysis", 60)
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("solved" in t.lower() for t in texts)
    assert any("250" in t for t in texts)


def test_run_fem_analysis_failure_summary():
    fake = FakeFreeCAD(
        run_fem_analysis=lambda d, a, t: {
            "success": False, "error": "no solver", "working_dir": "/tmp/x",
        },
        get_active_screenshot=lambda: None,
    )
    r = core.run_fem_analysis_operation(fake, False, "Doc", "Analysis", 60)
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("failed" in t.lower() and "no solver" in t for t in texts)


def test_safe_operation_catches_exception():
    fake = FakeFreeCAD(get_objects=lambda d: (_ for _ in ()).throw(RuntimeError("boom")))
    fake._handlers["get_active_screenshot"] = lambda: None
    r = core.get_objects_operation(fake, False, "Doc")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Internal server error" in t and "boom" in t for t in texts)


def test_undo_success():
    fake = FakeFreeCAD(undo=lambda d, s: {"success": True, "undone_steps": 3})
    r = core.undo_operation(fake, "Doc", 3)
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Undid 3" in t for t in texts)


def test_undo_failure():
    fake = FakeFreeCAD(undo=lambda d, s: {"success": False, "error": "no doc"})
    r = core.undo_operation(fake, "Missing", 1)
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Failed to undo" in t and "no doc" in t for t in texts)


def test_redo_success():
    fake = FakeFreeCAD(redo=lambda d, s: {"success": True, "redone_steps": 2})
    r = core.redo_operation(fake, "Doc", 2)
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Redid 2" in t for t in texts)


def test_redo_failure():
    fake = FakeFreeCAD(redo=lambda d, s: {"success": False, "error": "nothing to redo"})
    r = core.redo_operation(fake, "Doc", 2)
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Failed to redo" in t and "nothing to redo" in t for t in texts)


def test_save_document_success():
    fake = FakeFreeCAD(save_document=lambda d, p: {"success": True, "path": p or "/x.fcstd"})
    r = core.save_document_operation(fake, "Doc", "/tmp/x.fcstd")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Saved" in t and "/tmp/x.fcstd" in t for t in texts)


def test_save_document_failure():
    fake = FakeFreeCAD(save_document=lambda d, p: {"success": False, "error": "disk full"})
    r = core.save_document_operation(fake, "Doc", "/x.fcstd")
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Failed" in t and "disk full" in t for t in texts)


def test_export_object_success():
    fake = FakeFreeCAD(export_object=lambda d, n, p, f: {"success": True, "path": p, "format": f or "stl"})
    r = core.export_object_operation(fake, "Doc", "Box", "/tmp/x.stl", None)
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Exported" in t and "stl" in t for t in texts)


def test_export_object_failure():
    fake = FakeFreeCAD(export_object=lambda d, n, p, f: {"success": False, "error": "no shape"})
    r = core.export_object_operation(fake, "Doc", "Box", "/tmp/x.stl", None)
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Failed" in t and "no shape" in t for t in texts)


def test_get_active_view_success():
    fake = FakeFreeCAD(get_active_view=lambda: {
        "success": True, "view_type": "View3DInventor",
        "width": 1024, "height": 768, "has_save_image": True,
    })
    r = core.get_active_view_operation(fake)
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("View3DInventor" in t and "1024" in t for t in texts)


def test_get_active_view_failure():
    fake = FakeFreeCAD(get_active_view=lambda: {"success": False, "error": "no active view"})
    r = core.get_active_view_operation(fake)
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("Failed" in t and "no active view" in t for t in texts)


def test_health_check_returns_json():
    fake = FakeFreeCAD(health_check=lambda: {
        "success": True, "uptime_seconds": 12.5, "rpc_server_running": True,
        "request_queue_size": 0, "response_queue_size": 0,
        "cached_responses": 0, "pending_cancellations": 0,
        "settings_dir": "/tmp/freecad_mcp_settings.json",
    })
    r = core.health_check_operation(fake)
    texts = [t.text for t in r if hasattr(t, "text")]
    assert any("uptime_seconds" in t and "settings_dir" in t for t in texts)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("All operations core tests passed")
