import copy
import json
from pathlib import Path
import unittest

from citeafter.policy import (
    CiteafterError,
    digest,
    instant,
    intersects,
    lookup,
    parse_policy,
    pointer,
    render_policy,
    select_source,
    source_record,
    strict_json,
    tokens,
)
from citeafter.recipes import assess_support, evaluate_recipe, inspect_recipe

EXAMPLES = Path(__file__).resolve().parents[1] / "citeafter/examples/policies"
OBSERVED = ["2030-01-03T09:00:00Z", "2030-01-04T15:00:00Z", "2030-01-08T09:00:00Z"]


def sources():
    return [
        source_record((EXAMPLES / ("v" + str(index + 1) + ".json")).read_bytes(), at)
        for index, at in enumerate(OBSERVED)
    ]


class TemporalEvidence(unittest.TestCase):
    def test_future_announcement_does_not_rewrite_friday(self):
        rows = sources()
        chosen = select_source(rows, "2030-01-04T12:00:00Z", "2030-01-09T12:00:00Z")
        self.assertEqual(chosen["source"]["version_id"], "v1")

    def test_both_knowledge_and_business_boundaries_are_inclusive(self):
        rows = sources()
        self.assertEqual(
            select_source(rows, "2030-01-08T12:00:00Z", "2030-01-04T14:59:59.999999Z")["source"][
                "version_id"
            ],
            "v1",
        )
        self.assertEqual(
            select_source(rows, "2030-01-07T00:00:00Z", OBSERVED[1])["source"]["version_id"], "v2"
        )
        self.assertEqual(
            select_source(rows, "2030-01-06T23:59:59.999999Z", OBSERVED[1])["source"]["version_id"], "v1"
        )
        self.assertEqual(
            select_source(rows, "2030-01-08T12:00:00Z", OBSERVED[2])["source"]["version_id"], "v3"
        )

    def test_equal_effective_fork_cannot_win_by_observation_order(self):
        rows = sources()
        fork = copy.deepcopy(rows[2]["policy"])
        fork["version_id"] = "fork"
        fork["parent_version_id"] = "v1"
        rows.append(source_record(json.dumps(fork).encode(), "2030-01-09T00:00:00Z"))
        selected = select_source(rows, "2030-01-10T00:00:00Z", "2030-01-10T00:00:00Z")
        self.assertEqual(selected["status"], "needs_review")
        self.assertIsNone(selected["source"])

    def test_future_parent_knowledge_cannot_resolve_an_earlier_missing_parent(self):
        rows = sources()
        child = copy.deepcopy(rows[1]["policy"])
        child["parent_version_id"] = "missing"
        parent = copy.deepcopy(rows[0]["policy"])
        parent["version_id"] = "missing"
        available = [
            rows[0],
            source_record(json.dumps(child).encode(), OBSERVED[1]),
            source_record(json.dumps(parent).encode(), OBSERVED[2]),
        ]
        self.assertEqual(
            select_source(available, "2030-01-08T12:00:00Z", OBSERVED[1])["reason"],
            "missing_or_conflicting_known_parent",
        )

    def test_explicit_backdated_descendant_can_correct_a_later_effective_parent(self):
        rows = sources()[:2]
        child = copy.deepcopy(rows[1]["policy"])
        child["version_id"] = "correction"
        child["parent_version_id"] = "v2"
        child["effective_from"] = "2030-01-05T00:00:00Z"
        rows.append(source_record(json.dumps(child).encode(), OBSERVED[2]))
        self.assertEqual(
            select_source(rows, "2030-01-06T12:00:00Z", OBSERVED[2])["source"]["version_id"], "correction"
        )
        self.assertEqual(
            select_source(rows, "2030-01-06T12:00:00Z", OBSERVED[1])["source"]["version_id"], "v1"
        )

    def test_conflicting_version_bytes_require_review(self):
        rows = sources()[:1]
        changed = copy.deepcopy(rows[0]["policy"])
        changed["clauses"]["standard"]["rate_percent"] = "99.00"
        rows.append(source_record(json.dumps(changed).encode(), OBSERVED[1]))
        self.assertEqual(
            select_source(rows, "2030-01-08T12:00:00Z", OBSERVED[1])["reason"], "conflicting_version_identity"
        )

    def test_unicode_pointer_citation_uses_exact_utf8_bytes(self):
        policy = copy.deepcopy(sources()[0]["policy"])
        name = "返送/a~b"
        policy["clauses"][name] = {"kind": "shipping", "days": 3, "destination": "国内", "normative_text": ""}
        source = source_record(json.dumps(policy, ensure_ascii=False).encode(), OBSERVED[0])
        path = pointer(["clauses", name, "destination"])
        self.assertEqual(tokens(path), ["clauses", name, "destination"])
        result = evaluate_recipe(source, "shipping_days", name, {"destination": "国内"})
        quote = next(item for item in result["citations"] if item["pointer"] == path)
        self.assertEqual(source["rendered"]["body"][quote["start"] : quote["end"]].decode(), quote["quote"])
        self.assertEqual(quote["source_sha256"], digest(source["raw"]))
        self.assertEqual(lookup(policy, path), "国内")

    def test_pointer_intersection_compares_tokens_not_string_prefixes(self):
        self.assertTrue(intersects("/clauses/standard", "/clauses/standard/rate_percent"))
        self.assertFalse(intersects("/clauses/standard", "/clauses/standard_extra/rate_percent"))
        self.assertFalse(intersects("/clauses/a~1b", "/clauses/a/b"))
        with self.assertRaises(CiteafterError):
            tokens("/bad~2escape")

    def test_duplicate_keys_unpaired_unicode_and_untrusted_observation_are_refused(self):
        for raw in [b'{"a":1,"a":2}', b'{"a":"\\ud800"}', b'{"a":NaN}']:
            with self.assertRaises(CiteafterError):
                strict_json(raw)
        policy = copy.deepcopy(sources()[0]["policy"])
        policy["observed_at"] = OBSERVED[0]
        with self.assertRaises(CiteafterError):
            parse_policy(json.dumps(policy).encode())

    def test_exact_decimal_rounding_and_equal_output_do_not_hide_impact(self):
        first, second, _ = sources()
        result = evaluate_recipe(
            first, "discount_quote", "standard", {"amount": "1000.05", "currency": "USD"}
        )
        self.assertEqual(result["output"]["discount_amount"], "100.01")
        old = evaluate_recipe(first, "discount_quote", "standard", {"amount": "500.00", "currency": "USD"})
        current = inspect_recipe(second, "discount_quote", "standard", old["inputs"])
        self.assertEqual(assess_support(old, current)["impact_class"], "affected")
        self.assertIn("/clauses/standard/rate_percent", old["read_values"])
        self.assertEqual(
            evaluate_recipe(second, "discount_quote", "standard", old["inputs"])["output"], old["output"]
        )

    def test_cosmetic_change_does_not_clear_unrechecked_old_support(self):
        first, second, third = sources()
        old = evaluate_recipe(first, "discount_quote", "standard", {"amount": "2000.00", "currency": "USD"})
        self.assertEqual(
            assess_support(old, inspect_recipe(third, "discount_quote", "standard", old["inputs"]))[
                "impact_class"
            ],
            "affected",
        )
        updated = evaluate_recipe(second, "discount_quote", "standard", old["inputs"])
        self.assertEqual(
            assess_support(updated, inspect_recipe(third, "discount_quote", "standard", old["inputs"]))[
                "impact_class"
            ],
            "unaffected",
        )
        self.assertTrue(all(quote["source_version"] == "v2" for quote in updated["citations"]))

    def test_normative_text_and_missing_manual_edges_never_prove_support(self):
        first, second, _ = sources()
        old = evaluate_recipe(first, "discount_quote", "vip", {"amount": "7000.00", "currency": "USD"})
        self.assertEqual(
            assess_support(old, inspect_recipe(second, "discount_quote", "vip", old["inputs"]))[
                "impact_class"
            ],
            "needs_review",
        )
        manual = evaluate_recipe(
            first, "manual_claim", "manual_note", {"text": "Review terms"}, ["/clauses/shipping/days"]
        )
        now = inspect_recipe(
            second, "manual_claim", "manual_note", manual["inputs"], ["/clauses/shipping/days"]
        )
        self.assertFalse(manual["coverage_proven"])
        self.assertEqual(assess_support(manual, now)["impact_class"], "needs_review")
        own_text = "/clauses/manual_note/normative_text"
        self.assertIn(own_text, manual["read_values"])
        self.assertIn(own_text, {item["pointer"] for item in manual["citations"]})
        assessment = assess_support(manual, now)
        self.assertEqual(assessment["changed_required_paths"], [own_text])
        self.assertEqual(assessment["reason_paths"], [own_text, "/clauses/shipping/days"])

    def test_unsupported_input_explanation_retains_independent_changed_values(self):
        first, second, _ = sources()
        saved = evaluate_recipe(first, "discount_quote", "standard", {"amount": "2000.00", "currency": "EUR"})
        current = inspect_recipe(second, "discount_quote", "standard", saved["inputs"])
        assessment = assess_support(saved, current)
        self.assertEqual(assessment["impact_class"], "needs_review")
        self.assertEqual(
            assessment["reason_paths"], ["/clauses/standard/currency", "/clauses/standard/rate_percent"]
        )

    def test_unsupported_currency_is_not_converted_and_parent_citation_is_bound(self):
        first = sources()[0]
        result = evaluate_recipe(
            first, "discount_quote", "standard", {"amount": "2000.00", "currency": "EUR"}
        )
        self.assertIsNone(result["output"])
        self.assertEqual(result["reason_paths"], ["/clauses/standard/currency"])
        manual = evaluate_recipe(
            first, "manual_claim", "manual_note", {"text": "Review"}, ["/clauses/manual_note"]
        )
        self.assertEqual(len(manual["citations"]), 1)
        rendered = render_policy(first["policy"])
        quote = manual["citations"][0]
        self.assertEqual(rendered["body"][quote["start"] : quote["end"]].decode(), quote["quote"])

    def test_offset_equivalence_and_explicit_instants(self):
        self.assertEqual(instant("2030-01-04T15:00:00+03:00"), instant("2030-01-04T12:00:00Z"))
        for value in ["2030-01-04", "2030-01-04T12:00:00", "2030-02-30T00:00:00Z"]:
            with self.assertRaises(CiteafterError):
                instant(value)

    def test_invalid_offset_minutes_cannot_be_normalized_into_a_different_instant(self):
        for value in ["2030-01-04T12:00:00+01:60", "2030-01-04T12:00:00+00:60", "2030-01-04T12:00:00-02:99"]:
            with self.assertRaises(CiteafterError):
                instant(value)

    def test_calendar_overflow_and_oversized_integer_are_typed_invalid_inputs(self):
        with self.assertRaises(CiteafterError):
            instant("0001-01-01T00:00:00+23:59")
        with self.assertRaises(CiteafterError):
            strict_json(b'{"number":' + b"9" * 5000 + b"}")

    def test_lineage_cannot_cross_policy_namespaces(self):
        first, second, _ = sources()
        other = copy.deepcopy(second["policy"])
        other["policy_id"] = "other-policy"
        mixed = [first, source_record(json.dumps(other).encode(), OBSERVED[1])]
        self.assertEqual(
            select_source(mixed, "2030-01-08T12:00:00Z", OBSERVED[1])["reason"], "mixed_policy_families"
        )

    def test_returned_parent_dependency_cannot_mutate_its_source(self):
        source = sources()[0]
        inspected = inspect_recipe(
            source, "manual_claim", "manual_note", {"text": "Review"}, ["/clauses/standard"]
        )
        inspected["semantic_values"]["/clauses/standard"]["rate_percent"] = "99.00"
        result = evaluate_recipe(
            source, "discount_quote", "standard", {"amount": "2000.00", "currency": "USD"}
        )
        self.assertEqual(result["output"]["discount_amount"], "200.00")
        self.assertEqual(source["policy"]["clauses"]["standard"]["rate_percent"], "10.00")


if __name__ == "__main__":
    unittest.main()
