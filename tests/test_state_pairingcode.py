import os
import tempfile
import unittest

from modsync.pairing_code import PairingCode
from modsync.state import State


class PairingCodeTests(unittest.TestCase):
    def test_round_trip(self):
        code = PairingCode("ABCDEF1-GHIJKL2", "modsync-1234abcd", "Skyrim SE")
        self.assertEqual(PairingCode.decode(code.encode()), code)

    def test_decode_tolerates_prefix_and_whitespace(self):
        code = PairingCode("XID", "fid", "")
        self.assertEqual(PairingCode.decode(f"  {code.encode()}\n").folder_id, "fid")

    def test_sequence_round_trips_and_older_codes_still_decode(self):
        code = PairingCode("XID", "fid", "Deck", 6031)
        self.assertEqual(PairingCode.decode(code.encode()), code)
        old = PairingCode("XID", "fid", "Deck").encode()  # what 1.1.0-beta1 makes
        self.assertEqual(PairingCode.decode(old).sequence, 0)
        self.assertEqual(old, PairingCode("XID", "fid", "Deck", 0).encode())

    def test_lan_payload_carries_the_sequence_and_tolerates_its_absence(self):
        from modsync.pairing_lan import PairPayload

        payload = PairPayload("D", "f", "Deck", sequence=12)
        self.assertEqual(PairPayload.from_bytes(payload.to_bytes()).sequence, 12)
        self.assertEqual(PairPayload.from_bytes(b'{"device_id":"D"}').sequence, 0)

    def test_encode_has_readable_prefix(self):
        self.assertTrue(PairingCode("d", "f", "l").encode().startswith("MODSYNC1-"))


class StateUpdateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["XDG_CONFIG_HOME"] = tmp.name
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)

    def test_update_never_writes_back_stale_fields(self):
        """Two processes: one finishes a copy, the other, holding the old
        state, pauses. The copy must stay finished."""
        State(instance_path="/mo2", folder_id="v", copy_phase="receiving", copy_source="SRC").save()
        stale = State.load()
        State.update(copy_phase="", copy_source="", set_aside="/mo2/.modsync-before-join/x")
        stale.sync_paused = True
        merged = State.update(sync_paused=True)
        self.assertEqual((merged.copy_phase, merged.copy_source), ("", ""))
        self.assertEqual(merged.set_aside, "/mo2/.modsync-before-join/x")
        self.assertTrue(State.load().sync_paused)

    def test_update_rejects_unknown_fields_and_leaves_no_temp_files(self):
        State(instance_path="/mo2").save()
        with self.assertRaises(TypeError):
            State.update(nonsense=1)
        State.update(folder_id="v")
        self.assertEqual(sorted(p.name for p in State.path().parent.iterdir()), ["state.json", "state.json.lock"])


class StateTests(unittest.TestCase):
    def test_save_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_CONFIG_HOME"] = tmp
            try:
                State(
                    instance_path="/games/MO2",
                    folder_id="modsync-9",
                    instance_label="Skyrim SE",
                ).save()
                loaded = State.load()
                self.assertTrue(loaded.configured)
                self.assertEqual(loaded.instance_path, "/games/MO2")
                self.assertEqual(loaded.folder_id, "modsync-9")
                self.assertEqual(loaded.instance_label, "Skyrim SE")
            finally:
                os.environ.pop("XDG_CONFIG_HOME", None)

    def test_default_is_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_CONFIG_HOME"] = tmp
            try:
                self.assertFalse(State.load().configured)
            finally:
                os.environ.pop("XDG_CONFIG_HOME", None)


if __name__ == "__main__":
    unittest.main()
