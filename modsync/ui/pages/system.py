"""The **System** section: how ModSync fits into this machine.

The background service, the Steam launch hook ("Open ModSync before
Skyrim"), the firewall rules for pairing and syncing, the Steam shortcut,
diagnostics for bug reports, the setup wizard and a reset. Each toggle shows
its current state as a pill, so the answer to "is it on?" is on the tile.
"""

from __future__ import annotations

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QGridLayout

from modsync import __version__, background, diagnostics, firewall, launchhook, steamos
from modsync.steam import shortcuts
from modsync.ui import worker
from modsync.ui.input import Action
from modsync.ui.overlays import DetailsSheet
from modsync.ui.pages import Page
from modsync.ui.widgets import GlyphLabel, Panel, Tile, label


class SystemPage(Page):
    key = "system"
    label = "System"
    icon = "gear"

    def __init__(self, host) -> None:
        super().__init__(host)
        self.header("System", "On this machine",
                    "How ModSync fits in here: Steam, the background service and the firewall.")
        left, right = self.columns(1, 1)
        state = self.service.state
        what = (
            "keeps syncing and applies a queued Steam pin the moment Steam exits"
            if state.syncing
            else "applies a queued Steam pin the moment Steam exits and notices when Steam updates the game"
        )
        self._bg_what = f"A user service that starts at login and {what}, even when ModSync is closed."
        self.bg_tile = Tile("Run in background", self._bg_what, "moon")
        self.bg_tile.set_badge("checking", "off")
        self.bg_tile.clicked.connect(self._toggle_bg)
        self.bg_installed = False
        self.hook_tile = Tile("Open ModSync before Skyrim", "Steam launch: checking…", "rocket")
        self.hook_tile.set_badge("checking", "off")
        self.hook_tile.clicked.connect(self._toggle_hook)
        self.hook: launchhook.LaunchHookStatus | None = None
        self.fw_tile = Tile("Allow in firewall…", "", "shield")
        self.fw_tile.clicked.connect(self._toggle_firewall)
        self.fw_tile.setVisible(False)
        self.steam_tile = Tile("Add ModSync shortcut to Steam", "Add ModSync as a non-Steam shortcut in your "
                               "library, including Gaming Mode. Close Steam first.", "plus-box")
        self.steam_tile.clicked.connect(self._add_to_steam)
        for tile in (self.bg_tile, self.hook_tile, self.fw_tile, self.steam_tile):
            left.addWidget(tile)
        left.addStretch(1)

        self.diag_tile = Tile("Copy diagnostics", "Copy the doctor report and recent logs for a bug report. "
                              "Pairing codes and keys are redacted.", "clipboard", size="compact")
        self.diag_tile.clicked.connect(self._copy_diagnostics)
        self.wizard_tile = Tile("Setup wizard", "Go through the setup one step at a time.", "layers",
                                size="compact")
        self.wizard_tile.clicked.connect(lambda: host.start_setup())
        right.addWidget(self.wizard_tile)
        right.addWidget(self.diag_tile)
        self.reset_tile = None
        if state.has_instance:
            self.reset_tile = Tile("Reset setup…", "Forget the instance and any sync on this machine. Mods are "
                                   "not deleted.", "undo", role="danger", size="compact")
            self.reset_tile.clicked.connect(self._reset)
            right.addWidget(self.reset_tile)
        self.quit_tile = Tile("Quit ModSync", "Close the app. B on Home does the same.", "power", size="compact")
        self.quit_tile.clicked.connect(host.request_quit)
        right.addWidget(self.quit_tile)
        right.addWidget(self._controls_panel())
        right.addStretch(1)
        self.finish_layout()

        self._refresh_bg()
        self._refresh_hook()
        self.firewall: firewall.Check | None = None
        self._refresh_firewall()
        host.busyChanged.connect(self._on_busy)

    def _controls_panel(self) -> Panel:
        panel = Panel(margins=20, spacing=10)
        panel.add(label("CONTROLS", "eyebrow", wrap=False))
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        router = self.host.router
        for row, (key, text) in enumerate((
            ("dpad", "Move between tiles"),
            ("accept", "Choose"),
            ("back", "Back, or quit from Home"),
            ("prev_tab", "Previous section"),
            ("next_tab", "Next section"),
        )):
            grid.addWidget(GlyphLabel(key, router), row, 0)
            grid.addWidget(label(text, "secondary", wrap=False), row, 1)
        grid.setColumnStretch(1, 1)
        panel.layout_.addLayout(grid)
        panel.add(label(f"ModSync {__version__}. A mouse, touch screen or keyboard work too.", "muted"))
        return panel

    def _on_busy(self, busy: bool) -> None:
        self.wizard_tile.setEnabled(not busy)
        if self.reset_tile is not None:
            self.reset_tile.setEnabled(not busy)

    # --- reset ---
    def _reset(self) -> None:
        if self.host.busy:
            return

        def chosen(key: str | None) -> None:
            if key != "reset":
                return
            self.host.change_setup(self.service.reset, message="Resetting…")

        self.host.confirm(
            "Reset setup",
            "Start over on this machine? This forgets the chosen instance, stops any syncing and clears "
            "ModSync's setup so you can set it up differently. Your mods, downloads and profiles are not "
            "deleted; every file stays on disk.",
            [("reset", "Reset setup", "", "danger", "power"), ("cancel", "Cancel", "", "normal", "close")],
            chosen,
            default="cancel",
            eyebrow="System",
        )

    # --- firewall ---
    def _refresh_firewall(self) -> None:
        worker.run_async(firewall.check, self.service.state.firewall_rules_stamp,
                         on_done=self.on_firewall_checked, on_failed=lambda _: None)

    def on_firewall_checked(self, chk: firewall.Check) -> None:
        self.firewall = chk
        self.host.firewall_check = chk
        fw = chk.firewall
        self.fw_tile.setVisible(fw is not None)
        self.fw_tile.setEnabled(True)
        if fw is None:
            return
        ports = ", ".join(f"{p}/{proto}" for p, proto, _ in firewall.PORTS)
        if chk.allowed:
            self.fw_tile.setText("Remove firewall rules…")
            self.fw_tile.set_description(f"ModSync's ports are allowed in {fw.kind}. This deletes the rules "
                                         "ModSync added and asks for your password.")
            self.fw_tile.set_badge("Allowed", "ok")
        else:
            self.fw_tile.setText("Allow in firewall…")
            self.fw_tile.set_description(f"{fw.kind} is on and blocks pairing and syncing until ModSync's "
                                         f"ports ({ports}) are allowed. Asks for your password.")
            self.fw_tile.set_badge("Blocked", "warn")

    def _toggle_firewall(self) -> None:
        chk = self.firewall
        if chk is None or chk.firewall is None:
            return
        self.fw_tile.setEnabled(False)
        if chk.allowed:
            self.host.notify(f"Removing ModSync's rules from {chk.firewall.kind}…")
            worker.run_async(firewall.revoke, chk.firewall,
                             on_done=lambda _: self._after_firewall("", "Firewall rules removed."),
                             on_failed=lambda m: self._on_firewall_failed(m, remove=True))
        else:
            self.host.notify(f"Adding ModSync's rules to {chk.firewall.kind}…")
            worker.run_async(firewall.allow, chk.firewall,
                             on_done=lambda stamp: self._after_firewall(
                                 str(stamp or ""), "Firewall: ModSync's ports are now allowed."),
                             on_failed=self._on_firewall_failed)

    def _after_firewall(self, stamp: str, message: str) -> None:
        self.service.state.firewall_rules_stamp = stamp
        self.service.state.save()
        self.host.notify(message, "ok")
        self._refresh_firewall()  # re-read the rules rather than assume

    def _on_firewall_failed(self, message: str, *, remove: bool = False) -> None:
        self.fw_tile.setEnabled(True)
        if "cancelled" in message:
            self.host.notify("Firewall unchanged.")
            return
        fw = (self.firewall.firewall if self.firewall else None) or firewall.Firewall("ufw")
        DetailsSheet(self.host, "Could not change the firewall",
                     f"{message}\n\nIn a terminal, run:\n\n" + firewall.manual_instructions(fw, remove=remove),
                     eyebrow="Firewall").open()

    # --- launch hook ---
    def _toggle_hook(self) -> None:
        self.hook_tile.setEnabled(False)
        st = self.hook
        turning_off = bool(st and (st.installed or st.selected) and not (st.pending and st.pending.action == "select"))
        fn = launchhook.disable if turning_off else launchhook.enable
        worker.run_async(fn, on_done=self._after_hook, on_failed=self._on_hook_failed)

    def _after_hook(self, message: str) -> None:
        self.hook_tile.setEnabled(True)
        self.host.notify(message, "ok")
        self._refresh_hook()

    def _on_hook_failed(self, message: str) -> None:
        self.hook_tile.setEnabled(True)
        self.host.notify(f"⚠ {message}")
        self._refresh_hook()

    def _refresh_hook(self) -> None:
        worker.run_async(launchhook.status, on_done=self.on_hook_status,
                         on_failed=lambda _: self.hook_tile.set_badge("unknown", "off"))

    def on_hook_status(self, st: launchhook.LaunchHookStatus) -> None:
        self.hook = st
        if st.enabled and not st.pending:
            head, tone = "On", "ok"
        elif st.pending:
            head = "At reboot" if steamos.is_steam_frame() else "When Steam closes"
            tone = "info"
        elif st.installed or st.selected:
            head, tone = "Partly set up", "warn"
        else:
            head, tone = "Off", "off"
        summary = st.summary()
        for prefix in ("On. ", "Off. "):  # the pill already says which
            if summary.startswith(prefix):
                summary = summary[len(prefix):]
        self.hook_tile.set_badge(head, tone)
        if (st.pending and st.pending.action == "select") or st.enabled:
            action = "Choose to turn it off."
        elif st.installed or st.selected:
            action = "Choose to turn it off and reset."
        else:
            action = "Choose to turn it on."
        self.hook_tile.set_description(f"{summary} {action}".strip())
        self.hook_tile.setEnabled(st.steam_found)

    def poll(self) -> None:
        self._refresh_bg()
        if self.hook is not None and self.hook.pending is not None:
            worker.run_async(launchhook.apply_pending, on_done=self._on_hook_applied, on_failed=lambda _: None)

    def _on_hook_applied(self, message: object) -> None:
        if message:
            self.host.notify(str(message), "ok")
            self._refresh_hook()

    # --- background service ---
    def _toggle_bg(self) -> None:
        self.bg_tile.setEnabled(False)
        if self.bg_installed:
            worker.run_async(background.uninstall, on_done=lambda _: self._after_bg("Background service turned off."),
                             on_failed=self._on_bg_failed)
        else:
            worker.run_async(background.install, on_done=self._after_bg, on_failed=self._on_bg_failed)

    def _after_bg(self, message: str) -> None:
        self.bg_tile.setEnabled(True)
        self.host.notify(message, "ok")
        self._refresh_bg()

    def _on_bg_failed(self, message: str) -> None:
        self.bg_tile.setEnabled(True)
        self.host.notify(f"⚠ {message}")
        self._refresh_bg()

    def _refresh_bg(self) -> None:
        # Polled by the timer: fail into the pill, not a toast.
        worker.run_async(background.status, on_done=self.on_bg_status,
                         on_failed=lambda _: self.bg_tile.set_badge("unknown", "off"))

    def on_bg_status(self, st: dict) -> None:
        self.bg_installed = bool(st.get("installed"))
        active = st.get("active", "unknown")
        if active == "active":
            text, tone = "Running", "ok"
        elif self.bg_installed:
            text, tone = str(active).capitalize(), "warn"
        else:
            text, tone = ("Off" if active == "inactive" else str(active).capitalize()), "off"
        self.bg_tile.set_badge(text, tone)
        self.bg_tile.set_description(self._bg_what + (" Choose to turn it off." if self.bg_installed
                                                      else " Choose to turn it on."))

    # --- Steam shortcut ---
    def _add_to_steam(self) -> None:
        if shortcuts.steam_is_running():
            self.host.notify("⚠ Close Steam first, because it rewrites its shortcuts on exit. "
                             "Then choose \"Add ModSync shortcut to Steam\" again.")
            return
        worker.run_async(shortcuts.add_modsync_to_steam, on_done=self._on_steam_added,
                         on_failed=lambda m: self.host.notify(f"⚠ {m}"))

    def _on_steam_added(self, paths: list) -> None:
        if paths:
            self.host.notify(f"Added a ModSync shortcut for {len(paths)} Steam user(s). Start Steam to find it "
                             "in your library and in Gaming Mode.", "ok")
        else:
            self.host.notify("⚠ No Steam users found. Is Steam installed, and has it been run at least once?")

    # --- diagnostics ---
    def _copy_diagnostics(self) -> None:
        self.diag_tile.setEnabled(False)
        self.host.notify("Collecting diagnostics…")
        worker.run_async(diagnostics.build, on_done=self._on_diagnostics, on_failed=self._on_diagnostics_failed)

    def _on_diagnostics(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self.diag_tile.setEnabled(True)
        self.host.notify("Diagnostics copied to the clipboard. Paste them into your bug report.", "ok")

    def _on_diagnostics_failed(self, message: str) -> None:
        self.diag_tile.setEnabled(True)
        self.host.notify(f"⚠ Could not collect diagnostics: {message}")

    def hints(self):
        return []

    def handle_action(self, action: Action) -> bool:
        return False
