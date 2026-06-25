"""Command-line surface. `doctor` is stdlib-only; `launch_gui` lazy-imports PySide6."""

from __future__ import annotations


def doctor(args: list[str]) -> int:
    from modsync.report import build

    print("ModSync doctor\n")
    report = build()
    print(report.text)
    return 0 if report.steam_found else 1


def launch_gui(args: list[str]) -> int:
    try:
        from modsync.ui.app import run
    except ImportError as exc:
        print("The ModSync GUI requires PySide6, which isn't installed yet.")
        print(f"  ({exc})")
        print()
        print("For now you can run the stdlib-only discovery report:")
        print("  python3 -m modsync doctor")
        return 1
    return run(args)
