"""Firewall detection and the one-click allow, with the host commands mocked."""

import json
import subprocess
import unittest
from unittest.mock import patch

from modsync import firewall
from modsync.firewall import Firewall, FirewallError
from modsync.state import State
from modsync.sync import pairing


def _cp(returncode: int, stderr: str = "", stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class Detect(unittest.TestCase):
    def test_ufw_active(self):
        def fake(cmd, **kw):
            return _cp(0 if cmd[-1] == "ufw" else 3)

        with patch.object(firewall, "run_host", side_effect=fake) as run:
            self.assertEqual(firewall.detect(), Firewall("ufw"))
        self.assertEqual(run.call_args_list[0].args[0], ["systemctl", "is-active", "--quiet", "ufw"])

    def test_firewalld_active(self):
        def fake(cmd, **kw):
            return _cp(0 if cmd[-1] == "firewalld" else 3)

        with patch.object(firewall, "run_host", side_effect=fake):
            self.assertEqual(firewall.detect(), Firewall("firewalld"))

    def test_nothing_running_or_no_systemd(self):
        with patch.object(firewall, "run_host", return_value=_cp(3)):
            self.assertIsNone(firewall.detect())
        with patch.object(firewall, "run_host", side_effect=FileNotFoundError("systemctl")):
            self.assertIsNone(firewall.detect())


class Commands(unittest.TestCase):
    def test_ufw_rules_cover_every_port_once(self):
        script = Firewall("ufw").allow_script()
        for port, proto, _ in firewall.PORTS:
            self.assertEqual(script.count(f"ufw allow {port}/{proto}"), 1)
        self.assertNotIn("sudo", script)  # pkexec already runs it as root

    def test_firewalld_rules_are_permanent_and_reloaded(self):
        cmds = Firewall("firewalld").allow_commands()
        self.assertEqual(cmds[-1], ["firewall-cmd", "--reload"])
        for port, proto, _ in firewall.PORTS:
            self.assertIn(["firewall-cmd", "--permanent", f"--add-port={port}/{proto}"], cmds)

    def test_manual_instructions_use_sudo_per_command(self):
        text = firewall.manual_instructions(Firewall("ufw"))
        self.assertEqual(text.count("sudo "), len(firewall.PORTS))


class Allow(unittest.TestCase):
    def test_runs_the_script_under_pkexec(self):
        with patch.object(firewall, "run_host", return_value=_cp(0)) as run:
            firewall.allow(Firewall("ufw"))
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], ["pkexec", "sh", "-c"])
        self.assertEqual(cmd[3], Firewall("ufw").allow_script())

    def test_dismissed_password_prompt_is_a_cancel(self):
        for code in (126, 127):
            with patch.object(firewall, "run_host", return_value=_cp(code)):
                with self.assertRaises(FirewallError) as ctx:
                    firewall.allow(Firewall("ufw"))
                self.assertEqual(str(ctx.exception), "cancelled")

    def test_failure_surfaces_the_last_stderr_line(self):
        with patch.object(firewall, "run_host", return_value=_cp(1, stderr="a\nERROR: Bad port\n")):
            with self.assertRaises(FirewallError) as ctx:
                firewall.allow(Firewall("ufw"))
        self.assertEqual(str(ctx.exception), "ERROR: Bad port")

    def test_missing_pkexec(self):
        with patch.object(firewall, "run_host", side_effect=FileNotFoundError("pkexec")):
            with self.assertRaises(FirewallError):
                firewall.allow(Firewall("ufw"))


class StateFlag(unittest.TestCase):
    def test_firewall_allowed_round_trips_and_defaults_false(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(State, "path", return_value=Path(tmp) / "state.json"):
                self.assertFalse(State.load().firewall_allowed)
                s = State(instance_path="/x")
                s.firewall_allowed = True
                s.save()
                self.assertTrue(State.load().firewall_allowed)
                self.assertTrue(json.loads(State.path().read_text())["firewall_allowed"])


class StaticAddresses(unittest.TestCase):
    def test_dials_the_lan_address_first_then_falls_back_to_discovery(self):
        self.assertEqual(
            pairing.static_addresses("192.168.68.74"),
            ["tcp://192.168.68.74:22000", "dynamic"],
        )

    def test_unknown_host_leaves_the_default(self):
        self.assertIsNone(pairing.static_addresses(None))
        self.assertIsNone(pairing.static_addresses(""))


if __name__ == "__main__":
    unittest.main()
