"""Release notes from CHANGELOG.md: scripts/changelog_section.py."""

import importlib.util
import unittest
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "changelog_section.py"
_spec = importlib.util.spec_from_file_location("changelog_section", _PATH)
changelog_section = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(changelog_section)

CHANGELOG = """# Changelog

## [Unreleased]

## [2.0.0] - 2026-11-01

Copying replaces merging.

- Works on the Steam Frame

### Added

- A thing

### Fixed

- Another thing

## [1.0.0] - 2026-09-21

### Added

- First release
"""


class CollapseTests(unittest.TestCase):
    def test_keeps_the_intro_and_folds_the_lists(self):
        body = changelog_section.section(CHANGELOG, "2.0.0")
        self.assertEqual(changelog_section.collapse(body), (
            "Copying replaces merging.\n\n- Works on the Steam Frame\n\n"
            "<details>\n<summary>Full changelog</summary>\n\n"
            "### Added\n\n- A thing\n\n### Fixed\n\n- Another thing\n\n</details>"))

    def test_a_section_without_an_intro_is_all_folded(self):
        body = changelog_section.section(CHANGELOG, "1.0.0")
        self.assertEqual(changelog_section.collapse(body), (
            "<details>\n<summary>Full changelog</summary>\n\n"
            "### Added\n\n- First release\n\n</details>"))

    def test_a_section_without_subheadings_is_unchanged(self):
        self.assertEqual(changelog_section.collapse("Just a note."), "Just a note.")


if __name__ == "__main__":
    unittest.main()
