"""The **Sync** section, the optional part of ModSync.

Before the user opts in it offers two big choices: share this instance from
here, or copy another machine's. Copying asks first when this machine already
has mods: copy (set what's here aside) or merge. Once a vault exists it becomes
the live view: a completion ring, the devices, and pairing (a big PIN for
"Pair over network", or the code and its QR). While a copy is under way the
ring follows it instead, and nothing else can be paired. "Stop syncing" pauses
the vault or leaves it, keeping the instance either way. Files changed on two
machines at once, and files a copy set aside, are offered for review here.

``JoinPanel`` (scan, pick a machine, enter its PIN, or paste a code) is also
part of the setup flow's sync step.
"""

from __future__ import annotations

import socket
import threading
from functools import partial

import shiboken6

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from modsync import pairing_lan
from modsync.pairing_code import PairingCode
from modsync.service import COPY_ARCHIVE_DIR, SyncStatus
from modsync.sync import conflicts
from modsync.ui import worker
from modsync.ui.input import Action
from modsync.ui.overlays import BeaconSheet, CodeSheet, KeyboardSheet, PinSheet
from modsync.ui.pages import Page
from modsync.ui.widgets import Dot, Panel, ProgressRing, Tile, clear_layout, label


def _validate_code(text: str) -> str | None:
    try:
        PairingCode.decode(text)
    except Exception:
        return "That doesn't look like a valid pairing code. It starts with MODSYNC1-."
    return None


class JoinPanel(QWidget):
    """Copy the setup from the machine that has it: scan the network and pick
    it (then type its PIN), type its address, or paste its pairing code.

    Only real scan results become choosable tiles. "Scanning…" and error lines
    are plain text, so a stale or placeholder row can never be picked."""

    def __init__(self, host) -> None:
        super().__init__()
        self.host = host
        self.service = host.service
        self.announcements: list[pairing_lan.Announcement] = []
        self.machine_tiles: list[Tile] = []
        self._scanning = False
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(label("Pick the machine with the mods. It must be showing a PIN under "
                             "\"Pair over network\".", "secondary"), 1)
        v.addLayout(head)
        self.scan_tile = Tile("Scan network", "Look for machines showing a pairing PIN.", "radar", size="compact")
        self.scan_tile.clicked.connect(self.scan)
        v.addWidget(self.scan_tile)
        self.results = QVBoxLayout()
        self.results.setSpacing(10)
        v.addLayout(self.results)
        self.address_tile = Tile("Not listed? Enter its address…", "Use the address shown under \"Pair over "
                                 "network\" on that machine.", "globe", size="compact")
        self.address_tile.clicked.connect(lambda: self.ask_pin(None))
        v.addWidget(self.address_tile)
        self.code_tile = Tile("Paste a pairing code…", "Join with the MODSYNC1-… code from the other machine.",
                              "qr", size="compact")
        self.code_tile.clicked.connect(self.enter_code)
        v.addWidget(self.code_tile)

    @property
    def placeholder_texts(self) -> list[str]:
        return [self.results.itemAt(i).widget().text() for i in range(self.results.count())
                if not isinstance(self.results.itemAt(i).widget(), Tile)]

    def _clear(self) -> None:
        self.machine_tiles = []
        clear_layout(self.results)

    def scan(self) -> None:
        if self._scanning:
            return  # repeated requests cannot overlap
        self._scanning = True
        self.announcements = []
        self.scan_tile.setEnabled(False)
        self.scan_tile.setText("Scanning…")
        self._clear()
        self.results.addWidget(label("Listening for machines showing a PIN…", "muted"))
        worker.run_async(pairing_lan.discover, on_done=self.on_scanned, on_failed=self.on_scan_failed, timeout=3.0)

    def on_scanned(self, anns: list) -> None:
        self._scanning = False
        self.scan_tile.setEnabled(True)
        self.scan_tile.setText("Scan again")
        self.announcements = list(anns)
        self._clear()
        if not anns:
            self.results.addWidget(label(
                "No machines found. Start \"Pair over network\" on the other machine, then scan again.", "note"))
            chk = self.host.firewall_check
            if chk is not None and chk.firewall is not None and not chk.allowed:
                self.results.addWidget(label(
                    f"{chk.firewall.kind} is on here and drops their announcements. Use \"Allow in "
                    "firewall…\" under System, then scan again.", "warning"))
            else:
                self.results.addWidget(label(pairing_lan.FIREWALL_HINT, "muted"))
            return
        for ann in anns:
            tile = Tile(ann.name, f"{ann.host}  ·  showing a PIN", "devices", role="primary", chevron=True)
            tile.clicked.connect(lambda _=False, a=ann: self.ask_pin(a))
            self.results.addWidget(tile)
            tile.show()  # remain available when the page is opened again
            self.machine_tiles.append(tile)
        if self.isVisible():
            self.machine_tiles[0].setFocus()

    def on_scan_failed(self, message: str) -> None:
        self._scanning = False
        self.announcements = []
        self.scan_tile.setEnabled(True)
        self.scan_tile.setText("Scan again")
        self._clear()
        self.results.addWidget(label(f"Scan failed: {message}", "warning"))

    def ask_pin(self, announcement: pairing_lan.Announcement | None) -> PinSheet:
        sheet = PinSheet(self.host, announcement, self.join_network)
        sheet.open()
        return sheet

    def choose_mode(self, path: str, proceed) -> None:
        """Copy unless this machine has mods of its own; then ask. ``proceed``
        gets ``merge``."""
        n = self.service.local_mod_count(path)
        if not n:
            proceed(False)
            return

        def chosen(key: str | None) -> None:
            if key in ("copy", "merge"):
                proceed(key == "merge")

        self.host.confirm(
            "This machine already has mods",
            f"There are {n} mods and downloads here already. Copying makes this machine match the other "
            f"one: whatever is here that the other machine doesn't have, or has differently, moves to "
            f"{COPY_ARCHIVE_DIR} in the instance. Nothing is deleted, and files that already match aren't "
            "downloaded again.",
            [("copy", "Copy the other machine", "This machine ends up the same as the other one. The other "
              "machine isn't changed.", "primary", "download"),
             ("merge", "Merge both machines", "Mods from both end up on both. Where a file differs, the newer "
              "one wins everywhere, so one machine's mod list changes may be lost.", "normal", "sync"),
             ("cancel", "Cancel", "", "normal", "close")],
            chosen, default="copy", eyebrow="Copy from another machine")

    def join_network(self, target: pairing_lan.Announcement, pin: str) -> None:
        path = self.service.state.instance_path
        if not path:
            return
        if len(pin) != 6 or not pin.isdigit():
            self.host.notify("⚠ Enter the 6-digit PIN shown on the other machine.")
            return
        host = self.host

        def join(merge: bool) -> None:
            host.sync_started(f"Pairing with {target.name}…")
            worker.run_async(self.service.join_via_network, target, pin, path, merge=merge,
                             on_done=lambda _: host.vault_joined(), on_failed=host.sync_failed)

        self.choose_mode(path, join)

    def enter_code(self) -> None:
        KeyboardSheet(self.host, "Pairing code", "Paste the pairing code from the other machine.",
                      self.join_code, placeholder="MODSYNC1-…", validate=_validate_code, eyebrow="Join").open()

    def join_code(self, text: str) -> None:
        path = self.service.state.instance_path
        if not path:
            return
        try:
            code = PairingCode.decode(text)
        except Exception:
            self.host.notify("⚠ That doesn't look like a valid pairing code.")
            return
        host = self.host

        def join(merge: bool) -> None:
            host.sync_started("Joining…")
            worker.run_async(self.service.join_vault, code, path, merge=merge,
                             on_done=lambda _: host.vault_joined(), on_failed=host.sync_failed)

        self.choose_mode(path, join)


def create_vault(host) -> None:
    """Share this instance. ``host.vault_created`` moves the wizard on, or
    rebuilds the window, once Syncthing is up."""
    path = host.service.state.instance_path
    if not path or host.busy:
        return
    host.sync_started("Creating a vault and starting Syncthing…")
    worker.run_async(host.service.create_vault, path, host.service.state.instance_label,
                     on_done=lambda _: host.vault_created(), on_failed=host.sync_failed)


class SyncPage(Page):
    key = "sync"
    label = "Sync"
    icon = "sync"

    synced = Signal()  # the folder just reached 100%: mods from the other machine are here
    progress = Signal(str, int)  # every status poll: folder state, percent in sync here

    def __init__(self, host) -> None:
        super().__init__(host)
        state = self.service.state
        self.live = state.syncing
        self.join: JoinPanel | None = None
        self.beacon: BeaconSheet | None = None
        self._pairing = False
        self._pair_stop: threading.Event | None = None
        self._code: str = ""
        self._was_complete: bool | None = None
        self.header("Sync", "Sync with another machine",
                    "Optional. Mods, load order and downloads stay identical, and each machine keeps its own "
                    "game paths. A desktop and a Steam Deck, for example.")
        if self.live:
            self._build_live()
        elif state.has_instance:
            self._build_offer()
        else:
            self.choose_first = Tile("Choose Mod Organizer 2 first", "Sync shares an MO2 instance, so pick or "
                                     "install one before pairing.", "box", chevron=True)
            self.choose_first.clicked.connect(lambda: host.go("mods"))
            self.content_layout.addWidget(self.choose_first)
        self.finish_layout()

    # --- offer --------------------------------------------------------------------
    def _build_offer(self) -> None:
        row = QHBoxLayout()
        row.setSpacing(20)
        self.share = Tile("This machine has the mods", "Share this instance and get a pairing code and PIN "
                          "for the other machine.", "devices", size="choice")
        self.share.clicked.connect(lambda: create_vault(self.host))
        self.copy = Tile("Copy from another machine", "Find the machine with the mods on your network, or "
                         "paste its pairing code.", "download", size="choice")
        self.copy.clicked.connect(self.toggle_join)
        row.addWidget(self.share)
        row.addWidget(self.copy)
        self.content_layout.addLayout(row)
        self.join = JoinPanel(self.host)
        self.join.setVisible(False)
        self.join_panel = Panel()
        self.join_panel.add(label("COPY FROM ANOTHER MACHINE", "eyebrow", wrap=False))
        self.join_panel.add(self.join)
        self.join_panel.setVisible(False)
        self.content_layout.addWidget(self.join_panel)

    def toggle_join(self) -> None:
        show = not self.join_panel.isVisible()
        self.join_panel.setVisible(show)
        self.join.setVisible(show)
        if show:
            self.join.scan()
            self.join.scan_tile.setFocus()

    def preferred_focus(self):
        if self.live:
            return self.resume_tile or self.pair_tile
        return getattr(self, "share", None) or getattr(self, "choose_first", None)

    # --- live ----------------------------------------------------------------------
    def _build_live(self) -> None:
        left, right = self.columns(1, 1)
        gauge = Panel()
        top = QHBoxLayout()
        top.setSpacing(24)
        self.ring = ProgressRing(190)
        self.ring.set_value(0, "starting", busy=True)
        top.addWidget(self.ring)
        info = QVBoxLayout()
        info.setSpacing(6)
        info.addWidget(label("THIS MACHINE", "eyebrow", wrap=False))
        self.folder_state = label("Starting Syncthing…", "heading")
        info.addWidget(self.folder_state)
        self.folder_detail = label("", "secondary")
        info.addWidget(self.folder_detail)
        info.addStretch(1)
        top.addLayout(info, 1)
        gauge.layout_.addLayout(top)
        left.addWidget(gauge)
        devices = Panel()
        devices.add(label("DEVICES", "eyebrow", wrap=False))
        self.devices = QVBoxLayout()
        self.devices.setSpacing(10)
        devices.layout_.addLayout(self.devices)
        left.addWidget(devices)
        left.addStretch(1)

        state = self.service.state
        self._built_copying = state.copying
        self.conflicts_tile = Tile("Files changed on two machines", "", "warning", role="primary", chevron=True)
        self.conflicts_tile.clicked.connect(self.review_conflicts)
        self.conflicts_tile.setVisible(False)
        right.addWidget(self.conflicts_tile)
        self.set_aside_tile = None
        if state.set_aside:
            self.set_aside_tile = Tile("Files set aside when joining", "What was here before the copy and "
                                       "isn't on the other machine.", "folder", chevron=True)
            self.set_aside_tile.clicked.connect(self.review_set_aside)
            right.addWidget(self.set_aside_tile)
        self.resume_tile = None
        if state.copying:
            # Nothing else can be paired until this machine is a full copy.
            self.pair_tile = Tile("Stop copying…", "Leave the vault. What has arrived stays here.", "stop",
                                  role="danger")
            self.pair_tile.clicked.connect(self.stop_copy)
            tiles = [self.pair_tile]
        else:
            self.pair_tile = Tile("Pair over network…", "Show a PIN so another machine can find and join this "
                                  "one.", "radar", role="normal" if state.sync_paused else "primary")
            self.pair_tile.clicked.connect(self.pair_network)
            code = Tile("Show pairing code", "The code and QR another machine can join with.", "qr")
            code.clicked.connect(self.show_code)
            add = Tile("Add a machine by code…", "Paste the pairing code of a machine to add.", "plus")
            add.clicked.connect(self.add_device)
            rescan = Tile("Rescan", "Look for changed files now instead of waiting.", "refresh", size="compact")
            rescan.clicked.connect(self.rescan)
            tiles = [self.pair_tile, code, add, rescan]
            if state.sync_paused:
                self.resume_tile = Tile("Resume syncing", "Carry over what changed on each machine meanwhile.",
                                        "play", role="primary")
                self.resume_tile.clicked.connect(lambda: self.set_paused(False))
                tiles.insert(0, self.resume_tile)
        open_ui = Tile("Open Syncthing UI", "The engine underneath, in your browser.", "globe", size="compact")
        open_ui.clicked.connect(self.open_ui)
        tiles.append(open_ui)
        if not state.copying:
            stop = Tile("Stop syncing…", "Pause, or leave the vault. Mods are not deleted either way.",
                        "stop", role="danger", size="compact")
            stop.clicked.connect(self.stop_sync)
            tiles.append(stop)
        for tile in tiles:
            right.addWidget(tile)
        right.addStretch(1)
        self._names: dict[str, str] = {}
        self._load_code()
        self.refresh()
        self._accept_pending()

    def hints(self):
        return [([Action.ALT], "Rescan")] if self.live else []

    def handle_action(self, action: Action) -> bool:
        if self.live and action == Action.ALT:
            self.rescan()
            return True
        return False

    def poll(self) -> None:
        if not self.live:
            return
        self.refresh()
        self._accept_pending()

    def _load_code(self) -> None:
        worker.run_async(self.service.my_pairing_code, on_done=self._on_code,
                         on_failed=lambda m: self.host.notify(f"⚠ {m}"))

    def _on_code(self, code: PairingCode | None) -> None:
        if code:
            self._code = code.encode()

    def refresh(self) -> None:
        if self.live:
            worker.run_async(self.service.status, on_done=self.on_status,
                             on_failed=lambda m: self.host.notify(f"⚠ {m}"))

    def on_status(self, status: SyncStatus) -> None:
        self._names = {d.id[:7]: d.name or d.id[:7] for d in status.devices}
        self._names[status.device_id[:7]] = "this machine"
        self.conflicts_tile.setVisible(bool(status.conflicts))
        if status.conflicts:
            n = status.conflicts
            self.conflicts_tile.setText(f"{n} file{' was' if n == 1 else 's were'} changed on two machines")
            self.conflicts_tile.set_description("MO2 uses the newer version of each. Choose which to keep.")
        clear_layout(self.devices)
        if not status.devices:
            self.devices.addWidget(label("No other devices yet. Pair one to start syncing.", "muted"))
        for dev in status.devices:
            row = QHBoxLayout()
            row.setSpacing(12)
            row.addWidget(Dot("ok" if dev.connected else "off"))
            row.addWidget(label(dev.name or dev.id[:13], "heading", wrap=False), 1)
            row.addWidget(label("connected" if dev.connected else "offline", "ok" if dev.connected else "muted",
                                wrap=False))
            self.devices.addLayout(row)

        if status.copy is not None:
            self._show_copy(status)
            return
        if self._built_copying and not self.service.state.copying:
            # The background service finished the copy, or it was left elsewhere.
            self.host.rebuild()
            return
        state = status.folder_state or "starting"
        pct = int(round(status.completion or 0))
        if status.paused:
            self.ring.set_value(0, "paused")
            self.folder_state.setText("Paused")
            self.folder_detail.setText("Nothing syncs until you resume. What changes meanwhile carries over then, "
                                       "removed mods included.")
            self.progress.emit("paused", 0)
            self.host.syncStatus.emit(status)
            return
        self.ring.set_value(pct, state, busy=state in ("syncing", "scanning", "starting"))
        self.folder_state.setText({"idle": "Up to date", "syncing": "Syncing", "scanning": "Scanning files",
                                   "starting": "Starting Syncthing"}.get(state, state.capitalize()))
        connected = sum(1 for d in status.devices if d.connected)
        self.folder_detail.setText(f"Folder: {state}   ·   {pct}% in sync   ·   "
                                   f"{connected} of {len(status.devices)} device(s) online")
        self.progress.emit(state, pct)
        self.host.syncStatus.emit(status)
        complete = pct >= 100 and state == "idle"
        if complete and self._was_complete is False:
            self.synced.emit()
        self._was_complete = complete

    def _show_copy(self, status: SyncStatus) -> None:
        copy = status.copy
        source = copy.source
        if copy.phase == "done":
            kept = (f" {copy.set_aside} file{'' if copy.set_aside == 1 else 's'} from before "
                    f"{'is' if copy.set_aside == 1 else 'are'} kept in {COPY_ARCHIVE_DIR}." if copy.set_aside else "")
            self.host.notify(f"Copy finished: this machine has the same mods as {source}.{kept}", "ok")
            self.host.syncStatus.emit(status)
            self.synced.emit()
            self.host.rebuild()  # syncing both ways now: pairing and the rest come back
            return
        if copy.phase == "waiting":
            self.ring.set_value(0, "waiting", busy=True)
            self.folder_state.setText(f"Waiting for {source}")
            self.folder_detail.setText(f"Turn on {source} with ModSync open, or its background service on. "
                                       "The copy carries on by itself, even after ModSync restarts.")
        elif copy.phase == "receiving":
            pct = int(round(status.completion or 0))
            self.ring.set_value(pct, "copying", busy=True)
            self.folder_state.setText(f"Copying from {source}")
            left = f"{copy.need_items:,} item{'' if copy.need_items == 1 else 's'} to go" if copy.need_items else \
                "Checking that everything has arrived"
            errors = (f"   ·   {copy.errors} couldn't be copied yet; Open Syncthing UI shows why"
                      if copy.errors else "")
            self.folder_detail.setText(f"{left}{errors}. Nothing here is sent to {source} until the copy is done.")
        else:
            self.ring.set_value(100, "finishing", busy=True)
            self.folder_state.setText("Setting aside what's only here")
            self.folder_detail.setText(f"Files {source} doesn't have go to {COPY_ARCHIVE_DIR} in the instance.")
        self.progress.emit("copying", int(round(status.completion or 0)))
        self.host.syncStatus.emit(status)

    def _accept_pending(self) -> None:
        # Auto-accept a machine that joined with our code, so pairing needs only
        # one code, one way.
        worker.run_async(self.service.accept_pending, on_done=self._on_accepted, on_failed=lambda _: None)

    def _on_accepted(self, accepted: list) -> None:
        if accepted:
            n = len(accepted)
            self.host.notify(f"Paired with {n} new device{'' if n == 1 else 's'}.", "ok")
            self.refresh()

    # --- live: actions ---
    def show_code(self) -> None:
        if not self._code:
            self.host.notify("The pairing code isn't ready yet. Syncthing is still starting.", "warn")
            return
        CodeSheet(self.host, self._code).open()

    def add_device(self) -> None:
        def add(text: str) -> None:
            code = PairingCode.decode(text)
            worker.run_async(self.service.add_peer, code,
                             on_done=lambda _: (self.host.notify("Device added.", "ok"), self.refresh()),
                             on_failed=lambda m: self.host.notify(f"⚠ {m}"))

        KeyboardSheet(self.host, "Add a machine", "Paste the pairing code from the other machine.", add,
                      placeholder="MODSYNC1-…", validate=_validate_code, eyebrow="Sync").open()

    def pair_network(self) -> None:
        if self._pairing:
            self.cancel_pairing()
            return
        pin = pairing_lan.make_pin()
        self._pair_stop = threading.Event()
        self._pairing = True
        name = socket.gethostname() or "this machine"
        self.beacon = BeaconSheet(self.host, pin, name, self.cancel_pairing)
        self.beacon.open()
        self.host.notify(f"Waiting for another machine to enter PIN {pin[:3]} {pin[3:]}…")
        beacon = self.beacon
        stop = self._pair_stop

        def on_ready(ann: pairing_lan.Announcement) -> None:
            where = ann.host if ann.port == pairing_lan.PAIR_PORT else f"{ann.host}:{ann.port}"
            # The beacon may have been cancelled, or the page rebuilt, by now.
            self.host.call_soon(lambda: beacon.set_address(where) if shiboken6.isValid(beacon) else None)

        def paired(peer: object) -> None:
            if self._pair_stop is stop:
                self._on_paired(peer)

        def failed(message: str) -> None:
            if self._pair_stop is stop:
                self._on_pair_failed(message)

        worker.run_async(self.service.host_network_pairing, name, pin, on_ready=on_ready, stop=stop,
                         on_done=paired, on_failed=failed)

    def cancel_pairing(self) -> None:
        if self._pair_stop is not None:
            self._pair_stop.set()
        self._end_pairing("Network pairing cancelled.")

    def _on_paired(self, _peer: object) -> None:
        if not self._pairing:
            return
        self._end_pairing("Paired with a new machine over the network!", "ok")
        self.refresh()

    def _on_pair_failed(self, message: str) -> None:
        if not self._pairing:  # already cancelled
            return
        self._end_pairing(f"⚠ Pairing: {message}")

    def _end_pairing(self, message: str, tone: str | None = None) -> None:
        if not self._pairing:
            return
        self._pairing = False
        if self.beacon is not None:
            self.beacon.dismiss()
            self.beacon = None
        self.host.notify(message, tone)

    def rescan(self) -> None:
        worker.run_async(self.service.rescan, on_done=lambda _: self.host.notify("Rescan triggered."),
                         on_failed=lambda m: self.host.notify(f"⚠ {m}"))

    def open_ui(self) -> None:
        def url() -> str:
            self.service.ensure_running()
            return self.service.manager.base_url

        worker.run_async(url, on_done=lambda u: QDesktopServices.openUrl(QUrl(u)),
                         on_failed=lambda m: self.host.notify(f"⚠ {m}"))

    def stop_sync(self) -> None:
        def chosen(key: str | None) -> None:
            if key == "pause":
                self.set_paused(True)
            elif key == "stop":
                self.host.change_setup(self.service.stop_sync, message="Stopping sync…")

        paused = self.service.state.sync_paused
        choices = [] if paused else [
            ("pause", "Pause syncing", "Keep the vault and the paired devices. When you resume, what changed on "
             "each machine meanwhile carries over, removed mods included.", "primary", "pause")]
        choices += [
            ("stop", "Leave the vault", "Forget the vault and the paired devices. Joining again later compares "
             "the machines from scratch, so mods removed meanwhile come back.", "danger", "stop"),
            ("cancel", "Keep syncing" if not paused else "Stay paused", "", "normal", "close")]
        self.host.confirm(
            "Stop syncing",
            "Either way this machine keeps using the instance. Your mods, downloads and profiles are not "
            "deleted; every file stays on disk.",
            choices,
            chosen,
            default="cancel",
            eyebrow="Sync",
        )

    def set_paused(self, paused: bool) -> None:
        self.host.change_setup(self.service.pause_sync, paused,
                               message="Pausing sync…" if paused else "Resuming sync…")

    def stop_copy(self) -> None:
        def chosen(key: str | None) -> None:
            if key == "stop":
                self.host.change_setup(self.service.stop_sync, message="Stopping the copy…")

        self.host.confirm(
            "Stop copying",
            "This leaves the vault. What has arrived stays here, and whatever was already set aside stays in "
            f"{COPY_ARCHIVE_DIR}. To copy again later, join from the start.",
            [("stop", "Stop copying", "", "danger", "stop"), ("cancel", "Keep copying", "", "normal", "close")],
            chosen, default="cancel", eyebrow="Sync")

    # --- review: conflicts and set-aside files ---
    def review_conflicts(self) -> None:
        worker.run_async(self.service.conflicts, on_done=self._next_conflict,
                         on_failed=lambda m: self.host.notify(f"⚠ {m}"))

    def _next_conflict(self, found: list) -> None:
        if not shiboken6.isValid(self):
            return
        if not found:
            self.host.notify("No conflicts left.", "ok")
            self.refresh()
            return
        c = found[0]
        instance = self.service.state.instance_path or ""
        other = self._names.get(c.device, "another machine")
        rel = c.relative(instance)
        profile = c.original.parent.name
        title = {"modlist": f"Mod list of profile {profile}", "plugins": f"Plugins of profile {profile}",
                 "loadorder": f"Load order of profile {profile}"}.get(c.kind, rel)
        when = f", saved {c.when.day} {c.when:%b %H:%M}," if c.when else ""
        lines = "\n".join(f"•  {line}" for line in conflicts.differences(c, other))
        more = f"\n\n{len(found) - 1} more after this one." if len(found) > 1 else ""
        text = (f"{rel} changed on two machines before they synced. MO2 uses the version in use; the "
                f"version from {other}{when} is kept next to it.\n\n{lines}{more}")

        def chosen(key: str | None) -> None:
            if key not in ("current", "other"):
                self.refresh()
                return
            worker.run_async(self.service.resolve_conflict, c, key, on_done=lambda _: self.review_conflicts(),
                             on_failed=lambda m: self.host.notify(f"⚠ {m}"))

        self.host.confirm(
            title, text,
            [("current", "Keep the version in use", "The other goes to .modsync-conflicts in the instance.",
              "primary", "check"),
             ("other", f"Use the version from {other}", "The one in use goes to .modsync-conflicts in the "
              "instance. Close MO2 first.", "normal", "undo"),
             ("later", "Decide later", "", "normal", "close")],
            chosen, default="current", eyebrow="Conflict")

    def review_set_aside(self) -> None:
        path = self.service.state.set_aside

        def chosen(key: str | None) -> None:
            if key == "open":
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            elif key in ("delete", "forget"):
                self.host.change_setup(partial(self.service.dismiss_set_aside, delete=key == "delete"),
                                       message="Deleting the set-aside files…" if key == "delete" else
                                       "Done.")

        self.host.confirm(
            "Files set aside when joining",
            f"They were on this machine before the copy, but not on the other machine, or different there. "
            f"They are in {path}. Move back anything you want to keep, inside MO2's folders, and it syncs "
            "to every machine.",
            [("open", "Open the folder", "", "primary", "folder"),
             ("forget", "Stop showing this", "Keep the files but take this off the list.", "normal", "check"),
             ("delete", "Delete them", "The other machine's versions are the ones in use.", "danger", "trash"),
             ("keep", "Close", "", "normal", "close")],
            chosen, default="open", eyebrow="Sync")

    def shutdown(self) -> None:
        """Unblock a waiting network-pairing worker before teardown."""
        if self._pairing and self._pair_stop is not None:
            self._pair_stop.set()
        self._pairing = False
