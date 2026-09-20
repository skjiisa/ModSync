import tempfile
import unittest
from pathlib import Path

from modsync.steam import libraries as libs
from modsync.steam import vdf

LIBVDF = """
"libraryfolders"
{
\t"0"
\t{
\t\t"path"\t\t"__ROOT__"
\t\t"mounted"\t\t"1"
\t\t"apps"
\t\t{
\t\t\t"489830"\t\t"16095731388"
\t\t\t"730"\t\t"100"
\t\t}
\t}
\t"1"
\t{
\t\t"path"\t\t"__UNMOUNTED__"
\t\t"mounted"\t\t"0"
\t\t"apps" { "111" "1" }
\t}
}
"""

ACF = """
"AppState"
{
\t"appid"\t\t"489830"
\t"name"\t\t"The Elder Scrolls V: Skyrim Special Edition"
\t"installdir"\t\t"Skyrim Special Edition"
}
"""


class VdfTests(unittest.TestCase):
    def test_nested_parse(self):
        data = vdf.loads(LIBVDF)
        lf = data["libraryfolders"]
        self.assertEqual(lf["0"]["path"], "__ROOT__")
        self.assertEqual(lf["0"]["apps"]["489830"], "16095731388")
        self.assertEqual(lf["1"]["mounted"], "0")

    def test_spaces_and_parens_in_value(self):
        data = vdf.loads('"k" { "path" "/x/pfx (last camp)/y" }')
        self.assertEqual(data["k"]["path"], "/x/pfx (last camp)/y")

    def test_comment_skipped(self):
        data = vdf.loads('// a comment\n"k" "v"')
        self.assertEqual(data["k"], "v")


class LibrariesTests(unittest.TestCase):
    def test_read_skips_unmounted_and_finds_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "steamroot"
            (root / "steamapps").mkdir(parents=True)
            (root / "steamapps" / "common" / "Skyrim Special Edition").mkdir(parents=True)
            libvdf = LIBVDF.replace("__ROOT__", str(root)).replace(
                "__UNMOUNTED__", str(Path(tmp) / "unmounted")
            )
            (root / "steamapps" / "libraryfolders.vdf").write_text(libvdf)
            (root / "steamapps" / "appmanifest_489830.acf").write_text(ACF)

            libraries = libs.read_libraries(root)
            self.assertEqual(len(libraries), 1, "unmounted library should be skipped")
            self.assertEqual(libraries[0].path, root.resolve())
            self.assertIn(489830, libraries[0].app_ids)

            app = libs.find_app(libraries, 489830)
            self.assertIsNotNone(app)
            assert app is not None
            self.assertEqual(app.installdir, "Skyrim Special Edition")
            self.assertTrue(app.install_path.is_dir())

            self.assertIsNone(libs.find_app(libraries, 999999))



class RemovableLibraryTests(unittest.TestCase):
    """Skyrim on an SD-card library, plus a library whose path is gone (unplugged)."""

    def _layout(self, tmp: str) -> tuple[Path, Path, Path]:
        root = Path(tmp) / "steamroot"
        sdcard = Path(tmp) / "run/media/deck/SDCARD"
        unplugged = Path(tmp) / "run/media/deck/UNPLUGGED"  # never created
        (root / "steamapps").mkdir(parents=True)
        (sdcard / "steamapps" / "common" / "Skyrim Special Edition").mkdir(parents=True)
        (sdcard / "steamapps" / "appmanifest_489830.acf").write_text(ACF)
        (root / "steamapps" / "libraryfolders.vdf").write_text(
            vdf.dumps({"libraryfolders": {
                "0": {"path": str(root), "apps": {"730": "1"}},
                "1": {"path": str(sdcard), "apps": {"489830": "1"}},
                "2": {"path": str(unplugged), "apps": {"1091500": "1"}},
            }})
        )
        return root, sdcard, unplugged

    def test_game_in_second_library_on_removable_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, sdcard, unplugged = self._layout(tmp)
            libraries = libs.read_libraries(root)
            self.assertEqual([l.path for l in libraries], [root.resolve(), sdcard.resolve(), unplugged])
            app = libs.find_app(libraries, 489830)
            assert app is not None
            self.assertEqual(app.library.path, sdcard.resolve())
            self.assertTrue(app.install_path.is_dir())

    def test_unplugged_library_is_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, unplugged = self._layout(tmp)
            gone = next(l for l in libs.read_libraries(root) if l.path == unplugged)
            self.assertIn(1091500, gone.app_ids)  # still indexed by Steam ...
            self.assertEqual(list(libs.iter_apps(gone)), [])  # ... but nothing readable there
            self.assertIsNone(libs.find_app([gone], 1091500))
            # The same library listed by two roots is reported once.
            self.assertEqual(len(libs.all_libraries([root, root])), 3)

if __name__ == "__main__":
    unittest.main()
