"""**Home**: is everything ready, and the big button to play.

A readiness checklist (game version, SKSE, Mod Organizer 2, sync) sits next
to the main action. Each check opens the section that can fix it. Before
anything is set up, the main action starts the setup wizard instead.

When Steam's launch hook opened ModSync, Steam is waiting on this window:
Play becomes **Continue**, which closes ModSync and lets the hook hand the
same launch on (to MO2-LINT's redirector or the game's Proton), and
**Cancel launch** returns to Steam. ModSync's own launches are left out then:
they would start a second Proton in the prefix next to the one Steam is about
to run, or outlive the launch Steam is tracking.
"""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout

from modsync import launchhook
from modsync.games import SKYRIM_SE
from modsync.service import GameStatus, SyncStatus
from modsync.ui.pages import Page
from modsync.ui.widgets import HeroTile, Panel, StatusRow, Tile, label

TAGLINE = f"Set up {SKYRIM_SE.name} for modding on this machine."


class HomePage(Page):
    key = "home"
    label = "Home"
    icon = "home"

    def __init__(self, host) -> None:
        super().__init__(host)
        state = self.service.state
        steam = host.steam_launch
        self.head = self.header(
            "Launched from Steam" if steam else (state.instance_label if state.has_instance else "Welcome"),
            SKYRIM_SE.name,
            state.instance_path if state.has_instance else TAGLINE,
        )
        self.steam_note = None
        self.sync_warning = label("", "warning")
        self.sync_warning.setVisible(False)
        if steam is not None:
            self.steam_note = label(
                f"\"{steam.continue_label}\" goes on to {steam.hands_off_to}. \"Cancel launch\" or closing "
                "ModSync returns to Steam without starting anything.", "note")
            self.content_layout.addWidget(self.steam_note)
        self.content_layout.addWidget(self.sync_warning)

        left, right = self.columns(10, 11)
        checks = Panel(spacing=10)
        checks.add(label("READY TO PLAY?", "eyebrow", wrap=False))
        self.game_row = StatusRow("Game version", "mountain")
        self.skse_row = StatusRow("Script Extender (SKSE)", "layers")
        self.mo2_row = StatusRow("Mod Organizer 2", "box")
        self.sync_row = StatusRow("Sync", "sync")
        for row, target in ((self.game_row, "game"), (self.skse_row, "game"), (self.mo2_row, "mods"),
                            (self.sync_row, "sync")):
            row.clicked.connect(lambda _=False, t=target: host.go(t))
            row.set_state("Checking", "off")
            checks.add(row)
        left.addWidget(checks)
        left.addStretch(1)

        actions = QVBoxLayout()
        actions.setSpacing(14)
        self.next_step = Tile("", "", "wrench", role="primary", chevron=True)
        self.next_step.clicked.connect(lambda: host.go("game"))
        self.next_step.hide()
        self._recommendation = ""
        actions.addWidget(self.next_step)
        self.play: Tile
        self.cancel_launch: Tile | None = None
        self.open_mo2: Tile | None = None
        self.setup: Tile | None = None
        if steam is not None:
            self.play = HeroTile(steam.continue_label, f"Carry on to {steam.hands_off_to}.", "play")
            self.play.clicked.connect(lambda: host.decide_launch(launchhook.EXIT_CONTINUE))
            self.cancel_launch = Tile("Cancel launch", "Close ModSync and return to Steam without starting "
                                      "anything.", "close")
            self.cancel_launch.clicked.connect(lambda: host.decide_launch(launchhook.EXIT_CANCEL))
            actions.addWidget(self.play)
            actions.addWidget(self.cancel_launch)
            if not state.has_instance:
                self.setup = Tile("Setup wizard", "Choose or install Mod Organizer 2 first.", "layers")
                self.setup.clicked.connect(lambda: host.start_setup())
                actions.addWidget(self.setup)
        elif state.has_instance:
            self.play = HeroTile("Play Skyrim", "Use your selected MO2 profile and SKSE when installed.", "play")
            self.play.clicked.connect(lambda: host.launch(play=True))
            self.open_mo2 = Tile("Open Mod Organizer 2", "Manage mods, profiles and load order.", "box")
            self.open_mo2.clicked.connect(lambda: host.launch(play=False))
            actions.addWidget(self.play)
            actions.addWidget(self.open_mo2)
        else:
            self.setup = HeroTile("Set up ModSync", "Three steps: Mod Organizer 2, sync (optional), and the "
                                  "game version.", "rocket")
            self.setup.clicked.connect(lambda: host.start_setup())
            self.play = Tile("Play Skyrim", "Choose a Mod Organizer 2 instance first.", "play")
            self.play.setEnabled(False)
            actions.addWidget(self.setup)
            actions.addWidget(self.play)
        self.profile = label("", "muted")
        self.profile.setVisible(False)
        actions.addWidget(self.profile)
        actions.addStretch(1)
        right.addLayout(actions)
        self.finish_layout()
        self._play_description = self.play.description

        if not state.has_instance:
            self.mo2_row.set_state("Not chosen", "warn", "Pick or install an instance to play through it.")
        else:
            self.mo2_row.set_state("Ready", "ok", state.instance_label)
        if state.syncing:
            self.sync_row.set_state("Starting", "busy")
        else:
            self.sync_row.set_state("Off", "off", "Optional: keep this setup on another machine.")

        host.gameChecked.connect(self.on_game_checked)
        host.syncStatus.connect(self.on_sync_status)
        host.setupDescribed.connect(self.on_setup_described)
        host.busyChanged.connect(self.update_enabled)
        self.update_enabled()

    def preferred_focus(self):
        if self.setup is not None and self.host.steam_launch is None:
            return self.setup
        if self.next_step.isVisibleTo(self):
            return self.next_step
        return self.play

    # --- readiness -----------------------------------------------------------------
    def on_game_checked(self, st: GameStatus, vc) -> None:
        if vc.installed is None:
            self.game_row.set_state("Not found" if st.game_dir is None else "Unknown", "warn",
                                    "Install Skyrim through Steam." if st.game_dir is None else "")
        elif st.steam_updating:
            self.game_row.set_state(str(vc.installed), "busy", "Steam is updating the game.")
        elif st.needs_downgrade:
            self.game_row.set_state(f"Needs {st.wanted}", "warn", f"Installed: {vc.installed}.")
        elif st.needs_pin:
            self.game_row.set_state(str(vc.installed), "warn", "Steam has an update waiting.")
        else:
            self.game_row.set_state(str(vc.installed), "ok", "Matches your mods." if st.wanted else "")
        skse = st.skse_state
        if skse == "ok":
            self.skse_row.set_state("Ready", "ok", f"Built for {st.skse_runtime}.")
        elif skse in ("wrong", "several"):
            self.skse_row.set_state("Mismatch", "warn", "Install the build for this game version.")
        elif skse == "missing":
            self.skse_row.set_state("Not installed", "off", "SKSE mods won't load without it.")
        else:
            self.skse_row.set_state("After the game", "off", "Fix the game version first.")
        self.recommend_next_step(st, vc)

    def recommend_next_step(self, st: GameStatus, vc) -> None:
        """Recommend a repair without blocking an intentional launch or moving
        focus away from a choice the user has already made."""
        title = detail = risk = ""
        if self.service.state.has_instance or self.host.steam_launch is not None:
            if st.needs_downgrade or (vc.mismatch and not st.steam_updating):
                wanted = st.wanted or vc.expected
                title = "Fix game version"
                detail = f"Your mods need Skyrim {wanted}. Installed: {vc.installed}."
                risk = f"Mods built for {wanted} may not load if you play now."
            elif st.skse_state in ("wrong", "several"):
                title = "Fix Script Extender (SKSE)"
                detail = "SKSE does not match the installed game."
                risk = "SKSE mods may not load if you play now."
            elif st.needs_pin and not st.pending_pin:
                title = "Keep this game version"
                detail = "Steam has an update waiting. Keep the version your mods use."
                risk = "Steam may update Skyrim when you launch."
        was_recommended = bool(self._recommendation)
        had_focus = self.host.focusWidget() is self.next_step
        self._recommendation = title
        if title:
            self.next_step.setText(title)
            self.next_step.set_description(detail)
        self.next_step.setVisible(bool(title))
        if isinstance(self.play, HeroTile):
            self.play.set_role("normal" if title else "primary")
        self.play.set_description(risk or self._play_description)
        # Only the initial recommendation takes default focus, and only before
        # anything has been pressed: the check finishes a moment after the
        # window opens, and an A meant for Continue must not land on the repair.
        if (self.host._current == "home" and self.host.scope() is self.host.chrome
                and self.host.router.mode != "mouse"):
            if title and not was_recommended and self.host.focusWidget() is self.play and self.host.untouched:
                self.next_step.setFocus()
            elif not title and had_focus and self.play.isEnabled():
                self.play.setFocus()

    def on_setup_described(self, lines: list[str]) -> None:
        main = [line for line in lines if not line.startswith("⚠")]
        issues = [line for line in lines if line.startswith("⚠")]
        if main:
            self.profile.setText(main[0])
            self.profile.setVisible(True)
        if issues:
            self.mo2_row.set_state("Check", "warn", issues[0].lstrip("⚠ "))
        elif main:
            self.mo2_row.set_state("Ready", "ok", main[0])

    def on_sync_status(self, status: SyncStatus) -> None:
        state = status.folder_state or "starting"
        pct = int(round(status.completion or 0))
        online = sum(1 for d in status.devices if d.connected)
        detail = f"{online} of {len(status.devices)} device(s) online" if status.devices else "No other devices yet."
        if pct >= 100 and state == "idle":
            self.sync_row.set_state("Up to date", "ok", detail)
        else:
            self.sync_row.set_state(f"{pct}%", "busy", f"{state.capitalize()}. {detail}")
        if self.host.steam_launch is not None:
            arriving = state == "syncing" or pct < 100
            self.sync_warning.setVisible(arriving)
            if arriving:
                self.sync_warning.setText(
                    f"⚠ Still syncing ({pct}% here). Mods may still be arriving, and continuing now uses "
                    "whatever has arrived so far.")

    # --- buttons ------------------------------------------------------------------------
    def update_enabled(self, *_args) -> None:
        busy = self.host.busy
        self.next_step.setEnabled(not busy)
        if self.host.steam_launch is not None:
            # Neither start the game nor walk away while game files are rewritten.
            self.play.setEnabled(not busy)
            self.cancel_launch.setEnabled(not busy)
            if self.setup is not None:
                self.setup.setEnabled(not busy)
            return
        launcher = self.service.launcher
        if self.open_mo2 is not None:  # what this page was built for, not what is set up now
            ready = not busy
            self.play.setEnabled(ready and not launcher.running(play=True))
            self.open_mo2.setEnabled(ready and not launcher.running(play=False))
        elif self.setup is not None:
            self.setup.setEnabled(not busy)
