# UI previews

The screenshots in this folder come from `scripts/preview_ui.py`, which
renders the window with synthetic game, setup and sync data. Generating them
does not read your setup, install anything, modify the game or sync. They are
1280 × 800, the Steam Deck's screen.

## Home

Home, Sync and Settings are the three tabs. Home offers installation,
selection, repairs and launching without requiring a tab change. The MO2,
game version and SKSE rows open maintenance sheets for the remaining controls.
All standard Home actions fit at 1280 × 800 without scrolling or smaller fonts.

On first run, install MO2 directly, use an existing installation, or follow
guided setup. Copying an existing setup from another machine has an early
entry point too.

![First-run Home](home-welcome.png)

A repair runs from Home and shows its progress there. Play remains available
with any launch consequence explained. A single outline marks focus.

![Home with a repair](home.png)

When the setup is ready, Play becomes the primary action.

![Ready to play](home-ready.png)

Missing SKSE is an optional setup suggestion. Play remains primary and keeps
default controller focus, including when Steam opens ModSync.

![Matching SKSE installation](home-skse.png)

The Steam launch entry shows On, Off, Pending or Needs attention. It opens a
sheet with the launch hook and Steam shortcut controls.
The same settings remain available in Settings.

![Steam setup](steam-setup.png)

## Opened from Steam's Play button

With the launch hook on, Steam's Play button opens this same window. Play
uses the selected MO2 setup. Before MO2 is chosen, Continue hands the launch
on to MO2-LINT or the game. Cancel launch returns to Steam. A warning appears
while the vault is still syncing. Here a keyboard is in use, so the hints show keys.

![Opened from Steam](launch.png)

Missing SKSE keeps Play as the default Steam launch choice.

![Steam launch with optional SKSE](launch-skse-optional.png)

Copying a setup waits for the source's game-version record before offering
version-dependent repairs. The waiting state survives reopening the app.

![Waiting for the source's version](home-waiting.png)

## Game maintenance

The Game version row opens this sheet. Restore, unpin and technical details
remain available here. Its content scrolls; Close stays visible.

![Game version](game.png)

## Sync

The live view with a PlayStation controller connected. The hint bar shows
PlayStation symbols. Y (triangle) rescans.

![Live sync](sync-live.png)

Pairing with a machine found on the network, here with a mouse. With a
controller, up and down turn each digit and left and right move between
digits; with a mouse or touch screen, the digit row fills them in. Typed
digits work too. With a mouse or touch screen the hint bar turns into
buttons for what can't be tapped elsewhere, such as Cancel.

![PIN pad](pin-pad.png)

Text entry opens an on-screen keyboard with a Paste key. Shift produces
punctuation, and "#+=" opens the full symbols layer. LB / RB or the arrow
buttons move the cursor. Select and Select all let a controller replace part
or all of an entry. The cursor and selection stay visible while a key has
focus.

![Editing text](keyboard-editing.png)

## Settings

Short descriptions keep the actions visible together. Settings details and
Controls open in sheets with the background-service explanation, Steam
launch status, firewall ports and input shortcuts.

![Settings](system.png)

## Setup wizard

Choose the copy route before selecting MO2 to pair ahead of game preparation.

![Setup, step 1](setup-1.png)

The second step prepares the game version and matching SKSE. Continue anyway
explains an unresolved problem; Continue without SKSE explains which mods
need it. The final step makes sync optional, with Finish without sync first.
The early copy route skips game preparation until the incoming mods arrive.

![Setup, game and SKSE](setup-2.png)

![Setup, optional sync](setup-3.png)

## Hardware smoke test before merging

The automated checks render Qt offscreen and exercise copying with two local
Syncthing daemons. Steam and Proton on Deck hardware still need this check:

1. On the Deck, launch through Steam with SKSE missing. A must play, with the
   optional installation action remaining secondary.
2. Share a source setup whose game version differs from the Deck's. Select
   the early copy route and pair using the intended network/PIN or code flow.
3. Take the source offline before its version record arrives. Reopen ModSync
   on the Deck; Home must still say Waiting and offer no version-dependent repair.
4. Bring the source online. After its record arrives, verify that Home offers
   the source's required version and that the source record remains unchanged.
5. Finish game/SKSE setup, then verify Steam launches the selected MO2 profile.
   Check the Steam launch status and that all Home actions fit at 1280 × 800.

## Regenerate the previews

From a source checkout:

```sh
uv run python scripts/preview_ui.py --output scratch/ui-review
```

This writes offline previews, including the confirmation sheet, the
folder browser, the install sheet, the network PIN and a 1000 pixel wide
window used with a mouse, keyboard symbols and selection, and validation at
1000 pixels wide, using Qt's offscreen platform. This folder keeps
selected setup, ready, repair and maintenance screenshots. Copy the ones you
want here and compress them with `optipng`. The installed Flatpak
is a separate build and does not pick up source edits.
