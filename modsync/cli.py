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
    if sub == "serve":  # kept for units installed by older versions
        return serve(rest)
    print(
        "usage:\n"
        "  modsync vault create <instance-dir> [--label NAME]\n"
        "  modsync vault join <pairing-code> <instance-dir>\n"
        "  modsync serve                       keep syncing / watching in the foreground"
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
    vc = service.game_version_check()
    if vc.expected is not None:
        src = " (from the installed SKSE)" if vc.expected_from == "skse" else ""
        print(f"Recorded {vc.expected}{src} as the game version this vault is built for.")
        if vc.mismatch:
            print(f"  ! {vc.summary()}")
            print("    'modsync game status' shows the downgrade, or re-record with the dashboard's")
            print("    \"Use this machine's version\" if the SKSE here is stale.")
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


def serve(args: list[str]) -> int:
    """Keep this machine's setup going in the foreground: sync the vault if
    there is one, apply a queued Steam pin, watch for Steam updates. This is
    what the background service runs."""
    from modsync.service import ModSyncService

    return _run_until_interrupt(ModSyncService())


def _run_until_interrupt(service) -> int:
    from modsync.serve import Server

    def log(line: str) -> None:
        print(line, flush=True)

    return Server(service, log=log).run()


def service(args: list[str]) -> int:
    """Install/manage the optional systemd --user background service."""
    from modsync import background

    sub = args[0] if args else ""
    if sub == "install":
        print(background.install(enable_linger="--linger" in args))
        return 0
    if sub == "uninstall":
        background.uninstall()
        print("Removed the ModSync background service.")
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
        "  modsync service install [--linger]   run 'modsync serve' at login (systemd --user)\n"
        "  modsync service status\n"
        "  modsync service uninstall"
    )
    return 2


def game(args: list[str]) -> int:
    """Game runtime management: show versions, downgrade, pin Steam."""
    from modsync.service import ModSyncService

    sub = args[0] if args else ""
    rest = args[1:]
    if sub == "status":
        return _game_status(ModSyncService())
    if sub == "downgrade":
        return _game_downgrade(ModSyncService(), rest)
    if sub == "pin":
        return _game_pin(ModSyncService())
    print(
        "usage:\n"
        "  modsync game status                  installed vs vault version, Steam state, recipes\n"
        "  modsync game downgrade <version> [-y] apply the community patches to reach <version>\n"
        "  modsync game pin                     make Steam treat the installed files as current"
    )
    return 2


def _game_status(service) -> int:
    st = service.game_status()
    print(f"Installed:      {st.installed or '(not detected)'}"
          + (f"   ({st.game_dir})" if st.game_dir else ""))
    print(f"Vault expects:  {st.expected or '(not recorded)'}")
    if st.skse_runtime:
        print(f"SKSE here:      built for {st.skse_runtime}   ({st.skse_source})")
    elif st.skse_runtimes:
        print(f"SKSE here:      DLLs for several versions: {', '.join(st.skse_runtimes)}")
    else:
        print("SKSE here:      (not found)")
    print(f"Language:       {st.language}")
    if st.steam_is_current is None:
        print("Steam:          (state unknown)")
    elif st.steam_is_current:
        print(f"Steam:          up to date as far as Steam knows (build {st.steam_public_build})")
    else:
        print(f"Steam:          wants to update to build {st.steam_public_build} — 'modsync game pin' keeps the installed files")
    if st.pending_pin:
        print("                a pin is queued and will apply when Steam is closed")
    if st.recipe_from:
        print(f"Recipes:        from {st.recipe_from} → {', '.join(st.recipe_targets)}   [{st.recipe_origin}]")
        if st.can_downgrade_to:
            print(f"Downgradable:   {', '.join(st.can_downgrade_to)}")
        elif st.installed:
            print(f"Downgradable:   none from {st.installed} (recipes start at {st.recipe_from})")
    else:
        print("Recipes:        unavailable")
    if st.mismatch:
        print(f"\n! This machine runs {st.installed} but the vault was set up for {st.expected}.")
    elif st.needs_downgrade:
        print(
            f"\n! This machine runs {st.installed} but the installed SKSE is built for {st.wanted}, "
            "so this setup was most likely made for that version."
        )
    if st.needs_downgrade and st.suggested_target:
        print(f"  Run: modsync game downgrade {st.suggested_target}")
    return 0


def _game_downgrade(service, args: list[str]) -> int:
    import sys

    yes = "-y" in args or "--yes" in args
    positional = [a for a in args if not a.startswith("-")]
    if len(positional) != 1:
        print("usage: modsync game downgrade <version> [-y]")
        return 2
    target = positional[0]
    from modsync.downgrade import engine

    try:
        plan = service.plan_downgrade(target)
        engine.preflight(plan)
    except Exception as exc:
        print(f"Cannot downgrade: {exc}")
        return 1
    est = f"about {plan.estimated_bytes / 1e9:.1f} GB" if plan.estimated_bytes else "an unknown amount"
    print(f"Downgrade {plan.from_version} → {plan.target} ({plan.language}) in {plan.game_dir}")
    print(f"  {len(plan.archives)} patch archive(s), {est} to download (cached for next time).")
    print("  Game files are replaced in place; Steam keeps launching the game normally.")
    if not yes:
        ans = input("Proceed? [y/N] ").strip().lower()
        if ans not in {"y", "yes"}:
            print("Aborted.")
            return 1

    last = {"line": ""}

    def progress(p: engine.Progress) -> None:
        if p.stage == "download" and p.total:
            line = f"  [{p.stage}] {p.message}: {p.done / 1e6:,.0f}/{p.total / 1e6:,.0f} MB"
        elif p.total:
            line = f"  [{p.stage}] {p.message} ({p.done}/{p.total})"
        else:
            line = f"  [{p.stage}] {p.message}"
        if line != last["line"]:
            sys.stdout.write("\r" + line[:118].ljust(118))
            sys.stdout.flush()
            last["line"] = line

    try:
        result = service.run_downgrade(target, progress)
    except Exception as exc:
        print(f"\nDowngrade failed: {exc}")
        return 1
    print(f"\nDone: the game now reports {result.installed_version}; {len(result.patched_files)} files patched, "
          f"{result.downloaded_bytes / 1e6:,.0f} MB downloaded.")
    for note in result.notes:
        print(f"  • {note}")
    st = service.game_status(refresh_index=False)
    if st.needs_pin:
        print("Steam wants to update this install — run 'modsync game pin' (with Steam closed) to keep it.")
    return 0


def _game_pin(service) -> int:
    try:
        out = service.pin_game_version()
    except Exception as exc:
        print(f"Cannot pin: {exc}")
        return 1
    print(out.message)
    for c in out.changes:
        print(f"  {c.field}: {c.old} → {c.new}")
    return 0


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
