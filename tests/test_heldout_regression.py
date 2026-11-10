"""The first H result is immutable; later runs are explicitly regression checks."""

import hashlib
import json
from pathlib import Path
import unittest

from citeafter.evaluation import EXAMPLES, evaluate_cases

ROOT = Path(__file__).resolve().parents[1]


class PublishedHoldout(unittest.TestCase):
    def test_original_failed_report_is_preserved_and_known_regressions_are_closed(self):
        report_path = ROOT / "docs/results/heldout-first-v1.json"
        original = json.loads(report_path.read_bytes())
        provenance = json.loads((ROOT / "docs/results/provenance.json").read_bytes())
        self.assertEqual(
            hashlib.sha256(report_path.read_bytes()).hexdigest(), provenance["holdout"]["first_report_sha256"]
        )
        self.assertFalse(original["passed"])
        self.assertEqual(original["summary"]["passed_decisions"], 8)
        self.assertEqual(sum(len(row["mismatches"]) for row in original["cases"]), 6)
        for row in provenance["holdout"]["files"]:
            self.assertEqual(hashlib.sha256((ROOT / row["file"]).read_bytes()).hexdigest(), row["sha256"])
        current = evaluate_cases(EXAMPLES / "heldout")
        self.assertFalse(current["passed"])
        self.assertEqual(current["summary"]["decisions"], 10)
        self.assertEqual(current["summary"]["passed_decisions"], 9)
        self.assertEqual(current["summary"]["confusion_matrix"], original["summary"]["confusion_matrix"])
        for name in ("unsafe_releases", "unsafe_previews", "false_blocks", "temporal_mismatches"):
            self.assertEqual(current["summary"][name], 0)
        issues = {row["id"]: row["mismatches"] for row in current["cases"] if row["mismatches"]}
        self.assertEqual(set(issues), {"H09"})
        self.assertEqual(len(issues["H09"]), 1)
        self.assertEqual((issues["H09"][0]["phase"], issues["H09"][0]["field"]), ("initial", "read_values"))
        self.assertEqual(issues["H09"][0]["category"], "citation_dependency")
        self.assertTrue(current["input_bytes_unchanged"] and current["component_bytes_unchanged"])


if __name__ == "__main__":
    unittest.main()
