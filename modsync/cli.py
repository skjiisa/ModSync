"""Command-line surface. `doctor` is stdlib-only; `launch_gui` lazy-imports PySide6."""

from __future__ import annotations


def doctor(args: list[str]) -> int:
    from modsync.report import build

    print("ModSync doctor\n")
    report = build()
    print(report.text)
    from modsync.logging_setup import launch_hook_log_path, log_path

    print(f"Log file:        {log_path()}")
    print(f"Launch hook log: {launch_hook_log_path()}")
    print("'modsync diagnostics' bundles this report with the recent logs for a bug report.")
    return 0 if report.steam_found else 1


def diagnostics(args: list[str]) -> int:
    """Everything for a bug report, secrets redacted. Stdlib-only like `doctor`."""
    from modsync.diagnostics import build

    print(build(), end="")
    return 0


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


def mo2(args: list[str]) -> int:
    """The Mod Organizer 2 instance this machine uses: show, choose, or install one."""
    sub = args[0] if args else ""
    rest = args[1:]
    if sub == "status":
        return _mo2_status()
    if sub == "use":
        return _mo2_use(rest)
    if sub == "install":
        return _mo2_install(rest)
    print(
        "usage:\n"
        "  modsync mo2 status                   the instance in use and whether it is synced\n"
        "  modsync mo2 use <instance-dir>       use an existing portable instance here\n"
        "  modsync mo2 install <dest-dir>       install a fresh instance with MO2-LINT"
    )
    return 2


def _mo2_status() -> int:
    from modsync.state import State

    state = State.load()
    if not state.has_instance:
        print("No Mod Organizer 2 instance chosen yet. 'modsync mo2 use <dir>' or 'modsync mo2 install <dest>'.")
        return 1
    print(f"Instance:  {state.instance_path}")
    print(f"Label:     {state.instance_label}")
    from pathlib import Path

    if not Path(state.instance_path).is_dir():
        print("           ! this directory no longer exists (unplugged drive? moved?) — "
              "'modsync mo2 use <dir>' to point at it again")
        return 1
    print(f"Sync:      {'vault ' + str(state.folder_id) if state.syncing else 'off'}")
    return 0


def _mo2_use(args: list[str]) -> int:
    from pathlib import Path

    from modsync.service import ModSyncService

    if len(args) != 1:
        print("usage: modsync mo2 use <instance-dir>")
        return 2
    path = Path(args[0]).expanduser()
    if not path.is_dir():
        print(f"Not a directory: {path}")
        return 1
    service = ModSyncService()
    try:
        service.choose_instance(path)
    except RuntimeError as exc:
        print(f"Cannot switch instance: {exc}")
        return 1
    print(f"Using {path}")
    vc = service.game_version_check()
    if vc.expected is not None:
        src = " (from the installed SKSE)" if vc.expected_from == "skse" else ""
        print(f"This setup is built for {vc.expected}{src}.")
    if vc.mismatch or vc.skse_suggests:
        print(f"  ! {vc.summary()}")
        print("    'modsync game status' shows the downgrade.")
    return 0


def _mo2_install(args: list[str]) -> int:
    import sys
    from pathlib import Path

    from modsync.games import SKYRIM_SE
    from modsync.mo2.installers import Mo2LintBackend
    from modsync.service import ModSyncService
    from modsync.steam import shortcuts

    if len(args) != 1:
        print("usage: modsync mo2 install <dest-dir>")
        return 2
    dest = Path(args[0]).expanduser()
    backend = Mo2LintBackend()
    ok, reason = backend.available()
    if not ok:
        print(f"Cannot install: {reason}")
        return 1
    if shortcuts.steam_is_running():
        print("Close Steam first — the installer configures the game's Proton prefix.")
        return 1
    print(f"Installing Mod Organizer 2 for {SKYRIM_SE.name} to {dest} … (this can take several minutes)")
    result = backend.install(SKYRIM_SE, dest, on_output=lambda line: print(f"  {line}", file=sys.stderr))
    if not result.success or not result.instance_path:
        print(f"Install failed: {result.message}")
        return 1
    ModSyncService().choose_instance(result.instance_path)
    print(f"Installed and now in use: {result.instance_path}")
    return 0


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
        "  modsync sync create <instance-dir> [--label NAME]\n"
        "  modsync sync join <pairing-code> <instance-dir>\n"
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
        print("usage: modsync sync create <instance-dir> [--label NAME]")
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
        print("usage: modsync sync join <pairing-code> <instance-dir>")
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
    from modsync.logging_setup import configure
    from modsync.service import ModSyncService

    configure("serve", ["serve", *args])
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
    """Game runtime management: show versions, downgrade, pin Steam — and undo both."""
    from modsync.service import ModSyncService

    sub = args[0] if args else ""
    rest = args[1:]
    if sub == "status":
        return _game_status(ModSyncService())
    if sub == "downgrade":
        return _game_downgrade(ModSyncService(), rest)
    if sub == "restore":
        return _game_restore(ModSyncService(), rest)
    if sub == "pin":
        return _game_pin(ModSyncService())
    if sub == "unpin":
        return _game_unpin(ModSyncService())
    print(
        "usage:\n"
        "  modsync game status                  installed vs this setup's version, Steam state, recipes\n"
        "  modsync game downgrade <version> [-y] apply the community patches to reach <version>\n"
        "  modsync game restore [--discard]     put the original files back (undo the downgrade);\n"
        "        (--discard deletes the backup instead, e.g. after Steam re-installed the game)\n"
        "  modsync game pin                     make Steam treat the installed files as current\n"
        "  modsync game unpin                   let Steam update the game again"
    )
    return 2


def _game_status(service) -> int:
    from modsync.games import SKYRIM_SE

    st = service.game_status()
    if st.installed is None and st.game_dir is None:
        print(f"{SKYRIM_SE.name} was not found through Steam on this machine. Install it in Steam first.")
        return 1
    print(f"Installed:      {st.installed or '(not detected)'}"
          + (f"   ({st.game_dir})" if st.game_dir else ""))
    print(f"Setup needs:    {st.expected or '(not recorded)'}")
    if st.skse_runtime:
        print(f"SKSE here:      built for {st.skse_runtime}   ({st.skse_source})")
    elif st.skse_runtimes:
        print(f"SKSE here:      DLLs for several versions: {', '.join(st.skse_runtimes)}")
    else:
        print("SKSE here:      (not found)")
    print(f"Language:       {st.language}")
    if st.steam_updating:
        print("Steam:          updating the game right now — not ready; wait for Steam to finish before pinning or downgrading")
    elif st.steam_is_current is None:
        print("Steam:          (state unknown)")
    elif st.steam_is_current:
        print(f"Steam:          up to date as far as Steam knows (build {st.steam_public_build})")
    else:
        print(f"Steam:          wants to update to build {st.steam_public_build} — 'modsync game pin' keeps the installed files")
    if st.pending_pin:
        print("                a pin is queued and will apply when Steam is closed")
    elif st.can_unpin:
        print("                pinned by ModSync — 'modsync game unpin' lets Steam update again")
    if st.backup_present and st.game_dir:
        print(f"Backup:         originals from before the downgrade are in {st.game_dir}/.modsync-downgrade/backup")
        print("                'modsync game restore' puts them back")
    if st.recipe_from:
        print(f"Recipes:        from {st.recipe_from} → {', '.join(st.recipe_targets)}   [{st.recipe_origin}]")
        if st.can_downgrade_to:
            print(f"Downgradable:   {', '.join(st.can_downgrade_to)}")
        elif st.installed:
            print(f"Downgradable:   none from {st.installed} (recipes start at {st.recipe_from})")
    else:
        print("Recipes:        unavailable")
    if st.steam_updating:
        print(f"\n! Steam is still updating {SKYRIM_SE.name}; version checks wait until it finishes.")
    elif st.mismatch:
        print(f"\n! This machine runs {st.installed} but this setup was built for {st.expected}.")
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
    if result.backup_dir:
        print(f"  • The original files are kept in {result.backup_dir}; 'modsync game restore' puts them back.")
    st = service.game_status(refresh_index=False)
    if st.needs_pin:
        print("Steam wants to update this install — run 'modsync game pin' (with Steam closed) to keep it.")
    return 0


def _game_restore(service, args: list[str]) -> int:
    if "--discard" in args:
        try:
            freed = service.discard_downgrade_backup()
        except Exception as exc:
            print(f"Cannot discard the backup: {exc}")
            return 1
        print(f"Deleted the downgrade backup ({freed / 1e6:,.0f} MB freed). The game files were not touched.")
        return 0
    if args:
        print("usage: modsync game restore [--discard]")
        return 2
    try:
        result = service.restore_game_files()
    except Exception as exc:
        print(f"Cannot restore: {exc}")
        return 1
    came_from = f" (game version {result.from_version})" if result.from_version else ""
    print(f"Restored {len(result.restored)} original file(s){came_from} into {result.game_dir}:")
    for rel in result.restored:
        print(f"  ✓ {rel}")
    for problem in result.mismatches:
        print(f"  ! {problem}")
    if result.mismatches:
        print("Some restored files differ from what was recorded; use \"Verify integrity of game files\" in Steam to be safe.")
    print("The downgrade backup has been removed.")
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


def _game_unpin(service) -> int:
    try:
        out = service.unpin_game_version()
    except Exception as exc:
        print(f"Cannot unpin: {exc}")
        return 1
    print(out.message)
    for c in out.changes:
        print(f"  {c.field}: {c.old} → {c.new}")
    return 0


def steam(args: list[str]) -> int:
    """Steam integration helpers (currently: add/remove ModSync as a non-Steam game)."""
    from modsync.steam import shortcuts

    sub = args[0] if args else ""
    if sub in {"shortcut", "add-shortcut"}:
        if shortcuts.steam_is_running():
            print("Close Steam first — it rewrites shortcuts.vdf from memory on exit,")
            print("which would discard the change. Then run this again.")
            return 1
        if "--remove" in args:
            paths = shortcuts.remove_modsync_from_steam()
            if not paths:
                print("No 'ModSync' shortcut found in any Steam user's shortcuts.vdf; nothing to remove.")
                return 0
            for p in paths:
                print(f"  ✓ removed 'ModSync' from {p}")
            print("\nRestart Steam and it disappears from your library.")
            return 0
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
        "        (--native uses the local command instead of the Flatpak)\n"
        "  modsync steam shortcut --remove     take it out of the library again"
    )
    return 2


def launch(args: list[str]) -> int:
    """Open ModSync when the game is launched from Steam (the launch hook)."""
    from modsync import launchhook

    sub = args[0] if args else ""
    rest = args[1:]
    if sub == "status":
        st = launchhook.status()
        print(f"Launch hook:    {'on' if st.enabled else 'off'}")
        print(f"Steam runs {st.game.name} with: {st.current_mapping or '(Steam default)'}")
        if st.installed or st.underlying_name:
            print(f"Hands off to:   {st.hands_off_to}" + ("" if st.underlying_exists else "   (missing!)"))
        print(f"Steam running:  {'yes' if st.steam_running else 'no'}")
        print(f"\n{st.summary()}")
        return 0 if st.enabled else 1
    if sub == "enable":
        through = None
        i = 0
        while i < len(rest):
            if rest[i] == "--through" and i + 1 < len(rest):
                through, i = rest[i + 1], i + 2
            else:
                print("usage: modsync launch enable [--through <compat-tool-name>]")
                return 2
        try:
            print(launchhook.enable(through=through))
        except RuntimeError as exc:
            print(f"Cannot enable the launch hook: {exc}")
            return 1
        return 0
    if sub == "disable":
        try:
            print(launchhook.disable())
        except RuntimeError as exc:
            print(f"Cannot disable the launch hook: {exc}")
            return 1
        return 0
    if sub == "hub":
        return _launch_hub(rest)
    print(
        "usage:\n"
        "  modsync launch status                show whether Play opens ModSync first\n"
        "  modsync launch enable [--through T]  open ModSync when the game is launched from Steam\n"
        "  modsync launch disable               restore the previous launch behaviour\n"
        "  modsync launch hub --appid N         (run by Steam) the pre-launch window"
    )
    return 2


def _launch_hub(args: list[str]) -> int:
    """What the hook runs. Exit 0 continues the launch, 10 cancels it; any
    other failure also continues, so ModSync can never keep a game from starting."""
    from modsync import launchhook

    appid: int | None = None
    through: str | None = None
    i = 0
    while i < len(args):
        if args[i] == "--appid" and i + 1 < len(args):
            try:
                appid = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        elif args[i] == "--through" and i + 1 < len(args):
            through, i = args[i + 1], i + 2
        else:
            i += 1
    try:
        from modsync.ui.app import run_hub
    except ImportError as exc:
        print(f"ModSync hub unavailable (PySide6 missing: {exc}); continuing the launch.")
        return launchhook.EXIT_CONTINUE
    return run_hub(appid=appid, through=through)
