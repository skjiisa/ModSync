"""A compact, copy-pasteable (and QR-able) pairing code.

Carries just what a peer needs to join a vault: this device's Syncthing ID, the
shared folder id, a human label, and how far this device's list of the vault's
files went when the code was made (its Syncthing sequence), so a machine
copying the vault can tell when it has the whole list. Syncthing's own global
discovery handles actually connecting the two device IDs, so no IP addresses
are needed. Codes without a sequence (older versions) still decode, and older
versions ignore it. A sequence of 0 means the vault had nothing in it.

Pairing is machine-wide, so a code can also name just the machine, with no
vault (``folder_id`` empty): the two machines pair, and the games' folders
(``modsync.sync.folders``) are offered between them. Those codes start with
``MODSYNC2-``, which versions from before machine-wide pairing reject as
invalid instead of trying to join a vault with no id.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

_PREFIX = "MODSYNC1-"
_MACHINE_PREFIX = "MODSYNC2-"  # a machine only, no vault


@dataclass(frozen=True)
class PairingCode:
    device_id: str
    folder_id: str
    label: str = ""
    sequence: int | None = None  # None: not known (older versions never say)

    def encode(self) -> str:
        data = {"d": self.device_id, "f": self.folder_id, "l": self.label}
        if self.sequence is not None:
            data["s"] = self.sequence
        raw = json.dumps(data, separators=(",", ":"))
        body = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
        return (_PREFIX if self.folder_id else _MACHINE_PREFIX) + body

    @property
    def has_vault(self) -> bool:
        return bool(self.folder_id)

    @classmethod
    def decode(cls, code: str) -> "PairingCode":
        code = code.strip()
        for prefix in (_PREFIX, _MACHINE_PREFIX):
            if code.startswith(prefix):
                code = code[len(prefix):]
                break
        padding = "=" * (-len(code) % 4)
        raw = base64.urlsafe_b64decode(code + padding).decode("utf-8")
        data = json.loads(raw)
        return cls(device_id=data["d"], folder_id=data["f"], label=data.get("l", ""),
                   sequence=None if data.get("s") is None else int(data["s"]))
