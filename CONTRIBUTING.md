# Contributing to ModSync

Issues, bug reports from real Steam Decks, and pull requests are all welcome.

## License

ModSync is licensed under the GNU General Public License v3.0 or later
(`GPL-3.0-or-later`, see [LICENSE](LICENSE)). By contributing you agree that
your contribution is licensed under the same terms.

## Set up a development environment

You need Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), and on Linux
the Qt runtime libraries that PySide6 loads (`libegl1 libgl1 libxkbcommon0`).

```sh
git clone https://github.com/skjiisa/ModSync.git
cd ModSync
uv sync --extra dev
```

`uv run modsync doctor` runs the CLI from the checkout. `uv run modsync` opens
the GUI.

## Run the tests

The unit tests are fast, offline, and need no display. The UI smoke tests use
Qt's offscreen platform; set `QT_QPA_PLATFORM=offscreen` if you are headless.

```sh
uv run pytest -q
```

The Syncthing integration tests download and run a real Syncthing daemon. One
two-node test checks that a mod propagates while each machine keeps its own
`ModOrganizer.ini`. They only run when `MODSYNC_IT=1` is set:

```sh
MODSYNC_IT=1 uv run pytest -q tests/test_sync_integration.py -v
```

## Build the Flatpak

[packaging/flatpak/README.md](packaging/flatpak/README.md) has the full notes, and
[docs/advanced.md](docs/advanced.md) covers running from source.
In short:

```sh
flatpak install -y flathub org.kde.Platform//6.11 org.kde.Sdk//6.11 io.qt.PySide.BaseApp//6.11
flatpak-builder --user --install --force-clean \
    --state-dir="$HOME/.cache/modsync-flatpak/state" \
    "$HOME/.cache/modsync-flatpak/build" \
    packaging/flatpak/io.github.skjiisa.ModSync.yaml
flatpak run io.github.skjiisa.ModSync
```

If `flatpak-builder` is not installed natively, `flatpak run
org.flatpak.Builder` takes its place. To run the same packaging checks as CI:

```sh
desktop-file-validate packaging/flatpak/io.github.skjiisa.ModSync.desktop
appstreamcli validate --pedantic packaging/flatpak/io.github.skjiisa.ModSync.metainfo.xml
flatpak run --command=flatpak-builder-lint org.flatpak.Builder appstream packaging/flatpak/io.github.skjiisa.ModSync.metainfo.xml
flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest packaging/flatpak/io.github.skjiisa.ModSync.yaml
```

The manifest lint reports three `finish-args` errors (home filesystem,
`xdg-data/Steam:rw`, flatpak-spawn). ModSync needs all three, and the manifest
has a comment on each explaining why. CI runs the lint but does not fail on
them. ModSync is not published on Flathub, so no exception request is needed.

## Pull requests

- Branch from `main`. Keep each PR to one topic.
- Add or update tests for behaviour you change. `uv run pytest -q` must pass
  and CI (tests plus packaging checks) must be green.
- Do not commit machine-specific paths, API keys or Steam credentials. ModSync
  must never read or store the user's Nexus API key.
- Update the *Unreleased* section of `CHANGELOG.md` for user-visible changes.
- For anything touching Steam, Proton or the launch hook, say in the PR which
  machines you verified on (desktop distro, Steam Deck, native or Flatpak
  Steam). If you could not test on a Deck, say so.
- Explain why, not only what. Small commits are easier to review. Squashing
  on merge is fine.

## Reporting bugs

Use the bug report issue form. It asks for `modsync doctor` output and, for
Steam launch problems, `~/.local/state/modsync/launch-hook.log`. Both make a
report much easier to act on. `modsync diagnostics` (or the dashboard's Copy
diagnostics button) collects the same things with pairing codes and API keys
redacted.
