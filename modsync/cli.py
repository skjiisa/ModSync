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


def vault(args: list[str]) -> int:
    """Headless vault management (no GUI). Keeps Syncthing running in the
    foreground so it actually syncs; Ctrl-C to stop."""
    sub = args[0] if args else ""
    rest = args[1:]
    if sub == "create":
        return _vault_create(rest)
    if sub == "join":
        return _vault_join(rest)
    print(
        "usage:\n"
        "  modsync vault create <instance-dir> [--label NAME]\n"
        "  modsync vault join <pairing-code> <instance-dir>"
    )
    return 2


def _vault_create(args: list[str]) -> int:
    label = "Mod Organizer 2"
    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--label" and i + 1 < len(args):
            label, i = args[i + 1], i + 2
        else:
            positional.append(args[i])
            i += 1
    if len(positional) != 1:
        print("usage: modsync vault create <instance-dir> [--label NAME]")
        return 2

    from modsync.service import ModSyncService

    service = ModSyncService()
    print(f"Starting Syncthing and creating a vault for {positional[0]} …")
    code = service.create_vault(positional[0], label)
    print("\nShare this pairing code with your other machines:\n")
    print(f"  {code.encode()}\n")
    return _run_until_interrupt(service)


def _vault_join(args: list[str]) -> int:
    if len(args) != 2:
        print("usage: modsync vault join <pairing-code> <instance-dir>")
        return 2
    code_text, instance = args

    from modsync.pairing_code import PairingCode
    from modsync.service import ModSyncService

    try:
        code = PairingCode.decode(code_text)
    except Exception:
        print("That doesn't look like a valid pairing code (expected MODSYNC1-…).")
        return 2

    service = ModSyncService()
    print(f"Starting Syncthing and joining vault {code.folder_id} into {instance} …")
    mine = service.join_vault(code, instance)
    print("\nThis machine's pairing code (add it on the other machine to sync back):\n")
    print(f"  {mine.encode()}\n")
    return _run_until_interrupt(service)


def _run_until_interrupt(service) -> int:
    import time

    print("Syncing — press Ctrl-C to stop.")
    try:
        while True:
            try:
                status = service.status()
                connected = sum(1 for d in status.devices if d.connected)
                line = f"  devices {connected}/{len(status.devices)} connected"
                if status.folder_state is not None:
                    line += f" · folder {status.folder_state} · {int(status.completion or 0)}% in sync"
                print(line, flush=True)
            except Exception as exc:
                print(f"  (status unavailable: {exc})", flush=True)
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nStopping Syncthing …")
    finally:
        service.shutdown()
    return 0
