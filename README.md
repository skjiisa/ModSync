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

## Status

Early development. Working toward the MVP described in the plan.

- [x] Phase 0 — project scaffold
- [x] Phase 1 — Steam library + MO2 instance discovery
- [x] Phase 2 — Syncthing core (bundled, REST-controlled, pairing) — *two-node sync verified*
- [ ] Phase 3 — guided MO2 install (MO2-LINT / Jackify backends)  ← *next*
- [ ] Phase 4 — PySide6 wizard + dashboard
- [ ] Phase 5 — Flatpak + Steam Deck
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
