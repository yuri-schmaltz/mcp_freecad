"""Tests for addon/FreeCADMCP/rpc_server/_prompt_templates.py.

Loads the module directly via importlib because the addon directory
isn't on sys.path by default.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP" / "rpc_server"


@pytest.fixture
def pt():
    spec = importlib.util.spec_from_file_location(
        "_prompt_templates_for_test", ADDON_DIR / "_prompt_templates.py"
    )
    if spec is None or spec.loader is None:
        pytest.skip("cannot load _prompt_templates module spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_default_registry_has_builtin_templates(pt) -> None:
    reg = pt.PromptTemplateRegistry()
    names = reg.names()
    assert "Listar documentos" in names
    assert "Health check" in names
    assert "Caixa 10x10x10" in names
    assert "Auditoria FEM" in names
    assert "Diff de documentos" in names


def test_render_substitutes_known_var(pt) -> None:
    reg = pt.PromptTemplateRegistry()
    out = reg.render("Caixa 10x10x10", doc_name="Demo")
    assert "Demo" in out
    assert "{doc_name}" not in out


def test_render_with_missing_var_returns_empty(pt) -> None:
    reg = pt.PromptTemplateRegistry()
    out = reg.render("Caixa 10x10x10")
    assert "{doc_name}" not in out


def test_render_unknown_name_raises(pt) -> None:
    reg = pt.PromptTemplateRegistry()
    with pytest.raises(KeyError):
        reg.render("does-not-exist")


def test_get_returns_template(pt) -> None:
    reg = pt.PromptTemplateRegistry()
    tpl = reg.get("Health check")
    assert tpl is not None
    assert "health_check" in tpl.prompt


def test_get_returns_none_for_missing(pt) -> None:
    reg = pt.PromptTemplateRegistry()
    assert reg.get("nope") is None


def test_add_persists_to_disk(tmp_path, pt) -> None:
    config_path = tmp_path / "templates.json"
    reg = pt.PromptTemplateRegistry(path=config_path)
    reg.add("Custom", "do {thing}", persistent=True)
    assert config_path.exists()
    reg2 = pt.PromptTemplateRegistry(path=config_path)
    assert "Custom" in reg2.names()


def test_add_rejects_empty(pt) -> None:
    reg = pt.PromptTemplateRegistry()
    with pytest.raises(ValueError):
        reg.add("", "x")
    with pytest.raises(ValueError):
        reg.add("x", "")


def test_template_from_dict(pt) -> None:
    tpl = pt.PromptTemplate.from_dict({"name": "x", "prompt": "p"})
    assert tpl.name == "x"
    assert tpl.prompt == "p"


def test_load_corrupt_file_does_not_crash(tmp_path, pt) -> None:
    config_path = tmp_path / "templates.json"
    config_path.write_text("not json{{{")
    reg = pt.PromptTemplateRegistry(path=config_path)
    assert "Listar documentos" in reg.names()
