"""Production interaction that exposes look/count instead of the raw query tool.

The Mod-owned directory stays behind the semantic vocabulary. The model does
not see the native query backend. Unsupported directories fail before dispatch.

Results use request-scoped ordinary JSON with scope, coverage, unknown states
and native semantics. Same-read identities may refer to their retained native
ID; there is no numbered fact vocabulary. Raw evidence remains in the audit.

The runtime uses this single interaction's prompt, tools, submission, snapshot
and finalizing state.
"""

from __future__ import annotations

import copy
from typing import Any

from ..delivery.research_surface import ReadOnlyResearchSurface
from ...tool_contracts import PUBLIC_QUERY_VERSION, WORLD_OBSERVATION_VERSION, ToolExecutionError, ToolSpec
from .activity import make_activity_tool
from .object_tools import ObjectReader, extend_look_schema, observed_id_reference, state_contract
from .query_surface import Surface, SurfaceError
from .semantic_surface import (
    DEF_KINDS,
    THING_KINDS,
    apply_lookup,
    capability_values,
    interpret_lookup,
    lookup_spec,
    normalize_object,
    pack,
    plan_count,
    plan_look,
    render,
    render_blocked,
    tool_schemas,
    validate_aspects,
    validate_by,
    validate_detail,
    validate_metric,
)
from .world_order import order_world_fields

# Only this explicitly approved non-observation tool survives the adapter.
PASSTHROUGH_TOOLS = frozenset({"wiki", "inspect_actions", "inspect_threats"})



def _with_field_semantics(result):
    """Annotate observed measures in place within the projection, never raw evidence."""
    result = copy.deepcopy(result)
    for item in result.get("facts", []):
        facts = item.get("facts", {})
        summary = facts.get("health", {}).get("summary")
        if isinstance(summary, dict) and summary.get("state") != "unavailable":
            meanings = {
                "health_percent": {
                    "unit": "fraction",
                    "meaning": "Overall game health summary, not blood volume or proof of no injuries.",
                },
                "bleed_rate": {
                    "unit": "blood_loss_fraction_per_game_day",
                    "ticks_per_game_day": 60000,
                    "meaning": "Multiply by 100 for percentage points of blood loss per game day; not per second or an ETA.",
                },
                "pain": {
                    "unit": "fraction",
                    "meaning": "Game pain total, separate from injury severity and blood loss.",
                },
            }
            summary["field_semantics"] = {key: value for key, value in meanings.items() if key in summary}
        traits = facts.get("profile", {}).get("traits")
        if isinstance(traits, dict) and traits.get("state") != "unavailable":
            traits["field_semantics"] = {
                "degree": "Trait variant code, not a numeric speed or stat bonus.",
                "description": "Game-generated trait tooltip, including effects when available. Respect suppressed; a trait effect is not the pawn's final stat value.",
            }
    return result




def _compile_supported_surface(setup):
    directory = getattr(setup, "directory", None)
    tools = {getattr(tool, "name", None): tool for tool in setup.tools}
    required = {"query", "survey_space", "inspect_route", "inspect_state"}
    if (
        not isinstance(directory, dict)
        or directory.get("public_query_version") != PUBLIC_QUERY_VERSION
        or not required <= tools.keys()
        or state_contract(directory) is None
    ):
        raise ToolExecutionError(
            "semantic_surface_unavailable",
            "Current consultation requires public_query.v2 and world_observation.v2.3 with complete native state; update the RimWorld Mod.",
            recoverable=False,
        )
    try:
        surface = Surface(directory)
        surface.add_query_schema(tools["query"].parameters)
    except (KeyError, TypeError, ValueError) as error:
        raise ToolExecutionError("semantic_surface_unavailable", "The current public query directory cannot be compiled.", recoverable=False) from error
    return surface


def semantic_surface_available(setup: Any) -> bool:
    try:
        _compile_supported_surface(setup)
    except ToolExecutionError:
        return False
    return True


class SemanticInteraction(ReadOnlyResearchSurface):
    """Expose look/count and project native results as ordinary JSON."""

    def __init__(self, setup: Any, session: Any, update: Any) -> None:
        super().__init__(setup, session, update)
        self.original = {tool.name: tool for tool in setup.tools}
        self.surface = _compile_supported_surface(setup)
        self.sample_identity: tuple[Any, Any, Any] | None = None
        directory = setup.directory if isinstance(getattr(setup, "directory", None), dict) else {}
        self.object_contract = state_contract(directory)
        self.object_reader = ObjectReader(self)
        self._def_name_resolver = None

    # -- runtime contract -------------------------------------------------

    def read_tools(self) -> tuple[Any, ...]:
        handlers = {"look": self.look, "count": self.count}
        specs = []
        semantic_specs = tool_schemas(self.setup.directory)
        semantic_specs = [
            extend_look_schema(spec, self.object_contract) if spec["name"] == "look" else spec
            for spec in semantic_specs
        ]
        for spec in self.setup.directory["world_observation"]["tools"]:
            name = spec["name"]
            if name == "inspect_state":
                continue

            async def observe(args, operation=name):
                return await self._world_read(args, operation)

            handlers[name] = observe
            semantic_specs.append(spec)

        # Activity compares paths in one batch; native readers stay internal.
        semantic_specs = [spec for spec in semantic_specs if spec["name"] != "inspect_route"]
        for tool in semantic_specs:
            if tool["name"] in ("look", "inspect_route", "survey_space"):
                from ..context.context_tools import configure_read_spec

                tool = configure_read_spec(tool)
            handler = handlers[tool["name"]]

            async def guarded(args, handler=handler):
                try:
                    return await handler(args)
                except (SurfaceError, KeyError, TypeError) as error:
                    raise ToolExecutionError("semantic_request_invalid", str(error)) from error

            specs.append(ToolSpec(tool["name"], tool["description"], tool["parameters"], guarded))
        passthrough = tuple(self.original[name] for name in sorted(PASSTHROUGH_TOOLS) if name in self.original)
        specs.append(make_activity_tool(self))
        return (*specs, *passthrough)

    def snapshot(self) -> dict[str, Any]:
        return dict(
            native_reads=len(self.session.observations),
            finalizing=self.finalizing,
        )

    async def _world_read(self, args: dict[str, Any], operation: str) -> dict[str, Any]:
        """Read one catalog-advertised world operation for semantic and activity adapters."""
        if operation not in self.original:
            raise ToolExecutionError("unsupported", f"The connected Mod does not support {operation}.")
        if operation in ("inspect_route", "survey_space"):
            args = dict(args)
            include_supplied = "include" in args
            include = args.pop("include", [])
            if not isinstance(include, list) or any(
                v not in ("geometry", "environment", "objects") for v in include
            ):
                raise SurfaceError("spatial include accepts geometry, environment and objects")
            if operation == "inspect_route":
                include_objects = args.pop("include_objects", False)
                if type(include_objects) is not bool:
                    raise SurfaceError("include_objects must be a boolean")
                # On-demand native generation: the route object listing the
                # model view would discard is not generated in the first
                # place. A continuation cursor pages object rows, so it keeps
                # objects requested. Environment, gases, threats, coverage
                # and route segments are internal forensics of every native
                # read and never depend on this flag.
                args["objects"] = bool(include_objects or "objects" in include or args.get("cursor"))
            elif include_supplied:
                # A survey without include retains the legacy discovery default.
                # Explicit native objects and object-page cursors keep precedence.
                if "objects" not in args:
                    args["objects"] = bool("objects" in include or args.get("cursor"))
        native = await self.execute_native(args, tool_name=operation)
        data = native.get("data")
        if not isinstance(data, dict):
            raise ToolExecutionError("invalid_world_result", f"The connected Mod returned no {operation} data.")
        ordered = order_world_fields(data)
        ordered["observation_id"] = native.get("observation_id")
        for key in ("colony_id", "load_epoch", "connection_epoch"):
            if native.get(key) is not None:
                ordered[key] = native[key]
        ordered["status"] = native.get("status", "ok")
        ordered["issues"] = copy.deepcopy(native.get("issues", []))
        ordered["source_kind"] = "native"
        return ordered

    # -- native read ------------------------------------------------------

    async def execute_native(self, request: dict[str, Any], *, tool_name: str = "query") -> dict[str, Any]:
        """Perform the only native read this adapter makes, and record it once.

        A loaded-world change is fatal rather than merged: facts from two loads
        are not the same colony state, so the run must restart instead of
        answering with a mixture.
        """
        if getattr(self.session, "invalidated_error", None):
            raise ToolExecutionError(
                "world_changed", "The loaded world changed; start a new consultation.", recoverable=False
            )
        observation = await self.original[tool_name].handler(request)
        value = observation.to_dict()
        response = value["response"]
        payload = response["payload"]
        identity = (response["colony_id"], response["load_epoch"], response.get("connection_epoch"))
        expected_colony = getattr(self.session, "colony_id", None)
        if (expected_colony is not None and response["colony_id"] != expected_colony) or (
            self.sample_identity is not None and identity != self.sample_identity
        ):
            self.session.invalidated_error = "world_changed"
            raise ToolExecutionError(
                "world_changed",
                "The loaded world changed; start a new consultation. Do not combine the old facts.",
                recoverable=False,
            )
        self.sample_identity = identity
        self.session.observations.append(value)
        if payload["status"] not in {"ok", "partial"}:
            raise ToolExecutionError(
                payload.get("error_code") or "query_failed",
                payload.get("message") or "Native read failed",
                details={"issues": payload.get("issues", [])},
            )
        return dict(
            observation_id=value["observation_id"],
            colony_id=response.get("colony_id"),
            load_epoch=response.get("load_epoch"),
            connection_epoch=response.get("connection_epoch"),
            status=payload["status"],
            data=pack(payload.get("data"), payload.get("issues", [])),
            issues=payload.get("issues", []),
            pages=[
                copy.deepcopy(row.get("page"))
                for row in (payload.get("data") or {}).get("results", [])
                if isinstance(row, dict)
            ],
        )

    def _def_resolver(self):
        """Optional deterministic Def-name recovery; None keeps lookups unchanged.

        A broken or unavailable index degrades to no resolver: recovery is an
        affordance, never a new failure path for ordinary lookups.
        """
        view = getattr(self.setup, "knowledge_view", None)
        if view is None:
            return None
        if self._def_name_resolver is None:
            from .def_names import DefNameResolver

            self._def_name_resolver = DefNameResolver.try_from_knowledge_view(view)
        return self._def_name_resolver or None

    async def _resolve(self, obj: dict[str, Any]):
        """Resolve a name or definition, or return candidates instead of a pick.

        An ambiguous or incomplete lookup must never silently choose one object:
        the caller gets the candidates and the scope that produced them.
        """
        if obj.get("kind") in ("pawn", "thing") and len(obj.get("names", [])) == 1:
            reference = {"kind": obj["kind"], "name": obj["names"][0]}
            if "map" in obj:
                reference["map"] = obj["map"]
            recovered = observed_id_reference(reference, self.session.observations)
            if "id" in recovered:
                obj = {**obj, "ids": [recovered["id"]]}
                obj.pop("names")
        resolver = self._def_resolver()
        name_resolution = None
        requested_term = None
        if resolver is not None and isinstance(obj.get("definition"), str) and obj["kind"] in DEF_KINDS + THING_KINDS:
            requested_term = obj["definition"]
            advice = resolver.normalize(requested_term)
            if advice["state"] == "unique" and advice["auto_selection"] != requested_term:
                # Deterministic word-order recovery only rewrites the requested
                # name; the native exact lookup below stays the arbiter of what
                # exists, so a stale index degrades to an ordinary miss.
                name_resolution = {
                    "requested": requested_term,
                    "resolved_as": advice["auto_selection"],
                    "method": "token_normalization",
                    "catalog": resolver.source,
                }
                obj = {**obj, "definition": advice["auto_selection"]}
        spec = lookup_spec(self.surface, obj)
        if spec is None:
            return obj, None, None
        native = await self.execute_native(spec["request"])
        resolution = interpret_lookup(spec, native["data"])
        if name_resolution is not None:
            resolution["name_resolution"] = name_resolution
        if resolver is not None and spec.get("kind") == "definition" and resolution["status"] == "missing_in_scope":
            discovery = resolver.discovery(requested_term or spec["names"][0], *spec["names"])
            if discovery is not None:
                resolution["discovery"] = discovery
        return apply_lookup(obj, spec, resolution), resolution, native

    # -- tools ------------------------------------------------------------

    async def look(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self.object_reader.look(args)

    def _validate_capabilities(self, obj):
        if "capabilities" in obj:
            allowed = capability_values(self.setup.directory)
            if not allowed or any(value not in allowed for value in obj["capabilities"]):
                raise SurfaceError("Unsupported capability selection on this connection")

    async def _look_legacy(self, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict) or "object" not in args or "aspects" not in args:
            raise SurfaceError("look requires object and aspects")
        original = normalize_object(args["object"])
        self._validate_capabilities(original)
        aspects = validate_aspects(original["kind"], args["aspects"])
        detail = validate_detail(args.get("detail", 0))
        obj, resolution, lookup_native = await self._resolve(original)
        if obj is None:
            return render_blocked("look", original, resolution, lookup_native)
        plan = plan_look(self.surface, obj, aspects, detail)
        offset = args.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 8192:
            raise SurfaceError("offset must be 0..8192")
        plan.request["offset"] = offset
        native = await self.execute_native(plan.request)
        result = _with_field_semantics(render(plan, native, resolution))
        result["page"] = copy.deepcopy((native.get("pages") or [None])[0] or {})
        return result

    async def count(self, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict) or "object" not in args or "metric" not in args:
            raise SurfaceError("count requires object and metric")
        original = normalize_object(args["object"])
        self._validate_capabilities(original)
        metric = validate_metric(original["kind"], args["metric"])
        by = validate_by(original["kind"], args.get("by"))
        detail = validate_detail(args.get("detail", 0))
        obj, resolution, lookup_native = await self._resolve(original)
        if obj is None:
            return render_blocked("count", original, resolution, lookup_native)
        plan = plan_count(self.surface, obj, metric, by, detail)
        native = await self.execute_native(plan.request)
        return render(plan, native, resolution)


def semantic_interaction_factory(setup: Any, session: Any, update: Any):
    """Require the look/count surface; never expose a fallback query vocabulary."""
    return SemanticInteraction(setup, session, update)


__all__ = [
    "SemanticInteraction",
    "semantic_interaction_factory",
    "semantic_surface_available",
]
