#!/usr/bin/env python3
"""Render offline UI review screenshots; never reads or changes a real setup.

Run from the checkout: .venv/bin/python scripts/preview_ui.py --output scratch/ui-review
The example game, devices and pairing code are synthetic. No daemon is started.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QScrollArea
from modsync import gameversion, launchhook
from modsync.games import SKYRIM_SE
from modsync.pairing_code import PairingCode
from modsync.service import DeviceStatus, GameStatus, ModSyncService, SyncStatus
from modsync.state import State
from modsync.ui.app import _application
from modsync.ui.dashboard import Dashboard
from modsync.ui.launch_hub import LaunchHub
from modsync.ui.theme import apply_theme
from modsync.ui.wizard import WizardWidget


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("scratch/ui-review"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = _application([])
    version = gameversion.GameVersion.parse
    game = GameStatus(
        installed=version("1.6.1170"), expected=None, game_dir=Path("/example/Skyrim"),
        language="english", steam_public_build=1, steam_is_current=True,
        steam_running=False, pending_pin=False, recipe_from="1.7.104",
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
        patch.object(ModSyncService, "my_pairing_code", return_value=PairingCode("DEMO-DEVICE", "example", "Skyrim setup")),
        patch("modsync.ui.dashboard.background.status", return_value={"installed": False, "active": "inactive"}),
        patch("modsync.ui.dashboard.launchhook.status", return_value=hook),
        patch.object(Dashboard, "_scan_instances", return_value=["/home/you/Games/ModOrganizer2-SkyrimSE"]),
        patch("modsync.ui.wizard.ChooseInstancePage._scan", return_value=["/home/you/Games/ModOrganizer2-SkyrimSE"]),
        patch("modsync.ui.launch_hub.describe_setup", return_value=["Skyrim setup", "Profile: Default · 42 mods enabled"]),
    ]
    for item in patches:
        item.start()
    widgets = []
    try:
        def capture(widget, name, width=1280):
            widgets.append(widget)
            widget.resize(width, 800)
            widget.show()
            for _ in range(3):
                QThreadPool.globalInstance().waitForDone(5000)
                app.processEvents()
            path = args.output / f"{name}.png"
            if not widget.grab().save(str(path)):
                raise RuntimeError(f"Could not save {path}")
            overflow = [s.horizontalScrollBar().maximum() for s in widget.findChildren(QScrollArea)]
            print(f"{path}: {widget.width()}×{widget.height()}, horizontal overflow={overflow}")
            if widget.width() != width or any(overflow):
                raise RuntimeError(f"Content exceeds the requested width: {name}")
            if isinstance(widget, Dashboard):
                widget.shutdown()
            if isinstance(widget, LaunchHub):
                widget._timer.stop()
            widget.hide()

        for dark in (False, True):
            apply_theme(app, dark=dark)
            theme = "dark" if dark else "light"
            service = ModSyncService(manager=Mock())
            capture(Dashboard(service), f"dashboard-{theme}")
            service.state = State(instance_path="/home/you/Games/ModOrganizer2-SkyrimSE", instance_label="Skyrim setup")
            capture(Dashboard(service), f"configured-{theme}")
            service.state.folder_id = "example"
            capture(Dashboard(service), f"sync-{theme}")
            capture(LaunchHub(service, through="mo2_489830_redirector"), f"launch-{theme}")
            wizard = WizardWidget(service, installer=Mock())
            capture(wizard, f"welcome-{theme}")
            wizard.open_install()
            wizard._choose._dest_edit.setText("/home/you/Games/ModOrganizer2-SkyrimSE")
            capture(wizard, f"install-{theme}")
            wizard._go_to(3)
            capture(wizard, f"wizard-sync-{theme}")
            service.state = State()
            capture(Dashboard(service), f"compact-{theme}", width=900)
    finally:
        QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()
        for item in reversed(patches):
            item.stop()


if __name__ == "__main__":
    main()
