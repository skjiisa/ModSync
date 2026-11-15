"""What counts as playing: a game running, and no ModSync window open."""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from modsync import background, playing
from modsync.playing import Process

STEAM = "/home/deck/.local/share/Steam/ubuntu12_32/reaper SteamLaunch AppId=489830 -- /home/deck/x"
GOGDL = ("python3 ./gogdl --auth-config-path /home/deck/.config/heroic/gog_store/auth.json launch "
         "/home/deck/Games/Heroic/Cyberpunk 2077 1423049311 --no-wine --wrapper /x/play.sh /usr/bin/umu-run")


def games(*procs):
    return playing.games_in([Process(*p) for p in procs])


class GamesTests(unittest.TestCase):
    def test_anything_steam_launched(self):
        self.assertTrue(games((10, 1, "reaper", STEAM)))
        # On the Steam Frame FEX-Emu runs Steam's x86 reaper.
        self.assertTrue(games((10, 1, "FEXInterpreter", STEAM)))

    def test_a_command_line_that_merely_mentions_it_is_not(self):
        self.assertFalse(games((10, 1, "zsh", f"/usr/bin/zsh -c 'pgrep -f {STEAM}'")))
        self.assertFalse(games((10, 1, "grep", "grep reaper SteamLaunch")))
        self.assertFalse(games((10, 1, "oom_reaper", "[oom_reaper]")))

    def test_mo2_and_daggerfall_on_their_own(self):
        self.assertTrue(games((10, 1, "ModOrganizer.ex", r"Z:\home\deck\MO2\ModOrganizer.exe -p Default")))
        self.assertTrue(games((10, 1, "DaggerfallUnity", "/home/deck/Games/Daggerfall Unity/DaggerfallUnity.x86_64")))
        self.assertTrue(games((10, 1, "FEX", "/home/deck/Games/Daggerfall Unity/DaggerfallUnity.x86_64 -x")))

    def test_a_heroic_game_is_a_windows_program_its_launch_started(self):
        self.assertTrue(games((10, 1, "python3", GOGDL), (11, 10, "umu-run", "/usr/bin/umu-run x"),
                              (12, 11, "Cyberpunk2077.e", r"Z:\home\deck\Games\Heroic\Cyberpunk 2077\bin\x64\Cyberpunk2077.exe")))

    def test_a_launch_kept_alive_by_wine_services_is_not(self):
        # Seen for real: gogdl waiting 15 hours on a winedevice.exe after the game closed,
        # while an unrelated installer ran in another prefix.
        self.assertFalse(games((10, 1, "python3", GOGDL),
                               (11, 10, "winedevice.exe", r"C:\windows\system32\winedevice.exe"),
                               (20, 1, "AXRB-Setup-1.0.", r"Z:\home\deck\exes\AXRB-Setup-1.0.4.exe")))

    def test_another_windows_program_is_not(self):
        self.assertFalse(games((20, 1, "AXRB-Setup-1.0.", r"Z:\home\deck\exes\AXRB-Setup-1.0.4.exe")))

    def test_parses_ps(self):
        out = ("      1       0 systemd         /usr/lib/systemd/systemd --switched-root\n"
               "    523       1 Web Content     /usr/lib/firefox/firefox -contentproc\n"
               " 460404  460333 winedevice.exe  C:\\windows\\system32\\winedevice.exe\n"
               "      2       0 kthreadd        [kthreadd]\n")
        self.assertEqual(playing._parse_ps(out), [
            Process(1, 0, "systemd", "/usr/lib/systemd/systemd --switched-root"),
            Process(523, 1, "Web Content", "/usr/lib/firefox/firefox -contentproc"),
            Process(460404, 460333, "winedevice.exe", "C:\\windows\\system32\\winedevice.exe"),
            Process(2, 0, "kthreadd", "[kthreadd]"),
        ])

    def test_asks_the_host_and_says_no_when_it_cannot(self):
        ps = SimpleNamespace(returncode=0, stdout=f"     10       1 reaper          {STEAM}\n", stderr="")
        with patch.object(background, "run_host", return_value=ps) as run:
            self.assertTrue(playing.game_running())
        self.assertEqual(run.call_args.args[0][0], "ps")
        with patch.object(background, "run_host", side_effect=OSError("no ps")):
            self.assertFalse(playing.game_running())
        with patch.object(background, "run_host", return_value=SimpleNamespace(returncode=1, stdout="", stderr="x")):
            self.assertFalse(playing.game_running())


class WindowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"XDG_DATA_HOME": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(playing.release_window_lock)

    def test_an_open_window_is_seen_until_it_closes(self):
        self.assertFalse(playing.window_open())
        playing.hold_window_lock()
        self.assertTrue(playing.window_open())
        with patch.object(playing, "game_running", side_effect=AssertionError("no need to ask the host")):
            self.assertFalse(playing.playing())
        playing.release_window_lock()
        self.assertFalse(playing.window_open())
        with patch.object(playing, "game_running", return_value=True):
            self.assertTrue(playing.playing())


if __name__ == "__main__":
    unittest.main()
