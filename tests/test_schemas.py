"""Unit tests for the Pydantic request schemas.

These schemas sit between the MCP tool layer and the FreeCAD RPC
client. A failure here must surface as a clear error to the LLM, not
a vague FreeCAD fault.
"""
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freecad_mcp.schemas import (  # noqa: E402
    DocumentName,
    FreeCADObjectName,
    validate_create_object,
    validate_edit_object,
)


# --- CreateObjectRequest ------------------------------------------------

def test_create_object_minimal():
    req = validate_create_object({
        "doc_name": "MyDoc",
        "obj_type": "Part::Box",
        "obj_name": "Box",
    })
    assert req.doc_name == "MyDoc"
    assert req.obj_name == "Box"
    assert req.obj_type == "Part::Box"
    assert req.analysis_name is None
    assert req.obj_properties is None


def test_create_object_with_properties():
    req = validate_create_object({
        "doc_name": "MyDoc",
        "obj_type": "Part::Box",
        "obj_name": "Box",
        "obj_properties": {"Length": 10, "Width": 5},
    })
    assert req.obj_properties == {"Length": 10, "Width": 5}


def test_create_object_with_analysis():
    req = validate_create_object({
        "doc_name": "MyDoc",
        "obj_type": "Fem::MaterialCommon",
        "obj_name": "Steel",
        "analysis_name": "Analysis",
    })
    assert req.analysis_name == "Analysis"


def test_create_object_fem_type_requires_analysis():
    """Fem:: types other than Fem::AnalysisPython need an analysis."""
    with pytest.raises(ValidationError) as exc:
        validate_create_object({
            "doc_name": "Doc",
            "obj_type": "Fem::MaterialCommon",
            "obj_name": "Steel",
            # analysis_name missing
        })
    assert "analysis_name" in str(exc.value).lower()


def test_create_object_fem_analysis_python_does_not_require_analysis():
    """The container itself doesn't live in a parent analysis."""
    req = validate_create_object({
        "doc_name": "Doc",
        "obj_type": "Fem::AnalysisPython",
        "obj_name": "Analysis",
    })
    assert req.analysis_name is None


def test_create_object_rejects_empty_name():
    with pytest.raises(ValidationError):
        validate_create_object({
            "doc_name": "Doc",
            "obj_type": "Part::Box",
            "obj_name": "",
        })


def test_create_object_rejects_dotted_name():
    """FreeCAD names cannot contain dots (used as attribute accessors)."""
    with pytest.raises(ValidationError):
        validate_create_object({
            "doc_name": "Doc",
            "obj_type": "Part::Box",
            "obj_name": "Box.1",
        })


def test_create_object_rejects_extra_fields():
    """A typo in the field name should fail loudly, not be silently dropped."""
    with pytest.raises(ValidationError) as exc:
        validate_create_object({
            "doc_name": "Doc",
            "obj_type": "Part::Box",
            "obj_name": "Box",
            "obj_propertie": {"Length": 10},  # missing 's'
        })
    assert "obj_propertie" in str(exc.value)


def test_create_object_strips_whitespace():
    req = validate_create_object({
        "doc_name": "  Doc  ",
        "obj_type": "Part::Box",
        "obj_name": "  Box  ",
    })
    assert req.doc_name == "Doc"
    assert req.obj_name == "Box"


# --- EditObjectRequest --------------------------------------------------

def test_edit_object_minimal():
    req = validate_edit_object({
        "doc_name": "Doc",
        "obj_name": "Box",
        "obj_properties": {"Length": 20},
    })
    assert req.obj_properties == {"Length": 20}


def test_edit_object_rejects_missing_properties():
    with pytest.raises(ValidationError):
        validate_edit_object({
            "doc_name": "Doc",
            "obj_name": "Box",
            # obj_properties missing
        })


def test_edit_object_rejects_extra_fields():
    with pytest.raises(ValidationError):
        validate_edit_object({
            "doc_name": "Doc",
            "obj_name": "Box",
            "obj_properties": {},
            "extra": 1,
        })


# --- integration with operations ----------------------------------------

def test_create_object_operation_rejects_bad_payload(monkeypatch):
    """The operation layer must surface a validation error as a text
    response (not raise), so the LLM gets a clear message.
    """
    from freecad_mcp.operations import core as ops

    class _FakeConn:
        def create_object(self_inner, *args, **kwargs):
            raise AssertionError("FreeCAD should not be called for invalid input")

        def get_active_screenshot(self_inner):
            return None

    result = ops.create_object_operation(
        _FakeConn(),  # type: ignore[arg-type]
        only_text_feedback=True,
        doc_name="Doc",
        obj_type="Part::Box",
        obj_name="",  # invalid: empty
    )
    assert len(result) == 1
    assert "Invalid create_object" in result[0].text


def test_create_object_operation_fem_without_analysis_blocked(monkeypatch):
    """The classic 'create Fem::Material without analysis' bug.
    """
    from freecad_mcp.operations import core as ops

    class _FakeConn:
        def create_object(self_inner, *args, **kwargs):
            raise AssertionError("should not reach FreeCAD")

        def get_active_screenshot(self_inner):
            return None

    result = ops.create_object_operation(
        _FakeConn(),  # type: ignore[arg-type]
        only_text_feedback=True,
        doc_name="Doc",
        obj_type="Fem::ConstraintFixed",
        obj_name="Fix",
        # analysis_name missing
    )
    assert "Invalid create_object" in result[0].text
    assert "analysis_name" in result[0].text.lower()


# --- FreeCADObjectName / DocumentName validators ------------------------


def test_free_cad_object_name_accepts_valid_string():
    """FreeCADObjectName is a str subclass that holds validated names."""
    n = FreeCADObjectName("MyBox")
    assert str(n) == "MyBox"
    assert isinstance(n, str)


def test_document_name_accepts_valid_string():
    """DocumentName is a str subclass that holds validated names."""
    n = DocumentName("MyDoc")
    assert str(n) == "MyDoc"
    assert isinstance(n, str)


def test_free_cad_object_name_validator_methods_exist():
    """Even when pydantic doesn't auto-call __get_validators__, the
    methods are still defined (we keep them for v1 compatibility)."""
    validators = list(FreeCADObjectName.__get_validators__())
    assert len(validators) == 1
    # _validate is callable.
    assert callable(FreeCADObjectName._validate)


def test_free_cad_object_name_validate_wraps_string():
    """Calling _validate on a plain string wraps it in cls(value)."""
    n = FreeCADObjectName._validate("MyBox")
    assert isinstance(n, FreeCADObjectName)
    assert str(n) == "MyBox"


def test_free_cad_object_name_validate_rejects_invalid():
    """Calling _validate directly with a dotted name raises ValueError."""
    with pytest.raises(ValueError, match="must not contain dots"):
        FreeCADObjectName._validate("a.b")


def test_free_cad_object_name_validate_idempotent():
    """If we already have a FreeCADObjectName, validation is a no-op."""
    n1 = FreeCADObjectName("MyBox")
    assert FreeCADObjectName._validate(n1) is n1


def test_document_name_validator_methods_exist():
    validators = list(DocumentName.__get_validators__())
    assert len(validators) == 1


def test_document_name_validate_rejects_invalid():
    with pytest.raises(ValueError, match="must not contain dots"):
        DocumentName._validate("a.b")


def test_document_name_validate_idempotent():
    n1 = DocumentName("MyDoc")
    assert DocumentName._validate(n1) is n1


def test_document_name_validate_wraps_string():
    n = DocumentName._validate("MyDoc")
    assert isinstance(n, DocumentName)
    assert str(n) == "MyDoc"


# --- _validate_free_cad_name direct ------------------------------------


def test_validate_free_cad_name_rejects_non_string():
    from freecad_mcp.schemas import _validate_free_cad_name
    with pytest.raises(ValueError, match="non-empty"):
        _validate_free_cad_name(None)  # type: ignore[arg-type]


def test_create_object_warns_on_unknown_obj_type_prefix(caplog):
    """obj_type prefixes outside the allowlist log a warning but the
    request still passes validation (FreeCAD is the source of truth)."""
    import logging
    with caplog.at_level(logging.WARNING, logger="freecad_mcp.schemas"):
        req = validate_create_object({
            "doc_name": "MyDoc",
            "obj_name": "X",
            "obj_type": "Unknown::Foo",
        })
    assert req.obj_type == "Unknown::Foo"
    assert any("Unknown::Foo" in r.message for r in caplog.records)


if __name__ == "__main__":
    print("Run with pytest; direct invocation is not supported.")
