"""Pure (FreeCAD-independent) IP allowlist parser.

Why this exists
---------------
The RPC server restricts remote connections to a list of IPs / CIDR
subnets supplied by the operator (e.g. ``127.0.0.1, 192.168.0.0/24``).
The previous implementation lived inline in :mod:`rpc_server`; pulling
it out lets us unit-test it without spinning up FreeCAD / PySide and
makes the rules explicit:

* Each entry must be a valid IPv4 or IPv6 address or CIDR subnet.
* ``0.0.0.0/0`` and ``::/0`` are rejected — they match the entire
  internet and would silently expose ``execute_code`` (= arbitrary
  Python in the FreeCAD process) to anyone who can route to the host.
* The list is parsed strictly: empty input, leading/trailing commas,
  double commas, and other mal-formed shapes are reported as errors
  rather than silently skipped.

Public surface
--------------
* :func:`parse_allowlist` — returns ``(valid_entries, errors)``.
* :func:`parse_allowlist_to_networks` — returns ``list[ip_network]``,
  also logging warnings for any entries that were skipped.

The functions never raise on bad input — they accumulate errors so the
caller can present a complete report to the user.
"""
from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Callable

logger = logging.getLogger("FreeCADMCPip_allowlist")

# Regex for a well-formed comma-separated list. Each entry is
# ``\S+`` (no whitespace inside), separated by optional whitespace.
# The structure makes the constraints obvious:
#
#   ^\s*              — optional leading whitespace
#   [^,\s]+           — first entry (non-empty, no whitespace)
#   (\s*,\s*[^,\s]+)* — zero or more (whitespace + comma + whitespace + entry)
#   \s*$              — optional trailing whitespace
#
# Anything that violates this (leading/trailing comma, double comma,
# empty entry, etc.) is rejected by the regex and reported as malformed.
_ALLOWLIST_RE = re.compile(r"^\s*[^,\s]+(\s*,\s*[^,\s]+)*\s*$")


def parse_allowlist(
    raw: str | None,
) -> tuple[list[str], list[str]]:
    """Parse *raw* and return ``(valid_entries, errors)``.

    *valid_entries* contains the original substrings that passed
    validation (so the caller's settings file preserves user formatting).
    *errors* holds one human-readable string per failed check.
    """
    if raw is None or not raw.strip():
        return [], ["Input must not be empty."]

    if not _ALLOWLIST_RE.match(raw):
        return [], [
            "Malformed list \u2014 check for leading/trailing commas, "
            "double commas, or missing separators."
        ]

    valid: list[str] = []
    errors: list[str] = []
    for entry in raw.split(","):
        entry = entry.strip()
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            errors.append(f"Invalid IP/subnet: '{entry}'")
            continue
        if network.prefixlen == 0:
            errors.append(
                f"Refusing insecure wildcard '{entry}' (matches every IP). "
                "List concrete subnets instead, e.g. 192.168.0.0/16."
            )
            continue
        valid.append(entry)
    return valid, errors


def parse_allowlist_to_networks(
    raw: str | None,
    on_warning: Callable[[str], None] | None = None,
) -> list:
    """Parse *raw* and return the list of networks, skipping bad entries.

    Use this from the server lifecycle where bad entries should be
    dropped (with a warning) rather than aborting the start-up.

    ``on_warning`` is invoked once per skipped entry; pass a callable
    that prints to the FreeCAD console or to the logger. Falls back to
    :mod:`logging` warnings when ``None``.
    """
    valid, errors = parse_allowlist(raw)
    for msg in errors:
        if on_warning is not None:
            on_warning(msg)
        else:
            logger.warning("MCP RPC: %s, skipping", msg)
    return [ipaddress.ip_network(entry, strict=False) for entry in valid]


# Backward-compat alias for the previous module-private name.
def _parse_allowed_ips(allowed_ips_str: str) -> list:
    """Back-compat alias used by older callers (kept for safety)."""
    return list(parse_allowlist_to_networks(allowed_ips_str))


__all__ = [
    "parse_allowlist",
    "parse_allowlist_to_networks",
    "_parse_allowed_ips",
]
