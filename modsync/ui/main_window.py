"""The one ModSync window, built like a console app.

A row of sections across the top (Home, Game, Mod Organizer, Sync, System),
switched with the bumpers or Q/E, or by moving up onto them. The section's
tiles fill the middle and the hint bar along the bottom says what each button
does. Everything that used to be a dialog is a sheet over the window, and a
glowing halo marks focus. The setup wizard takes over the middle when it runs.

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
from functools import partial

import shiboken6
from PySide6.QtCore import QThreadPool, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QHBoxLayout, QMainWindow, QStackedWidget, QVBoxLayout, QWidget

from modsync import __version__, launchhook
from modsync.service import ModSyncService
from modsync.ui import nav, worker
from modsync.ui.input import Action, DIRECTIONS, InputRouter
from modsync.ui.overlays import ConfirmSheet, Overlay
from modsync.ui.pages import Page
from modsync.ui.pages.game import GamePage
from modsync.ui.pages.home import HomePage
from modsync.ui.pages.mods import ModsPage
from modsync.ui.pages.setup import SetupFlow
from modsync.ui.pages.sync import SyncPage
from modsync.ui.pages.system import SystemPage
from modsync.ui.widgets import (
    Backdrop, FocusHalo, GlyphLabel, HintBar, Icon, TabButton, Toasts, discard, label, pill,
)

# Testing aid: MODSYNC_HUB_AUTO_DECISION=continue|cancel decides a Steam launch by
# itself after a few seconds (continue = Play, once an MO2 instance is chosen), so the whole Steam → hook → ModSync → game chain
# can be exercised without a hand on the controller (e.g. from the game's
# launch options).
_AUTO_DECISION_ENV = "MODSYNC_HUB_AUTO_DECISION"
_AUTO_DECISION_MS = 3000
_POLL_MS = 4000

PAGES = (HomePage, GamePage, ModsPage, SyncPage, SystemPage)


def alive(w: QWidget | None) -> bool:
    return w is not None and shiboken6.isValid(w)


class TopBar(QWidget):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.setFixedHeight(72)
        h = QHBoxLayout(self)
        h.setContentsMargins(28, 12, 28, 4)
        h.setSpacing(10)
        mark = Icon("sync", 40)
        h.addWidget(mark)
        h.addWidget(label("ModSync", "heading", wrap=False))
        h.addSpacing(18)
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
            h.addSpacing(8)
        self.clock = label("", "secondary", wrap=False)
        h.addWidget(self.clock)
        self._tabs_on = True
        self.router = window.router
        window.router.modeChanged.connect(self._update_glyphs)
        self._update_glyphs()
        self._tick()
        timer = QTimer(self)
        timer.timeout.connect(self._tick)
        timer.start(15000)

    def _tick(self) -> None:
        self.clock.setText(time.strftime("%H:%M"))

    def set_tabs_visible(self, on: bool) -> None:
        for i in range(self.tabs_row.count()):
            w = self.tabs_row.itemAt(i).widget()
            if w is not None:
                w.setVisible(on)
        self._tabs_on = on
        self._update_glyphs()

    def _update_glyphs(self, *_args) -> None:
        show = self._tabs_on and self.router.mode != "mouse"
        self.lb.setVisible(show)
        self.rb.setVisible(show)


class MainWindow(QMainWindow):
    busyChanged = Signal(bool)
    gameChecked = Signal(object, object)  # GameStatus, VersionCheck
    syncStatus = Signal(object)  # SyncStatus
    setupDescribed = Signal(list)  # the instance's profile line and problems
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
        self._busy: set[str] = set()
        self._rebuild_pending = False
        self._current = "home"
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

        app = QApplication.instance()
        app.focusChanged.connect(self._on_focus_changed)
        self.router.modeChanged.connect(self._on_mode_changed)
        self._on_mode_changed(self.router.mode)
        self.go("home")

        auto = os.environ.get(_AUTO_DECISION_ENV, "").strip().lower()
        if steam_launch is not None and auto in ("continue", "cancel"):
            self.notify(f"Test mode: choosing \"{auto}\" automatically in a moment.")
            if auto == "cancel":
                decide = partial(self.decide_launch, launchhook.EXIT_CANCEL)
            elif self.service.state.has_instance:
                decide = partial(self.launch, play=True)  # what the Play tile does
            else:
                decide = partial(self.decide_launch, launchhook.EXIT_CONTINUE)
            QTimer.singleShot(_AUTO_DECISION_MS, self, decide)

    # --- pages ---------------------------------------------------------------------
    def _build_pages(self) -> None:
        for page_cls in PAGES:
            page = page_cls(self)
            self.pages[page.key] = page
            self.stack.addWidget(page)
            tab = TabButton(page.key, page.label, page.icon)
            tab.clicked.connect(lambda _=False, k=page.key: self.go(k, focus=self.router.mode != "mouse"))
            self.tabs[page.key] = tab
            self.topbar.tabs_row.addWidget(tab)
            tab.setVisible(self.setup is None)  # rebuilt while the wizard is open
        sync = self.pages["sync"]
        sync.synced.connect(self.pages["game"].panel.refresh)  # mods just arrived: re-check SKSE/version

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
        return self.pages[self._current]

    def go(self, key: str, *, focus: bool = True) -> None:
        if key not in self.pages or self.setup is not None:
            return
        old = self._current
        self._current = key
        for k, tab in self.tabs.items():
            tab.setChecked(k == key)
        page = self.pages[key]
        if self.stack.currentWidget() is not page:
            self.stack.setCurrentWidget(page)
            keys = list(self.pages)
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

    def cycle(self, step: int) -> None:
        keys = list(self.pages)
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
        for overlay in list(self.overlays):
            overlay.dismiss()
        self._clear_pages()
        self._build_pages()
        self._timer.start(_POLL_MS)
        self._current = current if current in self.pages else "home"
        self.stack.setCurrentWidget(self.pages[self._current])
        self.go(self._current)

    # --- setup wizard ---
    def start_setup(self) -> SetupFlow | None:
        if self.busy or self.setup is not None:
            return None
        self.setup = SetupFlow(self)
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

    def confirm(self, title, text, choices, on_choice, *, default=None, eyebrow="") -> ConfirmSheet:
        sheet = ConfirmSheet(self, title, text, choices, on_choice, default=default, eyebrow=eyebrow)
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
            self.setup.go_to(2)  # sharing from here: the game step comes next
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

    def instance_chosen(self, path: str) -> None:
        if self.setup is not None:
            self.setup.go_to(1)
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
    def launch(self, *, play: bool) -> None:
        """Play / Open MO2. Steam waiting: the same launch, prepared here and
        handed to the hook, which runs it as Steam's own launch."""
        if self.busy or (self.steam_launch is not None and self.steam_launch.decision is not None):
            return
        self.set_busy("launch", True)
        self.notify("Starting Skyrim through MO2…" if play else "Opening MO2…")
        if self.steam_launch is not None:
            worker.run_async(self.service.prepare_mo2, play=play, on_done=self._on_prepared,
                             on_failed=self._on_launch_failed)
            return
        worker.run_async(self.service.launch_mo2, play=play, on_done=self._on_launched,
                         on_failed=self._on_launch_failed)

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
        for message in self.service.launcher.poll():
            self.notify(f"⚠ {message}")
        self._apply_busy()  # a game started from here may have exited

    # --- quitting ---
    def request_quit(self) -> None:
        """Ask before closing: B on Home, Quit under System, or Ctrl+Q. Closing
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
            "Syncing and a queued Steam pin carry on only if the background service is on (under System)."
        )

        def chosen(key: str | None) -> None:
            if key == "quit":
                self.close()

        sheet = self.confirm(
            "Quit ModSync?", text,
            [("quit", "Quit and return to Steam" if steam is not None else "Quit ModSync", "", "primary", "power"),
             ("stay", "Stay", "", "normal", "close")],
            chosen, eyebrow="ModSync")
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
        focus = self.focusWidget()
        scope = self.scope()
        inside = alive(focus) and (focus is scope or scope.isAncestorOf(focus))
        if action in DIRECTIONS:
            if not inside:
                self.focus_scope_default()
                return True
            direction = action.value
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
                return True
            target = nav.neighbour(scope, focus, direction)
            if isinstance(target, TabButton) and not isinstance(focus, TabButton):
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
        if self.overlays:
            hints = self.overlays[-1].hints()
        elif self.setup is not None:
            hints = self.setup.hints()
        else:
            hints = [*self.page.hints(), ([Action.ACCEPT], "Select")]
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
        bounced = isinstance(new, TabButton) and gone and self.stack.isAncestorOf(old)
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

    def _on_mode_changed(self, mode: str) -> None:
        self.halo.enabled = mode != "mouse"
        self.halo.follow(self.focusWidget())
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
