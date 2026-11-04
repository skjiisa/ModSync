# ModSync in depth

Details that the README leaves out: running from source, controls, every
command, and how the downgrade, Steam pin, launch hook, sync and background
service work. The Steam Frame has [its own page](steam-frame.md).

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
`PATH`. MO2-LINT needs `pgrep` and `xdg-mime`. It bundles protontricks and
downloads `cabextract` and 7-Zip itself when the system lacks them.

The discovery layer uses only the Python standard library, so
`python3 -m modsync doctor` works without any dependencies installed. It
reports the Steam libraries, whether Skyrim SE is installed, its Proton
prefix, and any MO2 instances on the machine.

To build the Flatpak yourself, see
[packaging/flatpak/README.md](../packaging/flatpak/README.md). To set up a
development environment and run the tests, see
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Controls

Every screen works with a controller alone, in desktop mode or Gaming Mode. A
keyboard, a mouse and the Deck's touch screen work too.

| Controller | Keyboard | Does |
| --- | --- | --- |
| D-pad or left stick | Arrow keys | Move between tiles |
| A | Enter | Choose |
| B | Escape | Back: close a sheet, go back a wizard step, or return to Home. On Home it asks to quit |
| LB / RB | Q / E, Ctrl+Tab, or a tap of Ctrl / Alt | Previous / next section |
| X, Y | X, Y | Shortcuts named in the hint bar, such as "Check again" |
| LT / RT, right stick | Page Up / Page Down | Scroll |
| Start | Home | Home |

On a keyboard, Space also chooses, Backspace goes back and Ctrl+Q quits.
"Quit ModSync" under Settings quits too. When Steam's Play button opened
ModSync, quitting returns to Steam without starting the game.

The hint bar along the bottom shows what each button does. ModSync can't tell
a keyboard from a controller that Steam turns into keys, so wherever a
controller may be in use (a Steam Deck, or any controller Steam or ModSync can
see) each hint shows the controller button and the keyboard key together, with
PlayStation symbols for a PlayStation controller. On a machine with no
controller it shows only keys, and if the bar runs short of room it keeps the
controller buttons. A focused row shows the button that chooses it. Moving up from the top of a
section reaches the section tabs. Moving left and right along them switches
sections, and moving down goes back into the section.

There are two ways a controller reaches ModSync:

- **As keys.** Outside games, Steam Input applies a desktop configuration
  that turns buttons into keys, and the defaults differ by controller:

  | Button | Steam Deck | Steam Controller, Xbox, PlayStation |
  | --- | --- | --- |
  | A | Return | Enter |
  | B | Escape | Space |
  | X | on-screen keyboard | Page Up |
  | Y | Space | Page Down |
  | LB / RB | Ctrl / Alt | Ctrl / Alt |
  | View / Start | Tab / Escape | Tab / Escape |

  ModSync reads these keys back as the buttons that sent them, so A, B, X,
  Y and the bumpers do the same as on a controller it reads directly. It
  checks `/sys` for the hardware: a Steam Deck uses the Deck column, and any
  other Valve controller or a connected game controller uses the other one.
  On such a machine Space is that controller's button, so a keyboard's Space
  goes back (B) or runs the Y shortcut rather than choosing. Enter still
  chooses. Ctrl and Alt only switch sections when tapped on their own.
- **Directly.** ModSync reads controllers through SDL2, which ships in the
  Flatpak's runtime and on SteamOS. In Gaming Mode this is the virtual pad
  Steam gives the app it launched. SDL's HIDAPI drivers stay off, so ModSync
  never opens the Deck's own controls or turns off the trackpad mouse. When
  Steam turns a press into a key while SDL reads the same press, ModSync acts
  on it once. Presses are ignored while another window, such as MO2 or the
  game, is in front. Set `MODSYNC_GAMEPAD=0` to read only the keys.
  Reading a controller needs read access to its `/dev/input/event*` device.
  SteamOS and Steam's udev rules grant that for controllers. If a system
  doesn't, ModSync falls back to the keys Steam sends.

A mouse or a touch screen works on its own too, which is how first setup on a
Steam Frame (its pointer arrives as touch) or a Deck held like a tablet tends
to go. Every action is a tile to tap, and sections scroll with a finger. The
hint bar becomes buttons for what has no tile, such as Back, Cancel, Quit and
Leave setup. The PIN sheet adds a row of digits to tap. Focus styling stays
hidden until a controller or keyboard is used again, so nothing looks
selected that wasn't tapped. A press that moves is a scroll, not a choice.

Text entry (a pairing code, an address, an install folder) opens an on-screen
keyboard with a symbols layer, Paste and Clear. LB and RB move the cursor.
The PIN pad works like a combination lock: up and down turn a digit, and
left and right move between digits. Folders are picked
with a built-in browser rather than a file dialog. Typing on a real keyboard
works in all of them.

The keyboard's Shift key also produces punctuation, and "#+=" opens the full
symbols layer. LB / RB move the text cursor; the arrow buttons do the same.
Turn on Select before moving the cursor to select text, or choose Select all
to replace the whole entry. Start and End move to either end of the text.
The insertion point stays visible while a keyboard button has focus.

Home offers MO2 installation and selection, matching SKSE, and the next
repair when the game version, SKSE or MO2 needs attention. The MO2 and Game
version rows open maintenance sheets; restore, unpin and installation details
remain available there. Home, Sync and Settings are the three tabs. Play or
Continue remains available and says what may fail if you launch now. Missing
SKSE is an optional suggestion: it leaves Play primary and never takes default
controller focus, including when Steam's Play button opened ModSync. The Steam
launch entry shows On, Off, Pending or Needs attention and opens the shared
Steam controls. Under Settings, "How these settings work" explains the background service,
Steam launch settings and firewall ports; Controls shows the input shortcuts.

## Command line

Everything the app does is also a command. `modsync --help` and
`modsync <group> --help` list the arguments.

```sh
modsync doctor                      # Steam libraries, game, MO2 instances, current setup
modsync diagnostics                 # doctor plus recent logs, secrets redacted: paste into a bug report
modsync mo2 status | use <dir> | install <dest>
modsync mo2 usvfs status | apply | restore   # MO2 2.5.2's ARM64 workaround, see usvfs-arm64.md
modsync game status | downgrade <version> | restore | pin | unpin | skse
modsync sync create <instance-dir> | join <code> <instance-dir> [--merge]
modsync serve                       # foreground loop; what the background service runs
modsync service install [--linger] | status | uninstall
modsync firewall status | allow | remove   # ModSync's ports in ufw or firewalld
modsync steam shortcut [--remove]   # add ModSync as a non-Steam game, or remove it
modsync launch status | enable [--through <tool>] | disable
modsync dfu status | install [<dest>] | hook enable | disable   # Daggerfall Unity, see below
```

## Game version records

SKSE and every native DLL mod are compiled against one exact Skyrim runtime
(1.5.97, 1.6.1170, 1.7.104 and so on), and Steam updates the game silently.
When an instance is chosen, or a vault is created from it, ModSync records the
runtime it is built for in `modsync-vault.json` inside the instance. That is
the one ModSync-owned file that syncs, so every machine sharing the setup
compares against the same record. Each machine reads its own `SkyrimSE.exe`
version, and the app, `modsync doctor` and `modsync serve` warn when it
differs. After an intentional upgrade or downgrade, "Use this machine's
version" in the Game version sheet on Home re-records it.

The installed SKSE is a second clue. Its runtime DLL is named after the exact
game version it was built for, such as `skse64_1_6_1170.dll`. ModSync looks
for it in the game folder and in the instance's `mods/` folder, at the top
level or under `Root/`.

When copying another machine's setup, choose "Copy setup from another machine…"
on Home or before selecting MO2 in guided setup. This pairs before game
preparation. Joining removes the locally recorded version, then waits for the
source's `modsync-vault.json`. While waiting, Home shows Waiting and the game
sheet holds version-dependent repairs, SKSE installation and adoption of the
local version. This waiting state survives reopening ModSync. The source's
valid record enables the appropriate actions when it arrives; local SKSE does
not define the target during this wait. Keep the source machine online.

- When an existing MO2 setup is chosen, ModSync records the SKSE runtime
  rather than whatever Steam has patched the game to since. An old mod list
  is offered the right downgrade immediately.
- When there is no record for a local setup, the app and `modsync game status` suggest
  the SKSE runtime as the downgrade target.
- Otherwise the record wins, and SKSE built for a different version is
  called out as needing a reinstall.

DLLs for several versions make SKSE ambiguous, and ModSync ignores it.

### Installing SKSE

Once the game is on the right version, Home offers "Install SKSE
x.y.z" whenever the SKSE in the game folder is missing, built for another
version, or present in several versions. ModSync downloads the build for the
installed game version from skse.silverlock.org, checks it against a known
SHA-256, removes the old `skse64_*` files from the game folder and copies in
the loader, the runtime DLL and `Data/Scripts`. These are the same files a
hand install puts there. The archive is kept for next time. Builds that SKSE
publishes only on Nexus (currently 2.3.1 for 1.7.104) need a login, so for
those the tile opens the download page instead. `modsync game skse` does
the same from a terminal.

## How the downgrade works

Recipes come from [Mulderland's open-source downgrader](https://www.mulderland.com/en/games/the-elder-scrolls-5-skyrim-special-edition):
xdelta3 patches on a public CDN, described by an NSIS script on GitHub that
Mulderland updates within hours of each Bethesda patch. A scheduled GitHub
Action converts that script into `modsync/downgrade/recipes/skyrim-se.json`.
ModSync fetches the JSON at runtime, so new recipes reach users without a
ModSync release, and keeps the last copy it fetched. Offline, with GitHub
down, or with the file moved, it uses that copy, then the one bundled with
ModSync. A list for a newer ModSync, or one it can't read, is skipped the
same way, and so is a saved copy it can't read. If the game then
needs a version the list can't reach, the Game version sheet says which copy
it used and why, since a newer list may cover it. `modsync game status` says
the same. Opened from Steam's Play, ModSync doesn't fetch the list at all, so
the game starts sooner.

Applying a recipe is native. ModSync downloads the archive and checks its
SHA1, unpacks the 7z, runs `xdelta3 -d` on every file into a staging
directory on the same drive, then installs the results with backups. If a
replacement fails it rolls back. Files the target recipe excludes are backed
up and removed too. xdelta3 verifies the source checksum, so a wrong source
version fails cleanly. Interrupted downloads resume from temporary files, and
only completed downloads enter the cache (about 1.1 GB for 1.7.104 to
1.6.1170), so a repeat is offline.

The originals stay in `.modsync-downgrade/backup`. `modsync game restore`,
or "Restore original files" in the Game version sheet on Home, puts them back.
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
Deck: Power, then Restart Steam). Steam can't be quit on the Steam Frame, so
reboot it instead: the service starts before Steam and applies the pin first.
No vault is needed. `modsync game unpin`
lets Steam update the game again.

### When Steam updates the game anyway

A pin only stops automatic updates. Steam still replaces a downgraded game
when you choose Update, when "Verify integrity of game files" finds the
patched files, or when the next Bethesda patch arrives and nothing pins it.
ModSync reads the installed version from the game itself, so Home shows
"Needs 1.6.1170" again and offers the downgrade. The Game version sheet says
the backup from the last downgrade is no longer needed, because Steam has put
those files back. The next downgrade replaces that backup, and "Delete leftover
backup" removes it sooner. When Steam only wants to update, Home offers "Keep
this version" first.

Moving the game to another Steam library also undoes the pin. Steam marks the
moved game "Update Required, Files Missing", which leaves it free to check the
files and put the current version back. ModSync finds the game in its new
library, with the downgrade backup inside it, and Home offers "Keep this
version". Pinning clears both flags, so pin again after every move, before
playing.

These were checked on a real install on 2026-10-18. A verify restored 1.7.104
(5.2 GB downloaded), Home offered the downgrade, and the downgrade replaced the
leftover backup. With the pin undone, Home offered "Keep this version". With
Steam open the pin was queued, and the open app applied it within seconds of
Steam closing. After a move to a second library, Home offered "Keep this
version", and once pinned, Steam left the game alone.

If Steam's move stops with "Content file locked", the game folder or its
Proton prefix holds two files whose names differ only in capitals. Steam,
like Windows, takes them for one file. Its `content_log.txt` names the file.
SKSE scripts left by an older ModSync are one cause: Home offers to reinstall
SKSE, which removes them. Proton Experimental 11 is another: Wine keeps its
drivers in the prefix's `system32/driverstore/filerepository`, and Proton
adds `system32/DriverStore/FileRepository/igd_faux.inf_1` beside it at every
launch ([Proton#10270](https://github.com/ValveSoftware/Proton/issues/10270)).
Moving anything from `DriverStore` that `driverstore` lacks into
`driverstore`, then deleting `DriverStore`, lets the move finish. Keep the
lowercase folder: the proposed Proton fix deletes `DriverStore` outright.

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

### Pairing and the games' folders

Pairing is between machines, not between folders. Machines that have paired,
by joining the MO2 vault, with the LAN PIN or with a code, share every folder
ModSync syncs on either one. A vault created later is shared with them too.

Games other than Skyrim sync through their own folders, each with a fixed
Syncthing id (`modsync-<name>`). When a paired machine starts syncing one,
the others are offered it. Each takes it up as soon as that game is set up
there, at its own location, so no second code is needed.

```sh
modsync sync share <folder>    # start syncing one of a game's folders here
modsync sync stop <folder>     # stop; the files stay, and offers of it are left alone
modsync sync code              # this machine's code
modsync sync join <code>       # pair with a machine that syncs only games' folders
```

A machine that syncs only games' folders gives out a code starting with
`MODSYNC2-`. A machine with the vault gives the vault's `MODSYNC1-` code, which
joins the vault as before. Versions from before machine-wide pairing reject a
`MODSYNC2-` code rather than misreading it.

If Syncthing isn't answering when a folder is stopped, the folder shows as
stopping and is let go the next time ModSync reaches Syncthing. Leaving the
vault keeps the paired machines while a game's folder still syncs with them. If a game moves to another place on one machine, its folder there is
removed from Syncthing and added again at the new place, with its settings. This way, files missing
at the new place arrive from the others instead of being deleted on every machine.

### Copying from another machine

"Copy from another machine" makes this machine match the vault without
changing the machines already in the vault. The folder starts out
receive-only, so nothing here is sent anywhere. Files that already match are
kept as they are, so a machine that synced before downloads only what changed.

ModSync then waits for the other machine's whole list of files. Syncthing
reports a machine as ready as soon as it shares the folder, before any of its
list has arrived, and until then the folder looks complete. So the pairing
code and the network pairing carry how far the other machine's list went at
the time, and the copy waits until the list here has reached that and stopped
growing for a few seconds. (A code from an older ModSync doesn't say; then the
list only has to stop growing, or, if none arrives at all, five minutes pass.
A machine with nothing in its vault says so, and the copy doesn't wait.) Once
everything on it has arrived, ModSync sets
aside what is left over here: files the vault doesn't have, and this machine's
copy of any file that differs. They move to `.modsync-before-join/<date>/` in
the instance, which never syncs. Then the folder syncs both ways. With more
than two machines in the vault, this machine copies all of them together, not
only the one whose code it used.

The copy is saved as it goes, so it carries on after ModSync or the machine
restarts, wherever ModSync runs next (the app or the background service, which
picks up a copy started in the app even if it was running before). One of them
moves it on at a time.
Until it finishes, Play and Open MO2 are blocked, since anything MO2 changes
meanwhile would be set aside too. Anything written here after the first set of
files was set aside (MO2 left open, say) goes to a `during-copy-<n>/` folder
inside the same archive, so it never replaces a file from before the join.
Afterwards, Sync lists the set-aside files until you delete them or dismiss
the list. Leaving the vault during a copy waits for the step under way to
finish, so the copy can't bring the folder back.

When this machine already has mods, joining asks first. **Merge** joins both
ways at once instead: mods from both machines end up on both, and where a file
differs, the newer one wins everywhere. `modsync sync join --merge` does the
same from the command line.

### Conflicts

When two machines change the same file before they sync (a mod enabled on the
desktop while the Steam Deck was off, then another on the Deck), Syncthing
keeps the newer file and saves the other next to it as
`<name>.sync-conflict-<date>-<time>-<device>.<ext>`. MO2 ignores those, so
changes from one machine silently drop out of the mod list.

ModSync looks for them in every folder that syncs (`mods/`, `profiles/`,
`downloads/`, `overwrite/`) and at the instance root. Sync and Home show how
many there are, and the check before playing mentions them. For each one, Sync
describes what differs (mods only in one version, mods enabled in only one, a
different order; for INI files and download `.meta` files, the settings added,
removed or set differently) and lets you keep the version in use or the other
one. Neither version is labelled with a machine: the device in a conflict
copy's name is the machine that found the conflict, not the one that made the
change, so ModSync shows when the other version was last changed instead. A
profile's mod list, plugins and load order are decided together, so the
profile stays as one machine had it. The version you don't keep moves to a new
folder under `.modsync-conflicts/` in the instance. ModSync doesn't merge the
two versions: without the version both machines started from, it can't tell a
lost change from an intentional one.

`modsync sync conflicts` lists them from the command line with the same
descriptions, and `modsync sync conflicts keep current|other <file>` settles
one (with the rest of its profile, for a mod list, plugins or load order).
When a file has more than one other version, name the conflict copy itself.
Either way, a choice waits until MO2, and anything started from it, is closed,
including an MO2 that Steam or another ModSync started. While a copy from
another machine is under way there is nothing to choose: whatever differs is
set aside when the copy finishes. `modsync sync status` shows the vault, the machines, any copy under way and
the number of conflicts.

Looking through `mods/` reads every file name in it. On a large instance the
background check spaces its searches out by how long the last one took, so the
count on Home can lag by up to ten minutes. Opening the review on Sync, the
check before playing and `modsync sync conflicts` always search afresh.

### Before playing

With a vault, Play and Open MO2 first ask Syncthing whether this machine is
behind: changes another machine has announced but this one hasn't received,
or unresolved conflicts. Either is worth a "Play anyway?". Changes another
machine made while it was offline can't be known until it connects again.

### Pausing and leaving

"Stop syncing" offers two things. **Pause** keeps the vault and Syncthing's
record of what was in sync, so on resuming, what changed on each machine
meanwhile carries over, removed mods included. **Leave the vault** forgets it
and the paired devices. Joining again later compares the machines from scratch,
so a mod removed on one of them while apart comes back from the other. Either
way the instance stays in use here, and nothing is deleted.

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
is one more such tool: `compatibilitytools.d/modsync_489830_proton`. MO2-LINT
introduced the approach in
[PR #1096](https://github.com/Furglitch/modorganizer2-linux-installer/pull/1096)
for its own `mo2_489830_redirector`. The hook's `proton` script opens ModSync
and then carries on with the same launch, inside the Steam Linux Runtime
container its Proton asks for.

Play Skyrim and Open Mod Organizer 2 prepare the same launch as when ModSync
was opened directly: the same Proton, MO2 profile and SKSE, and the same
checks. ModSync doesn't start it, though. It writes the Proton, the instance
folder and the `ModOrganizer.exe` command line to the hook on stdout and
closes. The hook runs that command in place of the game's own program, with
Steam's verb (`waitforexitandrun`) and environment. MO2 and the game are
therefore Steam's launch: Steam shows the game as running until the whole
Wine session exits, counts playtime, and uploads saves to Steam Cloud only
after that. A Play started from ModSync when Steam didn't open it bypasses
Steam, so it doesn't sync cloud saves.

Before an MO2 instance is chosen, the button is Continue instead, which runs
what Steam was about to start: MO2-LINT's redirector when it is installed
("Continue to Mod Organizer"), otherwise the game with the previously
selected Proton ("Continue to Skyrim Special Edition"). If ModSync fails or
hands back nothing usable, the hook launches that too. The hook declares no
runtime requirement of its own, so Steam runs it on the host, where a window
can be shown. Steam Tinker Launch works the same way.

The hook survives updates on both sides. Nothing of MO2-LINT's is copied. The
chain refers to its tool directory and re-reads its `toolmanifest.vdf` on
every launch, so a reinstalled or upgraded redirector is picked up as-is.
ModSync starts through a stable command, `flatpak run io.github.skjiisa.ModSync`
or the installed `modsync`, never a versioned path. Steam's own choice is
recorded before it is changed and written back when the hook is turned off.
If ModSync
cannot start at all, the launch goes ahead anyway.

The "proton" in the name matters. Steam Cloud maps a game's Windows save
folders, such as Documents, into the Proton prefix only for a tool whose
internal name contains "proton". Under any other name Steam can't resolve the
save folder: `logs/cloud_log.txt` shows `Unable to resolve path with root
WinMyDocuments`, saves are neither downloaded nor uploaded, and Steam still
reports the sync as complete. The hook used to be called `modsync_489830_hub`,
which broke cloud saves in exactly this way. ModSync renames an existing hook
when ModSync opens or the background service starts. Steam Tinker Launch
found the same rule
([#185](https://github.com/sonic2kk/steamtinkerlaunch/issues/185)).

Steam reads `config.vdf` and `compatibilitytools.d` only on startup and
rewrites the former on exit, so turning the hook on or off ends with a Steam
restart. With Steam running, the switch is queued and applied by the open app
or the background service the moment Steam closes. You can also pick "ModSync
(Skyrim Special Edition)" yourself under Properties, then Compatibility.

Steam's Play button opens the regular ModSync window, with the launch waiting
on it. Home offers the same Play Skyrim and Open Mod Organizer 2 tiles as
always, with "Cancel launch" below them. Play has focus, so A or Enter plays.
When the game version or SKSE needs a repair, a repair tile such as "Fix game
version" sits above Play and takes focus instead, and Play says what may
fail. That only happens before anything is pressed, so a press made while the
check runs still lands on Play. If a launch can't be prepared, for example
because MO2 hasn't been told which game it manages yet, ModSync says why and
Steam keeps waiting. "Cancel launch" or closing the window ends the launch,
and Steam goes back to the library. Play, Open MO2 and Cancel stay disabled
while game or USVFS files are being rewritten. Setting
`MODSYNC_HUB_AUTO_DECISION=continue %command%` (Play, or Continue before an
instance is chosen) or `=cancel` in the game's launch options makes ModSync
decide by itself after a few seconds. That is useful for testing the chain
without a controller in hand.

`modsync launch enable --through <tool>` chains to a named compatibility tool
instead of the detected one.

## Daggerfall Unity

ModSync can set up [Daggerfall Unity](https://www.dfworkshop.net/) (DFU)
from the free Steam copy of *The Elder Scrolls II: Daggerfall*. This is
separate from the Skyrim setup and uses none of the MO2 tools. In the app,
the Daggerfall Unity line on Home opens a sheet for all of it: installing,
Steam's Play button, Play, and mods and saves sync. The line appears once
Steam has Daggerfall. The commands below do the same from a terminal.

1. Install *The Elder Scrolls II: Daggerfall* in Steam.
2. `modsync dfu install` downloads the official DFU 1.1.1 Linux build,
   checks it against a pinned checksum, and unpacks it unmodified into
   `~/Games/Daggerfall Unity`. To use another folder, pass it as an argument;
   the folder must be new or empty.
3. `modsync dfu hook enable` makes Steam's own Play button on Daggerfall
   start Daggerfall Unity, also in Gaming Mode. It uses the same launch hook
   as Skyrim (see "How the launch hook works"), but opens no ModSync window
   first. When Steam is running, the switch waits until Steam closes.

ModSync adds two things to the install:

- `Portable.txt`, so DFU keeps its settings, saves and mod load order in
  `PortableAppdata/` inside the install instead of `~/.config/unity3d`.
- Two settings in `PortableAppdata/settings.ini`. `MyDaggerfallPath` points
  at Steam's `DF/DAGGER` folder. The resolution is set to the primary
  display's, as reported by `xrandr`. DFU fills in everything else.
  - DFU's own first run asks for a resolution, but only while it still needs
    the data path, so it skips that step here. Without this, it falls back to
    1920×1080, which looks stretched on a screen of another shape.
  - To change the resolution later, use Advanced on DFU's startup screen.

DFU's startup screen still opens. That's where the mod list and DFU's own
settings are edited. Mods go in
`DaggerfallUnity_Data/StreamingAssets/Mods`. Use the Linux build of a mod
where one is offered.

To play the original Daggerfall in DOSBox, put
`MODSYNC_ORIGINAL_GAME=1 %command%` in its launch options under Properties,
or run `modsync dfu hook disable`. If Daggerfall Unity's folder is gone, Play
starts the original too.

If Daggerfall moves to another Steam library, run `modsync dfu install`
again. It only rewrites `MyDaggerfallPath`, and leaves mods, saves and
other settings alone. An install into another folder moves the Play
button with it. `modsync dfu status` shows where everything is, whether DFU
still points at Steam's copy, and what Play does.

### Syncing Daggerfall Unity's mods

`modsync sync share dfu-mods` syncs DFU's mods with every paired machine (see
"Pairing and the games' folders"). Each machine takes up the folder once DFU
is installed there, so mods dropped into one machine show up on the others.
The Syncthing folder is the DFU install. Its `.stignore` allows only these
paths:

- `DaggerfallUnity_Data/StreamingAssets/Mods` and the other `StreamingAssets`
  folders DFU ships with nothing but a readme: `Textures`, `Sound`, `Books`,
  `Movies`, `Presets`, `QuestPacks`, `SoundFonts` and `WorldData`.
- `PortableAppdata/Mods`, which holds the load order and on/off switches
  (`GameData/Mods.json`) and each mod's settings.
- `modsync-dfu-vault.json`, which records the DFU version the mods are for.
  `modsync dfu status` warns when this machine runs a different version.

The program, the files DFU ships itself (`Quests`, `Text`, `Tables` and the
like), `settings.ini`, key bindings and saves stay on each machine.

If two machines change the load order or a mod's settings before they sync,
Syncthing keeps the newer file and sets the other aside. `modsync sync
conflicts` lists those files under names like
`dfu-mods:PortableAppdata/Mods/GameData/Mods.json`. To settle one, close DFU
and run `modsync sync conflicts keep current|other <name>`. The version not
kept goes to `.modsync-conflicts` in the install.

A mod file itself (`.dfmod`) changed on two machines is settled
automatically. DFU loads every `.dfmod` it finds, so the other version could
load in place of the one in use. ModSync keeps the newer one, Syncthing's
choice, and moves the other to `.modsync-conflicts/set-aside` in the install.

If DFU moves to another folder, the synced files are copied from the old
install to the new one first, so mods that hadn't reached another machine
come along. Nothing already in the new folder is replaced, and the old
install is left alone.

Mods published per system work across Linux machines. A Windows build also
worked on Linux in testing: the Linux and Windows builds of Handpainted
Models differ only in their shaders, and both include the OpenGL shaders
DFU uses on Linux.

### Syncing Daggerfall Unity's saves

`modsync sync share dfu-saves` syncs saves, separately from mods, so a
machine can sync one without the other.

DFU numbers its saves per machine (`SAVE0`, `SAVE1`, and so on), each time
taking the first free number. Two machines that each made a save before
syncing can therefore both have a `SAVE7` holding different saves, or each
a QuickSave of the same character. Syncthing works one file at a time, so
syncing DFU's `Saves` folder directly would build one save out of two.

So the folder that syncs is a library ModSync keeps beside DFU's, at
`~/.local/share/modsync/dfu-saves`, or the Flatpak's data folder:

- Each version of each save is a folder of its own. Its name holds the
  save's time, the machine that made it, and that machine's count of
  changes, so no two are ever named alike. It's written once and never
  changed, so Syncthing never has two versions of one file to choose
  between. Its manifest lists its files, so a version still arriving is
  never used, and it's checked again as it goes into a slot.
- Beside each version is a small `.id` file holding its version vector (for
  each machine, how many changes to the save it takes in) and a digest of
  its files. These are kept for good, and which version wins is decided
  from them alone, so removing old versions' files never changes it. A
  version made after another takes in everything the other does, so a save
  you continued always replaces the one it came from, even when the clocks
  disagree. DFU's times are local time, so a different time zone or the end
  of summer time would otherwise let older progress win. Only saves made
  separately on two machines (each played on before they met) are decided by
  DFU's clock. The other one stays in the library, and once a machine has
  taken or kept the winner, what it saves next beats the other one too.
- Whenever DFU is closed, ModSync copies new and changed saves into the
  library. A local save whose files match a known version, even one whose
  files have since been removed, counts as that version, so nothing is
  published twice. ModSync then puts each save's winning version into a slot
  here. It replaces an older copy, in one step where the filesystem allows,
  or takes a free number for a save new here. The new files are copied and
  checked beside the slot first. What the slot is about to hold is written
  down before it changes, and its old contents are kept until that's done.
  So the next sync finishes or undoes one that was cut short, even if you
  played in between. A slot is never moved back to a version it already takes in,
  and until the winner's files have arrived its save stays as it is.
- The files of every version that could still win, and of the three newest
  others of each save, are kept.
- A save deleted in DFU is deleted on the other machines too, once no slot
  here still holds it (with two copies in two slots, once both are gone), unless one of them made it again separately, later by
  DFU's clock. Deletions are kept in the library for good, a few bytes each.
- One ModSync syncs a given Saves folder. If the Flatpak and a natively
  installed ModSync both have saves sync on for one install, the second
  says so and leaves the saves alone. Stopping saves sync in the first lets
  the second take over.

Play in Steam brings in the newest saves just before DFU starts, and stores
the new ones right after it exits. Each change another sync makes to DFU's
saves holds a lock the Play hook takes before starting DFU, so the hook
waits for the one change under way, which takes a moment. Only if a sync has
been stuck for ten minutes does the hook start DFU without it.
ModSync also syncs saves whenever it checks sync status. Saves made while
ModSync and its background service were both off travel the next time either
runs.

DFU warns by itself when a save needs mods this machine is missing. The
version check under "Syncing Daggerfall Unity's mods" covers DFU itself.

DFU only has an x86_64 build. On the Steam Frame it would have to run under
emulation, which is untested (#110). DFU's developers don't support builds or setups from third parties,
so report problems with a ModSync-installed DFU to ModSync.

## Background service

By default ModSync only acts while the app or `modsync serve` is running.
"Run in background" installs a `systemd --user` service that starts at login.
Without the app window it applies a queued Steam pin or launch-hook switch the
moment Steam exits, notices when Steam updates the game, and keeps a vault
syncing if one exists. If the service initially shares the app's process, it
starts a replacement on its next poll after the app closes, and transfers
resume. Both machines still need to be awake and connected.

Under Settings, "Run in background" shows whether the service is running,
inactive, failed or off. Open the ModSync Steam shortcut to check it in
Gaming Mode. Choosing it again while it is on stops and removes the service
without deleting mods. An open app can keep syncing.

## Logs and bug reports

Every way ModSync runs (GUI, CLI, `serve`, a Steam launch) logs to
`~/.local/state/modsync/modsync.log`. Set `MODSYNC_LOG_LEVEL=DEBUG` for more.
The launch hook logs to `~/.local/state/modsync/launch-hook.log`.

For a bug report, `modsync diagnostics` or "Copy diagnostics" under Settings
bundles the doctor report with the last 200 lines of both logs, with
pairing codes, API keys and device ids redacted.
