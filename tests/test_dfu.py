"""Daggerfall Unity: finding Steam's Daggerfall data, installing DFU from a
(fake) release zip, and pointing it at the data — offline, with the download
faked."""

import dataclasses
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from modsync import cli, dfu, launchhook
from modsync.games import DAGGERFALL
from modsync.state import State
from modsync.steam import vdf

_ZIP_FILES = {
    "DaggerfallUnity.x86_64": (b"#!/bin/sh\n", 0o755),
    "UnityPlayer.so": (b"\x7fELF", 0o755),
    "DaggerfallUnity_Data/StreamingAssets/Mods/readme.txt": (b"Put mods here.\n", 0o755),
    "DaggerfallUnity_Data/Resources/UnityPlayer.png": (b"\x89PNG", 0o644),
}


def _release_zip(path: Path, extra: dict[str, bytes] | None = None) -> str:
    with zipfile.ZipFile(path, "w") as zf:
        for name, (data, mode) in _ZIP_FILES.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o100000 | mode) << 16
            zf.writestr(info, data)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dagger(root: Path, *, textures: int = 472, skip: tuple[str, ...] = (), arena2: str = "ARENA2") -> Path:
    """A ``DF/DAGGER`` folder shaped like Steam's."""
    dagger = root / "DF" / "DAGGER"
    folder = dagger / arena2
    folder.mkdir(parents=True)
    for i in range(textures):
        (folder / f"TEXTURE.{i:03d}").touch()
    for name in ("ARCH3D.BSA", "BLOCKS.BSA", "MAPS.BSA", "DAGGER.SND", "WOODS.WLD"):
        if name not in skip:
            (folder / name).touch()
    return dagger


def _hook_off():
    return launchhook.LaunchHookStatus(DAGGERFALL, False, None, False, None, None, False, None, False, True)


class _Plat:
    def __init__(self, root: Path) -> None:
        self.root = root

    def steam_roots(self):
        return [self.root]


class SteamData(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_steams_folder_passes_dfus_check(self):
        self.assertEqual(dfu.data_problems(_dagger(self.tmp)), [])

    def test_lowercase_arena2_is_found_like_dfu_finds_it(self):
        dagger = _dagger(self.tmp, arena2="arena2")
        self.assertEqual(dfu.arena2(dagger), dagger / "arena2")
        self.assertEqual(dfu.data_problems(dagger), [])

    def test_explains_what_is_missing(self):
        self.assertIn("Steam hasn't installed", dfu.data_problems(None)[0])
        (self.tmp / "empty").mkdir()
        self.assertIn("no ARENA2", dfu.data_problems(self.tmp / "empty")[0])
        problems = dfu.data_problems(_dagger(self.tmp, textures=471, skip=("WOODS.WLD",)))
        self.assertEqual(len(problems), 2)
        self.assertIn("471 TEXTURE files", problems[0])
        self.assertIn("missing WOODS.WLD", problems[1])

    def test_finds_daggerfall_in_any_steam_library(self):
        steam, library = self.tmp / "steam", self.tmp / "sdcard"
        (steam / "steamapps").mkdir(parents=True)
        (library / "steamapps").mkdir(parents=True)
        (steam / "steamapps" / "libraryfolders.vdf").write_text(vdf.dumps({"libraryfolders": {
            "0": {"path": str(steam), "apps": {}},
            "1": {"path": str(library), "apps": {str(dfu.DAGGERFALL_APPID): "1"}},
        }}))
        (library / "steamapps" / f"appmanifest_{dfu.DAGGERFALL_APPID}.acf").write_text(vdf.dumps({"AppState": {
            "appid": str(dfu.DAGGERFALL_APPID), "name": "The Elder Scrolls II: Daggerfall",
            "installdir": "The Elder Scrolls Daggerfall",
        }}))
        with patch.object(dfu.platforms, "current", lambda: _Plat(steam)):
            self.assertIsNone(dfu.steam_daggerfall())  # manifest, but no files yet
            dagger = _dagger(library / "steamapps" / "common" / "The Elder Scrolls Daggerfall")
            self.assertEqual(dfu.steam_daggerfall(), dagger)


class SettingsIni(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.ini = self.tmp / "PortableAppdata" / "settings.ini"

    def test_new_file_holds_only_the_path(self):
        dfu.write_daggerfall_path(self.ini, Path("/games/DF/DAGGER"))
        self.assertEqual(self.ini.read_text(), "[Daggerfall]\nMyDaggerfallPath = /games/DF/DAGGER\n")
        self.assertEqual(dfu.read_daggerfall_path(self.ini), "/games/DF/DAGGER")

    def test_rewrites_the_key_and_nothing_else(self):
        text = (
            "[Daggerfall]\nMyDaggerfallPath = /old/DF/DAGGER\nMyDaggerfallUnitySavePath = \n\n"
            "[GUI]\nShowOptionsAtStart = True\nMyDaggerfallPath = not this one\n"
        )
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text(text)
        dfu.write_daggerfall_path(self.ini, Path("/new/DF/DAGGER"))
        self.assertEqual(self.ini.read_text(), text.replace("/old/DF/DAGGER", "/new/DF/DAGGER"))

    def test_adds_the_key_to_an_existing_section_or_file(self):
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text("[Daggerfall]\nMyDaggerfallUnitySavePath = \n")
        dfu.write_daggerfall_path(self.ini, Path("/d"))
        self.assertEqual(self.ini.read_text(), "[Daggerfall]\nMyDaggerfallPath = /d\nMyDaggerfallUnitySavePath = \n")
        self.ini.write_text("[GUI]\nShowOptionsAtStart = True\n")
        dfu.write_daggerfall_path(self.ini, Path("/d"))
        self.assertEqual(self.ini.read_text(), "[GUI]\nShowOptionsAtStart = True\n\n[Daggerfall]\nMyDaggerfallPath = /d\n")

    def test_several_values_in_another_section(self):
        dfu.write_daggerfall_path(self.ini, Path("/d"))
        dfu.write_settings(self.ini, "Video", {"ResolutionWidth": 3440, "ResolutionHeight": 1440})
        dfu.write_settings(self.ini, "Video", {"ResolutionHeight": 1600})
        self.assertEqual(
            self.ini.read_text(),
            "[Daggerfall]\nMyDaggerfallPath = /d\n\n[Video]\nResolutionHeight = 1600\nResolutionWidth = 3440\n",
        )
        self.assertEqual(dfu.read_setting(self.ini, "Video", "ResolutionWidth"), "3440")

    def test_a_byte_order_mark_does_not_hide_the_section(self):
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text("\ufeff[Daggerfall]\nMyDaggerfallPath = /old\n", encoding="utf-8")
        self.assertEqual(dfu.read_daggerfall_path(self.ini), "/old")
        dfu.write_daggerfall_path(self.ini, Path("/new"))
        self.assertEqual(self.ini.read_text(), "[Daggerfall]\nMyDaggerfallPath = /new\n")

    def test_empty_value_reads_as_unset(self):
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text("[Daggerfall]\nMyDaggerfallPath = \n")
        self.assertIsNone(dfu.read_daggerfall_path(self.ini))
        self.assertIsNone(dfu.read_daggerfall_path(self.tmp / "missing.ini"))


class DisplayResolution(unittest.TestCase):
    XRANDR = (
        "Screen 0: minimum 16 x 16, current 5360 x 1543, maximum 32767 x 32767\n"
        "HDMI-A-1 connected 1920x1080+0+0 (normal left inverted right x axis y axis) 479mm x 260mm\n"
        "   1920x1080     99.90*+\n"
        "DP-1 connected primary 3440x1440+1920+103 (normal left inverted right x axis y axis) 800mm x 337mm\n"
        "   3440x1440    164.87*+\n"
        "DP-2 disconnected (normal left inverted right x axis y axis)\n"
    )

    def _with(self, stdout, returncode=0):
        done = subprocess.CompletedProcess(["xrandr"], returncode, stdout, "")
        return patch.object(dfu.background, "run_host", lambda cmd, **kw: done)

    def test_the_primary_display_wins(self):
        with self._with(self.XRANDR):
            self.assertEqual(dfu.display_resolution(), (3440, 1440))

    def test_without_a_primary_the_first_connected_display(self):
        with self._with(self.XRANDR.replace(" primary", "")):
            self.assertEqual(dfu.display_resolution(), (1920, 1080))

    def test_a_rotated_deck_screen_reads_as_landscape(self):
        with self._with("eDP connected primary 1280x800+0+0 right (normal left inverted right x axis y axis) 0mm x 0mm\n"):
            self.assertEqual(dfu.display_resolution(), (1280, 800))

    def test_unknown_when_xrandr_fails_or_is_missing(self):
        with self._with("", returncode=1):
            self.assertIsNone(dfu.display_resolution())
        with self._with("Screen 0: minimum 16 x 16\n"):
            self.assertIsNone(dfu.display_resolution())

        def missing(cmd, **kw):
            raise FileNotFoundError("xrandr")

        with patch.object(dfu.background, "run_host", missing):
            self.assertIsNone(dfu.display_resolution())


class InstallDfu(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dagger = _dagger(self.tmp / "steam")
        self.release = self.tmp / "release.zip"
        self.sha = _release_zip(self.release)
        self.cache = self.tmp / "cache"
        self.downloads = 0

        def download(url, dest, progress, label, sha1=None):
            self.downloads += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.release, dest)
            return dest.stat().st_size

        for p in (
            patch.object(dfu, "DFU_SHA256", self.sha),
            patch.object(dfu.engine, "_download", download),
            patch.object(dfu, "is_running", lambda: False),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _install(self, dest):
        return dfu.install(dest, self.dagger, cache_dir=self.cache)

    def test_fresh_install_is_portable_and_points_at_steam(self):
        dest = self.tmp / "Games" / "Daggerfall Unity"
        result = self._install(dest)
        self.assertTrue(result.fresh)
        inst = result.install
        self.assertEqual(inst.path, dest)
        self.assertTrue(inst.executable.stat().st_mode & 0o111)
        self.assertEqual(dest.stat().st_mode & 0o777, 0o755)
        self.assertTrue(inst.portable)
        self.assertEqual(inst.version, dfu.DFU_VERSION)
        self.assertEqual(inst.daggerfall_path(), str(self.dagger))
        # Only the path: DFU's startup screen (mod list, settings) stays on.
        self.assertNotIn("ShowOptionsAtStart", inst.settings_ini.read_text())
        self.assertTrue(inst.mods_dir.is_dir())
        self.assertEqual([p.name for p in dest.parent.iterdir()], ["Daggerfall Unity"])  # no temp dir left

    def test_a_fresh_install_gets_the_displays_resolution(self):
        inst = dfu.install(self.tmp / "dfu", self.dagger, cache_dir=self.cache, resolution=(3440, 1440)).install
        self.assertEqual(dfu.read_setting(inst.settings_ini, "Video", "ResolutionWidth"), "3440")
        self.assertEqual(dfu.read_setting(inst.settings_ini, "Video", "ResolutionHeight"), "1440")
        self.assertNotIn("Fullscreen", inst.settings_ini.read_text())  # DFU's own default stays

    def test_installing_again_only_repoints_and_keeps_the_users_files(self):
        dest = self.tmp / "dfu"
        inst = self._install(dest).install
        (inst.mods_dir / "a.dfmod").write_bytes(b"mod")
        inst.settings_ini.write_text("[Daggerfall]\nMyDaggerfallPath = /old\n\n[Video]\nFullscreen = True\n")
        moved = _dagger(self.tmp / "sdcard")
        result = dfu.install(dest, moved, cache_dir=self.cache, resolution=(1280, 800))
        self.assertFalse(result.fresh)
        self.assertEqual(self.downloads, 1)
        self.assertEqual((inst.mods_dir / "a.dfmod").read_bytes(), b"mod")
        self.assertEqual(inst.settings_ini.read_text(), f"[Daggerfall]\nMyDaggerfallPath = {moved}\n\n[Video]\nFullscreen = True\n")

    def test_cached_download_is_reused_and_a_bad_one_replaced(self):
        self._install(self.tmp / "one")
        self._install(self.tmp / "two")
        self.assertEqual(self.downloads, 1)
        (self.cache / "dfu_linux_64bit-v1.1.1.zip").write_bytes(b"truncated")
        self._install(self.tmp / "three")
        self.assertEqual(self.downloads, 2)

    def test_refuses_folders_it_does_not_own(self):
        busy = self.tmp / "busy"
        busy.mkdir()
        (busy / "save.txt").touch()
        with self.assertRaisesRegex(dfu.DfuError, "isn't empty"):
            self._install(busy)
        other = self.tmp / "other"
        other.mkdir()
        (other / dfu.EXECUTABLE).touch()
        with self.assertRaisesRegex(dfu.DfuError, "didn't install"):
            self._install(other)
        (other / dfu.RECORD_NAME).write_text(json.dumps({"version": "1.0.0"}))
        with self.assertRaisesRegex(dfu.DfuError, "Daggerfall Unity 1.0.0"):
            self._install(other)

    def test_an_empty_folder_is_fine(self):
        dest = self.tmp / "empty"
        dest.mkdir()
        self.assertTrue(self._install(dest).fresh)

    def test_refuses_without_valid_data(self):
        dagger = _dagger(self.tmp / "broken", textures=3)
        with self.assertRaisesRegex(dfu.DfuError, "TEXTURE"):
            dfu.install(self.tmp / "dfu", dagger, cache_dir=self.cache)
        self.assertEqual(self.downloads, 0)

    def test_checksum_mismatch_installs_nothing(self):
        with patch.object(dfu, "DFU_SHA256", "0" * 64):
            with self.assertRaisesRegex(dfu.DfuError, "checksum"):
                self._install(self.tmp / "dfu")
        self.assertFalse((self.tmp / "dfu").exists())
        self.assertFalse((self.cache / "dfu_linux_64bit-v1.1.1.zip").exists())

    def test_unsafe_archive_entry_leaves_nothing_behind(self):
        self.sha = _release_zip(self.release, {"../escape.txt": b"x"})
        with patch.object(dfu, "DFU_SHA256", self.sha):
            with self.assertRaisesRegex(dfu.DfuError, "unsafe entry"):
                self._install(self.tmp / "games" / "dfu")
        self.assertEqual(list((self.tmp / "games").iterdir()), [])
        self.assertFalse((self.tmp / "games" / "escape.txt").exists())

    def test_symlink_entries_are_refused(self):
        with zipfile.ZipFile(self.release, "a") as zf:
            info = zipfile.ZipInfo("libfoo.so")
            info.external_attr = 0o120777 << 16
            zf.writestr(info, "/etc/passwd")
        with patch.object(dfu, "DFU_SHA256", hashlib.sha256(self.release.read_bytes()).hexdigest()):
            with self.assertRaisesRegex(dfu.DfuError, "unsafe entry: libfoo.so"):
                self._install(self.tmp / "dfu")
        self.assertFalse((self.tmp / "dfu").exists())

    def test_nothing_is_downloaded_while_dfu_runs(self):
        with patch.object(dfu, "is_running", lambda: True):
            with self.assertRaisesRegex(dfu.DfuError, "Close Daggerfall Unity"):
                self._install(self.tmp / "dfu")
        self.assertEqual(self.downloads, 0)
        self.assertFalse((self.tmp / "dfu").exists())

    def test_will_not_repoint_while_dfu_runs(self):
        dest = self.tmp / "dfu"
        self._install(dest)
        with patch.object(dfu, "is_running", lambda: True):
            with self.assertRaisesRegex(dfu.DfuError, "Close Daggerfall Unity"):
                self._install(dest)


class FollowInstall(unittest.TestCase):
    def test_an_enabled_hook_follows_a_moved_install(self):
        inst = dfu.Install(Path("/games/new"))
        on = launchhook.LaunchHookStatus(DAGGERFALL, True, "modsync_1812390_proton", True, None, None, True,
                                         None, False, True, native="/games/old/DaggerfallUnity.x86_64")
        with patch.object(dfu, "hook_status", lambda: on), patch.object(dfu.launchhook, "enable") as enable:
            enable.return_value = "Launch hook refreshed."
            self.assertEqual(dfu.follow_install(inst), "Launch hook refreshed.")
        enable.assert_called_once_with(dfu.DAGGERFALL_APPID, native=inst.executable)

    def test_nothing_to_do_when_off_or_already_there(self):
        inst = dfu.Install(Path("/games/dfu"))
        off = launchhook.LaunchHookStatus(DAGGERFALL, False, None, False, None, None, False, None, False, True)
        same = launchhook.LaunchHookStatus(DAGGERFALL, True, "modsync_1812390_proton", True, None, None, True,
                                           None, False, True, native=str(inst.executable))
        for st in (off, same):
            with patch.object(dfu, "hook_status", lambda st=st: st), patch.object(dfu.launchhook, "enable") as enable:
                self.assertIsNone(dfu.follow_install(inst))
            enable.assert_not_called()


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        p = patch.object(dfu, "display_resolution", lambda: (1280, 800))  # never this machine's xrandr
        p.start()
        self.addCleanup(p.stop)

    def _run(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.dfu(list(args))
        return code, out.getvalue()

    def test_install_without_steams_daggerfall_says_so(self):
        with patch.object(dfu, "steam_daggerfall", lambda: None):
            code, out = self._run("install")
        self.assertEqual(code, 1)
        self.assertIn("Install The Elder Scrolls II: Daggerfall in Steam", out)

    def test_install_remembers_the_folder_for_status_and_hook(self):
        dagger = _dagger(self.tmp / "steam")
        dest = self.tmp / "dfu"

        def fake_install(d, data, *, cache_dir, progress=None, resolution=None):
            inst = dfu.Install(Path(d))
            inst.executable.parent.mkdir(parents=True)
            inst.executable.touch()
            (inst.path / "Portable.txt").touch()
            dfu.write_daggerfall_path(inst.settings_ini, data)
            return dfu.Installed(inst, data, fresh=True)

        with patch.object(dfu, "steam_daggerfall", lambda: dagger), patch.object(dfu, "install", fake_install), \
                patch.object(dfu, "hook_status", _hook_off), patch.object(dfu, "follow_install", lambda inst: None):
            code, out = self._run("install", str(dest))
            self.assertEqual(code, 0, out)
            self.assertIn(f"Installed Daggerfall Unity {dfu.DFU_VERSION} into {dest}", out)
            self.assertEqual(State.load().dfu_path, str(dest))
            self.assertIn("Run 'modsync dfu hook enable'", out)
            code, out = self._run("status")
        self.assertEqual(code, 0, out)
        self.assertIn("Play in Steam:        Off.", out)
        self.assertIn(f"Daggerfall Unity:     {dest}", out)
        self.assertIn(f"Data:               {dagger}\n", out)

    def _fake_install(self, d, data, *, cache_dir, progress=None, resolution=None):
        inst = dfu.Install(Path(d))
        inst.executable.parent.mkdir(parents=True, exist_ok=True)
        inst.executable.touch()
        dfu.write_daggerfall_path(inst.settings_ini, data)
        return dfu.Installed(inst, data, fresh=True)

    def test_install_reports_the_hook_following_it(self):
        dagger = _dagger(self.tmp / "steam")
        with patch.object(dfu, "steam_daggerfall", lambda: dagger), patch.object(dfu, "install", self._fake_install), \
                patch.object(dfu, "follow_install", lambda inst: "Launch hook refreshed. Play starts Daggerfall Unity."):
            code, out = self._run("install", str(self.tmp / "new"))
        self.assertEqual(code, 0, out)
        self.assertIn("Launch hook refreshed.", out)

    def test_status_finds_an_install_in_the_default_folder(self):
        State.update(dfu_path="")
        default = self.tmp / "Games" / "Daggerfall Unity"
        self._fake_install(default, _dagger(self.tmp / "steam"), cache_dir=None)
        with patch.object(dfu, "default_install_dir", lambda: default), patch.object(dfu, "steam_daggerfall", lambda: None), \
                patch.object(dfu, "hook_status", _hook_off):
            code, out = self._run("status")
        self.assertIn(f"Daggerfall Unity:     {default}", out)

    def test_hook_rejects_unknown_arguments(self):
        with patch.object(dfu, "enable_hook") as enable:
            self.assertEqual(self._run("hook", "--enable")[0], 2)
            self.assertEqual(self._run("hook", "enable", "now")[0], 2)
        enable.assert_not_called()

    def test_hook_enable_needs_an_install(self):
        State.update(dfu_path="")
        with patch.object(dfu, "default_install_dir", lambda: self.tmp / "nothing"), \
                patch.object(dfu, "enable_hook") as enable:
            code, out = self._run("hook", "enable")
        self.assertEqual(code, 1)
        self.assertIn("run 'modsync dfu install' first", out)
        enable.assert_not_called()

    def test_hook_enable_uses_the_install_in_use(self):
        dest = self.tmp / "dfu"
        self._fake_install(dest, _dagger(self.tmp / "steam"), cache_dir=None)
        State.update(dfu_path=str(dest))
        with patch.object(dfu, "enable_hook", return_value="Launch hook enabled.") as enable:
            code, out = self._run("hook", "enable")
        self.assertEqual(code, 0, out)
        self.assertEqual(enable.call_args.args[0].path, dest)
        with patch.object(dfu, "disable_hook", side_effect=RuntimeError("Steam was not found")):
            code, out = self._run("hook", "disable")
        self.assertEqual(code, 1)
        self.assertIn("Steam was not found", out)

if __name__ == "__main__":
    unittest.main()


class _ModsFolderBase(unittest.TestCase):
    def setUp(self):
        from modsync.service import ModSyncService
        from tests.test_game_folders import FakeSyncthing

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        p = patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.tmp / "config")})
        p.start()
        self.addCleanup(p.stop)
        self.inst = dfu.Install(self.tmp / "Daggerfall Unity")
        self.inst.executable.parent.mkdir(parents=True)
        self.inst.executable.touch()
        (self.inst.path / dfu.RECORD_NAME).write_text(json.dumps({"version": "1.1.1"}))
        self.st = FakeSyncthing()
        self.service = ModSyncService(manager=self.st)


class ModsFolder(_ModsFolderBase):
    """dfu-mods: what syncs, where it is, and settling its conflicts."""

    def test_the_folder_is_the_install_in_use(self):
        self.assertIsNone(dfu.MODS_FOLDER.path())
        State.update(dfu_path=str(self.inst.path))
        self.assertEqual(dfu.MODS_FOLDER.path(), self.inst.path)
        State.update(dfu_path=str(self.tmp / "gone"))
        self.assertIsNone(dfu.MODS_FOLDER.path())

    def test_the_allow_list(self):
        lines = dfu.MODS_FOLDER.ignore
        allowed = {l[2:] for l in lines if l.startswith("!/")}
        assets = "DaggerfallUnity_Data/StreamingAssets/"
        self.assertIn(assets + "Mods", allowed)
        self.assertIn(assets + "Textures", allowed)
        self.assertIn("PortableAppdata/Mods", allowed)
        self.assertIn(dfu.VAULT_RECORD, allowed)
        for own in ("Quests", "Text", "Tables", "Fonts", "BIOGs", "Factions", "aa", "SpellIcons", "GameFiles"):
            self.assertNotIn(assets + own, allowed)
        self.assertEqual(lines[-1], "/*")

    def test_sharing_records_the_version_the_mods_are_for(self):
        State.update(dfu_path=str(self.inst.path))
        self.service.share_game_folder("dfu-mods")
        self.assertEqual(dfu.vault_version(self.inst), "1.1.1")
        self.assertEqual(self.st.folders["modsync-dfu-mods"]["path"], str(self.inst.path))
        self.assertEqual((self.inst.path / ".stignore").read_text(), dfu.MODS_FOLDER.stignore_text())
        self.assertIsNone(dfu.version_problem(self.inst))
        # A record that came from another machine is never overwritten.
        (self.inst.path / dfu.VAULT_RECORD).write_text(json.dumps({"dfu_version": "1.2.0"}))
        self.service.share_game_folder("dfu-mods")
        self.assertEqual(dfu.vault_version(self.inst), "1.2.0")
        self.assertIn("for Daggerfall Unity 1.2.0, but this machine has 1.1.1", dfu.version_problem(self.inst))

    def _conflict(self):
        data = self.inst.path / "PortableAppdata/Mods/GameData"
        data.mkdir(parents=True)
        (data / "Mods.json").write_text("mine")
        other = data / "Mods.sync-conflict-20261103-120000-ABCDEFG.json"
        other.write_text("theirs")
        (self.inst.path / "DaggerfallUnity_Data/StreamingAssets/Quests").mkdir(parents=True)
        (self.inst.path / "DaggerfallUnity_Data/StreamingAssets/Quests/Q.sync-conflict-20261103-120000-ABCDEFG.txt").touch()
        return data / "Mods.json", other

    def test_conflicts_are_found_only_where_things_sync(self):
        State.update(dfu_path=str(self.inst.path))
        self.service.share_game_folder("dfu-mods")
        current, other = self._conflict()
        found = self.service.game_conflicts()
        self.assertEqual([(spec.key, c.path) for spec, c in found], [("dfu-mods", other)])
        self.assertEqual(self.service.status().game_folders[0].conflicts, 1)

    def test_keeping_one_version_waits_for_dfu_to_close(self):
        State.update(dfu_path=str(self.inst.path))
        self.service.share_game_folder("dfu-mods")
        current, other = self._conflict()
        (spec, c), = self.service.game_conflicts()
        running = dataclasses.replace(spec, in_use=lambda: True)
        with patch("modsync.sync.folders.spec_for", lambda key: running):
            with self.assertRaisesRegex(RuntimeError, "Close the game first"):
                self.service.resolve_game_conflict("dfu-mods", c, "other")
        self.assertEqual(current.read_text(), "mine")
        with patch.object(dfu, "is_running", lambda: False):
            archive = self.service.resolve_game_conflict("dfu-mods", c, "other")
        self.assertEqual(current.read_text(), "theirs")
        self.assertFalse(other.exists())
        self.assertEqual((archive / "PortableAppdata/Mods/GameData/Mods.json").read_text(), "mine")
        self.assertEqual(archive.parent.name, ".modsync-conflicts")

    def test_the_cli_lists_and_settles_them(self):
        from modsync import cli

        State.update(dfu_path=str(self.inst.path))
        self.service.share_game_folder("dfu-mods")
        current, other = self._conflict()
        out = io.StringIO()
        with redirect_stdout(out), patch("modsync.service.ModSyncService", lambda: self.service), \
                patch.object(dfu, "is_running", lambda: False):
            self.assertEqual(cli.vault(["conflicts"]), 0)
            self.assertIn("dfu-mods:PortableAppdata/Mods/GameData/Mods.json", out.getvalue())
            code = cli.vault(["conflicts", "keep", "current", "dfu-mods:PortableAppdata/Mods/GameData/Mods.json"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(current.read_text(), "mine")
        self.assertFalse(other.exists())

    def test_status_says_whether_the_mods_sync(self):
        State.update(dfu_path=str(self.inst.path))
        out = io.StringIO()
        with redirect_stdout(out), patch.object(dfu, "steam_daggerfall", lambda: None), \
                patch.object(dfu, "hook_status", _hook_off):
            cli.dfu(["status"])
            self.assertIn("Mods sync:            off ('modsync sync share dfu-mods'", out.getvalue())
            State.update(game_folders={"dfu-mods": str(self.inst.path)})
            cli.dfu(["status"])
        self.assertIn("Mods sync:            on", out.getvalue())


class ModsFolderReview(_ModsFolderBase):
    """From GPT-6.1 Sol's review of #114."""

    def test_an_older_copy_of_a_mod_never_reaches_dfu(self):
        State.update(dfu_path=str(self.inst.path))
        self.service.share_game_folder("dfu-mods")
        mods = self.inst.mods_dir
        (mods / "Sub").mkdir(parents=True)
        (mods / "Sub/hp.dfmod").write_bytes(b"new")
        copy = mods / "Sub/hp.sync-conflict-20261103-120000-ABCDEFG.dfmod"
        copy.write_bytes(b"old")
        settings = mods / "hp.sync-conflict-20261103-120000-ABCDEFG.json"  # a mod's default settings: DFU skips these
        settings.write_text("{}")
        self.service.status()
        self.assertFalse(copy.exists())
        self.assertEqual((mods / "Sub/hp.dfmod").read_bytes(), b"new")
        aside = self.inst.path / dfu.SET_ASIDE_DIR / copy.relative_to(self.inst.path)
        self.assertEqual(aside.read_bytes(), b"old")
        self.assertTrue(settings.exists())
        self.assertEqual([c.path for _s, c in self.service.game_conflicts()], [settings])
        # Listing conflicts sets them aside too, even before any status was read.
        copy.write_bytes(b"old again")
        other = mods / "Sub/hp.sync-conflict-20261104-120000-ABCDEFG.dfmod"
        other.write_bytes(b"older")
        self.service.game_conflicts()
        self.assertFalse(other.exists())
        self.assertTrue(copy.exists())  # its place in the set-aside folder was taken: left for the user

    def test_records_from_two_first_sharers_warn_both(self):
        (self.inst.path / dfu.VAULT_RECORD).write_text(json.dumps({"dfu_version": "1.1.1"}))
        self.assertIsNone(dfu.version_problem(self.inst))
        (self.inst.path / "modsync-dfu-vault.sync-conflict-20261103-120000-ABCDEFG.json").write_text(
            json.dumps({"dfu_version": "1.2.0"}))
        self.assertEqual(dfu.vault_versions(self.inst), {"1.1.1", "1.2.0"})
        self.assertIn("for Daggerfall Unity 1.2.0, but this machine has 1.1.1", dfu.version_problem(self.inst))

    def test_joining_an_offered_folder_writes_no_record(self):
        State.update(dfu_path=str(self.inst.path))
        self.st.devices["DESKTOP"] = {"deviceID": "DESKTOP"}
        self.st.pending_folders["modsync-dfu-mods"] = {"offeredBy": {"DESKTOP": {}}}
        self.service.share_game_folder("dfu-mods")
        self.assertFalse((self.inst.path / dfu.VAULT_RECORD).exists())
        self.st.pending_folders["modsync-dfu-mods"] = {"offeredBy": {"STRANGER": {}}}
        self.service.stop_game_folder("dfu-mods")
        self.service.share_game_folder("dfu-mods")  # a stranger's offer isn't a shared folder to join
        self.assertTrue((self.inst.path / dfu.VAULT_RECORD).exists())

    def test_moving_dfu_carries_over_what_hadnt_synced(self):
        State.update(dfu_path=str(self.inst.path))
        self.service.share_game_folder("dfu-mods")
        (self.inst.mods_dir).mkdir(parents=True)
        (self.inst.mods_dir / "only-here.dfmod").write_bytes(b"local")
        data = self.inst.path / "PortableAppdata/Mods/GameData"
        data.mkdir(parents=True)
        (data / "Mods.json").write_text("old install")
        (self.inst.path / "PortableAppdata/settings.ini").write_text("old settings")
        (self.inst.path / "DaggerfallUnity_Data/StreamingAssets/Quests").mkdir(parents=True)
        (self.inst.path / "DaggerfallUnity_Data/StreamingAssets/Quests/Q.txt").write_text("old DFU")
        new = dfu.Install(self.tmp / "New DFU")
        new.executable.parent.mkdir(parents=True)
        new.executable.touch()
        (new.path / "PortableAppdata/Mods/GameData").mkdir(parents=True)
        (new.path / "PortableAppdata/Mods/GameData/Mods.json").write_text("new install")
        State.update(dfu_path=str(new.path))
        self.service._refresh_game_folders(force=True)
        self.assertEqual(self.st.folders["modsync-dfu-mods"]["path"], str(new.path))
        self.assertEqual((new.mods_dir / "only-here.dfmod").read_bytes(), b"local")
        self.assertEqual((new.path / "PortableAppdata/Mods/GameData/Mods.json").read_text(), "new install")
        self.assertEqual(dfu.vault_version(new), "1.1.1")  # the record moved with the mods
        self.assertFalse((new.path / "PortableAppdata/settings.ini").exists())  # never synced, never carried
        self.assertFalse((new.path / "DaggerfallUnity_Data/StreamingAssets/Quests/Q.txt").exists())
        self.assertTrue((self.inst.mods_dir / "only-here.dfmod").exists())  # the old install is left alone
