"""One model-facing selection grammar for negotiated object readers.

This module validates structure, not game facts. The connected source remains
the authority for field applicability, native units and predicate coverage.
"""

from __future__ import annotations

import copy
import json
import math

from .query_surface import SurfaceError

VERSION = "object_read.v1"
OPERATORS = ("eq", "ne", "in", "gt", "gte", "lt", "lte", "has")
READ_KEYS = frozenset({"field", "where", "columns", "children", "page"})


def parameters():
    scalar = {"type": ["string", "number", "boolean", "null"]}
    condition = {
        "type": "object", "additionalProperties": False,
        "properties": {op: ({"type": "array", "items": scalar} if op == "in" else scalar)
                       for op in OPERATORS}, "minProperties": 1,
    }
    where = {"type": "object", "additionalProperties": {"anyOf": [condition, {"$ref": "#/$defs/where"}]},
             "description": "Registered scalar/index columns only; a relationship column takes its child fields, e.g. definition:{def_name:{eq:...}}. Clauses AND. has uses a declared membership index. Unknown values never match false/null."}
    page = {"type": "object", "additionalProperties": False, "properties": {
        "size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        "cursor": {"type": "string", "minLength": 1},
    }}
    role = {"type": "object", "additionalProperties": False, "properties": {
        "id": {"type": "string", "minLength": 1},
        "name": {"type": "string", "minLength": 1},
        "map": {"type": "string", "minLength": 1},
    }, "oneOf": [{"required": ["id"], "not": {"required": ["name"]}},
                 {"required": ["name"], "not": {"required": ["id"]}}]}
    read = {"type": "object", "additionalProperties": False, "required": ["field"], "properties": {
        "field": {"type": "string", "minLength": 1},
        "where": {"$ref": "#/$defs/where"},
        "columns": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "children": {"type": "array", "items": {"$ref": "#/$defs/read"}},
        "page": {"$ref": "#/$defs/page"},
    }}
    return {"type": "object", "additionalProperties": False,
            "anyOf": [{"required": ["target", "select"]},
                      {"required": ["page"], "properties": {"page": {"required": ["cursor"]}},
                       "not": {"anyOf": [{"required": ["target"]}, {"required": ["select"]}, {"required": ["context"]}]}}],
            "properties": {
                "target": {"type": "object", "additionalProperties": False, "required": ["kind"], "properties": {
                    "kind": {"type": "string", "minLength": 1},
                    "scope": {"type": "object", "additionalProperties": False, "properties": {
                        "map": {"type": "string", "minLength": 1, "default": "current"},
                        "placement": {"enum": ["any", "spawned", "held"], "default": "any"},
                        "map_visibility": {"enum": ["all", "visible"], "default": "all"},
                    }}, "where": {"$ref": "#/$defs/where"},
                }},
                "select": {"type": "array", "items": {"$ref": "#/$defs/read"}},
                "page": {"$ref": "#/$defs/page"},
                "context": {"type": "object", "additionalProperties": False, "properties": {
                    name: {"anyOf": [{"$ref": "#/$defs/role"}, {"type": "null"}]}
                    for name in ("actor", "recipient")}},
            }, "$defs": {"read": read, "where": where, "page": page, "role": role}}


def _object(value, keys, path):
    if not isinstance(value, dict) or set(value) - set(keys):
        raise SurfaceError(f"{path} must be an object with only {','.join(sorted(keys))}")


def _name(value, path, *, direct=False):
    if not isinstance(value, str) or not value.strip() or direct and "." in value:
        raise SurfaceError(f"{path} must be a nonempty {'direct ' if direct else ''}name")


def _scalar(value, path):
    if value is not None and type(value) not in (str, bool, int, float):
        raise SurfaceError(f"{path} must be a scalar")
    if isinstance(value, float) and not math.isfinite(value):
        raise SurfaceError(f"{path} must be finite")


def validate_where(value, path):
    if not isinstance(value, dict):
        raise SurfaceError(f"{path} must be a column predicate object")
    for column, conditions in value.items():
        _name(column, path)
        if isinstance(conditions, dict) and conditions and not set(conditions) & set(OPERATORS):
            # A relationship column holds its child fields' predicates; the
            # connected source decides whether those names exist.
            validate_where(conditions, f"{path}.{column}")
            continue
        _object(conditions, OPERATORS, f"{path}.{column}")
        if not conditions:
            raise SurfaceError(f"{path}.{column} requires an operator")
        for op, operand in conditions.items():
            if op == "in":
                if not isinstance(operand, list):
                    raise SurfaceError(f"{path}.{column}.in must be an array")
                for item in operand:
                    _scalar(item, f"{path}.{column}.in")
            else:
                _scalar(operand, f"{path}.{column}.{op}")
            if op in ("gt", "gte", "lt", "lte") and type(operand) not in (int, float):
                raise SurfaceError(f"{path}.{column}.{op} requires a number")


def validate_page(value, path):
    _object(value, ("size", "cursor"), path)
    if "size" in value and (type(value["size"]) is not int or not 1 <= value["size"] <= 100):
        raise SurfaceError(f"{path}.size must be an integer 1..100")
    if "cursor" in value:
        _name(value["cursor"], path + ".cursor")


def validate_request(value):
    """Reject malformed structure before transport; never coerce old look keys."""
    _object(value, ("target", "select", "page", "context"), "look")
    if set(value) == {"page"}:
        validate_page(value["page"], "page")
        if "cursor" not in value["page"]:
            raise SurfaceError("A continuation-only look requires page.cursor")
        return copy.deepcopy(value)
    if "target" not in value or "select" not in value:
        raise SurfaceError("look requires target and select")
    target = value["target"]
    _object(target, ("kind", "scope", "where"), "target")
    _name(target.get("kind"), "target.kind")
    if "scope" in target:
        scope = target["scope"]
        _object(scope, ("map", "placement", "map_visibility"), "target.scope")
        if "map" in scope:
            _name(scope["map"], "target.scope.map")
        if scope.get("placement", "any") not in ("any", "spawned", "held"):
            raise SurfaceError("scope.placement must be any/spawned/held")
        if scope.get("map_visibility", "all") not in ("all", "visible"):
            raise SurfaceError("scope.map_visibility must be all/visible")
    if "where" in target:
        validate_where(target["where"], "target.where")

    def walk(reads, path, depth):
        if not isinstance(reads, list):
            raise SurfaceError(f"{path} must be an array")
        if depth > 12:
            raise SurfaceError("selection exceeds 12 relationship levels")
        for index, read in enumerate(reads):
            at = f"{path}[{index}]"
            _object(read, READ_KEYS, at)
            _name(read.get("field"), at + ".field", direct=True)
            if "columns" in read:
                if not isinstance(read["columns"], list):
                    raise SurfaceError(at + ".columns must be an array")
                for name in read["columns"]:
                    _name(name, at + ".columns", direct=True)
            if "where" in read:
                validate_where(read["where"], at + ".where")
            if "page" in read:
                validate_page(read["page"], at + ".page")
            if "children" in read:
                walk(read["children"], at + ".children", depth + 1)

    walk(value["select"], "select", 0)
    if "page" in value:
        validate_page(value["page"], "page")
    if "context" in value:
        _object(value["context"], ("actor", "recipient"), "context")
        for role, reference in value["context"].items():
            if reference is None:
                continue
            _object(reference, ("id", "name", "map"), "context." + role)
            if ("id" in reference) == ("name" in reference):
                raise SurfaceError(f"context.{role} requires exactly one id/name")
            for name, item in reference.items():
                _name(item, f"context.{role}.{name}")
    return copy.deepcopy(value)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def signature(value):
    """Cursor-free signature: a continuation identifies, never changes, a read."""
    clean = copy.deepcopy(value)

    def walk(node):
        if isinstance(node, dict):
            if "field" in node or "target" in node:
                node.setdefault("page", {"size": 20})
            if isinstance(node.get("page"), dict):
                node["page"].pop("cursor", None)
                node["page"].setdefault("size", 20)
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(clean)
    clean.setdefault("context", {})
    clean["context"] = {key: value for key, value in clean["context"].items() if value is not None}
    clean["target"].setdefault("scope", {})
    for key, value in {"map": "current", "placement": "any", "map_visibility": "all"}.items():
        clean["target"]["scope"].setdefault(key, value)
    return canonical(clean)


def tool_schema(directory):
    capability = directory.get("object_read", {})
    roots = capability.get("roots", {}) or {"pawns": "Pawn", "colonists": "Pawn", "things": "Thing", "maps": "Map", "defs": "ThingDef"}
    types = capability.get("types", {})
    catalogue = []
    for name, fields in types.items():
        if not isinstance(fields, dict):
            continue
        catalogue.append(name + "{" + ",".join(
            field + (":" + spec["type"] + ("[]" if spec.get("collection") else "") if isinstance(spec, dict) and not spec.get("scalar") else "")
            for field, spec in fields.items()) + "}")
    schema = parameters()
    schema["properties"]["target"]["properties"]["kind"]["enum"] = list(roots)
    return {"name": "look", "parameters": schema, "description": (
        "Read target/select with one recursive selection rule. field selects a relationship; columns select its direct values. "
        "Use field=self for root values. Relationships use children, including definition for an item's def_name. "
        "Omitted/empty columns and select read anchors only. where clauses AND; unknown is never false/null. "
        "Context actor acts, recipient receives; no inferred roles. "
        "Continue by copying resume, or look {page:{cursor}}; the cursor restores the bound selection and ancestors. "
        "page.size defaults20; one top-level cursor resumes the whole bound selection, including source and value tails. Scope defaults map=current, placement=any, map_visibility=all. "
        "Each page has its own sample; completed interval coverage is not one instant. "
        + "Roots " + ",".join(f"{kind}:{name}" for kind, name in roots.items()) + ". "
        + "Fields " + ";".join(catalogue)
    )}
