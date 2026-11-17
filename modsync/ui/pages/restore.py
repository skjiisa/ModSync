"""The **Restore points** sheet: earlier versions of the MO2 instance's
profiles (``modsync.restore_points``), newest first, then the versions not
kept when settling conflicts and the files a copy set aside.

Choosing a restore point picks a profile in it (when it has more than one),
then shows what restoring that profile would change and which mods it
enables that are gone, before anything is touched. Restoring waits until
MO2 and the games are closed and keeps how things are as a restore point
first. An archive with nothing ModSync can restore offers its folder to open.

Opened from the Mod Organizer 2 sheet and from Sync. Every row is a plain
list row, so the d-pad, a mouse or a finger all reach it the same way.
"""

from __future__ import annotations

from functools import partial

import shiboken6

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QAbstractScrollArea, QVBoxLayout, QWidget

from modsync import restore_points as rp
from modsync.ui import nav, worker
from modsync.ui.input import Action
from modsync.ui.overlays import ConfirmSheet, PageSheet
from modsync.ui.pages import Page
from modsync.ui.widgets import Panel, Tile, clear_layout, label, scroller

SHOWN = 3  # profiles named on a row before "and N more"


def _profiles(names: list[str]) -> str:
    if len(names) <= SHOWN:
        return ", ".join(names)
    return f"{', '.join(names[:SHOWN])} and {len(names) - SHOWN} more"


class ScrollingConfirm(ConfirmSheet):
    """A ConfirmSheet for text and choices of any length: the text scrolls
    (the triggers, or PgUp/PgDn), and with ``scroll_rows`` so do the choices
    before the last one, which stays in view. A restore's preview or a point
    with dozens of profiles still fits 1280 × 800 with its buttons showing."""

    TEXT_HEIGHT = 300
    ROWS_HEIGHT = 330

    def __init__(self, host, title, text, choices, on_choice, *, default=None, scroll_rows=False) -> None:
        super().__init__(host, title, text, choices, on_choice, default=default)
        tiles = list(self.tiles.values())
        for w in (self.text, *tiles):
            self.body.removeWidget(w)
        while self.body.count() > 1:  # the spacing after the text
            self.body.takeAt(1)
        self.text_area = self._area([self.text], self.TEXT_HEIGHT)
        self.body.addWidget(self.text_area)
        self.body.addSpacing(6)
        if scroll_rows and len(tiles) > 1:
            self.rows_area = self._area(tiles[:-1], self.ROWS_HEIGHT, spacing=8)
            self.body.addWidget(self.rows_area)
            tiles = tiles[-1:]
        for tile in tiles:
            self.body.addWidget(tile)

    @staticmethod
    def _area(widgets, height: int, *, spacing: int = 0):
        content = QWidget()
        v = QVBoxLayout(content)
        v.setContentsMargins(0, 0, 10, 0)
        v.setSpacing(spacing)
        for w in widgets:
            v.addWidget(w)
        v.addStretch(1)
        area = scroller(content)
        area.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        area.setMaximumHeight(height)
        return area

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # As tall as the wrapped text, up to TEXT_HEIGHT: the scroll area can't
        # tell how tall a wrapped label is before it knows its width.
        width = self.sheet.width() - self.body.contentsMargins().left() - self.body.contentsMargins().right() - 16
        if width > 0:
            self.text_area.setFixedHeight(min(self.TEXT_HEIGHT, self.text.heightForWidth(width) + 2))

    @property
    def text_scrolls(self) -> bool:
        return self.text_area.verticalScrollBar().maximum() > 0

    def hints(self):
        return [([Action.SCROLL_DOWN], "Scroll"), *super().hints()] if self.text_scrolls else super().hints()

    def handle_action(self, action: Action) -> bool:
        if action in (Action.SCROLL_UP, Action.SCROLL_DOWN) and self.text_scrolls:
            nav.scroll_by(self.text, -0.8 if action == Action.SCROLL_UP else 0.8)
            return True
        return super().handle_action(action)  # otherwise the list around the focused row


def row_words(point: rp.Point) -> tuple[str, str]:
    """A row's title and description."""
    if point.kind == "point" and not point.profiles:
        return rp.when(point), f"{point.described}. No profiles: the instance had none then."
    if point.kind == "point":
        n = len(point.profiles)
        return rp.when(point), f"{point.described}. {'Profile' if n == 1 else 'Profiles'} {_profiles(point.profiles)}."
    if point.restorable:
        lists = "; ".join(f"{name}: {', '.join(sorted(point.files[name]))}" for name in point.profiles[:SHOWN])
        return rp.when(point), f"{point.described}. {lists}."
    n = point.other_files
    return rp.when(point), f"{point.described}. {n} file{'' if n == 1 else 's'} to open and move back by hand."


class RestorePage(Page):
    key = "restore"
    label = "Restore points"
    icon = "undo"

    def __init__(self, host) -> None:
        super().__init__(host)
        self.points: list[rp.Point] = []
        self.rows: list[Tile] = []
        self.save_tile: Tile | None = None
        self._loading = False
        self._just_opened = False
        self._recovering = False
        # Each preview asked for; a result for an older one, or for a sheet
        # closed since, is dropped.
        self._request = 0
        self._asking = False
        state = self.service.state
        self.header("Restore points", "Earlier versions of your Mod Organizer 2 profiles, kept on this machine. "
                                      "Restoring one puts a profile's mod list, plugins, load order and settings "
                                      "back as they were then.")
        if not state.has_instance:
            self.content_layout.addWidget(label("Choose a Mod Organizer 2 instance first. ModSync keeps restore "
                                                "points of its profiles from then on.", "note"))
            self.finish_layout()
            return
        left, right = self.columns(3, 2)
        left.setSpacing(4)
        self.status = label("Looking for restore points…", "secondary")
        left.addWidget(self.status)
        self.list = QVBoxLayout()
        self.list.setSpacing(4)
        left.addLayout(self.list)
        left.addStretch(1)
        right.setSpacing(12)
        self.save_tile = Tile("Save a restore point now", "Keep the profiles as they are now.", "check",
                              role="primary")
        self.save_tile.clicked.connect(self.save_now)
        right.addWidget(self.save_tile)
        info = Panel()
        self.usage = info.add(label("", "heading"))
        info.add(label(f"ModSync keeps the newest {rp.KEEP} on this machine. They never sync. It takes one when "
                       "the profiles changed: before Play, before you choose a version of a profile changed on "
                       "two machines, before copying another machine's setup, before a restore, and every few "
                       "minutes while the background service runs.", "secondary"))
        right.addWidget(info)
        right.addWidget(label(rp.LIMITS, "note"))
        right.addStretch(1)
        self.finish_layout()
        host.busyChanged.connect(self._update_enabled)

    # --- loading ---
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self.parentWidget() is self.host.stack:
            return  # shown for a moment while the window rebuilds, not opened
        self._just_opened = True
        self.reload()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._request += 1  # the sheet closed: a preview still coming is no longer wanted
        self._asking = False

    def _sheet_open(self) -> bool:
        """This page's sheet is open, with nothing over it."""
        top = self.host.top_overlay
        return isinstance(top, PageSheet) and top.page is self

    def reload(self) -> None:
        if self.save_tile is None or self._loading:
            return
        self._loading = True
        worker.run_async(self._load, on_done=self._show, on_failed=self._load_failed)

    def _load(self) -> tuple[list[rp.Point], rp.Usage, bool]:
        """Off the GUI thread: reading the points, and whether a restore was cut short."""
        return self.service.restore_points(), self.service.restore_usage(), self.service.restore_pending()

    def _load_failed(self, message: str) -> None:
        self._loading = False
        if shiboken6.isValid(self):
            self.status.setText(f"Couldn't read the restore points: {message}")

    def _show(self, result) -> None:
        self._loading = False
        if not shiboken6.isValid(self):
            return
        points, usage, pending = result
        if pending:
            self._recover()
        had_focus = any(row.hasFocus() for row in self.rows)
        self.points = points
        self.rows = []
        clear_layout(self.list)
        self.usage.setText(f"{usage.points} restore point{'' if usage.points == 1 else 's'}, "
                           f"{rp.size_words(usage.bytes)}")
        if not points:
            self.status.setText("None yet. ModSync takes the first one the next time the profiles change, or "
                                "save one now.")
        else:
            self.status.setText("Newest first. Choose one to see what restoring it would change.")
        for point in points:
            title, description = row_words(point)
            row = Tile(title, description, None, size="compact", chevron=True)
            row.clicked.connect(partial(self.choose, point))
            self.list.addWidget(row)
            self.rows.append(row)
        self._update_enabled()
        # Opened with focus waiting on Save (the rows weren't there yet): the
        # newest restore point is the likelier choice.
        opened, self._just_opened = self._just_opened, False
        if self.rows and (had_focus or (opened and self.save_tile.hasFocus())):
            self.rows[0].setFocus()

    def _update_enabled(self, *_args) -> None:
        busy = self.host.busy
        for tile in [*self.rows, self.save_tile]:
            if tile is not None:
                tile.setEnabled(not busy)

    def preferred_focus(self):
        return self.rows[0] if self.rows else self.save_tile

    def hints(self):
        return [([Action.ALT], "Save now")] if self.save_tile is not None else []

    def handle_action(self, action: Action) -> bool:
        if action == Action.ALT and self.save_tile is not None:
            self.save_now()
            return True
        return False

    def _recover(self) -> None:
        """See a restore that was cut short through, with the window busy: it
        may write a profile, so nothing may start MO2 meanwhile."""
        if self._recovering or self.host.busy:
            return
        self._recovering = True
        self.host.set_busy("restore", True)
        worker.run_async(self.service.finish_interrupted_restore, on_done=self._recovered,
                         on_failed=self._recover_failed)

    def _recovered(self, done: str) -> None:
        self._recovering = False
        self.host.set_busy("restore", False)
        words = {"finished": "A restore cut short earlier has been finished.",
                 "undone": "A restore cut short earlier couldn't be finished, so the profile is back as it was.",
                 "left": "⚠ A restore cut short earlier couldn't be finished or undone. Its profile is as it "
                         "was left."}
        if done in words:
            self.host.notify(words[done], None if done == "left" else "ok")
        if shiboken6.isValid(self) and done:
            self.reload()

    def _recover_failed(self, message: str) -> None:
        self._recovering = False
        self.host.set_busy("restore", False)
        self.host.notify(f"⚠ Couldn't finish a restore cut short earlier: {message}")

    # --- saving ---
    def save_now(self) -> None:
        if self.host.busy:
            return
        self.host.set_busy("restore", True)
        worker.run_async(self.service.save_restore_point, on_done=self._saved, on_failed=self._failed)

    def _saved(self, point: rp.Point | None) -> None:
        self.host.set_busy("restore", False)
        if point is None:
            self.host.notify("⚠ This instance has no profiles to keep yet.")
        else:
            self.host.notify(f"Saved a restore point of {_profiles(point.profiles)}.", "ok")
        if shiboken6.isValid(self):
            self.reload()

    def _failed(self, message: str) -> None:
        self.host.set_busy("restore", False)
        self.host.notify(f"⚠ {message}")
        if shiboken6.isValid(self):
            self.reload()

    # --- restoring ---
    def choose(self, point: rp.Point) -> None:
        if self.host.busy or self._asking:
            return
        if point.kind == "point" and not point.profiles:
            self.host.confirm(
                f"{point.described}, {rp.when(point)}",
                "The instance had no profiles when ModSync took this restore point, so there is nothing in it "
                "to put back.", [("close", "Close", "", "normal", "close")], lambda _key: None)
        elif not point.restorable:
            self._open_archive(point)
        elif len(point.profiles) == 1:
            self.ask(point, point.profiles[0])
        else:
            self._pick_profile(point)

    def _open_archive(self, point: rp.Point) -> None:
        path = str(point.path)

        def chosen(key: str | None) -> None:
            if key == "open":
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))

        what = ("Files set aside when copying from another machine: what was here before the copy and wasn't "
                "on the other machine, or was different there." if point.kind == "copy" else
                "Versions not kept when files changed on two machines.")
        self.host.confirm(
            f"{point.described}, {rp.when(point)}",
            f"{what} They are in {path}. ModSync can't tell which of them belong together, so move back "
            "anything you want to keep by hand, inside MO2's folders. It then syncs to every machine.",
            [("open", "Open the folder", "", "normal", "folder"), ("close", "Close", "", "normal", "close")],
            chosen, default="open")

    def _pick_profile(self, point: rp.Point) -> None:
        def chosen(key: str | None) -> None:
            if key and key.startswith("profile:"):
                self.ask(point, key.removeprefix("profile:"))

        whole = ("Each profile is restored whole: its mod list, plugins, load order and settings together."
                 if point.whole else "Only the files in it change for the profile you choose.")
        ScrollingConfirm(
            self.host, f"{point.described}, {rp.when(point)}",
            f"Which profile should go back to how it was then? {whole} The others stay as they are.",
            [*((f"profile:{name}", name, ", ".join(sorted(point.files[name])) if not point.whole else "",
                "normal", None) for name in point.profiles),
             ("cancel", "Cancel", "", "normal", "close")],
            chosen, scroll_rows=True).open()

    def ask(self, point: rp.Point, profile: str) -> None:
        """Show what restoring would change, then restore if asked. One at a
        time, and only into the sheet that asked."""
        if self._asking:
            return
        self._asking = True
        self._request += 1
        token = self._request
        worker.run_async(self._preview, point.id, profile, on_done=partial(self._ask, token),
                         on_failed=partial(self._failed_preview, token))

    def _wanted(self, token: int) -> bool:
        """The preview ``token`` is the latest, and its sheet is still open."""
        if not shiboken6.isValid(self) or token != self._request:
            return False
        self._asking = False
        return self._sheet_open()

    def _preview(self, point_id: str, profile: str):
        """Off the GUI thread: reading both versions, and whether MO2 or a game runs."""
        preview = self.service.restore_preview(point_id, profile)
        return preview, (self.service.restore_blocked() if not preview.unchanged else "")

    def _failed_preview(self, token: int, message: str) -> None:
        if not self._wanted(token):
            return
        self.host.notify(f"⚠ {message}")
        self.reload()

    def _ask(self, token: int, result) -> None:
        if not self._wanted(token):
            return
        preview, blocked = result
        point, profile = preview.point, preview.profile
        lines = "\n".join(f"•  {line}" if not line.startswith("    ") else f"      {line.strip()}"
                          for line in rp.preview_lines(preview))
        title = f"Restore {profile}"
        intro = f"Profile {profile} as it was {rp.when(point)} ({point.described.lower()})."
        if preview.unchanged:
            ScrollingConfirm(self.host, title, f"{intro}\n\n{lines}", [("close", "Close", "", "normal", "close")],
                             lambda _key: None).open()
            return
        state = self.service.reload_state()
        if state.syncing and state.sync_paused:
            synced = "Sync is paused, so the restored files reach your other machines once you resume it."
        elif state.syncing:
            synced = ("Sync waits until the profile is whole, then the restored files reach your other machines "
                      "like any other change.")
        else:
            synced = ""
        undo = "ModSync keeps the profiles as they are now as a restore point first, so you can undo this."
        text = "\n\n".join(filter(None, [intro, lines, synced, undo]))
        if blocked:
            # The reason first: below a long preview it could be out of sight.
            ScrollingConfirm(self.host, title, f"{blocked}\n\n{text}", [("close", "Close", "", "normal", "close")],
                             lambda _key: None).open()
            return

        def chosen(key: str | None) -> None:
            if key == "restore":
                self.restore(point, profile)

        ScrollingConfirm(
            self.host, title, text,
            [("restore", f"Restore {profile}", "Puts back all of its files at once." if point.whole else
              "Puts back the files in it at once.", "primary", "undo"),
             ("cancel", "Cancel", "Nothing changes.", "normal", "close")],
            chosen, default="cancel").open()

    def restore(self, point: rp.Point, profile: str) -> None:
        if self.host.busy:
            return
        self.host.set_busy("restore", True)
        self.host.notify(f"Restoring {profile}…")
        worker.run_async(self.service.restore_profile, point.id, profile,
                         on_done=partial(self._restored, profile), on_failed=self._failed)

    def _restored(self, profile: str, outcome) -> None:
        self.host.set_busy("restore", False)
        message = f"Restored {profile}. The newest restore point has it as it was just before."
        if outcome.synced and outcome.paused:
            message += " Resume syncing to send it to your other machines."
        self.host.notify(message, "ok")
        if outcome.preview.missing_mods:
            n = len(outcome.preview.missing_mods)
            self.host.notify(f"⚠ {n} mod{' it enables is' if n == 1 else 's it enables are'} missing. Install "
                             f"{'it' if n == 1 else 'them'} again for the profile to work as it did.")
        if shiboken6.isValid(self):
            self.reload()
