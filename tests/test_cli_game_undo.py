"""`modsync game restore`, `modsync game unpin` and `modsync steam shortcut
--remove`: every path ModSync can take on the game and Steam has a way back."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from modsync.__main__ import main
from modsync.downgrade import engine
from modsync.service import ModSyncService
from modsync.steam import shortcuts, vdf
from modsync.steam.appmanifest import AppManifest
from tests.test_appmanifest import ACF, FAKE_APP, build_appinfo_v29
from tests.test_shortcuts import _sample


def run_cli(*args):
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(list(args))
    return code, out.getvalue()


class _GameBase(unittest.TestCase):
    """A Skyrim install in a tmp dir, with Steam's appmanifest and product
    cache, wired into the service without a real Steam on disk."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        self.game = self.tmp / "Skyrim Special Edition"
        self.game.mkdir()
        self.acf = self.tmp / "appmanifest_489830.acf"
        self.acf.write_text(ACF)
        self.appinfo = self.tmp / "appinfo.vdf"
        self.appinfo.write_bytes(build_appinfo_v29(489830, FAKE_APP))
        app, acf, info = SimpleNamespace(install_path=self.game, library=None), self.acf, self.appinfo
        for p in (
            patch.object(ModSyncService, "_steam_app", lambda self: (app, acf, info)),
            patch.object(shortcuts, "steam_is_running", lambda: False),
        ):
            p.start()
            self.addCleanup(p.stop)


class RestoreCommandTests(_GameBase):
    def test_no_backup_points_at_steam_verify(self):
        code, out = run_cli("game", "restore")
        self.assertEqual(code, 1)
        self.assertIn("Cannot restore", out)
        self.assertIn("Verify integrity of game files", out)

    def test_restores_files_and_removes_backup(self):
        backup = engine.backup_dir_for(self.game)
        (backup / "Data").mkdir(parents=True)
        (backup / "SkyrimSE.exe").write_bytes(b"original exe")
        (backup / "Data" / "Skyrim.esm").write_bytes(b"original esm")
        (self.game / "SkyrimSE.exe").write_bytes(b"downgraded exe")
        code, out = run_cli("game", "restore")
        self.assertEqual(code, 0, out)
        self.assertIn("Restored 2 original file(s)", out)
        self.assertIn("Data/Skyrim.esm", out)
        self.assertEqual((self.game / "SkyrimSE.exe").read_bytes(), b"original exe")
        self.assertEqual((self.game / "Data" / "Skyrim.esm").read_bytes(), b"original esm")
        self.assertFalse(engine.work_dir_for(self.game).exists())
        # a second run has nothing left to do
        code, out = run_cli("game", "restore")
        self.assertEqual(code, 1)

    def test_discard_deletes_backup_without_touching_game(self):
        backup = engine.backup_dir_for(self.game)
        backup.mkdir(parents=True)
        (backup / "SkyrimSE.exe").write_bytes(b"stale original")
        (self.game / "SkyrimSE.exe").write_bytes(b"current")
        code, out = run_cli("game", "restore", "--discard")
        self.assertEqual(code, 0, out)
        self.assertIn("Deleted the downgrade backup", out)
        self.assertEqual((self.game / "SkyrimSE.exe").read_bytes(), b"current")
        self.assertFalse(engine.work_dir_for(self.game).exists())

    def test_status_mentions_backup(self):
        backup = engine.backup_dir_for(self.game)
        backup.mkdir(parents=True)
        (backup / "SkyrimSE.exe").write_bytes(b"x")
        with patch("modsync.downgrade.recipe.load_index", side_effect=RuntimeError("offline")):
            code, out = run_cli("game", "status")
        self.assertEqual(code, 0)
        self.assertIn("modsync game restore", out)


class UnpinCommandTests(_GameBase):
    def test_pin_then_unpin_restores_manifest(self):
        before = vdf.loads(ACF)
        code, out = run_cli("game", "pin")
        self.assertEqual(code, 0, out)
        self.assertIn("Pinned", out)
        self.assertEqual(AppManifest.load(self.acf).state_flags, 4)
        self.assertTrue(ModSyncService._pin_record_path().exists())
        with patch("modsync.downgrade.recipe.load_index", side_effect=RuntimeError("offline")):
            code, out = run_cli("game", "status")
        self.assertIn("modsync game unpin", out)

        code, out = run_cli("game", "unpin")
        self.assertEqual(code, 0, out)
        self.assertIn("Unpinned", out)
        self.assertIn("StateFlags: 4 → 6", out)
        self.assertEqual(vdf.loads(self.acf.read_text()), before)
        self.assertFalse(ModSyncService._pin_record_path().exists())

    def test_unpin_without_record_flags_update_required(self):
        m = AppManifest.load(self.acf)
        m.state["StateFlags"] = "4"
        m.state["buildid"] = "24914197"
        m.save()
        code, out = run_cli("game", "unpin")
        self.assertEqual(code, 0, out)
        self.assertIn("re-check", out)
        again = AppManifest.load(self.acf)
        self.assertEqual(again.state_flags, 6)
        self.assertEqual(again.buildid, 0)

    def test_unpin_drops_queued_pin_and_needs_steam_closed(self):
        with patch.object(shortcuts, "steam_is_running", lambda: True):
            code, out = run_cli("game", "pin")
            self.assertEqual(code, 0)
            self.assertTrue(ModSyncService._pending_pin_path().exists())
            code, out = run_cli("game", "unpin")
            self.assertEqual(code, 1)
            self.assertIn("Close Steam first", out)
        code, out = run_cli("game", "unpin")
        self.assertEqual(code, 0, out)
        self.assertFalse(ModSyncService._pending_pin_path().exists())


class ShortcutRemoveCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.steam = Path(tmp.name)
        self.cfg = self.steam / "userdata" / "12345" / "config"
        self.cfg.mkdir(parents=True)
        self.original = shortcuts.dumps(_sample())
        (self.cfg / "shortcuts.vdf").write_bytes(self.original)
        steam = self.steam
        for p in (
            patch.object(shortcuts.platforms, "current", lambda: SimpleNamespace(steam_roots=lambda: [steam])),
            patch.object(shortcuts, "steam_is_running", lambda: False),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_add_then_remove_round_trips(self):
        code, out = run_cli("steam", "shortcut", "--native")
        self.assertEqual(code, 0, out)
        names = [e["AppName"] for e in shortcuts.load(self.cfg / "shortcuts.vdf")["shortcuts"].values()]
        self.assertEqual(names, ["Existing Game", "ModSync"])
        code, out = run_cli("steam", "add-shortcut", "--remove")
        self.assertEqual(code, 0, out)
        self.assertIn("removed 'ModSync'", out)
        self.assertEqual((self.cfg / "shortcuts.vdf").read_bytes(), self.original)

    def test_remove_when_absent_is_a_noop(self):
        code, out = run_cli("steam", "shortcut", "--remove")
        self.assertEqual(code, 0)
        self.assertIn("nothing to remove", out)
        self.assertEqual((self.cfg / "shortcuts.vdf").read_bytes(), self.original)

    def test_usage_lists_remove(self):
        code, out = run_cli("steam")
        self.assertEqual(code, 2)
        self.assertIn("--remove", out)


if __name__ == "__main__":
    unittest.main()
