import tempfile
import unittest
from pathlib import Path

from modsync.steam import shortcuts


def _sample() -> dict:
    return {
        "shortcuts": {
            "0": {
                "appid": -1234567890,
                "AppName": "Existing Game",
                "Exe": '"/usr/bin/true"',
                "StartDir": '"/usr/bin"',
                "icon": "",
                "LaunchOptions": "",
                "IsHidden": 0,
                "tags": {"0": "favorite"},
            }
        }
    }


class BinaryVdfCodec(unittest.TestCase):
    def test_round_trip_is_byte_identical(self):
        blob = shortcuts.dumps(_sample())
        self.assertEqual(shortcuts.loads(blob), _sample())
        self.assertEqual(shortcuts.dumps(shortcuts.loads(blob)), blob)

    def test_type_tags(self):
        blob = shortcuts.dumps({"m": {"s": "x", "i": 7}})
        # map tag, key, then str tag + key + value, int tag + key + int32, two END markers
        self.assertEqual(
            blob,
            b"\x00m\x00" + b"\x01s\x00x\x00" + b"\x02i\x00\x07\x00\x00\x00" + b"\x08" + b"\x08",
        )

    def test_truncated_input_raises(self):
        with self.assertRaises(shortcuts.VdfError):
            shortcuts.loads(b"\x00shortcuts\x00")


class AddOrUpdate(unittest.TestCase):
    def test_appends_then_updates_in_place(self):
        root = _sample()
        idx = shortcuts.add_or_update(
            root, app_name="ModSync", exe='"/usr/bin/flatpak"', start_dir='"/usr/bin"',
            launch_options="run io.github.skjiisa.ModSync",
        )
        self.assertEqual(idx, 1)
        self.assertEqual(len(root["shortcuts"]), 2)
        # Same AppName -> update, not a duplicate; existing entry untouched.
        idx2 = shortcuts.add_or_update(
            root, app_name="ModSync", exe='"/usr/bin/flatpak"', start_dir='"/usr/bin"'
        )
        self.assertEqual(idx2, 1)
        self.assertEqual(len(root["shortcuts"]), 2)
        self.assertEqual(root["shortcuts"]["0"]["AppName"], "Existing Game")
        entry = root["shortcuts"]["1"]
        self.assertEqual(entry["LaunchOptions"], "")
        self.assertLess(entry["appid"], 0)  # high bit set, stored as signed int32

    def test_remove_undoes_add_byte_for_byte(self):
        root = _sample()
        before = shortcuts.dumps(root)
        shortcuts.add_or_update(root, app_name="ModSync", exe='"/usr/bin/flatpak"', start_dir='"/usr/bin"')
        self.assertNotEqual(shortcuts.dumps(root), before)
        self.assertTrue(shortcuts.remove(root, app_name="ModSync"))
        self.assertEqual(shortcuts.dumps(root), before)
        self.assertFalse(shortcuts.remove(root, app_name="ModSync"))
        self.assertFalse(shortcuts.remove({}, app_name="ModSync"))

    def test_remove_modsync_from_steam_only_writes_where_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            steam = Path(tmp)
            with_it = steam / "userdata" / "111" / "config"
            without = steam / "userdata" / "222" / "config"
            with_it.mkdir(parents=True)
            without.mkdir(parents=True)
            original = shortcuts.dumps(_sample())
            (with_it / "shortcuts.vdf").write_bytes(original)
            (without / "shortcuts.vdf").write_bytes(original)

            class _Plat:
                def steam_roots(self):
                    return [steam]

            orig_current = shortcuts.platforms.current
            shortcuts.platforms.current = lambda: _Plat()
            try:
                shortcuts.add_modsync_to_steam(flatpak_id=None)
                (without / "shortcuts.vdf").write_bytes(original)  # user 222 never had it
                removed = shortcuts.remove_modsync_from_steam()
            finally:
                shortcuts.platforms.current = orig_current
            self.assertEqual(removed, [with_it / "shortcuts.vdf"])
            self.assertEqual((with_it / "shortcuts.vdf").read_bytes(), original)
            self.assertEqual((without / "shortcuts.vdf").read_bytes(), original)
            # the pre-removal file (with ModSync in it) is kept as the rollback copy
            names = [
                e["AppName"]
                for e in shortcuts.load(with_it / "shortcuts.vdf.modsync-bak")["shortcuts"].values()
            ]
            self.assertIn("ModSync", names)

    def test_writes_backup_before_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            steam = Path(tmp)
            cfg = steam / "userdata" / "12345" / "config"
            cfg.mkdir(parents=True)
            original = shortcuts.dumps(_sample())
            (cfg / "shortcuts.vdf").write_bytes(original)

            class _Plat:
                def steam_roots(self):
                    return [steam]

            orig_current = shortcuts.platforms.current
            shortcuts.platforms.current = lambda: _Plat()
            try:
                written = shortcuts.add_modsync_to_steam(flatpak_id=None)
            finally:
                shortcuts.platforms.current = orig_current
            self.assertEqual(written, [cfg / "shortcuts.vdf"])
            self.assertEqual((cfg / "shortcuts.vdf.modsync-bak").read_bytes(), original)
            names = [e["AppName"] for e in shortcuts.load(cfg / "shortcuts.vdf")["shortcuts"].values()]
            self.assertEqual(names, ["Existing Game", "ModSync"])


if __name__ == "__main__":
    unittest.main()
