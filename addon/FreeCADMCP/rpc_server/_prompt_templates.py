"""F2: built-in prompt templates for the FreeCAD MCP panel.

Five canned prompts the operator can launch with one click from the
``MCPControlPanel``. All templates accept a small ````{}``-style
substitution (no full Jinja2 — the panel cannot import anything outside
FreeCAD's PySide sandbox).

Templates persist to ``~/.config/FreeCAD/mcp-freecad/prompt_templates.json``
so the operator can add custom ones without code changes.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TEMPLATES: list[dict[str, str]] = [
    {
        "name": "Listar documentos",
        "prompt": "Liste os documentos FreeCAD abertos e mostre um resumo de cada um.",
    },
    {
        "name": "Health check",
        "prompt": "Execute health_check e descreva o estado do servidor RPC.",
    },
    {
        "name": "Caixa 10x10x10",
        "prompt": (
            "Crie um documento chamado {doc_name}, adicione uma Part::Box "
            "de 10x10x10mm chamada Box e salve o arquivo."
        ),
    },
    {
        "name": "Auditoria FEM",
        "prompt": (
            "Verifique o ambiente FEM (run_fem_analysis) e relate se há "
            "pré-requisitos faltando (calculix, Gmsh, etc.)."
        ),
    },
    {
        "name": "Diff de documentos",
        "prompt": (
            "Compare os documentos {doc_a} e {doc_b} usando diff_documents "
            "e resuma as diferenças principais."
        ),
    },
]


def _config_path() -> Path:
    override = os.environ.get("FREECAD_MCP_PROMPT_TEMPLATES_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "FreeCAD" / "mcp-freecad" / "prompt_templates.json"


@dataclass
class PromptTemplate:
    name: str
    prompt: str

    @classmethod
    def from_dict(cls, payload: dict[str, str]) -> PromptTemplate:
        return cls(
            name=str(payload.get("name", "")).strip(),
            prompt=str(payload.get("prompt", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "prompt": self.prompt}


@dataclass
class PromptTemplateRegistry:
    path: Path = field(default_factory=_config_path)
    templates: list[PromptTemplate] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.templates:
            self.templates = [PromptTemplate.from_dict(t) for t in DEFAULT_TEMPLATES]
            self._load_custom()

    def _load_custom(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, list):
            return
        for entry in raw:
            tpl = PromptTemplate.from_dict(entry)
            if tpl.name and tpl.prompt:
                self.templates.append(tpl)

    def save_custom(self) -> None:
        builtin_names = {t["name"] for t in DEFAULT_TEMPLATES}
        custom = [t.to_dict() for t in self.templates if t.name not in builtin_names]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(custom, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def names(self) -> list[str]:
        return [t.name for t in self.templates]

    def get(self, name: str) -> PromptTemplate | None:
        for t in self.templates:
            if t.name == name:
                return t
        return None

    def render(self, name: str, **kwargs: str) -> str:
        tpl = self.get(name)
        if tpl is None:
            raise KeyError(name)
        try:
            return tpl.prompt.format(**kwargs)
        except KeyError as missing:
            return tpl.prompt.replace("{" + missing.args[0] + "}", "")

    def add(self, name: str, prompt: str, *, persistent: bool = True) -> None:
        if not name or not prompt:
            raise ValueError("name and prompt are required")
        self.templates.append(PromptTemplate(name=name, prompt=prompt))
        if persistent:
            self.save_custom()


__all__ = [
    "DEFAULT_TEMPLATES",
    "PromptTemplate",
    "PromptTemplateRegistry",
]
