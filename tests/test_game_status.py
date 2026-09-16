"""GameStatus decides which downgrade to offer: the vault's version first, the
installed SKSE's runtime as the fallback for setups that were never recorded."""

import unittest

from modsync.gameversion import GameVersion
from modsync.service import GameStatus


def status(installed: str | None, expected: str | None, skse: str | None, **kw) -> GameStatus:
    return GameStatus(
        installed=GameVersion.parse(installed) if installed else None,
        expected=GameVersion.parse(expected) if expected else None,
        game_dir=None,
        language="english",
        steam_public_build=None,
        steam_is_current=None,
        steam_running=False,
        pending_pin=False,
        recipe_from=kw.pop("recipe_from", "1.7.104"),
        recipe_targets=kw.pop("recipe_targets", ["1.5.97", "1.6.640", "1.6.1170", "1.7.99"]),
        skse_runtime=GameVersion.parse(skse) if skse else None,
        skse_source="skse64_x.dll in the game folder" if skse else "",
        **kw,
    )


class SuggestedTargetTests(unittest.TestCase):
    def test_vault_version_is_suggested(self):
        st = status("1.7.104", "1.6.1170", None)
        self.assertTrue(st.needs_downgrade)
        self.assertEqual(st.wanted_from, "vault")
        self.assertEqual(st.suggested_target, "1.6.1170")

    def test_skse_runtime_is_the_fallback_when_vault_has_no_record(self):
        st = status("1.7.104", None, "1.6.1170")
        self.assertFalse(st.mismatch)
        self.assertTrue(st.needs_downgrade)
        self.assertEqual(st.wanted_from, "skse")
        self.assertEqual(st.suggested_target, "1.6.1170")

    def test_vault_beats_skse_when_both_exist(self):
        st = status("1.7.104", "1.6.640", "1.6.1170")
        self.assertEqual(st.wanted_from, "vault")
        self.assertEqual(st.suggested_target, "1.6.640")

    def test_skse_matching_installed_game_suggests_nothing(self):
        st = status("1.7.104", None, "1.7.104")
        self.assertFalse(st.needs_downgrade)
        self.assertIsNone(st.suggested_target)

    def test_unreachable_targets_are_not_suggested(self):
        # Game is not on the recipe's source version.
        st = status("1.6.1170", None, "1.5.97")
        self.assertTrue(st.needs_downgrade)
        self.assertIsNone(st.suggested_target)
        # Recipe has no path to the wanted version.
        st = status("1.7.104", None, "1.6.659")
        self.assertIsNone(st.suggested_target)

    def test_nothing_known(self):
        st = status("1.7.104", None, None)
        self.assertIsNone(st.wanted)
        self.assertEqual(st.wanted_from, "")
        self.assertFalse(st.needs_downgrade)
        self.assertIsNone(st.suggested_target)
        self.assertIsNone(status(None, None, "1.6.1170").suggested_target)


if __name__ == "__main__":
    unittest.main()
