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
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["MODSYNC_GAMEPAD"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QScrollArea

from modsync import gameversion, launchhook
from modsync.firewall import Check, Firewall
from modsync.games import SKYRIM_SE
from modsync.mo2 import usvfs
from modsync.pairing_code import PairingCode
from modsync.pairing_lan import Announcement
from modsync.service import DeviceStatus, GameStatus, ModSyncService, SyncStatus
from modsync.state import State
from modsync.ui.app import _application
from modsync.ui.input import InputRouter
from modsync.ui.main_window import MainWindow

INSTANCE = "/home/you/Games/ModOrganizer2-SkyrimSE"


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
        skse_source="skse64_1_5_97.dll in the game folder", backup_present=True,
    )
    check = gameversion.VersionCheck(
        game.installed, None, skse=gameversion.SkseCheck([
            gameversion.SkseFile(Path("skse64_1_5_97.dll"), version("1.5.97"), "game folder")
        ])
    )
    hook = launchhook.LaunchHookStatus(SKYRIM_SE, False, None, False, None, None, False, None, False, True)
    patches = [
        patch.object(State, "load", return_value=State()),
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
        if name.startswith("home") or name == "launch":
            vertical = window.pages["home"].area.verticalScrollBar().maximum()
            print(f"  Home vertical overflow={vertical}")
            if width == 1280 and vertical:
                raise RuntimeError(f"Home needs scrolling on the Steam Deck: {name}")
        if any(overflow):
            raise RuntimeError(f"Content exceeds the requested width: {name}")

    def window_for(state: State, *, steam=None, mode="keyboard") -> MainWindow:
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
        w = window_for(State())
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
        router.pad_style = "xbox"
        w.pages["sync"].add_device()
        capture(w, "keyboard")
        keyboard = w.top_overlay
        keyboard.shift.click()
        capture(w, "keyboard-shift")
        keyboard.shift.click()
        keyboard.symbols.click()
        capture(w, "keyboard-symbols")
        keyboard.symbols.click()
        keyboard.field.setText("/home/you/Games/mod_list")
        keyboard.field.setCursorPosition(20)
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

        steam = launchhook.SteamLaunch(SKYRIM_SE, "mo2_489830_redirector")
        w = window_for(State(**configured, folder_id="example"), steam=steam)
        capture(w, "launch")
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

        w = window_for(State(**configured), mode="mouse")
        capture(w, "compact-mouse", width=1000)
        router.pointer = "touch"  # what the Deck's screen and the Steam Frame's pointer report
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
