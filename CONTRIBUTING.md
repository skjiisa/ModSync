# Contributing to ModSync

Thanks for helping. ModSync is small and early; issues, bug reports from real
Steam Decks, and pull requests are all welcome.

## License

ModSync is licensed under the **GNU General Public License v3.0 or later**
(`GPL-3.0-or-later`, see [LICENSE](LICENSE)). By contributing you agree that
your contribution is licensed under the same terms.

## Set up a development environment

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), and on Linux the
usual Qt runtime libraries (`libegl1 libgl1 libxkbcommon0`) for PySide6.

```sh
git clone https://github.com/skjiisa/ModSync.git
cd ModSync
uv sync --extra dev
```

`uv run modsync doctor` runs the CLI from the checkout; `uv run modsync` opens
the GUI.

## Run the tests

```sh
# Unit tests: fast, offline, no display needed (the UI smoke tests use Qt's
# offscreen platform; set QT_QPA_PLATFORM=offscreen if you are headless).
uv run pytest -q
```

The Syncthing integration tests download and run a real Syncthing daemon,
including a two-node test that proves a mod propagates while each machine keeps
its own `ModOrganizer.ini`. They are skipped unless `MODSYNC_IT=1`:

```sh
MODSYNC_IT=1 uv run pytest -q tests/test_sync_integration.py -v
```

(The README's "Development" section shows the equivalent `unittest` commands.)

## Build the Flatpak

See [packaging/flatpak/README.md](packaging/flatpak/README.md) for the full
notes. In short:

```sh
flatpak install -y flathub org.kde.Platform//6.11 org.kde.Sdk//6.11 io.qt.PySide.BaseApp//6.11
flatpak-builder --user --install --force-clean \
    --state-dir="$HOME/.cache/modsync-flatpak/state" \
    "$HOME/.cache/modsync-flatpak/build" \
    packaging/flatpak/io.github.skjiisa.ModSync.yaml
flatpak run io.github.skjiisa.ModSync
```

If `flatpak-builder` is not installed natively, `flatpak run
org.flatpak.Builder` works in its place. To run the same packaging checks as CI:

```sh
desktop-file-validate packaging/flatpak/io.github.skjiisa.ModSync.desktop
appstreamcli validate --pedantic packaging/flatpak/io.github.skjiisa.ModSync.metainfo.xml
flatpak run --command=flatpak-builder-lint org.flatpak.Builder appstream packaging/flatpak/io.github.skjiisa.ModSync.metainfo.xml
flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest packaging/flatpak/io.github.skjiisa.ModSync.yaml
```

The manifest lint currently reports three known `finish-args` errors (home
filesystem, `xdg-data/Steam:rw`, flatpak-spawn); CI tolerates those until they
are resolved.

## Pull requests

- Branch from `main`; keep each PR to one topic.
- Add or update tests for behaviour you change. `uv run pytest -q` must pass
  and CI (tests + packaging checks) must be green.
- Do not commit machine-specific paths, API keys or Steam credentials. ModSync
  must never read or store the user's Nexus API key.
- Update `CHANGELOG.md` (the *Unreleased* section) for user-visible changes.
- Anything touching Steam, Proton or the launch hook: say in the PR which
  machine(s) you verified on (desktop distro, Steam Deck, native or Flatpak
  Steam). Deck testing is the scarcest resource, so note it if you could not.
- Explain *why*, not just what. Small, reviewable commits are appreciated;
  squashing on merge is fine.

## Reporting bugs

Use the bug report issue form. It asks for `modsync doctor` output and, for
Steam launch problems, `~/.local/state/modsync/launch-hook.log`; both make a
report far easier to act on.
