"""Cyberpunk 2077 from GOG through the Heroic Games Launcher, modded with Mod
Organizer 2 (see modsync.cyberpunk). Home shows it once Cyberpunk 2077 is
chosen in the game switcher at the top, whether or not Heroic has it yet.

The rows say how each part stands and change it when pressed: the MO2 row
picks another instance, Heroic's Play button switches between playing through
MO2 and the game as Heroic starts it, and the sync row turns mods sync on and
off here. The blue action installs MO2 until there is an instance (or
finishes an install that stopped part way), then plays through it. "Use an
existing installation…" picks an MO2 instance set up for Cyberpunk some other
way. Everything shown comes from one read off the UI thread
(``cyberpunk.overview``); a read that an action has overtaken is dropped.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import shiboken6
from PySide6.QtCore import QObject, Signal

from modsync import cyberpunk
from modsync.ui import worker
from modsync.ui.input import Action
from modsync.ui.overlays import FolderSheet
from modsync.ui.pages import Page
from modsync.ui.pages.daggerfall import _short
from modsync.ui.widgets import HeroTile, StatusRow, Tile, label

BUSY_KEY = "cyberpunk"
_SYNC_WORDS = {"on": ("On", "ok"), "off": ("Off", "off"), "stopping": ("Stopping", "busy")}
_TOOL_LINE = re.compile(r"^\s*(\d{4}-\d\d-\d\d |\$ |$)")  # MO2-LINT's log lines and commands


class _OutputBridge(QObject):
    """Marshals install output from the worker thread to the UI."""

    said = Signal(str)


class CyberpunkPage(Page):
    key = "cyberpunk"
    label = "Cyberpunk 2077"
    icon = "gamepad"

    def __init__(self, host) -> None:
        super().__init__(host)
        self.content_layout.setContentsMargins(40, 16, 40, 12)
        self.content_layout.setSpacing(14)
        self.overview: cyberpunk.Overview | None = None
        self._working = False
        self._focus_on_load = False
        self._shown_at = 0.0
        self._reads = 0  # the latest read's number: older ones are dropped
        self.head = self.header("Cyberpunk 2077", "From GOG, through the Heroic Games Launcher, with mods "
                                "in Mod Organizer 2. Cyber Engine Tweaks and RED4ext load as they should.")
        self.note = label("", "warning")
        self.note.hide()
        self.content_layout.addWidget(self.note)

        left, right = self.columns(1, 1)
        left.setSpacing(4)
        self.game_row = StatusRow("Cyberpunk 2077 in Heroic", "gamepad")
        self.mo2_row = StatusRow("Mod Organizer 2", "box")
        self.hook_row = StatusRow("Heroic's Play button", "rocket")
        self.mods_row = StatusRow("Mods sync", "sync")
        for row in (self.game_row, self.mo2_row, self.hook_row, self.mods_row):
            row.set_state("Checking", "off")
            left.addWidget(row)
        self.game_row.clicked.connect(self.refresh)
        self.mo2_row.clicked.connect(self._choose_existing)
        self.hook_row.clicked.connect(self._toggle_hook)
        self.mods_row.clicked.connect(self._toggle_sync)
        left.addStretch(1)

        self.install_tile = HeroTile("Install Mod Organizer 2",
                                     "Set up for Cyberpunk, with RootBuilder for CET and RED4ext.", "download")
        self.install_tile.clicked.connect(self._install)
        self.play = HeroTile("Play Cyberpunk 2077", "Through Mod Organizer 2, with your mods.", "play")
        self.play.clicked.connect(lambda: self._launch(play=True))
        self.open_mo2 = Tile("Open Mod Organizer 2", "Add, order and turn on mods.", "play-circle")
        self.open_mo2.clicked.connect(lambda: self._launch(play=False))
        self.use_existing = Tile("Use an existing installation…", "Pick an MO2 instance set up for Cyberpunk.",
                                 "folder", chevron=True)
        self.use_existing.clicked.connect(self._choose_existing)
        self.work = label("", "secondary")
        for widget in (self.install_tile, self.play, self.open_mo2, self.use_existing, self.work):
            right.addWidget(widget)
            widget.hide()
        right.addStretch(1)
        self.finish_layout()
        host.busyChanged.connect(self._update_enabled)

    def showEvent(self, event) -> None:  # noqa: N802
        """Read what's set up when the sheet opens, not when the window starts."""
        super().showEvent(event)
        if not self._working:
            self._focus_on_load = True
            self._shown_at = time.monotonic()
            self.refresh()

    # --- reading ---
    def refresh(self) -> None:
        self._reads += 1
        read = self._reads

        def done(ov: cyberpunk.Overview) -> None:
            # Gone with its window (Qt drops only bound methods' connections by
            # itself), or overtaken by a newer read or an action that changed what it read.
            if shiboken6.isValid(self) and read == self._reads and not self._working:
                self._show(ov)

        worker.run_async(cyberpunk.overview, on_done=done, on_failed=lambda e: self.host.notify(f"⚠ {e}"))

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
    def summary(ov: cyberpunk.Overview | None) -> tuple[str, str, str]:
        """How Cyberpunk stands, as the game picker shows it: value word, tone, detail.
        ``None`` before it has been read."""
        if ov is None:
            return "Checking", "off", ""
        if ov.game is None:
            return "Not installed", "off", "Install it from GOG in the Heroic Games Launcher."
        if not ov.prefix_ready:
            return "Not set up", "off", "Launch it once from Heroic."
        if ov.instance is None:
            return "No mods yet", "off", "Install Mod Organizer 2 for it."
        if ov.instance_problem or ov.version_problem:
            return "Needs a look", "warn", ov.instance_problem or ov.version_problem
        if ov.needs_setup:
            return "Needs a look", "warn", "Finish setting up Mod Organizer 2 for it."
        return "Ready", "ok", "Syncing mods." if ov.mods == "on" else ""

    def _show(self, ov: cyberpunk.Overview) -> None:
        self.overview = ov
        self.host.cyberpunk_checked(ov)
        game = ov.game
        if game is None:
            self.game_row.set_state("Not installed", "off", "Install it from GOG in Heroic, then launch it once.")
        elif not ov.prefix_ready:
            self.game_row.set_state("Not set up", "warn", "Launch it once from Heroic, so Heroic sets up its prefix.")
        else:
            version = ov.version[0] if ov.version else (game.version or "Installed")
            pinned = ", kept on this version by Heroic" if game.pinned else ""
            self.game_row.set_state(version, "ok", f"{_short(game.install_path)}{pinned}")
        if ov.instance is None:
            self.mo2_row.set_state("Not installed", "off", "Install it with the blue button.")
        elif ov.instance_problem:
            self.mo2_row.set_state("Needs a look", "warn", ov.instance_problem)
        elif ov.needs_setup:
            self.mo2_row.set_state("Not finished", "warn",
                                   "RootBuilder is missing, so CET and RED4ext won't load. Use the blue button.")
        else:
            self.mo2_row.set_state("Installed", "ok", _short(ov.instance))
        hook = ov.hook
        if ov.instance is None:
            self.hook_row.set_state("Game only", "off", "Starts the game without mods until MO2 is installed.")
        elif hook is not None and hook.enabled:
            self.hook_row.set_state("Mod Organizer 2", "ok", "Press to have it start the game as before.")
        else:
            self.hook_row.set_state("Game only", "off",
                                    "Press to have it play through MO2. Close Heroic first.")
        word, tone = _SYNC_WORDS[ov.mods]
        if ov.mods == "off" and ov.instance is not None and not ov.chosen:
            detail = "Press to use this instance here and sync its mods with your paired machines."
        else:
            detail = {"on": "Your mods sync with your paired machines. Saves sync through the game itself.",
                      "off": "Press to sync your mods with your paired machines.",
                      "stopping": "Finishes once Syncthing answers."}[ov.mods]
        self.mods_row.set_state(word, tone, detail)
        self.note.setText(ov.version_problem or "")
        self.note.setVisible(bool(ov.version_problem))
        ready = game is not None and ov.prefix_ready
        has = ov.instance is not None and not ov.instance_problem
        finishing = has and ov.needs_setup
        self.install_tile.setText("Finish setting up Mod Organizer 2" if finishing else "Install Mod Organizer 2")
        self.install_tile.setVisible(ready and (ov.instance is None or finishing) and not self._working)
        through_heroic = hook is not None and hook.enabled
        self.play.set_description("Through Heroic and Mod Organizer 2, with your mods and GOG cloud saves."
                                  if through_heroic else "Through Mod Organizer 2, with your mods.")
        self.play.setVisible(has and not self._working)
        self.open_mo2.setVisible(has and not self._working)
        self.use_existing.setVisible(not self._working)
        self.use_existing.setText("Use another installation…" if ov.instance is not None
                                  else "Use an existing installation…")
        self._update_enabled()
        if self._focus_on_load:
            # Shown before this was read: the blue action wasn't there to take focus.
            self._focus_on_load = False
            focused = self.focusWidget()
            settled = (self.host.router.last_input < self._shown_at and self.host.page is self
                       and self.host.scope() is self.host.chrome)
            if settled and (focused is None or focused is self.game_row):
                target = self.preferred_focus()
                if target is not None and target.isVisible():
                    target.setFocus()

    def _update_enabled(self, *_args) -> None:
        ov = self.overview
        busy = self.host.busy or self._working
        ready = ov is not None and ov.game is not None and ov.prefix_ready
        has = ready and ov.instance is not None and not ov.instance_problem
        self.install_tile.setEnabled(ready and not busy)
        self.play.setEnabled(has and not busy)
        self.open_mo2.setEnabled(has and not busy)
        self.use_existing.setEnabled(ov is not None and not busy)
        self.mo2_row.setEnabled(ov is not None and not busy)
        self.hook_row.setEnabled(has and not busy)
        # Stopping needs nothing to work; starting needs a usable instance.
        self.mods_row.setEnabled(not busy and ov is not None and (has or ov.mods != "off"))

    def preferred_focus(self):
        for tile in (self.install_tile, self.play):
            if tile.isVisible() and tile.isEnabled():
                return tile
        return self.game_row

    # --- doing ---
    def _run(self, fn, *args, message: str, on_done=None) -> None:
        """Run a change off the UI thread with the window busy, then show
        what it said and read everything again."""
        if self.host.busy or self._working:
            return
        self._working = True
        self._reads += 1  # a read under way now may predate what this changes
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
        self.refresh()

    def _install(self) -> None:
        """Install MO2, or finish the setup of the instance in use: the install
        skips MO2-LINT for an instance that's there and does what's missing."""
        ov = self.overview
        finishing = ov is not None and ov.instance is not None and ov.needs_setup
        dest = ov.instance if finishing else cyberpunk.default_instance_dir()
        bridge = _OutputBridge(self)
        bridge.said.connect(self._on_output)
        self.install_tile.hide()

        def output(line: str) -> None:
            if not _TOOL_LINE.match(line):
                bridge.said.emit(line.strip())

        self._run(lambda: cyberpunk.install_mo2(dest, output),
                  message="Finishing Mod Organizer 2's setup…" if finishing
                  else "Installing Mod Organizer 2 for Cyberpunk… This takes a few minutes.",
                  on_done=lambda instance: f"Mod Organizer 2 is installed in {_short(instance)}. "
                                           "Open it to add mods, or play.")

    def _on_output(self, line: str) -> None:
        if self._working and line:
            self.work.setText(line)

    def _choose_existing(self) -> None:
        ov = self.overview
        if ov is None or self.host.busy or self._working:
            return
        start = ov.instance.parent if ov.instance is not None else Path.home()
        FolderSheet(self.host, "Select your Mod Organizer 2 folder for Cyberpunk", start, self._use,
                    description="The folder with ModOrganizer.exe in it.").open()

    def _use(self, path: Path) -> None:
        """Mods sync, if on, moves along before this finishes (see the service)."""
        self._run(lambda: " ".join(self.service.use_cyberpunk_instance(path)), message="Checking that folder…")

    def _toggle_hook(self) -> None:
        ov = self.overview
        if ov is None or ov.instance is None:
            return
        if ov.hook is not None and ov.hook.enabled:
            self._run(cyberpunk.disable_hook, message="Giving Heroic's Play button back to the game…")
        else:
            self._run(lambda: " ".join(cyberpunk.enable_hook()), message="Pointing Heroic's Play button at MO2…")

    def _toggle_sync(self) -> None:
        ov = self.overview
        if ov is None or (ov.mods == "off" and ov.instance is None):
            return
        key = cyberpunk.MODS_FOLDER.key
        if ov.mods == "off":
            def share():
                if not ov.chosen:  # found, not handed over: pressing here hands it over
                    cyberpunk.use_instance(ov.instance)
                return self.service.share_game_folder(key)

            self._run(share, message="Starting mods sync…",
                      on_done=lambda _root: "Your Cyberpunk mods now sync with your paired machines.")
        else:
            self._run(self.service.stop_game_folder, key, message="Stopping mods sync…",
                      on_done=lambda done: ("Cyberpunk mods no longer sync here. Nothing was deleted."
                                            if done else "Mods sync stops once Syncthing answers."))

    def _launch(self, *, play: bool) -> None:
        ov = self.overview
        if ov is None or ov.instance is None:
            return

        def start() -> str:
            message = cyberpunk.launch(self.service.launcher, play=play)
            return f"{message} {ov.version_problem}" if ov.version_problem else message

        self._run(start, message="Starting Cyberpunk 2077 through MO2…" if play else "Opening Mod Organizer 2…")
