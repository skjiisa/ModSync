"""Read Steam's binary product cache (``appcache/appinfo.vdf``).

This is the client's local copy of the PICS data for every app it knows about,
so it tells us — offline, without SteamDB — what the **current public build**
of a game is: the build id and, per depot, the manifest id, sizes and language.
That is exactly what the appmanifest has to claim for Steam to consider an
install up to date, which is how we keep a downgraded game launchable from
Steam without an update (see ``appmanifest.py``).

Format (all little-endian):

* header: ``magic u32``, ``universe u32``; v29 (``0x07564429``) adds an ``i64``
  offset to a string table (``count u32`` then NUL-terminated strings) that holds
  every KeyValues *key*; keys are then ``u32`` indexes into it. v27/v28 store
  keys inline as NUL-terminated strings.
* per app until ``appid == 0``: ``appid u32``, ``size u32``, ``infoState u32``,
  ``lastUpdated u32``, ``picsToken u64``, ``sha1[20]``, ``changeNumber u32``,
  v28+ ``binarySha1[20]``, then the binary KeyValues blob.
* binary KeyValues: type byte then key, value; types 0 map, 1 string, 2 int32,
  3 float32, 4/5 u32, 7 uint64, 8 end-of-map.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAGIC_V27 = 0x07564427
MAGIC_V28 = 0x07564428
MAGIC_V29 = 0x07564429


class AppInfoError(ValueError):
    pass


@dataclass(frozen=True)
class DepotInfo:
    depot_id: int
    manifest_gid: str | None
    size: int | None
    download_size: int | None
    language: str | None  # Steam language code for language depots, else None
    oslist: str | None


@dataclass
class AppInfo:
    appid: int
    public_buildid: int | None
    public_timeupdated: int | None
    depots: dict[int, DepotInfo] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def appinfo_path(steam_root: Path | str) -> Path:
    return Path(steam_root) / "appcache" / "appinfo.vdf"


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.strtab: list[str] | None = None

    def cstr(self, pos: int) -> tuple[str, int]:
        end = self.data.index(b"\0", pos)
        return self.data[pos:end].decode("utf-8", "replace"), end + 1

    def key(self, pos: int) -> tuple[str, int]:
        if self.strtab is None:
            return self.cstr(pos)
        (idx,) = struct.unpack_from("<I", self.data, pos)
        try:
            return self.strtab[idx], pos + 4
        except IndexError as exc:
            raise AppInfoError(f"string table index {idx} out of range") from exc

    def kv(self, pos: int) -> tuple[dict[str, Any], int]:
        out: dict[str, Any] = {}
        data = self.data
        while True:
            t = data[pos]
            pos += 1
            if t == 8:
                return out, pos
            k, pos = self.key(pos)
            if t == 0:
                v, pos = self.kv(pos)
            elif t == 1:
                v, pos = self.cstr(pos)
            elif t == 2:
                (v,) = struct.unpack_from("<i", data, pos)
                pos += 4
            elif t == 3:
                (v,) = struct.unpack_from("<f", data, pos)
                pos += 4
            elif t in (4, 5):
                (v,) = struct.unpack_from("<I", data, pos)
                pos += 4
            elif t == 7:
                (v,) = struct.unpack_from("<Q", data, pos)
                pos += 8
            else:
                raise AppInfoError(f"unknown KeyValues type {t} at offset {pos - 1}")
            out[k] = v


def _iter_apps(data: bytes):
    if len(data) < 8:
        raise AppInfoError("file too short")
    magic, _universe = struct.unpack_from("<II", data, 0)
    if magic not in (MAGIC_V27, MAGIC_V28, MAGIC_V29):
        raise AppInfoError(f"unsupported appinfo.vdf magic {magic:#x}")
    reader = _Reader(data)
    pos = 8
    if magic == MAGIC_V29:
        (offset,) = struct.unpack_from("<q", data, pos)
        pos += 8
        (count,) = struct.unpack_from("<I", data, offset)
        p = offset + 4
        table: list[str] = []
        for _ in range(count):
            s, p = reader.cstr(p)
            table.append(s)
        reader.strtab = table
    entry_header = 4 + 4 + 8 + 20 + 4 + (20 if magic >= MAGIC_V28 else 0)
    while pos + 8 <= len(data):
        appid, size = struct.unpack_from("<II", data, pos)
        if appid == 0:
            return
        body = pos + 8
        yield appid, body + entry_header, reader
        pos = body + size


def read_app(path: Path | str, appid: int) -> AppInfo | None:
    """Parse only the requested app out of appinfo.vdf; None if it is not there."""
    data = Path(path).read_bytes()
    for aid, kv_pos, reader in _iter_apps(data):
        if aid != appid:
            continue
        kv, _ = reader.kv(kv_pos)
        return _to_appinfo(appid, kv.get("appinfo", kv))
    return None


def _to_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_appinfo(appid: int, app: dict[str, Any]) -> AppInfo:
    depots_kv = app.get("depots") or {}
    public = ((depots_kv.get("branches") or {}).get("public")) or {}
    depots: dict[int, DepotInfo] = {}
    for key, val in depots_kv.items():
        if not key.isdigit() or not isinstance(val, dict):
            continue
        manifests = val.get("manifests") or {}
        pub = manifests.get("public")
        gid: str | None = None
        size: int | None = None
        download: int | None = None
        if isinstance(pub, dict):  # newer layout: {gid, size, download}
            gid = str(pub.get("gid")) if pub.get("gid") is not None else None
            size = _to_int(pub.get("size"))
            download = _to_int(pub.get("download"))
        elif pub is not None:  # older layout: bare gid string
            gid = str(pub)
        config = val.get("config") or {}
        depots[int(key)] = DepotInfo(
            depot_id=int(key),
            manifest_gid=gid,
            size=size,
            download_size=download,
            language=config.get("language") if isinstance(config, dict) else None,
            oslist=config.get("oslist") if isinstance(config, dict) else None,
        )
    return AppInfo(
        appid=appid,
        public_buildid=_to_int(public.get("buildid")),
        public_timeupdated=_to_int(public.get("timeupdated")),
        depots=depots,
        raw=app,
    )
