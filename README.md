# ModSync

<!-- Draft notes for the 1.0 README. Prose to be written by hand. Details live in docs/advanced.md. -->

## What it is

- Gets Skyrim Special Edition ready for Mod Organizer 2 on Steam Deck / SteamOS and any Linux, and keeps it working.
- Installs a portable MO2 instance wired into the game's Proton prefix, through MO2-LINT.
- Detects the installed game version and the SKSE runtime.
- Downgrades the game with community patches when Steam has updated it.
- Pins Steam so the downgraded install keeps launching normally.
- Optional: syncs the whole setup between a desktop and a Steam Deck with Syncthing. Each machine keeps its own game paths.
- Each part works on its own. Someone who only wants the downgrader never sets up sync.
- Links: MO2 https://github.com/ModOrganizer2/modorganizer, MO2-LINT https://github.com/Furglitch/modorganizer2-linux-installer, Syncthing https://syncthing.net/

## Install

- Download the latest `.flatpak` from https://github.com/skjiisa/ModSync/releases. On a Steam Deck, do this in Desktop Mode.
- Open it with Discover and click Install, or `flatpak install --user ~/Downloads/ModSync-*-x86_64.flatpak`.
- Launch ModSync from the application menu. The Add to Steam button makes it launchable from Gaming Mode.
- To upgrade, install the newer release's file over the old one.
- Running from source and building the Flatpak: docs/advanced.md.

## First run

- The dashboard has three cards: Game, Mod Organizer 2, and Sync (optional).
- The setup wizard walks through the same things in order: choose or install an MO2 instance, optionally sync with another machine, then fix the game version if needed.
- Works in Gaming Mode: one window, no dialogs.
- "Reset setup..." forgets the instance and any sync without touching a mod file.

## Game version

- Mods are built for one exact Skyrim version, and Steam updates the game silently.
- ModSync remembers which version your setup needs and warns when the installed game differs.
- The Game card offers Downgrade, Keep this version, and Install SKSE as needed.

## Downgrading

- Downgrades use Mulderland's community patches and keep a backup, so "Restore original files" undoes it.
- "Keep this version" stops Steam from updating the game again. If Steam is open, it is applied when Steam next closes.
- Details, and the Steam Deck keyboard caveat for 1.6.x: docs/advanced.md.

## Syncing between machines (optional)

- "This machine has the mods" gives a pairing code, QR and LAN PIN. "Copy from another machine" joins.
- Only the instance syncs. Each machine keeps its own game paths. Saves go through Steam Cloud.
- A desktop firewall blocks pairing. "Allow in firewall..." on the dashboard fixes it. Ports: docs/advanced.md.

## Play button hook (optional)

- With the launch hook on, pressing Play on Skyrim opens ModSync first: profile, mod count, game version, sync state. Continue goes on to Mod Organizer 2 or the game. Cancel returns to Steam.
- Turning it on or off needs a Steam restart, which is queued if Steam is open.

## Background service (optional)

- "Run in background" keeps syncing and applies queued Steam changes after the app is closed.

## Command line and bug reports

- Everything on the dashboard is also a `modsync` command. Reference: docs/advanced.md.
- "Copy diagnostics" on the dashboard (or `modsync diagnostics`) collects what a bug report needs, with secrets redacted.

## Third-party components and licenses

ModSync is free software under the GNU GPL, version 3 or later. See [LICENSE](LICENSE).
It builds on, bundles or downloads the components below. ModSync does not distribute
any Bethesda game files. The downgrade patches are binary deltas that only apply to a
copy of Skyrim Special Edition you already own through Steam.

| Component | Role | How you get it | License |
| --- | --- | --- | --- |
| [Syncthing](https://github.com/syncthing/syncthing) | sync engine, driven over its REST API | bundled in the Flatpak; otherwise downloaded from its GitHub releases on first use | MPL-2.0 |
| [7-Zip](https://7-zip.org/) (`7zz`) | unpacks the downgrade patch archives | bundled in the Flatpak, built from source; otherwise your system `7z`, `7zz` or `bsdtar` | LGPL-2.1-or-later with the unRAR restriction and BSD-licensed parts, see its [License.txt](https://github.com/ip7z/7zip/blob/main/DOC/License.txt) |
| [SKSE](https://skse.silverlock.org/) | the Skyrim Script Extender | downloaded from skse.silverlock.org on request and verified against a pinned SHA-256; not bundled | its own [license](https://skse.silverlock.org/): free to redistribute unmodified, no source |
| [xdelta3](https://github.com/jmacd/xdelta-gpl) | applies the binary patches | bundled in the Flatpak (3.1.0 from the `xdelta-gpl` repository); otherwise your system `xdelta3` | GPL-2.0-or-later |
| [PySide6](https://pypi.org/project/PySide6-Essentials/) / Qt 6 | the GUI | bundled in the Flatpak on the KDE runtime; a dependency of the source install | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| [MO2-LINT](https://github.com/Furglitch/modorganizer2-linux-installer) | installs Mod Organizer 2 into the game's Proton prefix | downloaded on demand as a pinned prebuilt binary when you choose Install MO2 | GPL-3.0 |
| [Mod Organizer 2](https://github.com/ModOrganizer2/modorganizer) | the mod manager | installed by MO2-LINT; not bundled or downloaded by ModSync itself | GPL-3.0 |
| [Mulderland's Skyrim SE downgrader](https://github.com/Mulderland/MulderLoad) | the downgrade recipe and the community xdelta patches it points to | ModSync converts the recipe to JSON and downloads the patches from Mulderland's CDN only when you ask for a downgrade | no license declared upstream |
| [httpx](https://github.com/encode/httpx) | HTTP client for the Syncthing API | Python dependency | BSD-3-Clause |
| [platformdirs](https://github.com/tox-dev/platformdirs) | data and config directories | Python dependency | MIT |
| [qrcode](https://github.com/lincolnloop/python-qrcode) | pairing QR codes | Python dependency | BSD-3-Clause |
| [spake2](https://github.com/warner/python-spake2) | PIN-authenticated LAN pairing, with `cryptography` | Python dependency | MIT |

The Flatpak runs on the `org.kde.Platform` runtime, whose contents carry their own
licenses. Steam, Proton and `protontricks` are used where installed and are not part
of ModSync.
