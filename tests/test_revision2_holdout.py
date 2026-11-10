"""Published revision-2 cases are regression data after their immutable first score."""

import hashlib
import json
from pathlib import Path
import unittest

from citeafter.evaluation import evaluate_cases

ROOT = Path(__file__).resolve().parents[1]


class RevisionTwoHoldout(unittest.TestCase):
    def test_sealed_cases_and_original_report_match_the_current_frozen_rules(self):
        provenance = json.loads((ROOT / "docs/results/provenance.json").read_bytes())["revision_2"]
        heldout = provenance["holdout"]
        for row in heldout["files"]:
            self.assertEqual(hashlib.sha256((ROOT / row["file"]).read_bytes()).hexdigest(), row["sha256"])
        original = ROOT / heldout["first_report_file"]
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), heldout["first_report_sha256"])
        first = json.loads(original.read_bytes())
        current = evaluate_cases(ROOT / "examples/heldout-v2")
        self.assertTrue(first["passed"] and current["passed"])
        self.assertEqual(current["components"], first["components"])
        self.assertEqual(current["summary"]["decisions"], 10)
        self.assertEqual(current["summary"]["historical_queries"], 120)
        self.assertEqual(current["summary"]["confusion_matrix"], first["summary"]["confusion_matrix"])
        for name in (
            "mismatch_count",
            "unsafe_releases",
            "unsafe_previews",
            "false_blocks",
            "temporal_mismatches",
        ):
            self.assertEqual(current["summary"][name], 0)
        self.assertTrue(current["input_bytes_unchanged"] and current["component_bytes_unchanged"])


if __name__ == "__main__":
    unittest.main()
