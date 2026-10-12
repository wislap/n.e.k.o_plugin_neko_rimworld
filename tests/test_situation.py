"""Situation board: one native read, consolidated view and unmodified native audit."""

import copy
import json

import pytest

from plugin.plugins.neko_rimworld.agent.semantic_tools import semantic_interaction_factory
from plugin.plugins.neko_rimworld.observation import Observation
from plugin.plugins.neko_rimworld.query_tools import make_game_tools

from .test_agent import FakeLogger, settings, submission_call, turn
from .test_capability_composition import Script, native_observation
from .test_semantic_tools import public_source

BOARD = {
    "map": {"id": "Map_0", "is_player_home": True},
    "clock": {"ticks_game": 412300, "paused": False, "year": 5501, "quadrum": "Aprimay", "day_of_quadrum": 3, "hour": 7},
    "conditions": [{"def": "ToxicFallout", "label": "Toxic fallout", "scope": "map", "since_tick": 400000, "permanent": False, "ticks_left": 90000}],
    "threat_groups": [
        {"lord": "Lord_12", "faction": {"id": "Faction_9", "name": "Red Hand", "def": "Pirate"}, "lord_job": "LordJob_AssaultColony",
         "toil": "LordToil_AssaultColony", "members": ["Thing_Human981", "Thing_Human982"]},
    ],
    "events": [
        {"kind": "letter", "def": "ThreatBig", "label": "Raid", "tick": 410000, "pinned": False, "subjects": ["Thing_Human981", {"def": "Muffalo", "count": 23}]},
    ],
    "scope": {"events": "Game archive entries created within the last 60000 ticks"},
    "status": "ok",
    "read_only": True,
}


def situation_source():
    source = copy.deepcopy(public_source())
    source["situation"] = {
        "version": "situation.v1",
        "operation": "read_situation",
        "event_window_ticks": 60000,
        "description": "Current situation digest",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {"map": {"type": "string"}}},
    }
    return source


def board_observation(request):
    observation = native_observation(request).to_dict()
    observation["request"] = {"operation": "read_situation", "arguments": request}
    observation["response"]["payload"]["data"] = copy.deepcopy(BOARD)
    return Observation(observation)


async def run(source, calls, native):
    from plugin.plugins.neko_rimworld.agent import AgentAuditRegistry, AgentRuntime

    setup = make_game_tools(source, native)
    model = Script([*(turn("", call) for call in calls), turn("", submission_call("Raid ongoing.", "Raid ongoing."))])

    async def provide():
        return setup

    runtime = AgentRuntime(
        observation_tools=[], audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
        tools_provider=provide, model_factory=lambda _: model, interaction_factory=semantic_interaction_factory,
    )
    return await runtime.run("What is happening right now?", settings()), model


def look_schema(request):
    return next(t["function"] for t in request["tools"] if t["function"]["name"] == "look")


def situation_request():
    """Explicit model projection; the coarse source still retains its complete raw audit."""
    return {"target": {"kind": "situation"}, "select": [
        {"field": "clock", "columns": ["ticks_game", "paused", "year", "quadrum", "day_of_quadrum", "hour"]},
        {"field": "conditions", "columns": ["def", "label", "scope", "since_tick", "permanent", "ticks_left"]},
        {"field": "threat_groups", "columns": ["lord", "lord_job", "toil"], "children": [
            {"field": "faction", "columns": ["id", "name", "def"]}]},
        {"field": "events", "columns": ["kind", "def", "label", "tick", "pinned"]},
        {"field": "comparison", "columns": ["available"]},
    ]}


def readings(packet):
    return {reading["field"]: reading for reading in packet["rows"][0]["readings"]}


@pytest.mark.asyncio
async def test_situation_is_one_native_read_and_retains_all_source_facts():
    from plugin.plugins.neko_rimworld.agent import ToolCall

    requests = []

    async def native(request):
        requests.append(request)
        return board_observation(request)

    request = situation_request()
    call = ToolCall("a", "look", json.dumps(request))
    result, model = await run(situation_source(), [call], native)

    assert result["status"] == "ok"
    assert requests == [{"operation": "read_situation", "arguments": {"map": "current"}}]
    assert len(result["observations"]) == 1
    names = {t["function"]["name"] for t in model.requests[0]["tools"]}
    assert "read_situation" not in names and "look" in names
    schema = look_schema(model.requests[0])["parameters"]
    kinds = schema["properties"]["target"]["properties"]["kind"]["enum"]
    assert "situation" in kinds
    assert set(schema["properties"]) == {"target", "select", "page", "context"}
    tool_message = next(m for m in model.requests[1]["messages"] if m.get("role") == "tool")
    content = json.loads(tool_message["content"])
    selected = readings(content["data"])
    assert content["data"]["source_granularity"] == "coarse_situation"
    for read in request["select"]:
        section = read["field"]
        if section == "comparison":
            continue
        source_rows = BOARD[section] if isinstance(BOARD[section], list) else [BOARD[section]]
        assert [row["values"] for row in selected[section]["rows"]] == [
            {column: row[column] for column in read["columns"]} for row in source_rows]
    faction = selected["threat_groups"]["rows"][0]["readings"][0]["rows"][0]["values"]
    assert faction == BOARD["threat_groups"][0]["faction"]
    assert "members" not in selected["threat_groups"]["rows"][0]["values"]
    assert result["observations"][0]["response"]["payload"]["data"] == BOARD
    assert selected["comparison"]["rows"][0]["values"]["available"] is False
    assert "open-ended colony assessment" in model.requests[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_connection_without_situation_does_not_offer_the_kind():
    from plugin.plugins.neko_rimworld.agent import ToolCall

    requests = []

    async def native(request):
        requests.append(request)
        return native_observation(request)

    call = ToolCall("a", "look", json.dumps(situation_request()))
    _, model = await run(public_source(), [call], native)
    schema = json.dumps(look_schema(model.requests[0]))
    assert '"situation"' not in schema
    tool_message = next(m for m in model.requests[1]["messages"] if m.get("role") == "tool")
    assert json.loads(tool_message["content"])["error"]["code"] == "semantic_request_invalid"
    assert requests == [], 'an unavailable root is rejected before native transport'


@pytest.mark.asyncio
async def test_situation_rejects_selection_options():
    from plugin.plugins.neko_rimworld.agent import ToolCall

    requests = []

    async def native(request):
        requests.append(request)
        return board_observation(request)

    call = ToolCall("a", "look", json.dumps({**situation_request(), "detail": 3}))
    _, model = await run(situation_source(), [call], native)
    assert requests == []
    tool_message = next(m for m in model.requests[1]["messages"] if m.get("role") == "tool")
    assert json.loads(tool_message["content"])["error"]["code"] == "invalid_tool_arguments"


def test_situation_catalog_does_not_change_model_context():
    plain = make_game_tools(public_source(), lambda _: None)
    board = make_game_tools(situation_source(), lambda _: None)
    assert plain.context == board.context
    assert [t.name for t in board.tools] == [*(t.name for t in plain.tools), "read_situation"]
