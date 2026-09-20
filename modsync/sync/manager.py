"""Manage a dedicated Syncthing process for ModSync.

We run Syncthing with its own ``--home`` (separate from any user-installed
Syncthing), generate the config/identity on first use, read the API key + GUI
address from ``config.xml``, start ``syncthing serve``, and wait for the REST API
to come up.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from modsync.sync.api import SyncthingClient
from modsync.sync.binary import ensure_syncthing

log = logging.getLogger(__name__)


class SyncthingError(RuntimeError):
    pass


class SyncthingManager:
    def __init__(
        self,
        home: Path | str,
        binary: Path | str | None = None,
        gui_address: str | None = None,
        log_file: Path | str | None = None,
    ) -> None:
        self.home = Path(home)
        self.binary = Path(binary) if binary else ensure_syncthing()
        self._gui_override = gui_address
        self._log_file = Path(log_file) if log_file else None
        self._log_handle = None
        self.api_key: str | None = None
        self.address: str | None = None
        self._proc: subprocess.Popen | None = None
        self._attached = False  # true when using a Syncthing we didn't start
        self._lifecycle_lock = threading.RLock()

    # --- configuration / identity ---
    def ensure_config(self) -> None:
        config_xml = self.home / "config.xml"
        if not config_xml.exists():
            self.home.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [str(self.binary), "generate", "--home", str(self.home)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise SyncthingError(
                    f"`syncthing generate` failed: {result.stderr or result.stdout}"
                )
        self.api_key, address = self._read_gui(config_xml)
        self.address = self._gui_override or address

    @staticmethod
    def _read_gui(config_xml: Path) -> tuple[str, str]:
        gui = ET.parse(config_xml).getroot().find("gui")
        if gui is None:
            raise SyncthingError("no <gui> section in config.xml")
        return (gui.findtext("apikey") or "", gui.findtext("address") or "127.0.0.1:8384")

    # --- lifecycle ---
    def start(self, timeout: float = 30.0) -> None:
        # Dashboard jobs can notice the same dead daemon concurrently.
        with self._lifecycle_lock:
            if self.running:
                return
            self._start(timeout)

    def _start(self, timeout: float) -> None:
        self._attached = False
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        self.ensure_config()
        # If a Syncthing is already serving this home — our own background
        # service, or another ModSync window — attach to it instead of spawning
        # a second process that would fail to bind the same ports.
        try:
            with self.client() as client:
                if client.ping():
                    self._attached = True
                    log.info("attached to the Syncthing already serving %s at %s", self.home, self.address)
                    return
        except Exception:
            pass
        args = [
            str(self.binary),
            "serve",
            "--home",
            str(self.home),
            "--no-browser",
            "--no-restart",
        ]
        if self._gui_override:
            args += ["--gui-address", self._gui_override]
        if self._log_file:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = self._log_file.open("w")
            out = self._log_handle
        else:
            out = subprocess.DEVNULL
        log.info("starting %s serve --home %s (gui %s, log %s)", self.binary, self.home, self.address, self._log_file)
        self._proc = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT)
        try:
            self._wait_ready(timeout)
        except SyncthingError as exc:
            log.error("Syncthing did not come up: %s", exc)
            raise

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self.client() as client:
            while time.monotonic() < deadline:
                if self._proc is not None and self._proc.poll() is not None:
                    raise SyncthingError(
                        f"syncthing exited early (code {self._proc.returncode})"
                    )
                if client.ping():
                    return
                time.sleep(0.2)
        raise SyncthingError("timed out waiting for the Syncthing REST API")

    def stop(self, timeout: float = 10.0) -> None:
        with self._lifecycle_lock:
            self._stop(timeout)

    def _stop(self, timeout: float) -> None:
        if self._attached:
            # We attached to a Syncthing we didn't start; leave it running.
            self._attached = False
            return
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout)
        self._proc = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    @property
    def running(self) -> bool:
        if self._attached:
            # The app or service that started this daemon may have exited.
            # Let ensure_running() start a replacement on its next poll.
            with self.client() as client:
                return client.ping()
        return self._proc is not None and self._proc.poll() is None

    @property
    def base_url(self) -> str:
        if not self.address:
            raise SyncthingError("manager not configured; call ensure_config()/start()")
        return self.address if self.address.startswith("http") else f"http://{self.address}"

    def client(self) -> SyncthingClient:
        if not self.api_key or not self.address:
            raise SyncthingError("manager not configured; call ensure_config()/start()")
        return SyncthingClient(self.base_url, self.api_key)

    def device_id(self) -> str:
        with self.client() as client:
            return client.my_id()

    def __enter__(self) -> "SyncthingManager":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
