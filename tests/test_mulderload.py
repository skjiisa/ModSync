import json
import unittest
from pathlib import Path

from modsync.downgrade import mulderload

# A trimmed, synthetic recipe in the same shape as MulderLoad's Skyrim SE
# steam-downgrader.nsi (sections per target version, language if-blocks,
# DOWNLOAD_RANGE for split archives, DOWNLOAD_1 for single files).
NSI = r'''
!include "..\..\includes\tools\XDelta3.nsh"
Name "Skyrim Special Edition [Steam Downgrader]"
Var /GLOBAL Game_Language

Function OnSelectedFile
    !insertmacro FILE_HASH_EQUALS "$INSTDIR\SkyrimSE.exe" "2F784A183F884067A9A41338664B55F6DC198A48" $R0
     ${If} "$R0" == "1"
        MessageBox MB_OK "Correct game version detected!$\r$\nGame version: v1.7.104 (August 27, 2026)$\r$\n$\r$\nYou may proceed."
    ${EndIf}
    StrCpy $Game_Language "English"
    ${If} $Game_Language == "English"
        MessageBox MB_YESNO "Is your game in Chinese (Traditional)?" IDNO +2
    ${EndIf}
FunctionEnd

SectionGroup /e "Downgrade Steam version (v1.7.104) to" version
    Section /o "v1.5.97 (November 2019)" version_1_5_97
        AddSize 3000000
        SetOutPath "$INSTDIR"
        !insertmacro DOWNLOAD_RANGE "https://cdn.example/1.7.104_to_1.5.97/489831.7z.001" "489831.7z.001" "9212ac317ee76c1e4d994a2704a23436431613ca" 3
        !insertmacro NSIS7Z_EXTRACT "489831.7z.001" ".\" ""
        !insertmacro DOWNLOAD_1 "https://cdn.example/1.7.104_to_1.5.97/489833.7z" "489833.7z" "9ffed35a7c9356a8697c576c1abb5e9365a05fd3"
        ${If} $Game_Language == "French"
            !insertmacro DOWNLOAD_RANGE "https://cdn.example/1.7.99_to_1.5.97/489834.7z.001" "489834.7z.001" "e642aea4240a3b06b0dfc960e67130d1d31dd39f" 2
        ${ElseIf} $Game_Language == "Chinese (Traditional)"
            !insertmacro DOWNLOAD_1 "https://cdn.example/1.7.99_to_1.5.97/544860.7z" "544860.7z" "d561af144e244bddcda57fd764494cb42508420b"
        ${EndIf}
    SectionEnd
    Section "v1.6.1170 (January 2024)" version_1_6_1170
        AddSize 1132462
        SetOutPath "$INSTDIR"
        !insertmacro DOWNLOAD_RANGE "https://cdn.example/1.7.104_to_1.6.1170/489831.7z.001" "489831.7z.001" "0adfa48883116c088f172f51c34f56070ffcdf80" 2
        !insertmacro DOWNLOAD_1 "https://cdn.example/1.7.104_to_1.6.1170/489832.7z" "489832.7z" "c2e1c9e57ad823a59477f942c86575388a13dbff"
        !insertmacro DOWNLOAD_1 "https://cdn.example/1.7.104_to_1.6.1170/489833.7z" "489833.7z" "486c9d908b8ab444e28d9d577f6fbde61d4992e9"
    SectionEnd
    Section "" version_common
        # Common for all downgrades
        RMDIR /r "$INSTDIR\Data\ShaderCache"
        !insertmacro XDELTA3_GET
        !insertmacro XDELTA3_PATCH_FOLDER "$INSTDIR"
    SectionEnd
SectionGroupEnd
Section /o "Block future Steam update"
    SetFileAttributes "appmanifest_489830.acf" READONLY
SectionEnd
'''

STATIC = Path(__file__).resolve().parent.parent / "modsync/downgrade/recipes/skyrim-se.static.json"
INDEX = STATIC.with_name("skyrim-se.json")


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.recipe = mulderload.parse(NSI)

    def test_source_version_and_sha(self):
        self.assertEqual(self.recipe.source_version, "1.7.104")
        self.assertEqual(self.recipe.source_exe, "SkyrimSE.exe")
        self.assertEqual(self.recipe.source_exe_sha1, "2f784a183f884067a9a41338664b55f6dc198a48")

    def test_targets_and_sizes(self):
        self.assertEqual(sorted(self.recipe.targets), ["1.5.97", "1.6.1170"])
        self.assertEqual(self.recipe.targets["1.6.1170"].estimated_kib, 1132462)
        self.assertEqual(self.recipe.targets["1.5.97"].title, "v1.5.97 (November 2019)")

    def test_split_archive_expands_parts_with_sha_on_first_only(self):
        d = self.recipe.targets["1.6.1170"].depots["489831"]
        self.assertEqual(d.archive, "489831.7z.001")
        self.assertEqual([p.url[-4:] for p in d.parts], [".001", ".002"])
        self.assertEqual(d.parts[0].sha1, "0adfa48883116c088f172f51c34f56070ffcdf80")
        self.assertIsNone(d.parts[1].sha1)
        self.assertIsNone(d.language)

    def test_single_archive(self):
        d = self.recipe.targets["1.6.1170"].depots["489833"]
        self.assertEqual(len(d.parts), 1)
        self.assertEqual(d.parts[0].sha1, "486c9d908b8ab444e28d9d577f6fbde61d4992e9")

    def test_language_depots(self):
        t = self.recipe.targets["1.5.97"]
        self.assertEqual(t.depots["489834"].language, "french")
        self.assertEqual(len(t.depots["489834"].parts), 2)
        self.assertEqual(t.depots["544860"].language, "tchinese")
        self.assertIsNone(t.depots["489831"].language)
        self.assertIsNone(t.depots["489833"].language)

    def test_common_and_block_sections_ignored(self):
        self.assertNotIn("common", self.recipe.targets)
        for t in self.recipe.targets.values():
            self.assertTrue(all(k.isdigit() for k in t.depots))

    def test_errors(self):
        with self.assertRaises(mulderload.RecipeParseError):
            mulderload.parse("Section \"x\" version_1_2_3\nSectionEnd\n")
        bad = NSI.replace('$Game_Language == "French"', '$Game_Language == "Klingon"')
        with self.assertRaises(mulderload.RecipeParseError):
            mulderload.parse(bad)

    def test_build_index_shape(self):
        static = json.loads(STATIC.read_text())
        idx = mulderload.build_index(self.recipe, static, source_commit="abc123", generated_at="2026-09-14T00:00:00+00:00")
        self.assertEqual(idx["schema"], 1)
        self.assertEqual(idx["game"]["appid"], 489830)
        self.assertEqual(idx["from"]["version"], "1.7.104")
        self.assertIn("1.6.1170", idx["targets"])
        self.assertEqual(idx["source"]["commit"], "abc123")
        self.assertIn("abc123", idx["source"]["url"])
        self.assertEqual(idx["steam"]["depot_languages"]["489836"], "german")
        self.assertEqual(idx["steam"]["manifests"]["1.6.1170"]["489833"], "1914580699073641964")
        json.dumps(idx)  # must be serialisable


@unittest.skipUnless(INDEX.exists(), "generated index not present")
class GeneratedIndexTests(unittest.TestCase):
    """Sanity checks on the committed index — this is what the workflow runs."""

    def setUp(self):
        self.idx = json.loads(INDEX.read_text(encoding="utf-8"))

    def test_basic_shape(self):
        self.assertEqual(self.idx["schema"], 1)
        self.assertEqual(self.idx["game"]["appid"], 489830)
        self.assertRegex(self.idx["from"]["version"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(self.idx["from"]["exe_sha1"], r"^[0-9a-f]{40}$")
        self.assertTrue(self.idx["targets"])

    def test_every_target_has_base_depots_with_hashes(self):
        for version, target in self.idx["targets"].items():
            for depot in ("489831", "489832", "489833"):
                self.assertIn(depot, target["depots"], f"{version} lacks depot {depot}")
                first = target["depots"][depot]["parts"][0]
                self.assertRegex(first["sha1"] or "", r"^[0-9a-f]{40}$", f"{version}/{depot}")
                self.assertTrue(first["url"].startswith("https://"))

    def test_language_depots_match_static_table(self):
        langs = self.idx["steam"]["depot_languages"]
        for target in self.idx["targets"].values():
            for depot, entry in target["depots"].items():
                if entry["language"] is not None:
                    self.assertEqual(langs.get(depot), entry["language"], depot)


if __name__ == "__main__":
    unittest.main()
