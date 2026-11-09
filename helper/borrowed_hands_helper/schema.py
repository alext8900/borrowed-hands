"""Minimal JSON Schema checker for the subset protocol/schemas uses (the authoritative wire shapes).

Supported: type, const, enum, pattern, minimum, maximum, minLength, maxLength, maxItems, items, properties,
required, additionalProperties false, $ref (by file name), oneOf, allOf with if/then. Anything else in a
schema is a bug and raises at load time, so a schema change can never be silently ignored.
`pattern` uses JSON Schema search semantics, except that a trailing `$` means end of string (`\\Z`), so a
trailing newline never passes.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from ._shared import SCHEMAS

_KNOWN = {"$schema", "$id", "title", "description", "type", "const", "enum", "pattern", "minimum", "maximum",
          "minLength", "maxLength", "maxItems", "items", "properties", "required", "additionalProperties",
          "$ref", "oneOf", "allOf", "if", "then"}


class SchemaError(ValueError):
    pass


@lru_cache(maxsize=None)
def _load(name: str) -> dict:
    name = name.removesuffix(".json")
    with open(SCHEMAS / f"{name}.json", "rb") as f:
        s = json.load(f)
    _audit(s)
    return s


def _audit(s: Any) -> None:
    if isinstance(s, dict):
        unknown = set(s) - _KNOWN
        if unknown:
            raise RuntimeError(f"schema uses unsupported keywords {sorted(unknown)}")
        for k, v in s.items():
            if k == "properties":
                for sub in v.values():
                    _audit(sub)
            elif k in ("items", "if", "then"):
                _audit(v)
            elif k in ("oneOf", "allOf"):
                for sub in v:
                    _audit(sub)


@lru_cache(maxsize=None)
def _regex(pattern: str) -> re.Pattern[str]:
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.compile(pattern)


def _type_ok(v: Any, t: str) -> bool:
    if t == "object":
        return isinstance(v, dict)
    if t == "array":
        return isinstance(v, list)
    if t == "string":
        return isinstance(v, str)
    if t == "integer":
        return type(v) is int
    if t == "boolean":
        return type(v) is bool
    if t == "null":
        return v is None
    raise RuntimeError(f"unsupported type {t}")


def _check(v: Any, s: dict, path: str) -> None:
    if "$ref" in s:
        _check(v, _load(s["$ref"]), path)
    if "type" in s:
        types = s["type"] if isinstance(s["type"], list) else [s["type"]]
        if not any(_type_ok(v, t) for t in types):
            raise SchemaError(f"{path}: expected {'/'.join(types)}")
    if "const" in s and not (v == s["const"] and type(v) is type(s["const"])):
        raise SchemaError(f"{path}: must be {s['const']!r}")
    if "enum" in s and not any(v == e and type(v) is type(e) for e in s["enum"]):
        raise SchemaError(f"{path}: not one of the allowed values")
    if isinstance(v, str):
        if "pattern" in s and not _regex(s["pattern"]).search(v):
            raise SchemaError(f"{path}: bad format")
        if "minLength" in s and len(v) < s["minLength"]:
            raise SchemaError(f"{path}: too short")
        if "maxLength" in s and len(v) > s["maxLength"]:
            raise SchemaError(f"{path}: too long")
    if type(v) is int:
        if "minimum" in s and v < s["minimum"]:
            raise SchemaError(f"{path}: below minimum")
        if "maximum" in s and v > s["maximum"]:
            raise SchemaError(f"{path}: above maximum")
    if isinstance(v, list):
        if "maxItems" in s and len(v) > s["maxItems"]:
            raise SchemaError(f"{path}: too many items")
        if "items" in s:
            for i, x in enumerate(v):
                _check(x, s["items"], f"{path}[{i}]")
    if isinstance(v, dict):
        props = s.get("properties", {})
        for k in s.get("required", []):
            if k not in v:
                raise SchemaError(f"{path}: missing {k}")
        if s.get("additionalProperties") is False:
            extra = set(v) - set(props)
            if extra:
                raise SchemaError(f"{path}: unexpected {sorted(extra)[0]}")
        for k, sub in props.items():
            if k in v:
                _check(v[k], sub, f"{path}.{k}")
    if "oneOf" in s:
        hits = 0
        for sub in s["oneOf"]:
            try:
                _check(v, sub, path)
                hits += 1
            except SchemaError:
                pass
        if hits != 1:
            raise SchemaError(f"{path}: matches {hits} of the allowed shapes, need exactly 1")
    for sub in s.get("allOf", []):
        cond = sub.get("if")
        if cond is not None:
            try:
                _check(v, cond, path)
            except SchemaError:
                continue
            _check(v, sub.get("then", {}), path)
        else:
            _check(v, sub, path)


def validate(name: str, obj: Any) -> None:
    """Raise SchemaError if `obj` does not match protocol/schemas/<name>.json."""
    _check(obj, _load(name), name)
