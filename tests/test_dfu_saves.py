"""Daggerfall Unity saves through the library: two machines (two Saves folders)
that share one library, as Syncthing would leave it, never get a save mixed
from two."""

import json
import os
import shutil
import tempfile
import unittest
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


class TwoMachines(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.library = self.tmp / "library"
        self.a, self.b = self.tmp / "A" / "Saves", self.tmp / "B" / "Saves"
        self.a.mkdir(parents=True)
        self.b.mkdir(parents=True)

    def sync(self, saves):
        return dfu_saves.sync(saves, self.library, "aaaaaaaa" if saves == self.a else "bbbbbbbb")

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
        versions = sorted(p.name for p in (self.library / key).iterdir())
        self.assertEqual(len(versions), 2)  # A's older one can still be had
        older = self.library / key / versions[0]
        self.assertEqual((older / "SaveData.txt").read_text(), "SaveData.txt A's quicksave")

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
        self.assertNotIn(b_slot, json.loads((self.b / dfu_saves.MAPPING).read_text())["slots"])

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
        self.assertFalse(any(p.name.endswith(".deleted") for p in (self.library / key).iterdir()))

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
        version = next((self.library / key).iterdir())
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
        kept = sorted(int(p.name.split("-")[0]) for p in (self.library / key).iterdir())
        self.assertEqual(kept, [40, 50, 60])
        mapping = json.loads((self.a / dfu_saves.MAPPING).read_text())
        self.assertTrue(all((self.library / v).is_dir() for v in mapping["verified"]))

    def test_dfu_never_sees_modsyncs_files(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        (self.a / "SAVE1").mkdir()  # a folder DFU would skip: no SaveInfo
        self.round()
        names = {p.name for p in self.a.iterdir()}
        self.assertTrue({n for n in names if not n.startswith("SAVE")} <= {dfu_saves.MAPPING, ".modsync-saves.lock"})
        self.assertNotIn("SAVE1", slots(self.b))
        self.assertFalse((self.b / "SAVE0" / dfu_saves.MANIFEST).exists())

    def test_versions_have_a_manifest_and_unique_names(self):
        make_save(self.a, "SAVE0", "Ellte", "Ellte 1", 100, "v1")
        self.sync(self.a)
        key = dfu_saves.Info("Ellte", "Ellte 1", 0).key
        (version,) = (self.library / key).iterdir()
        self.assertRegex(version.name, r"^0{17}100-[0-9a-f]{8}$")
        manifest = json.loads((version / dfu_saves.MANIFEST).read_text())
        self.assertEqual(manifest["character"], "Ellte")
        self.assertEqual(set(manifest["files"]), {"SaveInfo.txt", "SaveData.txt", "QuestData.txt", "FactionData.txt"})
        self.assertFalse(any(p.name.startswith(".incoming") for p in self.library.iterdir()))

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
        self.assertFalse(dfu.saves_library().exists())

    def test_a_custom_save_folder_is_honoured(self):
        (self.tmp / "Elsewhere").mkdir()
        dfu.write_settings(self.inst.settings_ini, "Daggerfall", {"MyDaggerfallUnitySavePath": self.tmp / "Elsewhere"})
        self.assertEqual(dfu.saves_dir(self.inst), self.tmp / "Elsewhere")
        dfu.write_settings(self.inst.settings_ini, "Daggerfall", {"MyDaggerfallUnitySavePath": ""})
        self.assertEqual(dfu.saves_dir(self.inst), self.inst.data_dir / "Saves")

    def test_the_machine_id_stays_with_modsync_not_the_saves(self):
        State.update(game_folders={"dfu-saves": str(dfu.saves_library())})
        dfu.sync_saves()
        machine = (self.tmp / "data" / "modsync" / "dfu-saves-machine").read_text().strip()
        self.assertRegex(machine, r"^[0-9a-f]{8}$")
        mapping = json.loads((self.inst.data_dir / "Saves" / dfu_saves.MAPPING).read_text())
        self.assertNotIn("machine", mapping)
        (version,) = next(dfu.saves_library().iterdir()).iterdir()
        self.assertTrue(version.name.endswith("-" + machine))
        self.assertTrue((self.inst.path / dfu.SAVES_MARKER).exists())  # the Play hook syncs around DFU now

    def test_a_custom_save_folder_that_doesnt_exist_is_ignored_like_dfu_does(self):
        dfu.write_settings(self.inst.settings_ini, "Daggerfall", {"MyDaggerfallUnitySavePath": self.tmp / "nowhere"})
        self.assertEqual(dfu.saves_dir(self.inst), self.inst.data_dir / "Saves")

    def test_no_install_no_folder(self):
        State.update(dfu_path="")
        self.assertIsNone(dfu.SAVES_FOLDER.path())
        self.assertIn("isn't installed", dfu.sync_saves(force=True).skipped)


if __name__ == "__main__":
    unittest.main()
