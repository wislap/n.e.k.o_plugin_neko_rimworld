"""On-demand colony domains using the existing native read and audit owner.

The Mod owns business facts and calculations. This adapter only validates the
small model vocabulary and translates selectors; it never scans the colony or
adds background reads to discover which domain a question might need.
"""

from __future__ import annotations

import copy

from ...tool_contracts import ToolSpec
from .query_surface import SurfaceError


VERSION = "colony_state.v1"
DOMAINS = {
    "world": ("inspect_planet", ("places",)),
    "world_objects": ("inspect_planet", ("identity", "location")),
    "caravans": ("inspect_planet", ("identity", "location", "route", "supplies")),
    "factions": ("inspect_factions", ("identity", "relations", "settlements")),
    "quests": ("inspect_quests", ("identity", "requirements", "rewards", "deadlines")),
    "traders": ("inspect_trade", ("identity", "stock")),
    "research": ("inspect_research", ("progress", "requirements", "unlocks")),
}
ALIASES = {"world_object": "world_objects", "caravan": "caravans", "faction": "factions", "quest": "quests", "trader": "traders"}
DEFAULTS = {"world": ["places"], "caravans": ["identity", "location", "route"], "factions": ["identity", "relations"], "research": ["progress"]}
OPERATIONS = frozenset({*(operation for operation, _ in DOMAINS.values()), "evaluate_journey", "evaluate_trade"})


def available_operations(directory):
    contract = directory.get("colony_state") if isinstance(directory, dict) else None
    if not isinstance(contract, dict) or contract.get("version") != VERSION:
        return frozenset()
    return frozenset(row["operation"] for row in contract.get("operations", []) if isinstance(row, dict) and row.get("operation") in OPERATIONS)


def colony_kind(value):
    return ALIASES.get(value, value) if isinstance(value, str) else None


def _strings():
    return {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "string", "minLength": 1}}


def _reference(*, map_allowed=False):
    return {
        "type": "object", "additionalProperties": False,
        "properties": {"id": {"type": "string", "minLength": 1}, "name": {"type": "string", "minLength": 1}, **({"map": {"type": "string"}} if map_allowed else {})},
        # oneOf already rejects the both-present case: it would match twice.
        "oneOf": [{"required": ["id"]}, {"required": ["name"]}],
    }


def extend_look_spec(spec, directory):
    operations = available_operations(directory)
    kinds = [kind for kind, (operation, _) in DOMAINS.items() if operation in operations]
    if not kinds:
        return
    domains = [label for operation, label in (
        ("inspect_planet", "world places/caravans"),
        ("inspect_factions", "faction relations"),
        ("inspect_quests", "quest requirements/rewards/deadlines"),
        ("inspect_trade", "merchant stock"),
        ("inspect_research", "research progress/prerequisites"),
    ) if operation in operations]
    spec["description"] += " Also read " + "; ".join(domains) + "."
    if "inspect_trade" in operations:
        spec["description"] += ' Merchant offer quantities come from object.kind="traders",aspects=["stock"]; count totals alone do not identify seller or ownership.'
    aliases = [alias for alias, kind in ALIASES.items() if kind in kinds]
    props = spec["parameters"]["properties"]
    selectors = {
        "kind": {"enum": kinds + aliases},
        "ids": _strings(), "names": _strings(),
        "id": {"type": "string", "minLength": 1}, "name": {"type": "string", "minLength": 1},
    }
    if "inspect_planet" in operations:
        selectors.update(category={"type": "string"}, near={"oneOf": [_reference(), {
            "type": "object", "additionalProperties": False, "required": ["kind"],
            "properties": {"kind": {"const": "map"}, "map": {"type": "string", "default": "current"}},
        }]})
    if "inspect_trade" in operations:
        selectors["map"] = {"type": "string", "default": "current"}
    props["object"]["oneOf"].append({"type": "object", "additionalProperties": False, "required": ["kind"], "properties": selectors})
    extra_aspects = list(dict.fromkeys(a for kind in kinds for a in DOMAINS[kind][1]))
    props["aspects"]["items"]["enum"] = list(dict.fromkeys(props["aspects"]["items"]["enum"] + extra_aspects))
    rules = "; ".join(kind + ": " + ",".join(DOMAINS[kind][1]) for kind in kinds)
    props["aspects"]["description"] += " Colony domains: " + rules + ". Read only requested aspects. Lists default to current/public objects; research to current projects. Names resolve directly, without an ID lookup. category/near apply to world/world_objects/caravans; map and focus.items to traders."
    props["limit"] = {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "Colony-domain row limit; use offset=page.next_offset to continue."}
    if "inspect_trade" in operations:
        props["focus"]["properties"]["items"] = _strings()
        props["focus"]["description"] += " focus.items selects native item Def names or labels in trader stock. For named/ID-selected trader stock, limit/offset page stock; continue with stock.page.next_offset using the same trader. Untargeted trader lists page partners."


def _list(value, name):
    if not isinstance(value, list) or not 1 <= len(value) <= 32 or any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in value):
        raise SurfaceError(name + " requires 1..32 nonempty names or IDs")
    return list(dict.fromkeys(value))


def _ref(value, name, *, map_allowed=False):
    if not isinstance(value, dict) or set(value) - ({"id", "name", "map"} if map_allowed else {"id", "name"}) or ("id" in value) == ("name" in value):
        raise SurfaceError(name + " requires exactly one id or name")
    for key, entry in value.items():
        if not isinstance(entry, str) or not entry.strip() or len(entry) > 256:
            raise SurfaceError(name + "." + key + " requires a nonempty string")
    return copy.deepcopy(value)


async def read_colony(interaction, args, kind):
    operation, supported = DOMAINS[kind]
    if operation not in available_operations(interaction.setup.directory):
        raise SurfaceError("The connected Mod does not support " + kind)
    if set(args) - {"object", "aspects", "detail", "offset", "limit", "focus"}:
        raise SurfaceError("Colony look accepts object, aspects, detail, offset, limit and trader focus.items")
    obj = args["object"]
    allowed = {"kind", "id", "ids", "name", "names"}
    if operation == "inspect_planet":
        allowed |= {"category", "near"}
    if kind == "traders":
        allowed.add("map")
    if set(obj) - allowed:
        raise SurfaceError(kind + " object does not accept " + ", ".join(sorted(set(obj) - allowed)))
    request = {}
    for singular, plural in (("id", "ids"), ("name", "names")):
        if singular in obj and plural in obj:
            raise SurfaceError("Use " + singular + " or " + plural + ", not both")
        if singular in obj or plural in obj:
            request[plural] = _list([obj[singular]] if singular in obj else obj[plural], plural)
    aspects = args.get("aspects", DEFAULTS.get(kind, ["identity"]))
    if not isinstance(aspects, list) or not aspects or any(a not in supported for a in aspects):
        raise SurfaceError(kind + " aspects: " + ", ".join(supported))
    request["aspects"] = list(dict.fromkeys(aspects))
    for key, default, low, high in (("limit", 20, 1, 100), ("offset", 0, 0, 8192), ("detail", 0, 0, 100)):
        value = args.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise SurfaceError(key + f" must be {low}..{high}")
        if key != "detail":
            request[key] = value
    for key in ("map", "category"):
        if key in obj:
            if not isinstance(obj[key], str) or not obj[key].strip():
                raise SurfaceError(key + " requires a nonempty string")
            request[key] = obj[key]
    if operation == "inspect_planet":
        request["kind"] = kind
        if "near" in obj:
            near = obj["near"]
            if isinstance(near, dict) and near.get("kind") == "map":
                if set(near) - {"kind", "map"} or not isinstance(near.get("map", "current"), str) or not near.get("map", "current").strip():
                    raise SurfaceError("near map requires kind=map and optional map")
                request["near"] = {"kind": "map", "map": near.get("map", "current")}
            else:
                request["near"] = _ref(near, "near")
    focus = args.get("focus", {})
    if not isinstance(focus, dict) or set(focus) - ({"items"} if kind == "traders" else set()):
        raise SurfaceError("Only traders accept focus.items for colony reads")
    if "items" in focus:
        if "stock" not in aspects:
            raise SurfaceError("focus.items requires stock aspect")
        request["items"] = _list(focus["items"], "focus.items")
    return await interaction._world_read(request, operation)


def computation_tools(interaction):
    """Only two public computations; native inspect operations stay internal."""
    operations = available_operations(interaction.setup.directory)
    result = []
    if "evaluate_journey" in operations:
        async def journey(args):
            if not isinstance(args, dict) or set(args) - {"caravan", "destinations", "include"}:
                raise SurfaceError("evaluate_journey accepts caravan, destinations, include")
            request = {"caravan": _ref(args.get("caravan"), "caravan")}
            destinations = args.get("destinations")
            if not isinstance(destinations, list) or not 1 <= len(destinations) <= 16:
                raise SurfaceError("destinations requires 1..16 named or ID references")
            request["destinations"] = [_ref(ref, "destination") for ref in destinations]
            include = args.get("include", [])
            if not isinstance(include, list) or any(v != "supplies" for v in include):
                raise SurfaceError("journey include accepts supplies")
            if include:
                request["include"] = list(dict.fromkeys(include))
            return await interaction._world_read(request, "evaluate_journey")

        result.append(ToolSpec("evaluate_journey", "Compare native travel estimates for an existing player caravan to every requested world destination. Use names directly; no preliminary directory read. include supplies only when needed. Returns assumptions, no orders.", {
            "type": "object", "additionalProperties": False, "required": ["caravan", "destinations"],
            "properties": {"caravan": _reference(), "destinations": {"type": "array", "minItems": 1, "maxItems": 16, "items": _reference()}, "include": {"type": "array", "uniqueItems": True, "items": {"enum": ["supplies"]}}},
        }, _guard(journey)))
    if "evaluate_trade" in operations:
        async def trade(args):
            if not isinstance(args, dict) or set(args) - {"trader", "negotiator", "buy", "sell"}:
                raise SurfaceError("evaluate_trade accepts trader, negotiator, buy and sell")
            request = {key: _ref(args.get(key), key, map_allowed=True) for key in ("trader", "negotiator")}
            for key in ("buy", "sell"):
                rows = args.get(key, [])
                if not isinstance(rows, list) or len(rows) > 32:
                    raise SurfaceError(key + " requires at most 32 item/quantity rows")
                for row in rows:
                    if not isinstance(row, dict) or set(row) != {"item", "quantity"} or not isinstance(row["item"], str) or not row["item"].strip() or type(row["quantity"]) is not int or row["quantity"] <= 0:
                        raise SurfaceError(key + " rows require item name and positive integer quantity")
                request[key] = copy.deepcopy(rows)
            if not request["buy"] and not request["sell"]:
                raise SurfaceError("Request at least one buy or sell item")
            return await interaction._world_read(request, "evaluate_trade")

        basket = {"type": "array", "maxItems": 32, "items": {"type": "object", "additionalProperties": False, "required": ["item", "quantity"], "properties": {"item": {"type": "string", "minLength": 1}, "quantity": {"type": "integer", "minimum": 1}}}}
        result.append(ToolSpec("evaluate_trade", "Quote a requested buy/sell basket from the player's perspective with an existing trader and negotiator. Names resolve directly. Returns native prices, currency, quantities, funds and conditions; performs no trade or automatic substitution.", {
            "type": "object", "additionalProperties": False, "required": ["trader", "negotiator"],
            "properties": {"trader": _reference(map_allowed=True), "negotiator": _reference(map_allowed=True), "buy": basket, "sell": copy.deepcopy(basket)},
            "anyOf": [{"required": ["buy"], "properties": {"buy": {"minItems": 1}}}, {"required": ["sell"], "properties": {"sell": {"minItems": 1}}}],
        }, _guard(trade)))
    return tuple(result)


def _guard(handler):
    async def guarded(args):
        from ...tool_contracts import ToolExecutionError
        try:
            return await handler(args)
        except (SurfaceError, KeyError, TypeError) as error:
            raise ToolExecutionError("semantic_request_invalid", str(error)) from error
    return guarded
