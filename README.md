# ModSync

Sync a [Mod Organizer 2](https://github.com/ModOrganizer2/modorganizer) mod setup
across multiple machines — primarily **Steam Deck / SteamOS**, but also ordinary
Linux (and Windows as a post-1.0 target). Built around portable MO2 instances and
[Syncthing](https://syncthing.net/) as the sync engine.

First supported game: **Skyrim Special Edition** (Steam cloud already handles saves,
so only the portable MO2 instance needs syncing).

## How it works (the core idea)

A portable MO2 instance is *almost* entirely portable content (`mods/`, `profiles/`,
`downloads/`, `overwrite/`). The only machine-specific data — absolute paths to the
game, the Proton prefix, and tool executables — lives in **`ModOrganizer.ini`**.

So ModSync makes the **Syncthing shared folder = the MO2 instance folder**, and
**excludes `ModOrganizer.ini`** (plus logs/cache) via `.stignore`. Each machine keeps
its own correct `ModOrganizer.ini` (written once by the installer), and everything
else syncs. This also makes Steam Deck ↔ Windows sync work cleanly, because the files
that differ between OSes are exactly the ones that never sync.

### Game version check

SKSE and every native DLL mod are compiled against one exact Skyrim runtime
(1.5.97, 1.6.1170, 1.7.104, …), and Steam updates the game silently. A vault that
works on the desktop can stop working on the Deck the day Steam updates one of them.
So when a vault is created, ModSync records the installed runtime in
`modsync-vault.json` inside the instance (the one ModSync-owned file that *does*
sync). Every machine reads its own `SkyrimSE.exe` version and the dashboard,
`modsync doctor`, and `modsync vault serve` all warn when it differs from the vault.
After an intentional upgrade or downgrade, "Use this machine's version" on the
dashboard re-records it.

### Downgrading the game (and still launching it from Steam)

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
  moment Steam exits (on the Deck: Power → Restart Steam).

```sh
modsync game status                 # installed vs vault version, Steam state, recipes
modsync game downgrade 1.6.1170     # needs xdelta3 and 7z (or bsdtar) installed
modsync game pin                    # after the next Bethesda patch, with Steam closed
```

The Flatpak bundles xdelta3 and 7zz. Downgrading to 1.6.x on a Steam Deck brings
back the on-screen-keyboard crash that 1.7.99 fixed; the "Steam Deck Keyboard Fix
for Skyrim" SKSE plugin works around it.

## Status

Early development. Working toward the MVP described in the plan.

- [x] Phase 0 — project scaffold
- [x] Phase 1 — Steam library + MO2 instance discovery
- [x] Phase 2 — Syncthing core (bundled, REST-controlled, pairing) — *two-node sync verified*
- [x] Phase 4 — PySide6 wizard + dashboard — *full wizard→daemon→dashboard flow verified*
- [x] Phase 3 — guided MO2 install (MO2-LINT backend) — *real Skyrim SE install verified*
- [ ] Phase 5 — Flatpak + Steam Deck  ← *next*
- [ ] Post-1.0 — Windows target

### Background sync

By default, closing ModSync stops the Syncthing process it started. **Run in
background** installs a user service that starts at login and keeps the vault
available without the app window. If the service initially shares the app's
process, it starts a replacement on its next poll after the app closes; transfers
resume automatically. Both machines still need to be awake and connected.

The dashboard shows **Background service: running / inactive / failed / off**
separately from folder sync progress. Open the ModSync Steam shortcut to check it
in Gaming Mode. The service is independent of the desktop, but Steam Deck mode
transitions still need testing on hardware. **Turn off background sync** stops
and removes the service without deleting mods; an open app can continue syncing.

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
