"""Read the file version out of a Windows PE executable (stdlib only).

Every Windows executable that carries a version resource embeds a
``VS_FIXEDFILEINFO`` struct, which begins with the magic ``0xFEEF04BD`` followed
by ``dwStrucVersion`` (always ``0x00010000``) and then the file version as two
little-endian DWORDs (``major.minor`` in the high one, ``build.revision`` in the
low one). Rather than walk the PE section table and resource directory, we scan
the file for that magic + struct version — the pair is 8 bytes and does not occur
by accident in practice — and decode the version that follows. The scan is
memory-mapped so a 35 MB game binary costs nothing to read.

The fixed struct is not always filled in, though. Skyrim SE 1.5.97's
``SkyrimSE.exe`` leaves it at 1.0.0.0 and only carries the real number in the
``StringFileInfo`` table next to it (``ProductVersion`` = "1.5.97.0"); newer
builds fill in both. That string table is also what the SKSE loader reads to
identify the runtime, so it is the authoritative answer: we prefer
``ProductVersion``, then ``FileVersion``, and fall back to the fixed struct only
when neither string parses.
"""

from __future__ import annotations

import mmap
import re
import struct
from pathlib import Path

_SIGNATURE = b"\xbd\x04\xef\xfe"  # 0xFEEF04BD, little-endian
_STRUC_VERSION = 0x00010000
_FIXEDFILEINFO = struct.Struct("<IIII")  # signature, strucVersion, fileVersionMS, fileVersionLS

# VS_VERSIONINFO header: wLength, wValueLength, wType, then szKey
# "VS_VERSION_INFO\0" (32 bytes of UTF-16) and 2 bytes of padding to reach the
# 4-byte-aligned VS_FIXEDFILEINFO value.
_HEADER = struct.Struct("<HHH")
_ROOT_KEY = "VS_VERSION_INFO".encode("utf-16-le")
_ROOT_KEY_OFFSET = _HEADER.size  # where szKey starts inside the header
_FIXED_OFFSET = _HEADER.size + len(_ROOT_KEY) + 2 + 2  # + terminator + padding
_STRING_KEYS = ("ProductVersion", "FileVersion")
_VERSION_RE = re.compile(r"(\d+)[.,]\s*(\d+)[.,]\s*(\d+)(?:[.,]\s*(\d+))?")
_MAX_BLOCK = 1 << 16


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
                fixed = (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
                return _string_version(view, pos) or fixed
        pos = view.find(_SIGNATURE, pos + 1)
    return None


def _string_version(view: bytes | mmap.mmap, fixed_pos: int) -> tuple[int, int, int, int] | None:
    """The version named in the ``StringFileInfo`` table of the resource whose
    fixed struct sits at ``fixed_pos``, or None when there is no usable one."""
    start = fixed_pos - _FIXED_OFFSET
    if start < 0 or view[start + _ROOT_KEY_OFFSET : start + _ROOT_KEY_OFFSET + len(_ROOT_KEY)] != _ROOT_KEY:
        return None
    length, _, _ = _HEADER.unpack_from(view, start)
    if length <= _FIXED_OFFSET:
        length = _MAX_BLOCK
    block = bytes(view[start : start + min(length, _MAX_BLOCK)])
    for key in _STRING_KEYS:
        parsed = _string_value(block, key)
        if parsed:
            return parsed
    return None


def _string_value(block: bytes, key: str) -> tuple[int, int, int, int] | None:
    # A String struct is wLength, wValueLength (in WCHARs), wType, szKey, padding
    # to a 4-byte boundary, then the UTF-16 value.
    needle = key.encode("utf-16-le") + b"\x00\x00"
    at = block.find(needle)
    while at >= 0:
        header = at - _HEADER.size
        if header >= 0:
            _, value_len, _ = _HEADER.unpack_from(block, header)
            value_at = (at + len(needle) + 3) & ~3
            end = value_at + value_len * 2 if value_len else len(block)
            text = block[value_at:end].decode("utf-16-le", errors="replace").split("\x00", 1)[0]
            m = _VERSION_RE.search(text)
            if m:
                return (int(m[1]), int(m[2]), int(m[3]), int(m[4] or 0))
        at = block.find(needle, at + 1)
    return None
