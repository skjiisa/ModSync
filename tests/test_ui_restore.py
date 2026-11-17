"""The Restore points sheet: reached from the MO2 sheet and Sync, lists
restore points and archives, previews a restore before touching anything,
waits for MO2 and the games, and restores one profile whole."""

import os
from unittest.mock import patch

from modsync import playing, restore_points as rp
from modsync.mo2 import usvfs
from modsync.state import State
from modsync.ui.input import Action
from modsync.ui.overlays import ConfirmSheet, PageSheet
from tests.ui_support import UiTestCase


class RestoreSheetTests(UiTestCase):
    def setUp(self):
        super().setUp()
        os.environ["XDG_DATA_HOME"] = str(self.tmp / "data")
        self.addCleanup(os.environ.pop, "XDG_DATA_HOME", None)
        self.inst = self.tmp / "MO2"
        for mod in ("Alpha", "Beta"):
            (self.inst / "mods" / mod).mkdir(parents=True)
        (self.inst / "ModOrganizer.ini").write_text("[General]\ngameName=Skyrim Special Edition\n")
        self.write("Default", "+Alpha\n+Beta\n")
        self.running = False
        for p in (patch.object(usvfs, "in_use", side_effect=lambda _inst: self.running),
                  patch.object(playing, "game_running", return_value=False)):
            p.start()
            self.addCleanup(p.stop)

    def write(self, profile, modlist):
        folder = self.inst / "profiles" / profile
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "modlist.txt").write_text(modlist)
        (folder / "plugins.txt").write_text("*Alpha.esp\n")

    def modlist(self, profile="Default"):
        return (self.inst / "profiles" / profile / "modlist.txt").read_text()

    def open_sheet(self, state=None):
        (state or State(instance_path=str(self.inst))).save()
        window = self.window()
        window.go("mods")
        self.settle()
        mods = window.top_overlay
        window.pages["mods"].restore_tile.click()
        self.settle()
        sheet = window.top_overlay
        self.assertIsInstance(sheet, PageSheet)
        self.assertIsNot(sheet, mods)
        return window, window.pages["restore"]

    def test_the_mo2_sheet_opens_it_and_save_now_adds_a_point(self):
        window, page = self.open_sheet()
        self.assertEqual(page.rows, [])
        self.assertIn("None yet", page.status.text())
        self.assertIs(page.preferred_focus(), page.save_tile)
        self.assertIn(([Action.ALT], "Save now"), page.hints())
        page.save_tile.click()
        self.settle(4)
        self.assertEqual(len(page.rows), 1)
        self.assertIn("Saved by you. Profile Default.", page.rows[0].description)
        self.assertEqual(page.usage.text().split(",")[0], "1 restore point")
        self.assertIn("Saved a restore point of Default.", window.last_message)

    def test_preview_first_cancel_changes_nothing_restore_puts_it_back(self):
        point = rp.capture(self.inst, "play")
        self.write("Default", "+Alpha\n+Gamma\n")
        window, page = self.open_sheet()
        self.assertEqual(len(page.rows), 1)
        self.assertIs(window.focusWidget(), page.rows[0])  # not Save, which had it until the rows came
        page.rows[0].click()
        self.settle()
        confirm = window.top_overlay
        self.assertIsInstance(confirm, ConfirmSheet)
        self.assertEqual(confirm.title.text(), "Restore Default")
        text = confirm.text.text()
        self.assertIn("Only in the restore point: Beta.", text)
        self.assertIn("Only in the profile now: Gamma.", text)
        self.assertIn("so you can undo this", text)
        self.assertIs(window.focusWidget(), confirm.tiles["cancel"])
        confirm.choose("cancel")
        self.settle()
        self.assertEqual(self.modlist(), "+Alpha\n+Gamma\n")
        page.rows[0].click()
        self.settle()
        window.top_overlay.choose("restore")
        self.settle(4)
        self.assertEqual(self.modlist(), "+Alpha\n+Beta\n")
        self.assertIn("Restored Default.", " ".join(window.messages))
        ids = [row_point.id for row_point in page.points]
        self.assertEqual(len(ids), 2)  # how it was just before, then the one restored
        self.assertEqual(ids[1], point.id)
        self.assertEqual(page.points[0].reason, "restore")

    def test_waits_for_mo2_and_says_why(self):
        rp.capture(self.inst)
        self.write("Default", "+Alpha\n")
        window, page = self.open_sheet()
        self.running = True
        page.rows[0].click()
        self.settle()
        confirm = window.top_overlay
        self.assertEqual(list(confirm.tiles), ["close"])
        self.assertIn("Close Mod Organizer 2 and the game first.", confirm.text.text())
        self.assertEqual(self.modlist(), "+Alpha\n")

    def test_missing_mods_and_sync_are_named(self):
        rp.capture(self.inst)
        (self.inst / "mods" / "Beta").rmdir()
        self.write("Default", "+Alpha\n")
        window, page = self.open_sheet(State(instance_path=str(self.inst), folder_id="modsync-1", sync_paused=True))
        page.rows[0].click()
        self.settle()
        text = window.top_overlay.text.text()
        self.assertIn("1 mod it enables isn't in the instance any more: Beta.", text)
        self.assertIn("Sync is paused, so the restored files reach your other machines once you resume it.", text)

    def test_a_point_with_several_profiles_asks_which(self):
        self.write("Survival", "+Alpha\n")
        rp.capture(self.inst)
        self.write("Survival", "+Beta\n")
        window, page = self.open_sheet()
        self.assertIn("Profiles Default, Survival.", page.rows[0].description)
        page.rows[0].click()
        self.settle()
        picker = window.top_overlay
        self.assertEqual(list(picker.tiles), ["profile:Default", "profile:Survival", "cancel"])
        picker.choose("profile:Default")
        self.settle()
        self.assertIn("Profile Default is the same now. Restoring it changes nothing.", window.top_overlay.text.text())
        self.assertEqual(list(window.top_overlay.tiles), ["close"])

    def test_a_copys_set_aside_files_are_offered_to_open(self):
        old = self.inst / ".modsync-before-join" / "20261001-090000" / "mods" / "Old"
        old.mkdir(parents=True)
        (old / "old.esp").write_text("x")
        window, page = self.open_sheet()
        self.assertIn("1 file to open and move back by hand.", page.rows[0].description)
        with patch("modsync.ui.pages.restore.QDesktopServices.openUrl") as opened:
            page.rows[0].click()
            self.settle()
            window.top_overlay.choose("open")
        self.assertTrue(opened.call_args.args[0].toLocalFile().endswith("20261001-090000"))

    def test_without_an_instance_it_says_what_comes_first(self):
        window = self.window()
        page = window.pages["restore"]
        self.assertIsNone(page.save_tile)
        self.assertEqual(page.hints(), [])

    def test_sync_offers_it_too(self):
        State(instance_path=str(self.inst), folder_id="modsync-1").save()
        window = self.window()
        window.go("sync")
        window.pages["sync"].restore_tile.click()
        self.settle()
        self.assertIs(window.top_overlay.page, window.pages["restore"])
