"""Package-level metadata for passe-partout.

Single source of truth for the version string, derived from the installed
package metadata (which itself comes from pyproject.toml's `[project] version`).
Falls back to ``"unknown"`` when the package isn't installed (e.g. running
straight from a source checkout without ``uv sync`` / ``pip install -e .``).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("passe-partout")
except PackageNotFoundError:
    __version__ = "unknown"
