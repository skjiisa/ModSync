"""Failure paths: the CLI and the service layers against machines that are
missing Steam, have the game on a removable library, are mid-update, hold a
half-broken MO2 instance, share a port with something that is not Syncthing,
or try to pair with a peer that never answers.

Everything runs offline against a throwaway HOME: no real Steam, Syncthing or
network is touched.
"""

from __future__ import annotations

import base64
import http.server
import io
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from modsync import gameversion, pairing_lan
from modsync.__main__ import main
from modsync.downgrade import recipe
from modsync.gameversion import GameVersion
from modsync.pairing_lan import Announcement, PairError, PairPayload
from modsync.service import ModSyncService
from modsync.state import State
from modsync.steam import shortcuts, vdf
from modsync.sync.manager import SyncthingError, SyncthingManager
from tests import fakesteam
from tests.test_appmanifest import FAKE_APP, build_appinfo_v29
from tests.test_gameversion import fake_pe
from tests.test_mo2 import INI

SKYRIM = 489830


class IsolatedHome(unittest.TestCase):
    """A fresh HOME/XDG tree per test, Steam "not running", recipe index offline,
    and a dummy Syncthing binary so ModSyncService() never downloads one."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        fake_bin = self.tmp / "fake-syncthing"
        fake_bin.write_text("#!/bin/sh\nexit 1\n")
        for key, value in {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_DATA_HOME": str(self.tmp / "data"),
            "MODSYNC_SYNCTHING_BIN": str(fake_bin),
        }.items():
            env = patch.dict(os.environ, {key: value})
            env.start()
            self.addCleanup(env.stop)
        flatpak = patch.dict(os.environ)
        flatpak.start()
        self.addCleanup(flatpak.stop)
        os.environ.pop("FLATPAK_ID", None)
        for p in (
            patch.object(shortcuts, "steam_is_running", lambda: False),
            patch.object(
                recipe.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")
            ),
        ):
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = main(list(args))
        return code, out.getvalue()

    # --- a fake Steam under this HOME ---
    def install_steam(self) -> Path:
        return fakesteam.make_steam(self.home / ".local/share/Steam")

    def add_skyrim_appinfo(self, root: Path) -> None:
        (root / "appcache" / "appinfo.vdf").write_bytes(build_appinfo_v29(SKYRIM, FAKE_APP))

    def move_skyrim_to_removable_library(self, root: Path) -> tuple[Path, Path]:
        """Skyrim lives on an SD card library; another library is unplugged."""
        sdcard = self.tmp / "run/media/deck/SDCARD"
        unplugged = self.tmp / "run/media/deck/UNPLUGGED"
        (sdcard / "steamapps" / "common").mkdir(parents=True)
        acf = root / "steamapps" / f"appmanifest_{SKYRIM}.acf"
        (sdcard / "steamapps" / acf.name).write_text(acf.read_text())
        acf.unlink()
        (root / "steamapps/common/Skyrim Special Edition").rmdir()
        game = sdcard / "steamapps/common/Skyrim Special Edition"
        game.mkdir()
        (root / "steamapps" / "libraryfolders.vdf").write_text(
            vdf.dumps({"libraryfolders": {
                "0": {"path": str(root), "apps": {}},
                "1": {"path": str(sdcard), "apps": {str(SKYRIM): "1"}},
                "2": {"path": str(unplugged), "apps": {"730": "1"}},
            }}) + "\n"
        )
        return game, unplugged


# --- 1. Steam not installed at all --------------------------------------------------


class NoSteamInstalled(IsolatedHome):
    def test_doctor_says_so_and_fails(self):
        code, out = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("No Steam installation found", out)
        self.assertNotIn("Traceback", out)

    def test_game_status_says_so_and_fails(self):
        code, out = self.run_cli("game", "status")
        self.assertEqual(code, 1, out)
        self.assertIn("not found through Steam", out)
        self.assertNotIn("Traceback", out)

    def test_steam_shortcut_says_so_and_fails(self):
        code, out = self.run_cli("steam", "shortcut")
        self.assertEqual(code, 1)
        self.assertIn("make sure Steam is installed", out)

    def test_launch_enable_says_so_and_fails(self):
        code, out = self.run_cli("launch", "enable")
        self.assertEqual(code, 1)
        self.assertIn("Steam was not found", out)

    def test_launch_status_says_so_and_fails(self):
        code, out = self.run_cli("launch", "status")
        self.assertEqual(code, 1)
        self.assertIn("Steam was not found on this machine", out)


# --- 2. Skyrim on a removable library; one library unplugged --------------------------


class RemovableLibrary(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.root = self.install_steam()
        self.game, self.unplugged = self.move_skyrim_to_removable_library(self.root)
        (self.game / "SkyrimSE.exe").write_bytes(fake_pe(1, 6, 1170, 0))

    def test_doctor_lists_every_library_and_finds_the_game_on_the_card(self):
        code, out = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("Libraries (3)", out)
        self.assertIn(str(self.unplugged), out)
        self.assertIn(f"installed: {self.game}", out)
        self.assertIn("runtime version: 1.6.1170", out)

    def test_game_dir_is_found_on_the_card(self):
        self.assertEqual(gameversion.find_game_dir(None), self.game)

    def test_game_status_reads_the_card_and_survives_the_unplugged_library(self):
        code, out = self.run_cli("game", "status")
        self.assertEqual(code, 0, out)
        self.assertIn("Installed:      1.6.1170", out)
        self.assertIn(str(self.game), out)


# --- 3. Steam is mid-update -------------------------------------------------------


def _manifest(state_flags: str, to_download: str, downloaded: str) -> str:
    return vdf.dumps({"AppState": {
        "appid": str(SKYRIM),
        "name": "Skyrim SE",
        "StateFlags": state_flags,
        "installdir": "Skyrim Special Edition",
        "buildid": "13189953",
        "BytesToDownload": to_download,
        "BytesDownloaded": downloaded,
        "TargetBuildID": "24914197",
        "InstalledDepots": {"489831": {"manifest": "8442952117333549665"}},
        "UserConfig": {"language": "english"},
    }}) + "\n"


class GameUpdateInProgress(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.root = self.install_steam()
        self.add_skyrim_appinfo(self.root)
        self.game = self.root / "steamapps/common/Skyrim Special Edition"
        self.acf = self.root / "steamapps" / f"appmanifest_{SKYRIM}.acf"
        # Steam already committed the new executable; the vault expects the old one.
        (self.game / "SkyrimSE.exe").write_bytes(fake_pe(1, 7, 104, 0))
        inst = self.tmp / "MO2"
        inst.mkdir()
        gameversion.record_vault_version(inst, GameVersion.parse("1.6.1170"))
        State(instance_path=str(inst)).save()

    def test_running_update_is_reported_as_not_ready(self):
        self.acf.write_text(_manifest("1030", "1658888697", "500000000"))
        st = ModSyncService().game_status(refresh_index=False)
        self.assertTrue(st.steam_updating)
        self.assertFalse(st.needs_pin)
        self.assertFalse(st.needs_downgrade)
        self.assertIsNone(st.suggested_target)
        code, out = self.run_cli("game", "status")
        self.assertEqual(code, 0, out)
        self.assertIn("updating", out)
        self.assertNotIn("modsync game pin", out)
        self.assertNotIn("modsync game downgrade", out)
        self.assertNotIn("was built for", out)

    def test_paused_partial_download_is_reported_as_not_ready(self):
        self.acf.write_text(_manifest("6", "1658888697", "12345"))
        st = ModSyncService().game_status(refresh_index=False)
        self.assertTrue(st.steam_updating)
        self.assertFalse(st.needs_pin)

    def test_pending_update_with_nothing_downloaded_still_offers_the_pin(self):
        self.acf.write_text(_manifest("6", "1658888697", "0"))
        st = ModSyncService().game_status(refresh_index=False)
        self.assertFalse(st.steam_updating)
        self.assertTrue(st.needs_pin)
        self.assertTrue(st.mismatch)
        code, out = self.run_cli("game", "status")
        self.assertEqual(code, 0, out)
        self.assertIn("modsync game pin", out)
        self.assertIn("was built for 1.6.1170", out)


# --- 4. Corrupted / incomplete MO2 instance ------------------------------------------


class CorruptMo2Instance(IsolatedHome):
    def test_use_accepts_an_ini_without_content_dirs(self):
        inst = self.tmp / "MO2"
        inst.mkdir()
        (inst / "ModOrganizer.ini").write_text(INI)  # no mods/, no profiles/, no .exe
        code, out = self.run_cli("mo2", "use", str(inst))
        self.assertEqual(code, 0, out)
        self.assertIn("Using", out)
        code, out = self.run_cli("mo2", "status")
        self.assertEqual(code, 0, out)
        self.assertIn(str(inst), out)

    def test_use_survives_a_garbage_ini(self):
        inst = self.tmp / "MO2"
        (inst / "mods").mkdir(parents=True)
        (inst / "ModOrganizer.ini").write_bytes(b"\x00\xff[General\ngamePath=\xfe\xfe\n=\n")
        code, out = self.run_cli("mo2", "use", str(inst))
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)

    def test_status_reports_an_instance_dir_that_vanished(self):
        inst = self.tmp / "MO2"
        inst.mkdir()
        code, _ = self.run_cli("mo2", "use", str(inst))
        self.assertEqual(code, 0)
        inst.rmdir()  # SD card unplugged, directory deleted, ...
        code, out = self.run_cli("mo2", "status")
        self.assertEqual(code, 1, out)
        self.assertIn(str(inst), out)
        self.assertIn("no longer exists", out)

    def test_doctor_flags_a_half_installed_instance_recorded_by_mo2lint(self):
        self.install_steam()
        inst = self.tmp / "MO2"
        inst.mkdir()
        (inst / "ModOrganizer.ini").write_text(INI)
        mo2lint = Path(os.environ["XDG_CONFIG_HOME"]) / "mo2-lint"
        mo2lint.mkdir(parents=True)
        (mo2lint / "state.json").write_text(json.dumps({"instances": [{"instance_path": str(inst)}]}))
        code, out = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn(str(inst), out)
        self.assertIn("(missing)", out)
        self.assertNotIn("Traceback", out)


# --- 6. Something that is not Syncthing owns the configured port ---------------------


class _Canned(http.server.BaseHTTPRequestHandler):
    status = 200
    body = b""

    def do_GET(self):  # noqa: N802 (http.server API)
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):  # silence
        pass


class SyncthingPortConflict(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def serve(self, status: int, body: bytes) -> str:
        handler = type("Handler", (_Canned,), {"status": status, "body": body})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"127.0.0.1:{server.server_address[1]}"

    def manager(self, address: str, script: str) -> SyncthingManager:
        binary = self.tmp / "syncthing"
        binary.write_text("#!/bin/sh\n" + script + "\n")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        home = self.tmp / "home"
        home.mkdir()
        (home / "config.xml").write_text(
            f"<configuration><gui><address>{address}</address><apikey>k</apikey></gui></configuration>"
        )
        mgr = SyncthingManager(home, binary=binary, gui_address=address)
        self.addCleanup(mgr.stop)
        return mgr

    def test_port_owner_that_rejects_the_ping_gives_an_error_not_a_hang(self):
        address = self.serve(404, b"not found")
        mgr = self.manager(address, "exit 3")  # our own daemon cannot bind either
        started = time.monotonic()
        with self.assertRaisesRegex(SyncthingError, "exited early"):
            mgr.start(timeout=5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(mgr.running)

    def test_port_owner_that_answers_garbage_is_not_mistaken_for_syncthing(self):
        address = self.serve(200, b"<html>hello from some other app</html>")
        mgr = self.manager(address, "exit 3")
        with self.assertRaises(SyncthingError):
            mgr.start(timeout=5)
        self.assertFalse(mgr.running)

    def test_real_syncthing_on_the_port_is_attached_to(self):
        address = self.serve(200, b'{"ping":"pong"}')
        mgr = self.manager(address, "exit 3")
        mgr.start(timeout=5)
        self.assertTrue(mgr.running)
        mgr.stop()  # leaves the daemon we did not start alone

    def test_daemon_that_never_comes_up_times_out(self):
        address = self.serve(404, b"")
        mgr = self.manager(address, "sleep 30")
        started = time.monotonic()
        with self.assertRaisesRegex(SyncthingError, "timed out"):
            mgr.start(timeout=1)
        self.assertLess(time.monotonic() - started, 5)


# --- 7. Pairing with bad codes and silent peers --------------------------------------


class PairingFailures(IsolatedHome):
    def test_sync_join_rejects_malformed_codes(self):
        not_json = "MODSYNC1-" + base64.urlsafe_b64encode(b"not json").decode().rstrip("=")
        missing_keys = "MODSYNC1-" + base64.urlsafe_b64encode(b'{"x":1}').decode().rstrip("=")
        for bad in ("garbage", "MODSYNC1-!!!!", not_json, missing_keys, ""):
            with self.subTest(code=bad):
                code, out = self.run_cli("sync", "join", bad, str(self.tmp))
                self.assertEqual(code, 2, out)
                self.assertIn("valid pairing code", out)
                self.assertNotIn("Traceback", out)

    def test_join_unreachable_host_is_a_pair_error(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]
        ann = Announcement("ghost", "127.0.0.1", closed_port, "s")
        with self.assertRaisesRegex(PairError, "could not reach ghost"):
            pairing_lan.join_pairing(ann, PairPayload("ME"), "123456", timeout=1)

    def test_join_peer_that_never_answers_gives_up_within_the_timeout(self):
        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)  # accepted by the kernel, never read by anyone
        self.addCleanup(silent.close)
        ann = Announcement("mute", "127.0.0.1", silent.getsockname()[1], "s")
        started = time.monotonic()
        with self.assertRaises((PairError, OSError)):
            pairing_lan.join_pairing(ann, PairPayload("ME"), "123456", timeout=0.5)
        self.assertLess(time.monotonic() - started, 3)

    def test_host_with_no_joiner_times_out(self):
        with self.assertRaisesRegex(PairError, "timed out"):
            pairing_lan.host_pairing(PairPayload("ME", "f", "l"), "deck", "123456", timeout=0.6)


if __name__ == "__main__":
    unittest.main()
