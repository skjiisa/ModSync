"""The setup wizard: three full-screen steps built from the same parts as the
sections.

1. Mod Organizer 2: an instance found here, a folder, or a fresh install.
   It is remembered as soon as it is chosen.
2. Sync, optional: not now, share from here, or copy from another machine.
   A machine copying its mods can only check the game version once they have
   arrived, so joining ends the wizard and Home takes over.
3. Game version: the Game section's panel, then Finish.

B (Escape) goes back a step, and from the first step leaves the wizard.
Nothing in it is a one-way door.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

from modsync.ui import nav, theme
from modsync.ui.input import Action
from modsync.ui.pages.game import GamePanel
from modsync.ui.pages.mods import Mo2Chooser
from modsync.ui.pages.sync import JoinPanel, create_vault
from modsync.ui.widgets import Panel, PageHeader, Tile, scroller

STEPS = (
    ("Choose your Mod Organizer 2 instance",
     "Pick one found on this machine, browse to one, or install a fresh one here."),
    ("Sync with another machine",
     "Optional. Keep this setup on another machine, such as a desktop and a Steam Deck."),
    ("Game version",
     "SKSE and native DLL mods only load on the exact game version they were built for. If there is "
     "nothing to fix, finish."),
)


class StepTrack(QWidget):
    """Three segments filling up as the wizard moves on."""

    def __init__(self) -> None:
        super().__init__()
        self.index = 0
        self.setFixedHeight(8)

    def set_index(self, index: int) -> None:
        self.index = index
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        gap = 10
        w = (self.width() - gap * (len(STEPS) - 1)) / len(STEPS)
        for i in range(len(STEPS)):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color("accent") if i <= self.index else theme.color("raised"))
            p.drawRoundedRect(int(i * (w + gap)), 0, int(w), self.height(), 4, 4)


class SetupFlow(QWidget):
    def __init__(self, host, *, installer=None) -> None:
        super().__init__()
        self.host = host
        self.service = host.service
        self.index = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 18, 40, 20)
        outer.setSpacing(14)
        self.track = StepTrack()
        outer.addWidget(self.track)
        self.head = PageHeader("", "")
        outer.addWidget(self.head)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

        # 1 · Mod Organizer 2
        step1 = QWidget()
        v1 = QVBoxLayout(step1)
        v1.setContentsMargins(0, 0, 0, 0)
        v1.setSpacing(12)
        self.keep: Tile | None = None
        if self.service.state.has_instance:
            self.keep = Tile(f"Keep using {self.service.state.instance_label}", self.service.state.instance_path,
                             "check", role="primary", chevron=True)
            self.keep.clicked.connect(lambda: self.go_to(1))
            v1.addWidget(self.keep)
        self.chooser = Mo2Chooser(host, installer=installer)  # host.instance_chosen moves on
        v1.addWidget(self.chooser)
        v1.addStretch(1)

        # 2 · Sync
        step2 = QWidget()
        v2 = QVBoxLayout(step2)
        v2.setContentsMargins(0, 0, 0, 0)
        v2.setSpacing(12)
        if self.service.state.syncing:
            self.keep_sync = Tile("Keep syncing", "This instance is already shared.", "sync", role="primary",
                                  chevron=True)
            self.keep_sync.clicked.connect(lambda: self.go_to(2))
            v2.addWidget(self.keep_sync)
            self.local = self.share = self.copy = None
        else:
            row = QHBoxLayout()
            row.setSpacing(16)
            self.local = Tile("Not now", "Just use this machine. Sync can be set up any time later.", "check",
                              size="choice")
            self.local.clicked.connect(lambda: self.go_to(2))
            self.share = Tile("This machine has the mods", "Share from here and get a pairing code for the "
                              "other machine.", "devices", size="choice")
            self.share.clicked.connect(lambda: create_vault(host))
            self.copy = Tile("Copy from another machine", "Find the machine with the mods, or paste its "
                             "pairing code.", "download", size="choice")
            self.copy.clicked.connect(self._show_join)
            for tile in (self.local, self.share, self.copy):
                row.addWidget(tile)
            v2.addLayout(row)
        self.join = JoinPanel(host)  # host.vault_joined finishes the wizard
        self.join_panel = Panel()
        self.join_panel.add(self.join)
        self.join_panel.setVisible(False)
        v2.addWidget(self.join_panel)
        v2.addStretch(1)

        # 3 · Game version
        step3 = QWidget()
        v3 = QVBoxLayout(step3)
        v3.setContentsMargins(0, 0, 0, 0)
        v3.setSpacing(16)
        # Finish leads the step, so it is never below the fold when there is
        # nothing to fix.
        self.finish_tile = Tile("Finish", "Go to Home. Everything here stays available there.", "check",
                                role="primary", size="compact")
        self.finish_tile.clicked.connect(self.finish)
        v3.addWidget(self.finish_tile)
        self.game = GamePanel(host)
        self.game.status.connect(host.notify)
        self.game.busyChanged.connect(lambda on: host.set_busy("setup-game", on))
        self.game.checked.connect(self._on_game_checked)
        self._unresolved = ""
        v3.addWidget(self.game)
        v3.addStretch(1)

        for step in (step1, step2, step3):
            self.stack.addWidget(scroller(step))
        host.busyChanged.connect(self._on_busy)
        self.go_to(0, focus=False)

    @property
    def busy(self) -> bool:
        return self.host.busy

    def _on_busy(self, busy: bool) -> None:
        # A join or share in flight, a file operation, or a game started from
        # here: hold the choices, and never rewrite game files under a game.
        for tile in (self.local, self.share, self.copy, self.keep):
            if tile is not None:
                tile.setEnabled(not busy)
        self.chooser.setEnabled(not busy)
        keep_sync = getattr(self, "keep_sync", None)
        if keep_sync is not None:
            keep_sync.setEnabled(not busy)
        self.join_panel.setEnabled(not busy)
        self.game.setEnabled(self.game.busy or not (self.host.launching or self.host.busy_except("setup-game")))
        self.finish_tile.setEnabled(not busy)

    def go_to(self, index: int, *, focus: bool = True) -> None:
        self.index = max(0, min(index, len(STEPS) - 1))
        title, subtitle = STEPS[self.index]
        self.head.eyebrow.setText(f"SETUP  ·  STEP {self.index + 1} OF {len(STEPS)}")
        self.head.title.setText(title)
        self.head.set_subtitle(subtitle)
        self.track.set_index(self.index)
        self.stack.setCurrentIndex(self.index)
        if self.index == 2:
            self.game.refresh()  # the instance was chosen in step 1
        self.host.refresh_hints()
        if focus:
            self.focus_default()

    @staticmethod
    def unresolved(st, vc) -> str:
        """Why finishing now would leave the game unready for the mods, or ""."""
        if st.steam_updating:
            return ""
        if st.needs_downgrade or vc.mismatch:
            return f"Skyrim is {st.installed}, but your mods need {st.wanted or vc.expected}."
        if st.skse_state in ("wrong", "several"):
            return "SKSE doesn't match this version of Skyrim."
        if st.needs_pin:
            return "Steam has an update waiting that would change Skyrim's version."
        return ""

    def _on_game_checked(self, st, vc) -> None:
        self._unresolved = self.unresolved(st, vc)
        if self._unresolved:
            self.finish_tile.setText("Finish anyway")
            self.finish_tile.set_role("normal")
            self.finish_tile.set_description(f"{self._unresolved} You can fix it later under Game.")
        else:
            self.finish_tile.setText("Finish")
            self.finish_tile.set_role("primary")
            self.finish_tile.set_description("Go to Home. Everything here stays available there.")
        repair = self.repair_tile()
        if self.index == 2 and repair is not None and self.host.focusWidget() is self.finish_tile:
            repair.setFocus(Qt.FocusReason.OtherFocusReason)

    def repair_tile(self) -> Tile | None:
        """The action that fixes what is unresolved, when there is one."""
        if not self._unresolved:
            return None
        g = self.game
        return next((t for t in (g.downgrade, g.skse, g.pin, g.adopt) if t.isVisibleTo(self)), None)

    def focus_default(self) -> None:
        page = self.stack.currentWidget()
        preferred = {0: self.keep or (self.chooser.found_tiles[0] if self.chooser.found_tiles else None),
                     1: self.local or getattr(self, "keep_sync", None),
                     2: self.repair_tile() or self.finish_tile}.get(self.index)
        candidates = nav.focusables(page)
        target = preferred if preferred in candidates else (nav.reading_order(candidates, page) or [None])[0]
        if target is not None:
            target.setFocus(Qt.FocusReason.OtherFocusReason)

    def _show_join(self) -> None:
        self.join_panel.setVisible(True)
        self.join.scan()
        self.join.scan_tile.setFocus()

    def finish(self) -> None:
        if not self.busy:
            self.host.finish_setup()

    def hints(self) -> list[tuple[list[Action], str]]:
        return [([Action.ACCEPT], "Select"), ([Action.BACK], "Leave setup" if self.index == 0 else "Previous step")]

    def handle_action(self, action: Action) -> bool:
        if action == Action.BACK:
            if self.busy:
                return True
            if self.index == 0:
                self.host.finish_setup()
            else:
                self.go_to(self.index - 1)
            return True
        if action in (Action.PREV_TAB, Action.NEXT_TAB, Action.MENU):
            return True
        return False

    def shutdown(self) -> None:
        pass
