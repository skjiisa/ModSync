"""Set up, repair and launch a modded Skyrim installation on this machine.

Common actions run here. The readiness rows open maintenance sheets for
installation details, version management and less common repairs.
"""

from __future__ import annotations

from PySide6.QtWidgets import QProgressBar, QVBoxLayout

from modsync import launchhook
from modsync.games import SKYRIM_SE
from modsync.service import GameStatus, SyncStatus
from modsync.ui.input import Action
from modsync.ui.pages import Page
from modsync.ui.widgets import HeroTile, Panel, StatusRow, Tile, label

TAGLINE = "Install modding tools, then open MO2 or play."


class HomePage(Page):
    key = "home"
    label = "Home"
    icon = "home"

    def __init__(self, host) -> None:
        super().__init__(host)
        self.content_layout.setContentsMargins(40, 16, 40, 12)
        self.content_layout.setSpacing(14)
        state = self.service.state
        steam = host.steam_launch
        self._checked: tuple[GameStatus, object] | None = None
        self._described: list[str] = []
        self._repair: Tile | None = None
        self._repair_section = "game"
        self._recommendation = ""
        self.head = self.header(
            "Launched from Steam" if steam else (state.instance_label if state.has_instance else "Welcome"),
            SKYRIM_SE.name,
            "Manage your modding setup on this machine." if state.has_instance else TAGLINE,
        )
        self.steam_note = None
        if steam is not None:
            starts = ("Play and Open MO2 continue Steam's launch, including cloud saves." if state.has_instance else
                      f'"{steam.continue_label}" goes on to {steam.hands_off_to}.')
            self.head.set_subtitle("")
            self.steam_note = label(f"{starts} Cancel launch returns to Steam.", "secondary")
            self.content_layout.addWidget(self.steam_note)
        self.sync_warning = label("", "warning")
        self.sync_warning.hide()
        self.content_layout.addWidget(self.sync_warning)

        left, right = self.columns(1, 1)
        left.setSpacing(12)
        checks = Panel(margins=18, spacing=10)
        checks.add(label("YOUR MODDING SETUP", "eyebrow", wrap=False))
        self.mo2_row = StatusRow("Mod Organizer 2", "box")
        self.game_row = StatusRow("Game version", "mountain")
        self.skse_row = StatusRow("Script Extender (SKSE)", "layers")
        self.sync_row = StatusRow("Sync", "sync")
        for row, target in ((self.mo2_row, "mods"), (self.game_row, "game"),
                            (self.skse_row, "game"), (self.sync_row, "sync")):
            row.clicked.connect(lambda _=False, key=target: host.go(key))
            row.set_state("Checking", "off")
            checks.add(row)
        self.sync_row.setVisible(state.syncing)
        left.addWidget(checks)
        self.sync_setup = Tile("Set up sync", "", "sync", size="compact", chevron=True)
        self.sync_setup.clicked.connect(lambda: host.go("sync"))
        self.sync_setup.setVisible(not state.syncing)
        left.addWidget(self.sync_setup)
        self.steam_options = Tile("Set up Steam launch…", "", "rocket",
                                  size="compact", chevron=True)
        self.steam_options.clicked.connect(lambda: host.pages["system"].open_steam_settings())
        self.steam_options.setVisible(steam is None)
        left.addWidget(self.steam_options)
        self.copy_setup = Tile("Copy setup from another machine…", "", "devices", size="compact", chevron=True)
        self.copy_setup.clicked.connect(lambda: host.start_setup(copy_from_machine=True))
        self.copy_setup.setVisible(not state.has_instance and steam is None)
        left.addWidget(self.copy_setup)
        left.addStretch(1)

        actions = QVBoxLayout()
        actions.setSpacing(12)
        self.next_step = Tile("", "", "wrench", role="primary")
        self.next_step.clicked.connect(self._run_next_step)
        self.next_step.hide()
        if state.has_instance or steam is not None:
            actions.addWidget(self.next_step)
        self.work = label("", "secondary")
        self.work_bar = QProgressBar()
        for widget in (self.work, self.work_bar):
            widget.hide()
            actions.addWidget(widget)
        self.play: Tile
        self.cancel_launch: Tile | None = None
        self.open_mo2: Tile | None = None
        self.setup: Tile | None = None
        self.install_mo2: Tile | None = None
        self.choose_mo2: Tile | None = None
        if state.has_instance:
            self.play = HeroTile("Play Skyrim", "Use your MO2 profile and SKSE when installed.", "play")
            self.play.clicked.connect(lambda: host.launch(play=True))
            self.open_mo2 = Tile("Open Mod Organizer 2", "Manage mods, profiles and load order.", "box")
            self.open_mo2.clicked.connect(lambda: host.launch(play=False))
            actions.addWidget(self.play)
            actions.addWidget(self.open_mo2)
        elif steam is not None:
            self.play = HeroTile(steam.continue_label, f"Carry on to {steam.hands_off_to}.", "play")
            self.play.clicked.connect(lambda: host.decide_launch(launchhook.EXIT_CONTINUE))
            actions.addWidget(self.play)
            self.setup = Tile("Guided setup", "Choose or install Mod Organizer 2.", "layers", size="compact")
            self.setup.clicked.connect(lambda: host.start_setup())
        else:
            self.install_mo2 = HeroTile("Install Mod Organizer 2", "Set up a fresh MO2 installation for Skyrim.",
                                       "download")
            self.install_mo2.clicked.connect(lambda: host.pages["mods"].chooser.open_install())
            self.choose_mo2 = Tile("Use an existing installation…", "Find MO2 here or choose its folder.",
                                   "folder", chevron=True)
            self.choose_mo2.clicked.connect(lambda: host.go("mods"))
            actions.addWidget(self.install_mo2)
            actions.addWidget(self.choose_mo2)
            actions.addWidget(self.next_step)
            self.setup = Tile("Guided setup", "MO2, game and SKSE, then optional sync.", "layers", size="compact")
            self.setup.clicked.connect(lambda: host.start_setup())
            actions.addWidget(self.setup)
            self.play = Tile("Play Skyrim", "Choose a Mod Organizer 2 installation first.", "play", parent=self)
            self.play.setEnabled(False)
            self.play.hide()
        if steam is not None:
            self.cancel_launch = Tile("Cancel launch", "Return to Steam without starting the game.", "close",
                                      size="compact")
            self.cancel_launch.clicked.connect(lambda: host.decide_launch(launchhook.EXIT_CANCEL))
            actions.addWidget(self.cancel_launch)
            if self.setup is not None:
                actions.addWidget(self.setup)
        self.profile = label("", "muted")
        self.profile.setParent(self)
        self.profile.hide()  # The profile is shown in the MO2 readiness row.
        actions.addStretch(1)
        right.addLayout(actions)
        self.finish_layout()
        self._play_description = self.play.description

        self.mo2_row.set_state("Ready" if state.has_instance else "Not chosen", "ok" if state.has_instance else "warn",
                               state.instance_label if state.has_instance else "Install MO2 or choose an existing setup.")
        self.sync_row.set_state("Starting", "busy")
        host.gameChecked.connect(self.on_game_checked)
        host.mo2Checked.connect(self.on_mo2_checked)
        host.syncStatus.connect(self.on_sync_status)
        host.setupDescribed.connect(self.on_setup_described)
        host.busyChanged.connect(self.update_enabled)
        self.update_enabled()

    def preferred_focus(self):
        if self.install_mo2 is not None:
            return self.install_mo2
        if self.next_step.isVisibleTo(self):
            return self.next_step
        return self.play

    def _game_panel(self):
        page = self.host.pages.get("game")
        return page.panel if page is not None else None

    def _mo2_repairs(self) -> list[tuple[Tile, str]]:
        page = self.host.pages.get("mods")
        return page.repairs if page is not None else []

    def on_game_checked(self, st: GameStatus, vc) -> None:
        if vc.installed is None:
            self.game_row.set_state("Not found" if st.game_dir is None else "Unknown", "warn",
                                    "Install Skyrim through Steam." if st.game_dir is None else "Check the game installation.")
        elif st.steam_updating:
            self.game_row.set_state(str(vc.installed), "busy", "Steam is updating the game.")
        elif st.needs_downgrade:
            self.game_row.set_state(f"Needs {st.wanted}", "warn", f"Installed: {vc.installed}.")
        elif st.pending_pin:
            self.game_row.set_state(str(vc.installed), "busy", "Close Steam to keep this version.")
        elif st.needs_pin:
            self.game_row.set_state(str(vc.installed), "warn", "Steam has an update waiting.")
        else:
            self.game_row.set_state(str(vc.installed), "ok", "Matches your mods." if st.wanted else "Choose to manage this version.")
        if st.skse_state == "ok":
            self.skse_row.set_state("Ready", "ok", f"Built for {st.skse_runtime}.")
        elif st.skse_state in ("wrong", "several"):
            self.skse_row.set_state("Mismatch", "warn", "Install the matching SKSE build.")
        elif st.skse_state == "missing":
            self.skse_row.set_state("Not installed", "off", "Recommended for mods that need SKSE.")
        else:
            self.skse_row.set_state("After the game", "off", "Check the game version first.")
        self.recommend_next_step(st, vc)

    def on_mo2_checked(self) -> None:
        self._update_mo2_row()
        if self._checked is not None:
            self.recommend_next_step(*self._checked)

    def recommend_next_step(self, st: GameStatus, vc) -> None:
        """Offer the next action without moving focus after the user has acted."""
        self._checked = (st, vc)
        title = detail = risk = ""
        repair = None
        section, icon = "game", "wrench"
        panel = self._game_panel()
        repairs = self._mo2_repairs()
        if repairs:
            repair, detail = repairs[0]
            title, section = repair.text(), "mods"
            risk = "Skyrim may not start through MO2 until this is fixed."
        elif st.needs_downgrade or (vc.mismatch and not st.steam_updating):
            wanted = st.wanted or vc.expected
            title, icon = "Fix game version", "download"
            detail = f"Your mods need Skyrim {wanted}. Installed: {vc.installed}."
            risk = f"Mods built for {wanted} may not load if you play now."
            repair = panel.offered(panel.downgrade) if panel else None
        elif st.skse_state in ("wrong", "several"):
            title, icon = "Fix Script Extender (SKSE)", "layers"
            detail = "SKSE does not match the installed game."
            risk = "SKSE mods may not load if you play now."
            repair = panel.offered(panel.skse) if panel else None
        elif st.needs_pin and not st.pending_pin:
            title, icon = "Keep this game version", "pin"
            detail = "Steam has an update waiting. Keep the version your mods use."
            risk = "Steam may update Skyrim when you launch."
            repair = panel.offered(panel.pin) if panel else None
        elif st.skse_state == "missing":
            title, icon = "Install Script Extender (SKSE)", "layers"
            detail = "Recommended for mods that need SKSE."
            if st.skse_build is None:
                title = "Check SKSE compatibility"
                detail = "No matching SKSE build is known for this game version."
            repair = panel.offered(panel.skse) if panel else None
        if repair is not None and section == "game":
            title = repair.text()
        was_recommended = bool(self._recommendation)
        had_focus = self.host.focusWidget() is self.next_step
        self._recommendation = title
        self._repair, self._repair_section = repair, section
        if title:
            self.next_step.setText(title)
            self.next_step.set_description(detail)
            self.next_step.set_icon(icon)
            self.next_step.chevron.setVisible(repair is None)
        self.next_step.set_role("normal" if self.install_mo2 is not None else "primary")
        self.next_step.setVisible(bool(title) and not (panel and panel.busy))
        if isinstance(self.play, HeroTile):
            self.play.set_role("normal" if title else "primary")
        self.play.set_description(risk or self._play_description)
        if (self.host._current == "home" and self.host.scope() is self.host.chrome
                and self.host.router.mode != "mouse"):
            if title and not was_recommended and self.host.focusWidget() is self.play and self.host.untouched:
                self.next_step.setFocus()
            elif not title and had_focus and self.play.isEnabled():
                self.play.setFocus()

    def _run_next_step(self) -> None:
        if self.host.busy:
            return
        repair = self._repair
        if repair is not None and not repair.isHidden() and repair.isEnabled():
            repair.click()
        else:
            self.host.go(self._repair_section)

    def on_game_busy(self, on: bool) -> None:
        self.work.setVisible(on)
        self.work_bar.setVisible(on)
        self.next_step.setVisible(bool(self._recommendation) and not on)
        if on:
            self.on_game_progress("Starting…", 0, 0)

    def on_game_progress(self, text: str, value: int, maximum: int) -> None:
        self.work.setText(text)
        self.work_bar.setRange(0, maximum)
        self.work_bar.setValue(value)

    def on_setup_described(self, lines: list[str]) -> None:
        self._described = lines
        main = [line for line in lines if not line.startswith("⚠")]
        if main:
            self.profile.setText(main[0])
        self._update_mo2_row()

    def _update_mo2_row(self) -> None:
        if not self.service.state.has_instance:
            return
        issues = [line for line in self._described if line.startswith("⚠")]
        main = [line for line in self._described if not line.startswith("⚠")]
        repairs = self._mo2_repairs()
        if repairs:
            self.mo2_row.set_state("Needs a fix", "warn", repairs[0][1])
        elif issues:
            self.mo2_row.set_state("Check", "warn", "Choose to check the installation details.")
        elif main and main[0].startswith("Mod Organizer 2 has not been started"):
            self.mo2_row.set_state("Finish setup", "warn", "Open MO2 to finish its setup.")
        elif main:
            self.mo2_row.set_state("Ready", "ok", main[0])

    def on_sync_status(self, status: SyncStatus) -> None:
        state = status.folder_state or "starting"
        pct = int(round(status.completion or 0))
        online = sum(1 for device in status.devices if device.connected)
        detail = f"{online} of {len(status.devices)} devices online" if status.devices else "No other devices yet."
        if pct >= 100 and state == "idle":
            self.sync_row.set_state("Up to date", "ok", detail)
        else:
            self.sync_row.set_state(f"{pct}%", "busy", f"{state.capitalize()}. {detail}")
        if self.host.steam_launch is not None:
            arriving = state == "syncing" or pct < 100
            self.sync_warning.setVisible(arriving)
            if arriving:
                self.sync_warning.setText(f"Still syncing ({pct}%). Continuing uses only the mods that have arrived.")

    def hints(self):
        return [([Action.ALT], "Check again")]

    def handle_action(self, action: Action) -> bool:
        if action == Action.ALT:
            self.recheck()
            return True
        return False

    def recheck(self) -> None:
        if self.host.busy:
            return
        panel = self._game_panel()
        if panel is not None:
            panel.refresh()
        mods = self.host.pages.get("mods")
        if mods is not None:
            mods.recheck()

    def update_enabled(self, *_args) -> None:
        busy = self.host.busy
        for tile in (self.next_step, self.setup, self.install_mo2, self.choose_mo2, self.copy_setup,
                     self.steam_options, self.sync_setup):
            if tile is not None:
                tile.setEnabled(not busy)
        if self.host.steam_launch is not None:
            for tile in (self.play, self.open_mo2, self.cancel_launch):
                if tile is not None:
                    tile.setEnabled(not busy)
            return
        launcher = self.service.launcher
        if self.open_mo2 is not None:
            self.play.setEnabled(not busy and not launcher.running(play=True))
            self.open_mo2.setEnabled(not busy and not launcher.running(play=False))
