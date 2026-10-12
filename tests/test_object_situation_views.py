"""New selected boards keep original evidence and honest source coverage."""

import copy
import hashlib

import pytest

from ..agent.situation import board_view, compare_boards
from ..workspace.situation import saved_board
from ..workspace.store import WorkspaceStore
from ..workspace.views import WorkspaceViews


def selected_observation(identity="O1", *, partial=False, offset=0):
    complete = {"source_complete": True, "predicate_complete": True, "display_complete": True, "fields_complete": True}
    events = [{"anchor": {"id": "map/event/Letter_1", "native_id": "Letter_1", "identity_scope": "native"},
        "values": {"id": "Letter_1", "label": "Task", "tick": 10},
        "readings": [{"field": "quest", "input_path": [0], "coverage": complete, "page": {"has_more": False},
                      "rows": [{"anchor": {"id": "quest/Quest_1"}, "values": {"id": "Quest_1", "state": "Ongoing"}}]}]}]
    result = {
        "scope": {"map": "Map_0"}, "sample": {"started_tick": 10, "finished_tick": 10, "atomic": False},
        "rows": [{"readings": [
            {"field": "events", "rows": events, "coverage": complete, "page": {"has_more": False, "offset": offset}},
            {"field": "alerts", "status": "unavailable" if partial else "observed", "rows": [],
                "coverage": {**complete, "source_complete": not partial}, "page": {"has_more": False}},
        ]}],
    }
    return {"observation_id": identity, "received_at": "2026-10-11T00:00:00Z",
        "request": {"operation": "read_objects", "arguments": {"target": {"kind": "situation"}, "select": [{"field": "events"}, {"field": "alerts"}]}},
        "response": {"colony_id": "colony", "load_epoch": "epoch", "payload": {"status": "partial" if partial else "ok", "data": result, "issues": []}}}


def test_complete_events_survive_unavailable_alerts_and_unselected_sources():
    source = selected_observation(partial=True)
    original = copy.deepcopy(source)
    current = saved_board(source)
    assert current["events"][0]["id"] == "Letter_1"
    assert "events_unavailable" not in current
    assert current["alerts_unavailable"]["reason"] == "incomplete_selected_source"
    assert current["conditions_unavailable"]["reason"] == "not_selected"
    previous = copy.deepcopy(current)
    previous["events"] = []
    comparison = compare_boards(current, previous)
    assert comparison["available"] is True
    assert comparison["compared_sections"] == ["events"]
    assert board_view(current)["items"][0]["id"] == "quest/Quest_1"
    assert source == original


def test_end_page_and_text_tail_never_establish_complete_column():
    tail = saved_board(selected_observation(offset=20))
    assert "events_unavailable" in tail
    source = selected_observation()
    source["response"]["payload"]["data"]["rows"][0]["readings"][0]["rows"][0]["values"]["label"] = {
        "text": "last window", "offset": 2048, "returned_chars": 11, "total_chars": 2059, "complete": True}
    assert "events_unavailable" in saved_board(source)


def test_partial_selected_child_prevents_full_event_comparison():
    source = selected_observation()
    child = source["response"]["payload"]["data"]["rows"][0]["readings"][0]["rows"][0]["readings"][0]
    child["page"] = {"has_more": True, "cursor": "remaining-quest-records"}
    child["coverage"] = {**child["coverage"], "display_complete": False}
    assert "events_unavailable" in saved_board(source)


def test_source_baseline_uses_its_own_interval_not_a_later_sibling_delivery():
    from ..agent.situation import section_board

    source = selected_observation()
    data = source["response"]["payload"]["data"]
    data["sample"].update(started_tick=90, finished_tick=300)
    data["rows"][0]["readings"][0]["sample"] = {"started_tick": 90, "finished_tick": 100, "atomic": False}
    data["rows"][0]["readings"][1]["sample"] = {"started_tick": 200, "finished_tick": 300, "atomic": False}
    current = saved_board(source)
    baseline = section_board(current, "events")
    assert current["sample"]["finished_tick"] == 300
    assert baseline["sample"]["finished_tick"] == 100
    assert baseline["sample"]["load_epoch"] == "epoch"
    assert baseline["sample"]["colony_id"] == "colony"
    assert section_board(current, "alerts")["sample"]["finished_tick"] == 300


def test_saved_derived_view_roundtrip_does_not_rewrite_native_evidence(tmp_path):
    path = tmp_path / "work.sqlite"
    store = WorkspaceStore(path)
    wid = store.open_work("role", "colony")
    tid = store.accept(wid, "role", "Read events")
    store.start(wid, "role", tid)
    original = selected_observation()
    derived = copy.deepcopy(original)
    derived["derived_from"] = [original["observation_id"]]
    derived["selection_scopes"] = {"events": "same-selection"}
    store.finish(wid, "role", tid, {"status": "ok", "observations": [original], "situation_views": [derived]})
    native = store.read(wid, "role", tid, observation_id=original["observation_id"])
    assert native["observation"] == original
    next_turn = store.accept(wid, "role", "Continue")
    assert store.latest_situation(wid, "role", before_turn=next_turn)["derived_from"] == ["O1"]
    store.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    view = WorkspaceViews(path).read("turn", workspace_id=wid, turn_id=tid)["situation"]
    assert view["source_observation_ids"] == ["O1"]
    assert view["board"]["items"][0]["kind"] == "quest"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_derived_view_without_original_source_is_rejected_atomically(tmp_path):
    store = WorkspaceStore(tmp_path / "work.sqlite")
    try:
        wid = store.open_work("role", "colony")
        tid = store.accept(wid, "role", "Read")
        store.start(wid, "role", tid)
        derived = selected_observation()
        derived["derived_from"] = ["missing-source"]
        with pytest.raises(ValueError, match="retained original"):
            store.finish(wid, "role", tid, {"status": "ok", "observations": [], "situation_views": [derived]})
        assert store.turn(wid, "role", tid)["status"] == "running"
    finally:
        store.close()


def test_missing_baseline_original_is_rejected_before_committing_a_view(tmp_path):
    store = WorkspaceStore(tmp_path / "work.sqlite")
    try:
        wid = store.open_work("role", "colony")
        tid = store.accept(wid, "role", "Read")
        store.start(wid, "role", tid)
        original = selected_observation()
        derived = copy.deepcopy(original)
        derived["derived_from"] = ["O1"]
        derived["comparison_baselines"] = {"events": {"observation_id": "missing", "derived_from": ["missing"]}}
        with pytest.raises(ValueError, match="unique retained original"):
            store.finish(wid, "role", tid, {"status": "ok", "observations": [original], "situation_views": [derived]})
        assert store.turn(wid, "role", tid)["status"] == "running"
    finally:
        store.close()


def test_baseline_and_known_item_hashes_survive_restart_and_detect_changed_original(tmp_path):
    import json
    import sqlite3

    store = WorkspaceStore(tmp_path / "work.sqlite")
    try:
        wid = store.open_work("role", "colony")
        for identity in ("O1", "O2"):
            tid = store.accept(wid, "role", identity)
            store.start(wid, "role", tid)
            original = selected_observation(identity)
            derived = copy.deepcopy(original)
            derived["derived_from"] = [identity]
            if identity == "O2":
                derived["comparison_baselines"] = {"events": {"observation_id": "O1", "derived_from": ["O1"]}}
                derived["known_items"] = [{"id": "quest/Quest_1", "derived_from": ["O1"]}]
            store.finish(wid, "role", tid, {"status": "ok", "observations": [original], "situation_views": [derived]})
            assert "source_hashes" not in derived
        pending = store.accept(wid, "role", "Next")
        restored = store.latest_situation(wid, "role", before_turn=pending)
        assert set(restored["source_hashes"]) == {"O1", "O2"}
        row = store.db.execute("SELECT payload FROM observations WHERE observation_id='O1'").fetchone()
        changed = json.loads(row["payload"])
        changed["received_at"] = "changed"
        payload = json.dumps(changed)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.db.execute("UPDATE observations SET payload=? WHERE observation_id='O1'", (payload,))
        # Fault injection is confined to this temporary test database. The
        # saved view must still detect damage that bypassed the write guard.
        with store.db:
            store.db.execute("DROP TRIGGER sealed_observation")
            store.db.execute("UPDATE observations SET payload=?,sha256=? WHERE observation_id='O1'",
                             (payload, hashlib.sha256(payload.encode()).hexdigest()))
        with pytest.raises(ValueError, match="changed since"):
            store.latest_situation(wid, "role", before_turn=pending)
    finally:
        store.close()
