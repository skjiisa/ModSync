"""The long-running loop behind ``modsync serve`` and the background service.

It does whatever this machine's setup calls for, and nothing more:

* always: apply a queued Steam pin or launch-hook switch the moment Steam exits,
  and notice when Steam has updated the game under a setup built for another version;
* only when a vault exists: keep Syncthing running, auto-accept machines that
  paired with our code, keep the games' folders tidy, and see a copy through.

It is meant to cost next to nothing on a Steam Deck: a pass every ``IDLE_S``
seconds, or every ``BUSY_S`` while something waits on Steam or a copy. In
between, a thread keeps a request open that Syncthing answers the moment a
machine asks to pair or offers a folder. It never reads the sync
status nobody is looking at.

So someone who never syncs still gets the deferred pin applied — which is the
whole point of queueing it on the Deck, where Steam is always running.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from typing import Callable

from modsync import gameversion, launchhook
from modsync.games import SKYRIM_SE
from modsync.state import NewerStateError

Log = Callable[[str], None]

# Something waits on Steam exiting, or a copy is under way: Steam restarts within
# seconds on a switch between Gaming Mode and the desktop.
BUSY_S = 5.0
IDLE_S = 30.0
_VERSION_CHECK_S = 60.0  # the PE header + SKSE scan are cheap but not free
_UPKEEP_S = 60.0  # the games' folders: walks and, for DFU, a pass over the saves
_MIN_WAKE_S = 1.0  # however many events arrive


def notify(title: str, body: str) -> None:
    """Best-effort desktop notification; silently does nothing without notify-send."""
    exe = shutil.which("notify-send")
    if not exe:
        return
    try:
        subprocess.run([exe, "--app-name=ModSync", title, body], check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


class Server:
    def __init__(self, service, *, log: Log = print, notify: Callable[[str, str], None] = notify) -> None:
        self.service = service
        self.log = log
        self.notify = notify
        self._last_installed: gameversion.GameVersion | None = None
        self._clock = time.monotonic
        self._watcher: threading.Thread | None = None
        self._watch_on = threading.Event()  # syncing: the watch runs
        self._woken = threading.Event()  # the watch saw something for the next pass
        self._version_checked_at: float | None = None
        self._upkept_at: float | None = None

    @property
    def syncing(self) -> bool:
        """Syncthing has work: the vault, a game's folder, or a paired machine's offers."""
        return bool(self.service.state.active)

    def start(self) -> None:
        state = self.service.state
        if state.syncing:
            self.log(f"Serving vault {state.folder_id} for {state.instance_path}")
        elif state.has_instance:
            self.log(f"Watching {state.instance_path} (MO2 sync is off)")
        else:
            self.log("No instance chosen yet; watching Steam only")
        for key, path in state.game_folders.items():
            self.log(f"Serving {key} for {path}")
        for key, path in state.held_folders.items():
            self.log(f"Keeping a copy of {key} for other machines in {path}")
        if state.hold_all:
            self.log("Keeping a copy of every game's folder another machine offers, for games not set up here")
        if state.paired and not state.syncs_anything:
            self.log("Paired; waiting for another machine to offer a game's folder")
        vc = self.service.game_version_check()
        self._last_installed = vc.installed
        if vc.mismatch or vc.skse_suggests:
            self.log(f"\n  ! {vc.summary()}")
            if vc.skse_note():
                self.log(f"    {vc.skse_note()}")
            self.log("")
        for appid in launchhook.hooked_appids():
            try:
                hook = launchhook.upgrade(appid)
                if hook is not None:
                    self.log(f"  {hook}")
            except Exception as exc:
                self.log(f"  (launch hook update failed: {exc})")
        from modsync import mo2games, nxm

        for note in [*mo2games.refresh_hooks(), *nxm.refresh()]:
            self.log(f"  {note}")
        if self.syncing:
            self.service.ensure_running()
            self.log("Syncing — press Ctrl-C to stop.")
        else:
            self.log("Watching for Steam updates — press Ctrl-C to stop.")

    def tick(self) -> None:
        """One pass. Errors are logged, never raised, so the loop keeps going."""
        try:
            # The app may have joined, left or paused a vault since the last tick.
            self.service.reload_state()
        except NewerStateError:
            raise  # a newer ModSync took over the setup: stop (and stop syncing) rather than act on a stale one
        except Exception as exc:
            self.log(f"  (state unreadable: {exc})")
        try:
            pin = self.service.apply_pending_pin()
            if pin is not None:
                self.log(f"  {'✓' if pin.applied else '!'} {pin.message}")
                if pin.applied:
                    self.notify("Steam pin applied", pin.message)
        except Exception as exc:
            self.log(f"  (pin check failed: {exc})")
        try:
            hook = launchhook.apply_pending()
            if hook is not None:
                self.log(f"  {hook}")
                self.notify("ModSync launch hook", hook)
        except Exception as exc:
            self.log(f"  (launch hook check failed: {exc})")
        # Mid-copy the vault's game-version record may not have arrived yet.
        if self._due("_version_checked_at", _VERSION_CHECK_S) and not self.service.state.copy_phase:
            self._check_game_version()
        if not self.syncing:
            return
        try:
            for device_id in self.service.accept_pending():
                self.log(f"  ✓ accepted new device {device_id[:13]}…")
        except Exception as exc:
            self.log(f"  (pairing check failed: {exc})")
        if self._due("_upkept_at", _UPKEEP_S):
            try:
                self.service.upkeep()
            except Exception as exc:
                self.log(f"  (folder upkeep failed: {exc})")
        if self.service.state.copy_phase:
            self._advance_copy()

    def _due(self, attr: str, every: float) -> bool:
        """Whether what ``attr`` times last ran ``every`` seconds ago or more
        (or never), noting that it runs now."""
        now = self._clock()
        last = getattr(self, attr)
        if last is not None and now - last < every:
            return False
        setattr(self, attr, now)
        return True

    def _advance_copy(self) -> None:
        try:
            progress = self.service.advance_copy()
            status = self.service.status()
            connected = sum(1 for d in status.devices if d.connected)
            line = f"  devices {connected}/{len(status.devices)} connected"
            if status.folder_state is not None:
                line += f" · folder {status.folder_state} · {int(status.completion or 0)}% in sync"
            for fs in status.game_folders:
                line += f" · {fs.spec.key}{' (kept)' if fs.held else ''} {fs.state or '?'} · {int(fs.completion or 0)}%"
            copy = progress if progress is not None and progress.phase == "done" else status.copy
            if copy is not None and copy.phase != "done":
                line += f" · copying from {copy.source}: {copy.phase}"
            self.log(line)
            if copy is not None and copy.phase == "done":
                body = f"This machine now has the same mods as {copy.source}."
                if copy.set_aside:
                    body += (f" {copy.set_aside} file(s) that were only here are kept in "
                             f"{self.service.state.set_aside}.")
                self.log(f"  ✓ {body}")
                self.notify("Copy finished", body)
        except Exception as exc:
            self.log(f"  (status unavailable: {exc})")

    def _check_game_version(self) -> None:
        try:
            vc = self.service.game_version_check()
        except Exception as exc:
            self.log(f"  (version check failed: {exc})")
            return
        if vc.installed is None or vc.installed == self._last_installed:
            self._last_installed = vc.installed
            return
        was = self._last_installed
        self._last_installed = vc.installed
        if was is None:
            return
        wanted = vc.expected or vc.skse.runtime
        if wanted is not None and wanted != vc.installed:
            body = (
                f"{SKYRIM_SE.name} changed from {was} to {vc.installed}, but this setup was "
                f"built for {wanted}. Open ModSync to downgrade or pin the version."
            )
        else:
            body = f"{SKYRIM_SE.name} changed from {was} to {vc.installed}."
        self.log(f"  ! {body}")
        self.notify(f"{SKYRIM_SE.name} was updated", body)

    def interval(self) -> float:
        busy = self.service.state.copy_phase or self._steam_change_queued()
        return BUSY_S if busy else IDLE_S

    def _steam_change_queued(self) -> bool:
        try:
            return self.service.steam_change_queued()
        except Exception:
            return True

    def wait(self, seconds: float) -> None:
        """Sleep ``seconds``, or less: when Syncthing reports something the
        next pass answers (see ``_watch``), or when the app queues a Steam
        change meanwhile, which must be applied in the few seconds Steam is
        gone on a switch between Gaming Mode and the desktop. That check is
        a few file lookups every ``BUSY_S``, nothing more."""
        if seconds <= 0:
            return
        started = self._clock()
        watching = self.syncing
        if watching:
            self._start_watch()
            self._watch_on.set()
        else:
            self._watch_on.clear()
        try:
            while (left := seconds - (self._clock() - started)) > 0:
                if watching and self._woken.wait(min(BUSY_S, left)):
                    self._woken.clear()
                    # A burst of events still wakes it at most once a second.
                    time.sleep(max(0.0, _MIN_WAKE_S - (self._clock() - started)))
                    return
                if not watching:
                    time.sleep(min(BUSY_S, left))
                if self._steam_change_queued() and seconds > BUSY_S:
                    return
        finally:
            if not watching:
                self._woken.clear()

    def _start_watch(self) -> None:
        if self._watcher is None:
            self._watcher = threading.Thread(target=self._watch, name="modsync-sync-events", daemon=True)
            self._watcher.start()

    def _watch(self) -> None:
        """In its own thread, while syncing: one request after another held
        open by Syncthing, so a pairing request or a folder offer is answered
        at once, without the loop waking to ask."""
        while True:
            self._watch_on.wait()
            woke = self.service.watch_sync_events(IDLE_S)
            if woke:
                self._woken.set()
            elif woke is None:
                time.sleep(BUSY_S)  # Syncthing isn't up (yet): the next pass starts it

    def run(self, interval: float | None = None) -> int:
        """Loop until interrupted; ``interval`` fixes the wait between passes."""
        self.start()
        try:
            while True:
                self.tick()
                self.wait(self.interval() if interval is None else interval)
        except KeyboardInterrupt:
            self.log("\nStopping …")
        finally:
            self.service.shutdown()
        return 0
