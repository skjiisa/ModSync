"""Integration tests that start a real Syncthing daemon.

Skipped unless MODSYNC_IT=1 so the normal suite stays fast and offline. Run with:

    MODSYNC_IT=1 .venv/bin/python -m unittest tests.test_sync_integration -v
"""

import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from modsync.pairing_code import PairingCode
from modsync.service import ModSyncService
from modsync.state import State
from modsync.sync import pairing, stignore
from modsync.sync.binary import ensure_syncthing
from modsync.sync.manager import SyncthingManager

_DEVICE_ID_RE = re.compile(r"device=([A-Z2-7]{7}(?:-[A-Z2-7]{7}){7})")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _generate_device_id(binary: Path, home: Path) -> str:
    home.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [str(binary), "generate", "--home", str(home)],
        capture_output=True,
        text=True,
    )
    match = _DEVICE_ID_RE.search(result.stdout + result.stderr)
    assert match, f"no device id in generate output: {result.stdout}\n{result.stderr}"
    return match.group(1)


def _isolate_config(config_xml: Path, sync_port: int) -> None:
    """Pin a local sync listen port and disable discovery/relays/NAT so two
    daemons on one host connect only to each other, deterministically."""
    tree = ET.parse(config_xml)
    options = tree.getroot().find("options")
    assert options is not None
    for el in options.findall("listenAddress"):
        options.remove(el)
    ET.SubElement(options, "listenAddress").text = f"tcp://127.0.0.1:{sync_port}"
    for tag in (
        "globalAnnounceEnabled",
        "localAnnounceEnabled",
        "relaysEnabled",
        "natEnabled",
        "startBrowser",
    ):
        el = options.find(tag)
        if el is None:
            el = ET.SubElement(options, tag)
        el.text = "false"
    tree.write(config_xml)


@unittest.skipUnless(os.environ.get("MODSYNC_IT") == "1", "integration test; set MODSYNC_IT=1")
class SyncthingIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.binary = ensure_syncthing()

    def test_lifecycle_folder_and_ignores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            instance = tmp / "MO2_Instance"
            (instance / "mods" / "CoolMod").mkdir(parents=True)
            (instance / "ModOrganizer.ini").write_text("[General]\ngamePath=@ByteArray(Z:\\\\local)\n")

            mgr = SyncthingManager(
                tmp / "home", binary=self.binary, gui_address=f"127.0.0.1:{_free_port()}"
            )
            mgr.ensure_config()
            _isolate_config(mgr.home / "config.xml", _free_port())
            mgr.start(timeout=40)
            try:
                self.assertTrue(mgr.running)
                with mgr.client() as client:
                    my_id = client.my_id()
                    self.assertRegex(my_id, r"^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$")

                    # register a (real, valid-checksum) peer device id
                    peer_id = _generate_device_id(self.binary, tmp / "peerhome")
                    pairing.add_peer_device(client, peer_id, "Steam Deck")
                    self.assertIn(peer_id, [d["deviceID"] for d in client.devices()])

                    # share the instance folder with self + peer
                    folder = pairing.share_instance_folder(
                        client, "modsync-sse", instance, [peer_id]
                    )
                    self.assertEqual(Path(folder["path"]), instance)
                    shared = {d["deviceID"] for d in folder["devices"]}
                    self.assertIn(my_id, shared)
                    self.assertIn(peer_id, shared)

                    # .stignore written and parsed by Syncthing (content-only policy)
                    self.assertTrue((instance / ".stignore").exists())
                    ignores: list[str] = []
                    for _ in range(20):
                        ignores = client.get_ignores("modsync-sse").get("ignore", [])
                        if "/*" in ignores:
                            break
                        time.sleep(0.25)
                    self.assertIn("/*", ignores)
                    self.assertIn("!/mods", ignores)
            finally:
                mgr.stop()
            self.assertFalse(mgr.running)

    def test_two_node_sync_excludes_ini(self) -> None:
        """The core promise: a mod propagates A->B, but each machine keeps its
        own ModOrganizer.ini (it is never synced)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            inst_a = tmp / "A" / "MO2"
            inst_b = tmp / "B" / "MO2"
            (inst_a / "mods" / "CoolMod").mkdir(parents=True)
            (inst_b / "mods").mkdir(parents=True)
            (inst_a / "mods" / "CoolMod" / "plugin.esp").write_text("esp-A")
            ini_a = "[General]\ngamePath=@ByteArray(Z:\\\\home\\\\A)\n"
            ini_b = "[General]\ngamePath=@ByteArray(Z:\\\\home\\\\B)\n"
            (inst_a / "ModOrganizer.ini").write_text(ini_a)
            (inst_b / "ModOrganizer.ini").write_text(ini_b)

            sync_a, sync_b = _free_port(), _free_port()
            a = SyncthingManager(
                tmp / "homeA",
                binary=self.binary,
                gui_address=f"127.0.0.1:{_free_port()}",
                log_file=tmp / "a.log",
            )
            b = SyncthingManager(
                tmp / "homeB",
                binary=self.binary,
                gui_address=f"127.0.0.1:{_free_port()}",
                log_file=tmp / "b.log",
            )
            a.ensure_config()
            _isolate_config(a.home / "config.xml", sync_a)
            b.ensure_config()
            _isolate_config(b.home / "config.xml", sync_b)

            a.start(40)
            b.start(40)
            ca = cb = None
            background = None
            try:
                ca, cb = a.client(), b.client()
                id_a, id_b = ca.my_id(), cb.my_id()
                pairing.add_peer_device(ca, id_b, "B", addresses=[f"tcp://127.0.0.1:{sync_b}"])
                pairing.add_peer_device(cb, id_a, "A", addresses=[f"tcp://127.0.0.1:{sync_a}"])
                pairing.share_instance_folder(ca, "modsync-sse", inst_a, [id_b])
                pairing.share_instance_folder(cb, "modsync-sse", inst_b, [id_a])

                target = inst_b / "mods" / "CoolMod" / "plugin.esp"
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline and not target.exists():
                    time.sleep(0.5)

                self.assertTrue(target.exists(), "mod did not propagate A->B (see a.log/b.log)")
                self.assertEqual(target.read_text(), "esp-A")
                # the crucial guarantee — neither machine's ModOrganizer.ini changed:
                self.assertEqual((inst_b / "ModOrganizer.ini").read_text(), ini_b)
                self.assertEqual((inst_a / "ModOrganizer.ini").read_text(), ini_a)

                # Enable background sync while the app owns the daemon, then
                # close the app. The next service poll must recover and keep
                # serving the same vault and identity.
                background = SyncthingManager(
                    a.home, binary=self.binary, gui_address=a.address,
                    log_file=tmp / "background.log",
                )
                background.start(40)
                self.assertTrue(background.running)
                a.stop()
                self.assertFalse(background.running)
                with patch("modsync.service.State.load", return_value=State(
                    instance_path=str(inst_a), folder_id="modsync-sse"
                )):
                    service = ModSyncService(manager=background)
                service.ensure_running(timeout=40)
                with background.client() as client:
                    self.assertEqual(client.my_id(), id_a)
                    client.rescan("modsync-sse")

                (inst_b / "mods" / "from-deck.txt").write_text("new mod")
                cb.rescan("modsync-sse")
                received = inst_a / "mods" / "from-deck.txt"
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline and not received.exists():
                    time.sleep(0.5)
                self.assertTrue(received.exists(), "background did not resume syncing B->A")
                self.assertEqual(received.read_text(), "new mod")

                # Reopening and closing the app must leave the service's
                # replacement daemon alive.
                a.start(40)
                a.stop()
                self.assertTrue(background.running)
            finally:
                if ca is not None:
                    ca.close()
                if cb is not None:
                    cb.close()
                a.stop()
                b.stop()
                if background is not None:
                    background.stop()

    def test_service_create_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            os.environ["XDG_CONFIG_HOME"] = str(tmp / "cfg")
            os.environ["XDG_DATA_HOME"] = str(tmp / "data")
            try:
                instance = tmp / "MO2"
                (instance / "mods").mkdir(parents=True)
                (instance / "ModOrganizer.ini").write_text("[General]\n")

                mgr = SyncthingManager(
                    tmp / "home",
                    binary=self.binary,
                    gui_address=f"127.0.0.1:{_free_port()}",
                )
                mgr.ensure_config()
                _isolate_config(mgr.home / "config.xml", _free_port())
                svc = ModSyncService(manager=mgr)
                try:
                    code = svc.create_vault(instance, "Skyrim SE")
                    self.assertTrue(svc.state.configured)
                    self.assertEqual(Path(svc.state.instance_path), instance)
                    self.assertEqual(PairingCode.decode(code.encode()), code)

                    with mgr.client() as client:
                        self.assertEqual(
                            Path(client.get_folder(code.folder_id)["path"]), instance
                        )

                    status = svc.status()
                    self.assertEqual(status.folder_id, code.folder_id)
                    self.assertTrue(status.configured)
                    self.assertTrue((instance / ".stignore").exists())
                finally:
                    svc.shutdown()
            finally:
                os.environ.pop("XDG_CONFIG_HOME", None)
                os.environ.pop("XDG_DATA_HOME", None)


    def _two_nodes(self, tmp: Path):
        """Two isolated daemons that can only reach each other. Returns the
        managers and their sync ports."""
        nodes = []
        for name in ("A", "B"):
            port = _free_port()
            mgr = SyncthingManager(tmp / f"home{name}", binary=self.binary,
                                   gui_address=f"127.0.0.1:{_free_port()}", log_file=tmp / f"{name}.log")
            mgr.ensure_config()
            _isolate_config(mgr.home / "config.xml", port)
            mgr.start(40)
            nodes.append((mgr, port))
        return nodes

    def test_copy_from_another_machine(self) -> None:
        """Joining copies the vault exactly without touching the source: a file
        that matches stays put, one that differs is replaced (even though the
        joiner's is newer), and one only the joiner has is set aside, never sent.
        Afterwards the folder syncs both ways."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            os.environ["XDG_CONFIG_HOME"] = str(tmp / "cfg")
            os.environ["XDG_DATA_HOME"] = str(tmp / "data")
            inst_a, inst_b = tmp / "A" / "MO2", tmp / "B" / "MO2"
            old, new = time.time() - 86400, time.time() - 60

            def write(path: Path, text: str, mtime: float | None = None) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                if mtime:
                    os.utime(path, (mtime, mtime))

            write(inst_a / "mods" / "Same" / "a.esp", "same", old)
            write(inst_b / "mods" / "Same" / "a.esp", "same", old)
            write(inst_a / "mods" / "Diff" / "d.esp", "from A", old)
            write(inst_b / "mods" / "Diff" / "d.esp", "from B", new)
            write(inst_a / "mods" / "OnlyA" / "x.esp", "only A")
            write(inst_b / "mods" / "OnlyB" / "y.esp", "only B")
            write(inst_a / "profiles" / "Default" / "modlist.txt", "+OnlyA\n+Diff\n+Same\n", old)
            write(inst_b / "profiles" / "Default" / "modlist.txt", "+OnlyB\n+Diff\n+Same\n", new)
            same_inode = (inst_b / "mods" / "Same" / "a.esp").stat().st_ino
            (a, port_a), (b, port_b) = self._two_nodes(tmp)
            service = None
            try:
                with a.client() as ca, b.client() as cb:
                    id_a, id_b = ca.my_id(), cb.my_id()
                    pairing.add_peer_device(ca, id_b, "B", addresses=[f"tcp://127.0.0.1:{port_b}"])
                    pairing.share_instance_folder(ca, "modsync-copy", inst_a, [id_b])
                    pairing.add_peer_device(cb, id_a, "A", addresses=[f"tcp://127.0.0.1:{port_a}"])
                service = ModSyncService(manager=b)
                with patch("modsync.service.gameversion.find_game_dir", return_value=None):
                    service.choose_instance(inst_b)
                service.join_vault(PairingCode(id_a, "modsync-copy", "Desktop"), inst_b,
                                   peer_host="127.0.0.1")
                self.assertTrue(service.launch_check().blocked)

                phases = []
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline and service.state.copy_phase:
                    status = service.status()
                    if status.copy and (not phases or phases[-1] != status.copy.phase):
                        phases.append(status.copy.phase)
                    time.sleep(1)
                self.assertEqual(service.state.copy_phase, "", f"copy did not finish: {phases} (see B.log)")
                self.assertEqual(phases[-1], "done")

                content = lambda root: sorted(  # noqa: E731
                    str(p.relative_to(root)) for p in root.rglob("*")
                    if p.is_file() and p.parts[len(root.parts)] in ("mods", "profiles"))
                self.assertEqual(content(inst_b), content(inst_a))
                self.assertEqual((inst_b / "mods" / "Diff" / "d.esp").read_text(), "from A")
                self.assertEqual((inst_b / "profiles" / "Default" / "modlist.txt").read_text(),
                                 "+OnlyA\n+Diff\n+Same\n")
                self.assertEqual((inst_b / "mods" / "Same" / "a.esp").stat().st_ino, same_inode,
                                 "a matching file was downloaded again")
                # The source never saw the joiner's files.
                self.assertFalse((inst_a / "mods" / "OnlyB").exists())
                self.assertEqual((inst_a / "mods" / "Diff" / "d.esp").read_text(), "from A")
                self.assertEqual(list(inst_a.rglob("*.sync-conflict-*")), [])
                # ...which are all kept in the archive.
                archive = Path(service.state.set_aside)
                self.assertTrue(archive.is_relative_to(inst_b / ".modsync-before-join"))
                kept = {p.name: p.read_text() for p in archive.rglob("*") if p.is_file()}
                self.assertEqual(kept["y.esp"], "only B")
                self.assertIn("from B", kept.values())
                self.assertIn("+OnlyB\n+Diff\n+Same\n", kept.values())

                with b.client() as cb:
                    folder = cb.get_folder("modsync-copy")
                    self.assertEqual(folder["type"], "sendreceive")
                    self.assertEqual(folder["versioning"]["type"], "")
                    write(inst_b / "mods" / "NewOnB" / "n.esp", "new")
                    cb.rescan("modsync-copy")
                received = inst_a / "mods" / "NewOnB" / "n.esp"
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline and not received.exists():
                    time.sleep(0.5)
                self.assertTrue(received.exists(), "the folder doesn't sync B->A after the copy")
                self.assertFalse(service.launch_check().blocked)
            finally:
                a.stop()
                b.stop()
                os.environ.pop("XDG_CONFIG_HOME", None)
                os.environ.pop("XDG_DATA_HOME", None)

    def test_copy_waits_for_the_source(self) -> None:
        """Before the source has sent its index, the empty vault reads as 100%
        complete. That must not be taken as done: nothing here may be set aside."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            os.environ["XDG_CONFIG_HOME"] = str(tmp / "cfg")
            inst_b = tmp / "B" / "MO2"
            (inst_b / "mods" / "Mine").mkdir(parents=True)
            (inst_b / "mods" / "Mine" / "m.esp").write_text("mine")
            (a, _port_a), (b, port_b) = self._two_nodes(tmp)
            a.stop()  # the machine being copied is off
            try:
                with b.client() as cb:
                    id_a = _generate_device_id(self.binary, tmp / "offline")
                service = ModSyncService(manager=b)
                service.join_vault(PairingCode(id_a, "modsync-off", "Desktop"), inst_b)
                for _ in range(6):
                    status = service.status()
                    time.sleep(1)
                self.assertEqual(status.copy.phase, "waiting")
                self.assertEqual(service.state.copy_phase, "receiving")
                self.assertEqual((inst_b / "mods" / "Mine" / "m.esp").read_text(), "mine")
            finally:
                b.stop()
                os.environ.pop("XDG_CONFIG_HOME", None)

    def test_pause_keeps_history(self) -> None:
        """Pausing and resuming carries a deletion made in between, where
        leaving and joining again would bring the deleted mod back."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            os.environ["XDG_CONFIG_HOME"] = str(tmp / "cfg")
            inst_a, inst_b = tmp / "A" / "MO2", tmp / "B" / "MO2"
            for inst in (inst_a, inst_b):
                (inst / "mods" / "Gone").mkdir(parents=True)
                (inst / "mods" / "Gone" / "g.esp").write_text("g")
            (a, port_a), (b, port_b) = self._two_nodes(tmp)
            try:
                with a.client() as ca, b.client() as cb:
                    id_a, id_b = ca.my_id(), cb.my_id()
                    pairing.add_peer_device(ca, id_b, "B", addresses=[f"tcp://127.0.0.1:{port_b}"])
                    pairing.add_peer_device(cb, id_a, "A", addresses=[f"tcp://127.0.0.1:{port_a}"])
                    pairing.share_instance_folder(ca, "modsync-p", inst_a, [id_b])
                service = ModSyncService(manager=b)
                service.join_vault(PairingCode(id_a, "modsync-p", "A"), inst_b, merge=True)
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    with b.client() as cb:
                        s = cb.folder_status("modsync-p")
                    if s.get("state") == "idle" and s.get("globalFiles") and not s.get("needTotalItems"):
                        break
                    time.sleep(0.5)
                service.pause_sync()
                self.assertEqual(service.status().folder_state, "paused")
                shutil.rmtree(inst_b / "mods" / "Gone")
                service.pause_sync(False)
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline and (inst_a / "mods" / "Gone" / "g.esp").exists():
                    time.sleep(0.5)
                self.assertFalse((inst_a / "mods" / "Gone" / "g.esp").exists())
                self.assertFalse((inst_b / "mods" / "Gone").exists())
            finally:
                a.stop()
                b.stop()
                os.environ.pop("XDG_CONFIG_HOME", None)


if __name__ == "__main__":
    unittest.main()
