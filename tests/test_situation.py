"""Situation board: one native read reached through look, granularity set by the Mod."""

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


@pytest.mark.asyncio
async def test_situation_is_one_native_read_and_returns_entries_unchanged():
    from plugin.plugins.neko_rimworld.agent import ToolCall

    requests = []

    async def native(request):
        requests.append(request)
        return board_observation(request)

    call = ToolCall("a", "look", json.dumps({"object": {"kind": "situation"}, "aspects": ["state"]}))
    result, model = await run(situation_source(), [call], native)

    assert result["status"] == "ok"
    assert requests == [{"operation": "read_situation", "arguments": {"map": "current"}}]
    assert len(result["observations"]) == 1
    names = {t["function"]["name"] for t in model.requests[0]["tools"]}
    assert "read_situation" not in names and "look" in names
    kinds = [v["properties"]["kind"].get("const") for v in look_schema(model.requests[0])["parameters"]["properties"]["object"]["oneOf"]]
    assert "situation" in kinds
    tool_message = next(m for m in model.requests[1]["messages"] if m.get("role") == "tool")
    content = json.loads(tool_message["content"])
    situation = content["data"]["situation"]
    assert situation["threat_groups"] == BOARD["threat_groups"]
    assert situation["events"] == BOARD["events"]
    assert situation["conditions"] == BOARD["conditions"]


@pytest.mark.asyncio
async def test_connection_without_situation_does_not_offer_the_kind():
    from plugin.plugins.neko_rimworld.agent import ToolCall

    async def native(request):
        return native_observation(request)

    call = ToolCall("a", "look", json.dumps({"object": {"kind": "situation"}, "aspects": ["state"]}))
    _, model = await run(public_source(), [call], native)
    schema = json.dumps(look_schema(model.requests[0]))
    assert '"situation"' not in schema
    tool_message = next(m for m in model.requests[1]["messages"] if m.get("role") == "tool")
    assert "situation board" in tool_message["content"]


@pytest.mark.asyncio
async def test_situation_rejects_selection_options():
    from plugin.plugins.neko_rimworld.agent import ToolCall

    requests = []

    async def native(request):
        requests.append(request)
        return board_observation(request)

    call = ToolCall("a", "look", json.dumps({"object": {"kind": "situation"}, "aspects": ["state"], "detail": 3}))
    _, model = await run(situation_source(), [call], native)
    assert requests == []
    tool_message = next(m for m in model.requests[1]["messages"] if m.get("role") == "tool")
    assert "semantic_request_invalid" in tool_message["content"]


def test_situation_catalog_does_not_change_model_context():
    plain = make_game_tools(public_source(), lambda _: None)
    board = make_game_tools(situation_source(), lambda _: None)
    assert plain.context == board.context
    assert [t.name for t in board.tools] == [*(t.name for t in plain.tools), "read_situation"]
