# ModSync in depth

Details that the README leaves out: running from source, every command, and
how the downgrade, Steam pin, launch hook, sync and background service work.

## Running from source

You need Python 3.11 or newer. With [uv](https://docs.astral.sh/uv/):

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
installer into its data directory on first use. A downgrade needs `xdelta3`
and a 7z extractor (`7z`, `7zz` or `bsdtar`, which SteamOS ships) on the
`PATH`. MO2-LINT needs `protontricks`.

The discovery layer uses only the Python standard library, so
`python3 -m modsync doctor` works without any dependencies installed. It
reports the Steam libraries, whether Skyrim SE is installed, its Proton
prefix, and any MO2 instances on the machine.

To build the Flatpak yourself, see
[packaging/flatpak/README.md](../packaging/flatpak/README.md). To set up a
development environment and run the tests, see
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Command line

Everything the dashboard does is also a command. `modsync --help` and
`modsync <group> --help` list the arguments.

```sh
modsync doctor                      # Steam libraries, game, MO2 instances, current setup
modsync diagnostics                 # doctor plus recent logs, secrets redacted: paste into a bug report
modsync mo2 status | use <dir> | install <dest>
modsync game status | downgrade <version> | restore | pin | unpin | skse
modsync sync create <instance-dir> | join <code> <instance-dir>
modsync serve                       # foreground loop; what the background service runs
modsync service install [--linger] | status | uninstall
modsync firewall status | allow | remove   # ModSync's ports in ufw or firewalld
modsync steam shortcut [--remove]   # add ModSync as a non-Steam game, or remove it
modsync launch status | enable [--through <tool>] | disable
```

## Game version records

SKSE and every native DLL mod are compiled against one exact Skyrim runtime
(1.5.97, 1.6.1170, 1.7.104 and so on), and Steam updates the game silently.
When an instance is chosen, or a vault is created from it, ModSync records the
runtime it is built for in `modsync-vault.json` inside the instance. That is
the one ModSync-owned file that syncs, so every machine sharing the setup
compares against the same record. Each machine reads its own `SkyrimSE.exe`
version, and the dashboard, `modsync doctor` and `modsync serve` warn when it
differs. After an intentional upgrade or downgrade, "Use this machine's
version" on the dashboard re-records it.

The installed SKSE is a second clue. Its runtime DLL is named after the exact
game version it was built for, such as `skse64_1_6_1170.dll`. ModSync looks
for it in the game folder and in the instance's `mods/` folder, at the top
level or under `Root/`.

- When an existing MO2 setup is chosen, ModSync records the SKSE runtime
  rather than whatever Steam has patched the game to since. An old mod list
  is offered the right downgrade immediately.
- When there is no record, the dashboard and `modsync game status` suggest
  the SKSE runtime as the downgrade target.
- Otherwise the record wins, and SKSE built for a different version is
  called out as needing a reinstall.

DLLs for several versions make SKSE ambiguous, and ModSync ignores it.

### Installing SKSE

Once the game is on the right version, the Game card offers "Install SKSE
x.y.z" whenever the SKSE in the game folder is missing, built for another
version, or present in several versions. ModSync downloads the build for the
installed game version from skse.silverlock.org, checks it against a known
SHA-256, removes the old `skse64_*` files from the game folder and copies in
the loader, the runtime DLL and `Data/Scripts`. These are the same files a
hand install puts there. The archive is kept for next time. Builds that SKSE
publishes only on Nexus (currently 2.3.1 for 1.7.104) need a login, so for
those the button opens the download page instead. `modsync game skse` does
the same from a terminal.

## How the downgrade works

Recipes come from [Mulderland's open-source downgrader](https://www.mulderland.com/en/games/the-elder-scrolls-5-skyrim-special-edition):
xdelta3 patches on a public CDN, described by an NSIS script on GitHub that
Mulderland updates within hours of each Bethesda patch. A scheduled GitHub
Action converts that script into `modsync/downgrade/recipes/skyrim-se.json`.
ModSync fetches the JSON at runtime and falls back to the bundled copy, so
new recipes reach users without a ModSync release.

Applying a recipe is native. ModSync downloads the archive and checks its
SHA1, unpacks the 7z, runs `xdelta3 -d` on every file into a staging
directory on the same drive, then installs the results with backups. If a
replacement fails it rolls back. Files the target recipe excludes are backed
up and removed too. xdelta3 verifies the source checksum, so a wrong source
version fails cleanly. Interrupted downloads resume from temporary files, and
only completed downloads enter the cache (about 1.1 GB for 1.7.104 to
1.6.1170), so a repeat is offline.

The originals stay in `.modsync-downgrade/backup`. `modsync game restore`,
or "Restore original files" on the dashboard, puts them back.
`modsync game restore --discard` drops a stale backup once Steam has
re-installed the current version.

Downgrading to 1.6.x on a Steam Deck brings back the on-screen keyboard crash
that 1.7.99 fixed. The "Steam Deck Keyboard Fix for Skyrim" SKSE plugin works
around it.

### Why Steam keeps launching a downgraded game

Steam decides whether to update from `appmanifest_489830.acf` (state flags,
build id, depot manifest ids). It never hashes files. Right after a Steam
update the manifest already claims the current build, so a downgraded install
launches from Steam as-is. When the next Bethesda patch flips the flag, "Keep
this version" or `modsync game pin` rewrites the manifest to the current
public build, read from Steam's own `appinfo.vdf`. That needs Steam closed.
The background service applies a queued pin the moment Steam exits (on the
Deck: Power, then Restart Steam). No vault is needed. `modsync game unpin`
lets Steam update the game again.

## How sync works

A portable MO2 instance is almost entirely portable content: `mods/`,
`profiles/`, `downloads/` and `overwrite/`. The only machine-specific data,
the absolute paths to the game, the Proton prefix and tool executables, lives
in `ModOrganizer.ini`.

So the Syncthing shared folder is the MO2 instance folder, and `.stignore`
excludes `ModOrganizer.ini` along with logs and cache. Each machine keeps its
own `ModOrganizer.ini`, written once by the installer, and everything else
syncs. The files that differ between operating systems are exactly the ones
that never sync, so a Steam Deck and a Windows machine can share an instance
too. Steam Cloud already handles saves.

"Stop syncing" leaves the vault but keeps the instance. Nothing is deleted.

### Firewalls and "No machines found"

"Pair over network" finds the other machine with a UDP broadcast, then runs
the PIN handshake over TCP. Syncthing then needs its own ports. The Steam Deck
ships without a firewall, but a desktop running `ufw` or `firewalld` drops
all of this silently. When ModSync sees one running, "On this machine" gets a
firewall row. "Allow in firewall..." adds the rules and asks for your
password. Once they are in, "Remove firewall rules..." takes exactly those
rules out again. ModSync re-reads the rules every launch, so rules you add or
remove by hand show up too.

By hand, allow these on the machine that has the mods:

| Port | Used for |
| --- | --- |
| 21029 TCP and UDP | ModSync pairing (discovery and PIN handshake) |
| 22000 TCP | Syncthing transfers |
| 21027 UDP | Syncthing local discovery |

```sh
sudo ufw allow 21029 && sudo ufw allow 22000/tcp && sudo ufw allow 21027/udp
```

After LAN pairing each machine dials the other's address directly, so when
only one side has a firewall, port 21029 is the only one that must be open.
The Syncthing ports matter when both sides have one.

If the scan still finds nothing (some mesh Wi-Fi systems and "client
isolation" settings do not forward broadcasts), type the IP address that
"Pair over network" shows on the other machine into the address field and
join with the PIN. Only the discovery step needs broadcast. Pasting a pairing
code works regardless.

## How the launch hook works

Steam runs whatever compatibility tool is selected for a game, and the hook
is one more such tool: `compatibilitytools.d/modsync_489830_hub`. MO2-LINT
introduced the approach in
[PR #1096](https://github.com/Furglitch/modorganizer2-linux-installer/pull/1096)
for its own `mo2_489830_redirector`. The hook's `proton` script opens the hub
and then hands the launch on to the tool Steam used before, inside the Steam
Linux Runtime container that tool asks for. When MO2-LINT's redirector is
installed the chain is Play, ModSync, Mod Organizer 2. Otherwise it is Play,
ModSync, the game with the previously selected Proton, and the button says
"Continue to Skyrim Special Edition". The hook declares no runtime
requirement of its own, so Steam runs it on the host, where a window can be
shown. Steam Tinker Launch works the same way.

The hook survives updates on both sides. Nothing of MO2-LINT's is copied. The
chain refers to its tool directory and re-reads its `toolmanifest.vdf` on
every launch, so a reinstalled or upgraded redirector is picked up as-is. The
hub starts through a stable command, `flatpak run io.github.skjiisa.ModSync`
or the installed `modsync`, never a versioned path. Steam's own choice is
recorded before it is changed and written back by "Turn off". If ModSync
cannot start at all, the launch goes ahead anyway.

Steam reads `config.vdf` and `compatibilitytools.d` only on startup and
rewrites the former on exit, so turning the hook on or off ends with a Steam
restart. With Steam running, the switch is queued and applied by the open app
or the background service the moment Steam closes. You can also pick "ModSync
(Skyrim Special Edition)" yourself under Properties, then Compatibility.

The hub makes Continue the default button and Esc cancels. Setting
`MODSYNC_HUB_AUTO_DECISION=cancel %command%` in the game's launch options
makes the hub decide by itself after a few seconds, which is useful for
testing the chain without a controller in hand.

`modsync launch enable --through <tool>` chains to a named compatibility tool
instead of the detected one.

## Background service

By default ModSync only acts while the app or `modsync serve` is running.
"Run in background" installs a `systemd --user` service that starts at login.
Without the app window it applies a queued Steam pin or launch-hook switch the
moment Steam exits, notices when Steam updates the game, and keeps a vault
syncing if one exists. If the service initially shares the app's process, it
starts a replacement on its next poll after the app closes, and transfers
resume. Both machines still need to be awake and connected.

The dashboard shows "Background service: running, inactive, failed or off".
Open the ModSync Steam shortcut to check it in Gaming Mode. "Turn off
background service" stops and removes it without deleting mods. An open app
can keep syncing.

## Logs and bug reports

Every way ModSync runs (GUI, CLI, `serve`, the launch hub) logs to
`~/.local/state/modsync/modsync.log`. Set `MODSYNC_LOG_LEVEL=DEBUG` for more.
The launch hook logs to `~/.local/state/modsync/launch-hook.log`.

For a bug report, `modsync diagnostics` or the dashboard's "Copy diagnostics"
button bundles the doctor report with the last 200 lines of both logs, with
pairing codes, API keys and device ids redacted.
