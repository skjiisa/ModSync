"""The **Mod Organizer 2** section: the instance this machine uses (choose,
install, open), its profile and mod count, and two repairs: a current Visual
C++ runtime in the game prefix and the USVFS fix for ARM64.

``Mo2Chooser`` (instances found here, browse, install) is also the setup
flow's first step.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget

from modsync import platforms
from modsync.games import SKYRIM_SE, Game
from modsync.mo2 import discover as mo2_discover
from modsync.mo2 import instance as mo2_instance
from modsync.mo2 import usvfs
from modsync.mo2.installers import InstallerBackend, InstallResult, Mo2LintBackend
from modsync.ui import theme, worker
from modsync.ui.overlays import FolderSheet, KeyboardSheet, Overlay
from modsync.ui.pages import Page
from modsync.ui.widgets import Panel, Tile, label, touch_scroll


def describe_setup(instance_path: str | None) -> list[str]:
    """The selected profile, its enabled mod count and any instance problems.
    Cheap and file-system only."""
    if not instance_path or not Path(instance_path).is_dir():
        return []
    info = mo2_instance.inspect(instance_path)
    if not info.has_ini:
        return ["Mod Organizer 2 has not been started yet (no ModOrganizer.ini)."]
    enabled: int | None = None
    profiles_dir = info.content_dirs["profiles"].path if "profiles" in info.content_dirs else None
    if profiles_dir and info.selected_profile:
        modlist = profiles_dir / info.selected_profile / "modlist.txt"
        try:
            enabled = sum(1 for line in modlist.read_text(encoding="utf-8", errors="replace").splitlines()
                          if line.startswith("+"))
        except OSError:
            enabled = None
    detail = f"Profile: {info.selected_profile or '(none)'}"
    if enabled is not None:
        detail += f"  ·  {enabled} mods enabled"
    return [detail, *(f"⚠  {issue}" for issue in info.issues)]


def scan_instances() -> list[str]:
    plat = platforms.current()
    found = mo2_discover.discover_instances(plat.mo2_broad_roots(), plat.mo2_known_roots())
    return [str(p) for p in found]


class _LineEmitter(QObject):
    """Marshals installer output lines from a worker thread to the UI thread."""

    line = Signal(str)


class InstallSheet(Overlay):
    """Install a fresh Mod Organizer 2 through MO2-LINT, with its output live."""

    def __init__(self, host, on_installed, *, installer: InstallerBackend | None = None,
                 game: Game = SKYRIM_SE) -> None:
        super().__init__(host, "Install Mod Organizer 2", eyebrow="Mod Organizer 2", width=900)
        self._installer = installer or Mo2LintBackend()
        self._game = game
        self._on_installed = on_installed
        self._installing = False
        self._emitter = _LineEmitter()
        self._emitter.line.connect(self._append)
        self.dest = str(Path.home() / f"ModOrganizer2-{game.mo2lint_key}")
        self.dest_tile = Tile("Install to", self.dest, "folder", chevron=True)
        self.dest_tile.clicked.connect(self._browse)
        self.type_tile = Tile("Type a path…", "Enter the folder with the keyboard instead.", "keyboard",
                              size="compact")
        self.type_tile.clicked.connect(self._type)
        self.body.addWidget(self.dest_tile)
        self.body.addWidget(self.type_tile)
        self.body.addWidget(label(
            "Close Steam before installing. This downloads Mod Organizer 2 and sets up the game's Proton "
            "prefix, which can take several minutes. SKSE can be installed afterwards from Game.", "note"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        # Focusable, so focus stays in the sheet while the tiles are disabled
        # and the d-pad and right stick scroll the output.
        self.log.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        touch_scroll(self.log)
        self.log.setVisible(False)
        self.log.setMinimumHeight(180)
        self.body.addWidget(self.log, 1)
        self.run = Tile("Install", "", "download", role="primary")
        self.run.clicked.connect(self.start)
        self.body.addWidget(self.run)

    def hints(self):
        from modsync.ui.input import Action

        if self._installing:
            return [([Action.SCROLL_DOWN], "Scroll log")]
        return super().hints()

    def set_dest(self, path: str) -> None:
        self.dest = path
        self.dest_tile.set_description(path)

    def _browse(self) -> None:
        start = Path(self.dest).parent

        def chosen(folder: Path) -> None:
            self.set_dest(str(folder / f"ModOrganizer2-{self._game.mo2lint_key}"))

        FolderSheet(self.host, "Where should Mod Organizer 2 go?", start, chosen,
                    confirm="Install in this folder",
                    description=f"A new ModOrganizer2-{self._game.mo2lint_key} folder is made inside it.").open()

    def _type(self) -> None:
        KeyboardSheet(self.host, "Install to", "The folder to install Mod Organizer 2 into.",
                      lambda text: self.set_dest(text) if text else None, text=self.dest).open()

    def _append(self, line: str) -> None:
        self.log.appendPlainText(line)

    def start(self) -> None:
        if self._installing:
            return
        ok, reason = self._installer.available()
        self.log.setVisible(True)
        if not ok:
            self.log.appendPlainText(f"Cannot install: {reason}")
            return
        dest = self.dest.strip()
        if not dest:
            return
        self._set_installing(True)
        self.log.clear()
        self.log.appendPlainText(f"Installing MO2 for {self._game.name} to {dest} …")
        worker.run_async(
            self._installer.install,
            self._game,
            dest,
            script_extender=False,
            on_output=self._emitter.line.emit,
            on_done=self._on_done,
            on_failed=self._on_failed,
        )

    def _set_installing(self, on: bool) -> None:
        self._installing = on
        self.busy = on
        self.host.set_busy("install", on)
        for tile in (self.dest_tile, self.type_tile, self.run):
            tile.setEnabled(not on)
        self.run.setText("Installing…" if on else "Try again")
        if on:
            self.log.setFocus(Qt.FocusReason.OtherFocusReason)
        self.host.refresh_hints()

    def _on_done(self, result: InstallResult) -> None:
        self._set_installing(False)
        if result.success and result.instance_path:
            self.log.appendPlainText(f"\n✓ Installed to {result.instance_path}")
            self.dismiss()
            self.host.notify(f"Installed Mod Organizer 2 to {result.instance_path}.", "ok")
            self._on_installed(str(result.instance_path))
        else:
            self.log.appendPlainText(f"\n✗ Install failed: {result.message}")

    def _on_failed(self, message: str) -> None:
        self._set_installing(False)
        self.log.appendPlainText(f"\n✗ {message}")


class Mo2Chooser(QWidget):
    """Pick an instance: one found on this machine, a folder, or a fresh install.
    Once the service has taken it, ``host.instance_chosen`` moves the wizard on
    or rebuilds the window."""

    def __init__(self, host, *, installer: InstallerBackend | None = None) -> None:
        super().__init__()
        self.host = host
        self.service = host.service
        self.installer = installer
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        self.status = label("Looking for Mod Organizer 2 instances…", "secondary")
        v.addWidget(self.status)
        self.found = QVBoxLayout()
        self.found.setSpacing(12)
        v.addLayout(self.found)
        self.found_tiles: list[Tile] = []
        self.browse = Tile("Choose a folder…", "Pick the folder of an existing portable instance.", "folder",
                           chevron=True)
        self.browse.clicked.connect(self._browse)
        self.install = Tile("Install MO2…", f"Set up a fresh Mod Organizer 2 for {SKYRIM_SE.name} here.",
                            "box", chevron=True)
        self.install.clicked.connect(self.open_install)
        v.addWidget(self.browse)
        v.addWidget(self.install)
        worker.run_async(scan_instances, on_done=self._on_found,
                         on_failed=lambda m: self.status.setText(f"Scan failed: {m}"))

    def _on_found(self, paths: list[str]) -> None:
        if not paths:
            self.status.setText("No instances found automatically. Choose a folder, or install a fresh one.")
            return
        self.status.setText("Found on this machine:")
        for i, path in enumerate(paths[:4]):
            tile = Tile(Path(path).name or path, path, "box", role="primary" if i == 0 else "normal", chevron=True)
            tile.clicked.connect(lambda _=False, p=path: self.use(p))
            self.found.addWidget(tile)
            tile.show()  # inherit the chooser's visibility, including when opened later
            self.found_tiles.append(tile)
        if self.host.focusWidget() in (None, self.browse) and self.isVisible():
            self.found_tiles[0].setFocus()

    def _browse(self) -> None:
        start = self.service.state.instance_path or str(Path.home())
        FolderSheet(self.host, "Select your Mod Organizer 2 instance folder", start,
                    lambda path: self.use(str(path)), confirm="Use this instance").open()

    def open_install(self) -> InstallSheet:
        sheet = InstallSheet(self.host, self.use, installer=self.installer)
        sheet.open()
        return sheet

    def use(self, path: str) -> None:
        host = self.host  # the chooser may be gone when this finishes; the window isn't
        host.change_setup(
            self.service.choose_instance,
            path,
            message="Reading the instance…",
            on_done=lambda: host.instance_chosen(path),
        )


class ModsPage(Page):
    key = "mods"
    label = "Mod Organizer"
    icon = "box"

    def __init__(self, host) -> None:
        super().__init__(host)
        state = self.service.state
        self.usvfs_busy = False
        self.open_mo2: Tile | None = None
        self.chooser: Mo2Chooser | None = None
        self.header("Mod Organizer 2", state.instance_label if state.has_instance else "Mod Organizer 2",
                    "" if state.has_instance else
                    "No instance chosen yet. Pick the folder of an existing portable instance, or install a "
                    "fresh one.")
        if state.has_instance:
            self._build_instance()
        else:
            self.chooser = Mo2Chooser(host)
            left, right = self.columns(1, 1)
            left.addWidget(self.chooser)
            left.addStretch(1)
            info = Panel()
            info.add(label("WHY AN INSTANCE", "eyebrow", wrap=False))
            info.add(label(
                "ModSync works with a portable Mod Organizer 2 instance: one folder holding MO2, its "
                "profiles, mods and downloads. That folder is what Play launches and what Sync shares.",
                "secondary"))
            right.addWidget(info)
            right.addStretch(1)
        self.finish_layout()

    # --- instance view ---
    def _build_instance(self) -> None:
        state = self.service.state
        left, right = self.columns()
        card = Panel()
        card.add(label("INSTANCE", "eyebrow", wrap=False))
        card.add(label(state.instance_path, "secondary", selectable=True))
        self.setup_label = card.add(label("", "heading"))
        self.setup_label.setVisible(False)
        self.issues = card.add(label("", "warning"))
        self.issues.setVisible(False)
        if state.syncing:
            card.add(label("Shared through Sync, so it can't be switched while syncing.", "muted"))
        left.addWidget(card)
        worker.run_async(describe_setup, state.instance_path, on_done=self._on_described, on_failed=lambda _: None)

        # USVFS 0.5.7+ needs a current VC++ runtime in the game prefix. Only shown
        # when it's missing; installing it is always the user's click.
        self.runtime_note = label("", "warning")
        self.runtime_note.setVisible(False)
        left.addWidget(self.runtime_note)
        self.usvfs_note = label("", "warning")
        self.usvfs_note.setVisible(False)
        left.addWidget(self.usvfs_note)
        left.addStretch(1)

        if self.host.steam_launch is None:
            self.open_mo2 = Tile("Open Mod Organizer 2", "Open the chosen instance. Play Skyrim on Home uses "
                                 "the profile selected in MO2, with SKSE when it is installed.", "play-circle",
                                 role="primary")
            self.open_mo2.clicked.connect(lambda: self.host.launch(play=False))
            right.addWidget(self.open_mo2)
        folder = Tile("Open instance folder", "Show the instance in the file manager.", "folder")
        folder.clicked.connect(self._open_folder)
        right.addWidget(folder)
        if not state.syncing:
            change = Tile("Change instance…", "Use a different MO2 instance folder on this machine.", "refresh")
            change.clicked.connect(self._change)
            right.addWidget(change)
        self.runtime_tile = Tile("Install Visual C++ runtime…", "Microsoft's 2015-2022 runtime (x64) for the "
                                 "game's Proton prefix, so programs started from MO2 run.", "wrench")
        self.runtime_tile.clicked.connect(self._install_runtime)
        self.runtime_tile.setVisible(False)
        right.addWidget(self.runtime_tile)
        self.usvfs_apply = Tile("Apply ARM64 fix", "Download the verified USVFS fix for MO2 2.5.2, about 13 MB, "
                                "and back up the originals. Close MO2, Skyrim and anything MO2 started first.",
                                "chip")
        self.usvfs_apply.clicked.connect(lambda: self._usvfs_change(False))
        self.usvfs_restore = Tile("Restore original USVFS", "Put the backed-up files back. Games started "
                                  "through MO2 will need the fix again on ARM64.", "undo")
        self.usvfs_restore.clicked.connect(lambda: self._usvfs_change(True))
        for tile in (self.usvfs_apply, self.usvfs_restore):
            tile.setVisible(False)
            right.addWidget(tile)
        right.addStretch(1)
        self._refresh_runtime()
        self.usvfs_refresh()
        self.host.busyChanged.connect(self._update_enabled)

    def preferred_focus(self):
        if self.open_mo2 is not None:
            return self.open_mo2
        if self.chooser is not None and self.chooser.found_tiles:
            return self.chooser.found_tiles[0]
        return None

    def _on_described(self, lines: list[str]) -> None:
        main = [line for line in lines if not line.startswith("⚠")]
        issues = [line for line in lines if line.startswith("⚠")]
        self.setup_label.setText("\n".join(main))
        self.setup_label.setVisible(bool(main))
        self.issues.setText("\n".join(issues))
        self.issues.setVisible(bool(issues))
        self.host.setupDescribed.emit(lines)

    def _open_folder(self) -> None:
        path = self.service.state.instance_path
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _change(self) -> None:
        def use(path: Path) -> None:
            self.host.change_setup(self.service.choose_instance, str(path), message="Reading the instance…")

        FolderSheet(self.host, "Select your Mod Organizer 2 instance folder", self.service.state.instance_path,
                    use, confirm="Use this instance").open()

    def _update_enabled(self, *_args) -> None:
        launching = self.host.launching
        if self.open_mo2 is not None:
            self.open_mo2.setEnabled(not self.host.busy and not self.service.launcher.running(play=False))
        for tile in (self.usvfs_apply, self.usvfs_restore, self.runtime_tile):
            tile.setEnabled(not self.host.busy and not launching)

    # --- VC++ runtime in the game prefix ---
    def _refresh_runtime(self) -> None:
        worker.run_async(self.service.prefix_runtime_problems, on_done=self._on_runtime_checked,
                         on_failed=lambda _: None)

    def _on_runtime_checked(self, problems: list[str]) -> None:
        self.runtime_tile.setVisible(bool(problems))
        self.runtime_note.setVisible(bool(problems))
        self._update_enabled()
        if problems:
            self.runtime_note.setText(
                "This MO2's virtual file system needs a newer Visual C++ runtime in the game's Proton prefix, "
                "or nothing started from MO2 will run (" + "; ".join(problems) + ").")

    def _install_runtime(self) -> None:
        def chosen(key: str | None) -> None:
            if key != "go" or self.host.busy or self.host.launching:
                return
            self.host.set_busy("runtime", True)
            self.host.notify("Installing the Visual C++ runtime into the game's prefix…")
            worker.run_async(self.service.install_prefix_runtime,
                             on_done=lambda msg: self._runtime_finished(msg, "ok"),
                             on_failed=lambda msg: self._runtime_finished(f"⚠ {msg}", "warn"))

        self.host.confirm(
            "Install Visual C++ runtime",
            "ModSync will download Microsoft's Visual C++ 2015-2022 runtime (x64), check it, and install it "
            "silently into Skyrim's Proton prefix, the game's private copy of Windows. The DLLs it replaces "
            "are backed up first. Close MO2 and the game before continuing.",
            [("go", "Install", "", "primary", "download"), ("cancel", "Cancel", "", "normal", "close")],
            chosen,
        )

    def _runtime_finished(self, message: str, tone: str) -> None:
        self.host.set_busy("runtime", False)
        self.host.notify(message, tone)
        self._refresh_runtime()

    # --- USVFS ---
    def usvfs_refresh(self) -> None:
        if not self.usvfs_busy:
            worker.run_async(self.service.usvfs_status, on_done=self._usvfs_checked,
                             on_failed=self._usvfs_check_failed)

    def _usvfs_checked(self, result: usvfs.Status) -> None:
        relevant = result.state != "missing" and (usvfs.is_arm64() or result.can_restore)
        self.usvfs_note.setVisible(relevant)
        self.usvfs_note.setText(result.message)
        theme.role(self.usvfs_note, "note" if result.state in ("patched", "not-needed") else "warning")
        self.usvfs_apply.setVisible(relevant and result.can_apply)
        self.usvfs_restore.setVisible(relevant and result.can_restore)
        self._update_enabled()

    def _usvfs_check_failed(self, message: str) -> None:
        self.usvfs_note.setVisible(usvfs.is_arm64())
        self.usvfs_note.setText(f"Could not check USVFS: {message}")
        self.usvfs_apply.setVisible(False)
        self.usvfs_restore.setVisible(False)

    def _usvfs_change(self, restore: bool) -> None:
        if self.usvfs_busy or self.host.busy:
            return
        self.usvfs_busy = True
        self.host.set_busy("usvfs", True)
        self.usvfs_note.setText("Restoring original USVFS…" if restore else
                                "Downloading and applying the USVFS ARM64 fix…")
        operation = self.service.restore_usvfs if restore else self.service.apply_usvfs_fix
        worker.run_async(operation, on_done=self._usvfs_finished, on_failed=self._usvfs_failed)

    def _usvfs_finished(self, message: str) -> None:
        self._usvfs_done()
        self.host.notify(message, "ok")

    def _usvfs_failed(self, message: str) -> None:
        self._usvfs_done()
        self.host.notify(f"⚠ Could not change USVFS: {message}")

    def _usvfs_done(self) -> None:
        self.usvfs_busy = False
        self.host.set_busy("usvfs", False)
        self.usvfs_refresh()
