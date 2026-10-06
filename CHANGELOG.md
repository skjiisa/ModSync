# Changelog

All notable changes to ModSync are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- An ARM64 USVFS fix for the files shipped with MO2 2.5.2. "Apply ARM64 fix"
  appears under Mod Organizer, also while Steam waits on ModSync. It
  downloads a pinned build with the injection-stub stack-alignment fix,
  verifies the archive and each binary, and backs up the originals. The build comes from ModSync's mirror of
  ndabas's release, which also carries the source, with the original release
  as a fallback. "Restore original USVFS" reverses it.
  Unknown builds are left unchanged. The same actions are available through
  `modsync mo2 usvfs status|apply|restore`; `doctor` reports the fix's status.
  Changes are refused while MO2, or any game or tool it started, still has
  the files loaded. Tested on a Steam Frame: modded Skyrim through Steam's
  Play button and MO2, Apply and Restore from the launch hub, and offline
  reapplication from the cache.

- When an instance's USVFS is 0.5.7 or newer and the game's Proton prefix
  only has the old Visual C++ runtime (or Wine's stand-in), the app
  offers to install Microsoft's current runtime into the prefix. USVFS 0.5.7+
  uses the runtime of the programs MO2 starts, so without it nothing started
  from MO2 runs. The pinned, checked redistributable is installed silently
  through the game's Proton after backing up the DLLs it replaces; it never
  happens without a click. `modsync doctor` shows each instance's USVFS
  version and this check. Tested on a Steam Frame, from the Flatpak too.
- An aarch64 Flatpak bundle next to the x86_64 one, for ARM machines such as
  the Steam Frame. It bundles the arm64 Syncthing build. Not yet tested on an
  ARM device.

### Changed

- Consolidated navigation into Home, Sync and Settings. Home now offers MO2
  installation and selection, matching SKSE, relevant repairs and launch
  actions. Game and MO2 maintenance open in sheets with a fixed Close button.
  Home shows sync status only when sync is enabled, and includes Steam setup.
  The guided setup prepares MO2, then the game and SKSE, then optional sync.
  An early copy route pairs before checking the incoming setup's game version.
  Home layouts are checked at the Steam Deck's 1280 × 800 resolution.
- Steam's Play button now leads to the same Play Skyrim and Open Mod
  Organizer 2 as opening ModSync directly: the selected MO2 profile, SKSE
  when it is installed, and the same checks. Before, Continue only carried on
  to whatever Steam was about to start, which without MO2-LINT's redirector
  was the unmodded game. ModSync hands the MO2 command line back to the
  launch hook, which runs it as Steam's own launch, so Steam still tracks the
  game and syncs its cloud saves when it exits. Verified on the desktop: a
  save written while MO2 ran under Steam was uploaded right after it closed,
  with a matching SHA-1. Continue remains for a machine with no MO2 instance
  chosen. Existing hooks are updated when ModSync opens.

- A new interface, designed for a controller first. The dashboard and its
  dialogs are gone. The window now has five sections (Home, Game, Mod
  Organizer, Sync and System), switched with LB and RB, Q and E, or by moving
  up onto the tabs. Every action is a large tile that shows the description a
  tooltip used to hide. A single outline marks the focused tile, and a hint
  bar shows which button does what, with PlayStation symbols for a
  PlayStation controller. Questions, text entry, PIN entry and folder picking
  open in sheets inside the window instead of separate dialogs, and the hint
  bar names what A does on the focused control. Text entry has an on-screen
  keyboard with a symbols layer, shifted punctuation, cursor movement,
  selection and a Paste key, and the PIN pad works like a combination lock.
  Home recommends a repair when the game version or SKSE does not match, or
  Steam has an update waiting. Play stays available with the consequence
  explained. Home has a readiness checklist (game version, SKSE, Mod
  Organizer 2, sync) next to Play, or Continue when Steam's Play button
  opened ModSync. The setup wizard reuses the same parts. Its last step keeps
  Finish at the top, or "Finish anyway" with the repair focused when the game
  version still doesn't match. System and Game use shorter tile
  descriptions, and System's settings details and controls open in their own
  sheets. B on Home, or "Quit ModSync" under System, asks to quit, so ModSync
  can be closed without a mouse. A mouse or touch screen works on its own
  too: sections scroll with a finger, the hint bar becomes buttons for Back,
  Cancel and Quit, and the PIN sheet has digits to tap. ModSync always uses its dark theme now.
- Controllers work two ways. Outside games, Steam Input's desktop
  configuration turns buttons into keys, and ModSync reads them back as the
  buttons that sent them: Space is Y on a Steam Deck and B on a Steam
  Controller, Xbox or PlayStation pad, Page Up/Down are X/Y on the latter, and
  a tap of Ctrl or Alt is LB or RB. It tells which applies from the hardware
  in `/sys`. ModSync also reads controllers directly through SDL2, from the
  Flatpak runtime or SteamOS, which covers the virtual pad Steam provides in
  Gaming Mode. A press that arrives both ways counts once, and presses are
  ignored while another window is in front. The Flatpak gains
  `--device=input` for this. `MODSYNC_GAMEPAD=0` turns direct reading off.
  The controls are listed in docs/advanced.md.
- Steam's Play button opens the regular ModSync window instead of a separate
  launch hub. While Steam waits, Play turns into Continue, which hands the
  launch on to MO2-LINT or the game, and "Cancel launch" returns to Steam.
  Open MO2 is hidden then, so ModSync never starts a second Proton in the
  game's prefix next to Steam's. The Mod Organizer 2 card shows the selected
  profile and its enabled mod count. A warning above Continue says when the
  vault is still syncing. Esc no longer cancels the launch.
- Install MO2 uses MO2-LINT 7.0.1 (was 7.0.0-rc7). The download is checked
  against the release's SHA-256, and the cached copy is named by version so
  a new pin replaces the old binary instead of reusing it.
- On ARM64 machines such as the Steam Frame, the Play button hook finds
  Valve's ARM64 Proton builds and hands off to the one Steam would run.
- On machines other than x86_64, Install MO2 says up front that MO2-LINT
  only ships an x86_64 build. It no longer tries to run that build.
- When a game is left on "Default" in Steam, the Play button hook now hands
  off to the Proton Steam would pick: Valve's recommendation for the game on a
  Steam Deck or Steam Frame, then your global choice, then Proton stable. It
  used to prefer Proton Experimental.
- On the Steam Frame, where Steam can't be restarted, queued changes (the
  Play button switch, the update pin) tell you to reboot instead. The background
  service applies them before Steam starts. Tested on a Frame.

### Fixed

- Play refuses an MO2 instance that hasn't been told which game it manages,
  and says to finish setting it up in MO2. MO2 would otherwise stop at
  "Select the game managed by this instance".

- Steam Cloud stopped syncing Skyrim saves while the Play button switch was
  on. Steam maps a game's Windows save folders into its Proton prefix only
  for a compatibility tool whose internal name contains "proton", and the
  hook was named `modsync_489830_hub`. Steam couldn't resolve the save folder,
  so it skipped every save while still reporting the sync as complete. The
  hook is now `modsync_489830_proton`. When ModSync opens or the
  background service starts, an existing hook is moved to the new name and
  Steam is switched over to it, after a Steam restart if Steam is running.
  Saves made under the old name are still in the prefix, where Steam finds
  them again once the game launches through the new name.
- In the Flatpak, `modsync doctor` and `modsync diagnostics` pointed at a
  launch hook log inside the sandbox that is never written. Steam runs the
  hook on the host, so they now read the host's `~/.local/state/modsync/launch-hook.log`.
- When Steam couldn't be closed and you picked the Play button switch under
  Properties, then Compatibility yourself, ModSync kept saying it was waiting
  for Steam to close, and would have re-applied the switch the next time
  Steam closed, undoing any choice made since. A queued switch that is
  already in place is now dropped. Tested on a Steam Frame.
- With MO2-LINT's tool selected and the Play button switch off, the status
  now says Play opens Mod Organizer 2 (MO2-LINT), not the game directly.
- The Flatpak didn't recognise SteamOS: inside the sandbox `/etc/os-release`
  describes the Flatpak runtime. ModSync now reads the host's
  `/run/host/os-release`, so on a Steam Frame it says to reboot instead of
  restarting Steam, and Valve's Steam Deck/Frame Proton recommendations apply.
  Tested on a Frame.
- `modsync service install` failed in the Steam Frame's desktop terminal,
  because the nested Plasma session's `XDG_RUNTIME_DIR` has no systemd user
  bus. ModSync now falls back to the login session's. The Flatpak failed the
  same way, since `flatpak-spawn --host` runs `systemctl` in that nested
  session, and now passes the login session's directory to the host.
- On ARM64, ModSync no longer picks one of Valve's x86_64 Protons, which can't
  run there. When Skyrim is set to a Proton with no ARM64 build, the launch
  hook and Open MO2 use Steam's ARM64 default, as Steam does.
- MO2 2.5 crashed at start-up, writing hundreds of crash dumps, when Skyrim's
  Proton prefix had only the 2016 Visual C++ runtime that the game installs.
  When the prefix's runtime is missing, Wine's stand-in or older than 14.40,
  Open MO2 and Play now first copy a current runtime (14.44) next to
  `ModOrganizer.exe`, unpacked from Microsoft's redistributable. It is
  downloaded once and checked against a pinned SHA-256; the prefix is not
  changed. Prefixes set up by Install MO2 already have a current runtime, so
  nothing is downloaded there. Tested on a Steam Frame.
- On the Steam Frame's desktop, MO2 opened with Open MO2 or Play closed as soon
  as one of its dialogs closed. The desktop is nested inside gamescope, and
  Proton put MO2 on gamescope's display but the rest of Wine on the desktop's.
  ModSync now runs the whole launch on gamescope's display, with gamescope's
  Vulkan layer off, and says to use a mouse or the keyboard there: the VR
  pointer still makes MO2 close (Wine lacks a touch function Qt calls).
  Tested on a Frame.

## [1.0.0] - 2026-09-21

First stable release. Everything in the two release candidates, now tested end
to end on a Steam Deck and on a fresh Linux user account: the guided Mod
Organizer 2 install, game version detection with downgrade and Steam pin,
SKSE installation, the Steam Play button hook, and optional sync between
machines.

### Changed

- Rewrote the README and moved the technical detail into `docs/advanced.md`.
- Plainer wording throughout the app: shorter tooltips and dialogs, no
  decorative symbols, and the launch hook status no longer says "off" twice.

## [0.1.0-rc2] - 2026-09-20

Everything the first desktop and Steam Deck pass turned up: pairing through a
desktop firewall, a receiving machine that syncs before it checks the game
version, SKSE installed from the Game card, and MO2 installs from the Flatpak.

### Fixed

- Joining a vault no longer carries this machine's own game-version record
  into it. Choosing an instance records the local runtime in
  `modsync-vault.json`. On a machine about to copy its mods from another one,
  that file was newer than the vault's and won Syncthing's conflict
  resolution, so the two machines could disagree about the version the setup
  was built for.
- "Install MO2..." works from the Flatpak. It refused with "protontricks is not
  installed" because it looked inside the sandbox, and would then have run
  MO2-LINT inside the sandbox, where Steam and Proton are not visible. The
  check now asks the host for what MO2-LINT needs (`pgrep`, `xdg-mime`;
  protontricks is bundled in MO2-LINT) and the install runs on the host.
- Reinstalling MO2 into a folder MO2-LINT still lists (after "Reset setup" and
  deleting the instance) no longer fails with "An instance with the specified
  directory already exists". ModSync has MO2-LINT forget the stale entry
  first. A folder that holds a registered instance is refused with a pointer
  to "Use" instead, and a failed install no longer leaves an empty folder.
- Network pairing works behind a firewall. The PIN handshake now listens on a
  fixed port (TCP 21029, the same number as the UDP discovery port) instead
  of a random one, so a single firewall rule covers pairing. Announcements
  also go to each interface's own broadcast address, not only
  `255.255.255.255`.

### Changed

- The setup wizard asks about sync before the game version. A machine that
  copies its mods from another one finishes there, because it cannot know
  which version the mods need until they have arrived. The dashboard's Game
  card re-checks by itself when the vault's version record changes and when
  the folder first finishes syncing, so the downgrade and SKSE advice appears
  without a manual refresh.
- Network pairing is easier to follow. The machine with the mods shows the
  PIN large inside the Sync card, with the joiner's steps and its own address,
  instead of only in the status line. On the other machine, picking a listed
  machine opens a "Pair with ..." dialog that asks for that PIN. The first-run
  wizard numbers the same two steps.

### Added

- *On this machine* detects a running `ufw` or `firewalld` at launch and reads
  whether ModSync's ports are allowed (`firewall-cmd --query-port` or
  `/etc/ufw/user.rules`, falling back to the rules file's mtime where it is
  not readable). "Allow in firewall..." adds the rules through `pkexec`. Once
  they are in, the same button becomes "Remove firewall rules..." so the
  ports can be closed again after a sync or before uninstalling. The same is
  available as `modsync firewall status | allow | remove`.
- After LAN pairing, Syncthing is told the peer's address directly instead of
  relying on its own LAN discovery, so a firewalled machine only needs to
  dial out.
- The joiner can type the host's address when the scan finds nothing. The
  host shows its address next to the PIN, and the "No machines found" and
  "could not reach" messages say which ports to open. The README documents
  the ports.
- Pairing logs what it announces, receives and connects to, so a bug report
  can show where discovery broke.

## [0.1.0-rc1] - 2026-09-20

First public pre-release, shipped as a Flatpak bundle attached to the GitHub
release. Tested on a desktop Linux install with native and Flatpak Steam.

### Added

- Discovery (`modsync doctor`, standard library only): finds Steam libraries,
  the Skyrim SE install and its Proton prefix, and existing MO2 instances.
- Guided Mod Organizer 2 install as a portable instance wired into the game's
  Proton prefix, using MO2-LINT as the backend (`modsync mo2 ...`).
- Game version management: detects the installed Skyrim SE runtime and the
  SKSE runtime, downgrades the game natively with community xdelta patches
  from a bundled, auto-refreshed recipe index, and pins Steam so the
  downgraded install keeps launching (`modsync game status | downgrade | pin`).
- Optional sync between machines with a bundled Syncthing daemon: vault
  create and join, PIN-based LAN pairing, and a per-machine `ModOrganizer.ini`
  (`modsync sync create | join`).
- Background service (`modsync serve`, `modsync service install [--linger]`)
  that runs as a `systemd --user` unit and works without a vault.
- PySide6 UI: a first-run wizard (instance, game version, optional sync) and a
  dashboard with independent Game, Mod Organizer 2 and Sync cards.
- Steam integration: add ModSync as a non-Steam game for Gaming Mode
  (`modsync steam shortcut`) and a launch hook that opens ModSync when Skyrim
  is launched from Steam (`modsync launch enable | disable | status`).
- Flatpak packaging under `packaging/flatpak/` (manifest, desktop entry,
  AppStream metainfo, icon) with bundled Syncthing, xdelta3 and 7zz.
- App logo and window icon. GPL-3.0-or-later license.
- A scheduled workflow that keeps the downgrade recipe index in step with
  Mulderland's recipe.
- CI (unit tests, desktop and AppStream validation, flatpak-builder-lint),
  CHANGELOG, CONTRIBUTING guide and issue templates.
- `modsync --version`.
- Release workflow: pushing a `v*` tag builds the Flatpak bundle and the
  Python wheel and sdist and publishes them as a GitHub pre-release or
  release.

### Changed

- Add a Play Skyrim button that launches through the chosen MO2 instance and
  selected profile, preferring SKSE. Add a separate Open MO2 button.
- Use the updated inset Canva icon with deeper transparent rounded corners,
  keeping the supplied artwork in both the scalable SVG and the 512 pixel PNG.
- Shorten Skyrim version warnings and show the next action in plain language.
  Keep technical version details in a tooltip.
- Refresh the dashboard, setup wizard and launch hub with shared light and
  dark styling, readable secondary text, larger controls, visible keyboard
  focus and padded warnings. Replace all uses of Qt's border color for text.
- Wrap game actions and stack dashboard cards in narrower windows. Keep
  wizard navigation outside its scrolling content. Show sync progress before
  pairing.
- Say that the Steam shortcut button adds ModSync as a non-Steam shortcut.
- Open the installation step directly from "Install MO2...", add wizard step
  labels, and allow returning to the dashboard from any idle setup step.

### Fixed

- Install files that the downgrade archives ship whole, such as 1.5.97's
  binkw64.dll and Skyrim - Patch.bsa. Without them the downgraded game exited
  at startup. Restoring a downgrade removes them again.
- Show SKSE mismatch and pending Steam update warnings at normal text
  contrast, including before an MO2 instance is chosen.
- Keep the dashboard, setup wizard and launch hub open while a downgrade,
  restore or MO2 install is running. Restore now blocks competing game
  actions and launching the game until it finishes. Controls recover after
  failures.
- Stop dashboard polling and network pairing when opening the setup wizard.
- Clear old network pairing results when rescanning, so selecting a scanning
  or failure message cannot join a machine from the previous scan. The setup
  wizard disables Finish until a machine from the new results is selected.
