import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modsync import background
from modsync.state import EXIT_NEWER_STATE


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.unit = Path(tmp.name) / background.UNIT_NAME
        self.patch_path = patch.object(background, "unit_path", return_value=self.unit)
        self.patch_path.start()
        self.addCleanup(self.patch_path.stop)
        self.autostart = Path(tmp.name) / "autostart" / background.AUTOSTART_NAME
        for p in (patch.object(background, "autostart_path", return_value=self.autostart),
                  patch.object(background, "start_tray")):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def result(code=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def test_start_failure_is_reported(self):
        with patch.object(background, "_systemctl", side_effect=[
            self.result(), self.result(1, stderr="Access denied")
        ]):
            with self.assertRaisesRegex(RuntimeError, "Access denied"):
                background.install()

    def test_inactive_service_is_not_reported_as_started(self):
        with patch.object(background, "_systemctl", side_effect=[
            self.result(), self.result(), self.result(3, stdout="failed")
        ]):
            with self.assertRaisesRegex(RuntimeError, "is-active.*failed"):
                background.install()

    def test_success_checks_service_is_active(self):
        with patch.object(background, "_systemctl", return_value=self.result()) as call:
            self.assertIn("Enabled and started", background.install())
        self.assertIn("modsync serve", self.unit.read_text())
        call.assert_any_call("is-active", background.UNIT_NAME)

    def test_install_adds_the_tray_and_uninstall_removes_it(self):
        with patch.object(background, "_systemctl", return_value=self.result()):
            self.assertIn("tray icon", background.install())
            self.assertIn("modsync tray", self.autostart.read_text())
            background.start_tray.assert_called_once()
            background.uninstall()
        self.assertFalse(self.autostart.exists())

    def test_a_service_from_before_the_tray_gets_it_once(self):
        self.assertFalse(background.ensure_tray_autostart())  # no service: no tray
        self.unit.write_text(background.unit_text())
        self.assertTrue(background.ensure_tray_autostart())
        self.assertTrue(self.autostart.exists())
        self.assertFalse(background.ensure_tray_autostart())
        self.autostart.unlink()  # removed by hand: stays removed
        self.assertFalse(background.ensure_tray_autostart())
        self.assertFalse(self.autostart.exists())

    def test_only_the_setup_the_service_runs_adds_the_tray(self):
        # A separate dev setup's `serve` mustn't add a tray for the installed one.
        with patch.dict(os.environ, {"MODSYNC_HOME": "/home/me/setup-a"}):
            self.unit.write_text(background.unit_text())
            self.assertTrue(background.installed_here())
        with patch.dict(os.environ, {"MODSYNC_HOME": "/home/me/setup-b"}):
            self.assertFalse(background.installed_here())
            self.assertFalse(background.ensure_tray_autostart())
        self.assertFalse(self.autostart.exists())

    def test_failed_stop_preserves_service_file(self):
        self.unit.write_text("service")
        with patch.object(background, "_systemctl", return_value=self.result(1, stderr="denied")):
            with self.assertRaisesRegex(RuntimeError, "denied"):
                background.uninstall()
        self.assertTrue(self.unit.exists())

    def test_status_distinguishes_installed_from_running(self):
        self.unit.write_text("service")
        with patch.object(background, "_systemctl", side_effect=[
            self.result(3, stdout="failed\n"), self.result(stdout="enabled\n")
        ]):
            status = background.status()
        self.assertTrue(status["installed"])
        self.assertEqual(status["active"], "failed")
        self.assertEqual(status["enabled"], "enabled")

class ServiceUnit(unittest.TestCase):
    """The background service runs the setup it was installed from."""

    def test_a_separate_setup_goes_into_the_unit(self):
        with patch.dict(os.environ, {"MODSYNC_HOME": "/home/me/dev 100% $HOME"}), \
                patch.object(background, "in_flatpak", lambda: False):
            native = background.unit_text()
        self.assertIn('Environment="MODSYNC_HOME=/home/me/dev 100%% $HOME"\n', native)  # not expanded there
        self.assertIn(f"RestartPreventExitStatus={EXIT_NEWER_STATE}\n", native)
        with patch.dict(os.environ, {"MODSYNC_HOME": "/home/me/dev-${USER}", "FLATPAK_ID": "io.github.skjiisa.ModSync"}):
            flatpak = background.unit_text()
        # systemd expands $ in a command line even when it's quoted.
        self.assertIn('ExecStart=flatpak run "--env=MODSYNC_HOME=/home/me/dev-$${USER}" io.github.skjiisa.ModSync serve',
                      flatpak)

    def test_the_shared_setup_needs_nothing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MODSYNC_HOME", None)
            self.assertNotIn("MODSYNC_HOME", background.unit_text())


class TrayCommand(unittest.TestCase):
    """The autostart entry runs the tray from the copy and setup the service runs."""

    def test_native_and_flatpak(self):
        with patch.object(background, "in_flatpak", lambda: False), \
                patch.dict(os.environ, {"MODSYNC_HOME": "/home/me/dev 100%"}):
            text = background.autostart_text()
        self.assertIn('Exec=env "MODSYNC_HOME=/home/me/dev 100%%" ', text)
        self.assertTrue(text.rstrip().endswith("X-GNOME-Autostart-enabled=true"))
        self.assertIn(" -m modsync tray\n", text)
        with patch.dict(os.environ, {"FLATPAK_ID": "io.github.skjiisa.ModSync"}):
            os.environ.pop("MODSYNC_HOME", None)
            self.assertIn("Exec=flatpak run io.github.skjiisa.ModSync tray\n", background.autostart_text())
            self.assertEqual(background.command(), ["flatpak", "run", "io.github.skjiisa.ModSync"])

    def test_exec_round_trips_through_the_desktop_entry_rules(self):
        tricky = ["/usr/bin/python3", "/home/me/a b/py", 'a"b`c$d', "back\\slash", "50%", "line\nbreak", "tab\tx"]
        self.assertEqual(parse_exec(background._exec_value(tricky)), tricky)
        # The spec's own example: a literal backslash in a quoted argument is four.
        self.assertEqual(background._exec_value(["a\\b c"]), '"a' + 4 * "\\" + 'b c"')

    def test_the_tray_starts_only_with_a_display(self):
        with patch.object(background, "spawn_detached") as spawn:
            with patch.dict(os.environ, {"WAYLAND_DISPLAY": "", "DISPLAY": ""}):
                background.start_tray()
            spawn.assert_not_called()
            with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
                background.start_tray()
        self.assertEqual(spawn.call_args.args[0][-1], "tray")

    def test_detached_from_the_flatpak_through_the_host(self):
        # The sandbox's display names aren't the host's: the host's session supplies them.
        with patch.object(background, "in_flatpak", lambda: True), \
                patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":99.0"}), \
                patch.object(subprocess, "Popen") as popen:
            background.spawn_detached(["flatpak", "run", "x", "tray"])
        self.assertEqual(popen.call_args.args[0], ["flatpak-spawn", "--host", "flatpak", "run", "x", "tray"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


def parse_exec(value: str) -> list[str]:
    """An Exec value as the Desktop Entry spec reads it: string escapes, then
    quoted arguments. Raises on an escape either layer doesn't allow."""
    escapes = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    line, i = "", 0
    while i < len(value):
        if value[i] == "\\":
            line += escapes[value[i + 1]]
            i += 2
        else:
            line += value[i]
            i += 1
    args, cur, i = [], None, 0
    while i < len(line):
        c = line[i]
        if c == '"':
            cur, i = cur or "", i + 1
            while line[i] != '"':
                if line[i] == "\\":
                    if line[i + 1] not in '"`$\\':
                        raise ValueError(f"bad escape in {line!r}")
                    cur, i = cur + line[i + 1], i + 2
                else:
                    cur, i = cur + line[i], i + 1
            i += 1
        elif c == " ":
            if cur is not None:
                args.append(cur)
            cur, i = None, i + 1
        else:
            cur, i = (cur or "") + c, i + 1
    if cur is not None:
        args.append(cur)
    return [a.replace("%%", "%") for a in args]
