"""Keep the test-suite's logs and state out of the developer's real
~/.local/state and ~/.config, and keep it offline: no test may download
Syncthing or anything else. Set ``MODSYNC_IT=1`` for the integration tests,
which do use the network."""

import os
import tempfile
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

INTEGRATION = os.environ.get("MODSYNC_IT") == "1"


@pytest.fixture(scope="session")
def _scratch_home():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture(autouse=True)
def _isolated_state_and_config_home(_scratch_home):
    """Logs and ModSync's state and data go to a scratch dir, so a test that
    misses a patch can't overwrite the developer's real setup. Set before every test:
    tests that point XDG_CONFIG_HOME at their own tmp dir unset it afterwards."""
    names = ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME")
    previous = {name: os.environ.get(name) for name in (*names, "MODSYNC_HOME")}
    os.environ.pop("MODSYNC_HOME", None)
    for name in names:
        os.environ[name] = str(_scratch_home / name)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(autouse=True)
def _no_flatpak_data(_scratch_home):
    """Never the Flatpak's real folders, even on a machine that has it installed
    (a Steam Deck) and in a test that unsets the XDG variables."""
    from modsync import config

    with patch.object(config, "flatpak_installed", lambda: False), \
            patch.object(config, "flatpak_home", lambda: _scratch_home / "flatpak-home"):
        yield


@pytest.fixture(autouse=True, scope="session")
def _no_syncthing_download():
    """ModSyncService builds a SyncthingManager, which downloads Syncthing (via
    api.github.com) when none is in the data dir. On CI the data dir is empty,
    so without this the suite hits GitHub's rate limit (#72)."""
    if INTEGRATION or os.environ.get("MODSYNC_SYNCTHING_BIN"):
        yield
        return
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "syncthing"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
        os.environ["MODSYNC_SYNCTHING_BIN"] = str(fake)
        try:
            yield
        finally:
            os.environ.pop("MODSYNC_SYNCTHING_BIN", None)


@pytest.fixture(autouse=True, scope="session")
def _no_real_network():
    """Any real urlopen fails loudly with its URL, so a stray fetch is a test
    failure with a stack trace rather than a flaky 403 on CI. Tests that patch
    ``urlopen`` on the module they exercise are unaffected."""
    if INTEGRATION:
        yield
        return
    original = urllib.request.urlopen

    def refuse(url, *args, **kwargs):
        target = url.full_url if isinstance(url, urllib.request.Request) else url
        if str(target).startswith("file:"):  # the downgrade tests serve fixtures this way
            return original(url, *args, **kwargs)
        raise AssertionError(f"the test suite is offline; refused to open {target}")

    urllib.request.urlopen = refuse
    try:
        yield
    finally:
        urllib.request.urlopen = original


@pytest.fixture(autouse=True, scope="session")
def _no_real_steam_hook():
    """The dashboard and the serve loop bring the launch hook up to date on
    start, which rewrites compatibilitytools.d and config.vdf. Point that at no
    Steam at all; tests/test_launchhook.py patches in a fake one per test."""
    from unittest.mock import patch

    from modsync import launchhook

    with patch.object(launchhook, "steam_env", lambda: None):
        yield


@pytest.fixture(autouse=True)
def _no_real_dfu_install(_scratch_home):
    """modsync.dfu also looks in ~/Games/Daggerfall Unity, the default folder:
    never the developer's real one. Tests that need it patch it themselves."""
    from unittest.mock import patch

    with patch("modsync.dfu.default_install_dir", lambda: _scratch_home / "no-default-dfu"):
        yield


REAL_HOME = Path.home()


@pytest.fixture(autouse=True)
def _no_real_game_stores(_scratch_home):
    """The developer's machine looks like CI: no Steam, no Heroic and nothing
    MO2-LINT installed. The games from the table (modsync.mo2games) are looked
    for in all three, and a Play hook found for one is rewritten whenever
    ModSync starts, which a test run once did to a real Heroic. A test that
    needs a store points HOME at a folder of its own, or patches
    ``modsync.platforms.current`` with a fake platform."""
    from unittest.mock import patch

    from modsync import platforms
    from modsync.mo2.installers import mo2lint

    class NoRealStores(type(platforms.current())):
        def steam_roots(self):
            return [] if Path.home() == REAL_HOME else super().steam_roots()

        def heroic_config_dirs(self):
            return [] if Path.home() == REAL_HOME else super().heroic_config_dirs()

    real_state_path = mo2lint.mo2lint_state_path

    def state_path():
        return _scratch_home / "no-mo2-lint" / "state.json" if Path.home() == REAL_HOME else real_state_path()

    with patch("modsync.platforms.current", lambda: NoRealStores()), \
            patch.object(mo2lint, "mo2lint_state_path", state_path):
        yield
