# Packaging ModSync as a Flatpak

ModSync ships as a Flatpak so it installs on a Steam Deck and on any Linux
machine with a bundled Syncthing and PySide6. It needs no system Python and no
system Syncthing, so nothing collides with either.

## What's here

| File | Purpose |
| --- | --- |
| `io.github.skjiisa.ModSync.yaml` | The flatpak-builder manifest. |
| `io.github.skjiisa.ModSync.desktop` | Desktop launcher entry. |
| `io.github.skjiisa.ModSync.metainfo.xml` | AppStream metadata. |
| `icons/io.github.skjiisa.ModSync.svg` and `.png` | Editable SVG source and the installed 512 pixel PNG. |

The logo was designed in Canva. The latest export is kept in
`icons/source/ModSync.pdf`. The `.svg` is that PDF converted with
`pdftocairo -svg`, with the canvas set to 512 by 512. The hex background is a
small embedded bitmap. Everything else is vector. The `.png` is rendered from
the `.svg` with `rsvg-convert -w 512 -h 512`.

The outer `app-icon-corners` clip gives the icon transparent rounded corners
with a 96 pixel radius at 512 pixels. The PDF artwork and embedded bitmap are
unchanged. When you update the SVG, regenerate the PNG:

```sh
rsvg-convert -w 512 -h 512 packaging/flatpak/icons/io.github.skjiisa.ModSync.svg \
  -o packaging/flatpak/icons/io.github.skjiisa.ModSync.png
```

[docs/icon-review](../../docs/icon-review/README.md) shows the icon on light
and dark backgrounds at several sizes.

Install only the PNG into the icon theme. Qt's SVG renderer does not support
the clipping path the export uses, so a scalable SVG theme icon can render
square in Qt and KDE apps.

## Build and run locally

```sh
# One time: the runtime and SDK. The Platform may already be installed.
flatpak install -y flathub org.kde.Platform//6.11 org.kde.Sdk//6.11 io.qt.PySide.BaseApp//6.11

# Build and install into the user installation.
flatpak-builder --user --install --force-clean \
    --state-dir="$HOME/.cache/modsync-flatpak/state" \
    "$HOME/.cache/modsync-flatpak/build" \
    packaging/flatpak/io.github.skjiisa.ModSync.yaml

flatpak run io.github.skjiisa.ModSync
```

Keep the build directory outside the repository, otherwise the `dir` source
copies it into itself. Put the state directory next to the build directory.
flatpak-builder refuses to run when its state directory (by default
`.flatpak-builder` inside the repository) is on a different filesystem than the
build directory.

`flatpak-builder` comes from your distribution's `flatpak-builder` package or
from Flathub (`flatpak install flathub org.flatpak.Builder`). With the Flathub
one, run `flatpak run org.flatpak.Builder` in place of `flatpak-builder` with
the same arguments.

## Release bundle

ModSync is not published on Flathub and there are no plans to submit it. Each
GitHub release attaches a single-file bundle, `ModSync-<version>-x86_64.flatpak`,
that users install with Discover or `flatpak install --user`.

`.github/workflows/release.yml` builds the bundle on every `v*` tag using
[flatpak-github-actions](https://github.com/flathub-infra/flatpak-github-actions),
runs `modsync --version` inside it, and publishes the GitHub release together
with the wheel and sdist. *Run workflow* builds it on demand as an artifact
only. Tags with a suffix such as `v1.0.0-rc1` become pre-releases. The version
in the tag must equal the one in `pyproject.toml` and `modsync/__init__.py`
(`1.0.0rc1` for `v1.0.0-rc1`).

To build the same bundle locally, add `--repo` to the build and export it:

```sh
flatpak-builder --force-clean --repo="$HOME/.cache/modsync-flatpak/repo" \
    --state-dir="$HOME/.cache/modsync-flatpak/state" \
    "$HOME/.cache/modsync-flatpak/build" \
    packaging/flatpak/io.github.skjiisa.ModSync.yaml
flatpak build-bundle "$HOME/.cache/modsync-flatpak/repo" ModSync.flatpak \
    io.github.skjiisa.ModSync \
    --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo
```

`--runtime-repo` lets a machine without the KDE runtime fetch it from Flathub
when the bundle is installed. Bundles are unsigned and do not update
themselves. Users install the newer release's file over the old one. A hosted
OSTree repository on GitHub Pages would give `flatpak update` if that ever
matters. It would need a GPG signing key in the Actions secrets.

## How the manifest is put together

- **Bundled Syncthing.** A pinned static `syncthing` binary is installed to
  `/app/bin/syncthing`. `ensure_syncthing()` looks there, and at
  `$MODSYNC_SYNCTHING_BIN`, before it tries to download one, so inside the
  sandbox it always uses the bundled copy.
- **PySide6 from `io.qt.PySide.BaseApp`.** The shared base app builds PySide6
  against the Qt already in `org.kde.Platform`, so the app does not ship a
  second copy of Qt. The PyPI wheels bundle one, and `flatpak-pip-generator`
  refuses them for that reason. ModSync only imports QtCore, QtGui and
  QtWidgets, so `cleanup-commands` strip every other binding and the Qt
  developer tools the base app carries. The final app is about 100 MB instead
  of about 350 MB.
- **Pinned Python wheels.** `python3-modules.yaml` (httpx, qrcode,
  platformdirs, spake2 with cryptography and cffi) and `python3-hatchling.yaml`
  (the build backend, build only) are generated by `flatpak-pip-generator`, so
  the build runs offline.
- **`finish-args`.** Wayland with X11 fallback, network for Syncthing, and
  filesystem access to Steam libraries (`xdg-data/Steam`, `/run/media` for SD
  cards) plus `home` for MO2 instances, which can live anywhere under the home
  directory. `flatpak-spawn` runs `pgrep`, `systemctl` and MO2-LINT on the
  host, because the sandbox has its own PID namespace and cannot see Steam or
  Proton.

## Offline build and dependency bumps

No module uses `--share=network`. Every wheel is pinned by URL and sha256, and
PySide6 comes from `io.qt.PySide.BaseApp//6.11`. The runtime is
`org.kde.Platform//6.11` (6.9 is end of life). To bump the Python
dependencies, regenerate the two module files with
[flatpak-pip-generator](https://github.com/flatpak/flatpak-builder-tools/tree/master/pip).
It needs `requirements-parser`, `packaging` and `pyyaml`:

```sh
curl -sSLO https://raw.githubusercontent.com/flatpak/flatpak-builder-tools/master/pip/flatpak-pip-generator.py
uv run --with requirements-parser --with packaging --with pyyaml \
    python flatpak-pip-generator.py --runtime org.kde.Sdk//6.11 --yaml \
    --wheel-arches x86_64 --prefer-wheels=cryptography,cffi \
    --output python3-modules 'httpx>=0.27' 'qrcode>=7.4' 'platformdirs>=4' 'spake2>=0.8'
uv run --with requirements-parser --with packaging --with pyyaml \
    python flatpak-pip-generator.py --runtime org.kde.Sdk//6.11 --yaml \
    --wheel-arches x86_64 --build-only --output python3-hatchling hatchling
```

`--prefer-wheels=cryptography,cffi` picks the manylinux wheels (cryptography's
`cp311-abi3`, cffi's `cp313`, since the 6.11 SDK ships Python 3.13) so the
build needs no Rust toolchain. `--wheel-arches x86_64` matches the x86_64-only
Syncthing binary. Do not list PySide6. The generator exits and points at the
base app instead.

## Lint

```sh
flatpak run --command=flatpak-builder-lint org.flatpak.Builder \
    manifest packaging/flatpak/io.github.skjiisa.ModSync.yaml
```

The lint reports three `finish-args` errors: `home`, `xdg-data/Steam:rw` and
flatpak-spawn. ModSync needs all three and the manifest has a comment on each.
CI runs the lint and reports them without failing the build. They would only
matter for a Flathub listing, which is not planned.

## How the sandbox reaches Steam

- **Background sync service.** `modsync service install [--linger]` writes a
  `systemd --user` unit that runs `modsync serve`, so sync continues after the
  app is closed. `SyncthingManager` attaches to an already running daemon, so
  the service and the app share one Syncthing without a port clash.
- **Add to Steam.** `modsync steam shortcut` adds ModSync as a non-Steam game
  (`flatpak run io.github.skjiisa.ModSync`) for every Steam user, so it can be
  launched from Gaming Mode.
- **Launch hook.** `modsync launch enable` writes a compatibility tool into
  `~/.local/share/Steam/compatibilitytools.d` (covered by `xdg-data/Steam:rw`).
  Its `proton` script runs on the host and starts the hub with
  `/usr/bin/flatpak run io.github.skjiisa.ModSync launch hub ...`. Inside the
  sandbox, "is Steam running" is answered through `flatpak-spawn --host pgrep`.
- **Install MO2.** The protontricks check and MO2-LINT itself run on the host
  through `flatpak-spawn --host`, because Steam and Proton are not visible
  inside the sandbox.
