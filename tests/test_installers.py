import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

from modsync.games import SKYRIM_SE
from modsync.mo2.installers.mo2lint import Mo2LintBackend

FAKE_OK = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import sys, pathlib
    print("fake mo2-lint args:", sys.argv[1:])
    dest = pathlib.Path(sys.argv[3])
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "ModOrganizer.exe").write_bytes(b"MZ")
    (dest / "mods").mkdir(exist_ok=True)
    sys.exit(0)
    """
)

FAKE_FAIL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import sys
    print("boom", file=sys.stderr)
    sys.exit(3)
    """
)


def _script(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class Mo2LintBackendTests(unittest.TestCase):
    def test_install_success_builds_command_and_detects_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = _script(tmp / "fake-mo2-lint", FAKE_OK)
            dest = tmp / "MO2"
            lines: list[str] = []

            result = Mo2LintBackend(binary=fake).install(
                SKYRIM_SE, dest, script_extender=True, on_output=lines.append
            )

            self.assertTrue(result.success, result.message)
            self.assertEqual(result.instance_path, dest)
            self.assertTrue((dest / "ModOrganizer.exe").exists())
            joined = "\n".join(lines)
            self.assertIn("skyrim_se", joined)
            self.assertIn("--unattended", joined)
            self.assertIn("--script-extender", joined)

    def test_install_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = _script(tmp / "fake-mo2-lint", FAKE_FAIL)
            result = Mo2LintBackend(binary=fake).install(
                SKYRIM_SE, tmp / "MO2", on_output=lambda _line: None
            )
            self.assertFalse(result.success)
            self.assertEqual(result.returncode, 3)
            self.assertIsNone(result.instance_path)

    def test_inside_the_flatpak_the_binary_runs_on_the_host(self):
        """The sandbox can't see Steam/Proton; mo2-lint has to run via
        flatpak-spawn --host. The fake binary would be found on the host too."""
        from unittest.mock import patch

        from modsync.mo2.installers import mo2lint

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = _script(tmp / "fake-mo2-lint", FAKE_OK)
            seen = {}

            class FakeProc:
                stdout = iter(["ok\n"])

                def __init__(self, args, **kw):
                    seen["args"] = args

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def wait(self):
                    (tmp / "MO2" / "ModOrganizer.exe").write_text("")
                    return 0

            with patch.object(mo2lint.background, "in_flatpak", return_value=True), \
                    patch.object(mo2lint.subprocess, "Popen", FakeProc):
                result = Mo2LintBackend(binary=fake).install(SKYRIM_SE, tmp / "MO2")
            self.assertTrue(result.success)
            self.assertEqual(seen["args"][:2], ["flatpak-spawn", "--host"])
            self.assertEqual(seen["args"][2], str(fake))

    def test_available_checks_host_tools_not_protontricks(self):
        """protontricks is bundled in mo2-lint; what it needs from the system is
        procps/xdg-utils, and inside the Flatpak they must exist on the host."""
        import subprocess
        from unittest.mock import patch

        from modsync.mo2.installers import mo2lint

        def probe(stdout="", rc=0, stderr=""):
            return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)

        with patch.object(mo2lint.background, "run_host", return_value=probe()) as run:
            self.assertEqual(Mo2LintBackend().available(), (True, ""))
        self.assertEqual(run.call_args.args[0][:2], ["sh", "-c"])
        self.assertNotIn("protontricks", run.call_args.args[0][2])
        with patch.object(mo2lint.background, "run_host", return_value=probe("pgrep\n")):
            ok, reason = Mo2LintBackend().available()
            self.assertFalse(ok)
            self.assertIn("pgrep", reason)
            self.assertIn("procps", reason)
        with patch.object(mo2lint.background, "run_host", return_value=probe(rc=1, stderr="Portal call failed")), \
                patch.object(mo2lint.background, "in_flatpak", return_value=True):
            ok, reason = Mo2LintBackend().available()
            self.assertFalse(ok)
            self.assertIn("host system from the Flatpak", reason)


if __name__ == "__main__":
    unittest.main()
