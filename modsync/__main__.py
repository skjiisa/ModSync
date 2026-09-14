"""Entry point: `python -m modsync [doctor]`.

`doctor` runs the stdlib-only discovery report (no PySide6 needed). With no
arguments, ModSync launches the GUI (which does require PySide6).
"""

from __future__ import annotations

import sys

_USAGE = """\
modsync — sync Mod Organizer 2 setups across machines

usage:
  modsync doctor                         inspect this machine (Steam, Skyrim SE, MO2)
  modsync vault create <instance-dir>    create a sync vault and print a pairing code
  modsync vault join <code> <instance>   join a vault from another machine's code
  modsync                                launch the GUI (requires PySide6)

vault commands run headless and keep Syncthing alive until Ctrl-C.
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    cmd = argv[0] if argv else None

    if cmd in {"doctor", "discover", "scan"}:
        from modsync.cli import doctor

        return doctor(argv[1:])

    if cmd == "vault":
        from modsync.cli import vault

        return vault(argv[1:])

    if cmd in {"-h", "--help", "help"}:
        print(_USAGE)
        return 0

    from modsync.cli import launch_gui

    return launch_gui(argv)


if __name__ == "__main__":
    raise SystemExit(main())
