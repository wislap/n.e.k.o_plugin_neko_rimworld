"""Identity, provenance and comparison boundaries of the consolidated board."""

import copy

import pytest

from plugin.plugins.neko_rimworld.agent.situation import board_view, compare_boards


def board(**sections):
    return {"map": {"id": "Map_0"}, "sample": {"colony_id": "colony", "load_epoch": "epoch", "finished_tick": 10}, "status": "ok", "conditions": [], "events": [], "threat_groups": [], "alerts": [], **sections}


def test_duplicate_native_identity_keeps_distinct_facts_and_raw_is_unchanged():
    a = {"id": "Condition_1", "label": "Cold", "ticks_left": 10}
    b = {**a, "ticks_left": 9}
    raw = board(conditions=[a, a, b])
    frozen = copy.deepcopy(raw)
    view = board_view(raw)
    assert raw == frozen
    assert len(view["items"]) == 1
    assert [source["data"] for source in view["items"][0]["sources"]] == [a, b]


def test_same_person_label_or_definition_does_not_merge_different_matters():
    raw = board(alerts=[{"type": "Hunger", "label": "Warning", "subjects": ["Pawn_1"]}, {"type": "Treatment", "label": "Warning", "subjects": ["Pawn_1"]}], events=[{"id": "Letter_1", "label": "Warning", "subjects": ["Pawn_1"]}])
    view = board_view(raw)
    assert len(view["items"]) == 3
    assert len([item for item in view["items"] if item["id"] is None]) == 2


def test_direct_quest_links_group_task_letters_but_not_independent_conditions():
    quest = {"id": "Quest_1", "state": "Ongoing", "name": "A task"}
    events = [{"id": "Letter_1", "tick": 1, "quest": quest}, {"id": "Letter_2", "tick": 5, "quest": quest}]
    alert = {"type": "QuestWarning", "label": "Task needs attention", "quest": quest}
    view = board_view(board(events=events, alerts=[alert], conditions=[{"id": "Condition_1", "quest": quest}]))
    assert len(view["items"]) == 2
    task = next(item for item in view["items"] if item["kind"] == "quest")
    assert task["id"] == "quest/Quest_1"
    assert [source["data"] for source in task["sources"]] == [*events, alert]


def test_target_membership_is_preserved_but_does_not_prove_same_incident():
    link = {"section": "threat_groups", "id": "Lord_1", "relation": "target_current_member"}
    assert len(board_view(board(threat_groups=[{"id": "Lord_1"}], events=[{"id": "Letter_1", "links": [link]}]))["items"]) == 2


def test_new_source_in_existing_task_is_reported_without_inventing_resolution():
    quest = {"id": "Quest_1", "state": "Ongoing"}
    old = board(events=[{"id": "Letter_1", "quest": quest}], conditions=[{"id": "Condition_1"}])
    current = board(events=[{"id": "Letter_1", "quest": quest}, {"id": "Letter_2", "quest": quest}])
    result = compare_boards(current, old, baseline_observation_id="old")
    assert result["available"] is True
    assert result["new_item_ids"] == []
    assert result["new_source_ids"] == ["events/Letter_2"]
    assert "removed" not in result and "resolved" not in result


def test_distinct_alert_instances_without_native_id_are_not_deduplicated():
    alert = {"type": "QuestWarning", "quest": {"id": "Quest_1", "state": "Ongoing"}}
    view = board_view(board(alerts=[alert, copy.deepcopy(alert)]))
    assert len(view["items"]) == 1 and len(view["items"][0]["sources"]) == 2
    assert view["items"][0]["label"] == "Task Quest_1"


def test_newly_available_quest_association_does_not_make_old_letter_a_new_matter():
    old = board(events=[{"id": "Letter_1"}])
    current = board(events=[{"id": "Letter_1", "quest": {"id": "Quest_1", "state": "Ongoing"}}])
    result = compare_boards(current, old)
    assert result["new_item_ids"] == [] and result["new_source_ids"] == []


@pytest.mark.parametrize("change,reason", [
    ({"sample": {"colony_id": "other", "load_epoch": "epoch", "finished_tick": 10}}, "session_changed"),
    ({"sample": {"colony_id": "colony", "load_epoch": "reload", "finished_tick": 10}}, "session_changed"),
    ({"map": {"id": "Map_1"}}, "map_changed"),
    ({"status": "partial"}, "incomplete_read"),
    ({"sample": {"colony_id": "colony", "load_epoch": "epoch", "finished_tick": 9}}, "clock_unavailable_or_rewound"),
])
def test_incomparable_baselines_do_not_mark_everything_new(change, reason):
    result = compare_boards({**board(), **change}, board())
    assert result["available"] is False and result["reason"] == reason
    assert result["new_item_ids"] == [] and result["new_source_ids"] == []


def test_first_read_has_no_new_markers_and_unknown_extensions_are_retained():
    raw = board(events=[], extension={"fact": 1}, alerts=None, alerts_unavailable={"reason": "not_ready"})
    view = board_view(raw)
    assert view["extension"] == raw["extension"] and view["alerts"] is None
    assert compare_boards(raw, None)["reason"] == "no_previous_read"


def test_partial_threats_do_not_block_positive_additions_in_complete_sources():
    old = board(conditions=[], events=[], threat_groups=None, threat_groups_unavailable={"reason": "unsupported_behavior_patch"})
    old["status"] = "partial"
    old["issues"] = [{"code": "situation_partial", "path": [], "message": "See explicit unavailable sections"}]
    current = copy.deepcopy(old)
    current["events"] = [{"id": "Letter_2", "label": "Raid"}]
    result = compare_boards(current, old)
    assert result["available"] is True
    assert result["compared_sections"] == ["conditions", "events", "alerts"]
    assert "threat_groups" in result["excluded_sections"]
    assert result["new_item_ids"] == ["events/Letter_2"]


def test_previously_unread_section_is_excluded_instead_of_all_new():
    old = board(conditions=[], events=None, events_unavailable={"reason": "read_failed"})
    old["status"] = "partial"
    current = board(conditions=[], events=[{"id": "Letter_1"}])
    result = compare_boards(current, old)
    assert result["available"] is True and "events" not in result["compared_sections"]
    assert result["new_item_ids"] == []


def test_unscoped_issues_do_not_masquerade_as_complete_sources():
    old = board(conditions=[], events=[])
    current = board(conditions=[{"id": "Condition_1"}], events=[], status="partial", issues=[{"message": "unknown coverage"}])
    assert compare_boards(current, old)["available"] is False
