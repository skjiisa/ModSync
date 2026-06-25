"""Integration tests that start a real Syncthing daemon.

Skipped unless MODSYNC_IT=1 so the normal suite stays fast and offline. Run with:

    MODSYNC_IT=1 .venv/bin/python -m unittest tests.test_sync_integration -v
"""

import os
import re
import socket
import subprocess
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from modsync.pairing_code import PairingCode
from modsync.service import ModSyncService
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

                    # .stignore written and parsed by Syncthing
                    self.assertTrue((instance / ".stignore").exists())
                    ignores: list[str] = []
                    for _ in range(20):
                        ignores = client.get_ignores("modsync-sse").get("ignore", [])
                        if "/ModOrganizer.ini" in ignores:
                            break
                        time.sleep(0.25)
                    self.assertIn("/ModOrganizer.ini", ignores)
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
            finally:
                if ca is not None:
                    ca.close()
                if cb is not None:
                    cb.close()
                a.stop()
                b.stop()

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


if __name__ == "__main__":
    unittest.main()
