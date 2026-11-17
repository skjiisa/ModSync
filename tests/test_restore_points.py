"""Restore points keep every MO2 profile's lists and settings outside the
instance, restore one profile whole, say what they can't bring back, stay
within their bounds, and see a restore that was cut short through."""

import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from modsync import config, playing, restore_points as rp
from modsync.mo2 import usvfs
from modsync.service import ModSyncService
from modsync.state import State

STAMP = "sync-conflict-20261004-101500-ABCDEFG"


class FakeClient:
    def __init__(self):
        self.folders = {"modsync-1": {"id": "modsync-1", "paused": False}}
        self.history = []  # (folder, paused) for every change

    def get_folder(self, folder_id):
        return dict(self.folders[folder_id])

    def put_folder(self, folder):
        self.folders[folder["id"]] = dict(folder)
        self.history.append((folder["id"], folder["paused"]))


class FakeManager:
    def __init__(self, running=True):
        self.running = running
        self.fake = FakeClient()

    def attach(self):
        return self.running

    @contextmanager
    def client(self):
        yield self.fake


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        os.environ["XDG_DATA_HOME"] = str(self.tmp / "data")
        self.inst = self.tmp / "MO2"
        (self.inst / "mods").mkdir(parents=True)
        self.write("ModOrganizer.ini", "[General]\ngameName=Skyrim Special Edition\n")
        self.profile("Default", modlist="+Alpha\n+Beta\n", plugins="*Alpha.esp\n*Beta.esp\n")
        for mod in ("Alpha", "Beta", "Gamma"):
            (self.inst / "mods" / mod).mkdir()

    def write(self, rel, text):
        path = self.inst / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def read(self, rel):
        return (self.inst / rel).read_text()

    def profile(self, name, *, modlist, plugins, loadorder="Skyrim.esm\n", settings="[General]\nLocalSaves=false\n"):
        self.write(f"profiles/{name}/modlist.txt", modlist)
        self.write(f"profiles/{name}/plugins.txt", plugins)
        self.write(f"profiles/{name}/loadorder.txt", loadorder)
        self.write(f"profiles/{name}/settings.ini", settings)


class CaptureTests(Base):
    def test_a_point_holds_every_profiles_lists_and_settings_outside_the_instance(self):
        self.profile("Survival", modlist="+Gamma\n", plugins="*Gamma.esp\n")
        self.write("profiles/Default/skyrim.ini", "[Display]\nfGamma=1\n")
        self.write("profiles/Default/saves/save1.ess", "a save")  # not ours to keep
        self.write(f"profiles/Default/modlist.{STAMP}.txt", "+Other\n")  # a conflict's other version
        self.write("profiles/Default/big.ini", "x" * (rp.MAX_FILE + 1))
        point = rp.capture(self.inst, "manual")
        self.assertEqual(point.profiles, ["Default", "Survival"])
        self.assertEqual(sorted(point.files["Default"]),
                         ["loadorder.txt", "modlist.txt", "plugins.txt", "settings.ini", "skyrim.ini"])
        self.assertTrue(point.path.is_relative_to(config.data_dir()))
        self.assertFalse(point.path.is_relative_to(self.inst))
        self.assertEqual(point.files["Default"]["modlist.txt"].read_text(), "+Alpha\n+Beta\n")
        self.assertEqual(point.reason, "manual")
        self.assertGreater(point.size, 0)

    def test_only_a_change_makes_a_new_point_unless_forced(self):
        first = rp.capture(self.inst)
        self.assertIsNotNone(first)
        self.assertIsNone(rp.capture(self.inst))
        self.write("profiles/Default/modlist.txt", "+Alpha\n-Beta\n")
        second = rp.capture(self.inst)
        self.assertIsNotNone(second)
        self.assertIsNotNone(rp.capture(self.inst, "manual", force=True))
        self.assertEqual([p.id for p in rp.points(self.inst)][1:], [second.id, first.id])

    def test_points_taken_in_the_same_second_stay_in_order(self):
        with patch.object(rp, "datetime") as dt:
            from datetime import datetime

            dt.now.return_value = datetime(2026, 11, 16, 14, 32, 5)
            dt.fromisoformat = datetime.fromisoformat
            ids = [rp.capture(self.inst, force=True).id for _ in range(11)]
        self.assertEqual(ids[1], "20261116-143205-2")
        self.assertEqual([p.id for p in rp.points(self.inst)], list(reversed(ids)))

    def test_old_points_go_whole_and_the_newest_always_stays(self):
        with patch.object(rp, "KEEP", 3):
            ids = []
            for n in range(5):
                self.write("profiles/Default/modlist.txt", f"+Alpha\n# {n}\n")
                ids.append(rp.capture(self.inst).id)
            self.assertEqual([p.id for p in rp.points(self.inst)], ids[:-4:-1])
        with patch.object(rp, "MAX_BYTES", 1):
            self.write("profiles/Default/modlist.txt", "+Alpha\n# again\n")
            newest = rp.capture(self.inst)
        self.assertEqual([p.id for p in rp.points(self.inst)], [newest.id])
        self.assertEqual(rp.usage(self.inst).points, 1)

    def test_a_restore_under_way_keeps_its_points(self):
        old = rp.capture(self.inst)
        rp.begin(self.inst, old, "Default", before=old.id)
        with patch.object(rp, "KEEP", 1):
            for n in range(3):
                self.write("profiles/Default/modlist.txt", f"+Alpha\n# {n}\n")
                rp.capture(self.inst)
        self.assertIn(old.id, [p.id for p in rp.points(self.inst)])


class PreviewTests(Base):
    def test_says_what_changes_and_which_mods_are_gone(self):
        point = rp.capture(self.inst)
        self.write("profiles/Default/modlist.txt", "+Alpha\n+Gamma\n")
        self.write("profiles/Default/archives.txt", "Alpha.bsa\n")
        (self.inst / "mods" / "Beta").rmdir()
        preview = rp.preview(self.inst, point, "Default")
        changes = {c.name: c for c in preview.changes}
        self.assertEqual(sorted(changes), ["archives.txt", "modlist.txt"])
        self.assertEqual(changes["archives.txt"].change, "removed")
        self.assertIn("Only in the restore point: Beta.", changes["modlist.txt"].details)
        self.assertIn("Only in the profile now: Gamma.", changes["modlist.txt"].details)
        self.assertEqual(preview.missing_mods, ["Beta"])
        lines = rp.preview_lines(preview)
        self.assertIn("archives.txt goes, since the restore point didn't have it.", lines)
        self.assertTrue(any("1 mod it enables isn't in the instance any more: Beta." in line for line in lines))

    def test_a_deleted_profile_comes_back_and_an_unchanged_one_says_so(self):
        point = rp.capture(self.inst)
        self.assertTrue(rp.preview(self.inst, point, "Default").unchanged)
        for path in (self.inst / "profiles" / "Default").iterdir():
            path.unlink()
        (self.inst / "profiles" / "Default").rmdir()
        preview = rp.preview(self.inst, point, "Default")
        self.assertFalse(preview.exists_now)
        self.assertIn("Profile Default isn't in the instance now. Restoring it brings it back.",
                      rp.preview_lines(preview))


class RestoreTests(Base):
    def test_a_whole_profile_comes_back_and_the_rest_stay(self):
        self.profile("Survival", modlist="+Gamma\n", plugins="*Gamma.esp\n")
        point = rp.capture(self.inst)
        self.write("profiles/Default/modlist.txt", "+Alpha\n")
        self.write("profiles/Default/plugins.txt", "*Alpha.esp\n")
        self.write("profiles/Default/lockedorder.txt", "Alpha.esp|1\n")
        self.write("profiles/Survival/modlist.txt", "+Gamma\n+Alpha\n")
        rp.apply(self.inst, point, "Default")
        self.assertEqual(self.read("profiles/Default/modlist.txt"), "+Alpha\n+Beta\n")
        self.assertEqual(self.read("profiles/Default/plugins.txt"), "*Alpha.esp\n*Beta.esp\n")
        self.assertFalse((self.inst / "profiles/Default/lockedorder.txt").exists())
        self.assertEqual(self.read("profiles/Survival/modlist.txt"), "+Gamma\n+Alpha\n")
        self.assertFalse(list((self.inst / "profiles/Default").glob(".syncthing.*")))

    def test_a_restore_cut_short_is_finished_or_undone(self):
        point = rp.capture(self.inst)
        self.write("profiles/Default/modlist.txt", "+Alpha\n")
        self.write("profiles/Default/plugins.txt", "*Alpha.esp\n")
        before = rp.capture(self.inst, "restore")
        rp.begin(self.inst, point, "Default", before=before.id)
        self.write("profiles/Default/modlist.txt", "+Alpha\n+Beta\n")  # cut short after the first file
        journal = rp.pending(self.inst)
        self.assertEqual(rp.finish(self.inst, journal), "finished")
        self.assertEqual(self.read("profiles/Default/plugins.txt"), "*Alpha.esp\n*Beta.esp\n")
        self.assertEqual(rp.pending(self.inst)["phase"], "done")
        # Done once: seeing it through again changes nothing made since.
        self.write("profiles/Default/modlist.txt", "+Alpha\n+Beta\n+Gamma\n")
        self.assertEqual(rp.finish(self.inst, rp.pending(self.inst)), "done already")
        self.assertEqual(self.read("profiles/Default/modlist.txt"), "+Alpha\n+Beta\n+Gamma\n")
        rp.end(self.inst)
        self.assertIsNone(rp.pending(self.inst))
        # The point it came from has gone since: back to how it was.
        gone = rp.Point("20000101-000000", "point", self.tmp / "gone", None)
        rp.begin(self.inst, gone, "Default", before=before.id)
        self.assertEqual(rp.finish(self.inst, rp.pending(self.inst)), "undone")
        self.assertEqual(self.read("profiles/Default/modlist.txt"), "+Alpha\n")

    def test_a_point_without_a_profile_removes_it(self):
        before = rp.capture(self.inst)
        self.profile("New", modlist="+Gamma\n", plugins="*Gamma.esp\n")
        self.write("profiles/New/saves/one.ess", "a save")
        preview = rp.preview(self.inst, before, "New")
        self.assertIn("Profile New wasn't in the instance then.", rp.preview_lines(preview)[0])
        rp.apply(self.inst, before, "New")
        self.assertEqual([p.name for p in (self.inst / "profiles" / "New").iterdir()], ["saves"])

    def test_a_file_that_cant_be_copied_keeps_no_point(self):
        rp.capture(self.inst)
        self.write("profiles/Default/settings.ini", "[General]\nLocalSaves=true\n")
        real = rp.shutil.copy2

        def copy2(src, dst):
            if Path(src).name == "settings.ini":
                raise PermissionError("denied")
            return real(src, dst)

        with patch.object(rp.shutil, "copy2", copy2), self.assertRaises(rp.CaptureError):
            rp.capture(self.inst)
        self.assertEqual(len(rp.points(self.inst)), 1)
        self.assertEqual(list(rp.store_dir(self.inst).glob(".new-*")), [])

    def test_an_unreadable_profile_folder_keeps_no_point(self):
        with patch.object(rp, "_profile_files", side_effect=PermissionError("denied")), \
                self.assertRaises(rp.CaptureError):
            rp.capture(self.inst)
        self.assertEqual(rp.points(self.inst), [])


class ArchiveTests(Base):
    def test_a_conflicts_lists_are_restorable_and_other_archives_are_to_open(self):
        archive = self.inst / ".modsync-conflicts" / "20261004-101500"
        (archive / "profiles" / "Default").mkdir(parents=True)
        (archive / "profiles" / "Default" / f"modlist.{STAMP}.txt").write_text("+Gamma\n")
        (archive / "profiles" / "Default" / "plugins.txt").write_text("*Gamma.esp\n")
        (archive / "mods" / "Alpha").mkdir(parents=True)
        (archive / "mods" / "Alpha" / "a.ini").write_text("x")
        copy = self.inst / ".modsync-before-join" / "20261001-090000"
        (copy / "mods" / "Old").mkdir(parents=True)
        (copy / "mods" / "Old" / "old.esp").write_text("x")
        found = {p.kind: p for p in rp.points(self.inst)}
        conflict = found["conflict"]
        self.assertEqual(conflict.id, "conflict-20261004-101500")
        self.assertEqual(sorted(conflict.files["Default"]), ["modlist.txt", "plugins.txt"])
        self.assertEqual(conflict.other_files, 1)
        self.assertFalse(conflict.whole)
        self.assertFalse(found["copy"].restorable)
        self.assertEqual(found["copy"].other_files, 1)
        preview = rp.preview(self.inst, conflict, "Default")
        self.assertEqual(sorted(c.name for c in preview.changes), ["modlist.txt", "plugins.txt"])
        self.assertIn("Only these files change. The profile's other files stay as they are.",
                      rp.preview_lines(preview))
        rp.apply(self.inst, conflict, "Default")
        self.assertEqual(self.read("profiles/Default/modlist.txt"), "+Gamma\n")
        self.assertTrue((self.inst / "profiles/Default/settings.ini").exists())  # left alone


class ServiceTests(Base):
    def setUp(self):
        super().setUp()
        self.manager = FakeManager()
        State.update(instance_path=str(self.inst), folder_id="modsync-1")
        self.svc = ModSyncService(manager=self.manager)
        for p in (patch.object(usvfs, "in_use", return_value=False),
                  patch.object(playing, "game_running", return_value=False),
                  patch.object(self.svc.launcher, "running", return_value=False)):
            p.start()
            self.addCleanup(p.stop)

    def test_restoring_keeps_how_it_was_and_pauses_sync_until_the_profile_is_whole(self):
        point = rp.capture(self.inst)
        self.write("profiles/Default/modlist.txt", "+Alpha\n")
        seen = []
        real = rp._apply

        def watched(instance, pt, profile):
            seen.append(self.manager.fake.folders["modsync-1"]["paused"])
            return real(instance, pt, profile)

        with patch.object(rp, "_apply", watched):
            outcome = self.svc.restore_profile(point.id, "Default")
        self.assertEqual(seen, [True])
        self.assertEqual(self.manager.fake.history, [("modsync-1", True), ("modsync-1", False)])
        self.assertEqual(self.read("profiles/Default/modlist.txt"), "+Alpha\n+Beta\n")
        self.assertTrue(outcome.synced)
        self.assertFalse(outcome.paused)
        before = rp.find(self.inst, outcome.before)
        self.assertEqual(before.reason, "restore")
        self.assertEqual(before.files["Default"]["modlist.txt"].read_text(), "+Alpha\n")
        self.assertIsNone(rp.pending(self.inst))
        # ...and that restore point undoes it.
        self.svc.restore_profile(outcome.before, "Default")
        self.assertEqual(self.read("profiles/Default/modlist.txt"), "+Alpha\n")

    def test_a_vault_the_user_paused_stays_paused(self):
        point = rp.capture(self.inst)
        State.update(sync_paused=True)
        self.manager.fake.folders["modsync-1"]["paused"] = True
        outcome = self.svc.restore_profile(point.id, "Default")
        self.assertEqual(self.manager.fake.history, [])
        self.assertTrue(outcome.paused)

    def test_without_syncthing_running_nothing_is_paused(self):
        self.manager.running = False
        point = rp.capture(self.inst)
        self.svc.restore_profile(point.id, "Default")
        self.assertEqual(self.manager.fake.history, [])

    def test_waits_for_mo2_the_game_and_a_copy(self):
        point = rp.capture(self.inst)
        self.write("profiles/Default/modlist.txt", "+Alpha\n")
        with patch.object(usvfs, "in_use", return_value=True), self.assertRaisesRegex(RuntimeError, "Close Mod"):
            self.svc.restore_profile(point.id, "Default")
        with patch.object(playing, "game_running", return_value=True), \
                self.assertRaisesRegex(RuntimeError, "Close the game"):
            self.svc.restore_profile(point.id, "Default")
        State.update(copy_phase="receiving")
        with self.assertRaisesRegex(RuntimeError, "still being copied"):
            self.svc.restore_profile(point.id, "Default")
        self.assertEqual(self.read("profiles/Default/modlist.txt"), "+Alpha\n")  # nothing changed
        self.assertEqual(len(rp.points(self.inst)), 1)

    def test_a_failed_restore_puts_the_profile_back_and_resumes_sync(self):
        point = rp.capture(self.inst)
        self.write("profiles/Default/modlist.txt", "+Alpha\n")
        point.files["Default"]["plugins.txt"].unlink()  # the point is damaged
        with self.assertRaises(rp.RestoreError):
            self.svc.restore_profile(point.id, "Default")
        self.assertEqual(self.read("profiles/Default/modlist.txt"), "+Alpha\n")
        self.assertFalse(self.manager.fake.folders["modsync-1"]["paused"])
        self.assertIsNone(rp.pending(self.inst))

    def test_a_restore_cut_short_finishes_and_resumes_the_folder_it_paused(self):
        point = rp.capture(self.inst)
        self.write("profiles/Default/modlist.txt", "+Alpha\n")
        before = rp.capture(self.inst, "restore")
        rp.begin(self.inst, point, "Default", before=before.id)
        rp.note(self.inst, resume="modsync-1")
        self.manager.fake.folders["modsync-1"]["paused"] = True  # then the power went
        self.assertEqual(self.svc.finish_interrupted_restore(), "finished")
        self.assertEqual(self.read("profiles/Default/modlist.txt"), "+Alpha\n+Beta\n")
        self.assertFalse(self.manager.fake.folders["modsync-1"]["paused"])
        self.assertIsNone(rp.pending(self.inst))

    def test_a_restore_cut_short_waits_for_mo2_to_close(self):
        point = rp.capture(self.inst)
        self.write("profiles/Default/modlist.txt", "+Alpha\n")
        rp.begin(self.inst, point, "Default", before=point.id)
        with patch.object(usvfs, "in_use", return_value=True):
            self.assertEqual(self.svc.finish_interrupted_restore(), "")
        self.assertEqual(self.read("profiles/Default/modlist.txt"), "+Alpha\n")
        self.assertEqual(self.svc.finish_interrupted_restore(), "finished")

    def test_until_syncthing_runs_the_journal_waits_to_resume_it(self):
        point = rp.capture(self.inst)
        rp.begin(self.inst, point, "Default", before=point.id)
        rp.note(self.inst, resume="modsync-1")
        self.manager.running = False
        self.svc.finish_interrupted_restore()
        self.assertIsNotNone(rp.pending(self.inst))
        self.manager.running = True
        self.manager.fake.folders["modsync-1"]["paused"] = True
        self.svc.finish_interrupted_restore()
        self.assertFalse(self.manager.fake.folders["modsync-1"]["paused"])
        self.assertIsNone(rp.pending(self.inst))

    def test_one_restore_at_a_time_and_finishing_waits_for_it(self):
        point = rp.capture(self.inst)
        self.write("profiles/Default/modlist.txt", "+Alpha\n")
        rp.begin(self.inst, point, "Default", before=point.id)
        with rp.transaction(self.inst) as mine:  # another process's restore, under way
            self.assertTrue(mine)
            self.assertEqual(self.svc.finish_interrupted_restore(), "")
            with self.assertRaisesRegex(RuntimeError, "Another restore"):
                self.svc.restore_profile(point.id, "Default")
        self.assertEqual(self.read("profiles/Default/modlist.txt"), "+Alpha\n")
        self.assertEqual(rp.pending(self.inst)["phase"], "apply")

    def test_nothing_is_written_unless_syncthing_confirms_the_pause(self):
        point = rp.capture(self.inst)
        self.write("profiles/Default/modlist.txt", "+Alpha\n")
        client = self.manager.fake
        real = client.put_folder

        def lost(folder):  # paused, but the answer never came
            real(folder)
            raise OSError("connection reset")

        for broken in ({"get_folder": lambda fid: (_ for _ in ()).throw(OSError("refused"))},
                       {"put_folder": lost}):
            with self.subTest(broken=list(broken)), patch.multiple(client, **broken):
                with self.assertRaisesRegex(RuntimeError, "nothing was restored"):
                    self.svc.restore_profile(point.id, "Default")
                journal = rp.pending(self.inst)
                # Whether it paused is unknown: only resuming is left, to try again.
                self.assertTrue(journal is None or journal == {**journal, "phase": "done", "resume": "modsync-1"})
            self.svc.finish_interrupted_restore()
            self.assertEqual(self.read("profiles/Default/modlist.txt"), "+Alpha\n")
            self.assertFalse(client.folders["modsync-1"]["paused"])
            self.assertIsNone(rp.pending(self.inst))

    def test_a_resume_that_fails_is_retried_without_restoring_again(self):
        point = rp.capture(self.inst)
        self.write("profiles/Default/modlist.txt", "+Alpha\n")
        real = State.change
        calls = []

        def flaky(fn):
            calls.append(fn)
            if len(calls) == 2:  # the resume
                raise OSError("Syncthing went away")
            return real(fn)

        with patch.object(State, "change", flaky):
            self.svc.restore_profile(point.id, "Default")
        self.assertEqual(rp.pending(self.inst)["phase"], "done")
        self.assertTrue(self.manager.fake.folders["modsync-1"]["paused"])
        self.write("profiles/Default/modlist.txt", "+Alpha\n+Beta\n+Gamma\n")  # changed since in MO2
        self.assertEqual(self.svc.finish_interrupted_restore(), "done already")
        self.assertEqual(self.read("profiles/Default/modlist.txt"), "+Alpha\n+Beta\n+Gamma\n")
        self.assertFalse(self.manager.fake.folders["modsync-1"]["paused"])
        self.assertIsNone(rp.pending(self.inst))

    def test_a_folder_paused_for_a_game_stays_paused_until_the_restore_ends(self):
        point = rp.capture(self.inst)
        self.write("profiles/Default/modlist.txt", "+Alpha\n")
        State.update(paused_for_play=["modsync-1"])
        self.manager.fake.folders["modsync-1"]["paused"] = True
        seen = []
        real = rp._apply

        def mid_restore(instance, pt, profile):
            self.svc.resume_after_play()  # the background service, the game having closed
            seen.append(self.manager.fake.folders["modsync-1"]["paused"])
            return real(instance, pt, profile)

        with patch.object(rp, "_apply", mid_restore):
            self.svc.restore_profile(point.id, "Default")
        self.assertEqual(seen, [True])
        self.assertEqual(State.load().paused_for_play, ["modsync-1"])
        self.assertEqual(self.svc.resume_after_play(), ["modsync-1"])  # now it may

    def test_the_point_restored_from_isnt_pruned_by_the_one_taken_first(self):
        with patch.object(rp, "KEEP", 2):
            oldest = rp.capture(self.inst)
            self.write("profiles/Default/modlist.txt", "+Alpha\n")
            rp.capture(self.inst)
            self.write("profiles/Default/modlist.txt", "+Beta\n")
            self.svc.restore_profile(oldest.id, "Default")
        self.assertEqual(self.read("profiles/Default/modlist.txt"), "+Alpha\n+Beta\n")

    def test_restoring_into_an_instance_with_no_profiles_can_be_undone(self):
        point = rp.capture(self.inst)
        for path in (self.inst / "profiles" / "Default").iterdir():
            path.unlink()
        (self.inst / "profiles" / "Default").rmdir()
        outcome = self.svc.restore_profile(point.id, "Default")
        self.assertNotEqual(outcome.before, point.id)
        self.assertEqual(rp.find(self.inst, outcome.before).profiles, [])
        self.svc.restore_profile(outcome.before, "Default")
        self.assertFalse((self.inst / "profiles" / "Default").exists())

    def test_points_are_taken_before_playing_and_before_a_profile_conflict(self):
        from modsync.sync import conflicts

        with patch("modsync.service.build_plan", side_effect=RuntimeError("stop here")):
            with self.assertRaises(RuntimeError):
                self.svc.prepare_mo2(play=True)
        self.assertEqual(rp.points(self.inst), [])  # the plan comes first: nothing to play, nothing taken
        with patch("modsync.service.build_plan"), patch("modsync.service.vcruntime"), \
                patch("modsync.service.mark_portable"), patch("modsync.service.plan_system32"):
            self.svc.prepare_mo2(play=True)
        self.assertEqual([p.reason for p in rp.points(self.inst)], ["play"])
        self.write("profiles/Default/modlist.txt", "+Alpha\n")
        self.write(f"profiles/Default/modlist.{STAMP}.txt", "+Beta\n")
        found = conflicts.find(self.inst)
        self.svc.resolve_conflicts(found, "current")
        self.assertEqual([p.reason for p in rp.points(self.inst, archives=False)], ["conflict", "play"])

    def test_a_point_that_cant_be_taken_never_stops_what_comes_next(self):
        with patch.object(rp, "capture", side_effect=OSError("disk full")):
            self.svc.take_restore_point("play")  # logged, not raised


class CliTests(Base):
    def setUp(self):
        super().setUp()
        State.update(instance_path=str(self.inst))
        for p in (patch.object(usvfs, "in_use", return_value=False),
                  patch.object(playing, "game_running", return_value=False)):
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *args):
        from modsync.__main__ import main

        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["restore", *args])
        return code, out.getvalue()

    def test_list_show_apply_and_save(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("None yet.", out)
        code, out = self.run_cli("save")
        self.assertEqual(code, 0)
        point = rp.points(self.inst)[0]
        self.assertIn(f"Kept restore point {point.id} of Default.", out)
        self.write("profiles/Default/modlist.txt", "+Alpha\n")
        code, out = self.run_cli("list")
        self.assertIn(point.id, out)
        self.assertIn("Saved by you", out)
        self.assertIn(rp.LIMITS, out)
        code, out = self.run_cli("show", point.id)
        self.assertEqual(code, 0)
        self.assertIn("Only in the restore point: Beta.", out)
        code, out = self.run_cli("apply", point.id, "Default")
        self.assertEqual(code, 0, out)
        self.assertIn("Restored profile Default", out)
        self.assertEqual(self.read("profiles/Default/modlist.txt"), "+Alpha\n+Beta\n")
        code, out = self.run_cli("apply", point.id, "Nope")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was restored", out)
        code, out = self.run_cli("show", "nope")
        self.assertEqual(code, 1)
        self.assertEqual(self.run_cli("apply", point.id)[0], 2)

    def test_meta_from_a_newer_modsync_is_left_alone(self):
        point = rp.capture(self.inst)
        meta = json.loads((point.path / "meta.json").read_text())
        meta["schema"] = rp.SCHEMA + 1
        (point.path / "meta.json").write_text(json.dumps(meta))
        self.assertEqual(rp.points(self.inst), [])
        self.assertTrue(point.path.exists())


if __name__ == "__main__":
    unittest.main()
