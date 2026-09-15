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
    if sub == "serve":
        return _vault_serve(rest)
    print(
        "usage:\n"
        "  modsync vault create <instance-dir> [--label NAME]\n"
        "  modsync vault join <pairing-code> <instance-dir>\n"
        "  modsync vault serve"
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


def _vault_serve(args: list[str]) -> int:
    """Resume an already-configured vault and keep syncing. This is what the
    background service runs; it can also be used for a manual long-running sync."""
    from modsync.service import ModSyncService

    service = ModSyncService()
    if not service.state.configured:
        print(
            "No vault is configured on this machine yet.\n"
            "Run 'modsync vault create <instance-dir>' or\n"
            "    'modsync vault join <pairing-code> <instance-dir>' first."
        )
        return 1
    print(f"Serving vault {service.state.folder_id} for {service.state.instance_path}")
    vc = service.game_version_check()
    if vc.mismatch:
        print(f"\n  ! {vc.summary()}\n")
    service.ensure_running()
    return _run_until_interrupt(service)


def _run_until_interrupt(service) -> int:
    import time

    print("Syncing — press Ctrl-C to stop.")
    try:
        while True:
            try:
                for device_id in service.accept_pending():
                    print(f"  ✓ accepted new device {device_id[:13]}…", flush=True)
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


def service(args: list[str]) -> int:
    """Install/manage the optional systemd --user background sync service."""
    from modsync import background

    sub = args[0] if args else ""
    if sub == "install":
        print(background.install(enable_linger="--linger" in args))
        return 0
    if sub == "uninstall":
        background.uninstall()
        print("Removed the ModSync background sync service.")
        return 0
    if sub == "status":
        st = background.status()
        print(f"unit:      {st['unit_path']}")
        print(f"installed: {'yes' if st['installed'] else 'no'}")
        print(f"enabled:   {st['enabled']}")
        print(f"active:    {st['active']}")
        return 0
    print(
        "usage:\n"
        "  modsync service install [--linger]   sync in the background (systemd --user)\n"
        "  modsync service status\n"
        "  modsync service uninstall"
    )
    return 2


def steam(args: list[str]) -> int:
    """Steam integration helpers (currently: add ModSync as a non-Steam game)."""
    from modsync.steam import shortcuts

    sub = args[0] if args else ""
    if sub in {"shortcut", "add-shortcut"}:
        if shortcuts.steam_is_running():
            print("Close Steam first — it rewrites shortcuts.vdf from memory on exit,")
            print("which would discard the shortcut. Then run this again.")
            return 1
        native = "--native" in args
        paths = shortcuts.add_modsync_to_steam(
            flatpak_id=None if native else shortcuts.MODSYNC_FLATPAK_ID
        )
        if not paths:
            print(
                "No Steam users found — make sure Steam is installed and has been "
                "run at least once on this machine."
            )
            return 1
        for p in paths:
            print(f"  ✓ added/updated 'ModSync' in {p}")
        print("\nRestart Steam, then find ModSync in your library (it appears as a")
        print("non-Steam game). On the Deck you can then launch it from Gaming Mode.")
        return 0
    print(
        "usage:\n"
        "  modsync steam shortcut [--native]   add ModSync as a non-Steam game\n"
        "        (--native uses the local command instead of the Flatpak)"
    )
    return 2
