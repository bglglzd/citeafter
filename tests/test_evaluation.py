import json
from pathlib import Path
import tempfile
import unittest

from citeafter.evaluation import EXAMPLES, evaluate_cases


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owned = tempfile.TemporaryDirectory(prefix="citeafter-evaluator-tests-")
        cls.root = Path(cls.owned.name)
        cls.original = {path.name: path.read_bytes() for path in (EXAMPLES / "development").glob("*.json")}
        cls.report = evaluate_cases(EXAMPLES / "development", output_dir=cls.root / "attempt")

    @classmethod
    def tearDownClass(cls):
        cls.owned.cleanup()

    def test_original_development_labels_remain_visible_after_dependency_contract_expansion(
        self,
    ):
        report = self.report
        self.assertFalse(report["passed"])
        issues = {row["id"]: row["mismatches"] for row in report["cases"] if row["mismatches"]}
        self.assertEqual(set(issues), {"D17", "D19"})
        self.assertEqual(sum(map(len, issues.values())), 7)
        self.assertTrue(
            all(issue["category"] == "citation_dependency" for rows in issues.values() for issue in rows)
        )
        self.assertEqual({issue["field"] for issue in issues["D17"]}, {"reason_paths"})
        self.assertEqual({issue["field"] for issue in issues["D19"]}, {"reason_paths", "read_values"})
        self.assertTrue(report["input_bytes_unchanged"])
        summary = report["summary"]
        self.assertEqual(summary["decisions"], 20)
        self.assertEqual(summary["passed_decisions"], 18)
        self.assertEqual(
            summary["confusion_matrix"],
            {
                "affected": {"affected": 8, "unaffected": 0, "needs_review": 0},
                "unaffected": {"affected": 0, "unaffected": 6, "needs_review": 0},
                "needs_review": {"affected": 0, "unaffected": 0, "needs_review": 6},
            },
        )
        self.assertEqual(summary["binary_affected"]["eligible_decisions"], 14)
        self.assertEqual(
            (
                summary["binary_affected"]["tp"],
                summary["binary_affected"]["fp"],
                summary["binary_affected"]["fn"],
            ),
            (8, 0, 0),
        )
        self.assertEqual(summary["historical_queries"], 140)
        self.assertEqual(summary["unsafe_releases"], 0)
        self.assertEqual(summary["old_preview_release_attempts"], 16)
        self.assertEqual(summary["false_blocks"], 0)
        self.assertEqual(summary["diagnostic_specificity_differences"], 0)

    def test_actual_profile_distinguishes_lightweight_inspection_from_recipe_execution(
        self,
    ):
        summary = self.report["summary"]
        self.assertEqual(summary["v2_rechecked_decisions"], 8)
        self.assertEqual(summary["full_recompute_decisions"], 20)
        self.assertEqual(summary["v2_decisions_inspected"], 20)
        phases = summary["operation_measurements"]
        self.assertEqual(phases["v2_update"]["recipe_invocations"], 0)
        self.assertEqual(phases["v2_recheck"]["recipe_invocations"], 8)
        self.assertEqual(phases["v2_full_recompute"]["recipe_invocations"], 20)
        self.assertEqual(phases["v2_full_recompute"]["supported_recipe_results"], 14)
        for phase in (
            "inspection",
            "gate_preview",
            "old_preview_release_probe",
            "explicit_release",
            "receipt_replay",
        ):
            self.assertEqual(phases[phase]["recipe_invocations"], 0)

    def test_gold_and_previous_attempt_are_preserved(self):
        self.assertEqual(
            self.original,
            {path.name: path.read_bytes() for path in (EXAMPLES / "development").glob("*.json")},
        )
        target = self.root / "attempt" / "report.json"
        before = target.read_bytes()
        with self.assertRaises(FileExistsError):
            evaluate_cases(EXAMPLES / "development", output_dir=self.root / "attempt")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(len(list((self.root / "attempt").glob("case-*.json"))), 20)

    def test_scorer_rejects_wrong_class_and_wrong_independent_output_without_editing_original_gold(
        self,
    ):
        altered = self.root / "scorer-corruption-fixture"
        altered.mkdir()
        discount = json.loads(self.original["D02.json"])
        discount["after_v2"]["impact_class"] = "unaffected"
        shipping = json.loads(self.original["D12.json"])
        shipping["after_v2"]["independently_reevaluated_output"]["days"] = 999
        for case in (discount, shipping):
            (altered / (case["id"] + ".json")).write_text(json.dumps(case), encoding="utf-8")
        result = evaluate_cases(altered)
        self.assertFalse(result["passed"])
        issues = [issue for case in result["cases"] for issue in case["mismatches"]]
        self.assertTrue(
            any(issue["field"] == "impact_class" and issue["phase"] == "after_v2" for issue in issues)
        )
        self.assertTrue(
            any(issue["field"] == "output" and issue["phase"] == "v2_full_recompute" for issue in issues)
        )
        self.assertEqual(result["summary"]["binary_affected"]["fp"], 1)


if __name__ == "__main__":
    unittest.main()
