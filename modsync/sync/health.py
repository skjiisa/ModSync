"""What `modsync doctor` says about ModSync's Syncthing: the paired machines,
when each was last seen, and folders that don't add up (two at one path, a
vault that isn't there, a vault no machine has synced with in a long time).

Reads a running Syncthing when there is one, else its config.xml, and never
starts one."""

from __future__ import annotations

import base64
import hashlib
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

STALE_DAYS = 30  # a vault no other machine has synced with for this long is flagged


@dataclass
class Snapshot:
    me: str
    devices: list[dict]  # Syncthing's device entries, this machine's included
    folders: list[dict]  # Syncthing's folder entries
    running: bool = False
    connected: set[str] = field(default_factory=set)
    last_seen: dict[str, datetime | None] = field(default_factory=dict)  # None: never
    seen_known: bool = False  # Syncthing said when each machine was last seen


def seen_at(stats: dict) -> datetime | None:
    """When ``/rest/stats/device`` last saw a device; None if never (Syncthing
    gives a time in 1970, or year 1)."""
    raw = (stats or {}).get("lastSeen") or ""
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.year < 2000:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def from_config(home: Path) -> Snapshot | None:
    """What config.xml says, for when Syncthing isn't running."""
    try:
        root = ET.parse(home / "config.xml").getroot()
    except (OSError, ET.ParseError):
        return None
    devices = [{"deviceID": d.get("id", ""), "name": d.get("name", ""),
                "introducer": d.get("introducer") == "true"} for d in root.findall("device")]
    folders = [{"id": f.get("id", ""), "label": f.get("label", ""), "path": f.get("path", ""),
                "devices": [{"deviceID": d.get("id", "")} for d in f.findall("device")]}
               for f in root.findall("folder")]
    return Snapshot(me=device_id_of(home / "cert.pem"), devices=devices, folders=folders)


_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def _luhn32(s: str) -> str:
    factor, total = 1, 0
    for c in s:
        addend = factor * _B32.index(c)
        factor = 2 if factor == 1 else 1
        total += addend // 32 + addend % 32
    return _B32[(32 - total % 32) % 32]


def device_id_of(cert_pem: Path) -> str:
    """The device id Syncthing derives from its certificate (config.xml
    doesn't say which device is this one); "" if it can't be read."""
    try:
        text = cert_pem.read_text()
        body = text.split("-----BEGIN CERTIFICATE-----", 1)[1].split("-----END CERTIFICATE-----", 1)[0]
        digest = hashlib.sha256(base64.b64decode("".join(body.split()))).digest()
    except (OSError, IndexError, ValueError):
        return ""
    b32 = base64.b32encode(digest).decode().rstrip("=")
    chunks = [b32[i:i + 13] for i in range(0, 52, 13)]
    full = "".join(c + _luhn32(c) for c in chunks)
    return "-".join(full[i:i + 7] for i in range(0, 56, 7))


def from_client(client) -> Snapshot:
    me = client.my_id()
    conns = client.connections().get("connections", {})
    try:
        stats = client.device_stats()
    except Exception:
        stats = None
    return Snapshot(
        me=me, devices=client.devices(), folders=client.folders(), running=True,
        connected={d for d, c in conns.items() if c.get("connected")},
        last_seen={d: seen_at(s) for d, s in (stats or {}).items()},
        seen_known=stats is not None,
    )


def snapshot(home: Path) -> Snapshot | None:
    """Ask the Syncthing serving ``home`` if one is, else read its config.xml."""
    if not (home / "config.xml").is_file():
        return None
    try:
        from modsync.sync.api import SyncthingClient
        from modsync.sync.manager import SyncthingManager

        key, address = SyncthingManager._read_gui(home / "config.xml")
        base = address if address.startswith("http") else f"http://{address}"
        with SyncthingClient(base, key, timeout=3.0) as client:
            if client.ping():
                return from_client(client)
    except Exception:
        pass
    return from_config(home)


def _norm(path: str) -> str:
    """One spelling per directory: symlinks followed, case folded where the
    file system ignores it (Windows)."""
    return os.path.normcase(os.path.realpath(os.path.expanduser(path))) if path else ""


def lines(snap: Snapshot, vault_id: str | None, game_folder_ids: dict[str, str],
          now: datetime | None = None, forgotten: list[str] = ()) -> list[str]:
    """The doctor's lines: ``• `` for facts, ``! `` for problems.
    ``game_folder_ids`` maps each game folder ModSync syncs to its id;
    ``forgotten`` are the machines forgotten here."""
    now = now or datetime.now(timezone.utc)
    out: list[str] = []
    names = {d["deviceID"]: d.get("name") or "" for d in snap.devices}

    def who(did: str) -> str:
        name = names.get(did, "")
        return f"{name} ({did[:7]}…)" if name else f"{did[:7]}…"

    def seen(did: str) -> str:
        if did in snap.connected:
            return "connected"
        if not snap.seen_known:
            return ""
        when = snap.last_seen.get(did)
        if when is None:
            return "never connected"
        days = (now - when).days
        ago = "today" if days < 1 else f"{days} day{'' if days == 1 else 's'} ago"
        return f"last seen {when.astimezone().date().isoformat()} ({ago})"

    if snap.me:
        out.append(f"• this machine: {snap.me[:7]}…")
    peers = [d["deviceID"] for d in snap.devices if d.get("deviceID") and d["deviceID"] != snap.me]
    introducers = {d["deviceID"] for d in snap.devices if d.get("introducer")}
    for did in peers:
        notes = [n for n in (seen(did), "passes on the machines it knows" if did in introducers else "") if n]
        out.append(f"• paired: {who(did)}" + "".join(f", {n}" for n in notes))
    if forgotten:
        out.append(f"• forgotten here: {', '.join(d[:7] + '…' for d in forgotten)}")
    if not snap.running:
        out.append("• Syncthing isn't running, so when each machine was last seen isn't shown")
    elif not snap.seen_known:
        out.append("• Syncthing didn't say when each machine was last seen")

    ours = {vault_id: "the vault"} if vault_id else {}
    ours.update({fid: f"the {key} folder" for key, fid in game_folder_ids.items()})
    by_path: dict[str, list[dict]] = {}
    for f in snap.folders:
        by_path.setdefault(_norm(f.get("path", "")), []).append(f)
    for path, group in by_path.items():
        if path and len(group) > 1:
            listed = ", ".join(f"{f['id']}" + (f" ({ours[f['id']]})" if f["id"] in ours else "") for f in group)
            out.append(f"! Syncthing has {len(group)} folders at {path}: {listed}. It scans and syncs the same "
                       "files once for each. Remove the ones ModSync doesn't use in Syncthing (the files stay).")

    ids = {f["id"]: f for f in snap.folders}
    for fid, what in ours.items():
        if fid not in ids:
            out.append(f"! {what} ({fid}) isn't in Syncthing, so it doesn't sync")
    vault = ids.get(vault_id or "")
    if vault is not None and snap.seen_known:
        others = [d["deviceID"] for d in vault.get("devices", []) if d.get("deviceID") not in ("", snap.me)]
        recent = [d for d in others if d in snap.connected
                  or ((w := snap.last_seen.get(d)) is not None and (now - w).days < STALE_DAYS)]
        if others and not recent:
            out.append(f"! no machine that shares the vault has connected in {STALE_DAYS} days: "
                       f"{', '.join(who(d) for d in others)}. If those machines are gone, pair again "
                       "with the code of a machine you use.")
    return out
