# Packaging ModSync as a Flatpak

ModSync ships as a Flatpak so it installs cleanly on the Steam Deck (and any
Linux box) with a bundled Syncthing and PySide6 — no system Python, no system
Syncthing, no collisions.

## What's here

| File | Purpose |
| --- | --- |
| `io.github.skjiisa.ModSync.yaml` | The flatpak-builder manifest. |
| `io.github.skjiisa.ModSync.desktop` | Desktop launcher entry. |
| `io.github.skjiisa.ModSync.metainfo.xml` | AppStream metadata (store listing). |
| `icons/io.github.skjiisa.ModSync.svg` / `.png` | App icon (scalable + 512px). |

The `.png` is rendered from the `.svg` with `rsvg-convert -w 512 -h 512`.

## Build & run locally

```sh
# One-time: the runtime + SDK (the Platform may already be installed).
flatpak install -y flathub org.kde.Platform//6.9 org.kde.Sdk//6.9

# Build + install into the user installation. Keep the build dir OUTSIDE the
# repo so the `dir` source doesn't copy it into itself.
flatpak-builder --user --install --force-clean \
    /tmp/modsync-flatpak-build \
    packaging/flatpak/io.github.skjiisa.ModSync.yaml

flatpak run io.github.skjiisa.ModSync
```

`flatpak-builder` itself comes from either your distro (`flatpak-builder`
package) or Flathub (`flatpak install flathub org.flatpak.Builder`, then run
`flatpak run org.flatpak.Builder` in place of `flatpak-builder`).

## How the manifest is put together

- **Bundled Syncthing** — a pinned static `syncthing` binary installed to
  `/app/bin/syncthing`. ModSync's `ensure_syncthing()` looks there (and at
  `$MODSYNC_SYNCTHING_BIN`) *before* trying to download one, so inside the
  sandbox it always uses the bundled copy.
- **PySide6-Essentials** — ModSync only imports QtCore/QtGui/QtWidgets, so we
  skip the much larger `PySide6-Addons`. The abi3 wheels run on the runtime's
  Python unchanged.
- **`finish-args`** — Wayland (with X11 fallback), network for Syncthing, and
  filesystem access to Steam libraries (`xdg-data/Steam`, `/run/media` for SD
  cards) plus `home` for MO2 instances. Tighten `--filesystem=home` to specific
  instance roots once the UI records them.

## Reproducible / Flathub builds

The `python-deps` module currently pip-installs from PyPI with
`--share=network`, which is convenient locally but **not allowed on Flathub**
(builds run offline). Before submitting, pin every wheel:

```sh
# Produces a python3-modules.yaml of wheel URLs + sha256s.
flatpak-pip-generator --runtime=org.kde.Sdk//6.9 \
    PySide6-Essentials httpx qrcode platformdirs
```

Then replace the `python-deps` module with the generated file and drop the
`build-args: [--share=network]`.

## Still to do in Phase 5

- **Build + test on a real Deck** — build with the steps above, then run the
  full two-machine flow in both Desktop and Gaming Mode.
- **Flathub packaging** — pin the Python wheels (see *Reproducible / Flathub
  builds*) and drop the build-time network access.

Already implemented (just needs the Deck to exercise it):

- **Bundled Syncthing** + the app **icon**.
- **Background sync service** — `modsync service install [--linger]` writes a
  `systemd --user` unit running `modsync vault serve`, so sync continues after
  the app is closed. `SyncthingManager` attaches to an already-running daemon, so
  the service and the app coexist without a port clash.
- **"Add to Steam" helper** — `modsync steam shortcut` adds ModSync as a
  non-Steam game (`flatpak run io.github.skjiisa.ModSync`) for every Steam user,
  so it's launchable from Gaming Mode.
