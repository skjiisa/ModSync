"""What `modsync doctor` says about Syncthing (modsync.sync.health)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from modsync.sync import health, pairing

# A throwaway identity from `syncthing generate`; `syncthing device-id` gave
# the id below for it.
CERT = """-----BEGIN CERTIFICATE-----
MIIBnzCCAVGgAwIBAgIIGfrvecrLvCQwBQYDK2VwMEoxEjAQBgNVBAoTCVN5bmN0
aGluZzEgMB4GA1UECxMXQXV0b21hdGljYWxseSBHZW5lcmF0ZWQxEjAQBgNVBAMT
CXN5bmN0aGluZzAeFw0yNjExMDgwMDAwMDBaFw00NjExMDMwMDAwMDBaMEoxEjAQ
BgNVBAoTCVN5bmN0aGluZzEgMB4GA1UECxMXQXV0b21hdGljYWxseSBHZW5lcmF0
ZWQxEjAQBgNVBAMTCXN5bmN0aGluZzAqMAUGAytlcAMhALzAqTtusRb643kVcjrl
4drOKHZSLAvCnXBeRkEc07NKo1UwUzAOBgNVHQ8BAf8EBAMCBaAwHQYDVR0lBBYw
FAYIKwYBBQUHAwEGCCsGAQUFBwMCMAwGA1UdEwEB/wQCMAAwFAYDVR0RBA0wC4IJ
c3luY3RoaW5nMAUGAytlcANBAAmNQrb5Ff6aPmKou657efZRtwDp/HMmG3OYqVi8
+ia2uc99bP3puBs+TlZAmjjiF+zzfq69h5L1L4sYhzQeug8=
-----END CERTIFICATE-----
"""
CERT_ID = "35PLNT7-XF7SG4F-FKPZD3R-MNU347Z-NOVRFZZ-PRVHJLQ-OZTBTYE-P463YQP"

ME, DECK, OLD = "MEMEMEM-1", "DECKDEC-2", "OLDOLDO-3"
NOW = datetime(2026, 11, 7, 23, 0, tzinfo=timezone.utc)


def folder(fid, path, *devices, label=""):
    return {"id": fid, "label": label, "path": path, "devices": [{"deviceID": d} for d in (ME, *devices)]}


def snap(folders, *, running=True, connected=(), seen=None, seen_known=None):
    devices = [{"deviceID": ME, "name": "desktop"}, {"deviceID": DECK, "name": "steamdeck"},
               {"deviceID": OLD, "name": "modorganizer2"}]
    return health.Snapshot(me=ME, devices=devices, folders=folders, running=running,
                           connected=set(connected), last_seen=seen or {},
                           seen_known=running if seen_known is None else seen_known)


class DeviceIdTests(unittest.TestCase):
    def test_matches_syncthing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "cert.pem"
            cert.write_text(CERT)
            self.assertEqual(health.device_id_of(cert), CERT_ID)

    def test_unreadable_cert(self):
        self.assertEqual(health.device_id_of(Path("/nonexistent/cert.pem")), "")


class SeenAtTests(unittest.TestCase):
    def test_never(self):
        self.assertIsNone(health.seen_at({"lastSeen": "1970-01-01T00:00:00Z"}))
        self.assertIsNone(health.seen_at({"lastSeen": "0001-01-01T00:00:00Z"}))
        self.assertIsNone(health.seen_at({}))

    def test_nanoseconds_and_offset(self):
        when = health.seen_at({"lastSeen": "2026-11-07T22:30:06.123456789-05:00"})
        self.assertEqual(when, datetime(2026, 11, 8, 3, 30, 6, 123456, tzinfo=timezone.utc))


class LinesTests(unittest.TestCase):
    def test_the_desktop_on_2026_11_07(self):
        """Two vault folders at one path, the one in use shared only with a
        machine gone for weeks."""
        s = snap([folder("modsync-new", "/home/e/MO2", DECK), folder("modsync-old", "/home/e/MO2/", OLD)],
                 connected=[DECK], seen={OLD: NOW - timedelta(days=40)})
        out = health.lines(s, "modsync-old", {}, now=NOW)
        text = "\n".join(out)
        self.assertIn("• paired: steamdeck (DECKDEC…), connected", out)
        self.assertTrue(any(line.startswith("• paired: modorganizer2 (OLDOLDO…), last seen ") and "(40 days ago)" in line
                            for line in out), out)
        self.assertIn("! Syncthing has 2 folders at /home/e/MO2: modsync-new, modsync-old (the vault)", text)
        self.assertIn("! no machine that shares the vault has connected in 30 days: modorganizer2 (OLDOLDO…)", text)

    def test_a_healthy_setup_has_no_problems(self):
        s = snap([folder("modsync-v", "/home/e/MO2", DECK), folder("modsync-dfu-mods", "/home/e/DFU", DECK)],
                 connected=[DECK], seen={OLD: NOW - timedelta(days=2)})
        out = health.lines(s, "modsync-v", {"dfu-mods": "modsync-dfu-mods"}, now=NOW)
        self.assertFalse([line for line in out if line.startswith("!")], out)
        self.assertIn("• paired: modorganizer2 (OLDOLDO…), last seen "
                      f"{(NOW - timedelta(days=2)).astimezone().date().isoformat()} (2 days ago)", out)

    def test_machines_passing_on_others_and_forgotten_ones(self):
        s = snap([folder("modsync-v", "/m", DECK)], connected=[DECK])
        s.devices[1]["introducer"] = True
        out = health.lines(s, "modsync-v", {}, now=NOW, forgotten=["2LFPXRM-RKWI4S2"])
        self.assertIn("• paired: steamdeck (DECKDEC…), connected, passes on the machines it knows", out)
        self.assertIn("• forgotten here: 2LFPXRM…", out)

    def test_never_connected(self):
        out = health.lines(snap([folder("modsync-v", "/m", DECK)], connected=[DECK]), "modsync-v", {}, now=NOW)
        self.assertIn("• paired: modorganizer2 (OLDOLDO…), never connected", out)

    def test_a_folder_modsync_uses_is_missing(self):
        out = health.lines(snap([], connected=[DECK]), "modsync-v", {"dfu-saves": "modsync-dfu-saves"}, now=NOW)
        self.assertIn("! the vault (modsync-v) isn't in Syncthing, so it doesn't sync", out)
        self.assertIn("! the dfu-saves folder (modsync-dfu-saves) isn't in Syncthing, so it doesn't sync", out)

    def test_a_new_vault_with_no_other_machine_is_fine(self):
        out = health.lines(snap([folder("modsync-v", "/m")]), "modsync-v", {}, now=NOW)
        self.assertFalse([line for line in out if line.startswith("!")], out)

    def test_without_last_seen_times_nothing_is_concluded_from_them(self):
        # /rest/stats/device failed: a machine that synced yesterday isn't
        # "never connected", and the vault isn't stale.
        s = snap([folder("modsync-v", "/m", OLD)], seen_known=False)
        out = health.lines(s, "modsync-v", {}, now=NOW)
        self.assertIn("• paired: modorganizer2 (OLDOLDO…)", out)
        self.assertIn("• Syncthing didn't say when each machine was last seen", out)
        self.assertFalse([line for line in out if line.startswith("!")], out)

    def test_two_folders_at_one_place_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            (Path(tmp) / "alias").symlink_to(real)
            s = snap([folder("modsync-a", str(real)), folder("modsync-b", str(Path(tmp) / "alias"))])
            out = health.lines(s, None, {}, now=NOW)
        self.assertTrue(any(line.startswith("! Syncthing has 2 folders at ") for line in out), out)

    def test_without_a_running_syncthing_last_seen_is_left_out(self):
        s = snap([folder("modsync-v", "/m", OLD)], running=False)
        out = health.lines(s, "modsync-v", {}, now=NOW)
        self.assertIn("• paired: modorganizer2 (OLDOLDO…)", out)
        self.assertIn("• Syncthing isn't running, so when each machine was last seen isn't shown", out)
        self.assertFalse([line for line in out if line.startswith("!")], out)


class FromConfigTests(unittest.TestCase):
    def test_reads_config_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "cert.pem").write_text(CERT)
            (home / "config.xml").write_text(f"""<configuration version="51">
  <folder id="modsync-v" label="Skyrim SE" path="/home/e/MO2" type="sendreceive">
    <device id="{CERT_ID}" introducedBy=""></device>
    <device id="{DECK}" introducedBy=""></device>
  </folder>
  <device id="{CERT_ID}" name="desktop" introducer="false"></device>
  <device id="{DECK}" name="steamdeck" introducer="true"></device>
  <gui enabled="true"><address>127.0.0.1:1</address><apikey>k</apikey></gui>
</configuration>""")
            s = health.snapshot(home)  # nothing serves 127.0.0.1:1
        self.assertFalse(s.running)
        self.assertEqual(s.me, CERT_ID)
        self.assertEqual([d["deviceID"] for d in s.folders[0]["devices"]], [CERT_ID, DECK])
        self.assertEqual(health.lines(s, "modsync-v", {})[1], "• paired: steamdeck (DECKDEC…), passes on the machines it knows")


class StandInNamesTests(unittest.TestCase):
    class Client:
        def __init__(self, devices, folders, connected=()):
            self.devs = {d["deviceID"]: d for d in devices}
            self.flds = folders
            self.connected = set(connected)
            self.writes = []

        def my_id(self):
            return ME

        def devices(self):
            return [dict(d) for d in self.devs.values()]

        def folders(self):
            return self.flds

        def connections(self):
            return {"connections": {d: {"connected": True} for d in self.connected}}

        def put_device(self, device):
            self.writes.append((device["deviceID"], device.get("name"), device.get("paused")))
            self.devs[device["deviceID"]] = dict(device)

    def test_only_made_up_names_are_cleared(self):
        client = self.Client(
            [{"deviceID": ME, "name": "desktop"},
             {"deviceID": "DECK", "name": "Skyrim SE"},  # the vault's label, from a pairing code
             {"deviceID": "LAPTOP", "name": "desktop"},  # named after the LAN host itself
             {"deviceID": "FRAME", "name": "ModSync device"},
             {"deviceID": "OLD", "name": "modorganizer2"},  # the old vault's label, from state
             {"deviceID": "MINE", "name": "Living-room Deck"}],  # named by the user: kept
            [{"id": "modsync-v", "label": "Skyrim SE"}],
            connected=["DECK"])
        cleared = pairing.clear_stand_in_names(client, ["modorganizer2"])
        self.assertEqual(cleared, ["DECK", "LAPTOP", "FRAME", "OLD"])
        self.assertEqual({d: client.devs[d]["name"] for d in client.devs},
                         {ME: "desktop", "DECK": "", "LAPTOP": "", "FRAME": "", "OLD": "", "MINE": "Living-room Deck"})
        # A connected one reconnects, so Syncthing fills in its name now.
        self.assertEqual(client.writes[:2], [("DECK", "", True), ("DECK", "", None)])


class ReportTests(unittest.TestCase):
    def _report(self, state):
        from modsync import config, report

        lines = []
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "syncthing_home", return_value=Path(tmp)):
            home = Path(tmp)
            self.home = home
            if self.config_xml is not None:
                (home / "config.xml").write_text(self.config_xml)
            report._syncthing_section(lambda text="": lines.append(text), state)
        return lines

    def test_a_vault_syncthing_has_lost_is_reported(self):
        from modsync.state import State

        self.config_xml = """<configuration><device id="X" name="me"></device>
  <gui><address>127.0.0.1:1</address><apikey>k</apikey></gui></configuration>"""
        out = self._report(State(instance_path="/m", folder_id="modsync-v"))
        self.assertIn("  ! the vault (modsync-v) isn't in Syncthing, so it doesn't sync", out)

    def test_missing_settings_are_reported_when_something_should_sync(self):
        from modsync.state import State

        self.config_xml = None
        out = self._report(State(instance_path="/m", folder_id="modsync-v"))
        self.assertTrue(any("missing or can't be read" in line for line in out), out)
        self.assertEqual(self._report(State()), [])


if __name__ == "__main__":
    unittest.main()
