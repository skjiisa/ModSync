"""Entry point: `python -m modsync [command]`.

`doctor` runs the stdlib-only discovery report (no PySide6 needed). With no
arguments, ModSync launches the GUI (which does require PySide6).
"""

from __future__ import annotations

import sys

_USAGE = """\
modsync — set up Skyrim SE for modding on Steam Deck and Linux

usage:
  modsync                                launch the GUI (requires PySide6)
  modsync doctor                         inspect this machine (Steam, Skyrim SE, MO2, current setup)

  modsync mo2 status                     the Mod Organizer 2 instance in use
  modsync mo2 use <instance-dir>         use an existing portable instance
  modsync mo2 install <dest-dir>         install a fresh instance with MO2-LINT

  modsync game status                    installed game version vs this setup, Steam state
  modsync game downgrade <version>       downgrade the game with community xdelta patches
  modsync game pin                       keep Steam from updating the installed files

  modsync sync create <instance-dir>     optional: share this setup; prints a pairing code
  modsync sync join <code> <instance>    optional: copy another machine's setup here
  modsync serve                          keep syncing (if set up) and watching Steam, in the foreground

  modsync service install [--linger]     run 'modsync serve' at login (systemd --user)
  modsync steam shortcut                 add ModSync as a non-Steam game (Gaming Mode)

sync commands run headless and keep Syncthing alive until Ctrl-C.
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    cmd = argv[0] if argv else None

    if cmd in {"doctor", "discover", "scan"}:
        from modsync.cli import doctor

        return doctor(argv[1:])

    if cmd == "mo2":
        from modsync.cli import mo2

        return mo2(argv[1:])

    if cmd in {"sync", "vault"}:  # "vault" is the historical name
        from modsync.cli import vault

        return vault(argv[1:])

    if cmd == "serve":
        from modsync.cli import serve

        return serve(argv[1:])

    if cmd == "service":
        from modsync.cli import service

        return service(argv[1:])

    if cmd == "steam":
        from modsync.cli import steam

        return steam(argv[1:])

    if cmd == "game":
        from modsync.cli import game

        return game(argv[1:])

    if cmd in {"-h", "--help", "help"}:
        print(_USAGE)
        return 0

    from modsync.cli import launch_gui

    return launch_gui(argv)


if __name__ == "__main__":
    raise SystemExit(main())
