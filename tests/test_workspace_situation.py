"""Saved situation views use retained facts, scoped history and verified hashes."""

import copy
import hashlib
import json
import sqlite3

import pytest

from ..workspace.situation import saved_board
from ..workspace.store import WorkspaceStore
from ..workspace.views import WorkspaceViews


def observation(identity, *, tick=10, epoch="load", status="ok"):
    return {
        "observation_id": identity,
        "request": {"operation": "read_situation", "arguments": {"map": "current"}},
        "received_at": "2026-10-10T01:00:00Z",
        "response": {
            "colony_id": "colony", "load_epoch": epoch, "game_tick": tick, "snapshot_sequence": tick,
            "payload": {"status": status, "issues": [], "data": {
                "map": {"id": "Map_0"}, "clock": {"ticks_game": tick}, "status": "ok", "scope": {},
                "conditions": [{"id": "GameCondition_1", "def": "ToxicFallout", "label": "毒尘"}],
                "threat_groups": [], "events": [], "alerts": [],
            }},
        },
    }


def finish(store, wid, owner, *sources):
    tid = store.accept(wid, owner, "Read situation")
    store.start(wid, owner, tid)
    store.finish(wid, owner, tid, {"status": "ok", "observations": list(sources)})
    return tid


def test_saved_board_keeps_native_evidence_and_failure_scope():
    source = observation("partial", status="partial")
    source["response"]["payload"]["issues"] = [{"section": "alerts", "message": "unavailable"}]
    original = copy.deepcopy(source)
    raw = saved_board(source)
    assert raw["status"] == "partial" and raw["sample"]["load_epoch"] == "load"
    raw["conditions"][0]["label"] = "changed view"
    assert source == original
    assert saved_board({"request": {"operation": "query"}}) is None


def test_lazy_baseline_stays_in_owner_work_and_never_skips_reload(tmp_path):
    store = WorkspaceStore(tmp_path / "work.sqlite")
    try:
        wid = store.open_work("alpha", "colony")
        finish(store, wid, "alpha", observation("old"))
        finish(store, wid, "alpha", observation("reloaded", epoch="new-load"))
        other = store.open_work("beta", "colony")
        finish(store, other, "beta", observation("other-owner"))
        current = store.accept(wid, "alpha", "next")
        assert store.latest_situation(wid, "alpha", before_turn=current)["observation_id"] == "reloaded"
        with pytest.raises(ValueError, match="owner"):
            store.latest_situation(wid, "beta", before_turn=current)
    finally:
        store.close()


def test_situation_ui_reads_selected_turn_and_within_turn_baseline_without_writes(tmp_path):
    path = tmp_path / "work.sqlite"
    store = WorkspaceStore(path)
    wid = store.open_work("alpha", "colony")
    first = finish(store, wid, "alpha", observation("first"))
    selected = finish(store, wid, "alpha", observation("second", tick=20), observation("third", tick=30))
    finish(store, wid, "alpha", observation("future", tick=40))
    store.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    view = WorkspaceViews(path)
    initial = view.read("turn", workspace_id=wid, turn_id=first)["situation"]
    assert initial["observation_id"] == "first" and not initial["comparison"]["available"]
    assert not initial["comparison"]["new_item_ids"]
    saved = view.read("turn", workspace_id=wid, turn_id=selected)["situation"]
    assert saved["observation_id"] == "third" and saved["historical"]
    assert saved["comparison"]["baseline_observation_id"] == "second"
    assert saved["board"]["items"][0]["label"] == "毒尘"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_situation_ui_hash_checks_projected_evidence(tmp_path):
    path = tmp_path / "work.sqlite"
    store = WorkspaceStore(path)
    wid = store.open_work("alpha", "colony")
    tid = finish(store, wid, "alpha", observation("first"))
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER sealed_observation")
        db.execute("UPDATE observations SET sha256='invalid'")
    with pytest.raises(ValueError, match="hash check"):
        WorkspaceViews(path).read("turn", workspace_id=wid, turn_id=tid)


@pytest.mark.asyncio
@pytest.mark.parametrize("history_failure", [False, True])
async def test_workspace_baseline_is_lazy_and_read_failure_keeps_current_board(tmp_path, history_failure):
    from ..agent import AgentAuditRegistry, AgentRuntime
    from ..agent.semantic_tools import semantic_interaction_factory
    from ..observation import Observation
    from ..query_tools import make_game_tools
    from ..workspace.runtime import WorkspaceAgent
    from ..workspace.storage import WorkspaceStorage
    from .test_agent import FakeLogger, settings
    from .test_hosted_workspace import Script, call
    from .test_situation import readings, situation_source

    path = tmp_path / "work.sqlite"
    store = WorkspaceStore(path)
    wid = store.open_work("alpha", "colony")
    finish(store, wid, "alpha", observation("baseline"))
    plain_turn = store.accept(wid, "alpha", "Reply briefly")
    store.close()

    class TrackedStorage(WorkspaceStorage):
        baseline_reads = 0

        async def call(self, method, *args, **kwargs):
            if method == "latest_situation":
                self.baseline_reads += 1
                if history_failure:
                    raise OSError("Saved history unavailable")
            return await super().call(method, *args, **kwargs)

    storage = TrackedStorage(path)
    models = [Script([call("submit_response", answer="Done.")]), Script([
        call("look", target={"kind": "situation"}, select=[
            {"field": "conditions", "columns": ["id", "label"]},
            {"field": "comparison", "columns": ["available", "reason", "baseline_observation_id"]},
        ]),
        call("submit_response", answer="Read the current situation."),
    ])]
    board_model = models[1]

    async def native(request):
        current = observation("current", tick=20)
        current["request"] = request
        return Observation(current)

    async def provide():
        return make_game_tools(situation_source(), native)

    runtime = AgentRuntime(
        observation_tools=[], audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
        tools_provider=provide, model_factory=lambda _: models.pop(0), interaction_factory=semantic_interaction_factory,
    )
    agent = WorkspaceAgent(storage, runtime)
    try:
        await agent.execute(wid, "alpha", plain_turn, settings(), colony_id="colony")
        assert storage.baseline_reads == 0
        next_turn = await storage.call("accept", wid, "alpha", "What is happening?")
        result = await agent.execute(wid, "alpha", next_turn, settings(), colony_id="colony")
        assert storage.baseline_reads == 1 and result["result"]["status"] == "ok"
        message = next(m for m in board_model.requests[1]["messages"] if m.get("role") == "tool")
        content = json.loads(message["content"])["data"]
        selected = readings(content)
        assert selected["conditions"]["rows"][0]["values"] == {"id": "GameCondition_1", "label": "毒尘"}
        comparison = selected["comparison"]["rows"][0]["values"]
        assert comparison["available"] is (not history_failure)
        if history_failure:
            assert comparison["reason"] == "history_unavailable"
        else:
            assert comparison["baseline_observation_id"] == "baseline"
    finally:
        await storage.close()
