---
version: 1
slug: "modsync-ui"
primary_target: "modsync/ui"
related_targets: []
---

# ModSync window (Home, Sync, Settings, sheets, setup wizard)

Scope: the whole Qt window in `modsync/ui/`. Mode: Operate. Primary scene: Steam Deck in Gaming Mode, controller in hand, 1280 × 800 at arm's length; mouse, keyboard and touch must keep working.

Task: get from any setup state to Play. Home shows readiness (MO2, game version, SKSE, sync, Steam launch) and the single next action. Sheets handle maintenance; the wizard walks first setup.

Must stay untouched: every input path (SDL pads, Steam Input default desktop bindings mapped to actions, keyboard, mouse, touch), focus rules, busy locking, Steam launch decisions, all behavior.

## Direction contract

THESIS: ModSync is part of the Deck, not a poster on it. It refuses the AI dashboard default (aurora backdrop, glowing teal cards, icon-in-a-box on every tile, uppercase eyebrows) for the device's own flat, matte, list-first vocabulary.

OWN-WORLD: Matte charcoal ground #1b1d21 with a slightly lighter bar #23262b for the top bar and hint bar; rows #26292f with 4 px corners and no borders; white legends #f4f5f7; the app icon's blue #1a9fff is the only accent, used for the primary action and selection. State colors only where they mean something (green ready, amber needs attention, red danger), shown as a small dot plus a plain value word in the tone's color, never filled chips. One sans (the system UI face), bold for titles, no tracking tricks. No gradients, glows, sheens or translucency.

STORY: The player sees their setup as a short list with a status dot per line, sees which line needs them, presses A, and plays.

FIRST VIEWPORT: Top bar: real app icon, "ModSync", text tabs Home / Sync / Settings with bumper legends, clock right. Home: left column "Skyrim Special Edition" title, then flat rows MO2, Game version, SKSE, Sync/Set up sync, Steam launch, each with dot, name, value right. Right column: Play Skyrim as a full-width solid blue bar, Open Mod Organizer 2 a plain row under it. While the game version or SKSE needs a repair, that repair takes the blue bar above Play and Play steps down to a plain row (the Task line: one next action), still one press away. Hint bar along the bottom.

SIGNATURE: Focus is a lit row. The focused row fills white with dark text and prints the device's A legend at its right edge (Enter key cap on a keyboard); the primary blue bar keeps its blue and gains a white ring plus the legend. Nothing else glows.

FORM: Deck in Hand (Steam Deck hardware and SteamOS list vocabulary), the user's chosen card (IMPECCABLE'S PICK, ranked first on the grounded list); seed key 072c3455.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
