import struct
import tempfile
import unittest
from pathlib import Path

from modsync.steam import appinfo, vdf
from modsync.steam.appmanifest import AppManifest

REAL_APPINFO = Path.home() / ".local/share/Steam/appcache/appinfo.vdf"

ACF = '''"AppState"
{
\t"appid"\t\t"489830"
\t"universe"\t\t"1"
\t"name"\t\t"The Elder Scrolls V: Skyrim Special Edition"
\t"StateFlags"\t\t"6"
\t"installdir"\t\t"Skyrim Special Edition"
\t"buildid"\t\t"13189953"
\t"BytesToDownload"\t\t"1658888697"
\t"BytesDownloaded"\t\t"0"
\t"TargetBuildID"\t\t"24604991"
\t"AutoUpdateBehavior"\t\t"1"
\t"ScheduledAutoUpdate"\t\t"1787389852"
\t"InstalledDepots"
\t{
\t\t"489831"
\t\t{
\t\t\t"manifest"\t\t"8442952117333549665"
\t\t\t"size"\t\t"7492390909"
\t\t}
\t\t"489833"
\t\t{
\t\t\t"manifest"\t\t"1914580699073641964"
\t\t\t"size"\t\t"37157144"
\t\t}
\t}
\t"UserConfig"
\t{
\t\t"language"\t\t"german"
\t}
}
'''


def _bkv_str(s: str) -> bytes:
    return s.encode() + b"\0"


def build_appinfo_v29(appid: int, kv: dict) -> bytes:
    """Encode a minimal v29 appinfo.vdf with one app (keys via string table)."""
    strings: list[str] = []

    def key_idx(k: str) -> int:
        if k not in strings:
            strings.append(k)
        return strings.index(k)

    def enc(d: dict) -> bytes:
        out = b""
        for k, v in d.items():
            if isinstance(v, dict):
                out += b"\x00" + struct.pack("<I", key_idx(k)) + enc(v)
            elif isinstance(v, int):
                out += b"\x02" + struct.pack("<I", key_idx(k)) + struct.pack("<i", v)
            else:
                out += b"\x01" + struct.pack("<I", key_idx(k)) + _bkv_str(str(v))
        return out + b"\x08"

    blob = enc({"appinfo": kv})
    entry_hdr = struct.pack("<IIQ", 0, 0, 0) + b"\0" * 20 + struct.pack("<I", 1) + b"\0" * 20
    entry = struct.pack("<II", appid, len(entry_hdr) + len(blob)) + entry_hdr + blob
    body = entry + struct.pack("<I", 0)
    header_len = 4 + 4 + 8
    strtab_off = header_len + len(body)
    strtab = struct.pack("<I", len(strings)) + b"".join(_bkv_str(s) for s in strings)
    return struct.pack("<IIq", appinfo.MAGIC_V29, 1, strtab_off) + body + strtab


FAKE_APP = {
    "appid": 489830,
    "depots": {
        "489831": {"manifests": {"public": {"gid": "4940892828028256588", "size": "7490529657", "download": "5102624736"}}},
        "489833": {"manifests": {"public": {"gid": "4886117324142477814", "size": 37910440, "download": 13605104}}},
        "489836": {"config": {"language": "german"}, "manifests": {"public": {"gid": "2757270758137158257"}}},
        "branches": {"public": {"buildid": 24914197, "timeupdated": 1787839209}},
    },
}


class VdfDumpTests(unittest.TestCase):
    def test_round_trip_preserves_structure_and_order(self):
        data = vdf.loads(ACF)
        text = vdf.dumps(data) + "\n"
        self.assertEqual(vdf.loads(text), data)
        self.assertTrue(text.startswith('"AppState"\n{\n\t"appid"\t\t"489830"'))
        self.assertIn('\t"InstalledDepots"\n\t{\n\t\t"489831"\n\t\t{\n\t\t\t"manifest"', text)

    def test_escapes(self):
        data = {"k": 'a "quoted" \\ value'}
        self.assertEqual(vdf.loads(vdf.dumps(data)), data)


class AppInfoTests(unittest.TestCase):
    def test_parse_synthetic_v29(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "appinfo.vdf"
            p.write_bytes(build_appinfo_v29(489830, FAKE_APP))
            info = appinfo.read_app(p, 489830)
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info.public_buildid, 24914197)
            self.assertEqual(info.depots[489831].manifest_gid, "4940892828028256588")
            self.assertEqual(info.depots[489831].size, 7490529657)
            self.assertEqual(info.depots[489833].size, 37910440)
            self.assertEqual(info.depots[489836].language, "german")
            self.assertIsNone(info.depots[489831].language)
            self.assertIsNone(appinfo.read_app(p, 12345))

    def test_bad_magic(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "appinfo.vdf"
            p.write_bytes(b"\x01\x02\x03\x04" * 4)
            with self.assertRaises(appinfo.AppInfoError):
                appinfo.read_app(p, 1)

    @unittest.skipUnless(REAL_APPINFO.exists(), "no local Steam client cache")
    def test_real_cache_has_skyrim(self):
        info = appinfo.read_app(REAL_APPINFO, 489830)
        if info is None:
            self.skipTest("Skyrim SE not in this client's cache")
        self.assertIsNotNone(info.public_buildid)
        self.assertIn(489833, info.depots)
        self.assertEqual(info.depots[489836].language, "german")


class AppManifestTests(unittest.TestCase):
    def _manifest(self, tmp: str) -> AppManifest:
        p = Path(tmp) / "appmanifest_489830.acf"
        p.write_text(ACF)
        return AppManifest.load(p)

    def _info(self) -> appinfo.AppInfo:
        return appinfo._to_appinfo(489830, FAKE_APP)

    def test_read_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = self._manifest(tmp)
            self.assertEqual(m.appid, 489830)
            self.assertEqual(m.buildid, 13189953)
            self.assertEqual(m.state_flags, 6)
            self.assertEqual(m.language, "german")
            self.assertEqual(m.installed_depots, {489831: "8442952117333549665", 489833: "1914580699073641964"})
            self.assertFalse(m.is_current(self._info()))

    def test_pin_rewrites_update_fields_and_saves_in_steam_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = self._manifest(tmp)
            changes = m.pin_to(self._info())
            fields = {c.field for c in changes}
            self.assertIn("StateFlags", fields)
            self.assertIn("buildid", fields)
            self.assertIn("InstalledDepots/489831/manifest", fields)
            self.assertIn("InstalledDepots/489833/size", fields)
            self.assertTrue(m.is_current(self._info()))
            m.save()

            again = AppManifest.load(m.path)
            self.assertEqual(again.state_flags, 4)
            self.assertEqual(again.buildid, 24914197)
            self.assertEqual(again.state["TargetBuildID"], "0")
            self.assertEqual(again.state["ScheduledAutoUpdate"], "0")
            self.assertEqual(again.state["AutoUpdateBehavior"], "1")  # untouched
            self.assertEqual(again.installed_depots[489831], "4940892828028256588")
            self.assertEqual(again.state["InstalledDepots"]["489833"]["size"], "37910440")
            self.assertEqual(again.language, "german")
            # second pin is a no-op
            self.assertEqual(again.pin_to(self._info()), [])
            text = m.path.read_text()
            self.assertTrue(text.startswith('"AppState"\n{\n'))
            self.assertFalse(list(Path(tmp).glob("*.modsync-tmp")))


if __name__ == "__main__":
    unittest.main()
