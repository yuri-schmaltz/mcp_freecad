import os

try:
    import FreeCAD
    import FreeCADGui
except Exception:
    # Module loaded outside FreeCAD during unit tests; the test
    # harness injects FreeCAD stubs as needed.
    FreeCAD = None  # type: ignore[assignment]
    FreeCADGui = None  # type: ignore[assignment]


def _safe_resolve(parts_lib_path: str, relative_path: str) -> str:
    """Resolve ``relative_path`` against the parts library, refusing traversal.

    Raises ``ValueError`` if the input is empty, absolute, contains ``..``
    segments, or resolves outside the parts library root.
    """
    if not relative_path or not relative_path.strip():
        raise ValueError("relative_path must not be empty.")
    # Reject absolute paths and Windows-style drive letters early.
    if os.path.isabs(relative_path) or relative_path.startswith(("/", "\\")):
        raise ValueError(f"relative_path must be relative: {relative_path!r}")
    # Normalise and reject any path that escapes via ..
    safe = os.path.normpath(relative_path)
    if safe.startswith("..") or os.path.isabs(safe):
        raise ValueError(f"relative_path escapes parts library: {relative_path!r}")
    # Final defence: the resolved absolute path must live under the library root.
    lib_root = os.path.realpath(parts_lib_path)
    candidate = os.path.realpath(os.path.join(lib_root, safe))
    if candidate != lib_root and not candidate.startswith(lib_root + os.sep):
        raise ValueError(f"relative_path escapes parts library: {relative_path!r}")
    return candidate


def insert_part_from_library(relative_path):
    if FreeCAD is None or FreeCADGui is None:
        raise RuntimeError(
            "FreeCAD is not available; insert_part_from_library requires a "
            "running FreeCAD session."
        )
    parts_lib_path = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "parts_library")
    part_path = _safe_resolve(parts_lib_path, relative_path)

    if not os.path.exists(part_path):
        raise FileNotFoundError(f"Not found: {part_path}")

    FreeCADGui.ActiveDocument.mergeProject(part_path)


# ---------------------------------------------------------------------------
# Parts list with mtime-based cache invalidation
# ---------------------------------------------------------------------------
#
# The previous implementation used ``functools.cache`` which never expires.
# If the user dropped new ``.FCStd`` files into ``~/.FreeCAD/Mod/parts_library``
# (or any subdirectory) they would be invisible until FreeCAD was restarted.
#
# New behaviour:
# - Each call walks the directory (cheap on a typical parts library) and
#   records a signature ``(latest_mtime, count)`` keyed on the path.
# - The result is only re-cached if the signature changes, so steady-state
#   callers (which is the common case) pay only the walk cost — same as
#   before — but a freshly-added file is picked up on the very next call.
# - The cache is keyed by the parts-library root path so multiple roots
#   (e.g. during tests with a temporary directory) do not poison each other.
# - Returning a defensive copy of the cached list prevents callers from
#   mutating the cache in place.

_parts_list_cache: dict[str, tuple[tuple[float, int], list[str]]] = {}


def _safe_mtime(path: str) -> float:
    """Return the mtime of *path* or 0 if it cannot be stat'd."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _walk_parts(parts_lib_path: str) -> tuple[tuple[float, int], list[str]]:
    """Walk *parts_lib_path* and return ``((latest_mtime, count), relative_paths)``."""
    parts: list[str] = []
    latest = _safe_mtime(parts_lib_path)
    for root, _dirs, files in os.walk(parts_lib_path):
        for file in files:
            if not file.endswith(".FCStd"):
                continue
            full = os.path.join(root, file)
            fm = _safe_mtime(full)
            if fm > latest:
                latest = fm
            parts.append(os.path.relpath(full, parts_lib_path))
    parts.sort()
    return (latest, len(parts)), parts


def get_parts_list() -> list[str]:
    """Return a sorted list of relative paths to ``.FCStd`` files under the parts library.

    The result is cached per root path and invalidated automatically when
    files are added, removed, or modified (detected via mtime). The
    returned list is a defensive copy; mutating it does not poison the
    cache.
    """
    if FreeCAD is None:
        raise RuntimeError(
            "FreeCAD is not available; get_parts_list requires a running FreeCAD session."
        )
    parts_lib_path = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "parts_library")

    if not os.path.exists(parts_lib_path):
        raise FileNotFoundError(f"Not found: {parts_lib_path}")

    signature, parts = _walk_parts(parts_lib_path)
    cached = _parts_list_cache.get(parts_lib_path)
    if cached is None or cached[0] != signature:
        _parts_list_cache[parts_lib_path] = (signature, parts)
    return list(_parts_list_cache[parts_lib_path][1])


def reset_parts_list_cache() -> None:
    """Clear the parts-list cache (test / diagnostic helper)."""
    _parts_list_cache.clear()
