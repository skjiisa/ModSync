import json
import struct
import tempfile
import unittest
from pathlib import Path

from modsync import gameversion as gv
from modsync.steam import pe

REAL_EXE = Path.home() / ".local/share/Steam/steamapps/common/Skyrim Special Edition/SkyrimSE.exe"


def fake_pe(major: int, minor: int, build: int, rev: int, *, decoy: bool = False) -> bytes:
    """Bytes containing a VS_FIXEDFILEINFO header, surrounded by junk."""
    body = b"MZ" + b"\x00" * 100
    if decoy:
        # The magic without a valid dwStrucVersion must be skipped, not trusted.
        body += b"\xbd\x04\xef\xfe" + struct.pack("<III", 0xDEADBEEF, 9 << 16, 9 << 16)
        body += b"\x00" * 16
    body += b"\xbd\x04\xef\xfe" + struct.pack(
        "<III", 0x00010000, (major << 16) | minor, (build << 16) | rev
    )
    return body + b"\x00" * 64


class PeVersionTests(unittest.TestCase):
    def test_reads_version_from_fixedfileinfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "game.exe"
            p.write_bytes(fake_pe(1, 6, 1170, 0))
            self.assertEqual(pe.file_version(p), (1, 6, 1170, 0))

    def test_skips_decoy_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "game.exe"
            p.write_bytes(fake_pe(1, 7, 104, 0, decoy=True))
            self.assertEqual(pe.file_version(p), (1, 7, 104, 0))

    def test_no_version_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "plain.exe"
            p.write_bytes(b"MZ" + b"\x00" * 300)
            self.assertIsNone(pe.file_version(p))
            (Path(tmp) / "empty.exe").write_bytes(b"")
            self.assertIsNone(pe.file_version(Path(tmp) / "empty.exe"))
            self.assertIsNone(pe.file_version(Path(tmp) / "missing.exe"))

    @unittest.skipUnless(REAL_EXE.is_file(), "no local Skyrim SE install")
    def test_real_skyrim_binary(self):
        v = pe.file_version(REAL_EXE)
        self.assertIsNotNone(v)
        assert v is not None
        self.assertEqual(v[0], 1)
        self.assertIn(v[1], (5, 6, 7))


class GameVersionTests(unittest.TestCase):
    def test_str_drops_zero_revision(self):
        self.assertEqual(str(gv.GameVersion((1, 6, 1170, 0))), "1.6.1170")
        self.assertEqual(str(gv.GameVersion((1, 6, 1170, 3))), "1.6.1170.3")

    def test_parse_round_trip_and_equality(self):
        self.assertEqual(gv.GameVersion.parse("1.6.1170"), gv.GameVersion((1, 6, 1170, 0)))
        self.assertEqual(gv.GameVersion.parse("1.5.97.0"), gv.GameVersion((1, 5, 97, 0)))
        with self.assertRaises(ValueError):
            gv.GameVersion.parse("1.6.1170.0.1")
        with self.assertRaises(ValueError):
            gv.GameVersion.parse("latest")

    def test_installed_version_reads_main_exe_not_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / "SkyrimSELauncher.exe").write_bytes(fake_pe(1, 0, 0, 0))
            self.assertIsNone(gv.installed_version(game))
            (game / "SkyrimSE.exe").write_bytes(fake_pe(1, 7, 104, 0))
            self.assertEqual(str(gv.installed_version(game)), "1.7.104")


class VaultMetaTests(unittest.TestCase):
    def test_record_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = gv.record_vault_version(tmp, gv.GameVersion((1, 6, 1170, 0)))
            self.assertTrue((Path(tmp) / gv.VAULT_META_NAME).exists())
            loaded = gv.VaultMeta.load(tmp)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.appid, 489830)
            self.assertEqual(loaded.runtime, "1.6.1170")
            self.assertEqual(loaded.set_by, meta.set_by)
            self.assertEqual(str(loaded.version), "1.6.1170")

    def test_load_tolerates_missing_or_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(gv.VaultMeta.load(tmp))
            (Path(tmp) / gv.VAULT_META_NAME).write_text("{not json")
            self.assertIsNone(gv.VaultMeta.load(tmp))
            (Path(tmp) / gv.VAULT_META_NAME).write_text(json.dumps({"game": {"appid": 1}}))
            self.assertIsNone(gv.VaultMeta.load(tmp))


class VersionCheckTests(unittest.TestCase):
    def _instance_pointing_at(self, tmp: Path, game_dir: Path) -> Path:
        inst = tmp / "instance"
        inst.mkdir()
        (inst / "ModOrganizer.ini").write_text(
            "[General]\n"
            "gameName=@ByteArray(Skyrim Special Edition)\n"
            f"gamePath=@ByteArray(Z:{str(game_dir).replace('/', chr(92) * 2)})\n"
        )
        return inst

    def test_match_mismatch_and_unrecorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            game = tmp_p / "Skyrim Special Edition"
            game.mkdir()
            (game / "SkyrimSE.exe").write_bytes(fake_pe(1, 7, 104, 0))
            inst = self._instance_pointing_at(tmp_p, game)

            vc = gv.check(inst)
            self.assertEqual(str(vc.installed), "1.7.104")
            self.assertIsNone(vc.expected)
            self.assertFalse(vc.mismatch)
            self.assertFalse(vc.ok)
            self.assertIn("does not record", vc.summary())

            gv.record_vault_version(inst, gv.GameVersion((1, 6, 1170, 0)))
            vc = gv.check(inst)
            self.assertTrue(vc.mismatch)
            self.assertIn("1.7.104", vc.summary())
            self.assertIn("1.6.1170", vc.summary())

            gv.record_vault_version(inst, gv.GameVersion((1, 7, 104, 0)))
            vc = gv.check(inst)
            self.assertTrue(vc.ok)
            self.assertIn("matches", vc.summary())

    def test_meta_for_other_game_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            game = tmp_p / "Skyrim Special Edition"
            game.mkdir()
            (game / "SkyrimSE.exe").write_bytes(fake_pe(1, 6, 1170, 0))
            inst = self._instance_pointing_at(tmp_p, game)
            gv.VaultMeta(appid=377160, runtime="1.10.163").save(inst)
            vc = gv.check(inst)
            self.assertIsNone(vc.expected)
            self.assertFalse(vc.mismatch)


if __name__ == "__main__":
    unittest.main()
