# ModSync

<!-- Draft notes for the 1.0 README. Each bullet is a fact to keep or drop; prose to be written by hand. -->

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

### Flatpak (recommended)

- Bundles Syncthing, PySide6, xdelta3 and 7-Zip. Nothing to install on the host, no collision with a system Python or Syncthing.
- Not on Flathub, and no plans to be. Each GitHub release ships a single-file bundle: `ModSync-<version>-x86_64.flatpak`.
- Download from https://github.com/skjiisa/ModSync/releases. On a Steam Deck, do this in Desktop Mode.
- Install by opening the file with Discover, or: `flatpak install --user ~/Downloads/ModSync-*-x86_64.flatpak`
- The `org.kde.Platform` runtime comes from Flathub, which SteamOS already has set up. About 300 MB the first time, shared with other KDE apps.
- Launch from the application menu, or `flatpak run io.github.skjiisa.ModSync`.
- The dashboard's Add to Steam button makes it launchable from Gaming Mode.
- Bundles do not update themselves. To upgrade, install the newer release's file over the old one.
- Building from source: see packaging/flatpak/README.md.

### From source

- Needs Python 3.11+.
- uv: `git clone https://github.com/skjiisa/ModSync.git && cd ModSync && uv run modsync` (GUI) or `uv run modsync doctor` (CLI).
- pip: `python3 -m venv .venv && .venv/bin/pip install . && .venv/bin/modsync`
- Outside the Flatpak, ModSync downloads a Syncthing binary and the MO2-LINT installer into its data directory on first use.
- A downgrade needs `xdelta3` and a 7z extractor (`7z`, `7zz` or `bsdtar`, which SteamOS ships) on the PATH.
- MO2-LINT needs `protontricks`.
- `python3 -m modsync doctor` works with no dependencies installed (standard library only). Reports Steam libraries, whether Skyrim SE is installed, its Proton prefix, and any MO2 instances found.

## First run

- The dashboard is the home screen. Cards: Game, Mod Organizer 2, Sync (optional), plus an "On this machine" box for the background service, Steam shortcut, launch hook and firewall.
- Before setup, the MO2 card offers "Choose folder..." (existing portable instance; instances found on the machine get a "Use" button) or "Install MO2...". The Game card works with Steam alone.
- Setup wizard runs the same steps in order, in one window, no modal dialogs, so it works in Gaming Mode:
  1. Choose your MO2 instance: pick one found, browse to one, or install a fresh one. The guided install drives MO2-LINT and streams its log. The instance is remembered as soon as chosen.
  2. Sync with another machine (optional). "Not now" keeps just this machine. Otherwise share this setup and get a pairing code, or copy another machine's setup by picking it from the LAN list and entering its PIN, or by pasting its pairing code. Copying from another machine ends the wizard here; the Game card picks up the version check by itself once the mods arrive.
  3. Game version: installed runtime versus the one this setup needs, with Downgrade and Keep this version buttons.
- Sync can also be set up later from the dashboard.
- "Reset setup..." forgets the instance and any sync without touching a mod file.

## Game version

- SKSE and every native DLL mod are compiled against one exact Skyrim runtime (1.5.97, 1.6.1170, 1.7.104, ...). Steam updates the game silently.
- When an instance is chosen (or a vault created from it), ModSync records the runtime in `modsync-vault.json` inside the instance. That is the one ModSync-owned file that syncs, so every machine compares against the same record.
- Every machine reads its own `SkyrimSE.exe` version. Dashboard, `modsync doctor` and `modsync serve` warn when it differs from the record.
- "Use this machine's version" re-records it after an intentional upgrade or downgrade.
- SKSE's runtime DLL is named after the game version it was built for (`skse64_1_6_1170.dll`). ModSync looks for it in the game folder and in the instance's `mods/` (top level or `Root/`).
  - Choosing an existing MO2 setup records the SKSE runtime, not whatever Steam has patched the game to. An old mod list gets offered the right downgrade immediately.
  - With no record, the dashboard and `modsync game status` suggest the SKSE runtime as the downgrade target.
  - Otherwise the record wins, and SKSE built for a different version is called out as needing reinstall.
  - DLLs for several versions make SKSE ambiguous, and it is ignored.
- Install SKSE: once the game is on the right version, the Game card offers "Install SKSE x.y.z" when SKSE is missing, built for another version, or there are several.
  - Downloads the matching build from skse.silverlock.org, checks a known SHA-256, removes old `skse64_*` files, copies in the loader, runtime DLL and `Data/Scripts`. Same files as a hand install. Archive kept for next time.
  - Builds SKSE only publishes on Nexus (currently 2.3.1 for 1.7.104) need a login, so the button opens the download page instead.
  - CLI: `modsync game skse`.
- Result on a machine that copied its mods: join, mods and version record arrive, downgrade, Install SKSE, play.

## Downgrading the game

- Recipes come from Mulderland's open-source downgrader (https://www.mulderland.com/en/games/the-elder-scrolls-5-skyrim-special-edition): xdelta3 patches on a public CDN, described by an NSIS script on GitHub that is updated within hours of each Bethesda patch.
- A scheduled GitHub Action converts the script into `modsync/downgrade/recipes/skyrim-se.json`. ModSync fetches it at runtime, falling back to the bundled copy, so new recipes need no ModSync release.
- Applying is native: download with SHA1 check, unpack the 7z, `xdelta3 -d` every file into a staging dir on the same drive, install with backups and rollback on failure. Files the target recipe excludes are backed up and removed too.
- xdelta3 verifies the source checksum, so a wrong source version fails cleanly.
- Originals stay in `.modsync-downgrade/backup`. `modsync game restore` (or "Restore original files") undoes the downgrade. `modsync game restore --discard` drops a stale backup after Steam re-installed the current version.
- Interrupted downloads resume. Only completed downloads enter the cache (about 1.1 GB for 1.7.104 to 1.6.1170), so a repeat is offline.
- Steam keeps working: Steam decides whether to update from `appmanifest_489830.acf` (state flags, build id, depot manifest ids), never by hashing files. Right after an update the manifest already claims the current build, so a downgraded install launches as-is.
- When the next Bethesda patch flips the flag, "Keep this version" / `modsync game pin` rewrites the manifest to the current public build (read from Steam's `appinfo.vdf`). Needs Steam closed. The background service applies a queued pin the moment Steam exits (Deck: Power, Restart Steam). No vault needed.
- Downgrading to 1.6.x on a Steam Deck brings back the on-screen keyboard crash that 1.7.99 fixed. The "Steam Deck Keyboard Fix for Skyrim" SKSE plugin works around it.
- CLI:
  - `modsync game status` (installed vs recorded version, Steam state, recipes)
  - `modsync game downgrade 1.6.1170`
  - `modsync game pin` / `modsync game unpin`
  - `modsync game restore`

## Syncing between machines (optional)

- A portable MO2 instance is almost all portable content: `mods/`, `profiles/`, `downloads/`, `overwrite/`. The only machine-specific data (absolute paths to the game, the Proton prefix, tool executables) lives in `ModOrganizer.ini`.
- The Syncthing shared folder is the MO2 instance folder. `ModOrganizer.ini`, logs and cache are excluded via `.stignore`. Each machine keeps its own `ModOrganizer.ini`, written once by the installer.
- This also makes Steam Deck to Windows sync work, since the OS-specific files are the ones that never sync. Steam Cloud handles saves.
- Sync card: "This machine has the mods" (create a vault, get a pairing code, QR and LAN PIN) or "Copy from another machine". "Stop syncing" leaves the vault but keeps the instance. Nothing is deleted.
- CLI: `modsync sync create <instance-dir>`, `modsync sync join <code> <instance>`, `modsync serve`.

### Firewalls and "No machines found"

- "Pair over network" finds the other machine with a UDP broadcast, then runs the PIN handshake over TCP. Syncthing then needs its own ports.
- Steam Deck ships without a firewall. A desktop running `ufw` or `firewalld` drops all of this silently.
- When ModSync sees one running, "On this machine" gets a firewall row. "Allow in firewall..." adds the rules (asks for your password). Once in, "Remove firewall rules..." removes exactly those rules. Rules are re-read every launch, so hand edits are reflected.
- Ports to allow on the machine that has the mods:
  - 21029 TCP and UDP: ModSync pairing (discovery and PIN handshake)
  - 22000 TCP: Syncthing transfers
  - 21027 UDP: Syncthing local discovery
  - `sudo ufw allow 21029 && sudo ufw allow 22000/tcp && sudo ufw allow 21027/udp`
- After LAN pairing each machine dials the other's address directly, so only 21029 strictly needs opening when one side has a firewall. The Syncthing ports matter when both do.
- If the scan finds nothing (mesh Wi-Fi, client isolation), type the IP address shown next to the PIN on the other machine into the address field and join with the PIN. Pasting a pairing code works regardless.

## Opening ModSync from Steam's Play button (optional)

- With the launch hook on, pressing Play on Skyrim (Gaming Mode or desktop) opens ModSync as a small hub before anything else starts: instance and profile with enabled mod count, the game-version card with Downgrade and Keep this version, and the vault's sync state, so a mod list still arriving is noticed before the game loads.
- "Continue to Mod Organizer" carries the same Steam launch on. Cancel (or closing the window) ends it and Steam returns to the library.
- How: the hook is a compatibility tool, `compatibilitytools.d/modsync_489830_hub`, the approach MO2-LINT introduced in PR #1096 (https://github.com/Furglitch/modorganizer2-linux-installer/pull/1096) for its `mo2_489830_redirector`. Its `proton` script opens the hub, then hands the launch to the tool Steam used before, inside the Steam Linux Runtime container that tool asks for.
- With MO2-LINT's redirector installed the chain is Play, ModSync, Mod Organizer 2. Otherwise Play, ModSync, the game with the previously selected Proton, and the button says "Continue to Skyrim Special Edition".
- The hook declares no runtime requirement of its own, so Steam runs it on the host where a window can be shown (Steam Tinker Launch works the same way).
- Survives updates on both sides: nothing of MO2-LINT's is copied, the chain re-reads its `toolmanifest.vdf` on every launch, and the hub is started through a stable command (`flatpak run io.github.skjiisa.ModSync` or the installed `modsync`).
- Steam's own choice is recorded before it is changed and written back by "Turn off". If ModSync cannot start, the launch goes ahead anyway.
- Steam reads `config.vdf` and `compatibilitytools.d` only on startup and rewrites the former on exit, so turning the hook on or off ends with a Steam restart. With Steam running the switch is queued and applied by the app or background service when Steam closes (Deck: Power, Restart Steam). Or pick "ModSync (Skyrim Special Edition)" under Properties, Compatibility.
- The hub makes Continue the default button and Esc cancels. `MODSYNC_HUB_AUTO_DECISION=cancel %command%` in the game's launch options makes the hub decide by itself after a few seconds.
- CLI: `modsync launch status`, `modsync launch enable [--through T]` (T: a compat-tool name to chain to instead of the detected one), `modsync launch disable`.

## Background service

- By default ModSync only acts while the app or `modsync serve` is running.
- "Run in background" installs a user service that starts at login. Without the app window it applies a queued Steam pin or launch-hook switch when Steam exits, notices when Steam updates the game, and keeps a vault syncing if one exists.
- If the service initially shares the app's process, it starts a replacement on its next poll after the app closes. Transfers resume. Both machines still need to be awake and connected.
- Dashboard shows "Background service: running / inactive / failed / off". Open the ModSync Steam shortcut to check it in Gaming Mode.
- "Turn off background service" stops and removes it without deleting mods. An open app can keep syncing.

## Command line

- Everything the dashboard does is also a command. `modsync --help` and `modsync <group> --help` list the arguments.
- `modsync doctor`: Steam libraries, game, MO2 instances, current setup
- `modsync diagnostics`: doctor plus recent logs, secrets redacted, for bug reports
- `modsync mo2 status | use <dir> | install <dest>`
- `modsync game status | downgrade <version> | restore | pin | unpin | skse`
- `modsync sync create | join`
- `modsync serve`: foreground loop, what the background service runs
- `modsync service install [--linger] | status | uninstall`
- `modsync firewall status | allow | remove`
- `modsync steam shortcut [--remove]`
- `modsync launch status | enable | disable`

## Logs and bug reports

- Every way ModSync runs (GUI, CLI, `serve`, launch hub) logs to `~/.local/state/modsync/modsync.log`. `MODSYNC_LOG_LEVEL=DEBUG` for more.
- `modsync diagnostics` or the dashboard's "Copy diagnostics" button bundles the doctor report with the last 200 lines of that log and of the launch hook's, with pairing codes, API keys and device ids redacted.
- Contributing and development setup: CONTRIBUTING.md.

## Third-party components and licenses

- ModSync is GPL-3.0-or-later (LICENSE).
- ModSync distributes no Bethesda game files. The downgrade patches are binary deltas that only apply to a copy of Skyrim SE you already own through Steam.
- Syncthing: sync engine, driven over its REST API. Bundled in the Flatpak, otherwise downloaded from GitHub releases on first use. MPL-2.0.
- 7-Zip (`7zz`): unpacks the patch archives. Bundled in the Flatpak (built from source), otherwise system `7z`/`7zz`/`bsdtar`. LGPL-2.1-or-later with the unRAR restriction and BSD parts, see https://github.com/ip7z/7zip/blob/main/DOC/License.txt.
- SKSE: downloaded from skse.silverlock.org on request, verified against a pinned SHA-256, not bundled. Its own license (free to redistribute unmodified, no source).
- xdelta3: applies the patches. Bundled in the Flatpak (3.1.0 from xdelta-gpl), otherwise system `xdelta3`. GPL-2.0-or-later.
- PySide6 / Qt 6: the GUI. Bundled in the Flatpak (PySide6-Essentials on the KDE runtime), a dependency of the source install. LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only.
- MO2-LINT: installs MO2 into the Proton prefix. Downloaded on demand as a pinned prebuilt binary. GPL-3.0.
- Mod Organizer 2: installed by MO2-LINT, not bundled or downloaded by ModSync. GPL-3.0.
- Mulderland's Skyrim SE downgrader (https://github.com/Mulderland/MulderLoad): the recipe script and the community xdelta patches. Patches downloaded from Mulderland's CDN only on a downgrade. No license declared upstream.
- httpx (BSD-3-Clause), platformdirs (MIT), qrcode (BSD-3-Clause), spake2 (MIT, pulls in cryptography): Python dependencies.
- The Flatpak runs on `org.kde.Platform`, whose contents carry their own licenses. Steam, Proton and protontricks are used where installed and are not part of ModSync.
