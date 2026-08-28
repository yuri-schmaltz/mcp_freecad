"""Tests for the step_metadata module (v1.1.1)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_RPC_DIR = Path(__file__).resolve().parent.parent / "addon/FreeCADMCP/rpc_server"


@pytest.fixture
def sm_mod():
    spec = importlib.util.spec_from_file_location(
        "_step_metadata_for_test", _RPC_DIR / "step_metadata.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_step_metadata_for_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Sample STEP fixtures
# ---------------------------------------------------------------------------

AP214_STEP = """\
ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('FreeCAD export'),'2;1');
FILE_NAME('widget.step',
  '2026-08-27T12:00:00',
  ('john.doe'),
  ('ACME Corp.'),
  'FreeCAD 1.1.3',
  'FreeCAD',
  'GPL-2.0');
FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));
ENDSEC;
DATA;
/* product definition */
#1 = APPLICATION_PROTOCOL_DEFINITION(...);
ENDSEC;
END-ISO-10303-21;
"""


AP203_STEP = """\
ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('legacy.step',
  '2020-01-01T00:00:00',
  ('old'),
  ('old-co'),
  '',
  '',
  '');
FILE_SCHEMA(('CONFIG_CONTROL_DESIGN'));
ENDSEC;
DATA;
ENDSEC;
END-ISO-10303-21;
"""


EMPTY_STEP = "ISO-10303-21;\nENDSEC;\nEND-ISO-10303-21;\n"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_extract_ap214_metadata(sm_mod, tmp_path: Path) -> None:
    f = tmp_path / "widget.step"
    f.write_text(AP214_STEP, encoding="latin-1")
    res = sm_mod.step_extract_metadata(str(f))
    assert res["success"] is True
    assert res["path"] == str(f)
    assert "FreeCAD export" in res["description"]
    assert "AUTOMOTIVE_DESIGN" in res["schema"]
    assert res["implementation_level"] == "2;1"


def test_extract_ap203_metadata(sm_mod, tmp_path: Path) -> None:
    f = tmp_path / "legacy.step"
    f.write_text(AP203_STEP, encoding="latin-1")
    res = sm_mod.step_extract_metadata(str(f))
    assert res["success"] is True
    assert "CONFIG_CONTROL_DESIGN" in res["schema"]


def test_extract_handles_minimal_step(sm_mod, tmp_path: Path) -> None:
    f = tmp_path / "tiny.step"
    f.write_text(EMPTY_STEP, encoding="latin-1")
    res = sm_mod.step_extract_metadata(str(f))
    assert res["success"] is True
    assert res["size_bytes"] == len(EMPTY_STEP)


def test_extract_rejects_empty_path(sm_mod) -> None:
    assert sm_mod.step_extract_metadata("")["success"] is False
    assert sm_mod.step_extract_metadata("   ")["success"] is False


def test_extract_rejects_relative_path(sm_mod) -> None:
    res = sm_mod.step_extract_metadata("relative/path.step")
    assert res["success"] is False
    assert "absolute" in res["reason"]


def test_extract_missing_file(sm_mod, tmp_path: Path) -> None:
    res = sm_mod.step_extract_metadata(str(tmp_path / "nope.step"))
    assert res["success"] is False
    assert "not found" in res["reason"]


def test_extract_directory_path(sm_mod, tmp_path: Path) -> None:
    res = sm_mod.step_extract_metadata(str(tmp_path))
    assert res["success"] is False


def test_extract_oversized_file(sm_mod, tmp_path: Path) -> None:
    f = tmp_path / "big.step"
    # Write > 8 MB of garbage so the size cap rejects it.
    f.write_bytes(b"X" * (9 * 1024 * 1024))
    res = sm_mod.step_extract_metadata(str(f))
    assert res["success"] is False
    assert "too large" in res["reason"]


def test_extract_picks_up_header_fields(sm_mod, tmp_path: Path) -> None:
    """The HEADER section gives us the product name, author, etc."""
    f = tmp_path / "x.step"
    # Construct a STEP file that uses the legacy NAME_FIELD format.
    legacy_format = """\
ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('test'),'2;1');
NAME_FIELD('widget.step');
AUTHOR_FIELD('alice');
ORGANIZATION_FIELD('Acme');
FILE_SCHEMA(('CONFIG_CONTROL_DESIGN'));
ENDSEC;
DATA;
ENDSEC;
END-ISO-10303-21;
"""
    f.write_text(legacy_format, encoding="latin-1")
    res = sm_mod.step_extract_metadata(str(f))
    assert res["success"] is True
    # These come from HEADER FILE_NAME / NAME_FIELD etc.
    assert res.get("name") == "widget.step"
    assert res.get("author") == "alice"
    assert res.get("organization") == "Acme"  # ORGANIZATION_FIELD regex requires trailing comma


def test_parse_step_cards_returns_lowercase_keys(sm_mod) -> None:
    cards = sm_mod._parse_step_cards(AP214_STEP)
    assert "description" in cards
    assert "schema" in cards


def test_read_text_safe_encodes_latin1(sm_mod, tmp_path: Path) -> None:
    f = tmp_path / "weird.step"
    # Write a STEP file with high-bit latin-1 characters.
    f.write_bytes(b"FILE_DESCRIPTION(('caf\xe9'),'2;1');\nENDSEC;\n")
    text = sm_mod._read_text_safe(str(f))
    assert "caf" in text  # the \xe9 is decoded as latin-1


def test_read_text_safe_caps_at_max_bytes(sm_mod, tmp_path: Path) -> None:
    f = tmp_path / "big.step"
    f.write_bytes(b"X" * 1024)
    with pytest.raises(ValueError):
        sm_mod._read_text_safe(str(f), max_bytes=512)


def test_parse_header_section_extracts_name(sm_mod) -> None:
    legacy_format = """\
HEADER;
NAME_FIELD('widget.step');
AUTHOR_FIELD('alice');
ENDSEC;
"""
    fields = sm_mod._parse_header_section(legacy_format)
    assert fields.get("name") == "widget.step"
    assert fields.get("author") == "alice"


def test_parse_header_section_handles_missing_header(sm_mod) -> None:
    fields = sm_mod._parse_header_section("nothing here")
    assert fields == {}


def test_extract_size_bytes_matches_file(sm_mod, tmp_path: Path) -> None:
    f = tmp_path / "x.step"
    payload = AP214_STEP.encode("latin-1")
    f.write_bytes(payload)
    res = sm_mod.step_extract_metadata(str(f))
    assert res["size_bytes"] == len(payload)
