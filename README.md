# ModSync

Get [Mod Organizer 2](https://github.com/ModOrganizer2/modorganizer) modding of
**Skyrim Special Edition** working on **Steam Deck / SteamOS** and ordinary Linux
(Windows is a post-1.0 target), and keep it working:

- **Install Mod Organizer 2** as a portable instance wired into the game's Proton
  prefix (guided, via [MO2-LINT](https://github.com/Furglitch/modorganizer2-linux-installer)).
- **Keep the game on the version your mods need.** SKSE and native DLL mods only
  load on the exact runtime they were built for, and Steam updates the game
  silently. ModSync detects the installed version and the SKSE runtime, downgrades
  the game with community xdelta patches, and pins Steam so it keeps launching the
  downgraded install.
- **Optionally, sync the whole setup between machines** — desktop ↔ Steam Deck —
  with [Syncthing](https://syncthing.net/), while each machine keeps its own game
  paths.
- **Optionally, open ModSync when Skyrim is launched from Steam.** Play shows the
  mod setup, sync state and any game-version / SKSE problem first, with *Continue
  to Mod Organizer* and *Cancel*.

Each part stands on its own: someone who only wants the downgrader never creates a
vault. The dashboard is three cards — *Game*, *Mod Organizer 2*, *Sync* — and the
wizard walks the same steps with sync as a "Not now" option.

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
  removed too. If recovery fails or the process is killed, originals remain in
  `.modsync-downgrade/backup`; restore them before retrying. xdelta3 verifies the
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
modsync game status                 # installed vs vault version, Steam state, recipes
modsync game downgrade 1.6.1170     # needs xdelta3 and 7z (or bsdtar) installed
modsync game pin                    # after the next Bethesda patch, with Steam closed
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
modsync mo2 status | use <dir> | install <dest>
modsync game status | downgrade <version> | pin
modsync sync create | join          # optional
modsync serve                       # foreground loop; what the background service runs
modsync service install [--linger] | status | uninstall
modsync steam shortcut              # add ModSync as a non-Steam game (Gaming Mode)
modsync launch status | enable | disable   # open ModSync when Skyrim is launched from Steam
```

## Status

Early development. Working toward the MVP described in the plan.

- [x] Phase 0 — project scaffold
- [x] Phase 1 — Steam library + MO2 instance discovery
- [x] Phase 2 — Syncthing core (bundled, REST-controlled, pairing) — *two-node sync verified*
- [x] Phase 4 — PySide6 wizard + dashboard — *full wizard→daemon→dashboard flow verified*
- [x] Phase 3 — guided MO2 install (MO2-LINT backend) — *real Skyrim SE install verified*
- [ ] Phase 5 — Flatpak + Steam Deck  ← *next*
- [ ] Post-1.0 — Windows target

## Try the discovery report

The discovery layer uses only the Python standard library, so no install is needed:

```sh
python3 -m modsync doctor
```

It reports the Steam libraries, whether Skyrim SE is installed, its Proton prefix,
and any MO2 instances it can find on this machine.

## Development

```sh
# unit tests (fast, offline):
.venv/bin/python -m unittest discover -s tests -t .

# integration tests — download + run a real Syncthing daemon, incl. a two-node
# sync test proving a mod propagates while each machine keeps its ModOrganizer.ini:
MODSYNC_IT=1 .venv/bin/python -m unittest tests.test_sync_integration -v
```
