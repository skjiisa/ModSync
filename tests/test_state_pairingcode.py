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

    def test_encode_has_readable_prefix(self):
        self.assertTrue(PairingCode("d", "f", "l").encode().startswith("MODSYNC1-"))


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
