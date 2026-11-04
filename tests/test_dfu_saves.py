"""Daggerfall Unity saves through the library: two machines (two Saves folders)
that share one library, as Syncthing would leave it, never get a save mixed
from two."""

import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from modsync import dfu, dfu_saves
from modsync.state import State


def make_save(saves: Path, slot: str, character: str, name: str, real_time: int, body: str) -> Path:
    folder = saves / slot
    folder.mkdir(parents=True, exist_ok=True)
    info = {"saveVersion": 1, "saveName": name, "characterName": character,
            "dateAndTime": {"gameTime": 1, "realTime": real_time}, "dfuVersion": "1.1.1"}
    (folder / "SaveInfo.txt").write_text(json.dumps(info))
    for f in ("SaveData.txt", "QuestData.txt", "FactionData.txt"):
        (folder / f).write_text(f"{f} {body}")
    stamp = 1_700_000_000 + real_time % 100_000  # each save's files written together
    for p in folder.iterdir():
        os.utime(p, (stamp, stamp))
    return folder


def contents(slot: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(slot.iterdir())}


def slots(saves: Path) -> dict[str, str]:
    """slot -> "character: name @ time" for every save DFU would list."""
    out = {}
    for p in sorted(saves.iterdir()):
        info = dfu_saves.read_info(p) if p.name.startswith("SAVE") else None
        if info:
            out[p.name] = f"{info.character}: {info.name} @ {info.real_time}"
    return out


def version_dirs(key_dir: Path) -> list[Path]:
    """A save's versions whose files are in the library, oldest first."""
    return sorted(p for p in key_dir.iterdir() if p.is_dir())


def ident(version: Path) -> dict:
    return json.loads(version.with_name(version.name + ".id").read_text())


@contextmanager
def _reason(text):
    yield text


def busy_guard(fn):
    """A sync guard from a function giving the reason (or "")."""
    return lambda: _reason(fn())


class TwoMachines(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.library = self.tmp / "library"
        self.a, self.b = self.tmp / "A" / "Saves", self.tmp / "B" / "Saves"
        self.a.mkdir(parents=True)
        self.b.mkdir(parents=True)
        self.machines = {self.a: dfu_saves.Machine(self.tmp / "A.json"), self.b: dfu_saves.Machine(self.tmp / "B.json")}

    def sync(self, saves, **kw):
        return dfu_saves.sync(saves, self.library, self.machines[saves], **kw)

    def round(self):
        """A, then B, then A again: what a few sync passes on each settle to."""
        self.sync(self.a)
        self.sync(self.b)
        self.sync(self.a)

    def test_different_saves_in_the_same_slot_both_survive_whole(self):
        mine = make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "from A")
        theirs = make_save(self.b, "SAVE0", "Bjorn", "Bjorn 1", 200, "from B")
        mine_files, theirs_files = contents(mine), contents(theirs)
        self.round()
        for saves in (self.a, self.b):
            got = slots(saves)
            self.assertEqual(sorted(got.values()), ["Bjorn: Bjorn 1 @ 200", "Ellte: Ellte 1 @ 100"])
            for slot, label in got.items():
                expected = mine_files if label.startswith("Ellte") else theirs_files
                self.assertEqual(contents(saves / slot), expected)  # whole, never mixed
        self.assertEqual(slots(self.a)["SAVE0"], "Ellte: Ellte 1 @ 100")  # local slots stay where they were
        self.assertEqual(slots(self.b)["SAVE0"], "Bjorn: Bjorn 1 @ 200")

    def test_a_quicksave_on_each_machine_the_newest_wins_and_the_other_is_kept(self):
        make_save(self.a, "SAVE3", "Ellte", "QuickSave", 300, "A's quicksave")
        make_save(self.b, "SAVE0", "Ellte", "QuickSave", 400, "B's newer quicksave")
        self.round()
        for saves, slot in ((self.a, "SAVE3"), (self.b, "SAVE0")):
            self.assertEqual(slots(saves), {slot: "Ellte: QuickSave @ 400"})
            self.assertEqual((saves / slot / "SaveData.txt").read_text(), "SaveData.txt B's newer quicksave")
        key = dfu_saves.Info("Ellte", "QuickSave", 0).key
        versions = version_dirs(self.library / key)
        self.assertEqual(len(versions), 2)  # A's older one can still be had
        self.assertEqual((versions[0] / "SaveData.txt").read_text(), "SaveData.txt A's quicksave")

    def test_an_update_on_one_machine_replaces_the_copy_on_the_other(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        self.round()
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 150, "v2")
        self.round()
        self.assertEqual(slots(self.b), {"SAVE0": "Ellte: Ellte 1 @ 150"})
        self.assertEqual((self.b / "SAVE0" / "SaveData.txt").read_text(), "SaveData.txt v2")
        report = self.sync(self.b)
        self.assertFalse(report.changed)  # settled: nothing goes back and forth

    def test_a_deletion_reaches_the_other_machine(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        make_save(self.a, "SAVE1", "Ellte", "Ellte 2", 110, "v1")
        self.round()
        b_slot = next(s for s, label in slots(self.b).items() if "Ellte 1" in label)  # B numbers its own
        shutil.rmtree(self.a / "SAVE0")
        self.sync(self.a)
        report = self.sync(self.b)
        self.assertEqual(report.removed, [b_slot])
        self.assertEqual(list(slots(self.b).values()), ["Ellte: Ellte 2 @ 110"])
        self.assertNotIn(b_slot, json.loads(dfu_saves.mapping_path(self.b, self.library).read_text())["slots"])

    def test_a_save_made_again_after_its_deletion_comes_back(self):
        make_save(self.a, "SAVE0", "Ellte", "QuickSave", 100, "v1")
        self.round()
        shutil.rmtree(self.a / "SAVE0")
        self.sync(self.a)
        make_save(self.b, "SAVE0", "Ellte", "QuickSave", 300, "made again on B")  # B quicksaved after
        self.sync(self.b)
        self.sync(self.a)
        self.assertEqual(slots(self.a), {"SAVE0": "Ellte: QuickSave @ 300"})
        key = dfu_saves.Info("Ellte", "QuickSave", 0).key
        found = dfu_saves.records(self.library / key)
        self.assertTrue(any(r.deleted for r in found))  # deletions are kept...
        self.assertFalse(dfu_saves.winner(found).deleted)  # ...but this one no longer decides

    def test_a_slot_reused_for_another_save_deletes_the_old_one_everywhere(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        self.round()
        shutil.rmtree(self.a / "SAVE0")
        make_save(self.a, "SAVE0", "Bjorn", "Bjorn 1", 120, "new")  # DFU takes the first free number
        self.sync(self.a)
        self.sync(self.b)
        self.assertEqual(slots(self.b), {"SAVE0": "Bjorn: Bjorn 1 @ 120"})

    def test_a_deletion_noticed_late_never_beats_a_newer_save(self):
        make_save(self.a, "SAVE0", "Ellte", "QuickSave", 100, "v1")
        self.round()
        shutil.rmtree(self.a / "SAVE0")  # deleted in DFU at some point after 100...
        make_save(self.b, "SAVE0", "Ellte", "QuickSave", 200, "B saved again")  # ...B saves at 200
        self.sync(self.b)
        self.sync(self.a)  # A notices the deletion only now
        self.sync(self.b)
        self.assertEqual(slots(self.b), {"SAVE0": "Ellte: QuickSave @ 200"})
        self.assertEqual(slots(self.a), {"SAVE0": "Ellte: QuickSave @ 200"})

    def test_an_interrupted_swap_is_put_back_not_deleted(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        self.round()
        before = contents(self.b / "SAVE0")
        os.rename(self.b / "SAVE0", self.b / ".modsync-old-SAVE0")  # killed mid-swap
        (self.b / ".modsync-incoming-SAVE0").mkdir()
        report = self.sync(self.b)
        self.assertEqual(report.deleted, [])
        self.assertEqual(contents(self.b / "SAVE0"), before)
        self.assertFalse((self.b / ".modsync-old-SAVE0").exists())
        self.assertFalse((self.b / ".modsync-incoming-SAVE0").exists())
        self.sync(self.a)
        self.assertEqual(slots(self.a), {"SAVE0": "Ellte: Ellte 1 @ 100"})

    def test_a_save_dfu_cant_read_is_left_alone(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        self.round()
        (self.b / "SAVE0" / "SaveInfo.txt").write_text("{trunc")
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 150, "v2")
        self.sync(self.a)
        report = self.sync(self.b)
        self.assertEqual((report.deleted, report.placed), ([], []))
        self.assertEqual((self.b / "SAVE0" / "SaveInfo.txt").read_text(), "{trunc")  # not overwritten either
        self.sync(self.a)
        self.assertEqual(slots(self.a), {"SAVE0": "Ellte: Ellte 1 @ 150"})

    def test_leftovers_of_an_interrupted_store_are_cleaned_up(self):
        (self.library / ".incoming-dead").mkdir(parents=True)
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        self.sync(self.a)
        self.assertFalse((self.library / ".incoming-dead").exists())

    def test_a_version_still_arriving_is_not_used(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        self.sync(self.a)
        key = dfu_saves.Info("Ellte", "Ellte 1", 0).key
        (version,) = version_dirs(self.library / key)
        data = (version / "SaveData.txt").read_text()
        (version / "SaveData.txt").write_text(data[:3])  # half of it so far
        self.sync(self.b)
        self.assertEqual(slots(self.b), {})
        (version / "SaveData.txt").write_text(data)  # the rest arrives
        self.sync(self.b)
        self.assertEqual(slots(self.b), {"SAVE0": "Ellte: Ellte 1 @ 100"})

    def test_only_the_newest_few_versions_are_kept(self):
        for t in range(10, 70, 10):
            make_save(self.a, "SAVE0", "Ellte", "AutoSave", t, f"v{t}")
            self.sync(self.a)
        key = dfu_saves.Info("Ellte", "AutoSave", 0).key
        kept = [int(p.name.split("-")[0]) for p in version_dirs(self.library / key)]
        self.assertEqual(kept, [30, 40, 50, 60])  # the one in use and the three before it
        ids = sorted(p.name for p in (self.library / key).glob("*.id"))
        self.assertEqual(len(ids), 6)  # every version is still known
        mapping = json.loads(dfu_saves.mapping_path(self.a, self.library).read_text())
        self.assertTrue(all((self.library / v).is_dir() for v in mapping["verified"]))

    def test_dfu_never_sees_modsyncs_files(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        (self.a / "SAVE1").mkdir()  # a folder DFU would skip: no SaveInfo
        self.round()
        names = {p.name for p in self.a.iterdir()}
        self.assertTrue({n for n in names if not n.startswith("SAVE")}
                        <= {dfu_saves.mapping_path(self.a, self.library).name, ".modsync-saves.lock"})
        self.assertNotIn("SAVE1", slots(self.b))
        self.assertFalse((self.b / "SAVE0" / dfu_saves.MANIFEST).exists())

    def test_versions_have_a_manifest_and_unique_names(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        self.sync(self.a)
        key = dfu_saves.Info("Ellte", "Ellte 1", 0).key
        (version,) = version_dirs(self.library / key)
        self.assertEqual(version.name, f"{100:020d}-{self.machines[self.a].id}")
        self.assertEqual(ident(version)["vv"], {self.machines[self.a].id: 1})
        manifest = json.loads((version / dfu_saves.MANIFEST).read_text())
        self.assertEqual(manifest["character"], "Ellte")
        self.assertEqual(ident(version)["digest"], dfu_saves.digest(manifest["files"]))
        self.assertEqual(set(manifest["files"]), {"SaveInfo.txt", "SaveData.txt", "QuestData.txt", "FactionData.txt"})
        self.assertFalse(any(p.name.startswith(".incoming") for p in self.library.iterdir()))

    # --- from GPT-6.1 Sol's review ---

    def test_a_save_continued_with_an_earlier_clock_still_wins(self):
        """DFU's times are local time: the end of summer time, or another time
        zone, can give later progress an earlier time."""
        make_save(self.a, "SAVE0", "Ellte", "QuickSave", 500, "A's progress")
        self.round()
        make_save(self.b, "SAVE0", "Ellte", "QuickSave", 400, "B played on from it")
        self.round()
        for saves in (self.a, self.b):
            self.assertEqual((saves / "SAVE0" / "SaveData.txt").read_text(), "SaveData.txt B played on from it")
        for _ in range(4):  # and pruning never takes it away
            self.round()
        self.assertEqual((self.a / "SAVE0" / "SaveData.txt").read_text(), "SaveData.txt B played on from it")

    def test_a_version_half_pruned_elsewhere_never_replaces_newer_progress(self):
        key = dfu_saves.Info("Ellte", "AutoSave", 0).key
        for t in (100, 200, 300):
            make_save(self.a, "SAVE0", "Ellte", "AutoSave", t, f"v{t}")
            self.sync(self.a)
        versions = version_dirs(self.library / key)
        # Another machine pruned 200 and 300; Syncthing has removed part of 100 so far.
        shutil.rmtree(versions[1])
        shutil.rmtree(versions[2])
        (versions[0] / "QuestData.txt").unlink()
        self.sync(self.a)
        self.assertEqual((self.a / "SAVE0" / "SaveData.txt").read_text(), "SaveData.txt v300")
        report = self.sync(self.b)  # a machine that never had it doesn't get the broken one either
        self.assertEqual(slots(self.b), {})
        self.assertEqual(report.placed, [])

    def test_a_version_damaged_after_it_was_checked_is_never_placed(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        self.sync(self.a)
        self.sync(self.b)
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 200, "v2")
        self.sync(self.a)
        key = dfu_saves.Info("Ellte", "Ellte 1", 0).key
        newest = version_dirs(self.library / key)[-1]
        # B found it whole once (cached), then a file went missing before B used it.
        original = dfu_saves.available
        with patch.object(dfu_saves, "available", lambda r, v=None: (original(r, v),
                          (newest / "FactionData.txt").write_text("damaged"))[0]):
            report = self.sync(self.b)
        self.assertEqual(report.placed, [])
        self.assertEqual((self.b / "SAVE0" / "SaveData.txt").read_text(), "SaveData.txt v1")

    def test_two_libraries_on_one_machine_never_roll_a_save_back(self):
        """The Flatpak and a native ModSync on one machine: one Saves folder,
        two libraries."""
        other = self.tmp / "other-library"
        flatpak = dfu_saves.Machine(self.tmp / "C.json")
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v100")
        dfu_saves.sync(self.a, other, flatpak)
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 200, "v200")
        self.sync(self.a)
        dfu_saves.sync(self.a, other, flatpak)
        self.sync(self.a)
        self.assertEqual((self.a / "SAVE0" / "SaveData.txt").read_text(), "SaveData.txt v200")
        self.assertNotEqual(dfu_saves.mapping_path(self.a, self.library), dfu_saves.mapping_path(self.a, other))

    def test_nothing_is_done_once_dfu_turns_out_to_be_running(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        report = self.sync(self.a, guard=busy_guard(lambda: "DFU is running"))
        self.assertEqual(report.skipped, "DFU is running")
        self.assertEqual(list(self.library.glob("*/*")), [])

    def test_a_slot_is_swapped_in_one_step(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        self.round()
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 200, "v2")
        self.sync(self.a)
        renames = []
        original = os.rename
        with patch.object(dfu_saves.os, "rename", lambda a, b: (renames.append((a, b)), original(a, b))):
            self.sync(self.b)
        self.assertEqual((self.b / "SAVE0" / "SaveData.txt").read_text(), "SaveData.txt v2")
        self.assertFalse(any(str(b).endswith("SAVE0") or str(a).endswith("SAVE0") for a, b in renames))
        self.assertEqual([p.name for p in self.b.iterdir() if p.name.startswith(".modsync-incoming")], [])

    def test_without_the_one_step_swap_it_still_works(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        self.round()
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 200, "v2")
        self.sync(self.a)
        with patch.object(dfu_saves, "_exchange", lambda a, b: False):
            self.sync(self.b)
        self.assertEqual((self.b / "SAVE0" / "SaveData.txt").read_text(), "SaveData.txt v2")
        self.assertFalse((self.b / ".modsync-old-SAVE0").exists())

    # --- from GPT-6.1 Sol's re-check ---

    def test_no_amount_of_history_lets_an_old_save_win_again(self):
        make_save(self.a, "SAVE0", "Ellte", "AutoSave", 1000, "first")
        self.round()
        for t in range(1, 70):  # the clock went back, then many saves made from each other
            make_save(self.a, "SAVE0", "Ellte", "AutoSave", t, f"v{t}")
            self.round()
        for saves in (self.a, self.b):
            self.assertEqual((saves / "SAVE0" / "SaveData.txt").read_text(), "SaveData.txt v69")

    def test_a_deleted_save_never_comes_back_through_pruning(self):
        make_save(self.a, "SAVE0", "Ellte", "QuickSave", 1000, "deleted one")
        self.round()
        shutil.rmtree(self.a / "SAVE0")
        self.round()
        make_save(self.a, "SAVE0", "Ellte", "QuickSave", 1100, "made again")
        self.round()
        make_save(self.a, "SAVE0", "Ellte", "QuickSave", 900, "played on, clock behind")
        for _ in range(3):
            self.round()
        for saves in (self.a, self.b):
            self.assertEqual((saves / "SAVE0" / "SaveData.txt").read_text(), "SaveData.txt played on, clock behind")

    def test_a_lost_mapping_never_publishes_an_old_save_as_new(self):
        make_save(self.a, "SAVE0", "Ellte", "QuickSave", 1000, "v1000")
        self.round()
        make_save(self.a, "SAVE0", "Ellte", "QuickSave", 900, "played on")
        self.sync(self.a)
        dfu_saves.mapping_path(self.b, self.library).unlink()
        key = dfu_saves.Info("Ellte", "QuickSave", 0).key
        before = {p.name for p in (self.library / key).iterdir()}
        self.sync(self.b)
        self.round()
        self.assertFalse({p.name for p in (self.library / key).iterdir()} - before)  # nothing new from B
        for saves in (self.a, self.b):
            self.assertEqual((saves / "SAVE0" / "SaveData.txt").read_text(), "SaveData.txt played on")

    def test_a_removal_cut_short_never_publishes_what_was_left(self):
        make_save(self.a, "SAVE0", "Ellte", "QuickSave", 1000, "v1000")
        self.round()
        shutil.rmtree(self.a / "SAVE0")
        self.sync(self.a)
        key = dfu_saves.Info("Ellte", "QuickSave", 0).key
        before = {p.name for p in (self.library / key).iterdir()}
        original = dfu_saves.shutil.rmtree

        def killed(path, *args, **kwargs):
            if ".modsync-trash-" in str(path) and Path(path).exists():
                (Path(path) / "SaveData.txt").unlink()  # part of it gone, then the process dies
                raise KeyboardInterrupt
            return original(path, *args, **kwargs)

        with patch.object(dfu_saves.shutil, "rmtree", killed), self.assertRaises(KeyboardInterrupt):
            self.sync(self.b)
        self.assertNotIn("SAVE0", slots(self.b))  # never half there for DFU
        self.sync(self.b)
        self.round()
        new = {p.name for p in (self.library / key).iterdir()} - before
        self.assertTrue(all(n.endswith(".deleted") for n in new), new)  # at most B's own, matching deletion
        self.assertEqual(slots(self.a), {})
        self.assertEqual(slots(self.b), {})
        self.assertFalse(list(self.b.glob(".modsync-trash-*")))

    def test_a_placement_cut_short_before_it_was_noted_is_recognised(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        self.round()
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 200, "v2")
        self.sync(self.a)
        with patch.object(dfu_saves._Mapping, "save", lambda self: None):  # placed, but never noted
            self.sync(self.b)
        key = dfu_saves.Info("Ellte", "Ellte 1", 0).key
        before = {p.name for p in (self.library / key).iterdir()}
        report = self.sync(self.b)
        self.assertEqual(report.stored, [])
        self.assertEqual({p.name for p in (self.library / key).iterdir()}, before)

    def test_dfu_starting_midway_stops_the_sync_at_once(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        make_save(self.a, "SAVE1", "Ellte", "Ellte 2", 110, "v1")
        calls = []

        def busy():
            calls.append(1)
            return "DFU started" if len(calls) > 2 else ""  # after the lock and one slot

        report = self.sync(self.a, guard=busy_guard(busy))
        self.assertEqual(report.stored, ["Ellte: Ellte 1"])
        self.assertEqual(report.skipped, "DFU started")

    def test_a_lost_machine_file_never_reuses_a_count(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        self.sync(self.a)
        machine = self.machines[self.a]
        machine.path.write_text(json.dumps({"id": machine.id, "counter": 0}))
        self.machines[self.a] = dfu_saves.Machine(machine.path)
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 200, "v2")
        self.sync(self.a)
        key = dfu_saves.Info("Ellte", "Ellte 1", 0).key
        newest = version_dirs(self.library / key)[-1]
        self.assertEqual(ident(newest)["vv"], {machine.id: 2})

    # --- from GPT-6.1 Sol's third pass ---

    def test_a_pruned_winner_is_never_beaten_by_the_save_it_beat(self):
        make_save(self.a, "SAVE0", "Ellte", "QuickSave", 100, "A's")
        make_save(self.b, "SAVE0", "Ellte", "QuickSave", 90, "B's, made separately")
        self.round()
        self.assertEqual((self.b / "SAVE0" / "SaveData.txt").read_text(), "SaveData.txt A's")
        key = dfu_saves.Info("Ellte", "QuickSave", 0).key
        winner_files = next(p for p in version_dirs(self.library / key) if p.name.startswith(f"{100:020d}"))
        shutil.rmtree(winner_files)  # pruned elsewhere; what replaced it hasn't arrived here yet
        self.sync(self.b)
        self.assertEqual((self.b / "SAVE0" / "SaveData.txt").read_text(), "SaveData.txt A's")

    def test_a_lost_mapping_with_the_old_version_pruned_still_publishes_nothing(self):
        make_save(self.a, "SAVE0", "Ellte", "QuickSave", 1000, "v1000")
        self.round()  # B holds 1000 and goes offline
        for t in (2000, 3000, 4000, 5000, 900):
            make_save(self.a, "SAVE0", "Ellte", "QuickSave", t, f"v{t}")
            self.sync(self.a)
        key = dfu_saves.Info("Ellte", "QuickSave", 0).key
        self.assertFalse((self.library / key / f"{1000:020d}-{self.machines[self.a].id}").is_dir())  # pruned
        dfu_saves.mapping_path(self.b, self.library).unlink()
        before = {p.name for p in (self.library / key).iterdir()}
        self.sync(self.b)
        self.sync(self.a)
        self.assertEqual({p.name for p in (self.library / key).iterdir()} - before, set())
        for saves in (self.a, self.b):
            self.assertEqual((saves / "SAVE0" / "SaveData.txt").read_text(), "SaveData.txt v900")

    def test_two_processes_never_hand_out_the_same_count(self):
        path = self.tmp / "machine.json"
        app, service = dfu_saves.Machine(path), dfu_saves.Machine(path)
        self.assertEqual((app.next(), service.next(), app.next()), (1, 2, 3))
        self.assertEqual(service.next(seen=10), 11)

    def test_a_count_is_past_what_the_slot_held(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        self.sync(self.a)
        machine = self.machines[self.a]
        mapping = dfu_saves.mapping_path(self.a, self.library)
        data = json.loads(mapping.read_text())
        data["slots"]["SAVE0"]["vv"] = {machine.id: 7}  # it held a version this file never heard of
        mapping.write_text(json.dumps(data))
        machine.path.unlink()
        self.machines[self.a] = dfu_saves.Machine(machine.path)
        self.machines[self.a].id = machine.id
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 200, "v2")
        self.sync(self.a)
        key = dfu_saves.Info("Ellte", "Ellte 1", 0).key
        self.assertEqual(ident(version_dirs(self.library / key)[-1])["vv"], {machine.id: 8})

    def test_equal_vectors_still_settle_on_one_version(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "A")
        make_save(self.b, "SAVE0", "Ellte", "Ellte 1", 200, "B")
        same = {"0000aaaa": 5}
        for m in self.machines.values():
            m.id = "0000aaaa"  # a machine file copied to another machine, say
        with patch.object(dfu_saves.Machine, "next", lambda self, seen=0: 5):
            self.sync(self.a)
            self.sync(self.b)
        key = dfu_saves.Info("Ellte", "Ellte 1", 0).key
        self.assertEqual([ident(v)["vv"] for v in version_dirs(self.library / key)], [same, same])
        self.round()
        self.assertEqual((self.a / "SAVE0" / "SaveData.txt").read_text(), (self.b / "SAVE0" / "SaveData.txt").read_text())

    def test_reusing_a_slot_records_the_deletion_first(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "Ellte")
        make_save(self.b, "SAVE0", "Bjorn", "Bjorn 1", 110, "Bjorn")
        self.round()
        bjorn_slot = next(s for s, label in slots(self.a).items() if label.startswith("Bjorn"))
        shutil.rmtree(self.a / "SAVE0")
        shutil.copytree(self.a / bjorn_slot, self.a / "SAVE0")  # SAVE0 now holds a copy of Bjorn's
        with patch.object(dfu_saves._Mapping, "save", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.sync(self.a)  # cut short before the slot's new entry
        self.sync(self.a)
        self.sync(self.b)
        self.assertFalse(any("Ellte" in label for label in slots(self.b).values()))

    def test_ticks_match_dfus_clock(self):
        from datetime import datetime

        self.assertEqual(dfu_saves.ticks(datetime(2026, 11, 3, 17, 45, 45, 20331)), 639293247450203310)


class SyncSaves(unittest.TestCase):
    """dfu.sync_saves: only with saves sync on, DFU installed and closed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "config"),
                                      "XDG_DATA_HOME": str(self.tmp / "data")})
        env.start()
        self.addCleanup(env.stop)
        self.inst = dfu.Install(self.tmp / "DFU")
        self.inst.executable.parent.mkdir(parents=True)
        self.inst.executable.touch()
        make_save(self.inst.data_dir / "Saves", "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        State.update(dfu_path=str(self.inst.path))
        running = patch.object(dfu, "is_running", lambda: self.running)
        running.start()
        self.addCleanup(running.stop)
        self.running = False

    def test_off_until_saves_sync_is_turned_on(self):
        self.assertIn("don't sync", dfu.sync_saves().skipped)
        State.update(game_folders={"dfu-saves": str(dfu.saves_library())})
        self.assertEqual(dfu.sync_saves().stored, ["Ellte: Ellte 1"])
        self.assertEqual(dfu.SAVES_FOLDER.path(), self.tmp / "data" / "modsync" / "dfu-saves")

    def test_waits_while_dfu_runs(self):
        State.update(game_folders={"dfu-saves": str(dfu.saves_library())})
        self.running = True
        self.assertIn("running", dfu.sync_saves().skipped)
        self.assertEqual(list(dfu.saves_library().glob("*/*")), [])  # nothing stored

    def test_a_custom_save_folder_is_honoured(self):
        (self.tmp / "Elsewhere").mkdir()
        dfu.write_settings(self.inst.settings_ini, "Daggerfall", {"MyDaggerfallUnitySavePath": self.tmp / "Elsewhere"})
        self.assertEqual(dfu.saves_dir(self.inst), self.tmp / "Elsewhere")
        dfu.write_settings(self.inst.settings_ini, "Daggerfall", {"MyDaggerfallUnitySavePath": ""})
        self.assertEqual(dfu.saves_dir(self.inst), self.inst.data_dir / "Saves")

    def test_the_machine_id_stays_with_modsync_not_the_saves(self):
        State.update(game_folders={"dfu-saves": str(dfu.saves_library())})
        dfu.sync_saves()
        machine = json.loads((self.tmp / "data" / "modsync" / "dfu-saves-machine.json").read_text())["id"]
        self.assertRegex(machine, r"^[0-9a-f]{8}$")
        mapping = json.loads(dfu_saves.mapping_path(self.inst.data_dir / "Saves", dfu.saves_library()).read_text())
        self.assertNotIn("machine", mapping)
        (version,) = version_dirs(next(dfu.saves_library().iterdir()))
        self.assertTrue(version.name.endswith("-" + machine))
        self.assertTrue((self.inst.path / dfu.SAVES_MARKER).exists())  # the Play hook syncs around DFU now

    def test_a_custom_save_folder_that_doesnt_exist_is_ignored_like_dfu_does(self):
        dfu.write_settings(self.inst.settings_ini, "Daggerfall", {"MyDaggerfallUnitySavePath": self.tmp / "nowhere"})
        self.assertEqual(dfu.saves_dir(self.inst), self.inst.data_dir / "Saves")

    def test_the_play_hooks_lock_makes_other_syncs_give_way(self):
        import fcntl

        State.update(game_folders={"dfu-saves": str(dfu.saves_library())})
        with open(self.inst.path / dfu.PLAYING_LOCK, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # what the hook holds around DFU
            self.assertIn("starting or running", dfu.sync_saves().skipped)
            self.assertEqual(dfu.sync_saves(playing=True).stored, ["Ellte: Ellte 1"])  # the hook's own

    def test_the_guard_holds_the_play_lock_only_for_one_change(self):
        import fcntl

        def can_take_it_exclusively() -> bool:
            with open(self.inst.path / dfu.PLAYING_LOCK, "a") as other:
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                fcntl.flock(other, fcntl.LOCK_UN)
                return True

        with dfu._free_to_sync(self.inst, playing=False) as reason:
            self.assertEqual(reason, "")
            self.assertFalse(can_take_it_exclusively())  # the hook waits for this change
        self.assertTrue(can_take_it_exclusively())  # and only this one

    def test_no_install_no_folder(self):
        State.update(dfu_path="")
        self.assertIsNone(dfu.SAVES_FOLDER.path())
        self.assertIn("isn't installed", dfu.sync_saves(force=True).skipped)


if __name__ == "__main__":
    unittest.main()
