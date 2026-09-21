# Changelog

All notable changes to ModSync are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0-rc2] - 2026-09-20

Everything the first real desktop ↔ Steam Deck pass turned up. Pairing through
a desktop firewall, a receiving machine that syncs before it checks the game
version, SKSE installed from the Game card, and MO2 installs from the Flatpak.
Not yet exercised on the Deck itself: the join-then-auto-refresh flow and the
desktop-hosts pairing direction — this build is for that.

### Fixed

- Joining a vault no longer carries this machine's own game-version record into
  it. Choosing an instance records the local runtime in `modsync-vault.json`;
  on a machine about to copy its mods from another one that file was newer than
  the vault's and won Syncthing's conflict resolution, so the two machines could
  end up disagreeing about the version the setup was built for.
- "Install MO2…" works from the Flatpak. It refused with "protontricks is not
  installed" because it looked inside the sandbox, and would then have run
  MO2-LINT inside the sandbox, where Steam and Proton aren't visible. The
  check now asks the host for what MO2-LINT really needs (`pgrep`, `xdg-mime`;
  protontricks is bundled in MO2-LINT) and the install runs on the host.
- Reinstalling MO2 into a folder MO2-LINT still lists (after "Reset setup" and
  deleting the instance) no longer fails with "An instance with the specified
  directory already exists": ModSync has MO2-LINT forget the stale entry first.
  A folder that really holds a registered instance is refused with a pointer
  to "Use" instead, and a failed install no longer leaves an empty folder.

- Network pairing behind a firewall. The PIN handshake now listens on a fixed
  port (TCP 21029, the same number as the UDP discovery port) instead of a
  random one, so a single firewall rule covers pairing. Announcements also go
  to each interface's own broadcast address, not only `255.255.255.255`.

### Changed

- The setup wizard asks about sync before the game version, and a machine that
  copies its mods from another one finishes there: it can't know which version
  the mods need until they've arrived. The dashboard's Game card now re-checks
  by itself when the vault's version record changes and when the folder first
  finishes syncing, so the downgrade / SKSE advice appears without a manual
  refresh.
- Network pairing is harder to get lost in: the machine with the mods shows
  the PIN large inside the Sync card (with the joiner's steps and its own
  address) instead of only in the status line, and on the other machine
  picking a listed machine opens a "Pair with …" dialog that asks for that
  PIN. The first-run wizard numbers the same two steps.

### Added

- *On this machine* detects a running `ufw`/`firewalld` at launch and reads
  whether ModSync's ports are allowed (`firewall-cmd --query-port` /
  `/etc/ufw/user.rules`, falling back to the rules file's mtime where it isn't
  readable). **Allow in firewall…** adds the rules through `pkexec`; once
  they're in, the same button becomes **Remove firewall rules…** so the ports
  can be closed again after a sync or before uninstalling. Also
  `modsync firewall status | allow | remove`.
- After LAN pairing, Syncthing is told the peer's address directly instead of
  relying on its own LAN discovery, so a firewalled machine only needs to dial
  out.
- The joiner can type the host's address when the scan finds nothing (the host
  shows its address next to the PIN), and "No machines found" / "could not
  reach" messages now say which ports to open. The README documents the ports.
- Pairing logs what it announces, receives and connects to, so a bug report
  can show where discovery broke.

## [0.1.0-rc1] - 2026-09-20

First public pre-release. Ships as a Flatpak bundle attached to the GitHub
release; there is no Flathub listing. Tested on a desktop Linux install
(native and Flatpak Steam); the Steam Deck pass is what this release candidate
is for.

### Added

- `modsync --version`.
- Release workflow: pushing a `v*` tag builds the Flatpak bundle and the
  Python wheel/sdist and publishes them as a GitHub (pre-)release.

### Changed

- Add a prominent Play Skyrim button that launches through the chosen MO2
  instance and selected profile, preferring SKSE. Add a separate Open MO2 button.

- Use the updated inset Canva icon with deeper transparent rounded corners,
  preserving the supplied artwork in both the scalable SVG and 512px PNG.

- Shorten Skyrim version warnings and show the next action in
  plain language. Keep technical version details in a tooltip.

- Refresh the dashboard, setup wizard and launch hub with shared light/dark
  styling, readable secondary text, larger controls, visible keyboard focus
  and padded warnings. Replace all uses of Qt's border color for text.
- Wrap game actions and stack dashboard cards in narrower windows; keep wizard
  navigation outside its scrolling content. Show sync progress before pairing.
- Clarify that the Steam shortcut button adds ModSync as a non-Steam shortcut.
- Open the installation step directly from “Install MO2…”, add wizard step
  labels, and allow returning to the dashboard from any idle setup step.

### Fixed

- Install files that the downgrade archives ship whole, such as 1.5.97's
  binkw64.dll and Skyrim - Patch.bsa. Without them the downgraded game exited
  at startup. Restoring a downgrade removes them again.

- Show SKSE mismatch and pending Steam update warnings at normal text contrast,
  including before an MO2 instance is chosen.

- Keep the dashboard, setup wizard and launch hub open while a downgrade,
  restore or MO2 install is running. Restore now blocks competing game actions
  and launching the game until it finishes; controls recover after failures.
- Stop dashboard polling and network pairing when opening the setup wizard.
- Clear old network pairing results when rescanning, so selecting a scanning
  or failure message cannot join a machine from the previous scan. The setup
  wizard disables Finish until a machine from the new results is selected.

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
