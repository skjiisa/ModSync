#!/usr/bin/env python3
"""Render offline UI review screenshots; never reads or changes a real setup.

Run from the checkout: uv run python scripts/preview_ui.py --output scratch/ui-review
The example game, devices and pairing code are synthetic. No daemon is started.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["MODSYNC_GAMEPAD"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QScrollArea

from modsync import dfu, gameversion, launchhook, mo2games
from modsync.firewall import Check, Firewall
from modsync.games import DAGGERFALL, SKYRIM_SE
from modsync.mo2 import usvfs
from modsync.pairing_code import PairingCode
from modsync.pairing_lan import Announcement
from modsync.service import DeviceStatus, GameStatus, ModSyncService, SyncStatus
from modsync.state import State
from modsync.sync import conflicts
from modsync.sync.folders import FolderStatus
from modsync.ui.app import _application
from modsync.ui.input import InputRouter
from modsync.ui.main_window import MainWindow

INSTANCE = "/home/you/Games/ModOrganizer2-SkyrimSE"


def example_conflicts(root: Path) -> list:
    """A profile whose mod list and plugins changed on two machines, and a
    mod's INI. Written to a scratch dir so the review sheet reads real files."""
    stamp = "sync-conflict-20261016-213012-7JNL5T2"
    files = {
        "profiles/Default/modlist.txt": ("+SkyUI\n+Immersive Armors\n-Frostfall\n", "+SkyUI\n+Frostfall\n+Campfire\n"),
        "profiles/Default/plugins.txt": ("*SkyUI_SE.esp\n*Hothtrooper44_ArmorCompilation.esp\n",
                                         "*SkyUI_SE.esp\n*Frostfall.esp\n*Campfire.esm\n"),
        "mods/SkyUI/SKSE/Plugins/SkyUI.ini": ("[General]\nfScale=1.0\n", "[General]\nfScale=0.8\nbCompact=1\n"),
    }
    for rel, (here, there) in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(here)
        path.with_name(f"{path.stem}.{stamp}{path.suffix}").write_text(there)
    return conflicts.find(root)


def capture_restore_points(root: Path, window_for, capture, settle) -> None:
    """The Restore points sheet and a restore's preview, from a scratch
    instance with real restore points (kept under ``root`` too)."""
    from datetime import datetime

    from modsync import config, playing, restore_points

    instance = root / "ModOrganizer2-SkyrimSE"
    for mod in ("SkyUI", "Immersive Armors", "Campfire", "Frostfall"):
        (instance / "mods" / mod).mkdir(parents=True)
    profiles = instance / "profiles"

    def lists(name: str, mods: list[str]) -> None:
        (profiles / name).mkdir(parents=True, exist_ok=True)
        (profiles / name / "modlist.txt").write_text("".join(f"+{m}\n" for m in mods))
        (profiles / name / "plugins.txt").write_text("".join(f"*{m.replace(' ', '')}.esp\n" for m in mods))
        (profiles / name / "settings.ini").write_text("[General]\nLocalSaves=false\n")

    times = iter(datetime(2026, 11, d, h, m) for d, h, m in
                 ((12, 20, 4), (13, 18, 41), (14, 21, 15), (15, 9, 2), (16, 13, 37)))
    clock = Mock(wraps=datetime)
    clock.now.side_effect = lambda: next(times)
    with patch.object(config, "data_dir", return_value=root / "data"), \
            patch.object(restore_points, "datetime", clock), \
            patch("modsync.mo2.usvfs.in_use", return_value=False), \
            patch.object(playing, "game_running", return_value=False):
        clock.fromisoformat = datetime.fromisoformat
        lists("Default", ["SkyUI", "Immersive Armors", "Frostfall", "Campfire"])
        lists("Survival", ["SkyUI", "Frostfall", "Campfire"])
        for reason, change in (("auto", None), ("play", ("Default", ["SkyUI", "Immersive Armors", "Frostfall"])),
                               ("conflict", ("Survival", ["SkyUI", "Frostfall"])),
                               ("play", ("Default", ["SkyUI", "Immersive Armors", "Frostfall", "Wet and Cold"])),
                               ("manual", None)):
            if change is not None:
                lists(*change)
            restore_points.capture(instance, reason, force=True)
        lists("Default", ["SkyUI", "Ordinator"])
        (instance / "mods" / "Frostfall").rmdir()
        w = window_for(State(instance_path=str(instance), instance_label="Skyrim setup", folder_id="example"),
                       mode="gamepad")
        w.go("restore")
        settle()
        w.pages["restore"].focus_default()
        capture(w, "restore")
        w.pages["restore"].rows[-1].click()
        settle()
        w.top_overlay.choose("profile:Default")
        capture(w, "restore-preview")
        w.top_overlay.cancel()
        w.top_overlay.cancel()
        w.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("scratch/ui-review"))
    parser.add_argument("--only", help="comma-separated shot names")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    only = set(args.only.split(",")) if args.only else None
    app = _application([])
    router = InputRouter.instance()
    version = gameversion.GameVersion.parse
    game = GameStatus(
        installed=version("1.6.1170"), expected=None, game_dir=Path("/example/Skyrim"),
        language="english", steam_public_build=1, steam_is_current=True,
        steam_running=False, pending_pin=False, recipe_from="1.6.1170",
        recipe_targets=["1.5.97", "1.6.1170"], skse_runtime=version("1.5.97"),
        skse_source="skse64_1_5_97.dll in the game folder", backup_present=True, backup_bytes=1_140_000_000,
    )
    check = gameversion.VersionCheck(
        game.installed, None, skse=gameversion.SkseCheck([
            gameversion.SkseFile(Path("skse64_1_5_97.dll"), version("1.5.97"), "game folder")
        ])
    )
    hook = launchhook.LaunchHookStatus(SKYRIM_SE, False, None, False, None, None, False, None, False, True)
    # The controller family Steam Input would report: a Steam Deck unless a shot
    # says otherwise, never this machine's own controllers.
    family = {"value": "deck"}
    patches = [
        patch("modsync.ui.steaminput.family", lambda *a, **k: family["value"]),
        patch.object(State, "load", return_value=State()),
        # Nothing is written: choosing a game is remembered nowhere.
        patch.object(State, "update", lambda **changes: State()),
        patch.object(ModSyncService, "reload_state", lambda self: self.state),  # each shot keeps its own
        patch.object(ModSyncService, "game_status", return_value=game),
        patch.object(ModSyncService, "game_version_check", return_value=check),
        patch.object(ModSyncService, "status", return_value=SyncStatus(
            "DEMO", "example", True, "syncing", 64.0,
            [DeviceStatus("DEMO-DECK", "Steam Deck", True), DeviceStatus("DEMO-DESKTOP", "Desktop", False)])),
        patch.object(ModSyncService, "accept_pending", return_value=[]),
        patch.object(ModSyncService, "my_pairing_code",
                     return_value=PairingCode("DEMO-DEVICE", "example", "Skyrim setup")),
        patch.object(ModSyncService, "prefix_runtime_problems", return_value=[]),
        patch.object(ModSyncService, "usvfs_status", return_value=usvfs.Status("missing", "")),
        patch("modsync.ui.pages.system.background.status", return_value={"installed": True, "active": "active"}),
        patch("modsync.ui.pages.system.launchhook.status", return_value=hook),
        patch("modsync.launchhook.upgrade", return_value=None),
        # None of the table's games (modsync.mo2games) unless a shot says otherwise:
        # this machine's Steam and Heroic are not read.
        patch("modsync.mo2games.here", return_value=[]),
        patch("modsync.mo2games.listed", return_value=[]),
        patch("modsync.mo2games.refresh_hooks", return_value=[]),
        patch.object(ModSyncService, "games_elsewhere", lambda self: {}),
        patch.object(mo2games.ManagedGame, "overview", lambda self, hook=True, machines=None: mo2games.Overview(
            None, None, False, None, None, "off", None, None)),
        patch("modsync.ui.pages.system.firewall.check", return_value=Check(Firewall("ufw"), False, "ufw:1")),
        patch("modsync.ui.pages.mods.scan_instances", return_value=[INSTANCE]),
        patch("modsync.ui.pages.mods.describe_setup", return_value=["Profile: Default  ·  42 mods enabled"]),
        patch("modsync.ui.pages.sync.pairing_lan.discover", return_value=[
            Announcement("steamdeck", "192.168.1.31", 21029, "s")]),
    ]
    for item in patches:
        item.start()
    windows = []

    def settle():
        for _ in range(4):
            QThreadPool.globalInstance().waitForDone(5000)
            app.processEvents()
        QTest.qWait(700)  # let slides, the halo, rings and toasts finish moving

    def capture(window, name, width=1280):
        if only and name not in only:
            return
        window.resize(width, 800)
        settle()
        path = args.output / f"{name}.png"
        if not window.grab().save(str(path)):
            raise RuntimeError(f"Could not save {path}")
        overflow = [s.horizontalScrollBar().maximum() for s in window.findChildren(QScrollArea) if s.isVisible()]
        print(f"{path}: {window.width()}×{window.height()}, horizontal overflow={overflow}")
        if name.startswith(("home", "launch")):
            vertical = window.page.area.verticalScrollBar().maximum()  # the Home shown: the game's
            print(f"  Home vertical overflow={vertical}")
            if width == 1280 and vertical:
                raise RuntimeError(f"Home needs scrolling on the Steam Deck: {name}")
        if any(overflow):
            raise RuntimeError(f"Content exceeds the requested width: {name}")

    def window_for(state: State, *, steam=None, mode="keyboard", machine="deck") -> MainWindow:
        family["value"] = machine
        router._family = None
        router._set_mode(mode)
        service = ModSyncService(manager=Mock())
        service.state = state
        window = MainWindow(service=service, steam_launch=steam)
        windows.append(window)
        window.resize(1280, 800)
        window.show()
        window.activateWindow()
        settle()
        window.focus_scope_default()
        settle()
        return window

    configured = dict(instance_path=INSTANCE, instance_label="Skyrim setup")
    try:
        fresh = replace(game, skse_runtime=None)
        with patch.object(ModSyncService, "game_status", return_value=fresh), \
                patch.object(ModSyncService, "game_version_check", return_value=gameversion.VersionCheck(fresh.installed, None)):
            w = window_for(State(), machine="keyboard")  # a desktop with no controller: keys only
            capture(w, "home-welcome")
            w.close()

        w = window_for(State(**configured), mode="gamepad")
        router.pad_style = "xbox"
        capture(w, "home")
        ready = replace(game, expected=game.installed, skse_runtime=game.installed)
        w.pages["home"].on_game_checked(ready, gameversion.VersionCheck(ready.installed, ready.expected))
        w.pages["home"].focus_default()
        capture(w, "home-ready")
        missing = replace(ready, skse_runtime=None)
        with patch.object(ModSyncService, "game_status", return_value=missing):
            w.pages["game"].panel.refresh()
            settle()
        capture(w, "home-skse")
        panel = w.pages["game"].panel
        panel._begin_file_operation("Installing SKSE…")
        capture(w, "home-working")
        panel._end_file_operation()
        settle()
        dfu_install = dfu.Install(Path("/home/you/Games/Daggerfall Unity"))
        dagger = Path("/home/you/.local/share/Steam/steamapps/common/The Elder Scrolls Daggerfall/DF/DAGGER")

        def dfu_overview(installed: bool | None):
            """``None``: Steam doesn't have Daggerfall yet."""
            if installed is None:
                return dfu.Overview(None, dfu.data_problems(None), None, None, None, "off", "off", None)
            hook_on = installed
            status = launchhook.LaunchHookStatus(
                DAGGERFALL, hook_on, "modsync_1812390_proton" if hook_on else None, hook_on, None, None, True,
                None, False, True, native=str(dfu_install.executable) if hook_on else None)
            inst = replace(dfu_install) if installed else None
            return dfu.Overview(dagger, [], inst, str(dagger) if installed else None, status,
                                "on" if installed else "off", "on" if installed else "off", None,
                                "1.1.1" if installed else None, configured_ok=bool(installed))

        for installed, name in ((None, "daggerfall-steam"), (False, "daggerfall-install"), (True, "daggerfall")):
            with patch("modsync.dfu.overview", lambda hook=True, i=installed: dfu_overview(i)), \
                    patch.object(dfu.Install, "version", property(lambda self: "1.1.1")):
                dw = window_for(State(**configured), mode="gamepad")
                router._pad_seen = True  # Gaming Mode: SDL reads the Deck, so X arrives
                router.controllersChanged.emit()
                if installed:
                    dw.pages["home"].on_game_checked(ready, gameversion.VersionCheck(ready.installed, ready.expected))
                    capture(dw, "home-dfu")
                    dw.open_game_picker()
                    capture(dw, "game-switch")
                    dw.top_overlay.choose("daggerfall")
                else:
                    if installed is None:  # the picker offers it, focus staying on Skyrim
                        dw.open_game_picker()
                        capture(dw, "game-switch-steam")
                        dw.top_overlay.cancel()
                    dw.go("daggerfall")
                capture(dw, name)
                if installed is False:  # a Daggerfall Unity installed by hand, its data in ~/.config
                    unity = Path("/home/you/.config/unity3d/Daggerfall Workshop/Daggerfall Unity")
                    dw.pages["daggerfall"]._confirm_adopt(
                        dfu.Existing(dfu.Install(Path("/home/you/Games/DFU 1.1.1")), unity))
                    capture(dw, "daggerfall-existing")
                    dw.top_overlay.cancel()
                dw.close()
                router._pad_seen = False
                router.controllersChanged.emit()
        # Steam's Play on Daggerfall: its page, counting down.
        with patch("modsync.dfu.overview", lambda hook=True: dfu_overview(True)), \
                patch.object(dfu.Install, "version", property(lambda self: "1.1.1")):
            steam = launchhook.SteamLaunch(DAGGERFALL, "proton_experimental")
            dw = window_for(State(**configured), steam=steam, mode="gamepad")
            capture(dw, "countdown-daggerfall")
            steam.decision = 0
            dw.close()
        # A game from the table, as Steam has it: Starfield, with MO2 installed for it.
        from modsync.mo2games.steam import SteamGame

        starfield = mo2games.get("starfield")
        in_steam = SteamGame(starfield.game.appid, Path("/home/you/.local/share/Steam/steamapps/common/Starfield"),
                             Path("/home/you/.local/share/Steam/steamapps"), "23518663")

        def table_overview(installed: bool):
            status = launchhook.LaunchHookStatus(
                starfield.game, installed, f"modsync_{starfield.game.appid}_proton" if installed else None,
                installed, None, None, True, None, False, True)
            instance = Path("/home/you/ModOrganizer2-starfield") if installed else None
            return mo2games.Overview(in_steam, instance, installed, None, status, "on" if installed else "off",
                                     ("1.16.244.0", "1.16.244.0"), None, prefix_ready=True)

        fallout = mo2games.get("fallout4")
        kept = mo2games.Overview(None, None, False, None, None, "kept", None, None, machines=["Steam Deck"],
                                 kept_bytes=3_240_000_000, kept_versions=[("1.11.191.0", "1.11.191.0")])
        for installed, name in ((False, "game-table-install"), (True, "game-table")):
            with patch("modsync.mo2games.here", return_value=[starfield]), \
                    patch("modsync.mo2games.listed", return_value=[starfield, fallout]), \
                    patch.object(starfield, "installs", return_value=[in_steam]), \
                    patch.object(fallout, "overview", lambda hook=True, machines=None: kept), \
                    patch.object(starfield, "overview", lambda hook=True, machines=None, i=installed: table_overview(i)):
                tw = window_for(State(**configured, paired=True), mode="gamepad")
                router._pad_seen = True
                router.controllersChanged.emit()
                if installed:
                    tw.open_game_picker()
                    capture(tw, "game-switch-table")
                    tw.top_overlay.choose("add-game")
                    capture(tw, "game-add")
                    tw.top_overlay.cancel()
                    tw.switch_game(fallout.ui_key)
                    capture(tw, "game-kept")
                    tw.switch_game(starfield.ui_key)
                else:
                    tw.switch_game(starfield.ui_key)
                capture(tw, name)
                tw.close()
                router._pad_seen = False
                router.controllersChanged.emit()
        # Steam's Play on Starfield: its page, counting down.
        with patch.object(starfield, "overview", lambda hook=True, machines=None: table_overview(True)):
            steam = launchhook.SteamLaunch(starfield.game, "proton_experimental")
            tw = window_for(State(**configured), steam=steam, mode="gamepad")
            capture(tw, "countdown-table")
            steam.decision = 0
            tw.close()
        w.activateWindow()
        waiting = replace(game, expected=None, awaiting_vault_version=True)
        with patch.object(ModSyncService, "game_status", return_value=waiting):
            waiting_window = window_for(State(**configured, folder_id="example", awaiting_vault_version=True))
            capture(waiting_window, "home-waiting")
            waiting_window.close()
        w.activateWindow()
        w.pages["home"].on_game_checked(game, check)
        w.go("game")
        capture(w, "game")
        w.pages["game"].panel._start_downgrade()
        capture(w, "game-confirm")
        w.top_overlay.cancel()
        w.top_overlay.cancel()  # game maintenance sheet
        w.go("mods")
        capture(w, "mods")
        w.top_overlay.cancel()
        w.pages["home"].steam_options.click()
        capture(w, "steam-setup")
        w.top_overlay.cancel()
        w.go("sync")
        capture(w, "sync-offer")
        w.pages["sync"].toggle_join()
        capture(w, "sync-join")
        w.pages["sync"].join.ask_pin(Announcement("steamdeck", "192.168.1.31", 21029, "s")).set_pin("0428")
        capture(w, "pin-pad")
        w.top_overlay.cancel()
        w.go("system")
        capture(w, "system")
        w.pages["system"].details_tile.click()
        capture(w, "system-details")
        w.top_overlay.cancel()
        w.pages["system"].controls_tile.click()
        capture(w, "controls")
        w.top_overlay.cancel()
        w.close()

        router.pad_style = "playstation"
        w = window_for(State(**configured, folder_id="example"), mode="gamepad")
        w.go("sync")
        capture(w, "sync-live")
        with tempfile.TemporaryDirectory() as tmp:
            found = example_conflicts(Path(tmp))
            settled = SyncStatus("DEMO", "example", True, "idle", 100.0,
                                 [DeviceStatus("DEMO-DECK", "Steam Deck", True)], conflicts=len(found))
            with patch.object(ModSyncService, "conflicts", return_value=found), \
                    patch.object(ModSyncService, "status", return_value=settled):
                w.pages["sync"].refresh()
                capture(w, "sync-conflicts")
                w.pages["sync"].conflicts_tile.click()
                capture(w, "conflict-sheet")
                w.top_overlay.cancel()
                settle()
        router.pad_style = "xbox"
        w.pages["sync"].pair_join.enter_code()
        capture(w, "keyboard")
        keyboard = w.top_overlay
        keyboard.shift.click()
        capture(w, "keyboard-shift")
        keyboard.shift.click()
        keyboard.symbols.click()
        capture(w, "keyboard-symbols")
        keyboard.symbols.click()
        keyboard.field.setText("MODSYNC1-7QK2-X9PL")
        keyboard.field.setCursorPosition(14)
        keyboard.select.click()
        keyboard.move_cursor(1)
        keyboard.move_cursor(1)
        keyboard.select.setFocus()
        capture(w, "keyboard-editing")
        keyboard.select.setChecked(False)
        keyboard._validate = lambda text: "Enter a valid MODSYNC1- pairing code."
        keyboard.done()
        capture(w, "keyboard-error-compact", width=1000)
        w.top_overlay.cancel()
        with patch("modsync.ui.pages.sync.worker.run_async"), \
                patch("modsync.ui.pages.sync.pairing_lan.make_pin", return_value="042815"):
            w.pages["sync"].pair_network()
        w.pages["sync"].beacon.set_address("192.168.1.20")
        capture(w, "pair-beacon")
        w.pages["sync"].cancel_pairing()
        w.close()

        # A machine with Daggerfall Unity and no Mod Organizer 2.
        w = window_for(State(), mode="gamepad")
        w.go("sync")
        capture(w, "sync-pair")
        dfu_root = "/home/you/Games/Daggerfall Unity"
        games = SyncStatus("DEMO", None, False, None, None, [DeviceStatus("DEMO-DESKTOP", "desktop", True)],
                           game_folders=[FolderStatus(dfu.MODS_FOLDER, dfu_root, "idle", 100.0),
                                         FolderStatus(dfu.SAVES_FOLDER, f"{dfu_root}/modsync-saves", "syncing", 72.0,
                                                      need_items=4)])
        with patch.object(ModSyncService, "status", return_value=games), \
                patch.object(ModSyncService, "my_pairing_code", return_value=PairingCode("DEMO-DEVICE", "", "ModSync")):
            w.close()
            w = window_for(State(game_folders={"dfu-mods": dfu_root, "dfu-saves": f"{dfu_root}/modsync-saves"},
                                 paired=True), mode="gamepad")
            w.go("sync")
            capture(w, "sync-games")
        w.close()

        steam = launchhook.SteamLaunch(SKYRIM_SE, "mo2_489830_redirector")
        w = window_for(State(**configured, folder_id="example"), steam=steam)
        capture(w, "launch")
        with patch.object(ModSyncService, "game_status", return_value=missing):
            w.pages["game"].panel.refresh()
            settle()
            w.pages["home"].play.setFocus()
            capture(w, "launch-skse-optional")
        steam.decision = 0
        w.close()

        # Nothing to decide: Play counts down (Steam's Play set to "countdown", the default).
        steam = launchhook.SteamLaunch(SKYRIM_SE, "GE-Proton10-34")
        with patch.object(ModSyncService, "game_status", return_value=ready), \
                patch.object(ModSyncService, "game_version_check",
                             return_value=gameversion.VersionCheck(ready.installed, ready.expected)):
            w = window_for(State(**configured), steam=steam, mode="gamepad")
            capture(w, "launch-countdown")
            home = w.pages["home"]  # the wait is over: what shows until the window closes
            home.countdown._timer.stop()
            home.countdown.state = "starting"
            home.countdown.cover.start()
            capture(w, "launch-starting")
            steam.decision = 0
            w.close()
            # With a finger: no legends, and a tap anywhere but Play now stops it.
            steam = launchhook.SteamLaunch(SKYRIM_SE, "GE-Proton10-34")
            router.pointer = "touch"
            w = window_for(State(**configured), steam=steam, mode="mouse")
            capture(w, "launch-countdown-touch")
            router.pointer = "mouse"
            steam.decision = 0
            w.close()

        w = window_for(State(), mode="gamepad")
        w.start_setup()
        settle()
        w.setup.focus_default()
        capture(w, "setup-1")
        w.setup.chooser.open_install()
        capture(w, "install")
        w.top_overlay.cancel()
        w.setup.go_to(1)
        capture(w, "setup-2")
        w.setup.go_to(2)
        capture(w, "setup-3")
        w.setup.go_to(0)
        from modsync.ui.overlays import FolderSheet
        FolderSheet(w, "Select your Mod Organizer 2 instance folder", Path(__file__).resolve().parents[1],
                    lambda p: None, confirm="Use this instance").open()
        capture(w, "folder")
        w.top_overlay.cancel()
        w.close()

        with tempfile.TemporaryDirectory() as tmp:
            capture_restore_points(Path(tmp), window_for, capture, settle)

        w = window_for(State(**configured), mode="mouse")
        capture(w, "compact-mouse", width=1000)
        router.pointer = "touch"  # what the Deck's screen and the Steam Frame's pointer report
        router.modeChanged.emit("mouse")  # as a real touch does: relabel "Mouse" as "Touch"
        capture(w, "touch-home")
        w.go("sync")
        w.pages["sync"].toggle_join()
        w.pages["sync"].join.ask_pin(Announcement("steamdeck", "192.168.1.31", 21029, "s")).set_pin("0428")
        router._set_mode("mouse")
        capture(w, "touch-pin-pad")
        w.top_overlay.cancel()
        w.close()
        w = window_for(State(), mode="mouse")
        w.start_setup()
        w.setup.go_to(1)
        capture(w, "touch-setup-2")
        router.pointer = "mouse"
        w.close()
    finally:
        QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()
        for item in reversed(patches):
            item.stop()


if __name__ == "__main__":
    main()
