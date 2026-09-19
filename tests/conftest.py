"""Keep the test-suite's logs out of the developer's real ~/.local/state."""

import os
import tempfile

import pytest


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
