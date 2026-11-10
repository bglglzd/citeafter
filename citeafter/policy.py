"""Closed policy parsing, temporal selection and byte-bound rendered citations."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re

RENDERER = "citeafter-render-v1"
MAX_SOURCE_BYTES = 256 * 1024
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,79}\Z")
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")
NUMBER = re.compile(r"(?:0|[1-9]\d{0,10})(?:\.\d{1,2})?\Z")


class CiteafterError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, message, code="invalid_input"):
    if not condition:
        raise CiteafterError(code, message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def value_hash(value):
    return digest(canonical(value).encode("utf-8"))


def instant(value):
    require(isinstance(value, str) and STAMP.fullmatch(value), "Use an explicit-offset ISO instant")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as error:
        raise CiteafterError("invalid_input", "Invalid calendar instant") from error
    # Fixed precision also makes persisted UTC strings lexically sortable.
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def identifier(value):
    require(isinstance(value, str) and IDENTIFIER.fullmatch(value), "Identifier must be a bounded plain name")
    return value


def text(value, maximum=4000):
    require(isinstance(value, str) and len(value) <= maximum and not any(0xD800 <= ord(c) <= 0xDFFF for c in value),
            "Expected bounded Unicode text without lone surrogates")
    return value


def fields(value, expected, label):
    require(type(value) is dict and set(value) == set(expected), "Unexpected or missing " + label + " fields")


def decimal_value(value, maximum=Decimal("99999999999.99")):
    require(isinstance(value, str) and NUMBER.fullmatch(value), "Use a nonnegative decimal string with at most two places")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise CiteafterError("invalid_input", "Invalid decimal value") from error
    require(number <= maximum, "Decimal value exceeds the supported bound")
    return number


def strict_json(raw, limit=MAX_SOURCE_BYTES):
    require(isinstance(raw, bytes) and len(raw) <= limit, "JSON exceeds the byte limit")
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON object key")
            result[key] = value
        return result
    def constant(_):
        raise CiteafterError("invalid_input", "Nonfinite JSON number")
    try:
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise CiteafterError("invalid_input", "Expected bounded valid UTF-8 JSON") from error
    nodes = 0
    def walk(item, depth):
        nonlocal nodes
        nodes += 1
        require(nodes <= 20000 and depth <= 20, "JSON structure exceeds the supported bound")
        if isinstance(item, str):
            text(item, limit)
        elif type(item) is dict:
            for key, child in item.items():
                text(key, limit); walk(child, depth + 1)
        elif type(item) is list:
            for child in item:
                walk(child, depth + 1)
        elif type(item) is float:
            require(math.isfinite(item), "Nonfinite JSON number")
    walk(value, 0)
    return value


def parse_policy(raw):
    policy = strict_json(raw)
    fields(policy, ["schema_version", "policy_id", "version_id", "parent_version_id", "effective_from", "presentation", "clauses"], "policy")
    require(type(policy["schema_version"]) is int and policy["schema_version"] == 1, "Unsupported policy schema version")
    identifier(policy["policy_id"]); identifier(policy["version_id"])
    if policy["parent_version_id"] is not None:
        identifier(policy["parent_version_id"])
        require(policy["parent_version_id"] != policy["version_id"], "A policy version cannot parent itself")
    instant(policy["effective_from"])
    fields(policy["presentation"], ["title"], "presentation")
    text(policy["presentation"]["title"], 200)
    clauses = policy["clauses"]
    require(type(clauses) is dict and 1 <= len(clauses) <= 1000, "Expected one to 1000 stable clauses")
    for name, clause in clauses.items():
        require(isinstance(name, str) and 1 <= len(name) <= 80 and name.isprintable(), "Clause IDs must be bounded printable strings")
        text(name, 80)
        require(type(clause) is dict, "Expected a typed clause")
        kind = clause.get("kind")
        if kind == "discount":
            fields(clause, ["kind", "rate_percent", "minimum_amount", "currency", "normative_text"], "discount clause")
            decimal_value(clause["rate_percent"], Decimal("100")); decimal_value(clause["minimum_amount"])
            require(isinstance(clause["currency"], str) and re.fullmatch(r"[A-Z]{3}", clause["currency"]), "Use an explicit uppercase currency code")
        elif kind == "shipping":
            fields(clause, ["kind", "days", "destination", "normative_text"], "shipping clause")
            require(type(clause["days"]) is int and 0 <= clause["days"] <= 365, "Shipping days must be an integer from zero to 365")
            require(bool(text(clause["destination"], 80)), "Shipping destination cannot be empty")
        elif kind == "manual":
            fields(clause, ["kind", "normative_text"], "manual clause")
        else:
            raise CiteafterError("invalid_input", "Unknown clause kind; use the declared manual kind for unsupported text")
        text(clause["normative_text"])
    return policy


def pointer(tokens):
    return "".join("/" + token.replace("~", "~0").replace("/", "~1") for token in tokens)


def tokens(path):
    require(isinstance(path, str) and len(path) <= 1024 and (path == "" or path.startswith("/")), "Invalid JSON Pointer")
    result = []
    for token in path.split("/")[1:]:
        require(not re.search(r"~(?![01])", token), "Invalid JSON Pointer escape")
        result.append(token.replace("~1", "/").replace("~0", "~"))
    return result


def intersects(left, right):
    a, b = tokens(left), tokens(right)
    return a[:len(b)] == b or b[:len(a)] == a


def lookup(value, path):
    for token in tokens(path):
        require(type(value) is dict and token in value, "Unresolved policy pointer: " + path, "missing_dependency")
        value = value[token]
    return value


def semantic_value(path, value):
    parts = tokens(path)
    if len(parts) == 3 and parts[0] == "clauses" and parts[-1] in ("rate_percent", "minimum_amount"):
        return format(decimal_value(value).quantize(Decimal("0.01")), "f")
    return value


def render_policy(policy):
    pieces, spans, offset = [], {}, 0
    def walk(value, trail):
        nonlocal offset
        if type(value) is dict:
            start = offset
            for key in sorted(value):
                walk(value[key], trail + [key])
            if offset > start:
                spans[pointer(trail)] = {"start": start, "end": offset}
        else:
            path = pointer(trail)
            line = path + " = " + canonical(value) + "\n"
            data = line.encode("utf-8")
            spans[path] = {"start": offset, "end": offset + len(data), "quote": line}
            pieces.append(data); offset += len(data)
    walk(policy, [])
    body = b"".join(pieces)
    require(len(body) <= 1024 * 1024, "Rendered policy exceeds one MiB")
    for span in spans.values():
        span["quote"] = body[span["start"]:span["end"]].decode("utf-8", "strict")
    return {"renderer": RENDERER, "sha256": digest(body), "body": body, "spans": spans}


def source_record(raw, observed_at):
    policy = parse_policy(raw)
    rendered = render_policy(policy)
    return {"sha256": digest(raw), "policy": policy, "version_id": policy["version_id"],
            "policy_id": policy["policy_id"], "parent": policy["parent_version_id"],
            "effective_from": instant(policy["effective_from"]), "observed_at": instant(observed_at),
            "raw": raw, "rendered": rendered}


def select_source(records, valid_at, known_at):
    valid_at, known_at = instant(valid_at), instant(known_at)
    known = [row for row in records if row["observed_at"] <= known_at]
    by_label = {}
    for row in known:
        by_label.setdefault(row["version_id"], []).append(row)
    eligible = [row for row in known if row["effective_from"] <= valid_at]
    if not eligible:
        return {"status": "needs_review", "reason": "no_effective_known_source", "source": None}
    lineages = {}
    for row in eligible:
        visited, parent = {row["version_id"]}, row["parent"]
        if len(by_label[row["version_id"]]) != 1:
            return {"status": "needs_review", "reason": "conflicting_version_identity", "source": None}
        while parent is not None:
            if parent in visited:
                return {"status": "needs_review", "reason": "cyclic_lineage", "source": None}
            if len(by_label.get(parent, [])) != 1:
                return {"status": "needs_review", "reason": "missing_or_conflicting_known_parent", "source": None}
            visited.add(parent)
            parent = by_label[parent][0]["parent"]
        lineages[row["sha256"]] = visited
    maximal = [row for row in eligible if not any(row["version_id"] in lineages[other["sha256"]]
                and row["sha256"] != other["sha256"] for other in eligible)]
    if len(maximal) != 1:
        return {"status": "needs_review", "reason": "incomparable_effective_versions", "source": None}
    return {"status": "selected", "reason": "unique_effective_lineage", "source": maximal[0]}


def changed_fields(before, after):
    """Parent removal is a parent path, not an unsafe string-prefix match."""
    missing, result = object(), []
    def walk(left, right, trail):
        if type(left) is dict and type(right) is dict:
            for name in sorted(set(left) | set(right)):
                walk(left.get(name, missing), right.get(name, missing), trail + [name])
        elif left != right:
            path = pointer(trail)
            category = "metadata"
            if trail[:1] == ["presentation"]:
                category = "presentation"
            elif trail[:1] == ["clauses"]:
                category = "needs_review" if trail[-1:] in (["normative_text"], ["kind"]) else "business"
            result.append({"path": path, "category": category, "before_present": left is not missing,
                           "after_present": right is not missing, "before": None if left is missing else left,
                           "after": None if right is missing else right})
    walk(before, after, [])
    return result
