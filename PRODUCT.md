# Product

<!-- impeccable:product-schema 1 -->

## Platform

Native Linux desktop app (Qt 6 / PySide6), shipped as a Flatpak. Runs on SteamOS in Gaming Mode (gamescope, always maximized, 1280 × 800 on a Steam Deck) and desktop mode, and on other Linux desktops. None of `web`, `ios`, `android` or `adaptive` applies.

## Users

People modding Skyrim Special Edition on a Steam Deck, first and foremost in Gaming Mode with the controller in their hands and the screen at arm's length. Second: the same people on a Steam Machine, Steam Frame or Linux desktop, with a mouse, keyboard or touch screen. Many are not Linux experts; they want Mod Organizer 2 and a modded Skyrim working without a terminal.

## Product Purpose

ModSync turns a stock Steam copy of Skyrim SE into a working modded setup on SteamOS/Linux and keeps it working: install or adopt Mod Organizer 2 (through MO2-LINT), match the game version the mods need (downgrade, pin against Steam updates, restore), install the matching SKSE, launch Skyrim or MO2 through Proton, and optionally sync the mod list between machines (Syncthing underneath). Success is pressing Play and having the modded game start, on every machine the user owns.

## Positioning

A controller-first modding hub that lives inside SteamOS: it can sit in front of Steam's own Play button (the launch hook), repair the game version Steam just changed, and keep several machines' MO2 setups identical while each keeps its own game paths.

## Operating Context

- Gaming Mode: launched from the Steam library (a ModSync shortcut) or by Steam's Play button through the launch hook, in which case Steam is waiting and the window must end in Play, Open MO2, Continue or Cancel launch.
- Input: Steam Deck built-in controls and the Steam Controller arrive either as SDL gamepads or as Steam Input's default desktop keyboard/mouse bindings, which ModSync maps back to controller actions; Xbox and PlayStation pads; keyboard; mouse; touch screen (including the Steam Frame pointer, which arrives as touch). The hint bar shows the glyphs of the device in use.
- Sections: Home, Sync, Settings, with Game version and MO2 maintenance opened as sheets, a three-step setup wizard, an on-screen keyboard and a PIN pad for LAN pairing.

## Capabilities and Constraints

- Every action must be reachable with d-pad/stick + A/B/X/Y + bumpers alone; B goes back, B on Home asks to quit. Every action must also be tappable and clickable.
- Long-running operations (downgrade, install, sync setup) make the window busy; nothing else can change the setup meanwhile.
- 1280 × 800 is the design size; all standard Home actions must fit there without scrolling.
- Text contrast is tested (WCAG AA) against every surface in `tests/test_ui_theme.py`.

## Brand Commitments

- The app icon (`packaging/flatpak/icons/io.github.skjiisa.ModSync.png`, a Canva export: bright blue hex-pattern ground, a heavy black "M" in a white sticker outline, a white circle with sync arrows) is the only binding visual asset. It is not redrawn or recolored.
- Everything else about the previous look (frost teal palette, aurora/mountain backdrop, glowing tiles) is open to replacement.
- UI copy may be rewritten to read naturally, as long as facts and behavior stay the same.

## Evidence on Hand

- `docs/ui-review/*.png`: 1280 × 800 renders of every screen from `scripts/preview_ui.py` (synthetic data).
- No user research, testimonials or usage numbers exist; do not invent them.

## Product Principles

1. Play is the point. Whatever state the setup is in, the next useful action is obvious and one press away.
2. Controller first, never controller only. Every control works the same way with a pad, keys, mouse or a finger.
3. Explain consequences, not mechanics. Say what happens to the game or the mods, briefly, at the moment it matters.
4. Never surprise the user's files: destructive or long operations are confirmed and show progress.

## Accessibility & Inclusion

Readable at arm's length on a 7" 1280 × 800 screen: generous type, AA contrast, large hit targets, a focus indicator that is unmistakable without color alone.
