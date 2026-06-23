"""embed.py: the diagram file-naming slug contract.

Placement (which diagram lands at which heading/flow-label) used to be written
into the docs as managed blocks by embed.upsert_*; it is now derived at build
time by hooks._walk — see test_derivation.py."""

import unittest

from interface_diagrams.embed import qualified_name

from .helpers import SCRIPTS  # noqa: F401


class QualifiedName(unittest.TestCase):
    def test_joins_subsystem_and_parts(self):
        self.assertEqual(qualified_name("Skynode", "AMC", "mavlink-router"), "skynode-amc-mavlink_router")

    def test_none_parts_are_skipped(self):
        self.assertEqual(qualified_name("Sub", None, "Dev", None), "sub-dev")

    def test_filename_unsafe_characters_are_sanitized(self):
        # ':' and '/' reach path-diagram stems via interface names.
        self.assertEqual(qualified_name("Sub", "unix:/tmp/x.sock"), "sub-unix_tmp_x_sock")
        for ch in "[]|#^/\\: ().":
            self.assertNotIn(ch, qualified_name("S", f"a{ch}b"))

    def test_stems_are_lowercase_slugs(self):
        stem = qualified_name("Pilot Pro", "Galaxy Tab S5 (Android)", "AMC")
        self.assertEqual(stem, "pilot_pro-galaxy_tab_s5_android-amc")


if __name__ == "__main__":
    unittest.main()
