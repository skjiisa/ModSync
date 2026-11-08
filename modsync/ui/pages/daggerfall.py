"""Daggerfall Unity: set it up from Steam's Daggerfall, start it, and sync its
mods and saves (see modsync.dfu). Home shows it once Daggerfall is chosen in
the game switcher at the top, whether or not Steam has Daggerfall yet.

The rows say how each part stands and change it when pressed: the install row
picks another Daggerfall Unity folder, Steam's Play button switches between
Daggerfall Unity and the original game, and the two sync rows turn mods and
saves sync on and off on this machine. The blue action gets Daggerfall from
Steam until Steam has it, then installs Daggerfall Unity, then plays it.
"Use an existing installation…" (once one is in use, "Use another
installation…") picks a Daggerfall Unity the user installed themselves (see
``dfu.adopt``), whatever state the one in use is in.
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QProgressBar

from modsync import dfu
from modsync.ui import worker
from modsync.ui.input import Action
from modsync.ui.overlays import FolderSheet
from modsync.ui.pages import Page
from modsync.ui.widgets import HeroTile, StatusRow, Tile, label

BUSY_KEY = "dfu"
_SYNC_WORDS = {"on": ("On", "ok"), "off": ("Off", "off"), "stopping": ("Stopping", "busy")}


def _short(path) -> str:
    """A path as people write it: the home folder as ~."""
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


def _library_of(dagger: Path) -> Path:
    """The Steam library a game's folder is in (the folder holding steamapps)."""
    for parent in dagger.parents:
        if parent.name == "steamapps":
            return parent.parent
    return dagger


def _hook_on(hook) -> bool:
    """Whether Steam's Play button starts Daggerfall Unity. What's queued for
    when Steam closes is what Steam will do: that decides, not what it does now."""
    if hook is not None and hook.pending is not None:
        return hook.pending.action == "select"
    return hook is not None and hook.enabled


class _ProgressBridge(QObject):
    """Marshals install progress from the worker thread to the UI."""

    progressed = Signal(object)


class DaggerfallPage(Page):
    key = "daggerfall"
    label = "Daggerfall Unity"
    icon = "gamepad"

    def __init__(self, host) -> None:
        super().__init__(host)
        self.content_layout.setContentsMargins(40, 16, 40, 12)
        self.content_layout.setSpacing(14)
        self.overview: dfu.Overview | None = None
        self._working = False
        self._focus_on_load = False
        self._shown_at = 0.0
        self._steam_opened = False  # sent to Steam's install dialog since this was shown
        self.head = self.header("Daggerfall Unity", "The 1996 game on a modern engine, with mods. "
                                "Set up from your Steam copy of The Elder Scrolls II: Daggerfall.")
        self.note = label("", "warning")
        self.note.hide()
        self.content_layout.addWidget(self.note)

        left, right = self.columns(1, 1)
        left.setSpacing(4)
        self.data_row = StatusRow("Daggerfall in Steam", "mountain")
        self.install_row = StatusRow("Daggerfall Unity", "box")
        self.hook_row = StatusRow("Steam's Play button", "rocket")
        self.mods_row = StatusRow("Mods sync", "sync")
        self.saves_row = StatusRow("Saves sync", "sync")
        for row in (self.data_row, self.install_row, self.hook_row, self.mods_row, self.saves_row):
            row.set_state("Checking", "off")
            left.addWidget(row)
        self.data_row.clicked.connect(self._open_steam_page)
        self.install_row.clicked.connect(self._install_pressed)
        self.hook_row.clicked.connect(self._toggle_hook)
        self.mods_row.clicked.connect(lambda: self._toggle_sync(dfu.MODS_FOLDER))
        self.saves_row.clicked.connect(lambda: self._toggle_sync(dfu.SAVES_FOLDER))
        left.addStretch(1)

        self.steam_tile = HeroTile("Install Daggerfall in Steam",
                                   "It's free. Daggerfall Unity is set up from it.", "download")
        self.steam_tile.clicked.connect(self._open_steam_page)
        self.install_tile = HeroTile("Install Daggerfall Unity",
                                     "The official build, set up to use Steam's Daggerfall.", "download")
        self.install_tile.clicked.connect(self._install)
        self.use_existing = Tile("Use an existing installation…", "Pick the folder Daggerfall Unity is in.",
                                 "folder", chevron=True)
        self.use_existing.clicked.connect(self._choose_existing)
        self.play = HeroTile("Play Daggerfall Unity", "Its startup screen opens first: mods and settings.", "play")
        self.play.clicked.connect(self._play)
        self.mods_folder = Tile("Open the mods folder", "", "folder", size="compact")
        self.mods_folder.clicked.connect(self._open_mods_folder)
        self.work = label("", "secondary")
        self.work_bar = QProgressBar()
        for widget in (self.steam_tile, self.install_tile, self.play, self.mods_folder, self.use_existing,
                       self.work, self.work_bar):
            right.addWidget(widget)
        self.steam_tile.hide()
        self.install_tile.hide()
        self.use_existing.hide()
        self.play.hide()
        self.mods_folder.hide()
        self.work.hide()
        self.work_bar.hide()
        right.addStretch(1)
        self.finish_layout()
        host.busyChanged.connect(self._update_enabled)

    def showEvent(self, event) -> None:  # noqa: N802
        """Read what's set up when the sheet opens, not when the window starts."""
        super().showEvent(event)
        if not self._working:
            self._focus_on_load = True
            self._shown_at = time.monotonic()
            self._steam_opened = False
            self.refresh()

    # --- reading ---
    def refresh(self) -> None:
        worker.run_async(dfu.overview, on_done=self._show, on_failed=lambda e: self.host.notify(f"⚠ {e}"))

    def poll(self) -> None:
        if not self._working and self.isVisible():
            self.refresh()

    def hints(self):
        return [([Action.ALT], "Check again")]

    def handle_action(self, action: Action) -> bool:
        if action == Action.ALT:
            if not self._working:
                self.refresh()
            return True
        return False

    @staticmethod
    def summary(ov: dfu.Overview | None) -> tuple[str, str, str]:
        """How Daggerfall Unity stands, as the game picker shows it: value word, tone, detail.
        ``None`` before it has been read."""
        if ov is None:
            return "Checking", "off", ""
        if ov.install is None and ov.dagger is None:
            return "Not installed", "off", "Get Daggerfall in Steam first. It's free."
        if ov.install is None:
            return "Not installed", "off", "Set it up from Steam's Daggerfall."
        if ov.emulation_problem:
            return "Needs a look", "warn", ov.emulation_problem
        if (ov.problems and not ov.configured_ok) or ov.version_problem or ov.stale_path:
            return "Needs a look", "warn", ov.version_problem or "Its Daggerfall data needs attention."
        synced = [what for what, state in (("mods", ov.mods), ("saves", ov.saves)) if state == "on"]
        return "Ready", "ok", f"Syncing {' and '.join(synced)}." if synced else ""

    def _show(self, ov: dfu.Overview) -> None:
        self.overview = ov
        self.host.dfu_checked(ov)
        inst = ov.install
        if ov.own_data:
            self.data_row.set_state("Not used", "off",
                                    f"Daggerfall Unity uses the copy in {_short(ov.configured)}.")
        elif ov.dagger is None and self._steam_opened:
            self.data_row.set_state("Waiting for Steam", "busy", "This updates once Steam has installed it.")
        elif ov.dagger is None:
            # Not there yet is no fault, unless Daggerfall Unity is installed and needs it.
            self.data_row.set_state("Not installed", "off" if ov.install is None else "warn",
                                    "Install The Elder Scrolls II: Daggerfall in Steam. It's free.")
        elif ov.problems:
            self.data_row.set_state("Incomplete", "warn", ov.problems[0])
        else:
            self.data_row.set_state("Ready", "ok", f"In {_short(_library_of(ov.dagger))}")
        if inst is None and ov.dagger is None:
            self.install_row.set_state("Not installed", "off", "Set up from Steam's Daggerfall once it's there.")
        elif inst is None:
            self.install_row.set_state("Not installed", "off", "Install it with the blue button.")
        elif ov.stale_path:
            self.install_row.set_state("Points elsewhere", "warn", "Press to point it at Steam's Daggerfall again.")
        else:
            self.install_row.set_state(ov.version or "Installed", "ok", _short(inst.path))
        hook = ov.hook
        if inst is None:
            self.hook_row.set_state("Original game", "off",
                                    "Starts the original game until Daggerfall Unity is installed.")
        elif hook is not None and hook.pending is not None:
            self.hook_row.set_state("Waiting for Steam", "busy", "Applied once Steam closes.")
        elif hook is not None and hook.enabled:
            self.hook_row.set_state("Daggerfall Unity", "ok", "Press to have it start the original game again.")
        else:
            self.hook_row.set_state("Original game", "off", "Press to have it start Daggerfall Unity.")
        paired = self.service.reload_state().paired
        for row, state, what in ((self.mods_row, ov.mods, "mods"), (self.saves_row, ov.saves, "saves")):
            word, tone = _SYNC_WORDS[state]
            detail = {"on": f"Your {what} sync with your paired machines." if paired else
                      "No machine is paired yet. Pair one under Sync.",
                      "off": f"Press to sync your {what} with your paired machines.",
                      "stopping": "Finishes once Syncthing answers."}[state]
            row.set_state(word, tone, detail)
        notes = [n for n in (ov.emulation_problem, ov.version_problem) if n]
        self.note.setText(" ".join(notes))
        self.note.setVisible(bool(notes))
        self.steam_tile.setVisible(inst is None and ov.dagger is None and not self._working)
        self.install_tile.setVisible(inst is None and ov.dagger is not None and not self._working)
        # Always there, so another install can be chosen whatever state this one is in.
        self.use_existing.setVisible(not self._working)
        self.use_existing.setText("Use another installation…" if inst is not None else "Use an existing installation…")
        self.play.setVisible(inst is not None and not self._working)
        self.mods_folder.setVisible(inst is not None)
        if inst is not None:
            self.mods_folder.set_description(_short(inst.mods_dir))
        self._update_enabled()
        if self._focus_on_load:
            # Shown before this was read: the blue action wasn't there to take focus.
            self._focus_on_load = False
            focused = self.focusWidget()
            # Only while nothing was pressed since this was shown, and nothing covers it:
            # a press in flight must land where it aimed.
            settled = (self.host.router.last_input < self._shown_at and self.host.page is self
                       and self.host.scope() is self.host.chrome)
            if settled and (focused is None or focused is self.data_row):
                target = self.preferred_focus()
                if target is not None and target.isVisible():
                    target.setFocus()

    def _update_enabled(self, *_args) -> None:
        ov = self.overview
        busy = self.host.busy or self._working
        ready = ov is not None and not ov.problems
        has = ov is not None and ov.install is not None
        self.install_tile.setEnabled(ready and not busy)
        # Installed and pointing at usable data, the row picks another install, which needs no Steam data.
        choosing = has and not ov.stale_path
        self.install_row.setEnabled((ready or choosing) and not busy)
        self.use_existing.setEnabled(ov is not None and not busy)
        self.play.setEnabled(has and not busy)
        for row in (self.hook_row, self.mods_row, self.saves_row):
            row.setEnabled(has and not busy)

    def preferred_focus(self):
        if self.steam_tile.isVisible():
            return self.steam_tile
        if self.install_tile.isVisible() and self.install_tile.isEnabled():
            return self.install_tile
        if self.play.isVisible() and self.play.isEnabled():
            return self.play
        return self.data_row

    # --- doing ---
    def _run(self, fn, *args, message: str, on_done=None) -> None:
        """Run a change off the UI thread with the window busy, then show
        what it said and read everything again."""
        if self.host.busy or self._working:
            return
        self._working = True
        self.host.set_busy(BUSY_KEY, True)
        self.work.setText(message)
        self.work.show()
        self._update_enabled()

        def finished(result) -> None:
            self._finish()
            text = on_done(result) if on_done is not None else result
            if isinstance(text, str):
                self.host.notify(text)

        def failed(error: str) -> None:
            self._finish()
            self.host.notify(f"⚠ {error}")

        worker.run_async(fn, *args, on_done=finished, on_failed=failed)

    def _finish(self) -> None:
        self._working = False
        self.host.set_busy(BUSY_KEY, False)
        self.work.hide()
        self.work_bar.hide()
        self.refresh()

    def _install(self) -> None:
        ov = self.overview
        if ov is None or ov.problems:
            return
        bridge = _ProgressBridge(self)
        bridge.progressed.connect(self._on_progress)
        self.work_bar.setRange(0, 0)
        self.work_bar.show()
        self.install_tile.hide()

        def done(result) -> str:
            installed, followed = result
            if not installed.fresh:
                return followed or "Daggerfall Unity points at Steam's Daggerfall again."
            return followed or "Daggerfall Unity is installed. Steam's Play button can start it: press that row."

        self._run(lambda: dfu.setup(progress=lambda p: bridge.progressed.emit(p)),
                  message="Installing Daggerfall Unity…", on_done=done)

    def _install_pressed(self) -> None:
        """Installs or re-points while something needs it; otherwise picks another install."""
        ov = self.overview
        if ov is not None and ov.install is not None and not ov.stale_path:
            self._choose_existing()
        else:
            self._install()

    def _choose_existing(self) -> None:
        ov = self.overview
        if ov is None or self.host.busy or self._working:
            return
        games = Path.home() / "Games"
        start = ov.install.path.parent if ov.install is not None else games if games.is_dir() else Path.home()
        FolderSheet(self.host, "Select your Daggerfall Unity folder", start, self._examine,
                    description=f"The folder with {dfu.EXECUTABLE} in it.").open()

    def _examine(self, path: Path) -> None:
        worker.run_async(dfu.examine, path, on_done=self._confirm_adopt,
                         on_failed=lambda e: self.host.notify(f"⚠ {e}"))

    def _confirm_adopt(self, found: dfu.Existing) -> None:
        """Say what changes before it does: data copied into the install, or
        another install taking over this one's syncing and Play button."""
        ov = self.overview
        current = ov.install if ov is not None else None
        if current is not None and current.path == found.install.path:
            self.host.notify("That's the Daggerfall Unity in use already.")
            return
        notes = []
        if current is not None:
            note = f"It replaces the one in {_short(current.path)}, which stays as it is."
            moving = [what for what, on in (("Steam's Play button", _hook_on(ov.hook)), ("mods sync", ov.mods == "on"),
                                            ("saves sync", ov.saves == "on")) if on]
            if moving:
                listed = moving[0] if len(moving) == 1 else f"{', '.join(moving[:-1])} and {moving[-1]}"
                note += f" {listed[0].upper()}{listed[1:]} {'switches' if len(moving) == 1 else 'switch'} to it."
            notes.append(note)
        if found.data_from is not None:
            notes.append(f"Its settings, saves and load order are copied into its folder, and it uses those "
                         f"from now on. The originals in {_short(found.data_from)} stay where they are.")
        if not notes:
            self._adopt(found)
            return
        self.host.confirm(
            "Use this Daggerfall Unity?",
            " ".join(notes),
            [("go", "Use this one", _short(found.install.path), "primary", "check"),
             ("cancel", "Cancel", "", "normal", "close")],
            lambda key: self._adopt(found) if key == "go" else None,
            default="go",
        )

    def _adopt(self, found: dfu.Existing) -> None:
        def done(result) -> str:
            adopted, followed = result
            inst = adopted.install
            version = f" {inst.version}" if inst.version else ""
            parts = [f"Using Daggerfall Unity{version} in {_short(inst.path)}."]
            if adopted.data_from is not None:
                parts.append("Its settings and saves were copied into its folder.")
            if followed:
                parts.append(followed)
            return " ".join(parts)

        self._run(self.service.use_dfu_install, found.install.path, message="Setting up Daggerfall Unity…",
                  on_done=done)

    def _on_progress(self, p) -> None:
        if p.stage == "download" and p.total:
            self.work.setText(f"Downloading: {(p.done or 0) / 1e6:,.0f} / {p.total / 1e6:,.0f} MB")
            self.work_bar.setRange(0, 100)
            self.work_bar.setValue(int(100 * (p.done or 0) / p.total))
        elif p.total:
            self.work.setText("Unpacking…")
            self.work_bar.setRange(0, p.total)
            self.work_bar.setValue(p.done or 0)

    def _toggle_hook(self) -> None:
        ov = self.overview
        if ov is None or ov.install is None:
            return
        if _hook_on(ov.hook):
            self._run(dfu.disable_hook, message="Giving Steam's Play button back to the original game…")
        else:
            self._run(dfu.enable_hook, ov.install, message="Pointing Steam's Play button at Daggerfall Unity…")

    def _toggle_sync(self, spec) -> None:
        ov = self.overview
        if ov is None or ov.install is None:
            return
        state = ov.mods if spec is dfu.MODS_FOLDER else ov.saves
        what = "mods" if spec is dfu.MODS_FOLDER else "saves"
        if state == "off":
            self._run(self.service.share_game_folder, spec.key, message=f"Starting {what} sync…",
                      on_done=lambda _root: f"Your Daggerfall Unity {what} now sync with your paired machines.")
        else:
            self._run(self.service.stop_game_folder, spec.key, message=f"Stopping {what} sync…",
                      on_done=lambda done: (f"Daggerfall Unity {what} no longer sync here. Nothing was deleted."
                                            if done else f"{what.capitalize()} sync stops once Syncthing answers."))

    def _play(self) -> None:
        ov = self.overview
        if ov is None or ov.install is None:
            return
        self._run(dfu.launch, ov.install, message="Starting Daggerfall Unity…")

    def _open_mods_folder(self) -> None:
        if self.overview is None or self.overview.install is None:
            return
        worker.run_async(dfu.open_mods_folder,
                         on_done=lambda path: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))),
                         on_failed=lambda e: self.host.notify(f"⚠ {e}"))

    def _open_steam_page(self) -> None:
        """Steam's install dialog for Daggerfall. This page notices it on its next read."""
        if self.overview is not None and self.overview.dagger is None:
            QDesktopServices.openUrl(QUrl(f"steam://install/{dfu.DAGGERFALL_APPID}"))
            self._steam_opened = True
            self.host.notify("Steam opens to install Daggerfall. This page updates once it's installed.")
            self._show(self.overview)
