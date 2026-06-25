"""Thin httpx wrapper over Syncthing's REST API.

All requests carry the ``X-API-Key`` header. Methods cover what ModSync needs:
status/identity, reading + writing config (devices, folders), and ignores.
"""

from __future__ import annotations

from typing import Any

import httpx


class SyncthingClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SyncthingClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- liveness / identity ---
    def ping(self) -> bool:
        try:
            return self._http.get("/rest/system/ping").status_code == 200
        except httpx.HTTPError:
            return False

    def system_status(self) -> dict[str, Any]:
        return self._http.get("/rest/system/status").raise_for_status().json()

    def my_id(self) -> str:
        return self.system_status()["myID"]

    # --- config: whole + defaults ---
    def config(self) -> dict[str, Any]:
        return self._http.get("/rest/config").raise_for_status().json()

    def default_folder(self) -> dict[str, Any]:
        return self._http.get("/rest/config/defaults/folder").raise_for_status().json()

    def default_device(self) -> dict[str, Any]:
        return self._http.get("/rest/config/defaults/device").raise_for_status().json()

    # --- folders ---
    def folders(self) -> list[dict[str, Any]]:
        return self._http.get("/rest/config/folders").raise_for_status().json()

    def get_folder(self, folder_id: str) -> dict[str, Any]:
        return self._http.get(f"/rest/config/folders/{folder_id}").raise_for_status().json()

    def put_folder(self, folder: dict[str, Any]) -> None:
        # PUT with an explicit id is create-or-replace (idempotent).
        self._http.put(
            f"/rest/config/folders/{folder['id']}", json=folder
        ).raise_for_status()

    # --- devices ---
    def devices(self) -> list[dict[str, Any]]:
        return self._http.get("/rest/config/devices").raise_for_status().json()

    def put_device(self, device: dict[str, Any]) -> None:
        self._http.put(
            f"/rest/config/devices/{device['deviceID']}", json=device
        ).raise_for_status()

    # --- ignores (.stignore) ---
    def get_ignores(self, folder_id: str) -> dict[str, Any]:
        return (
            self._http.get("/rest/db/ignores", params={"folder": folder_id})
            .raise_for_status()
            .json()
        )

    def set_ignores(self, folder_id: str, patterns: list[str]) -> None:
        self._http.post(
            "/rest/db/ignores",
            params={"folder": folder_id},
            json={"ignore": patterns},
        ).raise_for_status()

    # --- system ---
    def restart(self) -> None:
        self._http.post("/rest/system/restart").raise_for_status()
