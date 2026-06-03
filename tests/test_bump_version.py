import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import bump_version as bv


class TestBumpPure(unittest.TestCase):
    def test_patch(self):
        self.assertEqual(bv.bump("0.1.0", "patch"), "0.1.1")

    def test_minor_resets_patch(self):
        self.assertEqual(bv.bump("0.1.5", "minor"), "0.2.0")

    def test_major_resets_minor_patch(self):
        self.assertEqual(bv.bump("1.4.7", "major"), "2.0.0")

    def test_invalid_version_raises(self):
        with self.assertRaises(ValueError):
            bv.bump("1.2", "patch")

    def test_invalid_level_raises(self):
        with self.assertRaises(ValueError):
            bv.bump("1.2.3", "huge")


class TestManifests(unittest.TestCase):
    def _repo(self, version="0.1.0", market_version=None):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".claude-plugin"))
        with open(
            os.path.join(d, ".claude-plugin", "plugin.json"), "w", encoding="utf-8"
        ) as f:
            json.dump({"name": "circle-skill", "version": version}, f)
        with open(os.path.join(d, "marketplace.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "name": "circle-skill",
                    "plugins": [
                        {"name": "circle-skill", "version": market_version or version}
                    ],
                },
                f,
            )
        return d

    def test_current_version_reads_synced(self):
        d = self._repo("0.3.2")
        self.assertEqual(bv.current_version(d), "0.3.2")

    def test_current_version_detects_desync(self):
        d = self._repo("0.1.0", market_version="0.2.0")
        with self.assertRaises(ValueError):
            bv.current_version(d)

    def test_set_version_updates_both_files_in_sync(self):
        d = self._repo("0.1.0")
        bv.set_version(d, "0.1.1")
        with open(
            os.path.join(d, ".claude-plugin", "plugin.json"), encoding="utf-8"
        ) as f:
            pj = json.load(f)
        with open(os.path.join(d, "marketplace.json"), encoding="utf-8") as f:
            mj = json.load(f)
        self.assertEqual(pj["version"], "0.1.1")
        self.assertEqual(mj["plugins"][0]["version"], "0.1.1")
        self.assertEqual(bv.current_version(d), "0.1.1")


if __name__ == "__main__":
    unittest.main()
