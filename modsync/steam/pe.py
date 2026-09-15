"""Read the file version out of a Windows PE executable (stdlib only).

Every Windows executable that carries a version resource embeds a
``VS_FIXEDFILEINFO`` struct, which begins with the magic ``0xFEEF04BD`` followed
by ``dwStrucVersion`` (always ``0x00010000``) and then the file version as two
little-endian DWORDs (``major.minor`` in the high one, ``build.revision`` in the
low one). Rather than walk the PE section table and resource directory, we scan
the file for that magic + struct version — the pair is 8 bytes and does not occur
by accident in practice — and decode the version that follows. The scan is
memory-mapped so a 35 MB game binary costs nothing to read.
"""

from __future__ import annotations

import mmap
import struct
from pathlib import Path

_SIGNATURE = b"\xbd\x04\xef\xfe"  # 0xFEEF04BD, little-endian
_STRUC_VERSION = 0x00010000
_FIXEDFILEINFO = struct.Struct("<IIII")  # signature, strucVersion, fileVersionMS, fileVersionLS


def file_version(path: Path | str) -> tuple[int, int, int, int] | None:
    """Return ``(major, minor, build, revision)`` or ``None`` if there is no
    version resource or the file cannot be read."""
    try:
        with open(path, "rb") as fh:
            try:
                view: bytes | mmap.mmap = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            except (ValueError, OSError):  # empty file / mmap unsupported
                view = fh.read()
            try:
                return _scan(view)
            finally:
                if isinstance(view, mmap.mmap):
                    view.close()
    except OSError:
        return None


def _scan(view: bytes | mmap.mmap) -> tuple[int, int, int, int] | None:
    pos = view.find(_SIGNATURE)
    while pos >= 0:
        if pos + _FIXEDFILEINFO.size <= len(view):
            _, struc_version, ms, ls = _FIXEDFILEINFO.unpack_from(view, pos)
            if struc_version == _STRUC_VERSION:
                return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
        pos = view.find(_SIGNATURE, pos + 1)
    return None
