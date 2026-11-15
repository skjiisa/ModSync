import json
import os
import shutil
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
                    (tmp / "MO2").mkdir(exist_ok=True)  # mo2-lint creates the folder, not ModSync
                    (tmp / "MO2" / "ModOrganizer.exe").write_text("")
                    return 0

            with patch.object(mo2lint.background, "in_flatpak", return_value=True), \
                    patch.object(mo2lint.subprocess, "Popen", FakeProc):
                result = Mo2LintBackend(binary=fake).install(SKYRIM_SE, tmp / "MO2")
            self.assertTrue(result.success)
            self.assertEqual(seen["args"][:2], ["flatpak-spawn", "--host"])
            self.assertEqual(seen["args"][2], str(fake))

    def test_without_proton_11_a_steam_install_uses_the_games_proton_by_protontricks_name(self):
        """MO2-LINT 7.0.3 asks for "Proton 11.0" unless told otherwise. Told
        another, it finds it by path (also relative to where it runs) or by
        folder name, requiring proton and toolmanifest.vdf, and hands the same
        value to protontricks, which matches only Steam's names for tools
        ("Proton Experimental", in "Proton - Experimental"). The fake does
        both lookups the way they do."""
        from unittest.mock import patch

        from modsync.games import CYBERPUNK
        from modsync.mo2.installers import mo2lint
        from modsync.steam import vdf

        lookups = textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, os, sys, pathlib
            args = sys.argv[1:]
            if "--proton-version" in args:
                value = args[args.index("--proton-version") + 1]
                found = pathlib.Path(value).expanduser()
                ok = (found / "proton").exists() and (found / "toolmanifest.vdf").exists()
                print("mo2-lint finds:", found.resolve() if ok else None)
                names = json.loads(os.environ["PROTONTRICKS_NAMES"])
                print("protontricks finds:", names.get(value))
            dest = pathlib.Path(sys.argv[3])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "ModOrganizer.exe").write_bytes(b"MZ")
            print("installed in:", dest)
            """)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = _script(tmp / "fake-mo2-lint", lookups)
            steamapps = tmp / "steamapps"
            experimental = steamapps / "common" / "Proton - Experimental"
            ge = tmp / "compatibilitytools.d" / "GE-Proton10-34"
            vendor = tmp / "compatibilitytools.d" / "vendor" / "runtime"
            for proton in (experimental, ge, vendor):
                proton.mkdir(parents=True)
                (proton / "proton").write_text("")
                (proton / "toolmanifest.vdf").write_text("")
            (steamapps / "appmanifest_1493710.acf").write_text(vdf.dumps({"AppState": {
                "appid": "1493710", "name": "Proton Experimental", "installdir": "Proton - Experimental"}}))
            (ge / "compatibilitytool.vdf").write_text(vdf.dumps({"compatibilitytools": {"compat_tools": {
                "GE-Proton10-34": {"install_path": ".", "display_name": "GE-Proton10-34"}}}}))
            # Declared beside it, not inside: a valid layout too.
            (tmp / "compatibilitytools.d" / "vendor.vdf").write_text(vdf.dumps({"compatibilitytools": {
                "compat_tools": {"vendor-proton": {"install_path": "vendor/runtime", "display_name": "Vendor"}}}}))
            # What protontricks would match each name to.
            names = {"Proton Experimental": str(experimental), "GE-Proton10-34": str(ge),
                     "vendor-proton": str(vendor)}
            os.environ["PROTONTRICKS_NAMES"] = json.dumps(names)
            self.addCleanup(os.environ.pop, "PROTONTRICKS_NAMES", None)
            os.chdir(tmp)
            self.addCleanup(os.chdir, Path(__file__).resolve().parent.parent)

            def install(game, launcher, found, *, default=False, dest="MO2"):
                lines: list[str] = []
                with patch("modsync.mo2.launch.game_proton_dir", return_value=found), \
                        patch.object(mo2lint, "default_proton_installed", return_value=default), \
                        patch.object(mo2lint, "data_dir", return_value=tmp / "data"):
                    result = Mo2LintBackend(binary=fake).install(game, dest, on_output=lines.append,
                                                                 launcher=launcher)
                self.assertTrue(result.success, "\n".join(lines))
                shutil.rmtree(result.instance_path)
                return "\n".join(lines), result

            for game, launcher, proton in ((SKYRIM_SE, None, experimental), (CYBERPUNK, "steam", ge),
                                           (CYBERPUNK, "steam", vendor)):
                with self.subTest(proton=proton.name):
                    said, result = install(game, launcher, proton)
                    self.assertIn(f"mo2-lint finds: {proton.resolve()}", said)
                    self.assertIn(f"protontricks finds: {proton}", said)
                    # A relative destination is where it was asked for, not in the lookup folder.
                    self.assertEqual(result.instance_path, tmp / "MO2")
                    self.assertIn(f"installed in: {tmp / 'MO2'}", said)
            self.assertEqual(list((tmp / "data" / "tmp").iterdir()), [])  # each lookup folder is removed
            # Proton 11.0 where MO2-LINT looks: MO2-LINT picks, as it intends.
            self.assertNotIn("--proton-version", install(SKYRIM_SE, None, experimental, default=True)[0])
            self.assertNotIn("--proton-version", install(SKYRIM_SE, None, None)[0])  # Steam can't say
            self.assertNotIn("--proton-version", install(CYBERPUNK, "gog", experimental)[0])  # not Steam's game

    def test_proton_11_counts_only_where_mo2lint_looks(self):
        """MO2-LINT uses the libraries of the last Steam it finds: with native
        and Flatpak Steam, the Flatpak's. Proton 11.0 in native Steam's library
        alone isn't found."""
        from unittest.mock import patch

        from modsync.mo2.installers import mo2lint

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            native, flatpak = home / ".local/share/Steam", home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
            for root in (native, flatpak):
                (root / "steamapps").mkdir(parents=True)
                (root / "steamapps/libraryfolders.vdf").write_text(f'"libraryfolders"\n{{\n "0"\n {{\n  "path" "{root}"\n }}\n}}\n')
            proton = native / "steamapps/common/Proton 11.0"
            proton.mkdir(parents=True)
            for name in ("proton", "toolmanifest.vdf"):
                (proton / name).write_text("")
            with patch.dict(os.environ, {"HOME": str(home)}), patch.object(mo2lint.platform, "machine", return_value="x86_64"):
                os.environ.pop("STEAM_DIR", None)
                self.assertEqual(mo2lint.mo2lint_libraries(), [flatpak])
                self.assertFalse(mo2lint.default_proton_installed())
                with patch.dict(os.environ, {"STEAM_DIR": str(native)}):
                    self.assertTrue(mo2lint.default_proton_installed())

    def test_launcher_and_environment_reach_mo2lint(self):
        """Cyberpunk on GOG: --launcher gog, and Wine stand-ins in the environment
        (modsync.cyberpunk.MO2LINT_ENV) — also through flatpak-spawn."""
        from unittest.mock import patch

        from modsync.games import CYBERPUNK
        from modsync.mo2.installers import mo2lint

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = _script(tmp / "fake-mo2-lint", FAKE_OK.replace(
                'print("fake mo2-lint args:", sys.argv[1:])',
                'import os; print("fake mo2-lint args:", sys.argv[1:], "WINE=" + os.environ.get("WINE", ""))'))
            lines: list[str] = []
            result = Mo2LintBackend(binary=fake).install(CYBERPUNK, tmp / "MO2", on_output=lines.append,
                                                         launcher="gog", env={"WINE": "/bin/false"})
            self.assertTrue(result.success, result.message)
            joined = "\n".join(lines)
            self.assertIn("'cyberpunk2077'", joined)
            self.assertIn("'--launcher', 'gog'", joined)
            self.assertIn("WINE=/bin/false", joined)

            seen = {}

            class FakeProc:
                stdout = iter([])

                def __init__(self, args, **kw):
                    seen["args"], seen["env"] = args, kw.get("env")

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def wait(self):
                    return 0

            with patch.object(mo2lint.background, "in_flatpak", return_value=True), \
                    patch.object(mo2lint.subprocess, "Popen", FakeProc):
                Mo2LintBackend._run(["mo2-lint", "install"], None, {"WINE": "/bin/false"})
            self.assertEqual(seen["args"], ["flatpak-spawn", "--host", "--env=WINE=/bin/false", "mo2-lint", "install"])

    def test_failure_leaves_no_empty_folder_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = _script(tmp / "fake-mo2-lint", FAKE_FAIL)
            Mo2LintBackend(binary=fake).install(SKYRIM_SE, tmp / "MO2")
            self.assertFalse((tmp / "MO2").exists())

    def _registry(self, tmp: Path, instance: Path) -> Path:
        import json

        state = tmp / "cfg" / "mo2-lint" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"instances": [{"index": 1, "instance_path": str(instance)}]}))
        return state

    def test_stale_registry_entry_is_cleared_through_mo2lint_before_installing(self):
        """After "Reset setup" and deleting the folder, MO2-LINT's state.json
        still lists the instance and it refuses to install there again."""
        from unittest.mock import patch

        from modsync.mo2.installers import mo2lint

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            dest = tmp / "MO2"
            state = self._registry(tmp, dest)
            fake = _script(tmp / "fake-mo2-lint", FAKE_OK)
            calls = []

            def run(args, on_output, env=None, cwd=None):
                calls.append(args[1:])
                if args[1] == "uninstall":
                    state.write_text('{"instances": []}')  # what mo2-lint uninstall does
                    return 0
                dest.mkdir()
                (dest / "ModOrganizer.exe").write_text("")
                return 0

            lines = []
            with patch.object(mo2lint, "mo2lint_state_path", return_value=state), \
                    patch.object(Mo2LintBackend, "_run", staticmethod(run)):
                result = Mo2LintBackend(binary=fake).install(SKYRIM_SE, dest, on_output=lines.append)
            self.assertTrue(result.success, result.message)
            self.assertEqual(calls[0], ["uninstall", "--directory", str(dest), "--unattended"])
            self.assertEqual(calls[1][:3], ["install", "skyrim_se", str(dest)])
            self.assertTrue(any("no longer exists" in line for line in lines))

    def test_registered_and_present_instance_is_not_reinstalled(self):
        from unittest.mock import patch

        from modsync.mo2.installers import mo2lint

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            dest = tmp / "MO2"
            dest.mkdir()
            (dest / "ModOrganizer.exe").write_text("")
            state = self._registry(tmp, dest)
            fake = _script(tmp / "fake-mo2-lint", FAKE_OK)
            with patch.object(mo2lint, "mo2lint_state_path", return_value=state), \
                    patch.object(Mo2LintBackend, "_run") as run:
                result = Mo2LintBackend(binary=fake).install(SKYRIM_SE, dest)
            run.assert_not_called()
            self.assertFalse(result.success)
            self.assertIn("Use", result.message)

    def test_uninstall_that_does_not_clear_the_entry_stops_the_install(self):
        from unittest.mock import patch

        from modsync.mo2.installers import mo2lint

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            dest = tmp / "MO2"
            state = self._registry(tmp, dest)
            fake = _script(tmp / "fake-mo2-lint", FAKE_OK)
            with patch.object(mo2lint, "mo2lint_state_path", return_value=state), \
                    patch.object(Mo2LintBackend, "_run", staticmethod(lambda a, o, env=None: 0)) :
                result = Mo2LintBackend(binary=fake).install(SKYRIM_SE, dest)
            self.assertFalse(result.success)
            self.assertIn("stale", result.message)

    def test_available_checks_host_tools_not_protontricks(self):
        """protontricks is bundled in mo2-lint; what it needs from the system is
        procps/xdg-utils, and inside the Flatpak they must exist on the host."""
        import subprocess
        from unittest.mock import patch

        from modsync.mo2.installers import mo2lint

        def probe(stdout="", rc=0, stderr=""):
            return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)

        # An x86_64 host; test_machines_without_a_build_are_refused covers the rest.
        machine = patch.object(mo2lint.platform, "machine", return_value="x86_64")
        machine.start()
        self.addCleanup(machine.stop)
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

    def test_machines_without_a_build_are_refused(self):
        """MO2-LINT ships x86_64 and aarch64 binaries. Another CPU can't run
        either, so it needs an mo2-lint on PATH built for it."""
        import subprocess
        from unittest.mock import patch

        from modsync.mo2.installers import mo2lint

        ok_probe = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        for machine in ("aarch64", "arm64", "x86_64", "AMD64"):
            with patch.object(mo2lint.platform, "machine", return_value=machine), \
                    patch.object(mo2lint.shutil, "which", return_value=None), \
                    patch.object(mo2lint.background, "run_host", return_value=ok_probe):
                self.assertEqual(Mo2LintBackend().available(), (True, ""), machine)
        with patch.object(mo2lint.platform, "machine", return_value="riscv64"), \
                patch.object(mo2lint.shutil, "which", return_value=None), \
                patch.object(mo2lint.background, "run_host", return_value=ok_probe) as run:
            ok, reason = Mo2LintBackend().available()
            self.assertFalse(ok)
            self.assertIn("riscv64", reason)
            run.assert_not_called()
        with patch.object(mo2lint.platform, "machine", return_value="riscv64"), \
                patch.object(mo2lint.shutil, "which", return_value="/usr/bin/mo2-lint"), \
                patch.object(mo2lint.background, "run_host", return_value=ok_probe):
            self.assertEqual(Mo2LintBackend().available(), (True, ""))

if __name__ == "__main__":
    unittest.main()


class EnsureMo2LintTests(unittest.TestCase):
    """Downloading the pinned MO2-LINT: checksum, versioned name, old copies."""

    def setUp(self):
        import hashlib
        import io
        import os
        from unittest.mock import patch

        from modsync.mo2.installers import mo2lint

        self.mo2lint = mo2lint
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"XDG_DATA_HOME": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.payload = b"\x7fELF fake mo2-lint"
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.urlopen = patch.object(mo2lint.urllib.request, "urlopen", side_effect=lambda *a, **k: io.BytesIO(self.payload))
        self.bin = Path(tmp.name) / "modsync" / "bin"

    def pinned(self, machine):
        """Run as ``machine``, with its asset pinned to the fake payload."""
        from contextlib import ExitStack
        from unittest.mock import patch

        name = self.mo2lint.MO2LINT_ASSETS[machine][0]
        stack = ExitStack()
        stack.enter_context(patch.object(self.mo2lint.platform, "machine", return_value=machine))
        stack.enter_context(patch.dict(self.mo2lint.MO2LINT_ASSETS, {machine: (name, self.sha)}))
        return stack

    def test_downloads_verifies_and_replaces_older_copies(self):
        from unittest.mock import patch

        self.bin.mkdir(parents=True)
        (self.bin / "mo2-lint").write_bytes(b"old unversioned")
        (self.bin / "mo2-lint-7.0.0-rc7").write_bytes(b"old pin")
        with self.urlopen as urlopen, self.pinned("x86_64"):
            path = self.mo2lint.ensure_mo2lint()
            self.assertEqual(path, self.bin / f"mo2-lint-{self.mo2lint.MO2LINT_VERSION}")
            self.assertEqual(path.read_bytes(), self.payload)
            self.assertTrue(path.stat().st_mode & stat.S_IXUSR)
            self.assertEqual(sorted(p.name for p in self.bin.iterdir()), [path.name])
            self.assertEqual(self.mo2lint.ensure_mo2lint(), path)  # cached: no second download
        urlopen.assert_called_once()
        self.assertTrue(urlopen.call_args.args[0].full_url.endswith(f"/{self.mo2lint.MO2LINT_VERSION}/mo2-lint"))

    def test_arm64_downloads_the_aarch64_build(self):
        with self.urlopen as urlopen, self.pinned("aarch64"):
            path = self.mo2lint.ensure_mo2lint()
        self.assertEqual(path.read_bytes(), self.payload)
        self.assertTrue(urlopen.call_args.args[0].full_url.endswith(f"/{self.mo2lint.MO2LINT_VERSION}/mo2-lint-aarch64"))

    def test_each_build_has_its_own_checksum(self):
        """The x86_64 checksum must not accept the aarch64 download, or the reverse."""
        from unittest.mock import patch

        assets = {"x86_64": ("mo2-lint", self.sha), "aarch64": ("mo2-lint-aarch64", "0" * 64)}
        with self.urlopen, patch.dict(self.mo2lint.MO2LINT_ASSETS, assets), \
                patch.object(self.mo2lint.platform, "machine", return_value="aarch64"):
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                self.mo2lint.ensure_mo2lint()

    def test_no_download_for_a_machine_without_a_build(self):
        from unittest.mock import patch

        with self.urlopen as urlopen, patch.object(self.mo2lint.platform, "machine", return_value="riscv64"):
            with self.assertRaisesRegex(RuntimeError, "riscv64"):
                self.mo2lint.ensure_mo2lint()
        urlopen.assert_not_called()

    def test_a_wrong_checksum_leaves_nothing_behind(self):
        with self.urlopen:
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                self.mo2lint.ensure_mo2lint()
        self.assertEqual(list(self.bin.iterdir()), [])
