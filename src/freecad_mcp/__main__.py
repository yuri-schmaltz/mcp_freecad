"""Entrypoint for ``python -m freecad_mcp``.

Forwards to :func:`freecad_mcp.server.main` so that ``python -m freecad_mcp``
behaves the same as the ``mcp-freecad`` console script defined in
``pyproject.toml`` (``[project.scripts]``).
"""

from .server import main

if __name__ == "__main__":
    main()
