"""Verified replacements, recovery, and refusal to overwrite an unknown USVFS."""

import hashlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from modsync.mo2 import usvfs


class UsvfsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.instance = self.root / "MO2"
        self.instance.mkdir()
        (self.instance / "ModOrganizer.exe").write_bytes(b"MO2")
        self.source = self.root / "payload"
        self.source.mkdir()
        self.old = {name: f"original {name}".encode() for name in usvfs.ORIGINAL}
        self.new = {name: f"patched {name}".encode() for name in usvfs.PATCHED}
        for name in self.old:
            (self.instance / name).write_bytes(self.old[name])
            (self.source / name).write_bytes(self.new[name])
        for p in (
            patch.object(usvfs, "ORIGINAL", {n: hashlib.sha256(b).hexdigest() for n, b in self.old.items()}),
            patch.object(usvfs, "PATCHED", {n: hashlib.sha256(b).hexdigest() for n, b in self.new.items()}),
            patch.object(usvfs, "is_arm64", return_value=True),
            patch.object(usvfs, "ensure_cached", return_value=self.source),
            patch.object(usvfs.background, "run_host", return_value=subprocess.CompletedProcess([], 1)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def assert_files(self, directory, expected):
        self.assertEqual({n: (directory / n).read_bytes() for n in expected}, expected)

    def test_apply_is_idempotent_and_restore_keeps_the_original_backup(self):
        self.assertTrue(usvfs.status(self.instance).can_apply)
        usvfs.apply(self.instance)
        self.assert_files(self.instance, self.new)
        self.assert_files(usvfs.backup_dir(self.instance), self.old)
        self.assertTrue(usvfs.status(self.instance).can_restore)
        with patch.object(usvfs, "ensure_cached") as cache:
            usvfs.apply(self.instance)
            cache.assert_not_called()
        usvfs.restore(self.instance)
        self.assert_files(self.instance, self.old)
        self.assert_files(usvfs.backup_dir(self.instance), self.old)
        usvfs.restore(self.instance)
        usvfs.apply(self.instance)
        self.assert_files(self.instance, self.new)

    def test_unknown_and_newer_builds_are_never_replaced(self):
        (self.instance / "usvfs_x64.dll").write_bytes(b"future release or user's own fix")
        self.assertEqual(usvfs.status(self.instance).state, "unknown")
        with self.assertRaisesRegex(usvfs.UsvfsError, "not recognized"):
            usvfs.apply(self.instance)
        usvfs.ensure_cached.assert_not_called()
        self.assertFalse(usvfs.backup_dir(self.instance).exists())

    def test_does_not_replace_a_symlink(self):
        dll = self.instance / "usvfs_x64.dll"
        external = self.root / "original.dll"
        dll.rename(external)
        dll.symlink_to(external)
        with self.assertRaises(usvfs.UsvfsError):
            usvfs.apply(self.instance)
        self.assertTrue(dll.is_symlink())
        self.assertEqual(external.read_bytes(), self.old[dll.name])

    def test_x86_host_can_restore_but_cannot_apply(self):
        usvfs.apply(self.instance)
        with patch.object(usvfs, "is_arm64", return_value=False):
            with self.assertRaisesRegex(usvfs.UsvfsError, "only offered on ARM64"):
                usvfs.apply(self.instance)
            usvfs.restore(self.instance)
            self.assertEqual(usvfs.status(self.instance).state, "not-needed")

    def test_backup_corruption_prevents_any_replacement(self):
        backup = usvfs.backup_dir(self.instance)
        backup.mkdir(parents=True)
        (backup / "usvfs_x86.dll").write_bytes(b"another original")
        with self.assertRaisesRegex(usvfs.UsvfsError, "not the expected original"):
            usvfs.apply(self.instance)
        self.assert_files(self.instance, self.old)
        self.assertEqual((backup / "usvfs_x86.dll").read_bytes(), b"another original")

    def test_bad_cached_payload_cannot_replace_instance(self):
        (self.source / "usvfs_x64.dll").write_bytes(b"corrupt")
        with self.assertRaisesRegex(usvfs.UsvfsError, "verification failed"):
            usvfs.apply(self.instance)
        self.assert_files(self.instance, self.old)

    def test_failed_replacement_rolls_back(self):
        copy = usvfs._atomic_copy

        def fail_second(source, target):
            if source.parent == self.source and source.name == "usvfs_x86.dll":
                raise OSError("disk full")
            copy(source, target)

        with patch.object(usvfs, "_atomic_copy", side_effect=fail_second):
            with self.assertRaisesRegex(usvfs.UsvfsError, "Could not replace"):
                usvfs.apply(self.instance)
        self.assert_files(self.instance, self.old)
        self.assert_files(usvfs.backup_dir(self.instance), self.old)

    def test_interrupted_apply_can_be_completed_or_restored(self):
        backup = usvfs.backup_dir(self.instance)
        backup.mkdir(parents=True)
        for name in self.old:
            shutil.copyfile(self.instance / name, backup / name)
        for action in (usvfs.apply, usvfs.restore):
            with self.subTest(action=action.__name__):
                for name in self.old:
                    (self.instance / name).write_bytes(self.old[name])
                (self.instance / "usvfs_x64.dll").write_bytes(self.new["usvfs_x64.dll"])
                current = usvfs.status(self.instance)
                self.assertEqual(current.state, "interrupted")
                self.assertTrue(current.can_apply and current.can_restore)
                action(self.instance)
                self.assert_files(self.instance, self.new if action == usvfs.apply else self.old)

    def test_restore_refuses_to_overwrite_a_later_update(self):
        usvfs.apply(self.instance)
        (self.instance / "usvfs_x64.dll").write_bytes(b"MO2 updated this")
        with self.assertRaisesRegex(usvfs.UsvfsError, "overwrite another build"):
            usvfs.restore(self.instance)
        self.assertEqual((self.instance / "usvfs_x64.dll").read_bytes(), b"MO2 updated this")

    def test_interrupted_restore_can_be_retried(self):
        usvfs.apply(self.instance)
        copy = usvfs._atomic_copy

        def fail_second(source, target):
            if target.name == "usvfs_x86.dll":
                raise OSError("disk full")
            copy(source, target)

        with patch.object(usvfs, "_atomic_copy", side_effect=fail_second):
            with self.assertRaises(OSError):
                usvfs.restore(self.instance)
        self.assertEqual(usvfs.status(self.instance).state, "interrupted")
        usvfs.restore(self.instance)
        self.assert_files(self.instance, self.old)

    def test_restore_on_an_untouched_instance_is_a_no_op(self):
        self.assertEqual(usvfs.restore(self.instance), "The original USVFS files are already in place.")
        self.assertFalse((self.instance / ".modsync-usvfs").exists())
        usvfs.background.run_host.assert_not_called()

    def test_restore_rechecks_the_files_after_the_process_probe(self):
        usvfs.apply(self.instance)

        def update_during_probe(*_args, **_kwargs):
            (self.instance / "usvfs_x64.dll").write_bytes(b"MO2 updated itself")
            return subprocess.CompletedProcess([], 1)

        with patch.object(usvfs.background, "run_host", side_effect=update_during_probe):
            with self.assertRaisesRegex(usvfs.UsvfsError, "instance changed"):
                usvfs.restore(self.instance)
        self.assertEqual((self.instance / "usvfs_x64.dll").read_bytes(), b"MO2 updated itself")

    def test_restore_requires_verified_backup(self):
        usvfs.apply(self.instance)
        (usvfs.backup_dir(self.instance) / "usvfs_x64.dll").write_bytes(b"damaged backup")
        self.assertFalse(usvfs.status(self.instance).can_restore)
        with self.assertRaisesRegex(usvfs.UsvfsError, "verified original"):
            usvfs.restore(self.instance)
        self.assert_files(self.instance, self.new)

    def test_status_reports_unreadable_files(self):
        with patch.object(usvfs, "_digest", side_effect=PermissionError("not readable")):
            self.assertEqual(usvfs.status(self.instance).state, "unreadable")

    def test_host_process_prevents_apply_and_restore(self):
        with patch.object(usvfs.background, "run_host", return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaisesRegex(usvfs.UsvfsError, "still running"):
                usvfs.apply(self.instance)
        usvfs.ensure_cached.assert_not_called()
        usvfs.apply(self.instance)
        with patch.object(usvfs.background, "run_host", return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaises(usvfs.UsvfsError):
                usvfs.restore(self.instance)
        self.assert_files(self.instance, self.new)

    def test_rechecks_host_and_files_after_download(self):
        with patch.object(usvfs.background, "run_host", side_effect=[
            subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0),
        ]):
            with self.assertRaisesRegex(usvfs.UsvfsError, "still running"):
                usvfs.apply(self.instance)
        self.assert_files(self.instance, self.old)

        def download():
            (self.instance / "usvfs_x64.dll").write_bytes(b"updated during download")
            return self.source

        with patch.object(usvfs, "ensure_cached", side_effect=download):
            with self.assertRaisesRegex(usvfs.UsvfsError, "instance changed"):
                usvfs.apply(self.instance)

    def test_cannot_run_two_replacements_together(self):
        with usvfs._locked(self.instance):
            with self.assertRaisesRegex(usvfs.UsvfsError, "Another ModSync"):
                usvfs.apply(self.instance)

    def test_host_probe_error_is_not_treated_as_no_processes(self):
        for outcome in (subprocess.CompletedProcess([], 2), OSError("no pgrep"),
                        subprocess.TimeoutExpired("pgrep", 10)):
            with self.subTest(outcome=outcome):
                with patch.object(usvfs.background, "run_host", side_effect=outcome if isinstance(outcome, Exception) else None,
                                  return_value=outcome):
                    with self.assertRaisesRegex(usvfs.UsvfsError, "Could not check"):
                        usvfs.apply(self.instance)


class CacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.payload = {name: f"patched {name}".encode() for name in usvfs.PATCHED}
        self.archive = b"verified archive"
        self.extractor = Mock()

        def extract(archive, dest):
            (dest / "bin").mkdir(parents=True)
            for name, data in self.payload.items():
                (dest / "bin" / name).write_bytes(data)

        self.extractor.extract.side_effect = extract
        for p in (
            patch.object(usvfs, "data_dir", return_value=self.root),
            patch.object(usvfs, "PATCHED", {n: hashlib.sha256(b).hexdigest() for n, b in self.payload.items()}),
            patch.object(usvfs, "ARCHIVE_SHA256", hashlib.sha256(self.archive).hexdigest()),
            patch.object(usvfs, "ARCHIVE_SIZE", len(self.archive)),
            patch.object(usvfs, "find_extractor", return_value=self.extractor),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_download_verifies_then_reuses_and_repairs_cache(self):
        with patch.object(usvfs.urllib.request, "urlopen", return_value=io.BytesIO(self.archive)) as download:
            cache = usvfs.ensure_cached()
            self.assertEqual(usvfs.ensure_cached(), cache)
            download.assert_called_once()
        (cache / "usvfs_x64.dll").write_bytes(b"corrupt")
        with patch.object(usvfs.urllib.request, "urlopen", return_value=io.BytesIO(self.archive)) as download:
            usvfs.ensure_cached()
            download.assert_called_once()
        self.assertEqual((cache / "usvfs_x64.dll").read_bytes(), self.payload["usvfs_x64.dll"])

    def test_corrupt_or_oversized_archive_is_not_extracted(self):
        for content in (b"incorrect", b"x" * (len(self.archive) + 1)):
            with self.subTest(content=content):
                with patch.object(usvfs.urllib.request, "urlopen", return_value=io.BytesIO(content)):
                    with self.assertRaises(usvfs.UsvfsError):
                        usvfs.ensure_cached()
        self.extractor.extract.assert_not_called()

    def test_falls_back_to_the_original_release_when_the_mirror_fails(self):
        mirror, original = usvfs.ARCHIVE_URLS
        seen = []

        def fetch(request, timeout):
            seen.append(request.full_url)
            if request.full_url == mirror:
                return io.BytesIO(b"tampered or truncated")
            return io.BytesIO(self.archive)

        with patch.object(usvfs.urllib.request, "urlopen", side_effect=fetch):
            cache = usvfs.ensure_cached()
        self.assertEqual(seen, [mirror, original])
        self.assertEqual((cache / "usvfs_x64.dll").read_bytes(), self.payload["usvfs_x64.dll"])

    def test_the_mirror_is_used_first_and_alone_when_it_works(self):
        with patch.object(usvfs.urllib.request, "urlopen", return_value=io.BytesIO(self.archive)) as fetch:
            usvfs.ensure_cached()
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0].full_url, usvfs.ARCHIVE_URLS[0])
        self.assertIn("skjiisa/usvfs", usvfs.ARCHIVE_URLS[0])

    def test_network_and_unpack_failures_are_usvfs_errors(self):
        # The CLI and UI report UsvfsError; anything else would be a traceback.
        import http.client

        for failure in (http.client.IncompleteRead(b"partial"), ValueError("bad proxy URL"), OSError("offline")):
            with self.subTest(failure=failure):
                with patch.object(usvfs.urllib.request, "urlopen", side_effect=failure):
                    with self.assertRaisesRegex(usvfs.UsvfsError, "Could not download"):
                        usvfs.ensure_cached()
        self.extractor.extract.side_effect = RuntimeError("7z failed")
        with patch.object(usvfs.urllib.request, "urlopen", return_value=io.BytesIO(self.archive)):
            with self.assertRaisesRegex(usvfs.UsvfsError, "Could not unpack"):
                usvfs.ensure_cached()

    def test_wrong_extracted_file_is_not_cached(self):
        self.payload["usvfs_x64.dll"] = b"wrong DLL"
        with patch.object(usvfs.urllib.request, "urlopen", return_value=io.BytesIO(self.archive)):
            with self.assertRaisesRegex(usvfs.UsvfsError, "expected binaries"):
                usvfs.ensure_cached()
        self.assertFalse((self.root / "usvfs" / usvfs.PATCH_ID).exists())


class HostProcessTests(unittest.TestCase):
    """The real probe, run against real processes that map (or don't map) files."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.instance = Path(tmp.name).resolve() / "MO2"
        self.instance.mkdir()
        for name in usvfs.ORIGINAL:
            (self.instance / name).write_bytes(b"\0" * 4096)
        flatpak = patch.object(usvfs.background, "in_flatpak", return_value=False)
        flatpak.start()
        self.addCleanup(flatpak.stop)

    def spawn(self, code, *args):
        process = subprocess.Popen([sys.executable, "-c", code, *args], stdout=subprocess.PIPE, text=True)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return process

    def test_a_process_with_the_dll_mapped_blocks_changes(self):
        # What Wine does with every DLL it loads; checked against MO2 under Proton.
        self.spawn(
            "import mmap, sys, time\n"
            "f = open(sys.argv[1], 'rb'); m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)\n"
            "print('ready', flush=True); time.sleep(60)",
            str(self.instance / "usvfs_x64.dll"),
        )
        with self.assertRaisesRegex(usvfs.UsvfsError, "still running"):
            usvfs._require_closed(self.instance)

    def test_the_steam_launch_chain_waiting_on_the_hub_does_not(self):
        # Steam's hook script stays alive while the hub is open, and its arguments
        # end in SkyrimSELauncher.exe; a command-line match used to trip on it.
        self.spawn(
            "import time; print('ready', flush=True); time.sleep(60)",
            "waitforexitandrun", str(self.instance.parent / "Skyrim Special Edition" / "SkyrimSELauncher.exe"),
        )
        usvfs._require_closed(self.instance)

    def test_another_instance_in_use_does_not(self):
        other = self.instance.parent / "Other"
        other.mkdir()
        (other / "usvfs_x64.dll").write_bytes(b"\0" * 4096)
        self.spawn(
            "import mmap, sys, time\n"
            "f = open(sys.argv[1], 'rb'); m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)\n"
            "print('ready', flush=True); time.sleep(60)",
            str(other / "usvfs_x64.dll"),
        )
        usvfs._require_closed(self.instance)
