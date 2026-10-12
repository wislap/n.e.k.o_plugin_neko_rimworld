"""Saved materialization requires complete intervals and stable identities."""

import copy

import pytest

from ..agent.object_read_results import ObjectReadResultAssembly
from ..agent.tool_contracts import ToolExecutionError


def packet(projection, start, end, identities, *, total=None, offset=0, visited=2, more=False):
    complete = {"source_complete": True, "predicate_complete": True, "fields_complete": True,
                "display_complete": False, "source_display_complete": not more, "captured_members": 2}
    return {"scope": {"map": "Map_0"}, "sample": {"load_epoch": "E1", "finished_tick": 10},
            "source_observation_ids": [f"O-{projection}-{start}"], "observation_id": f"O-{projection}-{start}",
            "rows": [{"anchor": {"id": "Map_0/situation"}, "readings": [{"field": "events", "input_path": [0],
                "snapshot_id": "events-S1", "coverage": complete,
                "page": {"offset": offset, "visited_through": visited, "window_complete": True, "has_more": more},
                "rows": [{"anchor": {"id": identity, "identity_scope": "native"}, "values": {"label": identity}}
                         for identity in identities]}]}],
            "delivery": {"projection_id": projection, "leaf_start": start, "leaf_end": end, "total_leaves": total,
                         "end_of_projection": total == end, "complete": start == 0 and total == end}}


def events(data):
    return data["rows"][0]["readings"][0]


def test_structural_chunks_join_without_mutating_originals():
    first = packet("P1", 0, 1, ["Letter1"])
    # The source window itself closes only after its last DTO chunk.
    events(first)["page"]["window_complete"] = False
    last = packet("P1", 1, 2, ["Letter2"], total=2)
    original = copy.deepcopy([first, last])
    assembly = ObjectReadResultAssembly()
    assert assembly.observe(first)["delivery"]["complete"] is False
    full = assembly.observe(last)
    assert full["delivery"]["complete"] is True
    assert events(full)["coverage"]["display_complete"] is True
    assert [row["values"]["label"] for row in events(full)["rows"]] == ["Letter1", "Letter2"]
    assert full["source_observation_ids"] == ["O-P1-0", "O-P1-1"]
    assert [first, last] == original


def test_tail_only_and_missing_intervals_never_establish_complete_projection():
    assembly = ObjectReadResultAssembly()
    tail = assembly.observe(packet("P1", 1, 2, ["Letter2"], total=2))
    assert tail["delivery"]["complete"] is False
    assert "assembly" not in tail


def test_membership_pages_join_only_the_same_snapshot():
    assembly = ObjectReadResultAssembly()
    first = packet("P1", 0, 1, ["Letter1"], total=1, visited=1, more=True)
    second = packet("P2", 0, 1, ["Letter2"], total=1, offset=1, visited=2)
    assembly.observe(first)
    full = assembly.observe(second)
    assert [row["values"]["label"] for row in events(full)["rows"]] == ["Letter1", "Letter2"]
    assert events(full)["page"]["offset"] == 0
    assert events(full)["page"]["has_more"] is False
    assert set(full["source_observation_ids"]) == {"O-P1-0", "O-P2-0"}
    different = packet("P3", 0, 1, ["Letter3"], total=1, offset=1, visited=2)
    events(different)["snapshot_id"] = "events-S2"
    assert len(events(assembly.observe(different))["rows"]) == 1


def test_projection_scope_changes_and_conflicting_replays_are_rejected():
    assembly = ObjectReadResultAssembly()
    first = packet("P1", 0, 1, ["Letter1"])
    events(first)["page"]["window_complete"] = False
    assembly.observe(first)
    changed = copy.deepcopy(first)
    changed["scope"]["map"] = "Map_1"
    with pytest.raises(ToolExecutionError, match="loaded world"):
        assembly.observe(changed)
    changed = copy.deepcopy(first)
    events(changed)["rows"][0]["values"]["label"] = "Different fact"
    with pytest.raises(ToolExecutionError, match="replay changed"):
        assembly.observe(changed)


def text_window(identity, text, offset, total):
    width = len(text.encode("utf-16-le")) // 2
    return {"value_snapshot_id": identity, "text": text, "offset": offset, "returned_chars": width,
            "total_chars": total, "unit": "UTF16_code_units", "complete": offset == 0 and offset + width == total,
            "end_of_value": offset + width == total}


def test_text_windows_restore_the_saved_projection_only_after_full_same_value_coverage():
    assembly = ObjectReadResultAssembly()
    first = packet("P1", 0, 1, ["Letter1"], total=1)
    events(first)["rows"][0]["values"]["label"] = text_window("V1", "AB👾", 0, 8)
    incomplete = assembly.observe(first)
    assert isinstance(events(incomplete)["rows"][0]["values"]["label"], dict)
    tail = copy.deepcopy(first)
    tail["delivery"] = {"kind": "text_window", "complete": False}
    tail["source_observation_ids"] = ["O-text-tail"]
    events(tail)["rows"][0]["values"]["label"] = text_window("V1", "CDGH", 4, 8)
    full = assembly.observe(tail)
    assert events(full)["rows"][0]["values"]["label"] == "AB👾CDGH"
    assert set(full["source_observation_ids"]) == {"O-P1-0", "O-text-tail"}
    assert isinstance(events(first)["rows"][0]["values"]["label"], dict)


def test_equal_length_different_value_snapshots_cannot_be_joined():
    assembly = ObjectReadResultAssembly()
    first = packet("P1", 0, 1, ["Letter1"], total=1)
    events(first)["rows"][0]["values"]["label"] = text_window("V1", "ABCD", 0, 8)
    assembly.observe(first)
    tail = copy.deepcopy(first)
    tail["delivery"] = {"kind": "text_window", "complete": False}
    events(tail)["rows"][0]["values"]["label"] = text_window("V2", "5678", 4, 8)
    with pytest.raises(ToolExecutionError, match="changed its sampled values"):
        assembly.observe(tail)


@pytest.mark.parametrize("flag", ["source_complete", "fields_complete", "predicate_complete_for_visited"])
def test_later_success_never_erases_failure_in_an_earlier_closed_source_window(flag):
    assembly = ObjectReadResultAssembly()
    first = packet("P1", 0, 1, ["Letter1"], total=1, visited=1, more=True)
    events(first)["coverage"][flag] = False
    second = packet("P2", 0, 1, ["Letter2"], total=1, offset=1, visited=2)
    events(second)["coverage"][flag] = True
    originals = copy.deepcopy([first, second])
    assembly.observe(first)
    coverage = events(assembly.observe(second))["coverage"]
    expected = "predicate_complete" if flag == "predicate_complete_for_visited" else flag
    assert coverage[expected] is False
    assert coverage["display_complete"] is True
    assert [first, second] == originals


def test_visited_predicate_proof_can_complete_without_taking_the_first_pages_global_flag():
    assembly = ObjectReadResultAssembly()
    first = packet("P1", 0, 1, ["Letter1"], total=1, visited=1, more=True)
    events(first)["coverage"].update(predicate_complete=False, predicate_complete_for_visited=True)
    second = packet("P2", 0, 1, ["Letter2"], total=1, offset=1, visited=2)
    events(second)["coverage"]["predicate_complete_for_visited"] = True
    assembly.observe(first)
    assert events(assembly.observe(second))["coverage"]["predicate_complete"] is True


def test_joined_samples_retain_earliest_start_and_latest_finish_without_rewriting_value_samples():
    assembly = ObjectReadResultAssembly()
    first = packet("P1", 0, 1, ["Letter1"], total=1, visited=1, more=True)
    second = packet("P2", 0, 1, ["Letter2"], total=1, offset=1, visited=2)
    for part, start, end in ((first, 10, 11), (second, 20, 21)):
        events(part)["sample"] = {"load_epoch": "E1", "started_tick": start, "finished_tick": end, "atomic": False}
        events(part)["rows"][0]["field_samples"] = {"label": {"started_tick": start, "finished_tick": end}}
    assembly.observe(first)
    source = events(assembly.observe(second))
    assert source["sample"] == {"load_epoch": "E1", "started_tick": 10, "finished_tick": 21, "atomic": False}
    assert [row["field_samples"]["label"]["finished_tick"] for row in source["rows"]] == [11, 21]


def test_three_text_windows_keep_the_middle_original_evidence_in_the_completed_value():
    assembly = ObjectReadResultAssembly()
    first = packet("P1", 0, 1, ["Letter1"], total=1)
    events(first)["rows"][0]["values"]["label"] = text_window("V1", "ABCD", 0, 12)
    assembly.observe(first)
    for text, offset, observation in (("EFGH", 4, "middle"), ("IJKL", 8, "tail")):
        part = copy.deepcopy(first)
        part["delivery"] = {"kind": "text_window", "complete": False}
        part["source_observation_ids"] = [observation]
        events(part)["rows"][0]["values"]["label"] = text_window("V1", text, offset, 12)
        result = assembly.observe(part)
    assert events(result)["rows"][0]["values"]["label"] == "ABCDEFGHIJKL"
    assert set(result["source_observation_ids"]) == {"O-P1-0", "middle", "tail"}


def test_complete_parent_membership_does_not_prove_selected_children_are_delivered():
    first = packet("P1", 0, 1, ["Letter1"], total=1, visited=1, more=True)
    second = packet("P2", 0, 1, ["Letter2"], total=1, offset=1, visited=2)
    events(first)["rows"][0]["readings"] = [{
        "field": "subjects", "input_path": [0, 0], "rows": [],
        "coverage": {"display_complete": False}, "page": {"has_more": True, "cursor": "remaining"}}]
    assembly = ObjectReadResultAssembly()
    assembly.observe(first)
    result = events(assembly.observe(second))
    assert result["coverage"]["source_delivery_complete"] is True
    assert result["coverage"]["display_complete"] is False


def test_explanations_accumulate_across_pages_and_long_meaning_keeps_all_originals():
    assembly = ObjectReadResultAssembly()
    first = packet("P1", 0, 1, ["Letter1"])
    events(first)["page"]["window_complete"] = False
    first["semantic_rules"] = {"Event.label": {"meaning": text_window("meaning-V1", "ABCD", 0, 12)}}
    assembly.observe(first)
    second = packet("P1", 1, 2, ["Letter2"], total=2)
    second["semantic_rules"] = {"Event.tick": {"unit": "game_ticks"}}
    full = assembly.observe(second)
    assert set(full["semantic_rules"]) == {"Event.label", "Event.tick"}
    for text, offset, identity in (("EFGH", 4, "meaning-middle"), ("IJKL", 8, "meaning-tail")):
        result = {"scope": first["scope"], "sample": first["sample"], "rows": [],
                  "semantic_rules": {"Event.label": {"meaning": text_window("meaning-V1", text, offset, 12)}},
                  "delivery": {"kind": "text_window", "complete": False}, "source_observation_ids": [identity]}
        full = assembly.observe(result)
    assert full["semantic_rules"] == {"Event.label": {"meaning": "ABCDEFGHIJKL"}, "Event.tick": {"unit": "game_ticks"}}
    assert set(full["source_observation_ids"]) == {"O-P1-0", "O-P1-1", "meaning-middle", "meaning-tail"}


def test_unified_projection_accumulates_array_windows_before_claiming_full_value():
    assembly = ObjectReadResultAssembly()
    for index, items in enumerate((["a", "b"], ["c", "d"], ["e", "f"])):
        part = packet("unified", index, index + 1, ["Letter1"], total=3 if index == 2 else None)
        part["delivery"]["cursor_scope"] = "selection"
        events(part)["rows"][0]["values"]["label"] = {"value_snapshot_id": "array-V1", "items": items,
            "offset": index * 2, "returned_items": 2, "total_items": 6, "unit": "items", "complete": False}
        current = assembly.observe(part)
        if index < 2:
            assert isinstance(events(current)["rows"][0]["values"]["label"], dict)
    assert events(current)["rows"][0]["values"]["label"] == ["a", "b", "c", "d", "e", "f"]
    assert len(current["source_observation_ids"]) == 3
