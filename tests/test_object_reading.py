"""Model-sent contract and real projection/continuation boundaries."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from plugin.plugins.neko_rimworld.agent.context_tools import configure_read_spec, model_tools
from plugin.plugins.neko_rimworld.agent.object_read_contract import parameters, signature, validate_request
from plugin.plugins.neko_rimworld.agent.object_reading import ObjectReading
from plugin.plugins.neko_rimworld.agent.query_surface import Surface, SurfaceError
from plugin.plugins.neko_rimworld.agent.tool_contracts import ToolExecutionError


def native_interaction(handler):
    capability = {"version": "object_read.v1", "operation": "read_objects", "parameters": parameters(),
                  "roots": {"pawns": "Pawn"}, "types": {"Pawn": {"health": {"type": "Health", "scalar": False, "collection": False}},
                                                               "Health": {"conditions": {"type": "Condition", "scalar": False, "collection": True}}}}
    released = []

    async def release(args):
        released.append(args)

    interaction = SimpleNamespace(setup=SimpleNamespace(directory={"object_read": capability}),
                                  original={"read_objects": object(), "release_object_reads": SimpleNamespace(handler=release)},
                                  session=SimpleNamespace(ledger=SimpleNamespace(run_id="test-consultation")))
    interaction.execute_native = handler
    return interaction, released


def test_model_sent_schema_is_one_recursive_grammar_and_no_old_rewrite():
    interaction, _ = native_interaction(None)
    reader = ObjectReading(interaction)
    spec = configure_read_spec(reader.schema())
    sent = model_tools([{"type": "function", "function": spec}])[0]["function"]
    assert set(sent["parameters"]["properties"]) == {"target", "select", "page", "context"}
    assert "aspects" not in json.dumps(sent)
    assert "focus" not in json.dumps(sent)
    assert "allOf" not in sent["parameters"]
    assert "Condition" in sent["description"]


@pytest.mark.parametrize("extra", [{"aspects": []}, {"focus": {}}, {"detail": 1}, {"include": []}, {"actor": {}}])
def test_old_public_options_are_not_silently_mapped(extra):
    with pytest.raises(SurfaceError):
        validate_request({"target": {"kind": "pawns"}, "select": [], **extra})


@pytest.mark.parametrize("read", [{"field": "health.conditions"}, {"field": "health", "columns": ["conditions.severity"]},
                                 {"field": "health", "where": {"pain": {"gt": True}}},
                                 {"field": "health", "page": {"size": True}},
                                 {"field": "health", "where": {"pain": {"in": "bad"}}}])
def test_invalid_shapes_do_not_reach_transport(read):
    with pytest.raises(SurfaceError):
        validate_request({"target": {"kind": "pawns"}, "select": [read]})


def test_relationship_where_takes_child_field_predicates():
    request = {"target": {"kind": "things", "where": {"definition": {"def_name": {"eq": "MedicineIndustrial"}}}}, "select": []}
    assert validate_request(request)["target"]["where"] == {"definition": {"def_name": {"eq": "MedicineIndustrial"}}}
    sent = json.dumps(parameters())
    assert "definition:{def_name:{eq:...}}" in sent


@pytest.mark.parametrize("where", [{"definition": {}}, {"definition": {"def_name": {}}},
                                   {"definition": {"def_name": {"like": "Med"}}},
                                   {"definition": {"def_name": "MedicineIndustrial"}}])
def test_relationship_where_still_requires_operator_leaves(where):
    with pytest.raises(SurfaceError):
        validate_request({"target": {"kind": "things", "where": where}, "select": []})


async def test_native_relationship_where_reaches_mod_unchanged():
    calls = []

    async def handler(arguments, *, tool_name):
        calls.append(arguments)
        return {"data": {"rows": []}, "observation_id": "o", "status": "ok", "issues": []}

    interaction, _ = native_interaction(handler)
    where = {"definition": {"def_name": {"eq": "MedicineIndustrial"}}}
    await ObjectReading(interaction).read({"target": {"kind": "pawns", "where": where}, "select": []})
    assert calls[0]["target"]["where"] == where


async def test_native_request_injects_private_lease_and_retains_every_fact():
    calls = []
    data = {"scope": {"map": "Map1"}, "rows": [{"anchor": {"id": "Pawn_A"}, "readings": [
        {"input_path": [0], "field": "health", "rows": [{"anchor": {"snapshot_local_id": "h"}, "readings": [
            {"input_path": [0, 0], "field": "conditions", "rows": [{"anchor": {"id": "Hediff1"}, "values": {"severity": 0.42, "unknown_extension": [1, 2, 3]}}]}]}]}]}]}

    async def handler(arguments, *, tool_name):
        calls.append((arguments, tool_name))
        return {"data": data, "observation_id": "observation1", "status": "ok", "issues": []}

    interaction, released = native_interaction(handler)
    reader = ObjectReading(interaction)
    request = {"target": {"kind": "pawns", "where": {"id": {"eq": "Pawn_A"}}}, "select": [
        {"field": "health", "children": [{"field": "conditions", "columns": ["severity"]}]}]}
    result = await reader.read(request)
    assert calls == [({**request, "lease_id": "test-consultation"}, "read_objects")]
    assert result["rows"] == data["rows"]
    assert "lease_id" not in json.dumps(reader.schema())
    result["rows"][0]["anchor"]["id"] = "changed"
    assert data["rows"][0]["anchor"]["id"] == "Pawn_A"
    await reader.aclose()
    await reader.aclose()
    assert released == [{"lease_id": "test-consultation"}]


async def test_json_fragments_rejoin_losslessly_across_utf16_surrogate_boundary():
    result = {"rows": [{"values": {"description": "左侧😀右侧"}}], "scope": {"map": "Map1"}}
    encoded = json.dumps(result, ensure_ascii=False).encode("utf-16-le")
    split = encoded.index("😀".encode("utf-16-le")) + 2
    pieces = [encoded[:split].decode("utf-16-le", "surrogatepass"), encoded[split:].decode("utf-16-le", "surrogatepass")]
    calls = []

    async def handler(args, *, tool_name):
        index = len(calls)
        calls.append(copy.deepcopy(args))
        return {"data": {"fragment": {"encoding": "json", "text": pieces[index], "offset": 0 if index == 0 else split // 2,
                                      "total_chars": len(encoded) // 2, "page": {"has_more": index == 0, **({"cursor": "next"} if index == 0 else {})}}},
                "observation_id": f"o{index}", "status": "ok", "issues": []}

    interaction, _ = native_interaction(handler)
    reader = ObjectReading(interaction)
    actual = await reader.read({"target": {"kind": "pawns"}, "select": []})
    assert actual["rows"] == result["rows"]
    assert calls[1]["page"] == {"cursor": "next"}
    assert len(calls) == 2


async def test_incomplete_fragment_fails_without_returning_truncated_facts():
    async def handler(args, *, tool_name):
        return {"data": {"fragment": {"encoding": "json", "text": "{}", "offset": 0, "total_chars": 50,
                                      "page": {"has_more": False}}}, "status": "ok"}

    interaction, _ = native_interaction(handler)
    with pytest.raises(ToolExecutionError, match="declared length"):
        await ObjectReading(interaction).read({"target": {"kind": "pawns"}, "select": []})


def legacy_interaction(records):
    directory = {"roots": [{"from": "pawns", "type": "Pawn"}], "field_index": {
        "Pawn": {"id": "String", "name": "String", "profile": "Profile", "health": "Health"},
        "Profile": {"skills": "[Skill]"}, "Skill": {"def_name": "String", "level": "Int", "passion": "String"},
        "Health": {"conditions": "[Condition]"}, "Condition": {"id": "String", "def_name": "String", "severity": "Float"}}, "common_fields": []}
    surface = Surface(directory)
    surface.argument_schemas = {"pawns": {"properties": {"map_id": {"type": "string"}}}}
    interaction = SimpleNamespace(setup=SimpleNamespace(directory=directory), surface=surface,
                                  original={}, session=SimpleNamespace(ledger=SimpleNamespace(run_id="legacy"), invalidated_error=None),
                                  sample_identity=("colony", "load1", "connection"), _def_resolver=lambda: None)
    calls = []

    def project(record, selection):
        output = {}
        for key, specification in selection.items():
            if specification is True:
                output[key] = copy.deepcopy(record.get(key))
            else:
                raw = record.get(key)
                if isinstance(raw, list):
                    filtered = [item for item in raw if all(item.get(column) == condition.get("eq", item.get(column))
                                                           for column, condition in specification.get("where", {}).items())]
                    output[key] = {"rows": [project(item, specification["select"]) for item in filtered], "state": "complete", "fields": "complete", "has_more": False}
                elif raw is None:
                    output[key] = None
                else:
                    output[key] = project(raw, specification["select"])
        return output

    async def handler(arguments, *, tool_name="query"):
        calls.append(copy.deepcopy(arguments))
        results = []
        for query in arguments["requests"]:
            rows = [project(record, query["select"]) for record in records]
            results.append({"rows": rows, "state": "complete", "fields": "complete", "has_more": False,
                            "scope": {"map_id": "Map1"}})
        return {"data": {"results": results}, "observation_id": f"o{len(calls)}", "load_epoch": "load1", "status": "ok", "issues": []}

    interaction.execute_native = handler
    return interaction, calls


async def test_legacy_duplicate_field_filters_do_not_read_cross_product_columns():
    interaction, calls = legacy_interaction([{"id": "Pawn_A", "profile": {"skills": [
        {"def_name": "Shooting", "level": 12, "passion": "Major"}, {"def_name": "Medicine", "level": 7, "passion": "Minor"}]}}])
    request = {"target": {"kind": "pawns"}, "select": [{"field": "profile", "children": [
        {"field": "skills", "where": {"def_name": {"eq": "Shooting"}}, "columns": ["level"]},
        {"field": "skills", "where": {"def_name": {"eq": "Medicine"}}, "columns": ["passion"]}]}]}
    result = await ObjectReading(interaction).read(request)
    assert result["selection_errors"] == []
    queries = calls[0]["requests"]
    assert len(queries) == 2
    assert set(queries[0]["select"]["profile"]["select"]["skills"]["select"]) == {"def_name", "level"}
    assert set(queries[1]["select"]["profile"]["select"]["skills"]["select"]) == {"def_name", "passion"}
    children = [reading["rows"][0]["readings"][0] for reading in result["rows"][0]["readings"]]
    assert [child["input_path"] for child in children] == [[0, 0], [0, 1]]
    assert children[0]["rows"][0]["values"] == {"level": 12}
    assert children[1]["rows"][0]["values"] == {"passion": "Minor"}


async def test_nested_cursor_restores_only_its_parent_without_any_native_reread():
    interaction, calls = legacy_interaction([{"id": "Pawn_A", "profile": {"skills": [
        {"def_name": f"Skill{i}", "level": i} for i in range(5)]}, "health": {"conditions": []}},
                                           {"id": "Pawn_B", "profile": {"skills": []}, "health": {"conditions": []}}])
    reader = ObjectReading(interaction)
    request = {"target": {"kind": "pawns"}, "select": [
        {"field": "profile", "children": [{"field": "skills", "columns": ["level"], "page": {"size": 2}}]},
        {"field": "health", "children": [{"field": "conditions", "columns": ["severity"]}]}]}
    result = await reader.read(request)
    cursor = result["page"]["cursor"]
    resumed = await reader.read({"page": {"cursor": cursor}})
    assert len(calls) == 1
    assert len(resumed["rows"]) == 1
    root = resumed["rows"][0]
    assert root["anchor"]["id"] == "Pawn_A"
    assert [item["field"] for item in root["readings"]] == ["profile"]
    page = root["readings"][0]["rows"][0]["readings"][0]
    assert [row["values"]["level"] for row in page["rows"]] == [2, 3]
    assert resumed["scope"] == {"map_id": "Map1"}
    cursor_only = await reader.read({"page": {"cursor": cursor}})
    assert len(calls) == 1
    assert cursor_only["rows"][0]["anchor"] == root["anchor"]
    assert [item["field"] for item in cursor_only["rows"][0]["readings"]] == ["profile"]
    replayed = cursor_only["rows"][0]["readings"][0]["rows"][0]["readings"][0]
    assert replayed["rows"] == page["rows"]


async def test_self_reads_root_values_through_the_existing_query_source():
    interaction, calls = legacy_interaction([{"id": "Pawn_A", "name": "Lumi"}])
    result = await ObjectReading(interaction).read({"target": {"kind": "pawns"}, "select": [{"field": "self", "columns": ["name"]}]})
    assert calls[0]["requests"][0]["select"] == {"id": True, "name": True}
    assert result["rows"][0]["readings"][0]["rows"][0]["values"] == {"name": "Lumi"}


async def test_native_cursor_only_keeps_transport_small_and_restores_semantic_binding():
    calls = []

    async def handler(arguments, *, tool_name):
        calls.append(copy.deepcopy(arguments))
        return {"data": {"rows": [], "page": {"cursor": "bound", "has_more": True}}, "status": "ok"}

    interaction, _ = native_interaction(handler)
    reader = ObjectReading(interaction)
    first = await reader.read({"target": {"kind": "pawns"}, "select": []})
    await reader.read(first["resume"])
    assert calls[1] == {"page": {"cursor": "bound"}, "lease_id": "test-consultation"}
    await reader.aclose()
    assert reader.cursor_requests == {}


def test_signature_cursor_is_not_a_new_read_but_page_size_is():
    first = {"target": {"kind": "pawns"}, "select": [{"field": "health"}]}
    next_page = copy.deepcopy(first)
    next_page["select"][0]["page"] = {"cursor": "opaque"}
    assert signature(first) == signature(next_page)
    next_page["select"][0]["page"]["size"] = 12
    assert signature(first) != signature(next_page)


async def test_coarse_state_preserves_base_and_recipient_checks_without_inferred_actor():
    interaction, calls = legacy_interaction([{"id": "Pawn_A"}])
    interaction.object_contract = {"parameters": {"properties": {"facets": {"items": {"enum": ["bed", "reservations"]}}}}}
    state_calls = []

    async def private_state(args):
        state_calls.append(copy.deepcopy(args))
        return {"facts": [{"object": {"id": "Pawn_A"}, "facts": {"state": {"facets": {"bed": {
            "medical_designation": False, "can_use_bed_now": True, "is_valid_bed_for": True,
            "context": {"actor": "Patient", "user": "Patient"}}}}}}]}

    interaction._read_object_compat = private_state
    request = {"target": {"kind": "pawns"}, "select": [{"field": "state", "children": [
        {"field": "bed", "columns": ["medical_designation", "can_use_bed_now", "is_valid_bed_for"]}]}],
               "context": {"recipient": {"id": "Patient"}}}
    result = await ObjectReading(interaction).read(request)
    bed = result["rows"][0]["readings"][0]["rows"][0]["readings"][0]["rows"][0]
    assert bed["values"]["medical_designation"] is False
    assert bed["values"]["can_use_bed_now"] is True
    assert bed["values"]["is_valid_bed_for"] == {"state": "unavailable", "reason": "actor_required"}
    assert "actor" not in state_calls[0]
    assert state_calls[0]["user"] == {"kind": "pawn", "id": "Patient"}
    assert result["source_granularity"] == "query_projection"


async def test_missing_column_is_unavailable_and_does_not_turn_null_or_false():
    interaction, _ = legacy_interaction([{"id": "Pawn_A", "profile": {"skills": [
        {"def_name": "Shooting", "level": {"state": "unavailable", "reason": "native_getter_failed"}}]}}])
    request = {"target": {"kind": "pawns"}, "select": [{"field": "profile", "children": [
        {"field": "skills", "columns": ["level"]}]}]}
    result = await ObjectReading(interaction).read(request)
    skills = result["rows"][0]["readings"][0]["rows"][0]["readings"][0]
    assert skills["coverage"]["source"] == "complete"
    assert skills["coverage"]["fields"] == "partial"
    assert skills["rows"][0]["values"]["level"]["state"] == "unavailable"


async def test_real_catalog_handler_registry_retains_bounded_semantic_pages_without_autofetch():
    from plugin.plugins.neko_rimworld.agent.contracts import ToolCall
    from plugin.plugins.neko_rimworld.agent.semantic_tools import SemanticInteraction
    from plugin.plugins.neko_rimworld.agent.tools import ToolRegistry
    from plugin.plugins.neko_rimworld.observation import Observation
    from plugin.plugins.neko_rimworld.query_tools import make_game_tools

    from .test_semantic_tools import Session, public_source

    source = public_source()
    source["object_read"] = {"version": "object_read.v1", "operation": "read_objects", "parameters": parameters(),
                             "roots": {"pawns": "Pawn"}, "types": {"Pawn": {"report": {"type": "String", "scalar": True, "collection": False},
                                 "self": {"type": "Pawn", "scalar": False, "collection": False}}}}
    whole = "原生报告😀" * 32000
    calls = []
    getter_reads = 0

    async def backend(envelope):
        nonlocal getter_reads
        calls.append(copy.deepcopy(envelope))
        arguments = envelope["arguments"]
        cursor = arguments.get("page", {}).get("cursor")
        offset = int(cursor.split("-")[1]) if cursor else 0
        if cursor is None:
            getter_reads += 1
        text = whole[offset:offset + 60000]
        next_offset = offset + len(text)
        more = next_offset < len(whole)
        window = {"text": text, "offset": offset, "total_chars": len(whole), "complete": not more,
                  **({"cursor": f"text-{next_offset}"} if more else {})}
        data = {"scope": {"map": "Map1"}, "rows": [{"anchor": {"id": "Pawn_A", "type": "Pawn"}, "readings": [
            {"input_path": [0], "field": "self", "rows": [{"anchor": {"id": "Pawn_A", "type": "Pawn"}, "values": {"report": window}}],
             "page": {"has_more": False}, "coverage": {"source_complete": True}}]}],
                "page": {"has_more": more, **({"cursor": f"text-{next_offset}"} if more else {})}}
        return Observation({"observation_id": f"o{len(calls)}", "request": envelope,
                            "response": {"colony_id": "colony", "load_epoch": "load", "connection_epoch": "connection",
                                         "payload": {"status": "ok", "data": data, "issues": []}}})

    session = Session()
    session.ledger = SimpleNamespace(run_id="registry-run")
    adapter = SemanticInteraction(make_game_tools(source, backend), session, lambda *_: None)
    prompt = adapter.prompt(4)
    assert "look uses target/select" in prompt
    assert "look.aspects" not in prompt and "look.focus" not in prompt
    assert "coarse query projection" not in prompt
    registry = ToolRegistry()
    for spec in adapter.read_tools():
        registry.register(spec)
    assert "read_objects" not in {item["function"]["name"] for item in registry.schemas()}
    request = {"target": {"kind": "pawns"}, "select": [{"field": "self", "columns": ["report"]}]}
    texts = []
    while True:
        prepared = registry.prepare(ToolCall(f"call{len(texts)}", "look", json.dumps(request, ensure_ascii=False)))
        result = await registry.execute(prepared)
        assert result.ok, result.value
        assert len(json.dumps(result.value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 80000
        assert len(calls) <= len(texts) + 1, "semantic pages must not be fetched without model intent"
        data = result.value["data"]
        texts.append(data["rows"][0]["readings"][0]["rows"][0]["values"]["report"]["text"])
        if not data["page"]["has_more"]:
            break
        request = data["resume"]
    assert "".join(texts) == whole
    assert len(whole) > 131072
    assert getter_reads == 1
    assert len(session.observations) == len(calls) == 3


async def test_selected_native_units_are_published_once_without_unselected_directory():
    data = {"rows": [{"anchor": {"id": ident, "type": "Pawn"}, "readings": [
        {"input_path": [0], "field": "health", "rows": [{"anchor": {"type": "Health"}, "readings": [
            {"input_path": [0, 0], "field": "summary", "rows": [{"anchor": {"type": "HealthSummary"}, "values": {"bleed_rate": 0.02}}]}]}]}]}
                     for ident in ("Pawn_A", "Pawn_B")]}

    async def handler(arguments, *, tool_name):
        return {"data": data, "observation_id": "o1", "status": "ok", "issues": []}

    interaction, _ = native_interaction(handler)
    types = interaction.setup.directory["object_read"]["types"]
    types["Health"]["summary"] = {"type": "HealthSummary", "scalar": False, "collection": False}
    types["HealthSummary"] = {"bleed_rate": {"type": "Float", "scalar": True, "collection": False}}
    interaction.surface = SimpleNamespace(notes={"HealthSummary": {
        "bleed_rate": "Blood-loss fraction per game day, not per second.", "health_percent": "Not blood volume."},
                                                "Trait": {"degree": "Variant identifier, not stat bonus."}})
    result = await ObjectReading(interaction).read({"target": {"kind": "pawns"}, "select": [
        {"field": "health", "children": [{"field": "summary", "columns": ["bleed_rate"]}]}]})
    assert result["field_semantics"] == {"HealthSummary.bleed_rate": "Blood-loss fraction per game day, not per second."}
    assert result["rows"] == data["rows"]


async def test_native_situation_derived_view_roundtrips_with_original_sources(tmp_path):
    from plugin.plugins.neko_rimworld.workspace.situation import saved_board
    from plugin.plugins.neko_rimworld.workspace.store import WorkspaceStore

    complete = {"source_complete": True, "predicate_complete": True, "fields_complete": True, "display_complete": True}

    def packet(event_ids, tick):
        readings = [{"input_path": [0], "field": "clock", "rows": [{"anchor": {"type": "ObjectClock"}, "values": {"ticks_game": tick}}],
                     "coverage": complete, "page": {"has_more": False}}]
        for index, field in enumerate(("events", "alerts", "conditions", "threat_groups"), 1):
            rows = [{"anchor": {"id": identity, "identity_scope": "native", "type": "ObjectEvent"}, "values": {"label": identity}}
                    for identity in event_ids] if field == "events" else []
            readings.append({"input_path": [index], "field": field, "rows": rows, "coverage": complete, "page": {"has_more": False}})
        readings.append({"input_path": [5], "field": "comparison", "rows": [{"anchor": {"type": "ObjectComparison"},
                            "values": {"available": False, "reason": "plugin_derived"}}], "coverage": complete,
                         "page": {"has_more": False}})
        return {"scope": {"map": "Map1"}, "sample": {"finished_tick": tick, "atomic": False},
                "rows": [{"anchor": {"id": "Map1/situation", "type": "ObjectSituation"}, "readings": readings}]}

    interaction, _ = native_interaction(None)
    cap = interaction.setup.directory["object_read"]
    cap["roots"] = {"situation": "ObjectSituation"}
    cap["types"]["ObjectSituation"] = {name: {"type": "Object" + name, "scalar": False, "collection": name != "clock"}
                                                for name in ("clock", "events", "alerts", "conditions", "threat_groups")}
    cap["types"]["ObjectSituation"]["comparison"] = {"type": "ObjectComparison", "scalar": False, "collection": False}
    session = interaction.session
    session.observations = []
    session.situation_views = []

    async def handler(args, *, tool_name):
        index = len(session.observations) + 1
        data = packet(["Letter1"] if index == 1 else ["Letter1", "Letter2"], index * 10)
        source = {"observation_id": f"o{index}", "received_at": "2026-10-11T10:00:00Z",
                  "request": {"operation": "read_objects", "arguments": copy.deepcopy(args)},
                  "response": {"colony_id": "colony", "load_epoch": "load", "payload": {"status": "ok", "data": copy.deepcopy(data), "issues": []}}}
        session.observations.append(source)
        return {"data": data, "observation_id": f"o{index}", "colony_id": "colony", "load_epoch": "load", "status": "ok", "issues": []}

    interaction.execute_native = handler
    reader = ObjectReading(interaction)
    request = {"target": {"kind": "situation"}, "select": [
        {"field": "clock", "columns": ["ticks_game"]}, {"field": "events", "columns": ["label"]},
        {"field": "alerts"}, {"field": "conditions"}, {"field": "threat_groups"},
        {"field": "comparison", "columns": ["available", "new_source_ids"]}]}
    await reader.read(request)
    current = await reader.read(request)
    comparison = current["rows"][0]["readings"][5]["rows"][0]["values"]
    assert comparison["available"] is True
    assert comparison["new_source_ids"] == ["events/Letter2"]
    assert session.observations[-1]["response"]["payload"]["data"]["rows"][0]["readings"][5]["rows"][0]["values"]["reason"] == "plugin_derived"
    assert session.situation_views[-1]["derived_from"] == ["o2"]
    assert session.situation_views[-1]["received_at"] == "2026-10-11T10:00:00Z"
    assert {key: saved_board(session.situation_views[-1])["comparison"][key] for key in comparison} == comparison
    store = WorkspaceStore(tmp_path / "work.sqlite")
    try:
        work = store.open_work("owner", "colony")
        turn = store.accept(work, "owner", "Current situation")
        store.start(work, "owner", turn)
        store.finish(work, "owner", turn, {"status": "ok", "observations": session.observations,
                                          "situation_views": session.situation_views})
        next_turn = store.accept(work, "owner", "Next question")
        restored = store.latest_situation(work, "owner", before_turn=next_turn)
        assert restored["derived_from"] == ["o2"]
        assert saved_board(restored)["comparison"] == saved_board(session.situation_views[-1])["comparison"]
    finally:
        store.close()
