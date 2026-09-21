# ModSync

ModSync gets **Skyrim Special Edition** ready for [Mod Organizer 2](https://github.com/ModOrganizer2/modorganizer)
on **Steam Deck / SteamOS** and ordinary Linux, and keeps it working. It installs a
portable MO2 instance wired into the game's Proton prefix (via
[MO2-LINT](https://github.com/Furglitch/modorganizer2-linux-installer)), detects the
installed game version and the SKSE runtime, downgrades the game with community
patches when Steam has updated it, pins Steam so the downgraded install keeps
launching normally, and — optionally — keeps the whole setup in sync between a
desktop and a Steam Deck with [Syncthing](https://syncthing.net/), while each machine
keeps its own game paths. Each part stands on its own: someone who only wants the
downgrader never sets up sync.

## Install

### Flatpak (recommended, Steam Deck and any Linux)

The Flatpak bundles Syncthing, PySide6, xdelta3 and 7-Zip, so nothing needs to be
installed on the host and nothing collides with a system Python or Syncthing.
ModSync is not on Flathub; each release ships a single-file bundle instead.

1. Download `ModSync-<version>-x86_64.flatpak` from the
   [latest release](https://github.com/skjiisa/ModSync/releases). On a Steam
   Deck, do this in Desktop Mode.
2. Open the file with **Discover** (double-click it in the file manager) and
   click *Install*, or from a terminal:

   ```sh
   flatpak install --user ~/Downloads/ModSync-*-x86_64.flatpak
   ```

   Either way the `org.kde.Platform` runtime is fetched from Flathub, which
   SteamOS already has set up (about 300 MB the first time, shared with every
   other KDE app).
3. Launch **ModSync** from the application menu, or `flatpak run io.github.skjiisa.ModSync`.
   The dashboard's *Add to Steam* button makes it launchable from Gaming Mode.

A bundle does not update itself: to upgrade, install the newer release's file
the same way over the old one. To build the Flatpak from source, see
[packaging/flatpak/README.md](packaging/flatpak/README.md).

### From source (uv or pip)

Requires Python 3.11+. With [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/skjiisa/ModSync.git
cd ModSync
uv run modsync            # GUI
uv run modsync doctor     # command line
```

Or with pip into a virtual environment:

```sh
python3 -m venv .venv && .venv/bin/pip install .
.venv/bin/modsync
```

Outside the Flatpak, ModSync downloads a Syncthing binary and the MO2-LINT
installer into its data directory on first use, and a game downgrade needs
`xdelta3` and a 7z extractor (`7z`/`7zz` or `bsdtar`, which SteamOS ships) on the
`PATH`. MO2-LINT itself needs `protontricks`.

The discovery layer uses only the Python standard library, so
`python3 -m modsync doctor` works even without the dependencies installed. It
reports the Steam libraries, whether Skyrim SE is installed, its Proton prefix, and
any MO2 instances it can find on this machine.

## First run

The dashboard is always the home screen, laid out as independent cards: **Game**,
**Mod Organizer 2**, and **Sync** (optional), plus an *On this machine* box for the
background service, the Steam shortcut and the launch hook. Before anything is set
up the MO2 card offers **Choose folder…** (an existing portable instance, with any
instances found on the machine listed with a **Use** button) or **Install MO2…**,
and the Game card already works with Steam alone.

**Setup wizard** runs the same steps in order, in one window and without modal
dialogs, so it works in Gaming Mode:

1. **Choose your Mod Organizer 2 instance** — pick one that was found, browse to
   one, or install a fresh one into a folder of your choice. The guided install
   drives MO2-LINT and streams its log into the page. The instance is remembered as
   soon as it is chosen.
2. **Sync with another machine** — optional. **Not now** keeps just this
   machine; otherwise either share this setup and get a pairing code, or copy
   another machine's setup here by picking it from the LAN list and entering its
   PIN, or by pasting its pairing code. Sync can also be set up later from the
   dashboard. Copying from another machine finishes the wizard here: the mods
   and their version record arrive by sync, and the dashboard's Game card picks
   up the version check on its own once they do.
3. **Game version** — the installed Skyrim runtime versus the one this setup needs
   (see [Game version](#game-version)), with **Downgrade** and **Keep this version**
   right there. Nothing to fix? Just finish.

**Reset setup…** on the dashboard forgets the instance and any sync without touching
a single mod file.

## Game version

SKSE and every native DLL mod are compiled against one exact Skyrim runtime
(1.5.97, 1.6.1170, 1.7.104, …), and Steam updates the game silently. A setup that
works today can stop working the day Steam patches the game. So when an instance
is chosen (or a vault created from it), ModSync records the runtime it is built
for in `modsync-vault.json` inside the instance — the one ModSync-owned file that
*does* sync, so every machine sharing the setup compares against the same record.
Every machine reads its own `SkyrimSE.exe` version and the dashboard,
`modsync doctor`, and `modsync serve` all warn when it differs from the record.
After an intentional upgrade or downgrade, "Use this machine's version" on the
dashboard re-records it.

The installed SKSE is a second clue. Its runtime DLL is named after the exact game
version it was built for (`skse64_1_6_1170.dll`), so ModSync looks for it in the
game folder and in the instance's `mods/` (top level or `Root/`) and:

- **When an existing MO2 setup is chosen**, it records the SKSE runtime rather
  than whatever Steam has patched the game to since. An old mod list that has not
  been run in a while is offered the right downgrade immediately, without the user
  remembering which version it was built for.
- **When there is no record**, the dashboard and `modsync game status` suggest
  the SKSE runtime as the downgrade target.
- **Otherwise it cross-checks**: the record wins, and SKSE built for a different
  version is called out so the user knows it needs reinstalling.

DLLs for several versions lying around make SKSE ambiguous, and it is ignored.

**Installing SKSE.** Once the game itself is on the right version, the Game card
offers **Install SKSE *x.y.z*** whenever the SKSE in the game folder is missing,
built for another version, or there are several. It downloads the build made for
the installed game version from skse.silverlock.org, checks it against a known
SHA-256, removes the old `skse64_*` files from the game folder and copies the
loader, runtime DLL and `Data/Scripts` in — the same files a hand install puts
there. The archive is kept for next time. Builds that SKSE publishes only on
Nexus (currently 2.3.1 for 1.7.104) can't be fetched without a login, so for
those the button opens the download page instead. `modsync game skse` does the
same from a terminal.

This is what makes a machine that copied its mods from another one self-contained:
join → the mods and the version record arrive → downgrade → Install SKSE → play.

## Downgrading the game (and still launching it from Steam)

Bethesda's patches change the executable, so SKSE and every native plugin stop
loading until the runtime matches again. ModSync can downgrade the game itself:

- **Recipes** come from [Mulderland's open-source downgrader](https://www.mulderland.com/en/games/the-elder-scrolls-5-skyrim-special-edition):
  xdelta3 patches on a public CDN, described by an NSIS script on GitHub that has
  been updated within hours of each Bethesda patch. A scheduled GitHub Action
  converts it into `modsync/downgrade/recipes/skyrim-se.json`, which ModSync
  fetches at runtime (falling back to the bundled copy), so new recipes reach
  users without a ModSync release.
- **Applying** is native: download + SHA1 check, unpack the 7z, `xdelta3 -d` every
  file into a staging dir on the same drive, then install with backups and rollback
  on replacement failure. Files excluded by the target recipe are backed up and
  removed too. The originals stay in `.modsync-downgrade/backup` after a
  successful downgrade, so `modsync game restore` (or "Restore original files"
  on the dashboard) undoes it; `modsync game restore --discard` drops a stale
  backup once Steam has re-installed the current version. xdelta3 verifies the
  source checksum, so a wrong source version fails cleanly. Interrupted downloads
  resume from temporary files; only completed downloads enter the cache (about
  1.1 GB for 1.7.104 → 1.6.1170), so a repeat is offline.
- **Steam keeps working.** Steam decides whether to update purely from
  `appmanifest_489830.acf` (state flags, build id, depot manifest ids), never by
  hashing files. Right after a Steam update the manifest already claims the
  current build, so a downgraded install launches from Steam as-is. When the next
  Bethesda patch flips the flag, "Keep this version" / `modsync game pin` rewrites
  the manifest to the current public build (read from Steam's own `appinfo.vdf`).
  That needs Steam closed; the background service applies a queued pin the
  moment Steam exits (on the Deck: Power → Restart Steam) — no vault needed.

```sh
modsync game status                 # installed vs recorded version, Steam state, recipes
modsync game downgrade 1.6.1170     # needs xdelta3 and 7z (or bsdtar) installed
modsync game pin                    # after the next Bethesda patch, with Steam closed
modsync game restore                # undo the downgrade: put the original files back
modsync game unpin                  # let Steam update the game again
```

The Flatpak bundles xdelta3 and 7zz. Downgrading to 1.6.x on a Steam Deck brings
back the on-screen-keyboard crash that 1.7.99 fixed; the "Steam Deck Keyboard Fix
for Skyrim" SKSE plugin works around it.

## Syncing between machines (optional)

A portable MO2 instance is *almost* entirely portable content (`mods/`, `profiles/`,
`downloads/`, `overwrite/`). The only machine-specific data — absolute paths to the
game, the Proton prefix, and tool executables — lives in **`ModOrganizer.ini`**.

So ModSync makes the **Syncthing shared folder = the MO2 instance folder**, and
**excludes `ModOrganizer.ini`** (plus logs/cache) via `.stignore`. Each machine keeps
its own correct `ModOrganizer.ini` (written once by the installer), and everything
else syncs. This also makes Steam Deck ↔ Windows sync work cleanly, because the files
that differ between OSes are exactly the ones that never sync. Steam Cloud already
handles saves, so only the instance needs syncing.

On the dashboard, the *Sync* card offers "This machine has the mods" (create a
vault and get a pairing code / QR / LAN PIN) or "Copy from another machine". "Stop
syncing" leaves the vault but keeps the instance; nothing is ever deleted.

```sh
modsync sync create <instance-dir>  # share from here; prints a pairing code
modsync sync join <code> <instance> # copy another machine's setup here
modsync serve                       # keep syncing in the foreground
```

### Firewalls and "No machines found"

"Pair over network" finds the other machine with a UDP broadcast, then runs the
PIN handshake over TCP; Syncthing then needs its own ports. The Steam Deck ships
without a firewall, but a desktop running `ufw`/`firewalld` will silently drop all
of this. When ModSync sees one of those running, *On this machine* gets a firewall
row: **Allow in firewall…** adds the rules (you'll be asked for your password) and,
once they're in, **Remove firewall rules…** takes exactly those rules out again —
for keeping the ports open only while you sync, or before uninstalling. ModSync
re-reads the rules every launch, so rules you add or remove by hand are reflected
too. By hand, allow these **on the machine that has the mods**:

| Port | Used for |
| --- | --- |
| 21029 TCP + UDP | ModSync pairing (discovery + PIN handshake) |
| 22000 TCP | Syncthing transfers |
| 21027 UDP | Syncthing local discovery |

```sh
sudo ufw allow 21029 && sudo ufw allow 22000/tcp && sudo ufw allow 21027/udp
```

After LAN pairing each machine dials the other's address directly, so only 21029
strictly needs opening when just one side has a firewall; the Syncthing ports
matter when both do.

If the scan still finds nothing (some Wi-Fi mesh systems and "client isolation"
settings don't forward broadcasts), type the IP address that "Pair over network"
shows on the other machine into the *address* field and join with the PIN — only
the discovery step needs broadcast. Pasting a pairing code works regardless.

## Opening ModSync from Steam's Play button (optional)

With the launch hook on, pressing **Play** on Skyrim — in Gaming Mode or on the
desktop — opens ModSync as a small modding hub before anything else starts: the
instance and profile in use with the number of enabled mods, the game-version
card (installed vs. expected runtime, SKSE, Steam's update state, with the
Downgrade and Keep-this-version buttons), and the vault's sync state if there is
one, so a mod list that is still arriving is noticed before the game loads.
**Continue to Mod Organizer** carries the very same Steam launch on; **Cancel**
(or closing the window) ends it cleanly and Steam returns to the library.

How it works: Steam runs whatever *compatibility tool* is selected for a game,
and the hook is one more such tool — `compatibilitytools.d/modsync_489830_hub`,
the approach MO2-LINT introduced in
[PR #1096](https://github.com/Furglitch/modorganizer2-linux-installer/pull/1096)
for its own `mo2_489830_redirector`. Its `proton` script opens the hub and then
hands the launch on to the tool Steam used before, inside the Steam Linux
Runtime container that tool asks for. When MO2-LINT's redirector is installed
the chain is Play → ModSync → Mod Organizer 2; otherwise it is Play → ModSync →
the game with the previously selected Proton, and the button says *Continue to
Skyrim Special Edition*. The hook declares no runtime requirement of its own, so
Steam runs it on the host where a window can be shown (Steam Tinker Launch
works the same way).

It survives updates on both sides. Nothing of MO2-LINT's is copied: the chain
refers to its tool directory and re-reads its `toolmanifest.vdf` on every
launch, so a reinstalled or upgraded redirector (new Proton, new runtime) is
picked up as-is; and the hub is started through a stable command
(`flatpak run io.github.skjiisa.ModSync` or the installed `modsync`), never a
versioned path. Steam's own choice is recorded before it is changed and written
back by **Turn off**. If ModSync cannot start at all, the launch goes ahead
anyway — the hook never keeps a game from starting.

Steam only reads `config.vdf` and `compatibilitytools.d` on startup, and rewrites
the former on exit, so turning the hook on or off ends with a Steam restart. With
Steam running the switch is queued and applied by the open app or the background
service the moment Steam is closed (on the Deck: Power → Restart Steam) — or pick
“ModSync (Skyrim Special Edition)” yourself under *Properties → Compatibility*.

```sh
modsync launch status               # on/off, what Steam runs the game with, what Continue leads to
modsync launch enable [--through T] # T: a compat-tool name to chain to instead of the detected one
modsync launch disable              # restore the previous launcher and remove the tool
```

Not yet exercised on a Steam Deck: the hub window's focus and the hand-off to
Mod Organizer under gamescope, and gamepad A/B mapping to Continue/Cancel (the
hub makes Continue the default button and Esc cancels). Setting
`MODSYNC_HUB_AUTO_DECISION=cancel %command%` in the game's launch options makes
the hub decide by itself after a few seconds, which is how the chain can be
tried without a controller in hand.

## Background service

By default ModSync only acts while the app (or `modsync serve`) is running.
**Run in background** installs a user service that starts at login and, without
the app window, applies a queued Steam pin or launch-hook switch the moment Steam
exits, notices when Steam updates the game, and — if a vault exists — keeps it
syncing. If the service
initially shares the app's process, it starts a replacement on its next poll after
the app closes; transfers resume automatically. Both machines still need to be
awake and connected.

The dashboard shows **Background service: running / inactive / failed / off**.
Open the ModSync Steam shortcut to check it in Gaming Mode. The service is
independent of the desktop, but Steam Deck mode transitions still need testing on
hardware. **Turn off background service** stops and removes it without deleting
mods; an open app can continue syncing.

## Command line

Everything the dashboard does is also a command:

```sh
modsync doctor                      # Steam libraries, game, MO2 instances, current setup
modsync diagnostics                 # doctor + recent logs, secrets redacted: paste into a bug report
modsync mo2 status | use <dir> | install <dest>
modsync game status | downgrade <version> | restore | pin | unpin | skse
modsync sync create | join          # optional
modsync serve                       # foreground loop; what the background service runs
modsync service install [--linger] | status | uninstall
modsync firewall status | allow | remove   # ModSync's ports in ufw/firewalld
modsync steam shortcut [--remove]   # add ModSync as a non-Steam game (Gaming Mode), or remove it
modsync launch status | enable | disable   # open ModSync when Skyrim is launched from Steam
```

`modsync --help` and `modsync <group> --help` list the arguments.

## Status

Pre-release: `0.1.0-rc1` is the first public build, published as a Flatpak
bundle on the [releases page](https://github.com/skjiisa/ModSync/releases).

**Works and verified on desktop Linux:** Steam library, game and MO2 instance
discovery; guided MO2 install through MO2-LINT against a real Skyrim SE install;
game-version and SKSE detection, downgrade and Steam pin; two-node Syncthing sync
with a pairing code; the wizard and dashboard; the background service; the Flatpak
build; and the Steam launch hook (from the source install and from the Flatpak).

**Implemented but not yet exercised on a Steam Deck:** the Flatpak on SteamOS
(including a downgrade with the bundled xdelta3/7zz), the two-machine flow in
Desktop and Gaming Mode, LAN PIN pairing between real machines, the launch hub's
focus and hand-off under gamescope with a controller, and Steam Deck mode
transitions for the background service. See "Still to do" in
[packaging/flatpak/README.md](packaging/flatpak/README.md).

**Not planned:** a Flathub listing; the GitHub release bundle is the release
channel. Windows is a later target.

## Third-party components and licenses

ModSync is free software under the **GNU GPL, version 3 or later** — see
[LICENSE](LICENSE). It builds on, bundles or downloads the following. ModSync does
**not** distribute any Bethesda game files: the downgrade patches are binary deltas
that only apply to a copy of Skyrim Special Edition you already own through Steam.

| Component | Role | How you get it | License |
| --- | --- | --- | --- |
| [Syncthing](https://github.com/syncthing/syncthing) | peer-to-peer sync engine, driven over its REST API | bundled in the Flatpak; otherwise downloaded from its GitHub releases on first use | MPL-2.0 |
| [7-Zip](https://7-zip.org/) (`7zz`) | unpacks the downgrade patch archives | bundled in the Flatpak (built from the 7-Zip source); otherwise your system `7z`/`7zz` or `bsdtar` | LGPL-2.1-or-later, with the unRAR restriction on the RAR code and BSD-licensed parts — see its [License.txt](https://github.com/ip7z/7zip/blob/main/DOC/License.txt) |
| [SKSE](https://skse.silverlock.org/) | the Skyrim Script Extender, which most mods need | downloaded from skse.silverlock.org on request (**Install SKSE** / `modsync game skse`), verified against a pinned SHA-256; not bundled | its own [license](https://skse.silverlock.org/) (free to redistribute unmodified; no source) |
| [xdelta3](https://github.com/jmacd/xdelta-gpl) | applies the binary patches | bundled in the Flatpak (3.1.0 from the `xdelta-gpl` repository); otherwise your system `xdelta3` | GPL-2.0-or-later (this release; Apache-2.0 sources exist separately at [jmacd/xdelta](https://github.com/jmacd/xdelta)) |
| [PySide6](https://pypi.org/project/PySide6-Essentials/) / Qt 6 | the GUI | bundled in the Flatpak (PySide6-Essentials on the KDE runtime); a dependency of the source install | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| [MO2-LINT](https://github.com/Furglitch/modorganizer2-linux-installer) | installs Mod Organizer 2 into the game's Proton prefix | downloaded on demand as a pinned prebuilt binary when you choose *Install MO2* | GPL-3.0 |
| [Mod Organizer 2](https://github.com/ModOrganizer2/modorganizer) | the mod manager | installed by MO2-LINT, not bundled or downloaded by ModSync itself | GPL-3.0 |
| [Mulderland's Skyrim SE downgrader](https://github.com/Mulderland/MulderLoad) | the downgrade recipe (NSIS script) and the community xdelta patches it points to | ModSync converts the script to a JSON recipe and downloads the patches from Mulderland's CDN only when you ask for a downgrade | see upstream — the repository page shows no license declaration |
| [httpx](https://github.com/encode/httpx) | HTTP client for the Syncthing API | Python dependency | BSD-3-Clause |
| [platformdirs](https://github.com/tox-dev/platformdirs) | data/config directories | Python dependency | MIT |
| [qrcode](https://github.com/lincolnloop/python-qrcode) | pairing QR codes | Python dependency | BSD-3-Clause |
| [spake2](https://github.com/warner/python-spake2) | PIN-authenticated LAN pairing (pulls in `cryptography`) | Python dependency | MIT |

The Flatpak runs on the `org.kde.Platform` runtime; its contents carry their own
licenses. Steam, Proton and `protontricks` are used where installed and are not
part of ModSync.

Every way ModSync runs (GUI, CLI, `serve`, the launch hub) logs to
`~/.local/state/modsync/modsync.log` (`MODSYNC_LOG_LEVEL=DEBUG` for more). For a bug
report, `modsync diagnostics` — or the dashboard's **Copy diagnostics** button — bundles
the doctor report with the last 200 lines of that log and of the launch hook's, with
pairing codes, API keys and device ids redacted.

## Development

```sh
# unit tests (fast, offline). pytest lives in the `dev` extra:
uv run --extra dev pytest -q
# or, with the standard library runner:
uv run python -m unittest discover -s tests -t .

# integration tests — download + run a real Syncthing daemon, incl. a two-node
# sync test proving a mod propagates while each machine keeps its ModOrganizer.ini:
MODSYNC_IT=1 uv run python -m unittest tests.test_sync_integration -v
```

The downgrade recipe index is regenerated by
[.github/workflows/recipe-index.yml](.github/workflows/recipe-index.yml) using
[scripts/build_recipe_index.py](scripts/build_recipe_index.py).
