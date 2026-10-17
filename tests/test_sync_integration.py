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

from modsync import gameversion
from modsync.games import SKYRIM_SE
from modsync.pairing_code import PairingCode
from modsync.service import ModSyncService
from modsync.state import State
from modsync.sync import conflicts, pairing, stignore
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

    def test_join_waits_offline_then_uses_the_source_version_when_it_syncs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source_instance, receiver_instance = tmp / "Source", tmp / "Receiver"
            source_instance.mkdir()
            receiver_instance.mkdir()
            game_dir = tmp / "game"
            game_dir.mkdir()
            (game_dir / "skse64_1_6_640.dll").touch()
            gameversion.VaultMeta(SKYRIM_SE.appid, "1.6.1170").save(source_instance)
            gameversion.VaultMeta(SKYRIM_SE.appid, "1.7.104").save(receiver_instance)
            source_port, receiver_port = _free_port(), _free_port()
            source_mgr = SyncthingManager(tmp / "source-home", binary=self.binary,
                                          gui_address=f"127.0.0.1:{_free_port()}", log_file=tmp / "source.log")
            receiver_mgr = SyncthingManager(tmp / "receiver-home", binary=self.binary,
                                            gui_address=f"127.0.0.1:{_free_port()}", log_file=tmp / "receiver.log")
            for manager, port in ((source_mgr, source_port), (receiver_mgr, receiver_port)):
                manager.ensure_config()
                _isolate_config(manager.home / "config.xml", port)
            try:
                with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp / "source-config")}):
                    source = ModSyncService(manager=source_mgr)
                    code = source.create_vault(source_instance, "Source")
                source_mgr.stop()  # no source record can arrive while joining
                with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp / "receiver-config")}), \
                        patch.object(ModSyncService, "_steam_app", return_value=(None, None, None)), \
                        patch.object(gameversion, "find_game_dir", return_value=game_dir), \
                        patch.object(gameversion, "installed_version", return_value=gameversion.GameVersion.parse("1.7.104")):
                    receiver = ModSyncService(manager=receiver_mgr)
                    receiver_code = receiver.join_vault(code, receiver_instance, peer_host="127.0.0.1")
                    self.assertIsNone(gameversion.VaultMeta.load(receiver_instance))
                    # Reopening during this offline period must retain the wait.
                    receiver = ModSyncService(manager=receiver_mgr)
                    waiting = receiver.game_status(refresh_index=False)
                    self.assertTrue(waiting.awaiting_vault_version)
                    self.assertIsNone(waiting.wanted)
                    self.assertEqual(waiting.skse_state, "")
                    with receiver_mgr.client() as client:
                        pairing.add_peer_device(client, code.device_id, "Source",
                                                addresses=[f"tcp://127.0.0.1:{source_port}"])
                    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp / "source-config")}):
                        source.ensure_running()
                        source.add_peer(receiver_code)
                        with source_mgr.client() as client:
                            pairing.add_peer_device(client, receiver_code.device_id, "Receiver",
                                                    addresses=[f"tcp://127.0.0.1:{receiver_port}"])
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline and gameversion.VaultMeta.load(receiver_instance) is None:
                        time.sleep(0.25)
                    received = gameversion.VaultMeta.load(receiver_instance)
                    self.assertIsNotNone(received, "source metadata did not sync")
                    self.assertEqual(received.runtime, "1.6.1170")
                    ready = receiver.game_status(refresh_index=False)
                    self.assertFalse(ready.awaiting_vault_version)
                    self.assertEqual(str(ready.wanted), "1.6.1170")
                    self.assertEqual(ready.suggested_target, "1.6.1170")
                    self.assertFalse(State.load().awaiting_vault_version)
                    self.assertEqual(gameversion.VaultMeta.load(source_instance).runtime, "1.6.1170")
            except Exception:
                for path in (tmp / "source.log", tmp / "receiver.log"):
                    if path.exists():
                        print(path.name, path.read_text()[-6000:])
                raise
            finally:
                source_mgr.stop()
                receiver_mgr.stop()

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
                with a.client() as ca:  # what A's pairing code says its list runs to
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline and ca.folder_status("modsync-copy")["state"] != "idle":
                        time.sleep(0.5)
                    sequence = ca.folder_status("modsync-copy")["sequence"]
                service = ModSyncService(manager=b)
                service.INDEX_QUIET_S = 3.0
                with patch("modsync.service.gameversion.find_game_dir", return_value=None):
                    service.choose_instance(inst_b)
                service.join_vault(PairingCode(id_a, "modsync-copy", "Desktop", sequence), inst_b,
                                   peer_host="127.0.0.1")
                self.assertTrue(service.launch_check().blocked)

                phases = []
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline and service.state.copy_phase:
                    progress = service.advance_copy()
                    if progress and (not phases or phases[-1] != progress.phase):
                        phases.append(progress.phase)
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
            (a, _port_a), (b, _port_b) = self._two_nodes(tmp)
            a.stop()  # the machine being copied is off
            try:
                id_a = _generate_device_id(self.binary, tmp / "offline")
                service = ModSyncService(manager=b)
                service.join_vault(PairingCode(id_a, "modsync-off", "Desktop"), inst_b)
                for _ in range(6):
                    progress = service.advance_copy()
                    time.sleep(1)
                self.assertEqual(progress.phase, "waiting")
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

    def test_a_file_written_again_during_the_copy_keeps_both_versions(self) -> None:
        """Trash-can versioning keeps one version per path. A file set aside by
        the first revert and written again before the copy finishes must not
        replace the version from before the join."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            os.environ["XDG_CONFIG_HOME"] = str(tmp / "cfg")
            inst_a, inst_b = tmp / "A" / "MO2", tmp / "B" / "MO2"
            for path, text in ((inst_a / "mods" / "OnlyA" / "x.esp", "only A"),
                               (inst_b / "mods" / "OnlyB" / "y.esp", "before the join")):
                path.parent.mkdir(parents=True)
                path.write_text(text)
            (a, port_a), (b, port_b) = self._two_nodes(tmp)
            try:
                with a.client() as ca, b.client() as cb:
                    id_a = ca.my_id()
                    pairing.add_peer_device(ca, cb.my_id(), "B", addresses=[f"tcp://127.0.0.1:{port_b}"])
                    pairing.share_instance_folder(ca, "modsync-again", inst_a, [cb.my_id()])
                    pairing.add_peer_device(cb, id_a, "A", addresses=[f"tcp://127.0.0.1:{port_a}"])
                with a.client() as ca:
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline and ca.folder_status("modsync-again")["state"] != "idle":
                        time.sleep(0.5)
                    sequence = ca.folder_status("modsync-again")["sequence"]
                service = ModSyncService(manager=b)
                service.INDEX_QUIET_S = 3.0
                service.join_vault(PairingCode(id_a, "modsync-again", "A", sequence), inst_b)
                archive = Path(service.state.copy_archive)
                rewritten = False
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline and service.state.copy_phase:
                    progress = service.advance_copy()
                    if progress and progress.phase == "setting-aside" and not rewritten:
                        self.assertEqual((archive / "mods" / "OnlyB" / "y.esp").read_text(), "before the join")
                        (inst_b / "mods" / "OnlyB").mkdir(exist_ok=True)
                        (inst_b / "mods" / "OnlyB" / "y.esp").write_text("written during the copy")
                        with b.client() as cb:
                            cb.rescan("modsync-again")
                        rewritten = True
                    time.sleep(1)
                self.assertTrue(rewritten)
                self.assertEqual(service.state.copy_phase, "", "copy did not finish (see B.log)")
                self.assertEqual((archive / "mods" / "OnlyB" / "y.esp").read_text(), "before the join")
                self.assertEqual((archive / "during-copy-1" / "mods" / "OnlyB" / "y.esp").read_text(),
                                 "written during the copy")
                self.assertFalse((inst_b / "mods" / "OnlyB" / "y.esp").exists())
                self.assertFalse((inst_a / "mods" / "OnlyB").exists())
            finally:
                a.stop()
                b.stop()
                os.environ.pop("XDG_CONFIG_HOME", None)

    def test_conflicts_are_found_and_settled_on_every_machine(self) -> None:
        """Both machines change a mod list and a mod's INI while B is paused.
        Syncthing keeps B's newer files and makes conflict copies of A's, on
        both machines. B finds them, keeps A's mod list (with the rest of its
        profile) and its own INI; the result reaches A, and the versions not
        kept stay in B's archive without syncing."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            os.environ["XDG_CONFIG_HOME"] = str(tmp / "cfg")
            inst_a, inst_b = tmp / "A" / "MO2", tmp / "B" / "MO2"
            files = {"profiles/Default/modlist.txt": "+Shared\n", "mods/Tweaks/tweaks.ini": "[General]\nfScale=1.0\n"}
            for inst in (inst_a, inst_b):
                for rel, text in files.items():
                    (inst / rel).parent.mkdir(parents=True, exist_ok=True)
                    (inst / rel).write_text(text)
            (a, port_a), (b, port_b) = self._two_nodes(tmp)

            def wait_for(check, what: str, timeout: float = 90) -> None:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if check():
                        return
                    time.sleep(0.5)
                self.fail(f"timed out waiting for {what} (see A.log, B.log)")

            def idle(mgr) -> bool:
                with mgr.client() as c:
                    s = c.folder_status("modsync-c")
                return s.get("state") == "idle" and bool(s.get("globalFiles")) and not s.get("needTotalItems")

            try:
                with a.client() as ca, b.client() as cb:
                    id_a, id_b = ca.my_id(), cb.my_id()
                    pairing.add_peer_device(ca, id_b, "A's peer", addresses=[f"tcp://127.0.0.1:{port_b}"])
                    pairing.add_peer_device(cb, id_a, "Desktop", addresses=[f"tcp://127.0.0.1:{port_a}"])
                    pairing.share_instance_folder(ca, "modsync-c", inst_a, [id_b])
                service = ModSyncService(manager=b)
                service.join_vault(PairingCode(id_a, "modsync-c", "A"), inst_b, merge=True)
                wait_for(lambda: idle(a) and idle(b), "the first sync")

                service.pause_sync()
                older, newer = time.time() - 120, time.time() - 30
                edits = {inst_a: ("+Shared\n+FromA\n", "[General]\nfScale=2.5\n", older),
                         inst_b: ("+Shared\n+FromB\n", "[General]\nfScale=1.0\nbNew=1\n", newer)}
                for inst, (modlist, ini, mtime) in edits.items():
                    for rel, text in zip(files, (modlist, ini)):
                        (inst / rel).write_text(text)
                        os.utime(inst / rel, (mtime, mtime))
                with a.client() as ca:
                    ca.rescan("modsync-c")
                wait_for(lambda: idle(a), "A to scan its changes")
                service.pause_sync(False)

                wait_for(lambda: len(service.conflicts()) == 2, "B's conflict copies")
                found = service.conflicts()
                self.assertEqual([c.relative(inst_b) for c in found],
                                 ["profiles/Default/modlist.txt", "mods/Tweaks/tweaks.ini"])
                # The copies hold A's changes, but the id in their names is B's,
                # the machine that found the conflict: it can't name the author.
                self.assertTrue(all(c.device == id_b[:7] for c in found))
                self.assertEqual(round(found[0].changed.timestamp()), round(older))
                self.assertEqual(service.status().conflicts, 2)
                wait_for(lambda: len(list(inst_a.rglob("*.sync-conflict-*"))) == 2, "A's conflict copies")

                modlist, ini = found
                self.assertIn("Only in the other version: FromA.", conflicts.differences(modlist))
                self.assertIn("Set differently: [General] fScale (1.0 in use, 2.5 in the other).",
                              conflicts.differences(ini))
                archive_1 = service.resolve_conflicts(conflicts.related(found, modlist), "other")
                archive_2 = service.resolve_conflicts([ini], "current")

                self.assertEqual(service.conflicts(), [])
                self.assertEqual((inst_b / "profiles/Default/modlist.txt").read_text(), "+Shared\n+FromA\n")
                self.assertEqual((archive_1 / "profiles/Default/modlist.txt").read_text(), "+Shared\n+FromB\n")
                self.assertEqual(len(list(archive_2.rglob("tweaks.sync-conflict-*.ini"))), 1)
                wait_for(lambda: not list(inst_a.rglob("*.sync-conflict-*")), "A to drop its conflict copies")
                wait_for(lambda: (inst_a / "profiles/Default/modlist.txt").read_text() == "+Shared\n+FromA\n",
                         "A to get the kept mod list")
                self.assertEqual((inst_a / "mods/Tweaks/tweaks.ini").read_text(), "[General]\nfScale=1.0\nbNew=1\n")
                self.assertFalse((inst_a / conflicts.ARCHIVE_DIR).exists())
            finally:
                a.stop()
                b.stop()
                os.environ.pop("XDG_CONFIG_HOME", None)


if __name__ == "__main__":
    unittest.main()
