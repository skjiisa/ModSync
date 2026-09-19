"""A throwaway Steam installation on disk for the launch-hook tests: one
library with Skyrim, Proton Experimental (a Valve tool needing Steam Linux
Runtime 4.0) and that runtime installed, a GE-Proton under
compatibilitytools.d, a config.vdf, and an appinfo.vdf carrying the Steam Play
manifests app that names Valve's tools."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from modsync.steam import vdf
from tests.test_appmanifest import build_appinfo_v29

PROTON_EXPERIMENTAL_APPID = 1493710
SLR4_APPID = 4183110
SNIPER_APPID = 1628350

STEAM_PLAY_MANIFESTS = {
    "appid": 891390,
    "extended": {
        "compat_tools": {
            "proton_experimental": {
                "appid": PROTON_EXPERIMENTAL_APPID,
                "require_tool_appid": SLR4_APPID,
                "display_name": "Proton Experimental",
                "from_oslist": "windows",
                "to_oslist": "linux",
            },
            "proton_9": {
                "appid": 2805730,
                "display_name": "Proton 9.0-4",
                "from_oslist": "windows",
                "to_oslist": "linux",
            },
            "steamlinuxruntime_4": {"appid": SLR4_APPID, "from_oslist": "linux", "to_oslist": "linux"},
        }
    },
}


def _acf(appid: int, name: str, installdir: str) -> str:
    return vdf.dumps({"AppState": {"appid": str(appid), "name": name, "installdir": installdir}}) + "\n"


def _executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def make_steam(root: Path, *, mapping: dict | None = None, with_mo2lint: bool = False) -> Path:
    """Build the fake Steam root and return it. ``mapping`` is what goes into
    CompatToolMapping (appid -> entry)."""
    steamapps = root / "steamapps"
    common = steamapps / "common"
    (root / "config").mkdir(parents=True)
    (root / "ubuntu12_32").mkdir()
    steamapps.mkdir(exist_ok=True)
    (steamapps / "libraryfolders.vdf").write_text(
        vdf.dumps({"libraryfolders": {"0": {"path": str(root), "apps": {"489830": "1"}}}}) + "\n"
    )
    (steamapps / "appmanifest_489830.acf").write_text(_acf(489830, "Skyrim SE", "Skyrim Special Edition"))
    (common / "Skyrim Special Edition").mkdir(parents=True)
    (steamapps / f"appmanifest_{PROTON_EXPERIMENTAL_APPID}.acf").write_text(
        _acf(PROTON_EXPERIMENTAL_APPID, "Proton Experimental", "Proton - Experimental")
    )
    proton_dir = common / "Proton - Experimental"
    _executable(proton_dir / "proton", "#!/usr/bin/env bash\nprintf 'proton %s\\n' \"$*\"\n")
    (proton_dir / "toolmanifest.vdf").write_text(
        '"manifest"\n{\n\t"version"\t\t"2"\n\t"commandline"\t\t"/proton %verb%"\n'
        f'\t"require_tool_appid"\t\t"{SLR4_APPID}"\n\t"use_sessions"\t\t"1"\n}}\n'
    )
    (steamapps / f"appmanifest_{SLR4_APPID}.acf").write_text(
        _acf(SLR4_APPID, "Steam Linux Runtime 4.0", "SteamLinuxRuntime_4")
    )
    _executable(common / "SteamLinuxRuntime_4" / "_v2-entry-point", "#!/usr/bin/env bash\nexit 0\n")

    tools = root / "compatibilitytools.d"
    ge = tools / "GE-Proton10-34"
    _executable(ge / "proton", "#!/usr/bin/env bash\nexit 0\n")
    (ge / "toolmanifest.vdf").write_text(
        '"manifest"\n{\n\t"version"\t\t"2"\n\t"commandline"\t\t"/proton %verb%"\n'
        f'\t"require_tool_appid"\t\t"{SNIPER_APPID}"\n}}\n'
    )
    (ge / "compatibilitytool.vdf").write_text(
        '"compatibilitytools"\n{\n  "compat_tools"\n  {\n    "GE-Proton10-34" // Internal name\n    {\n'
        '      "install_path" "."\n      "display_name" "GE-Proton10-34"\n'
        '      "from_oslist"  "windows"\n      "to_oslist"    "linux"\n    }\n  }\n}\n'
    )
    if with_mo2lint:
        mo2 = tools / "mo2_489830_redirector"
        _executable(mo2 / "proton", "#!/usr/bin/env bash\nexit 0\n")
        (mo2 / ".mo2-lint-proton-wrapper").touch()
        (mo2 / "toolmanifest.vdf").write_text(
            '"manifest"\n{\n\t"version"\t\t"2"\n\t"commandline"\t\t"/proton %verb%"\n'
            f'\t"require_tool_appid"\t\t"{SLR4_APPID}"\n}}\n'
        )
        (mo2 / "compatibilitytool.vdf").write_text(
            '"compatibilitytools"\n{\n  "compat_tools"\n  {\n    "mo2_489830_redirector"\n    {\n'
            '      "install_path" "."\n      "display_name" "MO2 Skyrim Special Edition"\n'
            '      "from_oslist" "windows"\n      "to_oslist" "linux"\n    }\n  }\n}\n'
        )

    (root / "appcache").mkdir()
    (root / "appcache" / "appinfo.vdf").write_bytes(build_appinfo_v29(891390, STEAM_PLAY_MANIFESTS))

    store = {"InstallConfigStore": {"Software": {"Valve": {"Steam": {
        "AutoUpdateWindowEnabled": "0",
        "CompatToolMapping": {str(k): v for k, v in (mapping or {}).items()},
    }}}}}
    (root / "config" / "config.vdf").write_text(vdf.dumps(store) + "\n")
    return root


def env_without_flatpak() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("FLATPAK_ID", None)
    return env
