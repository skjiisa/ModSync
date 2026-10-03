"""Unpacking a VC++ redistributable and giving an MO2 instance its own runtime."""

import hashlib
import io
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from modsync.mo2 import vcruntime
from modsync.mo2.launch import LaunchPlan
from modsync.service import ModSyncService
from tests.test_gameversion import fake_pe


def fake_dll(version=vcruntime.VERSION, machine=0x8664, filler=0):
    """A PE32+ header for ``machine`` followed by a version resource. ``filler``
    pads it so cabinets need several 32 KiB blocks."""
    header = b"MZ" + b"\x00" * (0x3C - 2) + struct.pack("<I", 0x40)
    header += b"PE\x00\x00" + struct.pack("<H", machine)
    return header + fake_pe(*version) + os.urandom(filler)


def make_cab(files: dict[str, bytes], *, mszip=True) -> bytes:
    """A single-folder cabinet, MSZIP-compressed in 32 KiB blocks the way
    Microsoft's tools write them (each block primed with the previous 32 KiB)."""
    stream = b"".join(files.values())
    blocks = []
    for at in range(0, len(stream), 32768):
        chunk = stream[at:at + 32768]
        if mszip:
            primed = {"zdict": stream[max(0, at - 32768):at]} if at else {}
            deflate = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS, **primed)
            packed = b"CK" + deflate.compress(chunk) + deflate.flush()
        else:
            packed = chunk
        blocks.append(struct.pack("<IHH", 0, len(packed), len(chunk)) + packed)
    entries = b""
    offset = 0
    for name, data in files.items():
        entries += struct.pack("<IIHHHH", len(data), offset, 0, 0, 0, 0) + name.encode() + b"\x00"
        offset += len(data)
    files_at = 36 + 8
    data_at = files_at + len(entries)
    body = b"".join(blocks)
    header = struct.pack(
        "<4sIIIIIBBHHHHH", b"MSCF", 0, data_at + len(body), 0, files_at, 0, 3, 1, 1, len(files), 0, 0, 0
    )
    folder = struct.pack("<IHH", data_at, len(blocks), 1 if mszip else 0)
    return header + folder + entries + body


def fake_redist(skip=()) -> bytes:
    """A VC_redist-shaped blob: a bootstrapper, a UX cabinet, then the attached
    container whose payloads include the x64 and ARM64 runtime cabinets."""
    x64 = make_cab({f"{name}_amd64": fake_dll(filler=40000) for name in vcruntime.DLLS if name not in skip})
    arm64 = make_cab({"msvcp140.dll_arm64": fake_dll(machine=0xAA64)})
    ux = make_cab({"0": b"<BurnManifest/>"}, mszip=False)
    attached = make_cab({"a0": b"not a cabinet", "a11": arm64, "a12": x64})
    return b"MZ bootstrapper" + os.urandom(5000) + ux + os.urandom(100) + attached


class ExtractTests(unittest.TestCase):
    def test_unpacks_the_x64_dlls_from_nested_mszip_cabinets(self):
        dlls = vcruntime.extract_dlls(fake_redist())
        self.assertEqual(sorted(dlls), sorted(vcruntime.DLLS))
        for data in dlls.values():
            self.assertTrue(vcruntime._is_amd64_dll(data))
            self.assertEqual(len(data), len(fake_dll(filler=40000)))

    def test_a_missing_dll_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "concrt140.dll"):
            vcruntime.extract_dlls(fake_redist(skip=("concrt140.dll",)))

    def test_unsupported_compression_is_refused(self):
        cab = bytearray(make_cab({"x": b"data"}))
        struct.pack_into("<H", cab, 36 + 6, 3)  # LZX
        with self.assertRaisesRegex(ValueError, "compression 3"):
            vcruntime._cab_members(bytes(cab))


class InstanceRuntimeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        data_home = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.tmp / "data")})
        data_home.start()
        self.addCleanup(data_home.stop)
        self.instance = self.tmp / "MO2"
        self.instance.mkdir()
        (self.instance / "ModOrganizer.exe").write_bytes(b"MZ")
        self.redist = fake_redist()

    def download(self):
        return patch.object(vcruntime, "_download", return_value=self.redist)

    def test_copies_the_runtime_once_and_caches_the_download(self):
        with self.download() as download:
            copied = vcruntime.ensure_instance_runtime(self.instance)
            self.assertEqual(sorted(copied), sorted(vcruntime.DLLS))
            self.assertEqual(vcruntime.outdated(self.instance), [])
            self.assertEqual(vcruntime.ensure_instance_runtime(self.instance), [])
            other = self.tmp / "Other"
            other.mkdir()
            (other / "ModOrganizer.exe").write_bytes(b"MZ")
            self.assertEqual(len(vcruntime.ensure_instance_runtime(other)), len(vcruntime.DLLS))
        download.assert_called_once()

    def test_replaces_old_copies_but_never_downgrades(self):
        (self.instance / "msvcp140.dll").write_bytes(fake_dll(version=(14, 0, 24215, 1)))
        newer = fake_dll(version=(14, 50, 1, 0))
        (self.instance / "vcruntime140.dll").write_bytes(newer)
        with self.download():
            copied = vcruntime.ensure_instance_runtime(self.instance)
        self.assertIn("msvcp140.dll", copied)
        self.assertNotIn("vcruntime140.dll", copied)
        self.assertEqual((self.instance / "vcruntime140.dll").read_bytes(), newer)

    def test_skips_the_download_when_the_prefix_runtime_is_current(self):
        system32 = self.tmp / "pfx" / "system32"
        system32.mkdir(parents=True)
        for name in vcruntime.PREFIX_RUNTIME_DLLS:
            (system32 / name).write_bytes(fake_dll(version=vcruntime.PREFIX_MIN_VERSION))
        with self.download() as download:
            self.assertEqual(vcruntime.ensure_instance_runtime(self.instance, system32=system32), [])
        download.assert_not_called()
        self.assertEqual(vcruntime.outdated(self.instance), list(vcruntime.DLLS))

    def test_copies_when_the_prefix_runtime_is_old_or_missing(self):
        system32 = self.tmp / "pfx" / "system32"
        system32.mkdir(parents=True)
        (system32 / "msvcp140.dll").write_bytes(fake_dll(version=(14, 0, 24215, 1)))
        with self.download() as download:
            copied = vcruntime.ensure_instance_runtime(self.instance, system32=system32)
        self.assertEqual(sorted(copied), sorted(vcruntime.DLLS))
        download.assert_called_once()
        # No prefix yet at all (first launch creates it): the copy still happens.
        other = self.tmp / "Other"
        other.mkdir()
        (other / "ModOrganizer.exe").write_bytes(b"MZ")
        with self.download():
            self.assertEqual(len(vcruntime.ensure_instance_runtime(other, system32=self.tmp / "nope")),
                             len(vcruntime.DLLS))

    def test_does_nothing_without_mo2(self):
        (self.instance / "ModOrganizer.exe").unlink()
        with self.download() as download:
            self.assertEqual(vcruntime.ensure_instance_runtime(self.instance), [])
        download.assert_not_called()

    def test_download_must_match_the_pinned_checksum(self):
        response = io.BytesIO(b"tampered")
        with patch.object(vcruntime.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                vcruntime._download()
        response = io.BytesIO(self.redist)
        with patch.object(vcruntime, "REDIST_SHA256", hashlib.sha256(self.redist).hexdigest()), \
                patch.object(vcruntime.urllib.request, "urlopen", return_value=response):
            self.assertEqual(vcruntime._download(), self.redist)


class LaunchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # A plain desktop, so the nested-desktop note can't leak in from the session running the tests.
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(tmp.name) / "config"), "DISPLAY": ":0"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("STEAM_GAME_DISPLAY_0", None)
        self.service = ModSyncService(manager=object())
        self.compat = Path(tmp.name) / "compatdata"
        self.plan = LaunchPlan(["proton", "run", "ModOrganizer.exe"], {"STEAM_COMPAT_DATA_PATH": str(self.compat)},
                               Path(tmp.name), "Mod Organizer 2", False)
        for p in (
            patch("modsync.service.build_plan", return_value=self.plan),
            patch.object(self.service.launcher, "start", return_value="Starting Mod Organizer 2…"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_launch_adds_the_runtime_first(self):
        with patch.object(vcruntime, "ensure_instance_runtime", return_value=[]) as ensure:
            self.assertEqual(self.service.launch_mo2(), "Starting Mod Organizer 2…")
        ensure.assert_called_once_with(
            self.plan.cwd, system32=self.compat / "pfx" / "drive_c" / "windows" / "system32"
        )

    def test_launch_goes_ahead_when_the_download_fails(self):
        with patch.object(vcruntime, "ensure_instance_runtime", side_effect=OSError("offline")):
            message = self.service.launch_mo2()
        self.assertTrue(message.startswith("Starting Mod Organizer 2…"))
        self.assertIn("Visual C++ runtime", message)
        self.service.launcher.start.assert_called_once_with(self.plan)


if __name__ == "__main__":
    unittest.main()


class PrefixRuntimeTests(unittest.TestCase):
    """USVFS 0.5.7+ needs a current runtime in the game prefix, not just next to MO2."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.system32 = self.tmp / "system32"
        self.system32.mkdir()

    def write(self, name, data):
        (self.system32 / name).write_bytes(data)

    def current_runtime(self):
        for name in vcruntime.PREFIX_RUNTIME_DLLS:
            self.write(name, fake_dll())

    def test_a_2016_runtime_and_wines_stand_in_are_reported(self):
        self.write("msvcp140.dll", fake_dll(version=(14, 0, 24215, 1)))
        self.write("vcruntime140_1.dll", b"MZ" + b"\0" * 62 + b"Wine builtin DLL" + fake_dll())
        problems = vcruntime.prefix_runtime_problems(self.system32)
        self.assertEqual(problems, [
            "msvcp140.dll is 14.0.24215.1",
            "vcruntime140.dll is missing",
            "vcruntime140_1.dll is Wine's built-in stand-in",
        ])

    def test_a_current_runtime_is_fine(self):
        self.current_runtime()
        self.assertEqual(vcruntime.prefix_runtime_problems(self.system32), [])

    def test_only_usvfs_057_and_later_need_it(self):
        instance = self.tmp / "MO2"
        instance.mkdir()
        self.assertFalse(vcruntime.usvfs_needs_prefix_runtime(instance))
        (instance / "usvfs_x64.dll").write_bytes(fake_dll(version=(0, 5, 6, 1)))
        self.assertFalse(vcruntime.usvfs_needs_prefix_runtime(instance))
        (instance / "usvfs_x64.dll").write_bytes(fake_dll(version=(0, 5, 7, 2)))
        self.assertTrue(vcruntime.usvfs_needs_prefix_runtime(instance))

    def test_backup_takes_only_the_runtime(self):
        self.current_runtime()
        self.write("concrt140.dll", b"x")
        self.write("kernel32.dll", b"x")
        backup = vcruntime.backup_prefix_runtime(self.system32, self.tmp / "backup")
        self.assertEqual(sorted(p.name for p in backup.iterdir()),
                         sorted([*vcruntime.PREFIX_RUNTIME_DLLS, "concrt140.dll"]))


class InstallPrefixRuntimeTests(unittest.TestCase):
    def setUp(self):
        import subprocess

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "config"),
                                      "XDG_DATA_HOME": str(self.tmp / "data")})
        env.start()
        self.addCleanup(env.stop)
        self.system32 = self.tmp / "system32"
        self.system32.mkdir()
        (self.system32 / "msvcp140.dll").write_bytes(fake_dll(version=(14, 0, 24215, 1)))
        self.service = ModSyncService(manager=object())
        self.service.state.instance_path = str(self.tmp / "MO2")
        self.runs = []

        def run(instance, exe, args, **kw):
            self.runs.append((exe.name, args))
            if self.installs:
                for name in vcruntime.PREFIX_RUNTIME_DLLS:
                    (self.system32 / name).write_bytes(fake_dll())
            return subprocess.CompletedProcess([], self.code, stdout="", stderr="")

        self.installs, self.code = True, 0
        for p in (
            patch("modsync.service.prefix_system32", return_value=self.system32),
            patch("modsync.service.run_in_prefix", side_effect=run),
            patch.object(vcruntime, "redist_installer", return_value=self.tmp / "VC_redist.x64.exe"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def backups(self):
        return list((self.tmp / "data" / "modsync" / "backups").iterdir())

    def test_installs_after_backing_up(self):
        self.assertIn("Installed", self.service.install_prefix_runtime())
        self.assertEqual(self.runs, [("VC_redist.x64.exe", ["/install", "/quiet", "/norestart"])])
        (backup,) = self.backups()
        self.assertEqual((backup / "msvcp140.dll").read_bytes(), fake_dll(version=(14, 0, 24215, 1)))

    def test_a_failed_install_says_where_the_backup_is(self):
        self.installs, self.code = False, 1603
        with self.assertRaisesRegex(RuntimeError, "code 1603.*backed up in"):
            self.service.install_prefix_runtime()

    def test_a_runtime_still_too_old_afterwards_is_an_error(self):
        self.installs = False
        with self.assertRaisesRegex(RuntimeError, "still has msvcp140.dll is 14.0.24215.1"):
            self.service.install_prefix_runtime()

    def test_refuses_while_mo2_runs(self):
        with patch.object(self.service.launcher, "running", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "Close Mod Organizer 2"):
                self.service.install_prefix_runtime()
        self.assertEqual(self.runs, [])
