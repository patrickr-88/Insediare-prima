"""``python -m usbinstaller`` entry point.

With no arguments and an available display, the GUI is launched — that is what
a technician gets when they double-click the launcher. Any argument switches to
the CLI.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        try:
            from .ui.gui import run_gui
        except Exception:  # noqa: BLE001 - fall back to the CLI on any GUI problem
            from .cli import main as cli_main

            return cli_main([])
        return run_gui()
    from .cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
