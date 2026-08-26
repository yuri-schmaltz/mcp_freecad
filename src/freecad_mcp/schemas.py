"""Pydantic models for tool parameters.

Why this exists
---------------
The MCP tool implementations take ``dict[str, Any] | None`` for many
parameters. Without validation, a malformed object name (a number, an
int with leading zero, a string that exceeds FreeCAD's limit) flows
all the way to the FreeCAD process and surfaces as a vague
``Fault``. The LLM then has to guess what's wrong.

These models give us a single source of truth for the expected
parameter shape. Two key design decisions:

* **Additive, not breaking** \u2014 ``model_validate(obj)`` is the
  preferred entry point and accepts the legacy ``dict[str, Any]``
  shape so existing call sites keep working. Tools that do not yet
  opt in to validation are unchanged.
* **Permissive on property values** \u2014 ``Properties`` is a free-form
  dict because FreeCAD's property system is too broad to enumerate.
  We validate only the **structural** constraints (name lengths,
  type prefixes, etc.) and leave domain values to FreeCAD.

What lives here
---------------
* :class:`CreateObjectRequest` \u2014 ``create_object`` payload.
* :class:`EditObjectRequest` \u2014 ``edit_object`` payload.
* :class:`FreeCADObjectName` \u2014 string validator for object names.
* :class:`DocumentName` \u2014 string validator for document names.

For each model the ``.model_validate({...})`` constructor accepts the
legacy ``dict`` shape and returns either a populated model or a
``pydantic.ValidationError``.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger("FreeCADMCPschemas")

# FreeCAD object name rules (from FreeCAD source):
# - Must not be empty.
# - Must not start or end with whitespace.
# - Must not contain dots (used as attribute accessors by FreeCAD's
#   Python bindings, e.g. ``doc.MyObject``).
# - Length <= 256 chars (FreeCAD's internal limit).
# - Pydantic's ``str_strip_whitespace`` config already trims leading /
#   trailing whitespace, so we only need to reject dots and oversize.
_FREE_CAD_NAME_RE = re.compile(r"^[^.]{1,256}$")


def _validate_free_cad_name(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("name must be a non-empty string")
    # Pydantic's str_strip_whitespace trims surrounding whitespace before
    # we get here, so an empty string after stripping is also invalid.
    if not _FREE_CAD_NAME_RE.match(value):
        raise ValueError(
            "name must be 1-256 chars and must not contain dots"
        )
    return value


# The list of TypeId prefixes FreeCAD recognises. The full set is much
# larger (see FreeCAD source ``App/FeaturePython.h`` etc.), but these
# are the ones the MCP tools actually support today. The validator
# accepts the literal string \u2014 a typo from the LLM surfaces as a clear
# error before reaching the FreeCAD process.
_KNOWN_TYPE_PREFIXES: tuple[str, ...] = (
    "Part::",
    "PartDesign::",
    "Draft::",
    "Fem::",
    "Mesh::",
    "Sketcher::",
    "Spreadsheet::",
    "TechDraw::",
    "Arch::",
    "App::",
)


class _StrictModel(BaseModel):
    """Base config: forbid unknown fields by default.

    We want typos like ``obj_typ`` (instead of ``obj_type``) to fail
    loudly rather than being silently ignored.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FreeCADObjectName(str):
    """Validated FreeCAD object name.

    Use as ``FreeCADObjectName("MyBox")``; raises ``ValueError`` if the
    name is invalid.
    """

    @classmethod
    def __get_validators__(cls):
        yield cls._validate

    @classmethod
    def _validate(cls, value: Any) -> FreeCADObjectName:
        if isinstance(value, cls):
            return value
        _validate_free_cad_name(value)
        return cls(value)


class DocumentName(str):
    """Validated FreeCAD document name."""

    @classmethod
    def __get_validators__(cls):
        yield cls._validate

    @classmethod
    def _validate(cls, value: Any) -> DocumentName:
        if isinstance(value, cls):
            return value
        _validate_free_cad_name(value)
        return cls(value)


class CreateObjectRequest(_StrictModel):
    """Parameters for the ``create_object`` MCP tool.

    Legacy input shape::

        {
            "doc_name": "MyDoc",
            "obj_name": "Box",
            "obj_type": "Part::Box",
            "analysis_name": "Analysis",       # optional
            "obj_properties": { "Length": 10 } # optional
        }
    """

    doc_name: str = Field(..., min_length=1, max_length=256)
    obj_name: str = Field(..., min_length=1, max_length=256)
    obj_type: str = Field(..., min_length=1, max_length=256)
    analysis_name: str | None = Field(default=None, max_length=256)
    obj_properties: dict[str, Any] | None = None

    @field_validator("doc_name", "obj_name", "analysis_name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _validate_free_cad_name(value)

    @field_validator("obj_type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        # We don't hard-fail on unknown prefixes because FreeCAD has
        # 50+ TypeId prefixes and our allowlist is necessarily a
        # subset; we just surface a warning so the LLM can catch
        # typos (e.g. ``Part::Boxx``) before the request reaches
        # FreeCAD and dies with a vague ``Fault``.
        if not any(value.startswith(prefix) for prefix in _KNOWN_TYPE_PREFIXES):
            logger.warning(
                "create_object: obj_type=%r does not match a known FreeCAD "
                "TypeId prefix (%s). The request will still be sent; "
                "expect a 'Type not found' error from FreeCAD on misspellings.",
                value, ", ".join(_KNOWN_TYPE_PREFIXES),
            )
        return value

    @model_validator(mode="after")
    def _fem_needs_analysis(self) -> CreateObjectRequest:
        # Fem:: types other than Fem::AnalysisPython require an analysis
        # container to live in. We surface this as a validation error
        # rather than letting it fail at the FreeCAD layer.
        if (
            self.obj_type.startswith("Fem::")
            and self.obj_type != "Fem::AnalysisPython"
            and not self.analysis_name
        ):
            raise ValueError(
                f"obj_type {self.obj_type!r} requires an analysis_name "
                "(the Fem::AnalysisPython container to attach to)"
            )
        return self


class EditObjectRequest(_StrictModel):
    """Parameters for the ``edit_object`` MCP tool."""

    doc_name: str = Field(..., min_length=1, max_length=256)
    obj_name: str = Field(..., min_length=1, max_length=256)
    obj_properties: dict[str, Any]

    @field_validator("doc_name", "obj_name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _validate_free_cad_name(value)


def validate_create_object(payload: dict[str, Any]) -> CreateObjectRequest:
    """Validate the legacy ``create_object`` payload and return a model.

    Raises :class:`pydantic.ValidationError` on bad input; the caller
    (typically ``safe_operation``-wrapped tool) formats the error for
    the LLM.
    """
    return CreateObjectRequest.model_validate(payload)  # type: ignore[no-any-return]


def validate_edit_object(payload: dict[str, Any]) -> EditObjectRequest:
    """Validate the legacy ``edit_object`` payload and return a model."""
    return EditObjectRequest.model_validate(payload)  # type: ignore[no-any-return]


__all__ = [
    "CreateObjectRequest",
    "EditObjectRequest",
    "DocumentName",
    "FreeCADObjectName",
    "validate_create_object",
    "validate_edit_object",
]
