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
        self.plan = LaunchPlan(["proton", "run", "ModOrganizer.exe"], {}, Path(tmp.name), "Mod Organizer 2", False)
        for p in (
            patch("modsync.service.build_plan", return_value=self.plan),
            patch.object(self.service.launcher, "start", return_value="Starting Mod Organizer 2…"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_launch_adds_the_runtime_first(self):
        with patch.object(vcruntime, "ensure_instance_runtime", return_value=[]) as ensure:
            self.assertEqual(self.service.launch_mo2(), "Starting Mod Organizer 2…")
        ensure.assert_called_once_with(self.plan.cwd)

    def test_launch_goes_ahead_when_the_download_fails(self):
        with patch.object(vcruntime, "ensure_instance_runtime", side_effect=OSError("offline")):
            message = self.service.launch_mo2()
        self.assertTrue(message.startswith("Starting Mod Organizer 2…"))
        self.assertIn("Visual C++ runtime", message)
        self.service.launcher.start.assert_called_once_with(self.plan)


if __name__ == "__main__":
    unittest.main()
