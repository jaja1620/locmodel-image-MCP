#!/usr/bin/env python3
"""locmodel-image installer entry point (also doubles as the MCP server).

Usage:
    locmodel-image-setup                  # double-click / no args -> install
    locmodel-image-setup install [...]    # explicit install
    locmodel-image-setup doctor           # self-check
    locmodel-image-setup list             # host registry status
    locmodel-image-setup uninstall [--purge]
    locmodel-image-setup emit marvis      # paste-into-chat instructions
    locmodel-image-setup serve            # MCP stdio server (hosts call this)
"""

from __future__ import annotations

import sys
from pathlib import Path


def _add_package_path() -> None:
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Handle the MCP entry before anything can write to stdout.
    if argv and argv[0] in ("serve", "--serve", "--stdio"):
        _add_package_path()
        from locsetup.cli import serve
        return serve()

    _add_package_path()

    interactive = not argv and sys.stdin.isatty()
    from locsetup.cli import run

    code = run(argv)

    if interactive:
        from locsetup import ui
        ui.pause_if_double_clicked(True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
