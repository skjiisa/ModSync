"""The one ModSync window, built like a console app.

Home, Sync and Settings are switched with the bumpers or Q/E. Home holds
setup, repairs and launch actions for one game at a time: the game switcher
beside the wordmark (or X) picks which, set up or not. Game and MO2 maintenance open as sheets without leaving Home. The setup
wizard takes over the middle when it runs.

The same window serves Steam's launch hook. Given a ``SteamLaunch``, Steam is
waiting on it: Play and Open MO2 prepare the same launch as ever and hand it to
the hook to run as Steam's own; Continue (no MO2 yet) and Cancel launch record
the decision. Each closes the window, closing it any other way cancels, and the
decision outlives rebuilds and trips through the wizard.

While game or USVFS files are being rewritten (or an install or launch is
under way) the window is busy: it can't close, rebuild, start the wizard or
decide a Steam launch until that finishes.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable

import shiboken6

from PySide6.QtCore import QSize, QThreadPool, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPainter
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QStackedWidget, QVBoxLayout, QWidget,
)

from modsync import __version__, dfu, launchhook, mo2games
from modsync.games import SKYRIM_SE
from modsync.state import State
from modsync.service import ModSyncService
from modsync.ui import nav, theme, worker
from modsync.ui.input import Action, DIRECTIONS, InputRouter
from modsync.ui.overlays import ConfirmSheet, GameSheet, Overlay, PageSheet
from modsync.ui.pages import Page
from modsync.ui.pages.daggerfall import DaggerfallPage
from modsync.ui.pages.game import GamePage
from modsync.ui.pages.home import HomePage
from modsync.ui.pages.managed import ManagedGamePage, game_summary
from modsync.ui.pages.mods import ModsPage
from modsync.ui.pages.setup import SetupFlow
from modsync.ui.pages.sync import SyncPage
from modsync.ui.pages.system import SystemPage
from modsync.ui.widgets import (
    Backdrop, FocusHalo, GameSwitch, GlyphLabel, HintBar, Icon, TabButton, Toasts, discard, label, pill,
    restyle_rows,
    sync_lit,
)

# Testing aid: MODSYNC_HUB_AUTO_DECISION=continue|cancel decides a Steam launch by
# itself after a few seconds (continue = Play, once an MO2 instance is chosen), so the whole Steam → hook → ModSync → game chain
# can be exercised without a hand on the controller (e.g. from the game's
# launch options).
_AUTO_DECISION_ENV = "MODSYNC_HUB_AUTO_DECISION"
_AUTO_DECISION_MS = 3000
_POLL_MS = 4000

APP_ID = "io.github.skjiisa.ModSync"

PAGES = (HomePage, GamePage, ModsPage, SyncPage, SystemPage, DaggerfallPage)
TAB_KEYS = ("home", "sync", "system")
# The games with a page of their own that Home can show: key, the page that is
# their Home, name in the switcher. Every other game is one from the table
# (modsync.mo2games), with a ManagedGamePage under its own key.
GAMES = (("skyrim", "home", "Skyrim"), ("daggerfall", "daggerfall", "Daggerfall Unity"))
GAME_PAGES = {key: page for key, page, _name in GAMES}
# The switcher's last row, which lists every other game from the table.
ADD_GAME = "add-game"
_GAME_POLLS = 8  # how many polls apart the other games are looked for
_SWITCH_NAME_MAX = 26  # a longer name is cut short in the switcher, never in the picker


def alive(w: QWidget | None) -> bool:
    return w is not None and shiboken6.isValid(w)


def dfu_here(ov: dfu.Overview | None) -> bool:
    """Daggerfall is on this machine: Steam has it, or Daggerfall Unity is installed."""
    return ov is not None and (ov.dagger is not None or ov.install is not None)


def managed_here(ov: mo2games.Overview | None) -> bool:
    """A game from the table belongs here: Steam or Heroic installed it, it
    has an MO2 instance here, a copy of its mods is kept here, or a paired
    machine syncs them (what mo2games.listed lists, and installed)."""
    return ov is not None and (ov.game is not None or ov.instance is not None or ov.elsewhere)


@dataclass
class GameLook:
    """The table's games as the switcher sees them, read off the UI thread."""

    overviews: dict[str, mo2games.Overview]  # the games it lists, by key in the app (the one shown left out)
    installed: dict[str, str]  # every game Steam or Heroic has here, by key in the app: the store's name


def look_for_managed(skip: str, service: ModSyncService, known: tuple[str, ...] = ()) -> GameLook:
    """Which of the table's games the switcher lists (set up here, kept here,
    or synced on a paired machine, see mo2games.listed) and how each stands,
    and which are installed here, for "Add a game…". ``known`` are the ones
    it lists already, read again too, so one that no longer qualifies doesn't
    keep showing how it stood. ``skip`` is the one shown, which reads itself."""
    from modsync.games import STORE_NAMES

    elsewhere = service.games_elsewhere()
    wanted = {game.ui_key: game for game in mo2games.listed(elsewhere)}
    for key in known:
        game = mo2games.for_ui(key)
        if game is not None:
            wanted.setdefault(key, game)
    overviews = {key: game.overview(hook=False, machines=elsewhere.get(game.folder.key, []))
                 for key, game in wanted.items() if key != skip}
    installed = {}
    for game in mo2games.here():
        found = game.installs()
        if found:
            installed[game.ui_key] = STORE_NAMES[found[0].store]
    return GameLook(overviews, installed)


def app_mark(size: int) -> QWidget:
    """The app icon, as installed (Flatpak) or from the checkout."""
    icon = QApplication.windowIcon()
    if icon.isNull():
        png = Path(__file__).resolve().parents[2] / "packaging" / "flatpak" / "icons" / f"{APP_ID}.png"
        icon = QIcon(str(png)) if png.is_file() else QIcon()
    if icon.isNull():
        return Icon("sync", size)
    mark = QLabel()
    ratio = QApplication.instance().devicePixelRatio() if QApplication.instance() else 1.0
    pixmap = icon.pixmap(QSize(size, size), ratio)
    mark.setPixmap(pixmap)
    mark.setFixedSize(size, size)
    mark.setScaledContents(True)
    return mark


class TopBar(QWidget):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.setFixedHeight(60)
        h = QHBoxLayout(self)
        h.setContentsMargins(24, 10, 24, 10)
        h.setSpacing(10)
        h.addWidget(app_mark(32))
        h.addWidget(label("ModSync", "heading", wrap=False))
        h.addSpacing(24)
        # Which game Home shows, once there is a choice: X, or choose it. Set
        # apart from the section tabs: it changes what Home shows, not where you are.
        game = QWidget()
        g = QHBoxLayout(game)
        g.setContentsMargins(0, 0, 18, 0)
        g.setSpacing(10)
        self.game_glyph = GlyphLabel("aux", window.router)
        g.addWidget(self.game_glyph)
        self.game_switch = GameSwitch()
        self.game_switch.clicked.connect(window.open_game_picker)
        self.game_switch.fit(window.switch_names())
        g.addWidget(self.game_switch)
        self.game_box = game
        h.addWidget(game)
        self.lb = GlyphLabel("prev_tab", window.router)
        h.addWidget(self.lb)
        self.tabs_row = QHBoxLayout()
        self.tabs_row.setSpacing(4)
        h.addLayout(self.tabs_row)
        self.rb = GlyphLabel("next_tab", window.router)
        h.addWidget(self.rb)
        h.addStretch(1)
        if window.steam_launch is not None:
            h.addWidget(pill("Steam is waiting", "info"))
            h.addSpacing(12)
        self.clock = label("", "secondary", wrap=False)
        h.addWidget(self.clock)
        self._tabs_on = True
        self._game = ""
        self.router = window.router
        window.router.modeChanged.connect(self._update_glyphs)
        window.router.controllersChanged.connect(self._update_glyphs)
        self._update_glyphs()
        self._tick()
        timer = QTimer(self)
        timer.timeout.connect(self._tick)
        timer.start(15000)

    def paintEvent(self, _event) -> None:  # noqa: N802
        QPainter(self).fillRect(self.rect(), theme.color("bar"))

    def _tick(self) -> None:
        self.clock.setText(time.strftime("%H:%M"))

    def set_tabs_visible(self, on: bool) -> None:
        for i in range(self.tabs_row.count()):
            w = self.tabs_row.itemAt(i).widget()
            if w is not None:
                w.setVisible(on)
        self._tabs_on = on
        self._update_glyphs()

    def set_game(self, name: str) -> None:
        """Show the switcher with this game's name, or hide it ("")."""
        self._game = name
        self.game_switch.setText(name)
        self._update_glyphs()

    def _update_glyphs(self, *_args) -> None:
        show = self._tabs_on and self.router.mode != "mouse"
        self.lb.setVisible(show)
        self.rb.setVisible(show)
        switch = self._tabs_on and bool(self._game)
        self.game_box.setVisible(switch)
        self.game_glyph.setVisible(switch and show and self.router.pad_sends(Action.AUX))


class MainWindow(QMainWindow):
    busyChanged = Signal(bool)
    gameChecked = Signal(object, object)  # GameStatus, VersionCheck
    syncStatus = Signal(object)  # SyncStatus
    setupDescribed = Signal(list)  # the instance's profile line and problems
    mo2Checked = Signal()  # runtime and USVFS checks finished
    steamHookChecked = Signal(object)  # shared Home and Settings launch status
    _call = Signal(object)  # run a callable on the UI thread

    def __init__(self, *, steam_launch: launchhook.SteamLaunch | None = None,
                 service: ModSyncService | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"ModSync {__version__}")
        self.resize(1280, 800)
        self.steam_launch = steam_launch
        self.service = service or ModSyncService()
        self.router = InputRouter.instance()
        self.opened_at = time.monotonic()
        self.firewall_check = None
        self.messages: list[str] = []
        self.overlays: list[Overlay] = []
        self.pages: dict[str, Page] = {}
        self.tabs: dict[str, TabButton] = {}
        self.setup: SetupFlow | None = None
        # Sees every action first while something covers the whole window
        # (Home's countdown), wherever focus has gone meanwhile.
        self.action_guard: Callable[[Action], bool] | None = None
        self._busy: set[str] = set()
        self._rebuild_pending = False
        self._current = "home"
        # The table's games Home can show, by their key in the app: the one it
        # was left on, the ones the switcher lists (see look_for_managed) and
        # any chosen under "Add a game…" since the window opened.
        self.managed: dict[str, mo2games.ManagedGame] = {}
        self.managed_overviews: dict[str, mo2games.Overview] = {}
        self.installed_here: dict[str, str] = {}  # the table's games Steam or Heroic has here: store name
        saved = self.service.state.home_game
        game = mo2games.for_ui(saved) if saved else None
        if game is not None:
            self.managed[saved] = game
        # Steam's Play button opened this for Skyrim: only Skyrim is shown.
        self.game = saved if steam_launch is None and (saved in GAME_PAGES or saved in self.managed) else "skyrim"
        # Reopened on a game other than Skyrim: until the first read says it's
        # still on this machine, the window may open on Skyrim instead.
        self._restored = self.game != "skyrim"
        self.dfu_overview: dfu.Overview | None = None
        self._game_polls = 0
        self._call.connect(lambda fn: fn())

        self.shell = Backdrop()
        self.setCentralWidget(self.shell)
        self.chrome = QWidget(self.shell)
        col = QVBoxLayout(self.chrome)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.topbar = TopBar(self)
        col.addWidget(self.topbar)
        self.root = QStackedWidget()
        col.addWidget(self.root, 1)
        self.hintbar = HintBar(self.router)
        col.addWidget(self.hintbar)
        self.stack = QStackedWidget()
        self.root.addWidget(self.stack)
        self.toasts = Toasts(self.shell)
        self.halo = FocusHalo(self.shell)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.poll)
        self._build_pages()
        self._timer.start(_POLL_MS)
        self._update_game_switch()
        self._look_for_games()

        app = QApplication.instance()
        app.focusChanged.connect(self._on_focus_changed)
        self.router.modeChanged.connect(self._on_mode_changed)
        self.router.controllersChanged.connect(self._on_controllers_changed)
        self._on_mode_changed(self.router.mode)
        self.go("home")

        auto = os.environ.get(_AUTO_DECISION_ENV, "").strip().lower()
        if steam_launch is not None and auto in ("continue", "cancel"):
            self.notify(f"Test mode: choosing \"{auto}\" automatically in a moment.")
            if auto == "cancel":
                decide = partial(self.decide_launch, launchhook.EXIT_CANCEL)
            elif self.service.state.has_instance:
                decide = partial(self.launch, play=True, checked=True)  # what the Play tile does
            else:
                decide = partial(self.decide_launch, launchhook.EXIT_CONTINUE)
            QTimer.singleShot(_AUTO_DECISION_MS, self, decide)

    # --- pages ---------------------------------------------------------------------
    def _build_pages(self) -> None:
        made = [page_cls(self) for page_cls in PAGES]
        made += [ManagedGamePage(self, game) for game in self.managed.values()]
        for page in made:
            self.pages[page.key] = page
            self.stack.addWidget(page)
            if page.key not in TAB_KEYS:
                continue
            tab = TabButton(page.key, page.label, page.icon)
            tab.clicked.connect(lambda _=False, k=page.key: self.go(k, focus=self.router.mode != "mouse"))
            self.tabs[page.key] = tab
            self.topbar.tabs_row.addWidget(tab)
            tab.setVisible(self.setup is None)  # rebuilt while the wizard is open
        panel, home = self.pages["game"].panel, self.pages["home"]
        self.pages["sync"].synced.connect(panel.refresh)
        panel.busyChanged.connect(home.on_game_busy)
        panel.progressed.connect(home.on_game_progress)
        self.pages["mods"].repairsChecked.connect(self.mo2Checked)

    def _clear_pages(self) -> None:
        for page in self.pages.values():
            page.shutdown()
            self.stack.removeWidget(page)
            discard(page)
        for tab in self.tabs.values():
            self.topbar.tabs_row.removeWidget(tab)
            discard(tab)
        self.pages.clear()
        self.tabs.clear()

    @property
    def page(self) -> Page:
        return self.pages[self._page_key(self._current)]

    def _page_key(self, key: str) -> str:
        """The page a section shows: Home is the chosen game's."""
        return GAME_PAGES.get(self.game, self.game) if key == "home" else key

    def go(self, key: str, *, focus: bool = True) -> None:
        if key not in self.pages or self.setup is not None:
            return
        game = next((g for g in self.game_keys() if GAME_PAGES.get(g, g) == key and key != "home"), None)
        if game is not None:
            self.switch_game(game, focus=focus)
            return
        if key not in self.tabs:
            if self.busy:
                return
            page = self.pages[key]
            if any(isinstance(sheet, PageSheet) and sheet.page is page for sheet in self.overlays):
                return
            PageSheet(self, page).open()
            return
        if self.overlays:
            return
        old = self._current
        self._current = key
        for k, tab in self.tabs.items():
            tab.setChecked(k == key)
        page = self.pages[self._page_key(key)]
        if self.stack.currentWidget() is not page:
            self.stack.setCurrentWidget(page)
            keys = list(self.tabs)
            if old in keys:
                self._slide(page, keys.index(key) - keys.index(old))
        if focus:
            page.focus_default()
        self.refresh_hints()

    def _slide(self, page: QWidget, direction: int) -> None:
        """A short glide in from the side the new section sits on."""
        if not direction or not self.isVisible():
            return
        from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation

        end = page.pos()
        anim = QPropertyAnimation(page, b"pos", page)
        anim.setDuration(180)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(end + QPoint(48 if direction > 0 else -48, 0))
        anim.setEndValue(end)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    # --- games ---
    @property
    def switchable(self) -> bool:
        """Every game can be picked, installed or not: its Home says how to get
        it. A window Steam's Play button opened for Skyrim shows only Skyrim."""
        return self.steam_launch is None

    def game_keys(self) -> list[str]:
        """Every game Home can show, in the picker's order."""
        return [*GAME_PAGES, *self.managed]

    def switch_name(self, game: str) -> str:
        """The game's name in the switcher at the top."""
        fixed = {key: name for key, _page, name in GAMES}
        name = fixed.get(game) or self.managed[game].name
        return name if len(name) <= _SWITCH_NAME_MAX else name[:_SWITCH_NAME_MAX - 1].rstrip() + "…"

    def switch_names(self) -> list[str]:
        return [self.switch_name(key) for key in self.game_keys()]

    def _update_game_switch(self) -> None:
        self.topbar.set_game(self.switch_name(self.game) if self.switchable else "")
        self.refresh_hints()

    def _offer(self, game: mo2games.ManagedGame) -> None:
        """A game from the table turned up on this machine: the picker offers
        it from now on, and it gets its page."""
        key = game.ui_key
        if key in self.managed:
            return
        self.managed[key] = game
        if self.pages and key not in self.pages:
            page = ManagedGamePage(self, game)
            self.pages[key] = page
            self.stack.addWidget(page)
        self.topbar.game_switch.fit(self.switch_names())

    def _show_skyrim(self) -> None:
        """Make Home Skyrim's without going there, and remember it."""
        if self.game != "skyrim":
            self.game = "skyrim"
            self.topbar.set_game("Skyrim" if self.switchable else "")
            self._remember_game("")

    @staticmethod
    def _remember_game(value: str) -> None:
        """Write the choice to disk only. Not through ``service.update_state``:
        its whole snapshot, returned on a worker, could land after a newer
        change (a forgotten instance) and bring the old setup back."""
        worker.run_async(State.update, home_game=value, on_failed=lambda _e: None)

    def _look_for_games(self) -> None:
        """Read whether the other games are here, off the UI thread (it reads
        Steam's libraries and Heroic's files). The one shown reads itself."""
        if self.steam_launch is not None:
            return
        if self.game != "daggerfall":
            worker.run_async(dfu.overview, hook=False, on_done=self.dfu_checked, on_failed=lambda _e: None)
        worker.run_async(look_for_managed, self.game, self.service, tuple(self.managed),
                         on_done=self._managed_found, on_failed=lambda _e: None)

    def game_here(self, game: str) -> bool:
        if game == "daggerfall":
            return dfu_here(self.dfu_overview)
        if game in self.managed:
            return managed_here(self.managed_overviews.get(game))
        return True

    def _managed_found(self, look: GameLook) -> None:
        """The table's games were read: each one the switcher lists is offered
        from now on."""
        if not shiboken6.isValid(self):
            return
        self.installed_here = look.installed
        for key, ov in look.overviews.items():
            game = mo2games.for_ui(key)
            if game is not None:
                self._offer(game)
                self.managed_checked(key, ov)

    def dfu_checked(self, ov: dfu.Overview) -> None:
        """Daggerfall Unity was read (here, or by its page): the picker says how it stands."""
        if not shiboken6.isValid(self):
            return
        self.dfu_overview = ov
        self._game_checked("daggerfall")

    def managed_checked(self, key: str, ov: mo2games.Overview) -> None:
        """A game from the table was read (here, or by its page): the picker says how it stands."""
        if not shiboken6.isValid(self):
            return
        self.managed_overviews[key] = ov
        self._game_checked(key)

    @property
    def cyberpunk_overview(self) -> mo2games.Overview | None:
        return self.managed_overviews.get("cyberpunk")

    def _game_checked(self, game: str) -> None:
        """The first read of the game the window reopened on: not on this machine,
        and nothing pressed since, Home is Skyrim's this time."""
        if self._restored and self.game == game:
            self._restored = False
            if not self.game_here(game) and self.router.last_input < self.opened_at:
                self._open_on_skyrim()

    def _open_on_skyrim(self) -> None:
        """Reopened on a game this machine doesn't have: Home is Skyrim's this
        time. The choice stays saved, for once the game is here."""
        shown = self.pages[GAME_PAGES.get(self.game, self.game)]
        was_inside = alive(self.focusWidget()) and shown.isAncestorOf(self.focusWidget())
        self.game = "skyrim"
        self._update_game_switch()
        if self._current == "home" and self.setup is None:
            self.stack.setCurrentWidget(self.page)
            if was_inside and not self.overlays:
                self.page.focus_default()

    def game_choices(self) -> list[tuple[str, str, str, str, str]]:
        """Each game the picker offers, with how it stands."""
        return [("skyrim", SKYRIM_SE.name, *self.pages["home"].summary()),
                ("daggerfall", "Daggerfall Unity", *DaggerfallPage.summary(self.dfu_overview)),
                *((key, game.name, *game_summary(game, self.managed_overviews.get(key)))
                  for key, game in self.managed.items())]

    def add_game_choices(self) -> list[tuple[str, str, str, str, str]]:
        """The table's games the switcher doesn't list: the ones installed
        here first, then every other one, by name."""
        from modsync.games import STORE_NAMES

        rest = [game for game in mo2games.all_games().values() if game.ui_key not in self.managed]
        rest.sort(key=lambda g: (g.ui_key not in self.installed_here, g.name.lower()))
        choices = []
        for game in rest:
            store = self.installed_here.get(game.ui_key)
            if store:
                choices.append((game.ui_key, game.name, "Installed", "off", f"In {store}. Set up mods for it here."))
            else:
                stores = ", ".join(STORE_NAMES[s] for s in game.game.stores)
                choices.append((game.ui_key, game.name, "", "off", f"From {stores}."))
        return choices

    def open_game_picker(self) -> None:
        if not self.switchable or self.setup is not None or self.overlays:
            return
        # Nothing to switch to while no other game is here: focus stays, so X then A
        # doesn't head for Steam's install dialog.
        here = self.game != "skyrim" or any(self.game_here(key) for key in self.game_keys() if key != "skyrim")
        extra = (ADD_GAME, "Add a game…", "Another game ModSync can set up Mod Organizer 2 for.")
        GameSheet(self, self.game_choices(), self.game, self._picked,
                  start=None if here else self.game, extra=extra).open()

    def _picked(self, key: str) -> None:
        if key == ADD_GAME:
            self.open_add_game()
        else:
            self.switch_game(key)

    def open_add_game(self) -> None:
        """Every game from the table the switcher doesn't list; the one chosen
        is shown, and stays in the switcher while the window is open."""
        if not self.switchable or self.setup is not None or self.overlays:
            return
        GameSheet(self, self.add_game_choices(), "", self.show_game, title="Add a game", accept="Open").open()

    def switch_game(self, game: str, *, focus: bool = True) -> None:
        """Show ``game`` on Home and go there. Remembered for next time."""
        if game not in self.game_keys() or self.setup is not None or self.overlays:
            return
        if game != "skyrim" and self.steam_launch is not None:
            return
        order = self.game_keys()
        step = order.index(game) - order.index(self.game)
        self._restored = False  # chosen now: stays, here or not
        if game != self.game:
            self.game = game
            self.topbar.set_game(self.switch_name(game))
            self._remember_game("" if game == "skyrim" else game)
        was_home = self._current == "home"
        self.go("home", focus=focus and self.router.mode != "mouse")
        if was_home:
            self._slide(self.page, step)

    def show_game(self, key: str) -> None:
        """Go to a game's page by its key, also one from the table that the
        picker doesn't offer yet."""
        game = mo2games.for_ui(key)
        if game is not None:
            self._offer(game)
        self.go(key)

    def cycle(self, step: int) -> None:
        keys = list(self.tabs)
        self.go(keys[(keys.index(self._current) + step) % len(keys)])

    def prepare_rebuild(self) -> None:
        """Stop polling and unblock waiting workers before the setup changes."""
        self._timer.stop()
        for page in self.pages.values():
            page.shutdown()

    def rebuild(self) -> None:
        """The setup changed (instance chosen, vault created/joined/left, reset):
        build every section again from what is set up now. While something is
        busy, it happens as soon as that finishes."""
        if self.busy:
            self._rebuild_pending = True
            return
        self._rebuild_pending = False
        current = self._current
        for overlay in reversed(self.overlays[:]):
            overlay.dismiss()
        self._clear_pages()
        self._build_pages()
        self._timer.start(_POLL_MS)
        self._current = current if current in self.pages else "home"
        self.stack.setCurrentWidget(self.page)
        self.go(self._current)

    # --- setup wizard ---
    def start_setup(self, *, copy_from_machine: bool = False) -> SetupFlow | None:
        if self.busy or self.setup is not None:
            return None
        for overlay in reversed(self.overlays[:]):
            overlay.dismiss()
        self.setup = SetupFlow(self, copy_from_machine=copy_from_machine)
        self.root.addWidget(self.setup)
        self.root.setCurrentWidget(self.setup)
        self.topbar.set_tabs_visible(False)
        self.setup.focus_default()
        self.refresh_hints()
        return self.setup

    def finish_setup(self) -> None:
        if self.busy or self.setup is None:
            return
        setup, self.setup = self.setup, None
        self.root.setCurrentWidget(self.stack)
        self.root.removeWidget(setup)
        discard(setup)
        self.topbar.set_tabs_visible(True)
        self._current = "home"
        self._show_skyrim()  # the wizard set up Skyrim: end on its Play
        self.rebuild()

    # --- host services for pages ---------------------------------------------------
    def notify(self, text: str, tone: str | None = None) -> None:
        if not text:
            return
        text = str(text)
        self.messages.append(text)
        if tone is None:
            tone = "warn" if text.startswith("⚠") else "info"
        self.toasts.show_message(text.removeprefix("⚠").strip(), tone)

    @property
    def untouched(self) -> bool:
        """Nothing has been pressed since this window opened. Only then may a
        late check move focus: a press in flight must land where it aimed."""
        return self.router.last_input < self.opened_at

    @property
    def last_message(self) -> str:
        return self.messages[-1] if self.messages else ""

    def confirm(self, title, text, choices, on_choice, *, default=None) -> ConfirmSheet:
        sheet = ConfirmSheet(self, title, text, choices, on_choice, default=default)
        sheet.open()
        return sheet

    # --- sync set-up results ---
    # These outlive the widget that started them: a join can take seconds, and
    # the wizard or page may be gone by then. The window is not.
    def sync_started(self, message: str) -> None:
        self.set_busy("sync-setup", True)
        self.notify(message)

    def sync_failed(self, message: str) -> None:
        self.set_busy("sync-setup", False)
        self.notify(f"⚠ {message}")

    def vault_created(self) -> None:
        self.set_busy("sync-setup", False)
        if self.setup is not None:
            self.finish_setup()  # sharing is the optional final step
        else:
            self.rebuild()

    def vault_joined(self) -> None:
        """Copying from another machine ends the wizard: the game version can
        only be checked once the mods have arrived."""
        self.set_busy("sync-setup", False)
        if self.setup is not None:
            self.finish_setup()
        else:
            self.rebuild()

    def machine_paired(self, message: str) -> None:
        """Paired with a machine, without joining a vault: only the games'
        folders sync between them. The wizard stays where it is, since
        Mod Organizer 2 still isn't set up here."""
        self.set_busy("sync-setup", False)
        self.notify(message, "ok")
        if self.setup is None:
            self.rebuild()

    def instance_chosen(self, path: str) -> None:
        if self.setup is not None:
            self.setup.go_to(2 if self.setup.copy_from_machine else 1)
            if self.setup.copy_from_machine:
                self.setup._show_join()
        else:
            self.rebuild()

    def change_setup(self, operation, *args, message: str, on_done=None) -> None:
        """Serialize instance changes, reset and stopping sync with other work.
        Resume polling on failure too, including a partially changed setup."""
        if self.busy:
            return
        self.prepare_rebuild()
        self.set_busy("setup-change", True)
        self.notify(message)

        def finished(_result) -> None:
            self.set_busy("setup-change", False)
            self._timer.start(_POLL_MS)
            if on_done is not None:
                on_done()
            else:
                self.rebuild()

        def failed(error: str) -> None:
            self.set_busy("setup-change", False)
            self.notify(f"⚠ {error}")
            self.rebuild()

        worker.run_async(operation, *args, on_done=finished, on_failed=failed)

    def call_soon(self, fn) -> None:
        """Run ``fn`` on the UI thread; safe to call from a worker."""
        self._call.emit(fn)

    def open_overlay(self, overlay: Overlay) -> None:
        overlay.restore_focus = self.focusWidget()
        self.overlays.append(overlay)
        overlay.setGeometry(self._overlay_rect())
        overlay.show()
        overlay.raise_()
        self.toasts.raise_()
        self.halo.raise_()
        overlay.focus_default()
        self.refresh_hints()

    def close_overlay(self, overlay: Overlay) -> None:
        if overlay in self.overlays:
            self.overlays.remove(overlay)
        discard(overlay)
        back = overlay.restore_focus
        if alive(back) and back.isVisible() and back.isEnabled() and self.scope().isAncestorOf(back):
            back.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.focus_scope_default()
        self.refresh_hints()

    @property
    def top_overlay(self) -> Overlay | None:
        return self.overlays[-1] if self.overlays else None

    # --- busy ------------------------------------------------------------------------
    def set_busy(self, key: str, on: bool) -> None:
        (self._busy.add if on else self._busy.discard)(key)
        self._apply_busy()

    def _overlay_rect(self):
        """Sheets cover everything but the hint bar, which stays clickable."""
        rect = self.shell.rect()
        rect.setBottom(rect.bottom() - self.hintbar.height())
        return rect

    def _apply_busy(self) -> None:
        busy = self.busy
        if not busy and self._rebuild_pending:
            QTimer.singleShot(0, self, self.rebuild)
        for key in ("mods", "sync"):
            page = self.pages.get(key)
            if page is not None:
                page.content.setEnabled(not busy)
        self.busyChanged.emit(busy)

    @property
    def busy(self) -> bool:
        return bool(self._busy)

    def busy_except(self, key: str) -> bool:
        return bool(self._busy - {key})

    @property
    def launching(self) -> bool:
        return "launch" in self._busy or self.service.launcher.running()

    # --- launching ---
    def launch(self, *, play: bool, checked: bool = False) -> None:
        """Play / Open MO2. Steam waiting: the same launch, prepared here and
        handed to the hook, which runs it as Steam's own launch.

        With a vault, syncing is asked first (``launch_check``): a copy still
        under way blocks, and changes still arriving or files changed on two
        machines are worth a "Play anyway"."""
        if self.busy or (self.steam_launch is not None and self.steam_launch.decision is not None):
            return
        if not checked and self.service.state.syncing:
            self.set_busy("launch", True)
            worker.run_async(self.service.launch_check, on_done=partial(self._on_launch_checked, play),
                             on_failed=lambda _m: self._on_launch_checked(play, None))
            return
        self.set_busy("launch", True)
        self.notify("Starting Skyrim through MO2…" if play else "Opening MO2…")
        if self.steam_launch is not None:
            worker.run_async(self.service.prepare_mo2, play=play, on_done=self._on_prepared,
                             on_failed=self._on_launch_failed)
            return
        worker.run_async(self.service.launch_mo2, play=play, on_done=self._on_launched,
                         on_failed=self._on_launch_failed)

    def _on_launch_checked(self, play: bool, check) -> None:
        self.set_busy("launch", False)
        if check is None or not (check.blocked or check.warnings):
            self.launch(play=play, checked=True)
            return
        if check.blocked:
            self.notify(f"⚠ {check.blocked}")
            return

        def chosen(key: str | None) -> None:
            if key == "play":
                self.launch(play=play, checked=True)
            elif key == "sync":
                self.go("sync")

        self.confirm(
            "Play anyway?" if play else "Open Mod Organizer 2 anyway?",
            "\n\n".join(check.warnings),
            [("play", "Play anyway" if play else "Open MO2 anyway", "", "primary", "play"),
             ("sync", "Go to Sync", "See what's arriving and choose versions.", "normal", "sync"),
             ("wait", "Not now", "", "normal", "close")],
            chosen, default="play")

    def _on_prepared(self, result) -> None:
        plan, _note = result  # the window closes now; prepare_mo2 has logged the note
        self.set_busy("launch", False)
        self.steam_launch.plan = plan
        self.decide_launch(launchhook.EXIT_CONTINUE)

    def _on_launched(self, message: str) -> None:
        self.set_busy("launch", False)
        self.notify(message, "ok")

    def _on_launch_failed(self, message: str) -> None:
        self.set_busy("launch", False)
        self.notify(f"⚠ {message}")

    def poll(self) -> None:
        for page in self.pages.values():
            page.poll()
        # Now and then, so the picker knows about a game installed meanwhile
        # (Daggerfall through Steam, the table's games through Steam or Heroic).
        self._game_polls += 1
        if self._game_polls % _GAME_POLLS == 0:
            self._look_for_games()
        for message in self.service.launcher.poll():
            self.notify(f"⚠ {message}")
        self._apply_busy()  # a game started from here may have exited

    # --- quitting ---
    def request_quit(self) -> None:
        """Ask before closing: B on Home, Quit under Settings, or Ctrl+Q. Closing
        while Steam waits cancels its launch, which the question says."""
        if self.overlays and isinstance(self.top_overlay, ConfirmSheet) and self.top_overlay.property("quit"):
            return
        if self.busy:
            self.notify("⚠ ModSync is still working. Quit once it has finished.")
            return
        steam = self.steam_launch
        text = (
            f"Steam is waiting to start {steam.game.name}. Quitting returns to Steam without starting "
            "anything." if steam is not None else
            "Syncing and a queued Steam pin carry on only if the background service is on (under Settings)."
        )

        def chosen(key: str | None) -> None:
            if key == "quit":
                self.close()

        sheet = self.confirm(
            "Quit ModSync?", text,
            [("quit", "Quit and return to Steam" if steam is not None else "Quit ModSync", "", "primary", "power"),
             ("stay", "Stay", "", "normal", "close")],
            chosen)
        sheet.setProperty("quit", True)

    # --- Steam launch ---
    def decide_launch(self, code: int) -> None:
        """Steam launch: record Continue or Cancel, then close so the hook can act.
        The first decision sticks, and none is taken while files are rewritten."""
        launch = self.steam_launch
        if launch is None or launch.decision is not None or self.busy:
            return
        launch.decision = code
        self.close()

    # --- input ---------------------------------------------------------------------
    def scope(self) -> QWidget:
        if self.overlays:
            return self.overlays[-1]
        if self.setup is not None:
            return self.setup
        return self.chrome

    def focus_scope_default(self) -> None:
        if self.overlays:
            self.overlays[-1].focus_default()
        elif self.setup is not None:
            self.setup.focus_default()
        elif self._current in self.pages:
            self.page.focus_default()

    def handle_action(self, action: Action) -> bool:
        if self.action_guard is not None and self.action_guard(action):
            return True
        focus = self.focusWidget()
        scope = self.scope()
        inside = alive(focus) and (focus is scope or scope.isAncestorOf(focus))
        if action in DIRECTIONS:
            if not inside:
                self.focus_scope_default()
                return True
            direction = action.value
            switch = self.topbar.game_switch
            if focus is switch:
                if action == Action.DOWN:
                    self.page.focus_default()
                elif action == Action.RIGHT:
                    self.tabs[self._current].setFocus(Qt.FocusReason.OtherFocusReason)
                return True
            if isinstance(focus, TabButton):
                if action == Action.DOWN:
                    self.page.focus_default()
                elif action in (Action.LEFT, Action.RIGHT):
                    # Moving along the tabs switches sections as you go, console style.
                    keys = list(self.tabs)
                    i = keys.index(focus.key) + (1 if action == Action.RIGHT else -1)
                    if 0 <= i < len(keys):
                        self.tabs[keys[i]].setFocus(Qt.FocusReason.OtherFocusReason)
                        self.go(keys[i], focus=False)
                    elif i < 0 and switch.isVisible():
                        switch.setFocus(Qt.FocusReason.OtherFocusReason)  # past the first tab: the game
                return True
            target = nav.neighbour(scope, focus, direction)
            if isinstance(target, (TabButton, GameSwitch)) and not isinstance(focus, TabButton):
                self.tabs[self._current].setFocus(Qt.FocusReason.OtherFocusReason)
                return True
            if target is not None:
                nav.move(scope, focus, direction)
                return True
            if action in (Action.UP, Action.DOWN):
                nav.scroll_by(focus, -0.5 if action == Action.UP else 0.5)
            return True
        if action == Action.ACCEPT:
            if inside and hasattr(focus, "click") and focus.isEnabled():
                if isinstance(focus, TabButton):
                    self.go(focus.key)
                else:
                    focus.click()
            elif not inside:
                self.focus_scope_default()
            return True
        if action in (Action.NEXT, Action.PREV):
            nav.step(scope, focus if inside else None, action == Action.NEXT)
            return True
        if action in (Action.SCROLL_UP, Action.SCROLL_DOWN):
            step = -0.8 if action == Action.SCROLL_UP else 0.8
            if inside and nav.scroll_area_of(focus) is not None:
                nav.scroll_by(focus, step)
            elif not self.overlays and self.setup is None:
                nav.scroll_by(self.page.area.widget(), step)
            return True
        if self.setup is not None or self.overlays:
            return True
        if action in (Action.PREV_TAB, Action.NEXT_TAB):
            self.cycle(-1 if action == Action.PREV_TAB else 1)
            return True
        if action == Action.AUX:
            self.open_game_picker()
            return True
        if action == Action.MENU:
            self.go("home")
            return True
        if action == Action.BACK:
            if self._current != "home":
                self.go("home")
            else:
                self.request_quit()  # B at the top level, console style
            return True
        return True  # AUX / ALT with nothing to do here

    def refresh_hints(self) -> None:
        cover = getattr(self.pages.get("home"), "cover", None)
        covered = cover.hints() if alive(cover) else None
        if self.overlays:
            hints = self.overlays[-1].hints()
        elif covered is not None:
            hints = covered  # Home's countdown sheet
        elif self.setup is not None:
            hints = self.setup.hints()
        else:
            hints = [*self.page.hints(), ([Action.ACCEPT], "Select")]
            if self._current == "home" and self.switchable:
                hints.insert(0, ([Action.AUX], "Switch game"))
            hints.append(([Action.BACK], "Home" if self._current != "home" else "Quit"))
            hints.append(([Action.PREV_TAB, Action.NEXT_TAB], "Sections"))
        self.hintbar.set_hints(hints)

    def _on_focus_changed(self, old: QWidget | None, new: QWidget | None) -> None:
        if new is None:
            # The focused control was deleted, hidden or disabled and Qt found
            # nowhere to put focus. (Losing focus to another window is fine.)
            if alive(old) and old.window() is self and QApplication.activeWindow() is self:
                QTimer.singleShot(0, self, self._repair_focus)
            return
        if not alive(new) or new.window() is not self:
            return
        # Qt hands focus to the next widget in its chain when the focused one is
        # hidden or disabled (a tile that just went away, a page being switched
        # out, a section locked while files change). That can be the tab bar, or
        # a tile underneath an open sheet: put it back.
        scope = self.scope()
        escaped = scope is not self.chrome and not (new is scope or scope.isAncestorOf(new))
        gone = alive(old) and (not old.isVisible() or not old.isEnabled())
        bounced = isinstance(new, (TabButton, GameSwitch)) and gone and self.stack.isAncestorOf(old)
        if escaped or bounced:
            QTimer.singleShot(0, self, self.focus_scope_default)
        for page in self.pages.values():
            if page.isAncestorOf(new):
                page.remember(new)
        if self.router.mode != "mouse":
            nav.reveal(new)
        self.halo.follow(new)
        self.refresh_hints()  # sheets name what A does on the focused control

    def _repair_focus(self) -> None:
        focus = self.focusWidget()
        scope = self.scope()
        if not alive(focus) or not (focus is scope or scope.isAncestorOf(focus)) or not focus.isEnabled():
            self.focus_scope_default()

    def _on_controllers_changed(self) -> None:
        restyle_rows(self)  # a lit row's legend follows what is connected

    def _on_mode_changed(self, mode: str) -> None:
        self.halo.enabled = mode != "mouse"
        self.halo.follow(self.focusWidget())
        sync_lit(self.focusWidget())  # a mouse unlights the focused row, a pad lights it
        restyle_rows(self)
        self.refresh_hints()
        self.shell.update()  # tiles drop or regain their focus styling

    # --- Qt ---------------------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        rect = self.shell.rect()
        self.chrome.setGeometry(rect)
        self.halo.setGeometry(rect)
        for overlay in self.overlays:
            overlay.setGeometry(self._overlay_rect())
        self.toasts.relayout()
        self.halo.raise_()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.chrome.setGeometry(self.shell.rect())
        self.halo.setGeometry(self.shell.rect())
        self.halo.raise_()
        if self.focusWidget() is None or self.focusWidget() is self:
            self.focus_scope_default()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Q and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self.request_quit()
            return
        if event.key() == Qt.Key.Key_F11:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt signature)
        if self.busy:
            event.ignore()
            return
        if self.steam_launch is not None and self.steam_launch.decision is None:
            self.steam_launch.decision = launchhook.EXIT_CANCEL  # closing never starts the game
        # Order matters: stop polling, let in-flight worker jobs finish (so none
        # emit back into widgets being torn down), then stop the daemon.
        self.prepare_rebuild()
        QThreadPool.globalInstance().waitForDone(5000)
        try:
            self.service.shutdown()
        except Exception:
            pass
        super().closeEvent(event)
