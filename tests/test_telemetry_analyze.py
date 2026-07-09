"""Юнит-тесты учёта разобранных записей телеметрии (fresh/mark)."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "telemetry_analyze",
    Path(__file__).resolve().parent.parent / "scripts" / "telemetry_analyze.py",
)
an = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(an)


class TestAnalyzeLedger(unittest.TestCase):
    def setUp(self):
        self.store = tempfile.mkdtemp()
        self.ledger = os.path.join(tempfile.mkdtemp(), "analyzed.json")
        for uuid in ("aaaaaaaaaaaa", "bbbbbbbbbbbb"):
            open(os.path.join(self.store, "m-p-%s.json" % uuid), "w").write("{}")

    def test_all_fresh_initially(self):
        self.assertEqual(
            an.fresh(self.store, self.ledger),
            ["m-p-aaaaaaaaaaaa.json", "m-p-bbbbbbbbbbbb.json"],
        )

    def test_mark_removes_from_fresh(self):
        an.mark(self.store, self.ledger, ["m-p-aaaaaaaaaaaa.json"], "2026-07-09T00:00:00+00:00")
        self.assertEqual(an.fresh(self.store, self.ledger), ["m-p-bbbbbbbbbbbb.json"])
        # метка времени сохранена по run_uuid
        with open(self.ledger) as f:
            self.assertEqual(json.load(f)["aaaaaaaaaaaa"], "2026-07-09T00:00:00+00:00")

    def test_mark_all_fresh(self):
        an.mark(self.store, self.ledger, an.fresh(self.store, self.ledger), "2026-07-09T00:00:00+00:00")
        self.assertEqual(an.fresh(self.store, self.ledger), [])

    def test_missing_store_is_empty(self):
        self.assertEqual(an.fresh("/no/such/dir", self.ledger), [])


if __name__ == "__main__":
    unittest.main()
