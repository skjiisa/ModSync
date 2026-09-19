"""The long-running loop behind ``modsync serve`` and the background service.

It does whatever this machine's setup calls for, and nothing more:

* always: apply a queued Steam pin or launch-hook switch the moment Steam exits,
  and notice when Steam has updated the game under a setup built for another version;
* only when a vault exists: keep Syncthing running, auto-accept machines that
  paired with our code, and report sync progress.

So someone who never syncs still gets the deferred pin applied — which is the
whole point of queueing it on the Deck, where Steam is always running.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Callable

from modsync import gameversion, launchhook
from modsync.games import SKYRIM_SE

Log = Callable[[str], None]

_VERSION_CHECK_EVERY = 12  # ticks; the PE header + SKSE scan are cheap but not free


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
        self._ticks = 0
        self._last_installed: gameversion.GameVersion | None = None

    @property
    def syncing(self) -> bool:
        return bool(self.service.state.syncing)

    def start(self) -> None:
        state = self.service.state
        if self.syncing:
            self.log(f"Serving vault {state.folder_id} for {state.instance_path}")
        elif state.has_instance:
            self.log(f"Watching {state.instance_path} (sync is off)")
        else:
            self.log("No instance chosen yet; watching Steam only")
        vc = self.service.game_version_check()
        self._last_installed = vc.installed
        if vc.mismatch or vc.skse_suggests:
            self.log(f"\n  ! {vc.summary()}")
            if vc.skse_note():
                self.log(f"    {vc.skse_note()}")
            self.log("")
        if self.syncing:
            self.service.ensure_running()
            self.log("Syncing — press Ctrl-C to stop.")
        else:
            self.log("Watching for Steam updates — press Ctrl-C to stop.")

    def tick(self) -> None:
        """One pass. Errors are logged, never raised, so the loop keeps going."""
        self._ticks += 1
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
        if self._ticks % _VERSION_CHECK_EVERY == 1:
            self._check_game_version()
        if not self.syncing:
            return
        try:
            for device_id in self.service.accept_pending():
                self.log(f"  ✓ accepted new device {device_id[:13]}…")
            status = self.service.status()
            connected = sum(1 for d in status.devices if d.connected)
            line = f"  devices {connected}/{len(status.devices)} connected"
            if status.folder_state is not None:
                line += f" · folder {status.folder_state} · {int(status.completion or 0)}% in sync"
            self.log(line)
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

    def run(self, interval: float = 5.0) -> int:
        self.start()
        try:
            while True:
                self.tick()
                time.sleep(interval)
        except KeyboardInterrupt:
            self.log("\nStopping …")
        finally:
            self.service.shutdown()
        return 0
