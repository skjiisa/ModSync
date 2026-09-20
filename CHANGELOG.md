# Changelog

All notable changes to ModSync are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Early development; nothing has been released yet. What exists so far:

### Fixed

- Show SKSE mismatch and pending Steam update warnings at normal text contrast,
  including before an MO2 instance is chosen.

- Keep the dashboard, setup wizard and launch hub open while a downgrade,
  restore or MO2 install is running. Restore now blocks competing game actions
  and launching the game until it finishes; controls recover after failures.
- Stop dashboard polling and network pairing when opening the setup wizard.

### Added

- **Discovery** (`modsync doctor`, stdlib only): finds Steam libraries, the
  Skyrim SE install and its Proton prefix, and existing MO2 instances.
- **Guided Mod Organizer 2 install** as a portable instance wired into the
  game's Proton prefix, using MO2-LINT as the backend (`modsync mo2 ...`).
- **Game version management**: detects the installed Skyrim SE runtime and the
  SKSE runtime, downgrades the game natively with community xdelta patches from
  a bundled, auto-refreshed recipe index, and pins Steam so the downgraded
  install keeps launching (`modsync game status | downgrade | pin`).
- **Optional sync between machines** with a bundled Syncthing daemon:
  vault create/join, PIN-based LAN pairing, per-machine `ModOrganizer.ini`
  (`modsync sync create | join`).
- **Background service** (`modsync serve`, `modsync service install
  [--linger]`) that runs as a `systemd --user` unit and works without a vault.
- **PySide6 UI**: first-run wizard (instance, game version, optional sync) and a
  dashboard with independent *Game*, *Mod Organizer 2* and *Sync* cards.
- **Steam integration**: add ModSync as a non-Steam game for Gaming Mode
  (`modsync steam shortcut`) and a launch hook that opens ModSync when Skyrim
  is launched from Steam (`modsync launch enable | disable | status`).
- **Flatpak packaging** under `packaging/flatpak/` (manifest, desktop entry,
  AppStream metainfo, icon) with bundled Syncthing, xdelta3 and 7zz.
- App logo and window icon; GPL-3.0-or-later license.
- Scheduled workflow that keeps the downgrade recipe index in step with
  Mulderland's recipe.
- CI (unit tests, desktop/AppStream validation, flatpak-builder-lint),
  CHANGELOG, CONTRIBUTING guide and issue templates.
