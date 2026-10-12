"""Object selection transport and explicit projection adapter for older Mods.

Native object_read.v1 owns selection execution and cursor identity. Older query
connections retain a separate, honestly labelled projection: its original
source bounds and coverage survive, and saved DTO pages never pretend to be
fresh game observations.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import uuid

from .object_read_contract import OPERATORS, VERSION, canonical, signature, tool_schema, validate_request
from .object_read_delivery import ObjectReadDelivery
from .object_read_results import ObjectReadResultAssembly
from .query_surface import SurfaceError
from .situation_history import SituationHistory
from .tool_contracts import ToolExecutionError


def negotiated(directory):
    value = directory.get("object_read")
    return value if isinstance(value, dict) and value.get("version") == VERSION and value.get("operation") == "read_objects" else None


def legacy_directory(surface):
    """Derive the old projection's vocabulary from its actual field directory."""
    mapping = {"game": "game", "maps": "maps", "colony": "colony", "pawns": "pawns",
               "colonists": "spawned_free_colonists", "things": "things", "defs": "thing_defs"}
    roots = {kind: surface.roots[source]["type"] for kind, source in mapping.items() if source in surface.roots}
    # Some supported historical directories only expose a single Pawn root.
    if "pawns" not in roots and "pawn" in surface.roots:
        roots["pawns"] = surface.roots["pawn"]["type"]
    types = {}
    for name, fields in surface.index.items():
        types[name] = {}
        for field, declaration in fields.items():
            token = declaration.split(" ")[0]
            child = token.strip("[]!")
            types[name][field] = {"type": child, "scalar": child not in surface.index,
                                 "collection": token.startswith("[")}
    for name in set(roots.values()):
        types[name]["identity"] = {"type": name + "Identity", "scalar": False, "collection": False}
        types[name + "Identity"] = {key: copy.deepcopy(spec) for key, spec in types[name].items() if spec.get("scalar")}
    for name, fields in types.items():
        fields["self"] = {"type": name, "scalar": False, "collection": False}
    return {"object_read": {"version": "query_projection", "roots": roots, "types": types}}


class ObjectReading:
    def __init__(self, interaction):
        self.interaction = interaction
        self.capability = negotiated(interaction.setup.directory)
        self.directory = ({"object_read": self.capability} if self.capability else legacy_directory(interaction.surface))
        if not self.capability:
            source = self.directory["object_read"]
            if getattr(interaction, "object_contract", None):
                from .object_tools import STATE_FACETS
                facets = interaction.object_contract.get("parameters", {}).get("properties", {}).get("facets", {}).get("items", {}).get("enum", [])
                if not facets:
                    facets = STATE_FACETS
                source["types"]["LegacyState"] = {name: {"type": "LegacyState_" + name, "scalar": False, "collection": False} for name in facets}
                for owner in set(source["roots"].values()):
                    if owner in ("Pawn", "Thing"):
                        source["types"][owner]["state"] = {"type": "LegacyState", "scalar": False, "collection": False}
            if "read_situation" in interaction.original:
                source["roots"]["situation"] = "LegacySituation"
                source["types"]["LegacySituation"] = {
                    name: {"type": "LegacySituation_" + name, "scalar": False, "collection": name != "clock"}
                    for name in ("clock", "conditions", "events", "alerts", "threat_groups", "comparison")}
        self.compat = None if self.capability else QueryProjection(interaction, self.directory["object_read"])
        self.lease_id = getattr(getattr(interaction.session, "ledger", None), "run_id", None) or uuid.uuid4().hex
        self.closed = False
        self.last_situation_view = None
        self.cursor_requests = {}
        self.cursor_read_ids = {}
        self.result_assembly = ObjectReadResultAssembly()
        self.delivery = ObjectReadDelivery()
        self.situation_history = SituationHistory()
        self.comparison_history_loaded = False

    def _definition_conditions(self, request):
        resolver_factory = getattr(self.interaction, "_def_resolver", None)
        resolver = resolver_factory() if resolver_factory else None
        resolutions = []
        if resolver is None:
            return request, resolutions
        request = copy.deepcopy(request)
        where = request["target"].get("where", {})
        nested = where.get("definition")
        located = [(column, where.get(column, {})) for column in ("definition", "definition.def_name", "def_name")]
        if isinstance(nested, dict) and isinstance(nested.get("def_name"), dict):
            located.append(("definition.def_name", nested["def_name"]))
        for column, condition in located:
            for op in ("eq", "in"):
                if op not in condition:
                    continue
                names = condition[op] if op == "in" else [condition[op]]
                normalized = []
                for name in names:
                    if not isinstance(name, str):
                        normalized.append(name)
                        continue
                    advice = resolver.normalize(name)
                    selected = advice.get("auto_selection") if advice.get("state") == "unique" else None
                    normalized.append(selected or name)
                    if selected and selected != name:
                        resolutions.append({"column": column, "requested": name, "resolved": selected, "basis": copy.deepcopy(advice)})
                condition[op] = normalized if op == "in" else normalized[0]
        return request, resolutions

    def schema(self):
        return tool_schema(self.directory)

    async def read(self, arguments):
        request = validate_request(arguments)
        if self.closed:
            raise ToolExecutionError("cursor_expired", "This consultation's object reader has closed.")
        continuation_only = set(request) == {"page"}
        cursor = request.get("page", {}).get("cursor")
        if continuation_only and cursor in self.delivery.pages:
            result = self.delivery.resume(cursor, getattr(self.interaction, "sample_identity", None))
            self._remember_cursors(result, self.cursor_requests[cursor], self.cursor_read_ids.get(cursor))
            return result
        if continuation_only:
            bound = self.cursor_requests.get(cursor)
            if bound is None:
                raise ToolExecutionError("cursor_expired", "This consultation has no such object cursor")
            effective_request = copy.deepcopy(bound)
        else:
            effective_request = request
        read_id = None
        if effective_request["target"]["kind"] == "situation":
            tokens = []

            def collect(value):
                if isinstance(value, dict):
                    if isinstance(value.get("cursor"), str):
                        tokens.append(value["cursor"])
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)

            collect(request)
            owners = {self.cursor_read_ids[token] for token in tokens if token in self.cursor_read_ids}
            if len(owners) > 1:
                raise ToolExecutionError("cursor_mismatch", "Situation cursors belong to different observations")
            read_id = next(iter(owners)) if owners else self.situation_history.begin()
        if self.capability:
            operation = self.capability["operation"]
            if operation not in self.interaction.original:
                raise ToolExecutionError("object_read_unavailable", "The advertised native object reader has no handler.")
            if continuation_only:
                resolutions = []
            else:
                request, resolutions = self._definition_conditions(request)
                effective_request = request
            native = await self.interaction.execute_native({**request, "lease_id": self.lease_id}, tool_name=operation)
            if not isinstance(native.get("data"), dict):
                raise ToolExecutionError("invalid_object_result", "Native object reader returned no object packet.")
            result = copy.deepcopy(native["data"])
            if "fragment" in result:
                result = await self._fragments(request, native)
            else:
                result["source_observation_ids"] = [native["observation_id"]] if native.get("observation_id") else []
            result["observation_id"] = native.get("observation_id")
            result.setdefault("status", native.get("status", "ok"))
            result.setdefault("issues", copy.deepcopy(native.get("issues", [])))
            result.setdefault("source_granularity", "native_selection")
            if resolutions:
                result["name_resolution"] = resolutions
            self._remember_cursors(result, effective_request, read_id)
            if effective_request["target"]["kind"] == "situation":
                await self._comparison(effective_request, result, native, read_id, canonical(tokens))
            self._meanings(result, effective_request)
            result = self.delivery.deliver(result, getattr(self.interaction, "sample_identity", None))
            self._remember_cursors(result, effective_request, read_id)
            return result
        if continuation_only and cursor in self.delivery.legacy_pages:
            result = self.delivery.resume_legacy(cursor, getattr(self.interaction, "sample_identity", None))
        elif continuation_only:
            result = self.compat.resume_cursor(request["page"])
        else:
            result = await self.compat.read(request)
        if not (continuation_only and cursor in self.delivery.legacy_pages):
            result = self.delivery.coordinate_legacy(result, self.compat.resume_cursor, getattr(self.interaction, "sample_identity", None))
        self._remember_cursors(result, effective_request)
        self._meanings(result, effective_request)
        result = self.delivery.deliver(result, getattr(self.interaction, "sample_identity", None))
        self._remember_cursors(result, effective_request)
        return result

    def _remember_cursors(self, result, request, read_id=None):
        """Keep semantic binding locally; the native cursor remains authoritative."""
        def walk(value):
            if isinstance(value, dict):
                token = value.get("cursor")
                if isinstance(token, str) and token:
                    self.cursor_requests[token] = copy.deepcopy(request)
                    if read_id is not None:
                        self.cursor_read_ids[token] = read_id
                for child in list(value.values()):
                    if child is not value.get("resume"):
                        walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(result)
        page = result.get("page", {})
        if page.get("cursor"):
            result["resume"] = {"page": {"cursor": page["cursor"]}}

    def _meanings(self, result, request):
        """Publish native meanings only for the actually delivered fields,
        once per type/field in this read. Never duplicate the full directory."""
        if isinstance(result.get("semantic_rules"), dict):
            return  # The native reader budgets and continues these explanations.
        notes = getattr(getattr(self.interaction, "surface", None), "notes", {})
        types = self.directory["object_read"].get("types", {})
        root_type = self.directory["object_read"].get("roots", {}).get(request["target"]["kind"])
        meanings = {}

        def add(owner, field):
            text = notes.get(owner, {}).get(field)
            if isinstance(text, str) and text:
                meanings[owner + "." + field] = text

        def visit(row, owner):
            owner = row.get("anchor", {}).get("type", owner)
            for reading in row.get("readings", []):
                field = reading.get("field")
                spec = types.get(owner, {}).get(field, {})
                if reading.get("rows"):
                    add(owner, field)
                if spec.get("scalar"):
                    continue
                child_type = spec.get("type")
                for child in reading.get("rows", []):
                    for column in child.get("values", {}):
                        add(child_type, column)
                    visit(child, child_type)

        for row in result.get("rows", []):
            visit(row, root_type)
        if meanings:
            result["field_semantics"] = {**result.get("field_semantics", {}), **meanings}

    async def _fragments(self, request, native):
        chunks = []
        offset = 0
        total = None
        cursors = set()
        observations = []
        while True:
            if native.get("observation_id"):
                observations.append(native["observation_id"])
            fragment = native.get("data", {}).get("fragment")
            if not isinstance(fragment, dict) or fragment.get("encoding") != "json" or not isinstance(fragment.get("text"), str):
                raise ToolExecutionError("invalid_object_fragment", "Object fragment is not a JSON text segment")
            if type(fragment.get("offset")) is not int or fragment["offset"] != offset or type(fragment.get("total_chars")) is not int:
                raise ToolExecutionError("invalid_object_fragment", "Object fragment offsets are discontinuous")
            if total is None:
                total = fragment["total_chars"]
            if total != fragment["total_chars"] or total < 0:
                raise ToolExecutionError("invalid_object_fragment", "Object fragment length changed during continuation")
            text = fragment["text"]
            # The native .NET offsets are UTF-16 code units, including a pair
            # for non-BMP characters; a fragment may split such a pair.
            width = len(text.encode("utf-16-le", errors="surrogatepass")) // 2
            if width == 0 and offset < total or offset + width > total:
                raise ToolExecutionError("invalid_object_fragment", "Object fragment did not advance within its declared length")
            chunks.append(text)
            offset += width
            page = fragment.get("page", {})
            if not page.get("has_more"):
                if offset != total:
                    raise ToolExecutionError("invalid_object_fragment", "Object JSON ended before the declared length")
                break
            cursor = page.get("cursor")
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ToolExecutionError("invalid_object_fragment", "Object fragment has no advancing cursor")
            cursors.add(cursor)
            follow = copy.deepcopy(request)
            follow["page"] = {**follow.get("page", {}), "cursor": cursor}
            native = await self.interaction.execute_native({**follow, "lease_id": self.lease_id}, tool_name=self.capability["operation"])
        try:
            text = "".join(chunks).encode("utf-16-le", errors="surrogatepass").decode("utf-16-le")
            result = json.loads(text)
        except (ValueError, UnicodeError) as error:
            raise ToolExecutionError("invalid_object_fragment", "Complete object fragments contain invalid JSON") from error
        if not isinstance(result, dict):
            raise ToolExecutionError("invalid_object_fragment", "Complete object JSON is not a result object")
        result["source_observation_ids"] = observations
        return result

    async def _comparison(self, request, result, native, read_id, page_identity):
        from ..workspace.situation import saved_board
        from .situation import SECTIONS

        source_ids = copy.deepcopy(result.get("source_observation_ids", []))
        if not source_ids:
            return
        originals = getattr(self.interaction.session, "observations", [])
        original = next((observation for observation in originals if observation.get("observation_id") == source_ids[0]), {})
        assembled = self.result_assembly.observe(result)
        wrapper = {"request": {"operation": "read_objects", "arguments": copy.deepcopy(request)},
                   "observation_id": native.get("observation_id"),
                   "received_at": original.get("received_at"),
                   "response": {"colony_id": native.get("colony_id"), "load_epoch": native.get("load_epoch"),
                                "payload": {"status": native.get("status", "ok"), "issues": native.get("issues", []), "data": assembled}},
                   "derived_from": copy.deepcopy(assembled.get("source_observation_ids", source_ids))}
        current = saved_board(wrapper)
        if current is None:
            return
        def read_scope(read):
            return {key: copy.deepcopy(read[key]) for key in ("field", "where", "columns") if key in read} | {
                "children": [read_scope(child) for child in read.get("children", [])]}
        section_scopes = {read["field"]: canonical({"target": request["target"], "where": read.get("where", {}),
                                                   "columns": read.get("columns", []),
                                                   "children": [read_scope(child) for child in read.get("children", [])]})
                          for read in request["select"] if read["field"] in SECTIONS}
        current["selection_scopes"] = section_scopes
        history = getattr(self.interaction.session, "situation_previous", None)
        if not self.comparison_history_loaded and history is not None:
            try:
                observation = await history(native)
                previous = saved_board(observation)
                if previous:
                    self.situation_history.restore(previous, observation)
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                self.situation_history.unavailable = True
        self.comparison_history_loaded = True
        comparison = self.situation_history.compare(read_id, current, native.get("observation_id"), page_identity, wrapper["derived_from"])
        for row in result.get("rows", []):
            for reading in row.get("readings", []):
                if reading.get("field") != "comparison":
                    continue
                at = reading.get("input_path", [])
                selected = request["select"][at[0]] if at and type(at[0]) is int and at[0] < len(request["select"]) else {}
                if not selected.get("columns"):
                    continue
                if reading.get("status") == "selection_error" or any(issue.get("code") != "plugin_derived_placeholder" for issue in reading.get("issues", [])):
                    continue
                if not reading.get("rows"):
                    reading["rows"] = [{"anchor": {"id": str(row.get("anchor", {}).get("id", "situation")) + "/comparison", "identity_scope": "snapshot"}, "values": {}}]
                predicate_values = {**comparison, **{column: comparison.get(column) for column in selected.get("where", {})}}
                matched = QueryProjection._predicate(predicate_values, selected.get("where", {}), primitive_membership=True)
                if matched is not True:
                    reading["rows"] = []
                for item in reading["rows"]:
                    item["values"] = {column: copy.deepcopy(comparison.get(column)) for column in selected["columns"]}
                reading["derived_from"] = copy.deepcopy(wrapper["derived_from"])
                reading["source_granularity"] = "saved_observation_comparison"
                reading["issues"] = []
                reading["status"] = "observed" if matched is not None else "membership_unknown"
                reading["coverage"] = {"source_complete": True, "predicate_complete": matched is not None, "fields_complete": matched is not None,
                                       "display_complete": True, "returned_count": len(reading["rows"])}
                reading["page"] = {"has_more": False, "returned_count": len(reading["rows"])}
        result["issues"] = [issue for issue in result.get("issues", []) if issue.get("code") != "plugin_derived_placeholder"]
        wrapper["selection_scopes"] = copy.deepcopy(section_scopes)
        wrapper["comparison"] = copy.deepcopy(comparison)
        wrapper["comparison_baselines"] = self.situation_history.export_baselines()
        wrapper["known_items"] = self.situation_history.export_known_items()
        wrapper["response"]["payload"]["data"] = assembled
        self.last_situation_view = wrapper
        views = getattr(self.interaction.session, "situation_views", None)
        if views is not None:
            views.append(copy.deepcopy(wrapper))

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        self.cursor_requests.clear()
        self.cursor_read_ids.clear()
        self.result_assembly.clear()
        self.delivery.clear()
        self.situation_history.clear()
        if self.compat:
            self.compat.pages.clear()
        release = self.interaction.original.get("release_object_reads")
        if release and self.capability:
            await release.handler({"lease_id": self.lease_id})


def _anchors(fields):
    return [name for name in ("id", "ref", "key", "def_name", "slot") if fields.get(name, {}).get("scalar")]


def _unavailable(value):
    return isinstance(value, dict) and (value.get("state") in ("unavailable", "unknown") or value.get("status") in ("unavailable", "unknown"))


def _scalar_equal(left, right):
    """JSON numeric equality excludes bool, without distinguishing 1 and1.0."""
    numeric = type(left) in (int, float) and type(right) in (int, float)
    return (numeric or type(left) is type(right)) and left == right


def _dotted(where, prefix=""):
    """The older query source names related fields by dotted path."""
    flat = {}
    for column, condition in where.items():
        if condition and not set(condition) & set(OPERATORS):
            flat.update(_dotted(condition, prefix + column + "."))
        else:
            flat[prefix + column] = condition
    return flat


def _intersect_condition(where, column, op, value):
    condition = where.setdefault(column, {})
    if op not in condition:
        condition[op] = value
    elif op == "in":
        condition[op] = [item for item in condition[op] if any(_scalar_equal(item, other) for other in value)]
    elif not _scalar_equal(condition[op], value):
        # Contradictory exact scope and explicit predicates select an empty
        # set. Never overwrite the caller's predicate with a broader default.
        condition.clear()
        condition["in"] = []


def _collection(value):
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        complete = value.get("state") == "complete" and value.get("has_more") is False
        return value["rows"], complete
    if isinstance(value, list):
        return value, True
    if value is None:
        return [], True
    return [value], not _unavailable(value)


class QueryProjection:
    """Selected old query DTOs, retaining its original 100-row source bound.

    This adapter does not claim getter-level selection for native world facets.
    Nor does it convert capped query sources into complete member snapshots.
    """

    def __init__(self, interaction, directory):
        self.interaction = interaction
        self.directory = directory
        self.types = directory["types"]
        self.pages = {}
        self.request_signature = None
        self.request = None
        self.source_scope = None
        self.source_sample = None
        self.serial = None

    def _error(self, path, field, message):
        return {"input_path": list(path), "field": field, "rows": [], "coverage": {"source": "not_read", "predicate": "not_read", "fields": "not_read"},
                "issues": [{"code": "selection_error", "message": message, "input_path": list(path)}],
                "page": {"has_more": False}}

    def _spec(self, owner, field):
        if field == "self" and owner in self.types:
            return {"type": owner, "scalar": False, "collection": False}
        return self.types.get(owner, {}).get(field)

    def _compile(self, owner, read):
        field = read["field"]
        spec = self._spec(owner, field)
        if spec is None:
            raise SurfaceError(f"Unknown field {owner}.{field} in this source directory")
        if spec["scalar"]:
            raise SurfaceError(f"{owner}.{field} is a value; select it in columns on self or its parent relationship")
        child = spec["type"]
        fields = self.types.get(child, {})
        columns = list(dict.fromkeys(read.get("columns", [])))
        for column in columns:
            if not fields.get(column, {}).get("scalar") and not child.startswith("Legacy"):
                raise SurfaceError(f"Unknown/non-scalar column {child}.{column}; use children for relationships")
        selected = {name: True for name in [*_anchors(fields), *columns]}
        for item in read.get("children", []):
            name = item["field"]
            if name in selected:
                raise SurfaceError(f"Duplicate child {child}.{name} needs an independent projection")
            selected[name] = self._compile(child, item)
        result = {"select": selected}
        where = copy.deepcopy(read.get("where", {}))
        for column in where:
            if not fields.get(column, {}).get("scalar"):
                raise SurfaceError(f"Unknown/non-scalar predicate column {child}.{column}")
        if field == "stats":
            names = where.get("def_name", {})
            exact = names.get("in", [names["eq"]] if "eq" in names else [])
            if not exact or len(exact) > 16 or any(not isinstance(name, str) for name in exact):
                raise SurfaceError("stats requires where.def_name eq/in with 1..16 exact names")
            declaration = self.interaction.surface.index.get(owner, {}).get(field, "")
            result["args"] = {"stat_names" if "stat_names" in declaration else "def_names": exact}
        if spec["collection"]:
            result["limit"] = 100
        if where:
            if not spec["collection"]:
                # Single-object predicates are applied after its selected DTO
                # is captured; the extra predicate columns remain in raw audit.
                for name in where:
                    if not fields.get(name, {}).get("scalar"):
                        raise SurfaceError(f"Unknown predicate column {child}.{name}")
                    selected[name] = True
            else:
                if any("has" in condition for condition in where.values()):
                    raise SurfaceError("This query source exposes no registered collection membership index")
                result["where"] = where
        return result

    def _register(self, kind, value):
        """Coarse world/board sources did not publish a field directory. Record
        the actual returned shape without claiming speculative fields exist."""
        records, _ = _collection(value)
        for record in records:
            if not isinstance(record, dict):
                continue
            fields = self.types.setdefault(kind, {})
            for name, item in record.items():
                existing = fields.get(name)
                if isinstance(item, list) and not item and existing is not None:
                    continue  # Empty data cannot redefine a registered element type.
                scalar = not isinstance(item, (dict, list)) or _unavailable(item) or isinstance(item, list) and bool(item) and all(not isinstance(child, (dict, list)) for child in item)
                child = kind + "_" + name
                fields[name] = {"type": "Scalar" if scalar else child, "scalar": scalar, "collection": isinstance(item, list)}
                if not scalar:
                    self._register(child, item)

    async def _state(self, records, request, read):
        ids = [record.get("id") for record in records if isinstance(record.get("id"), str)]
        if not ids:
            return
        actual_map = (self.source_scope or {}).get("map_id") or request["target"].get("scope", {}).get("map", "current")
        obj = {"kind": request["target"]["kind"], "ids": ids, "map": actual_map}
        if obj["kind"] not in ("pawns", "colonists", "things"):
            return
        args = {"object": obj, "aspects": ["state"], "detail": 100}
        selected = [item["field"] for item in read.get("children", []) if item["field"] in self.types.get("LegacyState", {})]
        if not selected and not read.get("columns") and not read.get("where"):
            for record in records:
                record["state"] = {}
            return
        if selected:
            args["focus"] = {"state": list(dict.fromkeys(selected))}
        context = request.get("context", {})
        for role, old in (("actor", "actor"), ("recipient", "user")):
            if context.get(role):
                args[old] = {"kind": "pawn", **context[role]}
        try:
            result = await self.interaction._read_object_compat(args)
        except (SurfaceError, ToolExecutionError) as error:
            for record in records:
                record["state"] = {"state": "unavailable", "reason": getattr(error, "code", "coarse_state_unavailable"), "message": str(error)}
            return
        by_id = {item.get("object", {}).get("id"): item for item in result.get("facts", [])}
        for record in records:
            fact = by_id.get(record.get("id"), {})
            if not fact:
                record["state"] = {"state": "unavailable", "reason": "coarse_state_not_returned"}
                continue
            state = fact.get("facts", {}).get("state", {})
            record["state"] = copy.deepcopy(state.get("facets", state))
            record["_state_sample"] = copy.deepcopy(fact.get("state_sample", {"observation_id": result.get("observation_id"), "atomic": False}))
            bed = record["state"].get("bed") if isinstance(record["state"], dict) else None
            # Older native state infers actor from user. It is raw source
            # behaviour, not the new public context contract. Keep base and
            # recipient-only predicates; never publish an inferred actor check.
            if isinstance(bed, dict) and not context.get("actor") and "is_valid_bed_for" in bed:
                bed["is_valid_bed_for"] = {"state": "unavailable", "reason": "actor_required"}
                if isinstance(bed.get("context"), dict) and "actor" in bed["context"]:
                    bed["context"]["actor"] = {"state": "unavailable", "reason": "actor_required"}
            self._register("LegacyState", record["state"])

    async def _situation(self, request):
        from ..workspace.situation import saved_board

        result = await self.interaction._look_situation({"object": {"kind": "situation", "map": request["target"].get("scope", {}).get("map", "current")}, "aspects": ["state"]})
        board = saved_board(self.interaction.session.observations[-1])
        if board is None:
            raise ToolExecutionError("invalid_object_result", "The coarse situation source has no saved native board")
        data = copy.deepcopy(board)
        data["comparison"] = result["comparison"]
        self._register("LegacySituation", data)
        self.source_scope = {"map": copy.deepcopy(data.get("map")), "target": copy.deepcopy(request["target"])}
        self.source_sample = copy.deepcopy(data.get("sample", {}))
        anchor = {"id": (data.get("map") or {}).get("id"), "type": "LegacySituation"}
        readings = []
        for index, read in enumerate(request["select"]):
            try:
                self._compile("LegacySituation", read)
                reading = self._project(data, "LegacySituation", read, (index,), [anchor])
                if read["field"] + "_unavailable" in data:
                    reading["coverage"]["source"] = "partial"
                readings.append(reading)
            except SurfaceError as error:
                readings.append(self._error((index,), read["field"], str(error)))
        return {"source_granularity": "coarse_situation", "observation_id": result.get("observation_id"), "status": result.get("status", "ok"),
                "scope": self.source_scope, "sample": self.source_sample, "rows": [{"anchor": anchor, "values": {}, "readings": readings}],
                "coverage": {"source": "complete" if result.get("status") == "ok" else "partial"}, "issues": copy.deepcopy(result.get("issues", [])),
                "page": {"returned": 1, "has_more": False}}

    def _variants(self, read):
        """Old query objects cannot contain duplicate child keys; split source
        projections instead of inventing an illegal public combination."""
        buckets = []
        for index, child in enumerate(read.get("children", [])):
            for variant in self._variants(child):
                variant = {**variant, "_input_index": index}
                bucket = next((items for items in buckets if all(item["field"] != variant["field"] for item in items)), None)
                if bucket is None:
                    bucket = []
                    buckets.append(bucket)
                bucket.append(variant)
        if not buckets:
            return [copy.deepcopy(read)]
        return [{**copy.deepcopy(read), "children": items} for items in buckets]

    def _root(self, target):
        kind = target["kind"]
        source = {"colonists": "spawned_free_colonists", "defs": "thing_defs"}.get(kind, kind)
        if source not in self.interaction.surface.roots:
            if kind == "pawns" and "pawn" in self.interaction.surface.roots:
                source = "pawn"
            else:
                raise SurfaceError(f"Root {kind} is not provided by this connection")
        scope = target.get("scope", {})
        args = {}
        available = self.interaction.surface.argument_schemas.get(source, {}).get("properties", {})
        if "map_id" in available:
            args["map_id"] = scope.get("map", "current")
        if "include_held" in available:
            args["include_held"] = scope.get("placement", "any") != "spawned"
        if "include_fogged" in available:
            args["include_fogged"] = scope.get("map_visibility", "all") == "all"
        if "visible_only" in available:
            args["visible_only"] = scope.get("map_visibility", "all") == "visible"
        where = _dotted(copy.deepcopy(target.get("where", {})))
        if source == "things" and "category" in where:
            for op, value in where.pop("category").items():
                _intersect_condition(where, "definition.category", op, value)
        if source == "maps" and scope.get("map", "current") not in ("all", "all_loaded"):
            selected_map = scope.get("map", "current")
            if selected_map == "current":
                _intersect_condition(where, "is_current", "eq", True)
            elif "ids" in available:
                args["ids"] = [selected_map]
            else:
                _intersect_condition(where, "id", "in", [selected_map])
        if scope.get("placement") == "held":
            _intersect_condition(where, "spawned", "eq", False)
        for column, condition in list(where.items()):
            if column in ("capabilities", "capability"):
                if set(condition) != {"has"} or "capabilities" not in available:
                    raise SurfaceError("This source requires a declared native capability membership index")
                args["capabilities"] = [condition["has"]]
                where.pop(column)
            if column in ("definition", "definition.def_name"):
                resolver = self.interaction._def_resolver()
                if resolver:
                    for op in ("eq", "in"):
                        if op not in condition:
                            continue
                        names = condition[op] if op == "in" else [condition[op]]
                        normalized = []
                        for name in names:
                            advice = resolver.normalize(name)
                            normalized.append(advice["auto_selection"] if advice.get("state") == "unique" else name)
                        condition[op] = normalized if op == "in" else normalized[0]
                if column == "definition":
                    where["definition.def_name"] = where.pop(column)
        if source == "pawn":
            ident = where.pop("id", {}).get("eq")
            if not isinstance(ident, str):
                raise SurfaceError("This older connection only exposes Pawn by one exact where.id.eq")
            args["id"] = ident
        return source, args, where

    def _anchor(self, value, kind, path):
        fields = self.types.get(kind, {})
        anchor = {key: copy.deepcopy(value[key]) for key in _anchors(fields) if key in value and not _unavailable(value[key])}
        anchor["type"] = kind
        if not any(key in anchor for key in ("id", "ref", "key", "def_name", "slot")):
            anchor["snapshot_local_id"] = self.serial + ":" + "/".join(map(str, path))
        return anchor

    def _page(self, rows, read, path, ancestors, coverage):
        size = read.get("page", {}).get("size", 20)
        result = {"returned": min(size, len(rows)), "has_more": len(rows) > size, "size": size}
        if len(rows) > size:
            token = uuid.uuid4().hex
            self.pages[token] = {"signature": self.request_signature, "rows": copy.deepcopy(rows), "offset": size,
                                 "size": size, "path": list(path), "ancestors": copy.deepcopy(ancestors),
                                 "field": read.get("field"), "coverage": copy.deepcopy(coverage),
                                 "scope": copy.deepcopy(self.source_scope), "sample": copy.deepcopy(self.source_sample),
                                 "identity": self.interaction.sample_identity}
            self.pages[token]["request"] = copy.deepcopy(self.request)
            result["cursor"] = token
        return copy.deepcopy(rows[:size]), result

    @staticmethod
    def _predicate(value, where, *, primitive_membership=False):
        unknown = False
        for column, conditions in where.items():
            if column not in value or _unavailable(value[column]):
                unknown = True
                continue
            actual = value[column]
            if conditions and not set(conditions) & set(OPERATORS):
                # Relationship column: its child-field predicates apply to the related record.
                nested = QueryProjection._predicate(actual, conditions, primitive_membership=primitive_membership) \
                    if isinstance(actual, dict) else None
                if nested is False:
                    return False
                unknown = unknown or nested is None
                continue
            for op, wanted in conditions.items():
                if op == "in":
                    match = any(_scalar_equal(actual, item) for item in wanted)
                elif op in ("eq", "ne"):
                    equal = _scalar_equal(actual, wanted)
                    match = equal if op == "eq" else not equal
                elif op == "has":
                    if not primitive_membership or not isinstance(actual, list):
                        return None  # Only a declared primitive source may evaluate it.
                    match = any(_scalar_equal(item, wanted) for item in actual)
                elif type(actual) not in (int, float) or type(actual) is bool:
                    unknown = True
                    continue
                else:
                    match = {"gt": actual > wanted, "gte": actual >= wanted,
                             "lt": actual < wanted, "lte": actual <= wanted}[op]
                if not match:
                    return False
        return None if unknown else True

    def _project(self, value, owner, read, path, ancestors):
        field = read["field"]
        spec = self._spec(owner, field)
        if not spec:
            return self._error(path, field, f"Unknown field {owner}.{field}")
        if field in ("identity", "self"):
            raw = value
        else:
            raw = value.get(field, {"state": "unavailable", "reason": "field_not_returned"})
        if spec["scalar"]:
            status = "unavailable" if _unavailable(raw) else "observed"
            matched = self._predicate({"value": raw}, read.get("where", {}))
            values = {} if "columns" in read and not read["columns"] else {"value": copy.deepcopy(raw)}
            rows = [{"anchor": {"type": spec["type"]}, "values": values}] if matched is True else []
            return {"input_path": list(path), "field": field, "rows": rows,
                    "coverage": {"source": status, "predicate": "complete" if matched is not None else "unknown", "fields": status}, "issues": [], "page": {"returned": len(rows), "has_more": False}}
        if _unavailable(raw):
            return {"input_path": list(path), "field": field, "rows": [],
                    "coverage": {"source": "unavailable", "predicate": "unknown", "fields": "unavailable"}, "issues": [copy.deepcopy(raw)], "page": {"returned": 0, "has_more": False}}
        records, complete = _collection(raw)
        previous_sample = self.source_sample
        if field == "state" and value.get("_state_sample"):
            self.source_sample = value["_state_sample"]
        projected = []
        predicate_complete = True
        for position, record in enumerate(records):
            if not isinstance(record, dict):
                complete = False
                continue
            matched = self._predicate(record, read.get("where", {}))
            if matched is None:
                predicate_complete = False
                continue
            if not matched:
                continue
            anchor = self._anchor(record, spec["type"], [*path, position])
            item = {"anchor": anchor, "sample": copy.deepcopy(self.source_sample), "values": {key: copy.deepcopy(record.get(key, {"state": "unavailable", "reason": "column_not_returned"}))
                                                  for key in dict.fromkeys(read.get("columns", []))}, "readings": []}
            for index, child in enumerate(read.get("children", [])):
                item["readings"].append(self._project(record, spec["type"], child, (*path, child.get("_input_index", index)), [*ancestors, anchor]))
            projected.append(item)
        coverage = {"source": "complete" if complete else "partial", "predicate": "complete" if predicate_complete else "unknown",
                    "fields": "complete" if all(not _unavailable(v) for row in projected for v in row["values"].values()) else "partial"}
        if raw is None:
            coverage["source"] = "not_applicable"
        rows, page = self._page(projected, read, path, ancestors, coverage)
        self.source_sample = previous_sample
        return {"input_path": list(path), "field": field, "rows": rows, "coverage": coverage, "issues": [], "page": page}

    async def read(self, request):
        if getattr(self.interaction.session, "invalidated_error", None):
            raise ToolExecutionError("world_changed", "The saved object projection belongs to a previous loaded world.")
        cursors = []

        def collect(reads, path=()):
            for index, read in enumerate(reads):
                at = (*path, index)
                if read.get("page", {}).get("cursor"):
                    cursors.append((read["page"]["cursor"], at))
                collect(read.get("children", []), at)

        collect(request["select"])
        if request.get("page", {}).get("cursor"):
            cursors.append((request["page"]["cursor"], ()))
        if cursors:
            return self._resume(request, cursors)
        self.request_signature = signature(request)
        self.request = copy.deepcopy(request)
        self.serial = uuid.uuid4().hex
        if request["target"]["kind"] == "situation":
            return await self._situation(request)
        source, args, where = self._root(request["target"])
        owner = self.interaction.surface.roots[source]["type"]
        selections = request["select"]
        jobs, errors = [], []
        for index, read in enumerate(selections):
            try:
                for variant in self._variants(read):
                    selected = None if variant["field"] == "state" and getattr(self.interaction, "object_contract", None) else self._compile(owner, variant)
                    fields = {key: True for key in _anchors(self.types.get(owner, {}))}
                    if read["field"] in ("identity", "self"):
                        fields.update(selected["select"])
                    elif selected is not None:
                        fields[read["field"]] = selected
                    jobs.append((index, fields, variant))
            except SurfaceError as error:
                errors.append(self._error((index,), read["field"], str(error)))
        if not selections:
            jobs.append((None, {key: True for key in _anchors(self.types.get(owner, {}))}, None))
        packet = {"source_granularity": "query_projection", "status": "ok", "scope": {"target": copy.deepcopy(request["target"])},
                  "sample": {"atomic": False}, "rows": [], "issues": [], "selection_errors": errors,
                  "coverage": {"source": "not_read", "predicate": "not_read", "fields": "not_read"}}
        rows_by_anchor = {}
        source_complete = True
        for start in range(0, len(jobs), 8):
            chunk = jobs[start:start + 8]
            queries = []
            for _, fields, _ in chunk:
                query = {"from": source, "args": args, "select": fields, "limit": 100}
                if where:
                    query["where"] = where
                queries.append(query)
            native = await self.interaction.execute_native({"requests": queries})
            packet["observation_id"] = native.get("observation_id")
            self.source_sample = {"observation_id": native.get("observation_id"), "load_epoch": native.get("load_epoch"), "atomic": False}
            results = native.get("data", {}).get("results", [])
            if len(results) != len(chunk):
                raise ToolExecutionError("invalid_object_result", "Query projection result count does not match its requests")
            for (index, _, variant), result in zip(chunk, results):
                self.source_scope = copy.deepcopy(result.get("scope", packet["scope"]))
                raw_rows, complete = _collection(result)
                source_complete &= complete
                if variant is not None and variant["field"] == "state" and getattr(self.interaction, "object_contract", None):
                    await self._state(raw_rows, request, variant)
                for position, record in enumerate(raw_rows):
                    anchor = self._anchor(record, owner, [position])
                    key = canonical(anchor)
                    row = rows_by_anchor.setdefault(key, {"anchor": anchor, "values": {}, "readings": []})
                    if index is not None:
                        reading = self._project(record, owner, variant, (index,), [anchor])
                        reading["observation_id"] = native.get("observation_id")
                        row["readings"].append(reading)
                packet["issues"].extend(copy.deepcopy(native.get("issues", [])))
        rows = list(rows_by_anchor.values())
        for row in rows:
            row["readings"].extend(copy.deepcopy(errors))
            row["readings"].sort(key=lambda item: item["input_path"])
        packet["scope"] = copy.deepcopy(self.source_scope or packet["scope"])
        packet["sample"] = copy.deepcopy(self.source_sample or packet["sample"])
        def incomplete(reading):
            coverage = reading.get("coverage", {})
            return (bool(reading.get("issues")) or coverage.get("fields") in ("partial", "unavailable", "not_read")
                    or coverage.get("predicate") == "unknown"
                    or any(incomplete(child) for row in reading.get("rows", []) for child in row.get("readings", [])))
        fields_complete = not errors and not any(incomplete(reading) for row in rows for reading in row["readings"])
        packet["coverage"] = {"source": "complete" if source_complete and jobs else "partial",
                              "predicate": "complete" if source_complete and jobs else "unknown", "fields": "complete" if fields_complete else "partial"}
        packet["rows"], packet["page"] = self._page(rows, request, (), [], packet["coverage"])
        if not source_complete:
            packet["status"] = "partial"
            packet["issues"].append({"code": "coarse_source_partial", "message": "The older query source did not fully capture these records. Saved DTO pages preserve its coverage; they do not complete or refresh that source."})
        if not fields_complete:
            packet["status"] = "partial"
        return packet

    def resume_cursor(self, page):
        token = page["cursor"]
        saved = self.pages.get(token)
        if saved is None:
            raise ToolExecutionError("cursor_expired", "This consultation has no such object cursor")
        return self._resume(None, [(token, tuple(saved["path"]))])

    def _resume(self, request, cursors):
        packet = {"source_granularity": "query_projection", "status": "ok", "continuation_only": True,
                  "rows": [], "issues": [], "selected_sources": [],
                  "meaning": "Only cursor-bound sources are resumed. Sibling selections are not executed. Saved DTO values retain their original samples."}
        roots = {}
        for token, path in cursors:
            saved = self.pages.get(token)
            if saved is None:
                raise ToolExecutionError("cursor_expired", "This consultation has no such object cursor")
            if request is not None and saved["signature"] != signature(request) or saved["path"] != list(path):
                raise SurfaceError("Cursor selection/ancestor does not match this request")
            if saved["identity"] != self.interaction.sample_identity:
                raise ToolExecutionError("cursor_expired", "The cursor belongs to a different loaded world")
            offset, size = saved["offset"], saved["size"]
            rows = copy.deepcopy(saved["rows"][offset:offset + size])
            page = {"returned": len(rows), "has_more": offset + size < len(saved["rows"]), "size": size}
            if page["has_more"]:
                next_token = uuid.uuid4().hex
                self.pages[next_token] = {**saved, "offset": offset + size}
                page["cursor"] = next_token
            packet["scope"] = copy.deepcopy(saved["scope"])
            packet["sample"] = copy.deepcopy(saved["sample"])
            packet["selected_sources"].append({"input_path": list(path), "ancestors": copy.deepcopy(saved["ancestors"])})
            if not path:
                packet["rows"].extend(rows)
                packet["page"] = page
                packet["coverage"] = copy.deepcopy(saved["coverage"])
                continue
            ancestors = saved["ancestors"]
            root_key = canonical(ancestors[0])
            current = roots.setdefault(root_key, {"anchor": copy.deepcopy(ancestors[0]), "values": {}, "readings": []})
            reads = (request if request is not None else saved["request"])["select"]
            for depth, index in enumerate(path):
                field = reads[index]["field"]
                item = next((r for r in current["readings"] if r["input_path"] == list(path[:depth + 1])), None)
                if item is None:
                    item = {"input_path": list(path[:depth + 1]), "field": field, "rows": [], "issues": [], "coverage": {"source": "ancestor_anchor_only"}}
                    current["readings"].append(item)
                if depth == len(path) - 1:
                    item.update(rows=rows, page=page, coverage=copy.deepcopy(saved["coverage"]))
                else:
                    anchor = ancestors[depth + 1]
                    child = next((r for r in item["rows"] if r["anchor"] == anchor), None)
                    if child is None:
                        child = {"anchor": copy.deepcopy(anchor), "values": {}, "readings": []}
                        item["rows"].append(child)
                    current = child
                    reads = reads[index].get("children", [])
        packet["rows"].extend(roots.values())
        return packet
