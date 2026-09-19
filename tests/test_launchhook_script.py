"""Run the rendered `proton` script the way Steam does and check what it execs.

Fakes stand in for everything it talks to: the underlying tool's `proton`, the
Steam Linux Runtime's `_v2-entry-point` and the `modsync` command, each writing
its argv/env to a file. Steam's environment (STEAM_COMPAT_*) is set by hand."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from modsync import launchhook
from modsync.games import SKYRIM_SE
from modsync.steam import libraries as libs
from modsync.steam.compattools import CompatTool
from tests import fakesteam

RECORDER = """#!/usr/bin/env bash
python3 - "$0" "$@" <<'PY'
import json, os, sys
name, *args = sys.argv[1:]
out = os.environ["RECORD_DIR"] + "/" + os.path.basename(os.path.dirname(name)) + ".json"
json.dump({"argv": args, "env": {k: v for k, v in os.environ.items() if k.startswith(("STEAM_COMPAT", "MODSYNC", "LD_"))}}, open(out, "w"))
PY
exit 0
"""
HUB_RECORDER = RECORDER.replace("exit 0", "exit ${HUB_EXIT:-0}")


@unittest.skipIf(shutil.which("bash") is None, "bash required")
class RenderedScript(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = fakesteam.make_steam(self.tmp / "Steam")
        self.records = self.tmp / "records"
        self.records.mkdir()
        # replace the fake Steam's stubs with recorders
        self.proton_dir = self.root / "steamapps/common/Proton - Experimental"
        fakesteam._executable(self.proton_dir / "proton", RECORDER)
        fakesteam._executable(self.root / "steamapps/common/SteamLinuxRuntime_4/_v2-entry-point", RECORDER)
        self.modsync_dir = self.tmp / "modsync-cmd"
        fakesteam._executable(self.modsync_dir / "modsync", HUB_RECORDER)
        self.tool_dir = self.root / "compatibilitytools.d" / "modsync_489830_hub"
        underlying = CompatTool("proton_experimental", "Proton Experimental", self.proton_dir, "valve")
        launchhook.render(
            self.tool_dir,
            game=SKYRIM_SE,
            underlying=underlying,
            command=[str(self.modsync_dir / "modsync")],
            library_paths=[lib.path for lib in libs.all_libraries([self.root])],
        )
        self.game_exe = str(self.root / "steamapps/common/Skyrim Special Edition/SkyrimSELauncher.exe")

    def run_hook(self, *args, hub_exit=0, extra_env=None, drop_libs=False):
        env = fakesteam.env_without_flatpak()
        env.update({
            "RECORD_DIR": str(self.records),
            "HUB_EXIT": str(hub_exit),
            "HOME": str(self.tmp),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "STEAM_COMPAT_APP_ID": "489830",
            "STEAM_COMPAT_INSTALL_PATH": str(self.root / "steamapps/common/Skyrim Special Edition"),
            "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(self.root),
            "STEAM_COMPAT_LIBRARY_PATHS": "" if drop_libs else str(self.root),
            "STEAM_COMPAT_TOOL_PATHS": str(self.tool_dir),
            "LD_PRELOAD": "/fake/gameoverlayrenderer.so",
            "LD_LIBRARY_PATH": "/fake/steam-runtime/lib",
        })
        env.pop("MODSYNC_LAUNCH_HUB", None)
        env.update(extra_env or {})
        return subprocess.run([str(self.tool_dir / "proton"), *args], env=env, capture_output=True, text=True, timeout=30)

    def record(self, name):
        p = self.records / f"{name}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def log(self):
        return (self.tmp / "state/modsync/launch-hook.log").read_text()

    def test_play_opens_hub_then_chains_through_the_runtime(self):
        result = self.run_hook("waitforexitandrun", self.game_exe)
        self.assertEqual(result.returncode, 0, result.stderr)
        hub = self.record("modsync-cmd")
        self.assertEqual(hub["argv"], ["launch", "hub", "--appid", "489830", "--through", "proton_experimental"])
        self.assertNotIn("LD_PRELOAD", hub["env"])  # the overlay must not be preloaded into Qt
        self.assertNotIn("LD_LIBRARY_PATH", hub["env"])  # nor Steam's runtime libs shadow the hub's Qt
        self.assertEqual(hub["env"]["MODSYNC_LAUNCH_HUB"], "1")
        entry = self.record("SteamLinuxRuntime_4")
        self.assertEqual(
            entry["argv"],
            ["--verb=waitforexitandrun", "--", str(self.proton_dir / "proton"), "waitforexitandrun", self.game_exe],
        )
        self.assertEqual(
            entry["env"]["STEAM_COMPAT_TOOL_PATHS"],
            f"{self.proton_dir}:{self.root}/steamapps/common/SteamLinuxRuntime_4",
        )
        self.assertEqual(entry["env"]["LD_PRELOAD"], "/fake/gameoverlayrenderer.so")  # the game keeps it
        self.assertEqual(entry["env"]["LD_LIBRARY_PATH"], "/fake/steam-runtime/lib")
        self.assertIsNone(self.record("Proton - Experimental"))  # exec'd by the runtime, not by us
        self.assertIn("continue", self.log())

    def test_cancel_ends_the_launch_cleanly(self):
        result = self.run_hook("waitforexitandrun", self.game_exe, hub_exit=launchhook.EXIT_CANCEL)
        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(self.record("modsync-cmd"))
        self.assertIsNone(self.record("SteamLinuxRuntime_4"))
        self.assertIn("cancelled", self.log())

    def test_hub_failure_never_blocks_the_game(self):
        result = self.run_hook("waitforexitandrun", self.game_exe, hub_exit=3)
        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(self.record("SteamLinuxRuntime_4"))
        self.assertIn("launching anyway", self.log())

    def test_hub_is_not_reopened_down_the_chain(self):
        self.run_hook("waitforexitandrun", self.game_exe, extra_env={"MODSYNC_LAUNCH_HUB": "1"})
        self.assertIsNone(self.record("modsync-cmd"))
        self.assertIsNotNone(self.record("SteamLinuxRuntime_4"))

    def test_run_verb_skips_hub_but_uses_runtime(self):
        self.run_hook("run", "iscriptevaluator.exe")
        self.assertIsNone(self.record("modsync-cmd"))
        entry = self.record("SteamLinuxRuntime_4")
        self.assertEqual(entry["argv"][0], "--verb=run")

    def test_path_queries_go_straight_to_proton(self):
        self.run_hook("getcompatpath", "C:\\\\x")
        self.assertIsNone(self.record("modsync-cmd"))
        self.assertIsNone(self.record("SteamLinuxRuntime_4"))
        self.assertEqual(self.record("Proton - Experimental")["argv"], ["getcompatpath", "C:\\\\x"])

    def test_runtime_found_via_baked_libraries_when_steam_env_is_bare(self):
        self.run_hook("waitforexitandrun", self.game_exe, drop_libs=True, extra_env={"STEAM_COMPAT_CLIENT_INSTALL_PATH": "/nope"})
        self.assertIsNotNone(self.record("SteamLinuxRuntime_4"))

    def test_missing_runtime_falls_back_to_plain_proton(self):
        shutil.rmtree(self.root / "steamapps/common/SteamLinuxRuntime_4")
        result = self.run_hook("waitforexitandrun", self.game_exe)
        self.assertEqual(result.returncode, 0)
        proton = self.record("Proton - Experimental")
        self.assertEqual(proton["argv"], ["waitforexitandrun", self.game_exe])
        self.assertEqual(proton["env"]["STEAM_COMPAT_TOOL_PATHS"], str(self.proton_dir))
        self.assertIn("WARNING", self.log())

    def test_underlying_gone_is_an_error(self):
        shutil.rmtree(self.proton_dir)
        result = self.run_hook("waitforexitandrun", self.game_exe)
        self.assertEqual(result.returncode, 1)
        self.assertIn("is gone", self.log())

    def test_re_reads_runtime_requirement_each_launch(self):
        # MO2-LINT (or Valve) moves the underlying tool to another runtime: no re-render needed.
        (self.proton_dir / "toolmanifest.vdf").write_text(
            f'"manifest"\n{{\n\t"require_tool_appid"\t\t"{fakesteam.SNIPER_APPID}"\n}}\n'
        )
        (self.root / f"steamapps/appmanifest_{fakesteam.SNIPER_APPID}.acf").write_text(
            fakesteam._acf(fakesteam.SNIPER_APPID, "SLR sniper", "SteamLinuxRuntime_sniper")
        )
        fakesteam._executable(self.root / "steamapps/common/SteamLinuxRuntime_sniper/_v2-entry-point", RECORDER)
        self.run_hook("waitforexitandrun", self.game_exe)
        self.assertIsNotNone(self.record("SteamLinuxRuntime_sniper"))
        self.assertIsNone(self.record("SteamLinuxRuntime_4"))


if __name__ == "__main__":
    unittest.main()
