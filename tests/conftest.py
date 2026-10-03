"""Keep the test-suite's logs out of the developer's real ~/.local/state, and
keep it offline: no test may download Syncthing or anything else. Set
``MODSYNC_IT=1`` for the integration tests, which do use the network."""

import os
import tempfile
import urllib.request
from pathlib import Path

import pytest

INTEGRATION = os.environ.get("MODSYNC_IT") == "1"


@pytest.fixture(autouse=True, scope="session")
def _isolated_state_home():
    with tempfile.TemporaryDirectory() as tmp:
        previous = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = previous


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
