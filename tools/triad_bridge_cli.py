"""Thin absolute-path entry point.

Usage from any working directory:

    python "<install>/tools/triad_bridge_cli.py" --config "<private config>" status --help

The package directory is prepended to this process's `sys.path` only; the
global PYTHONPATH/PATH are never modified.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from triad_bridge.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
