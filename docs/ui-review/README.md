# UI previews

The screenshots in this folder come from `scripts/preview_ui.py`, which
renders the window with synthetic game, setup and sync data. Generating them
does not read your setup, install anything, modify the game or sync. They are
1280 × 800, the Steam Deck's screen.

## Home

Home recommends a repair when the game version or SKSE does not match, or
Steam has an update waiting. The recommendation gets the accent fill; Play
stays available with the consequence explained. Each readiness check opens
the section that can fix it. A single outline marks focus.

![Home](home.png)

When the game is ready, Play becomes the primary action again.

![Ready to play](home-ready.png)

## Opened from Steam's Play button

With the launch hook on, Steam's Play button opens this same window. Play
becomes Continue, which hands Steam's launch on to MO2-LINT or the game.
Cancel launch, one move down, returns to Steam. A warning appears while the
vault is still syncing. Here a keyboard is in use, so the hints show keys.

![Opened from Steam](launch.png)

## Game

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

## System

Short descriptions keep the actions visible together. Settings details and
Controls open in sheets with the background-service explanation, Steam
launch status, firewall ports and input shortcuts.

![System](system.png)

## Setup wizard

![Setup, step 1](setup-1.png)

The game step emphasizes the action that fixes the unresolved problem.
Finish anyway explains what will remain unresolved. When the version is
ready, Finish is the primary action and optional installs are secondary.

![Setup, game version](setup-3.png)

## Regenerate the previews

From a source checkout:

```sh
uv run python scripts/preview_ui.py --output scratch/ui-review
```

This writes all 26 offline previews, including the confirmation sheet, the
folder browser, the install sheet, the network PIN and a 1000 pixel wide
window used with a mouse, keyboard symbols and selection, and validation at
1000 pixels wide, using Qt's offscreen platform. This folder keeps
one screenshot per screen to stay small. Copy the ones you want here and
compress them with `optipng`. The installed Flatpak
is a separate build and does not pick up source edits.
