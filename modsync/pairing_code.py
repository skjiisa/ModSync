"""A compact, copy-pasteable (and QR-able) pairing code.

Carries just what a peer needs to join a vault: this device's Syncthing ID, the
shared folder id, and a human label. Syncthing's own global discovery handles
actually connecting the two device IDs, so no IP addresses are needed.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

_PREFIX = "MODSYNC1-"


@dataclass(frozen=True)
class PairingCode:
    device_id: str
    folder_id: str
    label: str = ""

    def encode(self) -> str:
        raw = json.dumps(
            {"d": self.device_id, "f": self.folder_id, "l": self.label},
            separators=(",", ":"),
        )
        body = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
        return _PREFIX + body

    @classmethod
    def decode(cls, code: str) -> "PairingCode":
        code = code.strip()
        if code.startswith(_PREFIX):
            code = code[len(_PREFIX):]
        padding = "=" * (-len(code) % 4)
        raw = base64.urlsafe_b64decode(code + padding).decode("utf-8")
        data = json.loads(raw)
        return cls(device_id=data["d"], folder_id=data["f"], label=data.get("l", ""))
