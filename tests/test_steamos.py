import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modsync import background, steamos

FRAME = 'NAME="SteamOS"\nID=steamos\nVARIANT_ID="vr"\nVERSION_ID=0.3.0\n'
DECK = "NAME=SteamOS\nID=steamos\nVARIANT_ID=steamdeck\n"
ARCH = "NAME=CachyOS\nID=cachyos\n"


class SteamOSVariant(unittest.TestCase):
    def with_os_release(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "os-release"
        path.write_text(text)
        real = Path

        def fake_path(p, *rest):
            return path if str(p) == "/etc/os-release" else real(p, *rest)

        patcher = patch.object(steamos, "Path", side_effect=fake_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_frame_variant_is_quoted_and_needs_a_reboot(self):
        self.with_os_release(FRAME)
        self.assertEqual(steamos.variant(), "vr")
        self.assertTrue(steamos.is_steam_frame())
        self.assertIn("reboot", steamos.steam_restart_hint())
        self.assertNotIn("Restart Steam", steamos.steam_restart_hint())

    def test_deck_names_the_power_menu(self):
        self.with_os_release(DECK)
        self.assertFalse(steamos.is_steam_frame())
        self.assertIn("Power, then Restart Steam", steamos.steam_restart_hint())

    def test_other_distros_are_not_steamos(self):
        self.with_os_release(ARCH)
        self.assertIsNone(steamos.variant())
        self.assertTrue(steamos.steam_restart_hint().startswith("Restart Steam and"))


class UserManagerEnv(unittest.TestCase):
    """The Frame's nested Plasma desktop sets XDG_RUNTIME_DIR to a directory
    without systemd's sockets; systemctl --user must use the login session's."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.login = Path(tmp.name) / "1000"
        (self.login / "systemd").mkdir(parents=True)
        (self.login / "systemd" / "private").touch()
        self.nested = self.login / "nested_plasma"
        self.nested.mkdir()
        real = Path
        patcher = patch.object(
            background, "Path", side_effect=lambda p, *r: self.login if str(p).startswith("/run/user/") else real(p, *r)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for p in (patch.object(background, "in_flatpak", return_value=False),):
            p.start()
            self.addCleanup(p.stop)

    def test_nested_runtime_dir_falls_back_to_the_login_session(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.nested)}):
            env = background._user_manager_env()
        self.assertEqual(env["XDG_RUNTIME_DIR"], str(self.login))

    def test_a_working_runtime_dir_is_left_alone(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.login)}):
            self.assertIsNone(background._user_manager_env())

    def test_systemctl_gets_the_fixed_environment(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.nested)}), \
                patch.object(background.subprocess, "run") as run:
            background._systemctl("daemon-reload")
        self.assertEqual(run.call_args.kwargs["env"]["XDG_RUNTIME_DIR"], str(self.login))


if __name__ == "__main__":
    unittest.main()
