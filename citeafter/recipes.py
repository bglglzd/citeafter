"""Supported field reads are explicit; impact inspection does not execute recipes."""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import re
from .policy import (CiteafterError, canonical, decimal_value, fields, lookup, pointer,
                     require, semantic_value, text, tokens, value_hash)

EVALUATOR = "citeafter-recipes-v1"
READS = {
    "discount_quote": ["kind", "normative_text", "rate_percent", "minimum_amount", "currency"],
    "shipping_days": ["kind", "normative_text", "days", "destination"],
}


def check_inputs(recipe, inputs):
    if recipe == "discount_quote":
        fields(inputs, ["amount", "currency"], "discount input")
        decimal_value(inputs["amount"])
        require(isinstance(inputs["currency"], str) and re.fullmatch(r"[A-Z]{3}", inputs["currency"]), "Use an explicit currency code")
    elif recipe == "shipping_days":
        fields(inputs, ["destination"], "shipping input")
        require(bool(text(inputs["destination"], 80)), "Destination cannot be empty")
    elif recipe == "manual_claim":
        fields(inputs, ["text"], "manual input")
        require(bool(text(inputs["text"], 2000)), "Manual text cannot be empty")
    else:
        raise CiteafterError("invalid_input", "Unknown recipe")


def citation(source, path):
    rendered = source["rendered"]
    span = rendered["spans"].get(path)
    require(span is not None, "Pointer has no rendered citation", "missing_dependency")
    return {"source_sha256": source["sha256"], "source_version": source["version_id"],
            "renderer": rendered["renderer"], "render_sha256": rendered["sha256"],
            "pointer": path, "unit": "utf8-bytes", **span}


def inspect_recipe(source, recipe, clause, inputs, declared_paths=None):
    check_inputs(recipe, inputs)
    require(isinstance(clause, str) and 1 <= len(clause) <= 80 and clause.isprintable(), "Invalid clause ID")
    primary = pointer(["clauses", clause])
    if recipe == "manual_claim":
        require(type(declared_paths) is list and len(declared_paths) <= 16 and all(isinstance(path, str) for path in declared_paths)
                and len(set(declared_paths)) == len(declared_paths), "Use at most sixteen unique declared manual pointers")
        paths = []
        for path in declared_paths:
            tokens(path); paths.append(path)
    else:
        require(declared_paths is None, "Supported recipe dependencies are computed, not supplied")
        paths = [pointer(["clauses", clause, name]) for name in READS[recipe]]
    values, semantic, citations, missing = {}, {}, [], []
    for path in paths:
        try:
            value = lookup(source["policy"], path)
        except CiteafterError as error:
            if error.code != "missing_dependency":
                raise
            missing.append(path); continue
        values[path] = deepcopy(value)
        semantic[path] = semantic_value(path, value)
        citations.append(citation(source, path))
    require(sum(len(item["quote"].encode("utf-8")) for item in citations) <= 64 * 1024,
            "Citation material exceeds64KiB; choose narrower manual pointers")
    reasons, reason_paths = [], []
    if recipe == "manual_claim":
        reasons.append("manual_dependency_coverage_unproven"); reason_paths.append(primary)
    if missing:
        reasons.append("missing_dependency"); reason_paths.extend(missing)
    clause_value = source["policy"]["clauses"].get(clause)
    expected_kind = {"discount_quote": "discount", "shipping_days": "shipping"}.get(recipe)
    if recipe != "manual_claim" and (clause_value is None or clause_value["kind"] != expected_kind):
        reasons.append("unsupported_clause_kind"); reason_paths.append(pointer(["clauses", clause, "kind"]))
    if recipe != "manual_claim" and clause_value and clause_value["normative_text"]:
        reasons.append("nonempty_normative_text_requires_review"); reason_paths.append(pointer(["clauses", clause, "normative_text"]))
    if recipe == "discount_quote" and clause_value and clause_value["kind"] == "discount" and inputs["currency"] != clause_value["currency"]:
        reasons.append("unsupported_input_currency"); reason_paths.append(pointer(["clauses", clause, "currency"]))
    if recipe == "shipping_days" and clause_value and clause_value["kind"] == "shipping" and inputs["destination"] != clause_value["destination"]:
        reasons.append("unsupported_input_destination"); reason_paths.append(pointer(["clauses", clause, "destination"]))
    return {"recipe": recipe, "evaluator": EVALUATOR, "clause": clause, "inputs": deepcopy(inputs),
            "input_sha256": value_hash(inputs), "result_status": "needs_review" if reasons else "supported",
            "coverage_proven": recipe != "manual_claim" and not missing and not ("unsupported_clause_kind" in reasons),
            "required_paths": None if recipe == "manual_claim" else paths, "declared_paths": paths,
            "read_values": values, "semantic_values": semantic, "dependency_sha256": value_hash(semantic),
            "citations": citations, "reason_codes": sorted(set(reasons)), "reason_paths": sorted(set(reason_paths)),
            "source_sha256": source["sha256"], "source_version": source["version_id"]}


def evaluate_recipe(source, recipe, clause, inputs, declared_paths=None):
    result = inspect_recipe(source, recipe, clause, inputs, declared_paths)
    result["output"] = None
    if result["result_status"] != "supported":
        return result
    rule = source["policy"]["clauses"][clause]
    if recipe == "discount_quote":
        amount = decimal_value(inputs["amount"])
        eligible = amount >= decimal_value(rule["minimum_amount"])
        reduction = (amount * decimal_value(rule["rate_percent"]) / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if eligible else Decimal("0.00")
        result["output"] = {"eligible": eligible, "discount_amount": format(reduction, ".2f"),
                            "net_amount": format(amount - reduction, ".2f"), "currency": inputs["currency"]}
    else:
        result["output"] = {"days": rule["days"], "destination": inputs["destination"]}
    return result


def assess_support(saved, current):
    changed = sorted(path for path in set(saved["semantic_values"]) | set(current["semantic_values"])
                     if path not in saved["semantic_values"] or path not in current["semantic_values"]
                     or canonical(saved["semantic_values"][path]) != canonical(current["semantic_values"][path]))
    if current["result_status"] != "supported" or not current["coverage_proven"] or not saved["coverage_proven"]:
        return {"impact_class": "needs_review", "reason_codes": current["reason_codes"] or ["dependency_coverage_unproven"],
                "reason_paths": current["reason_paths"] or [pointer(["clauses", current["clause"]])], "changed_required_paths": changed}
    if changed:
        return {"impact_class": "affected", "reason_codes": ["changed_required_business_value"],
                "reason_paths": changed, "changed_required_paths": changed}
    return {"impact_class": "unaffected", "reason_codes": [], "reason_paths": [], "changed_required_paths": []}
