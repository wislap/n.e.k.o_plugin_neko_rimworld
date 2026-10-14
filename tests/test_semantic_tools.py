"""The semantic surface is the sole shipped interface.

What matters here is the wiring: which connections get the closed-enum vocabulary,
that the Mod's directory really leaves the prompt, and that unsupported
directories stop before dispatch. The compile and render semantics are
covered by the surface's own tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugin.plugins.neko_rimworld.agent.runtime.audit import AgentAuditRegistry
from plugin.plugins.neko_rimworld.agent.runtime.loop import AgentRuntime
from plugin.plugins.neko_rimworld.message_contracts import ToolCall
from plugin.plugins.neko_rimworld.agent.delivery.handoff import SubmissionDraft
from plugin.plugins.neko_rimworld.tool_contracts import ToolExecutionError, ToolSetup
from plugin.plugins.neko_rimworld.agent.world.semantic_tools import (
    SemanticInteraction,
    semantic_interaction_factory,
    semantic_surface_available,
)
from plugin.plugins.neko_rimworld.game.query_tools import make_game_tools

from .test_agent import FakeLogger, settings, turn

FIXTURES = Path(__file__).parent / "fixtures"
CATALOGUE_MARKER = "canonical fields"


def catalog():
    return json.loads((FIXTURES / "query_catalog.json").read_text(encoding="utf-8-sig"))


def public_source(*, directory=True, spatial=False):
    """A public_query catalog, with or without the directory the Surface needs."""
    source = catalog()
    parameters = {
        "type": "object",
        "properties": {
            "requests": {
                "type": "array",
                "items": {
                    "allOf": [
                        {
                            "oneOf": [
                                {"properties": {"from": {"const": "things"}, "args": {"type": "object"}}},
                                {"properties": {"from": {"const": "pawn"}, "args": {"type": "object"}}},
                            ]
                        }
                    ]
                },
            }
        },
    }
    public = {"version": "public_query.v2", "parameters": parameters, "rules": CATALOGUE_MARKER, "roots": []}
    if directory:
        public.update(
            roots=[
                {"from": "things", "type": "Thing", "description": "Map things."},
                {"from": "pawn", "type": "Pawn", "description": "One pawn."},
            ],
            field_index={
                "Thing": {
                    "id": "String",
                    "spawned": "Bool",
                    "stack_count": "Int",
                    "definition": "ThingDef",
                    "parent_holder": "Thing",
                },
                "Pawn": {"id": "String", "name": "String"},
            },
            common_fields=[],
            field_notes={},
        )
    source["public_query"] = public
    if spatial:
        source["spatial_query"] = {
            "version": "spatial_query.v1",
            "operation": "spatial_query",
            "parameters": __import__(
                "plugin.plugins.neko_rimworld.agent.world.semantic_surface", fromlist=["spatial_tool_schema"]
            ).spatial_tool_schema()["parameters"],
        }
    return source


async def _noop_query(arguments):
    raise AssertionError("no native read expected")


def setup_for(**kwargs) -> ToolSetup:
    return make_game_tools(public_source(**kwargs), _noop_query)


class Session:
    """Only the attributes the adapter reads while building tools or a prompt."""

    def __init__(self):
        self.submission = SubmissionDraft()
        self.observations = []


def _update(*_args, **_kwargs):
    return None


def test_semantic_surface_is_the_shipped_default():
    assert semantic_surface_available(setup_for()) is True




def test_the_mod_directory_leaves_the_prompt_but_the_enums_stay():
    """The reason this interface exists: the whole field catalogue stops being sent."""
    setup = setup_for()
    assert CATALOGUE_MARKER in setup.context, "the query interface needs the catalogue"

    adapter = semantic_interaction_factory(setup, Session(), _update)
    assert isinstance(adapter, SemanticInteraction)
    prompt = adapter.prompt(4)

    assert CATALOGUE_MARKER not in prompt
    assert '"field_index"' not in prompt
    assert "Specify the listed look.aspects" in prompt


def test_semantic_surface_replaces_the_query_vocabulary():
    adapter = semantic_interaction_factory(setup_for(), Session(), _update)
    assert [tool.name for tool in adapter.tools()] == ["look", "count", "submit_response"]

    adapter.finalizing = True
    assert [tool.name for tool in adapter.tools()] == ["submit_response"]


def test_native_activity_is_default_and_replaces_overlapping_path_tools():
    source = public_source(spatial=True)
    sections = ["identity", "health", "skills", "gear", "work", "state"]
    ref = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "map": {"type": "string"}},
    }
    state_properties = {name: {"type": "array", "items": ref} for name in ("targets",)}
    state_properties.update({name: ref for name in ("actor", "user")})
    state_properties.update({name: {} for name in ("stats", "include_fogged", "detail_limit", "exact_lookup")})
    state_properties["sections"] = {"items": {"enum": sections}}
    source["world_observation"] = {
        "version": "world_observation.v2.2",
        "tools": [
            {
                "name": "survey_space",
                "operation": "survey_space",
                "description": "survey",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "inspect_route",
                "operation": "inspect_route",
                "description": "route",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "inspect_state",
                "operation": "inspect_state",
                "description": "state",
                "parameters": {"type": "object", "properties": state_properties},
            },
        ],
    }
    adapter = semantic_interaction_factory(make_game_tools(source, _noop_query), Session(), _update)
    names = [tool.name for tool in adapter.tools()]
    assert "evaluate_activity" in names
    assert names.count("evaluate_activity") == 1
    assert "look" in names and "count" in names
    assert "inspect_route" not in names and "inspect_space" not in names


def test_spatial_tool_is_capability_gated_and_does_not_add_a_navigation_step():
    without = semantic_interaction_factory(setup_for(), Session(), _update)
    assert [tool.name for tool in without.tools()] == ["look", "count", "submit_response"]

    with_spatial = semantic_interaction_factory(setup_for(spatial=True), Session(), _update)
    assert [tool.name for tool in with_spatial.tools()] == ["look", "count", "inspect_space", "submit_response"]
    inspect = next(tool for tool in with_spatial.tools() if tool.name == "inspect_space")
    assert inspect.parameters["required"] == ["from", "to"]
    assert "look first" in inspect.description
    assert inspect.parameters["properties"]["from"]["properties"]["kind"]["enum"] == ["pawn"]
    assert set(inspect.parameters["properties"]["to"]["properties"]["kind"]["enum"]) == {"thing", "cell", "pawn"}


def test_spatial_tool_forwards_names_directly_and_renders_claim_shaped_result():
    adapter = semantic_interaction_factory(setup_for(spatial=True), Session(), _update)
    seen = []

    async def native(request, **kwargs):
        seen.append(request)
        return {
            "data": {
                "answer": "reachable",
                "from": {"kind": "pawn", "id": "Pawn_A"},
                "target": {"kind": "thing", "id": "Thing_Steel"},
                "checked": {"same_map": True, "native_reachability": True},
                "sample": {"game_tick": 42},
                "explanation": "Native reachability found a valid current path.",
            },
            "issues": [],
        }

    adapter.execute_native = native
    result = __import__("asyncio").run(
        adapter.inspect_space(
            {
                "from": {"kind": "pawn", "name": "Alice"},
                "to": {"kind": "thing", "name": "steel"},
            }
        )
    )
    assert seen == [
        {
            "from": {"kind": "pawn", "name": "Alice"},
            "to": {"kind": "thing", "name": "steel"},
        }
    ]
    assert result["answer"] == "reachable"
    assert result["checked"]["native_reachability"] is True
    assert result["sample"]["game_tick"] == 42


def test_a_mod_without_the_directory_is_rejected():
    setup = setup_for(directory=False)
    assert semantic_surface_available(setup) is False
    with pytest.raises(ToolExecutionError, match="requires a compilable"):
        semantic_interaction_factory(setup, None, None)


def test_a_setup_without_a_query_tool_is_rejected():
    """The adapter may only read through the Mod's query handler."""
    setup = ToolSetup(tools=(), directory=public_source()["public_query"], context="")
    assert semantic_surface_available(setup) is False
    with pytest.raises(ToolExecutionError, match="requires a compilable"):
        semantic_interaction_factory(setup, None, None)


def test_a_directory_that_cannot_compile_is_refused_before_the_prompt_promises_it():
    source = public_source()
    del source["public_query"]["field_index"]
    setup = make_game_tools(source, _noop_query)
    assert setup.directory is not None and "field_index" not in setup.directory
    assert semantic_surface_available(setup) is False
    with pytest.raises(ToolExecutionError, match="requires a compilable"):
        semantic_interaction_factory(setup, None, None)




@pytest.mark.asyncio
async def test_unsupported_semantic_connection_never_calls_the_model():
    class NeverCalled:
        async def complete(self, *args, **kwargs):
            raise AssertionError("Unsupported connection must stop before model dispatch")

    async def provider():
        return setup_for(directory=False)

    runtime = AgentRuntime( tools_provider=provider, audit_registry=AgentAuditRegistry(),
                           logger=FakeLogger(), model_factory=lambda _: NeverCalled(),
                           )
    result = await runtime.run("question", settings())
    assert result["status"] == "degraded"
    assert result["audit"]["error_code"] == "semantic_surface_unavailable"


class _Model:
    """Returns the scripted turns in order.

    `test_agent.ScriptedModel` asserts the first tool is the legacy `query_game`,
    which is not true on the public_query path this surface serves.
    """

    def __init__(self, turns):
        self._turns = list(turns)
        self.requests = []

    async def complete(self, messages, tools, *, timeout):  # noqa: ARG002
        self.requests.append({"messages": list(messages), "tools": list(tools)})
        return self._turns.pop(0)


@pytest.mark.asyncio
async def test_wiki_is_the_only_extra_passthrough_and_never_an_observation(tmp_path):
    from dataclasses import replace

    from plugin.plugins.neko_rimworld.agent.reference.wiki_tools import WikiStore, make_wiki_tool
    from plugin.plugins.neko_rimworld.agent.runtime.tools import ToolRegistry
    from plugin.plugins.neko_rimworld.tool_contracts import ToolSpec

    from .test_wiki_tools import store_from_directory, write_snapshot

    write_snapshot(tmp_path)
    wiki = make_wiki_tool(store_from_directory(tmp_path))
    forbidden = ToolSpec("unapproved", "not exposed", {}, _noop_query)
    setup = setup_for()
    setup = replace(setup, tools=(*setup.tools, wiki, forbidden))
    session = Session()
    adapter = semantic_interaction_factory(setup, session, _update)
    assert [tool.name for tool in adapter.tools()] == ["look", "count", "wiki", "submit_response"]
    assert "wiki" in {tool.name for tool in adapter.tools()}
    registry = ToolRegistry()
    for tool in adapter.tools():
        registry.register(tool)
    result = await registry.execute(registry.prepare(ToolCall("wiki1", "wiki", '{"query":"Steel"}')))
    assert result.observation is None
    assert session.observations == []
    adapter.finalizing = True
    assert [tool.name for tool in adapter.tools()] == ["submit_response"]


def test_missing_wiki_does_not_advertise_knowledge():
    adapter = semantic_interaction_factory(setup_for(), Session(), _update)
    assert "wiki(query)" not in adapter.prompt(4)


def report():
    return {
        "report_id": "report-1",
        "state": "final",
        "created_at": "2026-09-01T00:00:00Z",
        "question": "早先那一问。",
        "full_text": "早先的完整报告。",
        "observations": [],
    }


def test_spatial_catalog_to_real_handler_dispatch_and_observation_retention():
    import asyncio

    from plugin.plugins.neko_rimworld.game.observation import Observation
    from plugin.plugins.neko_rimworld.game.protocol import QUERY_RESULT, Payload, SessionDescriptor, make_envelope

    calls = []

    async def backend(request):
        calls.append(request)
        envelope = make_envelope(
            QUERY_RESULT,
            "1" * 32,
            1,
            payload=Payload(
                status="ok",
                data={
                    "answer": "unreachable",
                    "checked": {"native_reachability": True},
                    "explanation": "Native check returned false.",
                    "meaning": "The blocking cause is not identified.",
                },
                issues=[],
            ),
            session=SessionDescriptor("2" * 32, "3" * 32, 42, 1),
            request_id="4" * 32,
        )
        return Observation.from_response(request["operation"], request["arguments"], envelope)

    session = Session()
    adapter = semantic_interaction_factory(make_game_tools(public_source(spatial=True), backend), session, _update)
    tool = next(t for t in adapter.tools() if t.name == "inspect_space")
    args = {"from": {"kind": "pawn", "name": "Alice"}, "to": {"kind": "cell", "map": "current", "x": 1, "z": 2}}
    result = asyncio.run(tool.handler(args))
    assert calls == [{"operation": "spatial_query", "arguments": args}]
    assert result["answer"] == "unreachable"
    assert result["meaning"] == "The blocking cause is not identified."
    assert len(session.observations) == 1
    assert session.observations[0]["request"]["operation"] == "spatial_query"
    adapter.finalizing = True
    assert [t.name for t in adapter.tools()] == ["submit_response"]
