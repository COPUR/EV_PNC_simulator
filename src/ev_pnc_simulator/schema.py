"""JSON Schema helpers for the pinned OpenAPI contracts: validation and minimal valid samples.

OICP files have every `$ref` inlined; OPCP files keep local `#/components/...` refs. Both are handled.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from jsonschema import Draft7Validator

# Upstream writes `maximum` on strings where it means `maxLength`; Draft 7 ignores it on non-numbers.
_FALLBACK_STRING = {
    "date-time": "2026-11-02T10:00:00.000Z",
    "date": "2026-11-02",
    "uuid": "b2688855-7f00-0002-6d8e-48d883f6abb6",
    "uri": "https://example.invalid/x",
    "email": "x@example.invalid",
}


class SampleError(ValueError):
    pass


def resolve(doc: dict, node: Any) -> Any:
    """Follow local `$ref`s until a concrete schema node is reached."""
    seen = 0
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            return {}  # a reference into another file (OPCP error bodies): treated as an open schema
        target: Any = doc
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        node = target
        seen += 1
        if seen > 50:
            raise SampleError("reference loop")
    return node


def inline(doc: dict, node: Any, _stack: tuple[str, ...] = ()) -> Any:
    """Deep copy of `node` with local refs expanded; a reference inside itself is cut to an open schema."""
    if isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/") or ref in _stack:
            return {}
        return inline(doc, resolve(doc, node), (*_stack, ref))
    if isinstance(node, dict):
        return {k: inline(doc, v, _stack) for k, v in node.items()}
    if isinstance(node, list):
        return [inline(doc, v, _stack) for v in node]
    return node


def validator(doc: dict, schema: Any, ignore_required: tuple[str, ...] = ()) -> Draft7Validator:
    """Validator for `schema`; `ignore_required` lists top-level properties the contract wrongly requires."""
    schema = inline(doc, schema)
    if ignore_required and isinstance(schema, dict) and "required" in schema:
        schema = {**schema, "required": [r for r in schema["required"] if r not in ignore_required]}
    return Draft7Validator(schema)


def errors(v: Draft7Validator, instance: Any) -> list[str]:
    out = []
    for e in sorted(v.iter_errors(instance), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in e.absolute_path) or "(body)"
        out.append(f"{where}: {e.message}")
    return out


def sample(doc: dict, node: Any, *, depth: int = 0, prefer: str | None = None) -> Any:
    """Smallest instance that satisfies `node`: required properties only, examples where given."""
    node = resolve(doc, node)
    if not isinstance(node, dict):
        return {}
    if depth > 8:
        raise SampleError("schema too deep for a minimal sample")
    if "example" in node and node.get("type") not in ("object", "array"):
        return copy.deepcopy(node["example"])
    if "enum" in node:
        return node["enum"][0]
    if "allOf" in node:
        merged: dict = {}
        for part in node["allOf"]:
            piece = sample(doc, part, depth=depth + 1, prefer=prefer)
            if isinstance(piece, dict):
                merged.update(piece)
        return merged
    for key in ("oneOf", "anyOf"):
        if key in node:
            branches = node[key]
            chosen = branches[0]
            if prefer:
                chosen = next((b for b in branches if prefer in resolve(doc, b).get("properties", {})), chosen)
            return sample(doc, chosen, depth=depth + 1, prefer=prefer)
    kind = node.get("type")
    if kind is None and "properties" in node:
        kind = "object"
    if kind == "object":
        props = node.get("properties", {})
        return {name: sample(doc, props[name], depth=depth + 1) for name in node.get("required", []) if name in props}
    if kind == "array":
        items = node.get("items", {})
        want = node.get("minItems", 0)
        return [sample(doc, items, depth=depth + 1) for _ in range(want)]
    if kind == "boolean":
        return True
    if kind in ("integer", "number"):
        return node.get("minimum", 0)
    if kind == "string":
        if "example" in node:
            return node["example"]
        if node.get("format") in _FALLBACK_STRING:
            return _FALLBACK_STRING[node["format"]]
        if "pattern" in node:
            return from_pattern(node["pattern"])
        return "x" * max(node.get("minLength", 1), 1)
    return {}


def from_pattern(pattern: str) -> str:
    """Shortest string the regex accepts. Covers literals, classes, groups, first branch, minimum repeats."""
    value = _gen(re._parser.parse(pattern))
    if re.search(pattern, value) is None:
        raise SampleError(f"could not build a string for {pattern!r}")
    return value


def _gen(tree) -> str:
    out = []
    for op, arg in tree:
        name = str(op)
        if name == "LITERAL":
            out.append(chr(arg))
        elif name == "NOT_LITERAL":
            out.append("a" if arg != ord("a") else "b")
        elif name == "ANY":
            out.append("a")
        elif name == "IN":
            out.append(_pick(arg))
        elif name in ("MAX_REPEAT", "MIN_REPEAT"):
            lo, _hi, sub = arg
            out.append(_gen(sub) * lo)
        elif name == "SUBPATTERN":
            out.append(_gen(arg[-1]))
        elif name == "BRANCH":
            out.append(_gen(arg[1][0]))
        elif name == "CATEGORY":
            out.append(_category(arg))
        # AT (anchors) and lookarounds add nothing
    return "".join(out)


def _pick(items) -> str:
    for op, arg in items:
        name = str(op)
        if name == "LITERAL":
            return chr(arg)
        if name == "RANGE":
            return chr(arg[0])
        if name == "CATEGORY":
            return _category(arg)
    return "a"


def _category(arg) -> str:
    name = str(arg)
    if "DIGIT" in name and "NOT" not in name:
        return "0"
    if "SPACE" in name and "NOT" not in name:
        return " "
    return "a"
