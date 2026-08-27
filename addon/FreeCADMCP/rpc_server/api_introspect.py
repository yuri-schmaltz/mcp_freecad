"""Live API introspection for FreeCAD callables.

Exposes two helpers:

* :func:`api_introspect` — return the ``inspect.Signature`` and
  docstring of any Python callable reachable from the FreeCAD
  process (e.g. ``Part.makeBox``, ``App.Vector``).

* :func:`api_search` — fuzzy keyword search across a configurable
  set of FreeCAD modules (``FreeCAD``, ``FreeCADGui``, ``Part``,
  ``PartDesign``, ``Mesh``, ``MeshPart``, ``Sketcher``, ``Draft``,
  ``TechDraw``, ``Path``, ``Fem``, ``Arch``, ``Spreadsheet``,
  ``DraftVecUtils``, ``math``).

The module never calls the callable — it only inspects it.

It does **not** import FreeCAD at import-time: the caller must
pass in the modules via the ``modules`` argument (or call
:func:`default_modules` to gather them once FreeCAD is loaded).
"""
from __future__ import annotations

import inspect
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ParamInfo:
    """Description of a single function/method parameter."""

    name: str
    kind: str  # POSITIONAL_OR_KEYWORD / POSITIONAL_ONLY / etc.
    default: Any = inspect.Parameter.empty  # may be sentinel
    annotation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if out["default"] is inspect.Parameter.empty:
            out["default"] = None
            out["has_default"] = False
        else:
            out["has_default"] = True
        return out


@dataclass
class FunctionInfo:
    """Inspect summary of a callable."""

    qualified_name: str
    signature: str
    doc: str
    parameters: list[ParamInfo] = field(default_factory=list)
    is_method: bool = False
    is_class: bool = False
    base_classes: list[str] = field(default_factory=list)
    module: str = ""
    return_annotation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["parameters"] = [p.to_dict() for p in self.parameters]
        return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _annotation_str(ann: Any) -> str | None:
    if ann is inspect.Parameter.empty or ann is None:
        return None
    try:
        return str(ann)
    except Exception:
        return None


def _safe_signature(callable_: Any) -> inspect.Signature | None:
    try:
        return inspect.signature(callable_)
    except (TypeError, ValueError):
        return None
    except Exception:
        return None


def _summarize_callable(
    qualified_name: str,
    callable_: Any,
    *,
    is_method: bool = False,
) -> FunctionInfo:
    sig_str = ""
    params: list[ParamInfo] = []
    ret_ann: str | None = None
    sig = _safe_signature(callable_)
    if sig is not None:
        try:
            sig_str = str(sig)
        except Exception:
            sig_str = "(unrepresentable)"
        for p in sig.parameters.values():
            params.append(
                ParamInfo(
                    name=p.name,
                    kind=str(p.kind),
                    default=p.default,
                    annotation=_annotation_str(p.annotation),
                )
            )
        ret_ann = _annotation_str(sig.return_annotation)

    doc = ""
    try:
        doc = inspect.getdoc(callable_) or ""
    except Exception:
        doc = ""

    module = getattr(callable_, "__module__", "") or ""

    base_classes: list[str] = []
    if inspect.isclass(callable_):
        for base in getattr(callable_, "__bases__", ()):
            base_classes.append(getattr(base, "__name__", repr(base)))

    return FunctionInfo(
        qualified_name=qualified_name,
        signature=sig_str,
        doc=doc,
        parameters=params,
        is_method=is_method,
        is_class=inspect.isclass(callable_),
        base_classes=base_classes,
        module=module,
        return_annotation=ret_ann,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def api_introspect(path: str, modules: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve ``path`` and return a serialisable summary.

    ``path`` may be:

    * A fully qualified name resolvable via ``modules[*]``
      (e.g. ``Part.makeBox``, ``FreeCAD.Vector``).
    * A dotted path like ``Part.Shape.Faces`` — returns the summary
      of the **outermost** callable/class (``Part.Shape``).

    Returns ``{"success": bool, "reason"?: str, "info"?: FunctionInfo}``.
    """
    target = _resolve(path, modules)
    if target is _NOT_FOUND:
        return {"success": False, "reason": f"{path!r} not found"}
    if target is _AMBIGUOUS:
        return {
            "success": False,
            "reason": f"{path!r} is ambiguous (resolves to multiple attributes)",
        }
    try:
        info = _summarize_callable(path, target)
    except Exception as e:
        return {"success": False, "reason": f"introspection failed: {type(e).__name__}: {e}"}
    return {"success": True, "info": info.to_dict()}


def api_search(
    query: str,
    modules: Mapping[str, Any],
    *,
    modules_filter: Iterable[str] | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Search public attributes of ``modules`` whose name contains ``query``.

    Case-insensitive substring match. ``query`` may also be a regex if
    it starts with ``/`` and ends with ``/``. Results are ranked:

    1. Exact name match (case-insensitive) first.
    2. Substring match.
    3. Docstring hit.

    Returns a list of ``FunctionInfo.to_dict()`` dicts, capped at
    ``limit`` (default 25).
    """
    pattern: re.Pattern[str] | None = None
    if query.startswith("/") and query.endswith("/") and len(query) >= 3:
        try:
            pattern = re.compile(query[1:-1], re.IGNORECASE)
        except re.error:
            pattern = None
    q = query.lower()

    hits: list[tuple[int, FunctionInfo]] = []
    for mod_name, mod in modules.items():
        if modules_filter and mod_name not in modules_filter:
            continue
        if mod is None:
            continue
        for name in dir(mod):
            if name.startswith("__") and name.endswith("__"):
                # skip dunders unless query explicitly asks for them
                if not query.startswith("__"):
                    continue
            try:
                obj = getattr(mod, name)
            except Exception:
                continue

            score = 0
            lname = name.lower()
            if pattern is not None:
                if pattern.search(name):
                    score = 3
                elif (
                    callable(obj)
                    and inspect.getdoc(obj)
                    and pattern.search(inspect.getdoc(obj))
                ):
                    score = 1
                else:
                    continue
            else:
                if lname == q:
                    score = 4
                elif q in lname:
                    score = 3
                elif callable(obj) and inspect.getdoc(obj) and q in inspect.getdoc(obj).lower():
                    score = 1
                else:
                    continue

            if score == 0:
                continue

            try:
                info = _summarize_callable(f"{mod_name}.{name}", obj)
            except Exception:
                continue
            hits.append((score, info))

    # Sort by score desc, then by name asc.
    hits.sort(key=lambda h: (-h[0], h[1].qualified_name.lower()))
    return [info.to_dict() for _, info in hits[:limit]]


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


_NOT_FOUND = object()
_AMBIGUOUS = object()


def _resolve(path: str, modules: Mapping[str, Any]) -> Any:
    """Resolve ``a.b.c`` against the given modules dict.

    Returns the resolved object, ``_NOT_FOUND`` or ``_AMBIGUOUS``.
    """
    parts = path.split(".")
    if not parts or not parts[0]:
        return _NOT_FOUND
    head, rest = parts[0], parts[1:]
    if head not in modules:
        return _NOT_FOUND
    obj = modules[head]
    for i, p in enumerate(rest):
        try:
            nxt = getattr(obj, p)
        except AttributeError:
            # try one more: perhaps the user gave `a.b` and `b` is in
            # modules directly (e.g. ``Part.Vector`` where Part has
            # both a function and a class with a Vector attribute).
            if i == 0 and p in modules:
                obj = modules[p]
                continue
            return _NOT_FOUND
        except Exception:
            return _NOT_FOUND
        obj = nxt
    return obj


def default_modules() -> dict[str, Any]:
    """Return the standard FreeCAD module mapping.

    Imports are best-effort: if a module is missing (e.g. a slim
    Flatpak without Path workbench), it is silently skipped.
    """
    import sys

    out: dict[str, Any] = {}
    candidates = (
        "FreeCAD",
        "FreeCADGui",
        "Part",
        "PartDesign",
        "Mesh",
        "MeshPart",
        "Sketcher",
        "Draft",
        "TechDraw",
        "Path",
        "Fem",
        "Arch",
        "Spreadsheet",
        "DraftVecUtils",
        "math",
        "os",
    )
    for name in candidates:
        try:
            mod = sys.modules.get(name)
            if mod is None:
                mod = __import__(name)
            out[name] = mod
        except Exception:
            continue
    return out


__all__ = [
    "FunctionInfo",
    "ParamInfo",
    "api_introspect",
    "api_search",
    "default_modules",
]
