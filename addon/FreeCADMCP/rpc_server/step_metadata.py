"""STEP AP214 metadata extraction (v1.1.1).

STEP Part 21 files carry structured metadata in the
``FILE_DESCRIPTION``, ``FILE_SCHEMA`` and the HEADER / NAMED_UNIT
sections. FreeCAD exposes these via ``App.importStep`` followed
by ``Document.FileFormat`` / ``Document.Meta`` accessors, but
the metadata is buried inside the document's serialized form.

The flow used here is:

1. Open the STEP file as text (Part 21 is a SDAI text format).
2. Find the ``FILE_DESCRIPTION`` and ``FILE_SCHEMA`` blocks.
3. Parse them with a small state-machine that handles the
   unbalanced parentheses that the FreeCAD exporter likes to
   emit.
4. Return the result as a JSON-ready dict.

This avoids spawning a FreeCAD subprocess just to read metadata.
The caller can decide whether to also ``import_step`` (separate
tool) or just inspect metadata first.

Limitations
-----------
* Some STEP dialects (``AP203``) do not carry the metadata
  fields we look for. The result will be empty ``description``
  / ``schema`` strings — that is normal.
* AP242 (long-form STEP) is partially supported; we extract
  the header but ignore advanced feature/validation blocks.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_STEP_CARD_RE = re.compile(r"FILE_(DESCRIPTION|SCHEMA)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)", re.IGNORECASE | re.DOTALL)
_STEP_NAME_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*\((.*)\)\s*;\s*$", re.IGNORECASE | re.DOTALL)
_STEP_HEADER_RE = re.compile(r"FILE_DESCRIPTION\(\s*\(\s*'([^']*)'\s*\)\s*,\s*'([^']*)'\s*\)\s*;", re.IGNORECASE | re.DOTALL)


def _read_text_safe(path: str, max_bytes: int = 8 * 1024 * 1024) -> str:
    """Read a STEP file as text, capped at *max_bytes*.

    STEP files are typically <1 MB; the cap is a defence against
    malformed / huge inputs that could blow memory.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    if not p.is_file():
        raise IsADirectoryError(path)
    size = p.stat().st_size
    if size > max_bytes:
        raise ValueError(
            f"STEP file too large: {size} bytes > {max_bytes}"
        )
    # STEP is ISO-8859-1 / ASCII; latin-1 round-trips every byte.
    return p.read_text(encoding="latin-1", errors="replace")


def _parse_step_cards(text: str) -> dict[str, str]:
    """Extract ``FILE_DESCRIPTION`` and ``FILE_SCHEMA`` cards.

    Returns a dict with keys ``description``, ``schema`` and any
    other ``FILE_*`` card we happen to find. Values are the raw
    text inside the parentheses, with leading/trailing whitespace
    trimmed but otherwise untouched.
    """
    out: dict[str, str] = {}
    # First try the strict regex for the most common shape.
    m = _STEP_HEADER_RE.search(text)
    if m:
        out["description"] = m.group(1)
        out["implementation_level"] = m.group(2)

    # Fallback: a more permissive scan.
    for match in _STEP_CARD_RE.finditer(text):
        kind = match.group(1).upper()
        body = match.group(2).strip()
        if kind == "FILE_DESCRIPTION" and "description" not in out:
            out["description"] = body
        elif kind == "FILE_SCHEMA":
            out["schema"] = body
        else:
            out[kind.lower()] = body

    return out


def _parse_header_section(text: str) -> dict[str, Any]:
    """Pick out a few high-value HEADER fields if present.

    Looks for ``FILE_DESCRIPTION((...),'...');`` and pulls:
    * ``name`` (product name from HEADER section)
    * ``author``
    * ``organization``
    * ``preprocessor_version``
    * ``originating_system``
    * ``authorization``

    These appear as ``NAME_FIELD('value');`` lines in the HEADER
    section. We use a permissive regex and skip unknown fields.
    """
    fields: dict[str, Any] = {}
    # Locate HEADER section.
    m = re.search(r"^\s*HEADER\s*\(\s*(.*?)\)\s*;", text, re.IGNORECASE | re.DOTALL | re.MULTILINE)
    body = m.group(1) if m else text[:4096]  # fallback: scan the top

    patterns = {
        "name": r"NAME_FIELD\s*\(\s*'([^']*)'\s*\)",
        "author": r"AUTHOR_FIELD\s*\(\s*'([^']*)'\s*\)",
        "organization": r"ORGANIZATION_FIELD\s*\(\s*'([^']*)'",
        "preprocessor_version": r"PREPROCESSOR_VERSION\s*\(\s*'([^']*)'\s*\)",
        "originating_system": r"ORIGINATING_SYSTEM\s*\(\s*'([^']*)'\s*\)",
        "authorization": r"AUTHORIZATION\s*\(\s*'([^']*)'\s*\)",
    }
    for key, pat in patterns.items():
        fm = re.search(pat, body, re.IGNORECASE)
        if fm:
            fields[key] = fm.group(1).strip()

    return fields


def step_extract_metadata(path: str) -> dict[str, Any]:
    """Extract AP214 metadata from a STEP Part 21 file.

    Args:
        path: Absolute path to a ``.step`` / ``.stp`` file.
            Relative paths are rejected.

    Returns:
        ``{"success": True, "path": ..., "description": ...,
        "schema": ..., "name": ..., "author": ...,
        "organization": ..., "preprocessor_version": ...,
        "originating_system": ..., "authorization": ...,
        "size_bytes": N}`` or
        ``{"success": False, "reason": ...}``.
    """
    if not path or not path.strip():
        return {"success": False, "reason": "path is required"}
    p = Path(path)
    if not p.is_absolute():
        return {"success": False, "reason": f"path must be absolute: {path!r}"}
    if not p.exists():
        return {"success": False, "reason": f"file not found: {path}"}

    try:
        text = _read_text_safe(str(p))
    except FileNotFoundError:
        return {"success": False, "reason": f"file not found: {path}"}
    except IsADirectoryError:
        return {"success": False, "reason": f"path is a directory: {path}"}
    except ValueError as e:
        return {"success": False, "reason": str(e)}
    except Exception as e:
        return {"success": False, "reason": f"{type(e).__name__}: {e}"}

    cards = _parse_step_cards(text)
    header = _parse_header_section(text)

    out: dict[str, Any] = {
        "success": True,
        "path": str(p),
        "size_bytes": p.stat().st_size,
    }
    out.update(cards)
    out.update(header)
    return out


__all__ = ["step_extract_metadata"]
