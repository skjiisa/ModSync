"""A game ModSync runs Mod Organizer 2 for (see modsync.mo2games), as Home
shows it once the game is chosen in the switcher at the top. One page class
serves every game from the table; which game, and which store has it, only
change the words.

The rows say how each part stands and change it when pressed: the MO2 row
picks another instance, the store's Play button switches between playing
through MO2 and the game as the store starts it, and the sync row turns mods
sync on and off here. The blue action installs MO2 until there is an instance
(or finishes an install that stopped part way), then plays through it. "Use an
existing installation…" picks an MO2 instance set up for the game some other
way. Everything shown comes from one read off the UI thread
(``ManagedGame.overview``); a read that an action has overtaken is dropped.

A game that isn't installed here can still have a page: when a paired
machine syncs its mods, or a copy of them is kept here for the other
machines. The page then says which machines those are and how much the
copy takes, keeps or stops keeping it, and offers to install the game.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import shiboken6
from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from modsync import steamos
from modsync.games import STORE_NAMES
from modsync.mo2games import ManagedGame, Overview
from modsync.steam import compattools
from modsync.ui import worker
from modsync.ui.input import Action
from modsync.ui.overlays import FolderSheet
from modsync.ui.pages import Page
from modsync.ui.pages.daggerfall import _short
from modsync.ui.widgets import HeroTile, StatusRow, Tile, label

_SYNC_WORDS = {"on": ("On", "ok"), "off": ("Off", "off"), "stopping": ("Stopping", "busy"), "kept": ("Kept", "ok")}
_TOOL_LINE = re.compile(r"^\s*(\d{4}-\d\d-\d\d |\$ |$)")  # MO2-LINT's log lines and commands


class _OutputBridge(QObject):
    """Marshals install output from the worker thread to the UI."""

    said = Signal(str)


def launcher_of(game) -> str:
    """The program that starts the game: "Steam" or "Heroic"."""
    return "Steam" if game.store == "steam" else "Heroic"


def where_to_install(mg: ManagedGame) -> str:
    """Where the game comes from, as a sentence for a machine without it."""
    heroic = " or ".join(name for name, has in (("GOG", mg.game.gog_id), ("Epic", mg.game.epic_id)) if has)
    if mg.game.appid and heroic:
        return f"Install it from Steam, or from {heroic} in the Heroic Games Launcher."
    if heroic:
        return f"Install it from {heroic} in the Heroic Games Launcher."
    return "Install it from Steam."


def machines_text(names: list[str]) -> str:
    """"Steam Deck", "Steam Deck and Laptop", "Steam Deck, Laptop and Desktop"."""
    if not names:
        return "your other machines"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def size_text(size: int | None) -> str:
    if size is None:
        return ""
    if size >= 1e9:
        return f"{size / 1e9:.1f} GB"
    return f"{max(size, 1) / 1e6:.0f} MB" if size >= 1e6 else "under 1 MB"


def kept_text(ov: Overview) -> str:
    """"For Steam Deck · 3.2 GB"."""
    size = size_text(ov.kept_bytes)
    return f"For {machines_text(ov.machines)}" + (f" · {size}" if size else "")


def game_summary(mg: ManagedGame, ov: Overview | None) -> tuple[str, str, str]:
    """How the game stands, as the game picker shows it: value word, tone, detail.
    ``None`` before it has been read."""
    if ov is None:
        return "Checking", "off", ""
    if ov.game is None:
        if ov.mods == "kept":
            return "Kept", "ok", kept_text(ov) + "."
        if ov.machines:
            return "Not here", "off", f"Modded on {machines_text(ov.machines)}."
        return "Not installed", "off", where_to_install(mg)
    if not ov.prefix_ready:
        return "Not set up", "off", f"Launch it once from {launcher_of(ov.game)}."
    if ov.instance is None:
        if ov.mods == "kept":
            return "Kept", "ok", "Install Mod Organizer 2 for it to use the mods kept here."
        if ov.machines:
            return "No mods yet", "off", f"Modded on {machines_text(ov.machines)}. Install Mod Organizer 2 for it."
        return "No mods yet", "off", "Install Mod Organizer 2 for it."
    if ov.instance_problem or ov.version_problem:
        return "Needs a look", "warn", ov.instance_problem or ov.version_problem
    if ov.needs_setup:
        return "Needs a look", "warn", "Finish setting up Mod Organizer 2 for it."
    return "Ready", "ok", "Syncing mods." if ov.mods == "on" else ""


class ManagedGamePage(Page):
    icon = "gamepad"

    def __init__(self, host, mg: ManagedGame) -> None:
        self.mg = mg
        self.key = mg.ui_key
        self.label = mg.name
        self.busy_key = mg.ui_key
        super().__init__(host)
        self.content_layout.setContentsMargins(40, 16, 40, 12)
        self.content_layout.setSpacing(14)
        self.overview: Overview | None = None
        self._working = False
        self._focus_on_load = False
        self._shown_at = 0.0
        self._reads = 0  # the latest read's number: older ones are dropped
        self._pending = 0  # reads under way: a poll waits for them rather than overtaking them
        self.head = self.header(mg.name, mg.summary)
        self.note = label("", "warning")
        self.note.hide()
        self.content_layout.addWidget(self.note)
        self.info = label("", "secondary")  # what the mods kept here are for, before the game is installed
        self.info.hide()
        self.content_layout.addWidget(self.info)

        left, right = self.columns(1, 1)
        left.setSpacing(4)
        self.game_row = StatusRow(mg.name, "gamepad")
        self.mo2_row = StatusRow("Mod Organizer 2", "box")
        self.hook_row = StatusRow("The store's Play button", "rocket")
        self.mods_row = StatusRow("Mods sync", "sync")
        for row in (self.game_row, self.mo2_row, self.hook_row, self.mods_row):
            row.set_state("Checking", "off")
            left.addWidget(row)
        self.game_row.clicked.connect(self.refresh)
        self.mo2_row.clicked.connect(self._choose_existing)
        self.hook_row.clicked.connect(self._toggle_hook)
        self.mods_row.clicked.connect(self._toggle_sync)
        left.addStretch(1)

        self.store_tile = HeroTile(f"Install {mg.name} in Steam", "", "download")
        self.store_tile.clicked.connect(self._open_store)
        self.install_tile = HeroTile("Install Mod Organizer 2", mg.install_note(), "download")
        self.install_tile.clicked.connect(self._install)
        self.play = HeroTile(f"Play {mg.name}", "Through Mod Organizer 2, with your mods.", "play")
        self.play.clicked.connect(lambda: self._launch(play=True))
        self.open_mo2 = Tile("Open Mod Organizer 2", "Add, order and turn on mods.", "play-circle")
        self.open_mo2.clicked.connect(lambda: self._launch(play=False))
        self.use_existing = Tile("Use an existing installation…", f"Pick an MO2 instance set up for {mg.short}.",
                                 "folder", chevron=True)
        self.use_existing.clicked.connect(self._choose_existing)
        self.work = label("", "secondary")
        for widget in (self.store_tile, self.install_tile, self.play, self.open_mo2, self.use_existing, self.work):
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
        """Read everything again. A read under way is overtaken: refresh is
        for after an action, or when the page opens. Polls go through
        :meth:`poll`, which waits for one under way, so a slow Syncthing
        (asked which machines sync the game) can't keep the page unread."""
        self._reads += 1
        self._pending += 1
        read = self._reads

        def landed() -> bool:
            if not shiboken6.isValid(self):  # gone with its window
                return False
            self._pending -= 1
            return True

        def done(ov: Overview) -> None:
            # Overtaken by a newer read, or by an action that changed what it read.
            if landed() and read == self._reads and not self._working:
                self._show(ov)

        def failed(error: str) -> None:
            if landed():
                self.host.notify(f"⚠ {error}")

        def look() -> Overview:
            machines = self.service.games_elsewhere().get(self.mg.folder.key, [])
            return self.mg.overview(machines=machines)

        worker.run_async(look, on_done=done, on_failed=failed)

    def poll(self) -> None:
        if not self._working and self.isVisible() and not self._pending:
            self.refresh()

    def hints(self):
        return [([Action.ALT], "Check again")]

    def handle_action(self, action: Action) -> bool:
        if action == Action.ALT:
            if not self._working:
                self.refresh()
            return True
        return False

    def summary(self, ov: Overview | None) -> tuple[str, str, str]:
        return game_summary(self.mg, ov)

    def _hook_words(self, ov: Overview) -> tuple[str, str, str]:
        """The store's Play button: value word, tone, detail."""
        hook, game = ov.hook, ov.game
        if ov.instance is None:
            return "Game only", "off", "Starts the game without mods until MO2 is installed."
        pending = getattr(hook, "pending", None)
        if pending is not None:  # Steam only changes its settings while it's closed
            turning_on = pending.action == "select"
            return ("Waiting for Steam", "busy",
                    ("Plays through MO2" if turning_on else "Starts the game as before")
                    + f" once Steam has closed. {steamos.steam_restart_hint()}")
        if hook is not None and hook.enabled:
            return "Mod Organizer 2", "ok", "Press to have it start the game as before."
        if game is not None and game.store == "steam":
            current = getattr(hook, "current_mapping", None)
            if current and compattools.MO2LINT_TOOL_RE.match(current):
                return "Opens MO2", "off", "Press to have it play through MO2 instead of opening it."
            return "Game only", "off", "Press to have it play through MO2."
        return "Game only", "off", "Press to have it play through MO2. Close Heroic first."

    def _show(self, ov: Overview) -> None:
        self.overview = ov
        self.host.managed_checked(self.mg.ui_key, ov)
        mg, game = self.mg, ov.game
        if game is None:
            self.head.set_subtitle(mg.summary)
            self.game_row.set_state("Not installed", "off", where_to_install(mg))
            self.hook_row.setText("The store's Play button")
        else:
            launcher = launcher_of(game)
            origin = "From Steam." if game.store == "steam" else \
                f"From {STORE_NAMES[game.store]}, through the Heroic Games Launcher."
            self.head.set_subtitle(f"{origin} {mg.summary}")
            self.hook_row.setText(f"{launcher}'s Play button")
            if not ov.prefix_ready:
                self.game_row.set_state("Not set up", "warn",
                                        f"Launch it once from {launcher}, so {launcher} sets up its prefix.")
            else:
                version = ov.version[0] if ov.version else (
                    game.version if game.store != "steam" and game.version else "Installed")
                pinned = ", kept on this version by Heroic" if game.pinned else ""
                self.game_row.set_state(version, "ok", f"{_short(game.install_path)}{pinned}")
        if ov.instance is None and game is None:
            self.mo2_row.set_state("Not installed", "off", f"Install {mg.short} first, then Mod Organizer 2 for it.")
        elif ov.instance is None:
            self.mo2_row.set_state("Not installed", "off", "Install it with the blue button.")
        elif ov.instance_problem:
            self.mo2_row.set_state("Needs a look", "warn", ov.instance_problem)
        elif ov.needs_setup:
            self.mo2_row.set_state("Not finished", "warn", f"{mg.setup_missing} Use the blue button.".strip())
        else:
            self.mo2_row.set_state("Installed", "ok", _short(ov.instance))
        hook = ov.hook
        self.hook_row.set_state(*self._hook_words(ov))
        self.mods_row.set_state(*self._mods_words(ov))
        self.note.setText(ov.version_problem or "")
        self.note.setVisible(bool(ov.version_problem))
        kept_for = ", ".join(v[0] if v[0] == v[1] else f"{v[0]} (build {v[1]})" for v in ov.kept_versions)
        self.info.setText(f"The mods kept here are set up for {mg.name} {kept_for}." if game is None and kept_for else "")
        self.info.setVisible(bool(self.info.text()))
        self.store_tile.set_description(
            f"Your mods from {machines_text(ov.machines)} are already kept here." if ov.mods == "kept" else
            f"Your mods from {machines_text(ov.machines)} come over once Mod Organizer 2 is installed for it."
            if ov.machines else "Then install Mod Organizer 2 for it here.")
        self.store_tile.setVisible(game is None and bool(mg.game.appid) and not self._working)
        ready = game is not None and ov.prefix_ready
        has = ov.instance is not None and not ov.instance_problem
        finishing = has and ov.needs_setup
        self.install_tile.setText("Finish setting up Mod Organizer 2" if finishing else "Install Mod Organizer 2")
        self.install_tile.set_description(
            f"Then it uses your mods from {machines_text(ov.machines)} kept here, without downloading them again."
            if ov.mods == "kept" and not finishing else mg.install_note())
        self.install_tile.setVisible(ready and (ov.instance is None or finishing) and not self._working)
        through_store = game is not None and hook is not None and hook.enabled
        self.play.set_description(
            f"Through {launcher_of(game)} and Mod Organizer 2, with your mods and {STORE_NAMES[game.store]} cloud saves."
            if through_store else "Through Mod Organizer 2, with your mods.")
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
        self.store_tile.setEnabled(not busy)
        self.install_tile.setEnabled(ready and not busy)
        self.play.setEnabled(has and not busy)
        self.open_mo2.setEnabled(has and not busy)
        self.use_existing.setEnabled(ov is not None and not busy)
        self.mo2_row.setEnabled(ov is not None and not busy)
        self.hook_row.setEnabled(has and not busy)
        # Stopping needs nothing to work; starting needs a usable instance, or
        # (keeping a copy for the other machines) another machine that syncs it.
        self.mods_row.setEnabled(not busy and ov is not None and (has or ov.mods != "off" or bool(ov.machines)))

    def preferred_focus(self):
        for tile in (self.store_tile, self.install_tile, self.play):
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
        self.host.set_busy(self.busy_key, True)
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
        self.host.set_busy(self.busy_key, False)
        self.work.hide()
        self.refresh()

    def _install(self) -> None:
        """Install MO2, or finish the setup of the instance in use: the install
        skips MO2-LINT for an instance that's there and does what's missing."""
        ov = self.overview
        finishing = ov is not None and ov.instance is not None and ov.needs_setup
        dest = ov.instance if finishing else self.mg.default_instance_dir()
        bridge = _OutputBridge(self)
        bridge.said.connect(self._on_output)
        self.install_tile.hide()

        def output(line: str) -> None:
            if not _TOOL_LINE.match(line):
                bridge.said.emit(line.strip())

        use_kept = ov is not None and ov.mods == "kept" and not finishing
        machines = machines_text(ov.machines) if ov is not None else ""

        def install():
            instance = self.mg.install_mo2(dest, output)
            if use_kept:  # the copy kept for the other machines moves into the new instance
                bridge.said.emit("Moving in the mods kept here…")
                self.service.share_game_folder(self.mg.folder.key)
            return instance

        def done(instance) -> str:
            if use_kept:
                return (f"Mod Organizer 2 is installed in {_short(instance)}, with your mods from {machines}. "
                        "They sync with your other machines from there.")
            return f"Mod Organizer 2 is installed in {_short(instance)}. Open it to add mods, or play."

        self._run(install,
                  message="Finishing Mod Organizer 2's setup…" if finishing
                  else f"Installing Mod Organizer 2 for {self.mg.short}… This takes a few minutes.",
                  on_done=done)

    def _on_output(self, line: str) -> None:
        if self._working and line:
            self.work.setText(line)

    def _choose_existing(self) -> None:
        ov = self.overview
        if ov is None or self.host.busy or self._working:
            return
        start = ov.instance.parent if ov.instance is not None else Path.home()
        FolderSheet(self.host, f"Select your Mod Organizer 2 folder for {self.mg.short}", start, self._use,
                    description="The folder with ModOrganizer.exe in it.").open()

    def _use(self, path: Path) -> None:
        """Mods sync, if on, moves along before this finishes (see the service)."""
        self._run(lambda: " ".join(self.service.use_game_instance(self.mg, path)), message="Checking that folder…")

    def _toggle_hook(self) -> None:
        ov = self.overview
        if ov is None or ov.instance is None or ov.game is None:
            return
        launcher = launcher_of(ov.game)
        pending = getattr(ov.hook, "pending", None)
        if pending is not None:  # a change waiting for Steam to close: pressing takes it back
            turning_off = pending.action == "select"
        else:
            turning_off = ov.hook is not None and ov.hook.enabled
        if turning_off:
            self._run(self.mg.disable_hook, message=f"Giving {launcher}'s Play button back to the game…")
        else:
            self._run(lambda: " ".join(self.mg.enable_hook()), message=f"Pointing {launcher}'s Play button at MO2…")

    def _mods_words(self, ov: Overview) -> tuple[str, str, str]:
        """The mods sync row: value word, tone, detail. Off, on (synced with
        this machine's MO2), kept (a copy for the other machines), stopping."""
        mg, game = self.mg, ov.game
        word, tone = _SYNC_WORDS[ov.mods]
        machines = machines_text(ov.machines)
        has = ov.instance is not None and not ov.instance_problem
        if ov.mods == "stopping":
            return word, tone, "Finishes once Syncthing answers."
        if ov.mods == "kept":
            size = size_text(ov.kept_bytes)
            if has and ov.own_mods:
                return word, tone, (f"A copy is kept here for {machines}. Press to sync your mods with this "
                                    "Mod Organizer 2 instead: they merge with the ones it has, and download "
                                    "again from your other machines.")
            if has:
                return word, tone, (f"A copy is kept here for {machines}. Press to use it with this Mod "
                                    "Organizer 2. It moves in if it's up to date with a machine that's on; "
                                    "otherwise the mods download again.")
            return word, tone, (f"Kept here for {machines}" + (f" ({size})" if size else "")
                                + ", so they sync through this machine. Press to stop keeping it.")
        if ov.mods == "on":
            if not self.service.reload_state().paired:
                return word, tone, "No machine is paired yet. Pair one under Sync."
            saves = mg.saves_note or (f"Saves sync through {launcher_of(game)}, as they do without mods."
                                      if game is not None else "")
            return word, tone, f"Your mods sync with your paired machines. {saves}".strip()
        if has and not ov.chosen:
            return word, tone, "Press to use this instance here and sync its mods with your paired machines."
        if has:
            return word, tone, "Press to sync your mods with your paired machines."
        if ov.machines:
            return "Off", "off", (f"Modded on {machines}. Press to keep a copy here, so they sync through "
                                  "this machine.")
        return word, tone, "Install Mod Organizer 2 for it first."

    def _open_store(self) -> None:
        """Steam's install dialog for the game. This page notices it on its next read."""
        if self.mg.game.appid:
            QDesktopServices.openUrl(QUrl(f"steam://install/{self.mg.game.appid}"))
            self.host.notify(f"Steam opens to install {self.mg.name}. This page updates once it's installed.")

    def _toggle_sync(self) -> None:
        ov = self.overview
        if ov is None:
            return
        key = self.mg.folder.key
        has = ov.instance is not None and not ov.instance_problem
        if ov.mods == "kept" and not has:
            self._confirm_stop_keeping()
            return
        if ov.mods == "off" and not has:
            if ov.machines:
                self._run(self.service.hold_game_folder, key, message="Keeping a copy of the mods…",
                          on_done=lambda _root: f"A copy of your {self.mg.short} mods is kept here for "
                                                f"{machines_text(ov.machines)}.")
            return
        if ov.mods in ("off", "kept"):
            def share():
                if not ov.chosen:  # found, not handed over: pressing here hands it over
                    self.mg.use_instance(ov.instance)
                return self.service.share_game_folder(key)

            used = ov.mods == "kept" and not ov.own_mods
            self._run(share, message="Moving in the mods kept here…" if used else "Starting mods sync…",
                      on_done=lambda _root: (f"Your {self.mg.short} mods kept here are now this Mod Organizer 2's, "
                                             "and sync with your paired machines from there." if used else
                                             f"Your {self.mg.short} mods now sync with your paired machines."))
        else:
            self._run(self.service.stop_game_folder, key, message="Stopping mods sync…",
                      on_done=lambda done: (f"{self.mg.short} mods no longer sync here. Nothing was deleted."
                                            if done else "Mods sync stops once Syncthing answers."))

    def _confirm_stop_keeping(self) -> None:
        name = self.mg.short

        def chosen(choice: str | None) -> None:
            if choice == "stop":
                self._run(self.service.stop_game_folder, self.mg.folder.key, message="Stopping…",
                          on_done=lambda done: (f"{name}'s mods are no longer kept here. Nothing was deleted."
                                                if done else "It stops once Syncthing answers."))

        self.host.confirm(
            f"Stop keeping {name}'s mods?",
            f"This machine keeps {name}'s mods for your other machines; the game isn't set up here. Stopped, "
            "they sync them with each other only while both are on. The copy's files stay on this machine. "
            "Turning 'Keep games not installed here' off and on again keeps it again.",
            [("stop", "Stop keeping it", "", "danger", "stop"), ("cancel", "Cancel", "", "normal", "close")],
            chosen, default="cancel")

    def _launch(self, *, play: bool) -> None:
        ov = self.overview
        if ov is None or ov.instance is None:
            return

        def start() -> str:
            message = self.mg.launch(self.service.launcher, play=play)
            return f"{message} {ov.version_problem}" if ov.version_problem else message

        self._run(start, message=f"Starting {self.mg.name} through MO2…" if play else "Opening Mod Organizer 2…")
