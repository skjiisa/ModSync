import json
import struct
import tempfile
import unittest
from pathlib import Path

from modsync import gameversion as gv
from modsync.steam import pe

REAL_EXE = Path.home() / ".local/share/Steam/steamapps/common/Skyrim Special Edition/SkyrimSE.exe"


def _string(key: str, value: str) -> bytes:
    """One String struct of a StringFileInfo table (wLength, wValueLength, wType, key, value)."""
    key_b = key.encode("utf-16-le") + b"\x00\x00"
    val_b = value.encode("utf-16-le") + b"\x00\x00"
    body = key_b + b"\x00" * (-(6 + len(key_b)) % 4) + val_b
    body += b"\x00" * (-(6 + len(body)) % 4)
    return struct.pack("<HHH", 6 + len(body), len(val_b) // 2, 1) + body


def fake_pe(
    major: int,
    minor: int,
    build: int,
    rev: int,
    *,
    decoy: bool = False,
    strings: dict[str, str] | None = None,
) -> bytes:
    """Bytes containing a VS_VERSIONINFO resource, surrounded by junk.

    ``strings`` adds a StringFileInfo table (e.g. ``{"ProductVersion": "1.5.97.0"}``)
    after the fixed struct, the way a real resource lays it out."""
    body = b"MZ" + b"\x00" * 100
    if decoy:
        # The magic without a valid dwStrucVersion must be skipped, not trusted.
        body += b"\xbd\x04\xef\xfe" + struct.pack("<III", 0xDEADBEEF, 9 << 16, 9 << 16)
        body += b"\x00" * 16
    fixed = b"\xbd\x04\xef\xfe" + struct.pack(
        "<III", 0x00010000, (major << 16) | minor, (build << 16) | rev
    )
    fixed += b"\x00" * (52 - len(fixed))  # the rest of VS_FIXEDFILEINFO
    children = b""
    if strings:
        table = b"".join(_string(k, v) for k, v in strings.items())
        lang = "040904b0".encode("utf-16-le") + b"\x00\x00"
        lang += b"\x00" * (-(6 + len(lang)) % 4)
        table = struct.pack("<HHH", 6 + len(lang) + len(table), 0, 1) + lang + table
        sfi = "StringFileInfo".encode("utf-16-le") + b"\x00\x00"
        children = struct.pack("<HHH", 6 + len(sfi) + len(table), 0, 1) + sfi + table
    root_key = "VS_VERSION_INFO".encode("utf-16-le") + b"\x00\x00"
    payload = root_key + b"\x00\x00" + fixed + children
    body += struct.pack("<HHH", 6 + len(payload), 52, 0) + payload
    return body + b"\x00" * 64


class PeVersionTests(unittest.TestCase):
    def test_reads_version_from_fixedfileinfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "game.exe"
            p.write_bytes(fake_pe(1, 6, 1170, 0))
            self.assertEqual(pe.file_version(p), (1, 6, 1170, 0))

    def test_prefers_string_table_over_fixed_struct(self):
        # Skyrim SE 1.5.97 leaves VS_FIXEDFILEINFO at 1.0.0.0 and only names the
        # real version in the string table, which is also what SKSE reads.
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "game.exe"
            p.write_bytes(fake_pe(1, 0, 0, 0, strings={"FileVersion": "1.5.97.0", "ProductVersion": "1.5.97.0"}))
            self.assertEqual(pe.file_version(p), (1, 5, 97, 0))
            p.write_bytes(fake_pe(1, 0, 0, 0, strings={"ProductVersion": "1, 6, 1170, 0"}))
            self.assertEqual(pe.file_version(p), (1, 6, 1170, 0))

    def test_falls_back_to_fixed_struct_when_strings_are_unusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "game.exe"
            p.write_bytes(fake_pe(1, 7, 104, 0, strings={"ProductVersion": "latest", "CompanyName": "x"}))
            self.assertEqual(pe.file_version(p), (1, 7, 104, 0))

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
            self.assertIn("No version is recorded", vc.summary())

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


class SkseScanTests(unittest.TestCase):
    def test_reads_runtime_from_dll_name_in_game_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / "skse64_loader.exe").write_bytes(b"MZ")
            (game / "skse64_steam_loader.dll").write_bytes(b"MZ")
            (game / "SKSE64_1_6_1170.dll").write_bytes(b"MZ")  # case-insensitive
            sk = gv.scan_skse(game)
            self.assertEqual(str(sk.runtime), "1.6.1170")
            self.assertFalse(sk.ambiguous)
            self.assertEqual(sk.files[0].where, "game folder")
            self.assertIn("SKSE64_1_6_1170.dll in the game folder", sk.describe())

    def test_gog_suffix_and_directories_are_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / "skse64_1_6_659_gog.dll").write_bytes(b"MZ")
            (game / "skse64_1_5_97.dll").mkdir()  # a directory is not a DLL
            self.assertEqual(str(gv.scan_skse(game).runtime), "1.6.659")

    def test_several_runtimes_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / "skse64_1_5_97.dll").write_bytes(b"MZ")
            (game / "skse64_1_6_1170.dll").write_bytes(b"MZ")
            sk = gv.scan_skse(game)
            self.assertIsNone(sk.runtime)
            self.assertTrue(sk.ambiguous)
            self.assertEqual([str(v) for v in sk.runtimes], ["1.5.97", "1.6.1170"])

    def test_same_runtime_in_two_places_is_not_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            game = tmp_p / "game"
            game.mkdir()
            (game / "skse64_1_6_1170.dll").write_bytes(b"MZ")
            inst = tmp_p / "instance"
            (inst / "mods" / "SKSE" / "Root").mkdir(parents=True)
            (inst / "mods" / "SKSE" / "Root" / "skse64_1_6_1170.dll").write_bytes(b"MZ")
            sk = gv.scan_skse(game, inst)
            self.assertEqual(len(sk.files), 2)
            self.assertEqual(str(sk.runtime), "1.6.1170")

    def test_finds_skse_kept_as_a_mod(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "instance"
            (inst / "mods" / "SKSE64" ).mkdir(parents=True)
            (inst / "mods" / "SKSE64" / "skse64_1_5_97.dll").write_bytes(b"MZ")
            # Deeper files must not be picked up: only the mod root and Root/.
            (inst / "mods" / "Other" / "deep" / "er").mkdir(parents=True)
            (inst / "mods" / "Other" / "deep" / "er" / "skse64_1_6_640.dll").write_bytes(b"MZ")
            sk = gv.scan_skse(None, inst)
            self.assertEqual(str(sk.runtime), "1.5.97")
            self.assertIn("SKSE64", sk.files[0].where)

    def test_nothing_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            sk = gv.scan_skse(tmp, None)
            self.assertEqual(sk.files, [])
            self.assertIsNone(sk.runtime)
            self.assertEqual(sk.describe(), "")
        self.assertIsNone(gv.scan_skse(None, None).runtime)
        self.assertIsNone(gv.scan_skse("/definitely/not/here", "/nor/here").runtime)


class ChooseVaultVersionTests(unittest.TestCase):
    def _skse(self, *versions: str) -> gv.SkseCheck:
        return gv.SkseCheck(
            [gv.SkseFile(Path(f"skse64_{v.replace('.', '_')}.dll"), gv.GameVersion.parse(v), "game folder") for v in versions]
        )

    def test_skse_wins_over_installed_game(self):
        installed = gv.GameVersion.parse("1.7.104")
        self.assertEqual(
            gv.choose_vault_version(installed, self._skse("1.6.1170")),
            (gv.GameVersion.parse("1.6.1170"), "skse"),
        )

    def test_falls_back_to_installed_game(self):
        installed = gv.GameVersion.parse("1.7.104")
        self.assertEqual(gv.choose_vault_version(installed, self._skse()), (installed, "game"))
        self.assertEqual(gv.choose_vault_version(installed, self._skse("1.5.97", "1.6.1170")), (installed, "game"))
        self.assertEqual(gv.choose_vault_version(None, self._skse()), (None, ""))

    def test_source_is_recorded_in_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            gv.record_vault_version(tmp, gv.GameVersion.parse("1.6.1170"), source="skse")
            loaded = gv.VaultMeta.load(tmp)
            assert loaded is not None
            self.assertEqual(loaded.set_from, "skse")
            # Older manifests without the field still load.
            (Path(tmp) / gv.VAULT_META_NAME).write_text(
                json.dumps({"modsync": 1, "game": {"appid": 489830, "runtime": "1.6.1170"}})
            )
            loaded = gv.VaultMeta.load(tmp)
            assert loaded is not None
            self.assertEqual(loaded.set_from, "")


class SkseInVersionCheckTests(unittest.TestCase):
    def _setup(self, tmp: Path, game_version: tuple, skse: str | None) -> tuple[Path, Path]:
        game = tmp / "Skyrim Special Edition"
        game.mkdir()
        (game / "SkyrimSE.exe").write_bytes(fake_pe(*game_version))
        if skse:
            (game / f"skse64_{skse.replace('.', '_')}.dll").write_bytes(b"MZ")
        inst = tmp / "instance"
        inst.mkdir()
        (inst / "ModOrganizer.ini").write_text(
            "[General]\n"
            "gameName=@ByteArray(Skyrim Special Edition)\n"
            f"gamePath=@ByteArray(Z:{str(game).replace('/', chr(92) * 2)})\n"
        )
        return game, inst

    def test_imported_setup_after_steam_update(self):
        # The user's scenario: old MO2 setup with SKSE for 1.6.1170, game now 1.7.104,
        # no vault record yet.
        with tempfile.TemporaryDirectory() as tmp:
            _, inst = self._setup(Path(tmp), (1, 7, 104, 0), "1.6.1170")
            vc = gv.check(inst)
            self.assertIsNone(vc.expected)
            self.assertEqual(str(vc.skse.runtime), "1.6.1170")
            self.assertEqual(str(vc.skse_suggests), "1.6.1170")
            self.assertIn("most likely made for 1.6.1170", vc.skse_note())

    def test_skse_matches_game_and_no_vault_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, inst = self._setup(Path(tmp), (1, 6, 1170, 0), "1.6.1170")
            vc = gv.check(inst)
            self.assertIsNone(vc.skse_suggests)
            self.assertIn("matching the game", vc.skse_note())

    def test_vault_record_takes_precedence_over_skse(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, inst = self._setup(Path(tmp), (1, 7, 104, 0), "1.6.1170")
            gv.record_vault_version(inst, gv.GameVersion.parse("1.6.1170"), source="skse")
            vc = gv.check(inst)
            self.assertTrue(vc.mismatch)
            self.assertIsNone(vc.skse_suggests)  # the vault speaks; SKSE only comments
            self.assertIn("from its installed SKSE", vc.summary())
            self.assertIn("will work again once the game is 1.6.1170", vc.skse_note())

    def test_skse_disagreeing_with_vault_is_called_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, inst = self._setup(Path(tmp), (1, 6, 1170, 0), "1.5.97")
            gv.record_vault_version(inst, gv.GameVersion.parse("1.6.1170"))
            vc = gv.check(inst)
            self.assertTrue(vc.ok)
            self.assertIn("SKSE will need to be reinstalled", vc.skse_note())

    def test_no_skse_means_no_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, inst = self._setup(Path(tmp), (1, 7, 104, 0), None)
            vc = gv.check(inst)
            self.assertEqual(vc.skse_note(), "")
            self.assertIsNone(vc.skse_suggests)


if __name__ == "__main__":
    unittest.main()
