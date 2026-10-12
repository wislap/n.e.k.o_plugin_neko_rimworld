"""Observation ordering, frozen baselines and cross-source task identity."""

import copy

from ..agent.situation import compare_saved_sections, section_board
from ..agent.situation_history import SituationHistory


def board(tick, *, events=None, alerts=None, map_id="Map_0"):
    return {"map": {"id": map_id}, "sample": {"colony_id": "colony", "load_epoch": "epoch", "finished_tick": tick},
            "status": "ok", "conditions": [], "threat_groups": [],
            "events": events or [], "alerts": alerts or []}


def baseline(history, current, observation):
    read = history.begin()
    history.compare(read, current, observation, "first")
    return read


def test_replay_keeps_its_comparison_and_does_not_replace_baseline_with_replay_envelope():
    history = SituationHistory()
    baseline(history, board(10), "O0")
    current = board(20, events=[{"id": "Letter2"}])
    read = history.begin()
    expected = history.compare(read, current, "O1", "cursor1")
    expected["new_source_ids"].append("caller mutation")
    replay = history.compare(read, current, "O-replay", "cursor1")
    assert replay["baseline_observation_id"] == "O0"
    assert replay["new_source_ids"] == ["events/Letter2"]
    assert history.export_baselines()["events"]["observation_id"] == "O1"


def test_read_freezes_baseline_before_later_reads_and_late_completion_cannot_reorder_history():
    history = SituationHistory()
    baseline(history, board(10), "O0")
    older = history.begin()
    newer = history.begin()
    history.compare(newer, board(30, events=[{"id": "Letter3"}]), "O3", "first")
    comparison = history.compare(older, board(40, events=[{"id": "Letter2"}]), "O2-late", "tail")
    assert comparison["baseline_observation_id"] == "O0"
    assert comparison["new_source_ids"] == ["events/Letter2"]
    assert history.export_baselines()["events"]["observation_id"] == "O3"


def test_partial_window_cannot_become_a_comparable_baseline_but_its_complete_tail_can():
    history = SituationHistory()
    baseline(history, board(10), "O0")
    read = history.begin()
    partial = board(20, events=[{"id": "Letter2"}])
    partial.update(status="partial", events_unavailable={"reason": "remaining_window"})
    history.compare(read, partial, "O1-prefix", "first")
    assert history.export_baselines()["events"]["observation_id"] == "O0"
    complete = history.compare(read, board(21, events=[{"id": "Letter2"}]), "O1-tail", "tail")
    assert complete["new_source_ids"] == ["events/Letter2"]
    assert history.export_baselines()["events"]["observation_id"] == "O1-tail"


def test_new_event_source_for_a_quest_known_from_alerts_is_not_a_new_matter():
    quest = {"id": "Quest42", "state": "Ongoing"}
    old = board(10, alerts=[{"type": "Reminder", "quest": quest}])
    baselines = {section: {"board": section_board(old, section), "observation_id": "O0"}
                 for section in ("events", "alerts")}
    current = board(20, events=[{"id": "Letter42", "quest": quest}])
    result = compare_saved_sections(current, baselines)
    assert result["new_source_ids"] == ["events/Letter42"]
    assert result["new_item_ids"] == []


def test_quest_positive_evidence_is_colony_wide_but_conditions_are_map_local():
    quest = {"id": "Quest42", "state": "Ongoing"}
    map_a = board(10, alerts=[{"type": "Reminder", "quest": quest}], map_id="Map_A")
    map_a["conditions"] = [{"id": "Condition1"}]
    map_b = board(10, map_id="Map_B")
    baselines = {"alerts": {"board": section_board(map_a, "alerts"), "observation_id": "A"},
                 "events": {"board": section_board(map_b, "events"), "observation_id": "B"},
                 "conditions": {"board": section_board(map_b, "conditions"), "observation_id": "B"}}
    current = board(20, events=[{"id": "Letter42", "quest": quest}], map_id="Map_B")
    current["conditions"] = [{"id": "Condition1"}]
    result = compare_saved_sections(current, baselines)
    assert result["new_item_ids"] == ["conditions/Condition1"]
    assert result["new_source_ids"] == ["conditions/Condition1", "events/Letter42"]


def test_lazy_restore_imports_only_retained_history_into_an_already_started_read():
    history = SituationHistory()
    read = history.begin()
    previous = board(10)
    original = copy.deepcopy(previous)
    history.restore(previous, {"observation_id": "retained"})
    result = history.compare(read, board(20, events=[{"id": "Letter2"}]), "current", "first")
    assert result["baseline_observation_id"] == "retained"
    assert result["new_source_ids"] == ["events/Letter2"]
    assert previous == original


def test_late_saved_observation_cannot_replace_a_newer_exported_baseline_after_restart():
    history = SituationHistory()
    older, newer = history.begin(), history.begin()
    history.compare(newer, board(20), "newer", "first")
    late = board(30, events=[{"id": "Letter2"}])
    history.compare(older, late, "older-late", "first")
    restarted = SituationHistory()
    restarted.restore(late, {"observation_id": "older-late", "comparison_baselines": history.export_baselines()})
    read = restarted.begin()
    result = restarted.compare(read, board(40), "next", "first")
    assert result["baseline_observation_id"] == "newer"


def test_seen_task_is_not_new_after_disappearing_from_latest_sources_and_after_restart():
    history = SituationHistory()
    quest = {"id": "Quest42", "state": "Ongoing"}
    baseline(history, board(10, events=[{"id": "Letter42", "quest": quest}]), "seen")
    empty = board(20)
    baseline(history, empty, "empty")
    restored = SituationHistory()
    restored.restore(empty, {"observation_id": "empty", "comparison_baselines": history.export_baselines(),
                             "known_items": history.export_known_items()})
    read = restored.begin()
    result = restored.compare(read, board(30, alerts=[{"id": "Alert42", "quest": quest}]), "again", "first")
    assert result["new_source_ids"] == ["alerts/Alert42"]
    assert result["new_item_ids"] == []
    assert any(item["id"] == "quest/Quest42" and item["derived_from"] == ["seen"] for item in restored.export_known_items())
