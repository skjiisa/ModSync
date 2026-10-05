"""Copying, pausing, conflicts and the check before playing, in the window."""

from unittest.mock import patch

from modsync.pairing_code import PairingCode
from modsync.service import CopyProgress, DeviceStatus, LaunchCheck, ModSyncService, SyncStatus
from modsync.state import State
from tests.ui_support import UiTestCase

CODE = PairingCode("A" * 56, "modsync-abc", "Desktop").encode()
STAMP = "sync-conflict-20261004-101500-AAAAAAA"


class JoinModeTests(UiTestCase):
    def _panel(self):
        State(instance_path=str(self.tmp)).save()
        window = self.window()
        window.go("sync")
        return window, window.pages["sync"].join

    def test_an_empty_instance_copies_without_asking(self):
        window, panel = self._panel()
        with patch("modsync.ui.pages.sync.worker.run_async") as run:
            panel.join_code(CODE)
        self.assertIsNone(window.top_overlay)
        self.assertEqual(run.call_args.kwargs["merge"], False)

    def test_existing_mods_ask_copy_or_merge(self):
        (self.tmp / "mods" / "Mine").mkdir(parents=True)
        window, panel = self._panel()
        for key, merge in (("copy", False), ("merge", True), ("cancel", None)):
            with self.subTest(key), patch("modsync.ui.pages.sync.worker.run_async") as run:
                panel.join_code(CODE)
                sheet = window.top_overlay
                self.assertEqual(sheet.title.text(), "This machine already has mods")
                self.assertIs(window.focusWidget(), sheet.tiles["copy"])
                sheet.choose(key)
                if merge is None:
                    run.assert_not_called()
                else:
                    self.assertEqual(run.call_args.kwargs["merge"], merge)
                window._busy.clear()

    def test_network_join_asks_too(self):
        from modsync.pairing_lan import Announcement

        (self.tmp / "downloads").mkdir()
        (self.tmp / "downloads" / "mod.7z").write_bytes(b"")
        (self.tmp / "downloads" / "mod.7z.meta").write_text("")
        self.assertEqual(ModSyncService.local_mod_count(self.tmp), 1)
        window, panel = self._panel()
        peer = Announcement("Desktop", "192.0.2.2", 21029, "s")
        with patch("modsync.ui.pages.sync.worker.run_async") as run:
            panel.join_network(peer, "123456")
            window.top_overlay.choose("merge")
        self.assertEqual(run.call_args.args[:4], (window.service.join_via_network, peer, "123456", str(self.tmp)))
        self.assertTrue(run.call_args.kwargs["merge"])


class LiveSyncTests(UiTestCase):
    def _window(self, **state):
        State(instance_path=str(self.tmp), folder_id="modsync-abc", **state).save()
        window = self.window()
        window.go("sync")
        return window, window.pages["sync"]

    def status(self, **kw):
        return SyncStatus("ME" * 4, "modsync-abc", True, kw.pop("state", "idle"), kw.pop("pct", 100.0),
                          [DeviceStatus("DESKTOP1", "Desktop", True)], **kw)

    def test_copy_in_progress_hides_pairing_and_follows_the_copy(self):
        window, sync = self._window(copy_phase="receiving", copy_source="DESKTOP1",
                                    copy_archive=str(self.tmp / ".modsync-before-join" / "x"))
        self.assertEqual(sync.pair_tile.text(), "Stop copying…")
        self.assertFalse(hasattr(sync, "_code") and sync._code)
        sync.on_status(self.status(copy=CopyProgress("waiting", "Desktop")))
        self.assertEqual(sync.folder_state.text(), "Waiting for Desktop")
        sync.on_status(self.status(state="syncing", pct=40.0, copy=CopyProgress("receiving", "Desktop", 12)))
        self.assertEqual(sync.folder_state.text(), "Copying from Desktop")
        self.assertIn("12 items to go", sync.folder_detail.text())
        self.assertEqual(window.pages["home"].sync_row.badge.text(), "Copying")

    def test_a_finished_copy_rebuilds_into_the_normal_view(self):
        window, sync = self._window(copy_phase="setting-aside", copy_source="DESKTOP1")
        State(instance_path=str(self.tmp), folder_id="modsync-abc", set_aside=str(self.tmp / "a")).save()
        window.service.state = State.load()
        sync.on_status(self.status(copy=CopyProgress("done", "Desktop", set_aside=3)))
        self.settle()
        self.assertIn("Copy finished", window.last_message)
        self.assertIn("3 files from before are kept", window.last_message)
        sync = window.pages["sync"]
        self.assertEqual(sync.pair_tile.text(), "Pair over network…")
        self.assertIsNotNone(sync.set_aside_tile)

    def test_a_copy_finished_by_the_background_service_rebuilds_too(self):
        window, sync = self._window(copy_phase="receiving", copy_source="DESKTOP1")
        window.service.state = State(instance_path=str(self.tmp), folder_id="modsync-abc")
        sync.on_status(self.status())
        self.settle()
        self.assertEqual(window.pages["sync"].pair_tile.text(), "Pair over network…")

    def test_launch_is_blocked_while_copying(self):
        window, _sync = self._window(copy_phase="receiving", copy_source="DESKTOP1")
        with patch.object(ModSyncService, "launch_check",
                          return_value=LaunchCheck(blocked="Mods are still being copied from Desktop.")), \
                patch.object(window.service, "launch_mo2") as launch:
            window.launch(play=True)
            self.settle()
        launch.assert_not_called()
        self.assertIn("still being copied", window.last_message)
        self.assertFalse(window.busy)

    def test_warnings_ask_before_playing(self):
        window, _sync = self._window()
        check = LaunchCheck(warnings=["3 changes from another machine haven't arrived yet."])
        for key, launches in (("wait", False), ("sync", False), ("play", True)):
            with self.subTest(key), patch.object(ModSyncService, "launch_check", return_value=check), \
                    patch.object(window.service, "launch_mo2", return_value="ok") as launch:
                window.launch(play=True)
                self.settle()
                sheet = window.top_overlay
                self.assertEqual(sheet.title.text(), "Play anyway?")
                self.assertIn("haven't arrived", sheet.text.text())
                sheet.choose(key)
                self.settle()
                self.assertEqual(launch.called, launches)
                if key == "sync":
                    self.assertEqual(window._current, "sync")

    def test_nothing_to_say_launches_straight_away(self):
        window, _sync = self._window()
        with patch.object(ModSyncService, "launch_check", return_value=LaunchCheck()), \
                patch.object(window.service, "launch_mo2", return_value="ok") as launch:
            window.launch(play=False)
            self.settle()
        launch.assert_called_once_with(play=False)
        self.assertIsNone(window.top_overlay)

    def test_stop_syncing_offers_pause_and_paused_offers_resume(self):
        window, sync = self._window()
        sync.stop_sync()
        sheet = window.top_overlay
        self.assertEqual(list(sheet.tiles), ["pause", "stop", "cancel"])
        with patch.object(window, "change_setup") as change:
            sheet.choose("pause")
        self.assertEqual(change.call_args.args, (window.service.pause_sync, True))

        window, sync = self._window(sync_paused=True)
        self.assertIs(sync.preferred_focus(), sync.resume_tile)
        sync.on_status(self.status(state="paused", paused=True))
        self.assertEqual(sync.folder_state.text(), "Paused")
        self.assertEqual(window.pages["home"].sync_row.badge.text(), "Paused")
        sync.stop_sync()
        self.assertEqual(list(window.top_overlay.tiles), ["stop", "cancel"])

    def test_conflicts_are_reviewed_one_at_a_time(self):
        profile = self.tmp / "profiles" / "Default"
        profile.mkdir(parents=True)
        (profile / "modlist.txt").write_text("+Mine\n")
        (profile / f"modlist.{STAMP}.txt").write_text("+Theirs\n")
        (profile / "plugins.txt").write_text("*A.esp\n")
        (profile / f"plugins.{STAMP}.txt").write_text("A.esp\n")
        window, sync = self._window()
        sync.on_status(self.status(conflicts=2))
        self.assertTrue(sync.conflicts_tile.isVisibleTo(sync))
        self.assertEqual(sync.conflicts_tile.text(), "2 files were changed on two machines")
        self.assertEqual(window.pages["home"].sync_row.badge.text(), "2 conflicts")
        sync._names["AAAAAAA"] = "Desktop"
        sync.conflicts_tile.click()
        self.settle()
        sheet = window.top_overlay
        self.assertEqual(sheet.title.text(), "Mod list of profile Default")
        self.assertIn("Only in the version from Desktop: Theirs.", sheet.text.text())
        self.assertIn("1 more after this one", sheet.text.text())
        sheet.choose("other")
        self.settle(4)
        self.assertEqual((profile / "modlist.txt").read_text(), "+Theirs\n")
        sheet = window.top_overlay
        self.assertEqual(sheet.title.text(), "Plugins of profile Default")
        sheet.choose("current")
        self.settle(4)
        self.assertEqual((profile / "plugins.txt").read_text(), "*A.esp\n")
        self.assertEqual(window.service.conflicts(), [])
        self.assertEqual(window.last_message, "No conflicts left.")
        archived = sorted(p.name for p in (self.tmp / ".modsync-conflicts").rglob("*.txt"))
        self.assertEqual(archived, ["modlist.txt", f"plugins.{STAMP}.txt"])

    def test_set_aside_files_can_be_deleted(self):
        archive = self.tmp / ".modsync-before-join" / "20261004-101500"
        (archive / "mods").mkdir(parents=True)
        (archive / "mods" / "old.esp").write_text("x")
        window, sync = self._window(set_aside=str(archive))
        sync.set_aside_tile.click()
        self.assertIn(str(archive), window.top_overlay.text.text())
        window.top_overlay.choose("delete")
        self.settle(4)
        self.assertFalse(archive.exists())
        self.assertEqual(State.load().set_aside, "")
        self.assertIsNone(window.pages["sync"].set_aside_tile)
