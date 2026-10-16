---
name: ModSync
description: A controller-first Skyrim modding hub that looks like part of the Steam Deck.
colors:
  night: "#1b1d21"
  bar: "#23262b"
  surface: "#26292f"
  raised: "#32363d"
  sunken: "#16181b"
  border: "#3a3e45"
  text: "#f4f5f7"
  secondary: "#b8bdc4"
  muted: "#9aa1aa"
  disabled: "#737a84"
  accent-deep: "#1a9fff"
  on-accent: "#00182c"
  accent: "#4fb4ff"
  focus: "#f4f5f7"
  focus-pressed: "#d9dce1"
  on-focus: "#16181b"
  on-focus-dim: "#4a4f57"
  ok: "#7dd88b"
  ok-on-focus: "#1d7a33"
  warn: "#ffc857"
  warn-on-focus: "#8a5a00"
  warn-bg: "#3a3020"
  danger: "#ff8a80"
  danger-on-focus: "#b3261e"
  danger-bg: "#3d2424"
  info: "#8cc8ff"
  info-on-focus: "#0b63b8"
typography:
  title:
    fontFamily: "system-ui, sans-serif"
    fontSize: "22pt"
    fontWeight: 700
  hero-action:
    fontFamily: "system-ui, sans-serif"
    fontSize: "20pt"
    fontWeight: 700
  choice-title:
    fontFamily: "system-ui, sans-serif"
    fontSize: "15pt"
    fontWeight: 700
  heading:
    fontFamily: "system-ui, sans-serif"
    fontSize: "14pt"
    fontWeight: 700
  row-title:
    fontFamily: "system-ui, sans-serif"
    fontSize: "13pt"
    fontWeight: 600
  tab-label:
    fontFamily: "system-ui, sans-serif"
    fontSize: "12.5pt"
    fontWeight: 600
  body:
    fontFamily: "system-ui, sans-serif"
    fontSize: "12pt"
    fontWeight: 400
  value:
    fontFamily: "system-ui, sans-serif"
    fontSize: "12pt"
    fontWeight: 600
  hint-label:
    fontFamily: "system-ui, sans-serif"
    fontSize: "11.5pt"
    fontWeight: 600
  legend:
    fontFamily: "system-ui, sans-serif"
    fontSize: "11pt"
    fontWeight: 400
  field:
    fontFamily: "system-ui, sans-serif"
    fontSize: "15pt"
    fontWeight: 400
  key-cap:
    fontFamily: "system-ui, sans-serif"
    fontSize: "15pt"
    fontWeight: 700
  digit:
    fontFamily: "system-ui, sans-serif"
    fontSize: "30pt"
    fontWeight: 700
  mono:
    fontFamily: "monospace"
    fontSize: "10pt"
    fontWeight: 400
rounded:
  meter: "3px"
  row: "4px"
  hero: "6px"
  sheet: "6px"
spacing:
  2xs: "2px"
  xs: "4px"
  sm: "8px"
  md: "12px"
  row-x: "18px"
  lg: "20px"
  gutter: "24px"
  sheet-x: "32px"
  page-x: "40px"
components:
  row:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    typography: "{typography.row-title}"
    rounded: "{rounded.row}"
    padding: "14px 14px 14px 18px"
  row-compact:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    typography: "{typography.row-title}"
    rounded: "{rounded.row}"
    padding: "11px 14px 11px 18px"
  row-hover:
    backgroundColor: "{colors.raised}"
  row-pressed:
    backgroundColor: "{colors.sunken}"
  row-lit:
    backgroundColor: "{colors.focus}"
    textColor: "{colors.on-focus}"
  row-lit-pressed:
    backgroundColor: "{colors.focus-pressed}"
    textColor: "{colors.on-focus}"
  row-danger:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.danger}"
  row-danger-lit:
    backgroundColor: "{colors.focus}"
    textColor: "{colors.danger-on-focus}"
  action-primary:
    backgroundColor: "{colors.accent-deep}"
    textColor: "{colors.on-accent}"
    typography: "{typography.hero-action}"
    rounded: "{rounded.hero}"
    padding: "20px 20px 20px 26px"
    height: "96px"
  choice-card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    typography: "{typography.choice-title}"
    rounded: "{rounded.row}"
    padding: "22px 24px"
  tab:
    textColor: "{colors.secondary}"
    typography: "{typography.tab-label}"
    rounded: "{rounded.row}"
    height: "36px"
  tab-selected:
    backgroundColor: "{colors.border}"
    textColor: "{colors.text}"
  tab-lit:
    backgroundColor: "{colors.focus}"
    textColor: "{colors.on-focus}"
  text-field:
    backgroundColor: "{colors.sunken}"
    textColor: "{colors.text}"
    typography: "{typography.field}"
    rounded: "{rounded.row}"
    padding: "10px 14px"
  panel:
    backgroundColor: "{colors.bar}"
    textColor: "{colors.text}"
    rounded: "{rounded.row}"
    padding: "20px"
  sheet:
    backgroundColor: "{colors.night}"
    textColor: "{colors.text}"
    rounded: "{rounded.sheet}"
    padding: "28px 32px"
  callout-warning:
    backgroundColor: "{colors.warn-bg}"
    textColor: "{colors.warn}"
    rounded: "{rounded.row}"
    padding: "12px 16px"
  callout-note:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.secondary}"
    rounded: "{rounded.row}"
    padding: "12px 16px"
  top-bar:
    backgroundColor: "{colors.bar}"
    textColor: "{colors.text}"
    height: "60px"
    padding: "10px 24px"
  hint-bar:
    backgroundColor: "{colors.bar}"
    textColor: "{colors.secondary}"
    typography: "{typography.hint-label}"
    height: "52px"
    padding: "4px 24px"
  key-cap:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text}"
    typography: "{typography.key-cap}"
    rounded: "{rounded.row}"
    size: "58px 54px"
  key-cap-lit:
    backgroundColor: "{colors.focus}"
    textColor: "{colors.on-focus}"
  key-cap-on:
    backgroundColor: "{colors.accent-deep}"
    textColor: "{colors.on-accent}"
  digit-cell:
    backgroundColor: "{colors.sunken}"
    textColor: "{colors.text}"
    typography: "{typography.digit}"
    rounded: "{rounded.row}"
    width: "72px"
    height: "104px"
  status-dot:
    size: "10px"
  sync-meter:
    backgroundColor: "{colors.sunken}"
    rounded: "{rounded.meter}"
    height: "8px"
  toast:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.row}"
    padding: "13px 18px 13px 16px"
    width: "520px"
---

# Design System: ModSync

## Overview

**Creative North Star: "Deck in Hand"**

ModSync is part of the Steam Deck, not a poster on it. The window speaks the device's own vocabulary: a matte charcoal ground, flat rows with small corners, white legends, and the blue of the app icon as the one accent. A screen is a short list. Each line says what it is on the left and its state on the right, and the line that needs the player is the one that is lit. It is a native Qt window sized for 1280 × 800 at arm's length (type in Qt points, geometry in pixels), and everything is painted flat: no gradients, glows, sheens or shadows.

Focus carries the identity. A controller never hovers, so the selected control is unmistakable without color: the focused row fills white, its text turns dark, and the button that chooses it is printed at its right edge in the glyphs of the device in hand (the Deck's A disc, an Xbox or PlayStation face button, or an Enter key cap). Nothing else lights up. With a mouse or a finger, rows never light; they only tint on hover and press.

The look deliberately replaces the earlier aurora-backdrop, glowing-teal-card dashboard. Icons in boxes on every tile, uppercase eyebrows and filled status chips are not part of this world.

**Key Characteristics:**
- Matte, flat, list-first; depth is tonal steps of charcoal only.
- One blue (the app icon's) for the single primary action and for selection.
- Focus is a lit white row with the device's own button legend.
- State is a small dot plus a plain value word in the tone's color.
- One system sans; hierarchy by size and weight, never tracking or case.

## Colors

A cool charcoal ramp with white legends, one saturated blue taken from the app icon, and three state tones that each have a darker twin for use on a lit (white) row.

### Primary
- **Deck Icon Blue** (accent-deep): the app icon's blue. Fills the one primary action per screen (Play, Continue, Install MO2, or the recommended repair), the progress fill of the sync meter and progress bars, text selection, and a checked key (Shift on) on the on-screen keyboard. Text on it is **Abyss Navy** (on-accent).
- **Link Blue** (accent): link text through the application palette. It is never a fill.

### Neutral
- **Night** (night): the window ground and the ground of sheets.
- **Bar Charcoal** (bar): top bar, hint bar, and read-only panels (version readout, device list).
- **Row Slate** (surface): every choosable row at rest; also note callouts and hint chips.
- **Lifted Slate** (raised): rows and keys under the mouse, toasts, tooltips, keys at rest.
- **Well** (sunken): pressed rows, text fields, the log view, meter tracks, empty PIN cells.
- **Seam** (border): the selected tab, scrollbar handles, idle status dots, key legends on the dark ground, text-field strokes.
- **Legend White** (text): titles and primary text. **Secondary Grey** (secondary): descriptions, idle values, unselected tabs, hint labels. **Muted Grey** (muted): placeholders, chevrons, quiet captions. **Disabled Grey** (disabled): disabled text.
- **Lit White** (focus) with **Lit Pressed** (focus-pressed) while held; text on it is **Ink** (on-focus) and **Ink Dim** (on-focus-dim) for descriptions and idle values.

### State tones
- **Ready Green** (ok), **Attention Amber** (warn), **Danger Coral** (danger), **Working Blue** (info, also used for "busy"): state dots, value words and danger-row titles on the dark ground.
- Each has an on-focus twin (ok-on-focus, warn-on-focus, danger-on-focus, info-on-focus) that replaces it the moment its row lights, so the word stays readable on white.
- **Amber Well** (warn-bg) and **Coral Well** (danger-bg) are the only tinted surfaces, used solely behind callout text.

### Named Rules
**The One Blue Rule.** Each screen has at most one blue action. When a repair (game version, SKSE) is recommended, the repair takes the blue bar and Play steps down to a plain row directly beneath it, still one press away.

**The Twin Tone Rule.** Every color that can sit on a row has a version for the lit row. Never paint a light-ground tone onto the white focus fill; switch to its on-focus twin. Text contrast against every surface is held to WCAG AA by the test suite.

## Typography

**Body Font:** the system UI sans (the Qt application font, never smaller than 12pt)
**Mono Font:** the system monospace, only for the log view

**Character:** One plain sans at generous sizes, the way the Deck sets its own UI. Weight does the work: bold for titles and actions, semibold for row titles, values and labels, regular for prose.

### Hierarchy
- **Title** (700, 22pt): page titles ("Skyrim Special Edition", "Sync", "Settings"), sheet titles, the installed version readout.
- **Hero action** (700, 20pt): the title of the blue primary bar.
- **Choice title** (700, 15pt): the tall choice cards in the setup wizard.
- **Heading** (700, 14pt): the "ModSync" wordmark beside the icon, panel headings ("Syncing", "Devices").
- **Row title** (600, 13pt): the name on every row.
- **Body** (400, 12pt): row descriptions, page subtitles, sheet prose, in Secondary Grey.
- **Value** (600, 12pt): the state word at a row's right edge ("Ready", "Needs 1.5.97", "Off").
- **Tab label** (600, 12.5pt) and **Hint label** (600, 11.5pt): section tabs and hint-bar actions.
- **Legend** (11pt base, printed at 78-82% inside the cap or disc): controller and key legends.
- **Field** (400, 15pt), **Key cap** (700, 15pt; special keys 600, 11.5pt), **Digit** (700, 30pt): text entry, the on-screen keyboard and the PIN cells.

### Named Rules
**The Weight Not Tracking Rule.** Hierarchy comes from size and weight only. No uppercase labels, no letter-spacing, no eyebrows above titles.

## Layout

The design canvas is 1280 × 800 with the window always maximized in Gaming Mode. A 60px top bar (24px side margins) carries the app icon at 32px, the wordmark, bumper legends around the text tabs Home / Sync / Settings, and the clock on the right. A 52px hint bar along the bottom shows the input device on the left and the live button hints on the right.

Pages sit between them with 40px side margins (18px top, 28px bottom; Home tightens to 16 and 12) and 20px between blocks (14px on Home). Content is two columns with a 24px gutter: on the left the readiness list or readouts, on the right the action column with the blue primary bar on top. Rows in a list stack with a 4px gap, so the list reads as one block of lines; in sheets and the folder picker they stack at 8px. Every standard Home action fits without scrolling; where a page does scroll, the scrollbar is a 6px handle with no track.

Sheets center over the window, 560-760px wide, with 28px top and bottom and 32px side padding and 14px between items. Maintenance sheets (Game version, MO2) fill the content area under the top bar with 24px margins.

## Elevation & Depth

The system is flat. There are no shadows anywhere. Depth is tonal: Well is lowest (fields, pressed rows), then the Night ground, the Bar Charcoal chrome and panels, the Row Slate rows, and Lifted Slate for hover, toasts and keys. Sheets sit over a black scrim at 67% opacity, the only translucency in the build: it dims the page behind so the sheet reads as the whole task. The sheet itself is opaque Night.

### Named Rules
**The Matte Rule.** No gradients, glows, sheens or shadows on any surface. A surface rises by being one charcoal step lighter, never by casting anything.

## Shapes

Small, consistent corners: 4px on rows, tabs, panels, callouts, fields, keys, PIN cells, toasts and controller legend caps. Only the blue primary bar and the sheets open up to 6px. The 8px meters round at 3px; status dots are 10px circles; the tab alert dot is 8px. Rows and panels have no borders: edges come from the tonal step against the ground. The only strokes are focus (a 3px white ring) and the 2px text-field outline.

## Components

### Rows (Tile, StatusRow)
The unit of everything choosable: a flat Row Slate bar, title on the left with an optional description beneath, an optional value word and chevron on the right.
- **Shape:** 4px corners, no border; padding 14px with 18px on the leading edge (compact status rows 11px vertical).
- **States:** hover tints to Lifted Slate, press sinks to Well. Lit fills white with Ink text, Ink Dim description, and the value word in its on-focus twin; held, it dims to Lit Pressed. Disabled rows paint at 50% opacity with Disabled Grey text.
- **Status rows** lead with a 10px dot (Seam grey when idle) and end with the value word in the same tone and a chevron in Muted Grey.
- **Danger rows** title in Danger Coral (danger-on-focus when lit) on the normal Row Slate; they are never filled red.
- **Icons** appear only on the hero bar and the tall choice cards (24-32px line icons, 2px stroke). List rows carry no icons.

### Primary action (HeroTile)
The one blue bar per screen: Deck Icon Blue, at least 96px tall, 6px corners, a 20pt bold title in Abyss Navy with its description beneath, and a line icon at the lead. Hover lightens it 12%, press darkens it 12%. When lit it keeps its blue and gains a 3px white inset ring and a dark legend disc at the right edge. When a repair outranks it, it steps down to a plain row (normal padding, no icon).

### The lit legend (TileEnd)
**The Lit Row Rule.** Focus is a lit row: the focused row fills white with dark text and prints the device's accept legend at its right edge; the primary bar keeps its blue and adds a white ring plus the legend. Nothing else glows.

Legends are drawn as the device prints them: Deck and Xbox face buttons as a light disc with a dark letter (dark disc on lit or blue rows), PlayStation as its colored symbols, keyboard keys as outlined caps ("Enter", "Esc"), bumpers and triggers as small 4px-cornered caps. The glyph set follows whichever input was used last.

### Navigation (TabButton, TopBar)
Text-only tabs, 36px tall inside a 40px slot, 18px side padding, 12.5pt semibold. Unselected tabs are Secondary Grey text on nothing; hover adds Row Slate; the current tab sits on a Seam fill in Legend White; a focused tab lights white with Ink text. A tab needing attention shows an 8px tone dot after its name. Bumper legends flank the tab row.

### Hint bar (HintBar, HintButton)
Bar Charcoal, 52px. With a controller or keyboard, each hint is the device legend(s) followed by an 11.5pt semibold Secondary Grey label. With a mouse or touch, hints become plain text buttons: Legend White on Row Slate with 4px corners, Lifted Slate under the pointer, no legends. Either way, clicking or tapping a hint performs it.

### Panels and callouts
- **Panel:** a read-only Bar Charcoal block, 4px corners, 20px padding, 12px gap, for readouts that are not choices (installed version, sync progress, devices).
- **Callouts:** 4px corners, 12px × 16px padding. Warning is Attention Amber text on Amber Well; note is Secondary Grey on Row Slate.

### State words and dots
**The Dot-and-Word Rule.** A state is a small dot plus a plain value word in the tone's color, semibold. Never a filled chip, badge or pill background.

### Inputs / Fields
- **Text field:** Well ground, 2px Seam stroke, 4px corners, 10px × 14px padding, 15pt text; focus turns the stroke white. Selection uses Deck Icon Blue with Abyss Navy text.
- **Log view:** Well ground, no stroke, 10pt monospace in Secondary Grey; a 2px white stroke on focus.

### On-screen keyboard and PIN pad (KeyButton, DigitCell)
Keys are 58 × 54px (wide keys span multiples with 8px gaps), Lifted Slate at rest with Legend White 15pt bold letters (special keys Secondary Grey, 11.5pt semibold). Hover goes to Seam, press to Well, lit to white with Ink. A checked Shift fills blue; lit and checked, it keeps a 3px blue underline. PIN cells are 72 × 104px Well boxes with 30pt bold digits, lit white for the active cell.

### Feedback (SyncMeter, Toast, FocusHalo)
- **Sync meter:** an 8px Well track with a Deck Icon Blue fill, 3px corners, easing to its new value.
- **Toast:** Lifted Slate, 4px corners, 520px wide, a 22px tone icon and the message; fades in and out.
- **Focus halo:** for controls that do not light themselves (fields, the log view), a 3px white outline with 6px corners slides to the focused control. Rows, tabs and keys own their focus and never get the halo.

## Do's and Don'ts

### Do:
- **Do** keep exactly one Deck Icon Blue action per screen, and let a recommended repair take it from Play.
- **Do** show focus as the lit white row with the active device's accept legend; on the blue bar, a 3px white ring plus the legend.
- **Do** switch every tone to its on-focus twin when its row lights.
- **Do** express state as a 10px dot and a semibold value word in the tone's color.
- **Do** build depth from the charcoal steps (Well, Night, Bar Charcoal, Row Slate, Lifted Slate) and keep rows borderless at 4px corners.
- **Do** print button hints in the glyphs of the device in hand.
- **Do** keep every standard Home action visible at 1280 × 800 without scrolling.

### Don't:
- **Don't** add gradients, glows, sheens, shadows or backdrop imagery; the only translucency is the scrim behind sheets.
- **Don't** fill state words into chips or badges, or color a whole row by its state.
- **Don't** put an icon on every row; icons belong to the hero bar, choice cards, toasts and the hint bar's device mark.
- **Don't** use uppercase labels, letter-spaced eyebrows or a second typeface for display.
- **Don't** light rows under a mouse or finger; hover and press tint, focus lights.
- **Don't** redraw or recolor the app icon; it appears as shipped, at 32px in the top bar.
