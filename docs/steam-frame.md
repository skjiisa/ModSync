# ModSync on the Steam Frame

Modded Skyrim works on the Steam Frame through ModSync. The Frame runs SteamOS
on ARM64 and plays Windows games with Proton 11's ARM64 build, which changes a
few steps compared with a Steam Deck. This page covers those steps and what
doesn't work yet.

## Install ModSync

Download `ModSync-<version>-aarch64.flatpak` from the
[releases page](https://github.com/skjiisa/ModSync/releases). The `x86_64`
bundle is for the Steam Deck and other PCs and won't install on the Frame.

In Desktop Mode, open the file with Discover, or run:

```sh
flatpak install --user ~/Downloads/ModSync-*-aarch64.flatpak
flatpak run io.github.skjiisa.ModSync
```

The Frame's pointer arrives in ModSync as touch, so every action is a tile to
tap. A controller, a keyboard or a mouse works too.

## Get Mod Organizer 2

**Install Mod Organizer 2** on Home works on the Frame. ModSync downloads
MO2-LINT's ARM64 build, which fetches the `cabextract` and 7-Zip that SteamOS
lacks. You can't close Steam on the Frame, so ModSync doesn't ask you to:
MO2-LINT installs with Steam running. Reboot when it finishes, so Steam's Play
button picks up the new instance. The new instance then needs the update to the
ARM64 fix described below.

An MO2 2.5.2 instance you already have works too. Choose **Use an existing
installation…** on Home, or run `modsync mo2 use <folder>`.

## The ARM64 fix for MO2

MO2's virtual file system, USVFS, has two problems on the Frame. Without any
fix, every program MO2 starts, Skyrim included, fails before it opens. With
only the first fix, Skyrim started through SKSE runs without your mods, even
though MO2 lists them. [USVFS on ARM64](usvfs-arm64.md) explains both and what
ModSync installs.

Every instance needs ModSync's fix once:

- An instance from **Install Mod Organizer 2**, or any MO2-LINT 7.0.3 or newer,
  comes with the first fix only. Home shows **Update ARM64 fix**.
- Instances installed before MO2-LINT 7.0.3, or from the official MO2 archive,
  have neither. Home shows **Apply ARM64 fix**.

Home suggests it as the next step, and the Mod Organizer 2 row says "Needs a
fix" until it's done. It's also in the Mod Organizer 2 sheet, which opens from
that row. Close MO2, Skyrim and anything started from MO2 first. ModSync checks
and refuses while they run. The download is about 13 MB.

**Restore original USVFS** in the same sheet puts MO2's original files back.
An instance that came with the first fix never had them, so it has nothing to
restore.

## Play

Play Skyrim through Steam's Play button, with ModSync's Play hook on (see
below), or from ModSync. Through Steam, the Frame's pointer works in Mod
Organizer 2.

In Desktop Mode, use a mouse or keyboard in Mod Organizer 2. The pointer
arrives as touch there, and Wine lacks a function MO2's Qt needs for touch
([#70](https://github.com/skjiisa/ModSync/issues/70)).

## Changes that need Steam closed: reboot instead

Some settings only take effect while Steam is closed:

- **Open ModSync before Skyrim**, which puts ModSync in front of Steam's Play
  button
- **Keep this version**, which stops Steam from updating a downgraded game

On a Deck, ModSync applies these when Steam restarts. Steam can't be
restarted on the Frame, so turn on **Run in background** under Settings, then
reboot. The background service starts before Steam and makes the change
first. For the Play button, Settings says "At reboot" while the change waits.
For **Keep this version**, Home and the Game version sheet still say to close
Steam; on the Frame, reboot instead.

For the Play button there's a second way that needs no reboot. In Steam, open
Skyrim's Properties, then Compatibility, and pick **ModSync (Skyrim Special
Edition)**. ModSync notices, shows the Play hook as On and drops the queued
change. **Check again** on Home checks right away.

## Troubleshooting

**ModSync, or another Flatpak, doesn't start from Desktop Mode after a
reboot.** It spins in the app menu and vanishes, or a terminal shows
`bwrap: Can't find source path /run/user/1000/nested_plasma/.flatpak-helper/monitor`.
Restarting Desktop Mode fixes it. This has been seen once and its cause is
unknown.

**No mouse cursor in Desktop Mode.** Create a file in `~/.config/environment.d/`,
such as `cursor.conf`, containing:

```
KWIN_FORCE_SW_CURSOR=1
```

Then reboot.

**Something else.** `modsync diagnostics`, or **Copy diagnostics** under
Settings, gathers ModSync's log and the Play hook's log for a bug report. In
the Flatpak it reads the hook's log from the Frame itself, since Steam runs
the hook outside the sandbox.

## Known limits

- The USVFS fix ModSync installs is its own build. The first fix is a
  backport of [usvfs#93](https://github.com/ModOrganizer2/usvfs/pull/93),
  which isn't in an MO2 release yet. The second exists only in ModSync's
  [USVFS fork](https://github.com/skjiisa/usvfs).
- A program MO2 starts passes your mods on to programs it starts with
  `CreateProcess`, which covers SKSE's loader. A tool that starts the game
  another way, such as `ShellExecute`, starts it without your mods.
- Settings that need Steam closed need a reboot instead, as described above.
- Two actions refuse while Steam runs and aren't queued for a reboot, so they
  can't be done from ModSync on the Frame: **Unpin** (letting Steam update a
  pinned game) and **Add ModSync shortcut to Steam**. Steam's "Verify
  integrity of game files" undoes a pin as well, and puts back the current
  game version. Steam's "Add a Non-Steam Game" adds ModSync by hand.
- In Desktop Mode, MO2 needs a mouse or keyboard
  ([#70](https://github.com/skjiisa/ModSync/issues/70)).
