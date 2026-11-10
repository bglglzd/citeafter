"""Published H is now regression data; retain its original strict failures."""

import hashlib
import json
from pathlib import Path
import unittest

from citeafter.evaluation import EXAMPLES, evaluate_cases

ROOT = Path(__file__).resolve().parents[1]


class PublishedHoldout(unittest.TestCase):
    def test_business_invariants_and_original_diagnostic_disagreements_remain_visible(self):
        report_path = ROOT / "docs/results/heldout-first-v1.json"
        original = json.loads(report_path.read_bytes())
        provenance = json.loads((ROOT / "docs/results/provenance.json").read_bytes())
        self.assertEqual(
            hashlib.sha256(report_path.read_bytes()).hexdigest(), provenance["holdout"]["first_report_sha256"]
        )
        self.assertFalse(original["passed"])
        for row in provenance["holdout"]["files"]:
            self.assertEqual(hashlib.sha256((ROOT / row["file"]).read_bytes()).hexdigest(), row["sha256"])
        current = evaluate_cases(EXAMPLES / "heldout")
        self.assertFalse(current["passed"])
        self.assertEqual(current["summary"]["decisions"], 10)
        self.assertEqual(current["summary"]["passed_decisions"], 8)
        self.assertEqual(current["summary"]["confusion_matrix"], original["summary"]["confusion_matrix"])
        for name in ("unsafe_releases", "unsafe_previews", "false_blocks", "temporal_mismatches"):
            self.assertEqual(current["summary"][name], 0)
        actual_issues = {row["id"]: row["mismatches"] for row in current["cases"] if row["mismatches"]}
        original_issues = {row["id"]: row["mismatches"] for row in original["cases"] if row["mismatches"]}
        self.assertEqual(actual_issues, original_issues)
        self.assertEqual(sum(len(rows) for rows in actual_issues.values()), 6)
        self.assertTrue(
            all(issue["field"] == "reason_paths" for rows in actual_issues.values() for issue in rows)
        )
        self.assertTrue(current["input_bytes_unchanged"] and current["component_bytes_unchanged"])


if __name__ == "__main__":
    unittest.main()
