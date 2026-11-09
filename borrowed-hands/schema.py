"""A tiny JSON Schema checker for exactly the subset protocol/schemas uses (no jsonschema dependency).

Supported: type (name or list), const, enum, pattern, minimum, maximum, minLength, maxLength, maxItems, items,
properties, required, additionalProperties (false or a schema), $ref (a file name in schemas/), oneOf, allOf,
if/then. Annotation keys are ignored. Any other keyword is a programming error and raises at load time, so a
schema can never silently check less than it says.

Patterns follow ECMA-262 semantics for `$`: it matches only at the very end (Python's `$` also matches before a
trailing newline, so a trailing `$` is compiled as `\\Z`).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, List

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

ERROR_CODES = ("denied", "needs_grant", "helper_unavailable", "helper_busy", "invalid_arguments", "expired",
               "replay", "timeout", "cancelled", "needs_resolution", "internal_error")

_ANNOTATIONS = {"$schema", "$id", "title", "description", "default"}
_KEYWORDS = {"type", "const", "enum", "pattern", "minimum", "maximum", "minLength", "maxLength", "maxItems",
             "items", "properties", "required", "additionalProperties", "$ref", "oneOf", "allOf", "if", "then"}


def _walk_keywords(node: Any, where: str) -> None:
    if not isinstance(node, dict):
        return
    unknown = set(node) - _KEYWORDS - _ANNOTATIONS
    if unknown:
        raise ValueError(f"{where}: unsupported schema keyword(s) {sorted(unknown)}")
    for key in ("items", "if", "then"):
        if key in node:
            _walk_keywords(node[key], f"{where}/{key}")
    if isinstance(node.get("additionalProperties"), dict):
        _walk_keywords(node["additionalProperties"], f"{where}/additionalProperties")
    for k, sub in (node.get("properties") or {}).items():
        _walk_keywords(sub, f"{where}/properties/{k}")
    for key in ("oneOf", "allOf"):
        for i, sub in enumerate(node.get(key) or []):
            _walk_keywords(sub, f"{where}/{key}/{i}")


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    path = SCHEMA_DIR / name
    if path.parent != SCHEMA_DIR or not path.is_file():
        raise KeyError(name)
    doc = json.loads(path.read_text(encoding="utf-8"))
    _walk_keywords(doc, name)
    return doc


@lru_cache(maxsize=None)
def _regex(pattern: str) -> "re.Pattern[str]":
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.compile(pattern)


_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _same(a: Any, b: Any) -> bool:
    """JSON equality: 1 != True, 1 == 1."""
    return type(a) is type(b) and a == b if isinstance(a, bool) or isinstance(b, bool) else a == b


_SAFE_KEY = re.compile(r"[A-Za-z0-9_.-]{1,40}")


def _key(k: Any) -> str:
    """An unknown key as it may appear in an error message: a short plain name as is, anything else (newlines,
    markup, bidi controls, long text) only by its length, so a key can never smuggle text into a message."""
    return k if isinstance(k, str) and _SAFE_KEY.fullmatch(k) else f"(a {len(str(k))}-character name)"


def _check(node: dict, v: Any, path: str, out: List[str]) -> None:
    if "$ref" in node:
        _check(load(node["$ref"]), v, path, out)
    if "type" in node:
        types = node["type"] if isinstance(node["type"], list) else [node["type"]]
        if not any(_TYPES[t](v) for t in types):
            out.append(f"{path}: expected {'/'.join(types)}")
            return
    if "const" in node and not _same(node["const"], v):
        out.append(f"{path}: must equal {node['const']!r}")
    if "enum" in node and not any(_same(e, v) for e in node["enum"]):
        out.append(f"{path}: not one of the allowed values")
    if isinstance(v, str):
        if "pattern" in node and not _regex(node["pattern"]).search(v):
            out.append(f"{path}: does not match the required pattern")
        if "minLength" in node and len(v) < node["minLength"]:
            out.append(f"{path}: shorter than {node['minLength']}")
        if "maxLength" in node and len(v) > node["maxLength"]:
            out.append(f"{path}: longer than {node['maxLength']}")
    if _TYPES["number"](v):
        if "minimum" in node and v < node["minimum"]:
            out.append(f"{path}: below {node['minimum']}")
        if "maximum" in node and v > node["maximum"]:
            out.append(f"{path}: above {node['maximum']}")
    if isinstance(v, list):
        if "maxItems" in node and len(v) > node["maxItems"]:
            out.append(f"{path}: more than {node['maxItems']} items")
        if "items" in node:
            for i, x in enumerate(v):
                _check(node["items"], x, f"{path}[{i}]", out)
    if isinstance(v, dict):
        props = node.get("properties", {})
        for k in node.get("required", []):
            if k not in v:
                out.append(f"{path}: missing {k}")
        extra = node.get("additionalProperties", True)
        for k, x in v.items():
            if k in props:
                _check(props[k], x, f"{path}.{k}", out)
            elif extra is False:
                out.append(f"{path}: unexpected key {_key(k)}")
            elif isinstance(extra, dict):
                _check(extra, x, f"{path}.{_key(k)}", out)
    if "allOf" in node:
        for sub in node["allOf"]:
            _check(sub, v, path, out)
    if "oneOf" in node:
        matches = sum(1 for sub in node["oneOf"] if not _sub_errors(sub, v, path))
        if matches != 1:
            out.append(f"{path}: matches {matches} of oneOf (need exactly 1)")
    if "if" in node and not _sub_errors(node["if"], v, path):
        if "then" in node:
            _check(node["then"], v, path, out)


def _sub_errors(node: dict, v: Any, path: str) -> List[str]:
    out: List[str] = []
    _check(node, v, path, out)
    return out


def errors(name: str, doc: Any) -> List[str]:
    """Validation errors of `doc` against schemas/<name> (empty list = valid). Messages never echo values."""
    out: List[str] = []
    _check(load(name), doc, "$", out)
    return out


def check(node: dict, doc: Any) -> List[str]:
    """Validate against an inline schema (tool parameters). Same subset and semantics as `errors`."""
    _walk_keywords(node, "inline")
    out: List[str] = []
    _check(node, doc, "$", out)
    return out
