"""The **Game** section: which Skyrim runtime is installed, which one this
setup needs, and the actions that fix a mismatch (downgrade with community
patches, pin the Steam manifest, install the matching SKSE, re-record the
version) and undo them (restore the original files, unpin).

It needs nothing but Steam, so it works before an MO2 instance is chosen. The
setup flow's last step shows the same ``GamePanel``, and Home runs its repair
tiles directly and mirrors their progress.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QProgressBar, QVBoxLayout, QWidget

from modsync import gameversion
from modsync.downgrade.engine import Progress
from modsync.games import SKYRIM_SE
from modsync.service import GameStatus, PinOutcome
from modsync.ui import theme, worker
from modsync.ui.input import Action
from modsync.ui.overlays import DetailsSheet
from modsync.ui.pages import Page
from modsync.ui.widgets import Panel, Tile, label, pill, set_pill


class _ProgressBridge(QObject):
    """Marshals engine progress callbacks from the worker thread to the UI."""

    progressed = Signal(object)


class GamePanel(QWidget):
    status = Signal(str)  # one-line messages for the host's toasts
    changed = Signal()  # the game files or the setup record were modified
    busyChanged = Signal(bool)  # a downgrade, restore or SKSE install is rewriting game files
    checked = Signal(object, object)  # GameStatus, VersionCheck after every check
    progressed = Signal(str, int, int)  # a file operation's text, value and maximum (0: no estimate)

    def __init__(self, host, *, refresh_index: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.host = host
        self.service = host.service
        self._game: GameStatus | None = None
        self._busy = False
        # Steam's launch is waiting while the launch hook has ModSync open: use
        # the recipe index already on disk rather than fetching the latest one.
        self._refresh_index = refresh_index
        self.details = ""

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(24)

        # --- readout ---
        readout = Panel()
        readout.add(label("INSTALLED", "eyebrow", wrap=False))
        self.version = readout.add(label("…", "bignum", wrap=False))
        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.wanted_chip = pill()
        self.skse_chip = pill()
        self.steam_chip = pill()
        for chip in (self.wanted_chip, self.skse_chip, self.steam_chip):
            chip.setVisible(False)
            chips.addWidget(chip)
        chips.addStretch(1)
        readout.layout_.addLayout(chips)
        self.message = readout.add(label("Checking game version…", "note"))
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        readout.add(self.progress)
        self.progress_label = readout.add(label("", "secondary"))
        self.progress_label.setVisible(False)
        row.addWidget(readout, 11, Qt.AlignmentFlag.AlignTop)

        # --- actions ---
        actions = QVBoxLayout()
        actions.setSpacing(12)
        self.downgrade = self._tile(
            "Downgrade…", "Switch Skyrim to the version your mods need. Original files are backed up.",
            "download", self._start_downgrade,
            role="primary")
        self.skse = self._tile(
            "Install SKSE", "Download the matching build from skse.silverlock.org and replace the old SKSE.", "layers",
            self._install_skse)
        self.pin = self._tile(
            "Keep this version", "Keep Steam from updating this version. Applied after Steam closes.", "pin",
            self._pin_version)
        self.unpin = self._tile(
            "Unpin", "Let Steam update Skyrim again. Close Steam first.", "undo", self._unpin_version)
        self.adopt = self._tile(
            "Use this machine's version", "After changing Skyrim on purpose, record this version for your mods.",
            "check", self._adopt_version)
        self.restore = self._tile(
            "Restore original files", "Undo the downgrade using the backed-up files.", "undo",
            lambda: self._restore_files())
        self.discard = self._tile(
            "Delete leftover backup", "Steam has replaced these files. Delete the old backup to free disk space.", "trash",
            self._discard_backup)
        self.refresh_tile = self._tile(
            "Check again", "Re-read the installed version, SKSE and Steam's update state.", "refresh",
            self.refresh, size="compact")
        self.details_tile = self._tile(
            "Technical details", "Where these versions come from, and the files behind them.", "info",
            self._show_details, size="compact")
        for tile in (self.downgrade, self.skse, self.pin, self.unpin, self.adopt, self.restore, self.discard):
            tile.setVisible(False)
            actions.addWidget(tile)
        actions.addWidget(self.refresh_tile)
        actions.addWidget(self.details_tile)
        actions.addStretch(1)
        row.addLayout(actions, 9)

        self._meta_stamp: int | None = None
        self.refresh()

    def _tile(self, title, description, icon, slot, *, role="normal", size="normal") -> Tile:
        tile = Tile(title, description, icon, role=role, size=size)
        tile.clicked.connect(slot)
        return tile

    @property
    def action_tiles(self) -> list[Tile]:
        return [self.downgrade, self.skse, self.pin, self.unpin, self.adopt, self.restore, self.discard]

    def offered(self, tile: Tile) -> Tile | None:
        """``tile`` when the last check offered it, for running it from elsewhere."""
        return tile if not tile.isHidden() and not self._busy else None

    # --- data flow ----------------------------------------------------------
    @property
    def game(self) -> GameStatus | None:
        return self._game

    def refresh(self) -> None:
        if self.busy:
            return
        self._meta_stamp = self._vault_meta_stamp()
        self.refresh_tile.setEnabled(False)
        worker.run_async(
            self.service.game_status,
            refresh_index=self._refresh_index,
            on_done=self._on_game_status,
            on_failed=self._on_check_failed,
        )

    def poll(self) -> None:
        """Called by the host's timer: apply a queued Steam pin once Steam exits,
        and re-check the version when the vault's record changes under us —
        that's how a machine copying its mods learns what they were built for."""
        if self._vault_meta_stamp() != self._meta_stamp:
            self.refresh()
        if self._game is None or not self._game.pending_pin:
            return
        worker.run_async(self.service.apply_pending_pin, on_done=self._on_pending_pin_applied,
                         on_failed=lambda _: None)

    def _vault_meta_stamp(self) -> int | None:
        path = self.service.state.instance_path
        if not path:
            return None
        try:
            return gameversion.VaultMeta.path(path).stat().st_mtime_ns
        except OSError:
            return None

    def _on_check_failed(self, message: str) -> None:
        self.refresh_tile.setEnabled(not self.busy)
        theme.role(self.message, "warning")
        self.message.setText("Could not check Skyrim's version. Try \"Check again\".")
        self.details = message

    def _on_game_status(self, st: GameStatus) -> None:
        self.refresh_tile.setEnabled(not self.busy)
        self._game = st
        vc = self.service.game_version_check()
        has_instance = self.service.state.has_instance
        lines = []
        warning = False
        self.version.setText(str(vc.installed) if vc.installed is not None else "—")
        if vc.installed is None and st.game_dir is None:
            lines.append("Skyrim wasn't found. Install it through Steam first.")
        elif vc.installed is None:
            lines.append("Could not read Skyrim's version. Try \"Check again\".")
            warning = True
        else:
            if st.steam_updating:
                lines.append("Steam is updating Skyrim. Wait for it to finish, then check again.")
            elif vc.mismatch:
                lines.append(f"⚠ Your mod setup needs version {vc.expected}. Some mods may not work yet.")
                warning = True
            elif vc.skse_suggests:
                lines.append(f"⚠ SKSE needs Skyrim {vc.skse_suggests}.")
                warning = True
            elif vc.ok:
                lines.append("Matches the version your mod setup needs.")

            build = st.skse_build
            fix = (
                f" Choose \"Install SKSE {build.version}\"."
                if build and build.downloadable
                else f" Get SKSE for {vc.installed} from {build.page.split('/')[2]} with \"Get SKSE…\"."
                if build
                else ""
            )
            if st.skse_state == "several":
                lines.append(
                    "Several SKSE versions are in the game folder, and only one can run."
                    + (fix or " Keep the one built for the installed game.")
                )
                warning = True
            elif st.skse_state == "wrong":
                lines.append(f"⚠ The installed SKSE is built for Skyrim {st.skse_runtime}, not {vc.installed}.{fix}")
                warning = True
            elif st.skse_state == "missing":
                lines.append(f"SKSE is not installed here. SKSE mods will not load without it.{fix}")

        if st.suggested_target:
            lines.append(f"Choose \"Downgrade to {st.suggested_target}\" to switch versions.")
        elif st.needs_downgrade:
            if st.recipe_from and str(st.installed) != st.recipe_from and str(st.wanted) in st.recipe_targets:
                lines.append(f"Update Skyrim in Steam first, then return here to switch to {st.wanted}.")
            else:
                lines.append(f"ModSync can't switch this install to {st.wanted} yet.")

        if st.pending_pin:
            lines.append("Close Steam to apply \"Keep this version\".")
        elif st.needs_pin:
            lines.append("Steam has an update ready. Choose \"Keep this version\" to stay on this version.")
            warning = True
        if st.backup_present and st.backup_stale:
            lines.append(
                f"Steam has re-installed Skyrim {st.backup_from or st.installed}, so the backup from your "
                f"last downgrade ({st.backup_bytes / 1e9:.1f} GB) is no longer needed."
            )
        elif st.backup_present:
            came_from = f" ({st.backup_from})" if st.backup_from else ""
            lines.append(f"\"Restore original files\" puts back the files{came_from} from before your last downgrade.")

        theme.role(self.message, "warning" if warning else "note")
        self.message.setText("\n".join(lines))
        details = [vc.summary(), vc.skse_note()]
        if st.recipe_from:
            details.append(f"Available downgrade patches start from Skyrim {st.recipe_from}.")
        self.details = "\n".join(detail for detail in details if detail)
        self._update_chips(st, vc)

        self.adopt.setVisible(has_instance and vc.installed is not None and not vc.ok and not self._busy)
        target = st.suggested_target
        self.downgrade.setVisible(target is not None and not self._busy)
        if target:
            self.downgrade.setText(f"Downgrade to {target}…")
        self.pin.setVisible(st.needs_pin and not st.pending_pin and not self._busy)
        self.unpin.setVisible(st.can_unpin and not self._busy)
        self.restore.setVisible(st.backup_present and not st.backup_stale and not self._busy)
        self.discard.setVisible(st.backup_present and st.backup_stale and not self._busy)
        build = st.skse_build
        self.skse.setVisible(build is not None and not self._busy)
        if build is not None:
            if build.downloadable:
                self.skse.setText(f"Install SKSE {build.version}")
                self.skse.set_role("primary" if target is None else "normal")
            else:
                self.skse.setText("Get SKSE…")
                self.skse.set_role("normal")
                self.skse.set_description(f"SKSE for {vc.installed} is only offered on {build.page.split('/')[2]}. "
                                          "This opens its download page.")
        self.checked.emit(st, vc)

    def _update_chips(self, st: GameStatus, vc) -> None:
        if st.wanted is not None and st.installed is not None:
            set_pill(self.wanted_chip, f"Needs {st.wanted}", "ok" if st.wanted == st.installed else "warn")
        else:
            set_pill(self.wanted_chip, "", "off")
        skse_state = st.skse_state
        if skse_state == "ok":
            set_pill(self.skse_chip, "SKSE ready", "ok")
        elif skse_state in ("wrong", "several"):
            set_pill(self.skse_chip, "SKSE mismatch", "warn")
        elif skse_state == "missing":
            set_pill(self.skse_chip, "No SKSE", "off")
        else:
            set_pill(self.skse_chip, "", "off")
        if st.steam_updating:
            set_pill(self.steam_chip, "Steam updating", "busy")
        elif st.pending_pin:
            set_pill(self.steam_chip, "Pin queued", "info")
        elif st.needs_pin:
            set_pill(self.steam_chip, "Update waiting", "warn")
        elif st.pinned_by_modsync:
            set_pill(self.steam_chip, "Pinned", "info")
        else:
            set_pill(self.steam_chip, "", "off")

    def _show_details(self) -> None:
        DetailsSheet(self.host, "Game version details", self.details or "Nothing checked yet.").open()

    # --- downgrade ----------------------------------------------------------
    def _start_downgrade(self) -> None:
        if self.busy:
            return
        st = self._game
        target = st.suggested_target if st else None
        if not st or not target:
            return
        if st.backup_present and not st.backup_stale:
            # The engine would refuse: a new downgrade overwrites the only copy
            # of those originals. Let the user pick instead of failing later.
            self._resolve_backup_then_downgrade(st)
            return
        self._confirm_downgrade(st, target)

    def _resolve_backup_then_downgrade(self, st: GameStatus) -> None:
        came_from = f"Skyrim {st.backup_from} " if st.backup_from else ""

        def chosen(key: str | None) -> None:
            if key == "restore":
                self._restore_files(confirm=False)
            elif key == "discard":
                self._begin_file_operation("Deleting the old backup…")
                worker.run_async(
                    self.service.discard_downgrade_backup,
                    on_done=self._on_backup_discarded_for_downgrade,
                    on_failed=self._on_discard_failed,
                )

        self.host.confirm(
            "Files from an earlier downgrade are still backed up",
            f"ModSync still has the {came_from}files it set aside before your last downgrade "
            f"({st.backup_bytes / 1e9:.1f} GB), and they differ from what is installed now. "
            "Downgrading again would replace that backup.",
            [("restore", "Restore them first", "Put those files back, then downgrade from there.", "primary", "undo"),
             ("discard", "Delete backup and downgrade", "Only if you no longer need those files.", "danger", "trash"),
             ("cancel", "Cancel", "", "normal", "close")],
            chosen,
        )

    def _on_backup_discarded_for_downgrade(self, freed: object) -> None:
        self._busy = False
        self.busyChanged.emit(False)
        self._show_progress(False)
        st = self._game
        if st is None:
            self.refresh()
            return
        st.backup_present = False
        st.backup_stale = False
        target = st.suggested_target
        if target:
            self._confirm_downgrade(st, target)
        else:
            self.refresh()

    def _confirm_downgrade(self, st: GameStatus, target: str) -> None:
        deck_note = (
            " On a Steam Deck, 1.6.x brings back the on-screen keyboard crash; the "
            "\"Steam Deck Keyboard Fix for Skyrim\" SKSE plugin works around it."
            if target.startswith(("1.6.", "1.5."))
            else ""
        )
        why = f"{target} is what the installed SKSE ({st.skse_source}) is built for. " if st.wanted_from == "skse" else ""

        def chosen(key: str | None) -> None:
            if key == "go":
                self._run_downgrade(target)

        self.host.confirm(
            f"Downgrade {SKYRIM_SE.name} to {target}",
            f"{why}This rewrites the {SKYRIM_SE.name} files in Steam's folder from {st.installed} to {target} "
            "using Mulderland's open-source, checksummed patches. About 1 GB is downloaded and kept for next "
            "time. Steam keeps launching the game normally afterwards. The original files are kept in the game "
            f"folder, and \"Restore original files\" undoes the downgrade.{deck_note}",
            [("go", f"Downgrade to {target}", "Takes a few minutes.", "primary", "download"),
             ("cancel", "Cancel", "", "normal", "close")],
            chosen,
            eyebrow="Game version",
        )

    def _run_downgrade(self, target: str) -> None:
        self._begin_file_operation("Starting…")
        bridge = _ProgressBridge(self)
        bridge.progressed.connect(self._on_progress)
        worker.run_async(
            self.service.run_downgrade,
            target,
            lambda p: bridge.progressed.emit(p),
            on_done=self._on_downgraded,
            on_failed=self._on_downgrade_failed,
        )

    def _show_progress(self, on: bool) -> None:
        self.progress.setVisible(on)
        self.progress_label.setVisible(on)

    def _begin_file_operation(self, message: str) -> None:
        self._busy = True
        self.busyChanged.emit(True)
        for tile in self.action_tiles:
            tile.setVisible(False)
        self.refresh_tile.setEnabled(False)
        self._show_progress(True)
        self._set_progress(message, 0, 0)

    def _set_progress(self, text: str, value: int, maximum: int) -> None:
        self.progress.setRange(0, maximum)
        self.progress.setValue(value)
        self.progress_label.setText(text)
        self.progressed.emit(text, value, maximum)

    @property
    def busy(self) -> bool:
        """A downgrade or restore is rewriting game files; don't navigate away."""
        return self._busy

    def _on_progress(self, p: Progress) -> None:
        if p.stage == "download" and p.total:
            self._set_progress(f"Downloading {p.message}: {(p.done or 0) / 1e6:,.0f} / {p.total / 1e6:,.0f} MB",
                               int(100 * (p.done or 0) / p.total), 100)
        elif p.total:
            self._set_progress(f"{p.stage.capitalize()}: {p.message}", p.done or 0, p.total)
        else:
            self._set_progress(f"{p.stage.capitalize()}: {p.message}", 0, 0)

    def _end_file_operation(self) -> None:
        self._busy = False
        self.busyChanged.emit(False)
        self._show_progress(False)
        self.refresh()

    def _on_downgraded(self, result: object) -> None:
        self._end_file_operation()
        version = getattr(result, "installed_version", "?")
        notes = " ".join(getattr(result, "notes", []) or [])
        self.status.emit(f"Downgrade complete. The game now reports {version}. {notes}".strip())
        self.changed.emit()

    def _on_downgrade_failed(self, message: str) -> None:
        self._end_file_operation()
        self.status.emit(f"⚠ Downgrade failed: {message}")

    # --- pin / record -------------------------------------------------------
    def _pin_version(self) -> None:
        worker.run_async(self.service.pin_game_version, on_done=self._on_pinned, on_failed=self._on_error)

    def _on_pinned(self, out: PinOutcome) -> None:
        self.status.emit(out.message)
        self.refresh()

    def _unpin_version(self) -> None:
        worker.run_async(self.service.unpin_game_version, on_done=self._on_pinned, on_failed=self._on_error)

    # --- restore ------------------------------------------------------------
    def _restore_files(self, *, confirm: bool = True) -> None:
        if self.busy:
            return
        if confirm:
            def chosen(key: str | None) -> None:
                if key == "go":
                    self._restore_files(confirm=False)

            self.host.confirm(
                "Restore the original game files",
                f"This moves the {SKYRIM_SE.name} files ModSync backed up before the downgrade back into "
                "Steam's folder, replacing the downgraded ones, and removes the backup. SKSE and other mods "
                "built for the downgraded version will stop working until you downgrade again.",
                [("go", "Restore original files", "", "primary", "undo"),
                 ("cancel", "Cancel", "", "normal", "close")],
                chosen,
                default="cancel",
                eyebrow="Game version",
            )
            return
        self._begin_file_operation("Restoring original files…")
        worker.run_async(self.service.restore_game_files, on_done=self._on_restored,
                         on_failed=self._on_restore_failed)

    def _on_restored(self, result: object) -> None:
        self._end_file_operation()
        restored = getattr(result, "restored", []) or []
        mismatches = getattr(result, "mismatches", []) or []
        version = getattr(result, "from_version", None)
        msg = f"Restored {len(restored)} original file(s)" + (f" (game version {version})." if version else ".")
        if mismatches:
            msg += (
                f" ⚠ {len(mismatches)} differ from what was recorded. Use \"Verify integrity of game "
                "files\" in Steam to be safe."
            )
        self.status.emit(msg)
        self.changed.emit()

    def _on_restore_failed(self, message: str) -> None:
        self._end_file_operation()
        self.status.emit(f"⚠ Restore failed: {message}")

    # --- SKSE ---------------------------------------------------------------
    def _install_skse(self) -> None:
        if self.busy or self._game is None:
            return
        build = self._game.skse_build
        if build is None:
            return
        if not build.downloadable:
            QDesktopServices.openUrl(QUrl(build.page))
            self.status.emit(
                f"Opened the SKSE download page. Unpack SKSE {build.version} into "
                f"{self._game.game_dir}, then choose \"Check again\"."
            )
            return
        self._begin_file_operation(f"Installing SKSE {build.version}…")
        bridge = _ProgressBridge(self)
        bridge.progressed.connect(self._on_progress)
        worker.run_async(
            self.service.install_skse,
            lambda p: bridge.progressed.emit(p),
            on_done=self._on_skse_installed,
            on_failed=self._on_skse_failed,
        )

    def _on_skse_installed(self, result: object) -> None:
        self._end_file_operation()
        build = getattr(result, "build", None)
        removed = getattr(result, "removed", []) or []
        msg = f"Installed SKSE {build.version} for Skyrim {build.runtime}." if build else "Installed SKSE."
        if removed:
            msg += f" Removed the old {', '.join(removed)}."
        self.status.emit(msg)
        self.changed.emit()

    def _on_skse_failed(self, message: str) -> None:
        self._end_file_operation()
        self.status.emit(f"⚠ SKSE install failed: {message}")

    # --- leftover backup ----------------------------------------------------
    def _discard_backup(self) -> None:
        if self.busy:
            return
        self._begin_file_operation("Deleting the old backup…")
        worker.run_async(self.service.discard_downgrade_backup, on_done=self._on_backup_discarded,
                         on_failed=self._on_discard_failed)

    def _on_backup_discarded(self, freed: object) -> None:
        self._end_file_operation()
        mb = int(freed) / 1e6 if isinstance(freed, (int, float)) else 0
        self.status.emit(f"Deleted the old backup ({mb:,.0f} MB freed). The game files were not touched.")

    def _on_discard_failed(self, message: str) -> None:
        self._end_file_operation()
        self.status.emit(f"⚠ Could not delete the old backup: {message}")

    def _on_pending_pin_applied(self, out: object) -> None:
        if out is not None:
            self.status.emit(getattr(out, "message", "Pin applied."))
            self.refresh()

    def _adopt_version(self) -> None:
        worker.run_async(self.service.adopt_local_game_version, on_done=self._on_version_adopted,
                         on_failed=self._on_error)

    def _on_version_adopted(self, meta: object) -> None:
        if meta is None:
            self._on_error("Could not detect the game version on this machine.")
            return
        self.status.emit(
            "Recorded this machine's game version as the one this setup is built for. "
            "Other machines sharing the setup are warned if theirs differs."
        )
        self.refresh()
        self.changed.emit()

    def _on_error(self, message: str) -> None:
        self.status.emit(f"⚠ {message}")


class GamePage(Page):
    key = "game"
    label = "Game"
    icon = "mountain"

    def __init__(self, host) -> None:
        super().__init__(host)
        self.header("Game", f"{SKYRIM_SE.name} version",
                    "SKSE and native DLL mods only load on the exact game version they were built for. "
                    "Steam updates the game silently; this puts it back.")
        self.panel = GamePanel(host, refresh_index=host.steam_launch is None)
        self.panel.status.connect(host.notify)
        self.panel.busyChanged.connect(lambda on: host.set_busy("game", on))
        self.panel.changed.connect(self._on_changed)
        self.panel.checked.connect(host.gameChecked.emit)
        self.content_layout.addWidget(self.panel)
        self.finish_layout()
        host.busyChanged.connect(self._on_host_busy)

    @property
    def busy(self) -> bool:
        return self.panel.busy

    def preferred_focus(self):
        visible = [t for t in self.panel.action_tiles if t.isVisible()]
        return visible[0] if visible else self.panel.refresh_tile

    def hints(self):
        return [([Action.ALT], "Check again")]

    def handle_action(self, action: Action) -> bool:
        if action == Action.ALT:
            self.panel.refresh()
            return True
        return False

    def poll(self) -> None:
        self.panel.poll()

    def _on_host_busy(self, _busy: bool) -> None:
        # Never rewrite game files while something this app launched is running
        # or USVFS is being replaced.
        self.panel.setEnabled(self.panel.busy or not (self.host.launching or self.host.busy_except("game")))

    def _on_changed(self) -> None:
        # A downgrade or a re-record reaches other machines through the vault.
        if self.service.state.syncing:
            worker.run_async(self.service.rescan, on_failed=lambda _: None)
