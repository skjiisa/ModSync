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


if __name__ == "__main__":
    unittest.main()
