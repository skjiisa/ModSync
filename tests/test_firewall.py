"""Firewall detection and the one-click allow, with the host commands mocked."""

import json
import subprocess
import unittest
from unittest.mock import patch

from modsync import firewall
from modsync.firewall import Check, Firewall, FirewallError
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
        self.assertEqual(len(text.splitlines()), len(firewall.PORTS))
        self.assertIn("sudo ufw delete allow 21029/tcp", firewall.manual_instructions(Firewall("ufw"), remove=True))

    def test_remove_mirrors_allow_exactly(self):
        for kind in ("ufw", "firewalld"):
            fw = Firewall(kind)
            adds = [c for c in fw.allow_commands() if c != ["firewall-cmd", "--reload"]]
            dels = [c for c in fw.remove_commands() if c != ["firewall-cmd", "--reload"]]
            self.assertEqual(len(adds), len(dels), kind)
            for add, dele in zip(adds, dels):
                if kind == "ufw":
                    self.assertEqual(dele, [add[0], "delete", *add[1:]])
                else:
                    self.assertEqual(dele, [s.replace("--add-port", "--remove-port") for s in add])
        self.assertEqual(Firewall("firewalld").remove_commands()[-1], ["firewall-cmd", "--reload"])

    def test_remove_script_keeps_going_past_missing_rules(self):
        script = Firewall("ufw").remove_script()
        self.assertNotIn("&&", script)
        self.assertEqual(script.count("; "), len(firewall.PORTS) - 1)


class Allow(unittest.TestCase):
    def test_runs_the_script_under_pkexec_and_returns_the_new_stamp(self):
        def fake(cmd, **kw):
            return _cp(0, stdout="1789950025\n" if cmd[0] == "stat" else "")

        with patch.object(firewall, "run_host", side_effect=fake) as run:
            self.assertEqual(firewall.allow(Firewall("ufw")), "ufw:1789950025")
        cmd = run.call_args_list[0].args[0]
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

    def test_revoke_runs_the_remove_script_and_returns_the_new_stamp(self):
        def fake(cmd, **kw):
            return _cp(0, stdout="1789951000\n" if cmd[0] == "stat" else "")

        with patch.object(firewall, "run_host", side_effect=fake) as run:
            self.assertEqual(firewall.revoke(Firewall("ufw")), "ufw:1789951000")
        cmd = run.call_args_list[0].args[0]
        self.assertEqual(cmd[:3], ["pkexec", "sh", "-c"])
        self.assertEqual(cmd[3], Firewall("ufw").remove_script())
        with patch.object(firewall, "run_host", return_value=_cp(126)):
            with self.assertRaises(FirewallError) as ctx:
                firewall.revoke(Firewall("ufw"))
            self.assertEqual(str(ctx.exception), "cancelled")


UFW_RULES_ALLOWED = """\
### tuple ### allow tcp 11434 0.0.0.0/0 any 0.0.0.0/0 in
### tuple ### allow tcp 9943:9944 0.0.0.0/0 any 0.0.0.0/0 alvr - in
### tuple ### allow tcp 21029 0.0.0.0/0 any 0.0.0.0/0 in comment=4d6f
### tuple ### allow udp 21029 0.0.0.0/0 any 0.0.0.0/0 in comment=4d6f
### tuple ### allow tcp 22000 0.0.0.0/0 any 0.0.0.0/0 in comment=5379
### tuple ### allow udp 21027 0.0.0.0/0 any 0.0.0.0/0 in comment=5379
"""


class UfwRules(unittest.TestCase):
    def test_every_port_present(self):
        self.assertTrue(firewall.ufw_rules_allow(UFW_RULES_ALLOWED))

    def test_one_port_missing(self):
        text = UFW_RULES_ALLOWED.replace("### tuple ### allow udp 21027 0.0.0.0/0 any 0.0.0.0/0 in comment=5379\n", "")
        self.assertFalse(firewall.ufw_rules_allow(text))

    def test_manual_rule_without_protocol_covers_both(self):
        # `ufw allow 21029` writes proto "any"; ranges and app profiles count too.
        text = (
            "### tuple ### allow any 21029 0.0.0.0/0 any 0.0.0.0/0 in comment=4d6f\n"
            "### tuple ### allow tcp 21000:23000 0.0.0.0/0 any 0.0.0.0/0 in\n"
            "### tuple ### allow udp 21027 0.0.0.0/0 any 0.0.0.0/0 syncthing - in\n"
        )
        self.assertTrue(firewall.ufw_rules_allow(text))

    def test_out_deny_and_other_lines_do_not_count(self):
        text = (
            "# comment\n"
            "-A ufw-user-input -p tcp --dport 21029 -j ACCEPT\n"
            "### tuple ### allow any 21029 0.0.0.0/0 any 0.0.0.0/0 out\n"
            "### tuple ### deny tcp 22000 0.0.0.0/0 any 0.0.0.0/0 in\n"
        )
        self.assertFalse(firewall.ufw_rules_allow(text))


class ReadRules(unittest.TestCase):
    def test_ufw_readable_rules_are_parsed(self):
        with patch.object(firewall, "run_host", return_value=_cp(0, stdout=UFW_RULES_ALLOWED)):
            self.assertTrue(Firewall("ufw").ports_allowed())
        with patch.object(firewall, "run_host", return_value=_cp(0, stdout="")):
            self.assertFalse(Firewall("ufw").ports_allowed())

    def test_ufw_unreadable_rules_are_unknown(self):
        with patch.object(firewall, "run_host", return_value=_cp(1, stderr="Permission denied")):
            self.assertIsNone(Firewall("ufw").ports_allowed())

    def test_ufw_stamp_is_the_rules_file_mtime(self):
        with patch.object(firewall, "run_host", return_value=_cp(0, stdout="1789950025\n")) as run:
            self.assertEqual(Firewall("ufw").rules_stamp(), "ufw:1789950025")
        self.assertEqual(run.call_args.args[0], ["stat", "-c", "%Y", firewall.UFW_RULES])
        with patch.object(firewall, "run_host", return_value=_cp(1)):
            self.assertEqual(Firewall("ufw").rules_stamp(), "")

    def test_firewalld_queries_each_port(self):
        with patch.object(firewall, "run_host", return_value=_cp(0)) as run:
            self.assertTrue(Firewall("firewalld").ports_allowed())
        queried = {c.args[0][1] for c in run.call_args_list}
        self.assertEqual(queried, {f"--query-port={p}/{pr}" for p, pr, _ in firewall.PORTS})
        with patch.object(firewall, "run_host", return_value=_cp(1)):
            self.assertFalse(Firewall("firewalld").ports_allowed())
        with patch.object(firewall, "run_host", return_value=_cp(252, stderr="not running")):
            self.assertIsNone(Firewall("firewalld").ports_allowed())


class LaunchCheck(unittest.TestCase):
    def test_no_firewall_is_allowed(self):
        with patch.object(firewall, "detect", return_value=None):
            self.assertEqual(firewall.check("ufw:1"), Check(None, True, ""))

    def test_readable_rules_win_over_any_remembered_stamp(self):
        fw = Firewall("ufw")
        with patch.object(firewall, "detect", return_value=fw), \
                patch.object(Firewall, "rules_stamp", return_value="ufw:2"), \
                patch.object(Firewall, "ports_allowed", return_value=False):
            self.assertEqual(firewall.check("ufw:2"), Check(fw, False, "ufw:2"))
        with patch.object(firewall, "detect", return_value=fw), \
                patch.object(Firewall, "rules_stamp", return_value="ufw:2"), \
                patch.object(Firewall, "ports_allowed", return_value=True):
            self.assertEqual(firewall.check(""), Check(fw, True, "ufw:2"))

    def test_unreadable_rules_fall_back_to_the_stamp(self):
        fw = Firewall("ufw")
        with patch.object(firewall, "detect", return_value=fw), \
                patch.object(Firewall, "rules_stamp", return_value="ufw:2"), \
                patch.object(Firewall, "ports_allowed", return_value=None):
            self.assertTrue(firewall.check("ufw:2").allowed)  # unchanged since we allowed
            self.assertFalse(firewall.check("ufw:1").allowed)  # rules edited since
            self.assertFalse(firewall.check("").allowed)  # never allowed
        with patch.object(firewall, "detect", return_value=fw), \
                patch.object(Firewall, "rules_stamp", return_value=""), \
                patch.object(Firewall, "ports_allowed", return_value=None):
            self.assertFalse(firewall.check("").allowed)  # nothing to go on: warn


class Cli(unittest.TestCase):
    def _run(self, args, chk, **patches):
        import io
        from contextlib import redirect_stdout
        from modsync.cli import firewall_cmd

        out = io.StringIO()
        state = State(instance_path="/x")
        with patch.object(firewall, "check", return_value=chk), redirect_stdout(out), \
                patch.object(State, "load", return_value=state), patch.object(State, "save"), \
                patch.multiple(firewall, **patches) if patches else patch.object(firewall, "PORTS", firewall.PORTS):
            code = firewall_cmd(args)
        return code, out.getvalue(), state

    def test_status(self):
        code, out, _ = self._run(["status"], Check(Firewall("ufw"), True, "ufw:1"))
        self.assertEqual(code, 0)
        self.assertIn("allowed:  yes", out)
        code, out, _ = self._run(["status"], Check(None, True, ""))
        self.assertEqual(code, 0)
        self.assertIn("none detected", out)

    def test_allow_and_remove_update_the_remembered_stamp(self):
        code, out, state = self._run(["allow"], Check(Firewall("ufw"), False, "ufw:1"), allow=lambda fw: "ufw:2")
        self.assertEqual(code, 0)
        self.assertEqual(state.firewall_rules_stamp, "ufw:2")
        code, out, state = self._run(["remove"], Check(Firewall("ufw"), True, "ufw:2"), revoke=lambda fw: "ufw:3")
        self.assertEqual(code, 0)
        self.assertEqual(state.firewall_rules_stamp, "")
        self.assertIn("Removed", out)

    def test_cancel_prints_the_manual_commands(self):
        def cancelled(fw):
            raise FirewallError("cancelled")

        code, out, _ = self._run(["allow"], Check(Firewall("ufw"), False, "ufw:1"), allow=cancelled)
        self.assertEqual(code, 1)
        self.assertIn("sudo ufw allow 21029/tcp", out)


class StateStamp(unittest.TestCase):
    def test_stamp_round_trips_and_old_boolean_is_ignored(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(State, "path", return_value=Path(tmp) / "state.json"):
                State.path().write_text(json.dumps({"instance_path": "/x", "firewall_allowed": True}))
                s = State.load()
                self.assertEqual(s.firewall_rules_stamp, "")
                s.firewall_rules_stamp = "ufw:1789950025"
                s.save()
                self.assertEqual(State.load().firewall_rules_stamp, "ufw:1789950025")


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
