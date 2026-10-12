"""Consolidate native situation sources without guessing event identity."""

from __future__ import annotations

import copy

SECTIONS = {
    "conditions": "condition",
    "threat_groups": "threat_group",
    "events": "event",
    "alerts": "alert",
}


def _identity(section, row):
    value = row.get("id") or (row.get("lord") if section == "threat_groups" else None)
    return f"{section}/{value}" if isinstance(value, str) and value else None


def board_view(board):
    """Keep every distinct source fact; consolidate only explicit identities/links.

    Letters and cached alerts directly referencing one Quest form one task
    entry, with all facts retained inside sources. Target membership, labels
    and faction do not establish that two observations describe the same matter.
    """
    view = copy.deepcopy(board)
    groups = {}
    entries = []
    for section, kind in SECTIONS.items():
        rows = view.get(section)
        if not isinstance(rows, list):
            continue
        del view[section]
        for row in rows:
            if not isinstance(row, dict):
                # Preserve future source extensions rather than dropping data.
                entries.append({"id": None, "kind": kind, "sources": [{"section": section, "id": None, "data": row}]})
                continue
            source_id = _identity(section, row)
            quest = row.get("quest")
            quest_id = quest.get("id") if section in {"events", "alerts"} and isinstance(quest, dict) else None
            linked_quest = isinstance(quest_id, str) and bool(quest_id)
            key = f"quest/{quest_id}" if linked_quest else source_id
            item = groups.get(key) if key is not None else None
            if item is None:
                item = {
                    "id": key,
                    "kind": "quest" if linked_quest else kind,
                    "label": (quest.get("name") or quest.get("label") or f"Task {quest_id}") if linked_quest else row.get("label"),
                    "sources": [],
                }
                if not item["label"]:
                    item["label"] = row.get("label") or row.get("lord_job") or row.get("type") or kind
                entries.append(item)
                if key is not None:
                    groups[key] = item
            source = {"section": section, "id": source_id, "data": row}
            if source_id is None or source not in item["sources"]:
                item["sources"].append(source)
    view["items"] = entries
    view["view_meaning"] = (
        "One entry per directly linked task or native source identity; sources retain every distinct native fact and reminder. "
        "Direct Quest links group task letters and alerts. Other target relations are not event identity. "
        "Alerts are cached UI signals, not a complete problem or safety assessment."
    )
    return view


def _tick(board):
    sample = board.get("sample", {})
    return sample.get("finished_tick", sample.get("game_tick", board.get("clock", {}).get("ticks_game")))


def _complete_sections(board):
    unavailable = {key for key in board if key.endswith("_unavailable")}
    known = {f"{section}_unavailable" for section in SECTIONS} | {"quest_state_unavailable"}
    if board.get("status") not in {"ok", "partial"} or unavailable - known:
        return set()
    # The native aggregate warning summarizes the explicit unavailable fields;
    # it does not invalidate every otherwise complete section. Unknown issues do.
    if any(not isinstance(issue, dict) or issue.get("code") != "situation_partial" or issue.get("path") or not unavailable for issue in board.get("issues", [])):
        return set()
    if board.get("status") == "partial" and not unavailable:
        return set()  # An unscoped partial cannot establish a complete section.
    return {
        section for section in SECTIONS
        if isinstance(board.get(section), list)
        and f"{section}_unavailable" not in unavailable
        and not (section in {"events", "alerts"} and "quest_state_unavailable" in unavailable)
    }


def compare_boards(current, previous, *, baseline_observation_id=None):
    """Positive additions since an identified comparable observation, never resolution."""
    result = {"available": False, "new_item_ids": [], "new_source_ids": []}
    if previous is None:
        return {**result, "reason": "no_previous_read"}
    for field in ("colony_id", "load_epoch"):
        identity = current.get("sample", {}).get(field)
        if not identity or identity != previous.get("sample", {}).get(field):
            return {**result, "reason": "session_changed"}
    map_id = current.get("map", {}).get("id")
    if not map_id or map_id != previous.get("map", {}).get("id"):
        return {**result, "reason": "map_changed"}
    compared = _complete_sections(current) & _complete_sections(previous)
    if not compared:
        return {**result, "reason": "incomplete_read"}
    now, before = _tick(current), _tick(previous)
    if not isinstance(now, int) or not isinstance(before, int) or now < before:
        return {**result, "reason": "clock_unavailable_or_rewound"}
    current_view, previous_view = (board_view({section: board[section] for section in SECTIONS if section in compared}) for board in (current, previous))
    old_items = {item["id"] for item in previous_view["items"] if item["id"] is not None}
    old_sources = {source["id"] for item in previous_view["items"] for source in item["sources"] if source["id"] is not None}
    return {
        "available": True,
        "baseline_observation_id": baseline_observation_id,
        "baseline_tick": before,
        "compared_sections": [section for section in SECTIONS if section in compared],
        "excluded_sections": [section for section in SECTIONS if section not in compared],
        "new_item_ids": [item["id"] for item in current_view["items"] if item["id"] is not None and item["id"] not in old_items and not any(source["id"] in old_sources for source in item["sources"] if source["id"] is not None)],
        "new_source_ids": list(dict.fromkeys(source["id"] for item in current_view["items"] for source in item["sources"] if source["id"] is not None and source["id"] not in old_sources)),
        "meaning": "Only stable native identities in sections complete in both reads are compared; excluded sections and ID-less cached alerts are not covered. New since the baseline read, not necessarily newly occurred. Missing entries do not establish resolution.",
    }


def section_board(board, section):
    """One comparable source with its original sample, never mixed timestamps."""
    result = {key: copy.deepcopy(board[key]) for key in ("map", "sample", "clock", "scope", "selection_scopes") if key in board}
    if section in board.get("section_samples", {}):
        result["sample"] = copy.deepcopy(board["section_samples"][section])
    result.update(status="partial", issues=[{"code": "situation_partial", "path": [], "message": "Other sources were not selected for this comparison."}])
    for name in SECTIONS:
        result[name] = copy.deepcopy(board.get(name)) if name == section else None
        if name != section or name + "_unavailable" in board:
            result[name + "_unavailable"] = copy.deepcopy(board.get(name + "_unavailable", {"reason": "not_selected"}))
    return result


def compare_saved_sections(current, baselines):
    """Use each source's last comparable observation; partial reads don't replace it."""
    result = {"available": False, "new_item_ids": [], "new_source_ids": [], "compared_sections": [],
              "excluded_sections": list(SECTIONS), "baselines": {}}
    # A task previously observed in another source is an existing matter.
    # Quest identity is colony-wide; map-local source identities are not.
    known_items = set()
    for saved in baselines.values():
        previous = saved["board"]
        if any(current.get("sample", {}).get(key) != previous.get("sample", {}).get(key)
               for key in ("colony_id", "load_epoch")):
            continue
        same_map = current.get("map", {}).get("id") == previous.get("map", {}).get("id")
        known_items.update(item["id"] for item in board_view(previous)["items"]
                           if item["id"] is not None and (same_map or item["kind"] == "quest"))
    for section in SECTIONS:
        if section not in _complete_sections(current):
            continue
        saved = baselines.get(section)
        if not saved:
            continue
        previous, observation_id = saved["board"], saved["observation_id"]
        if previous.get("selection_scopes", {}).get(section) != current.get("selection_scopes", {}).get(section):
            continue
        compared = compare_boards(section_board(current, section), previous, baseline_observation_id=observation_id)
        if not compared["available"]:
            continue
        result["compared_sections"].append(section)
        result["baselines"][section] = {"observation_id": observation_id, "tick": compared.get("baseline_tick")}
        for field in ("new_item_ids", "new_source_ids"):
            additions = [identity for identity in compared[field] if field != "new_item_ids" or identity not in known_items]
            result[field] = list(dict.fromkeys([*result[field], *additions]))
    result["available"] = bool(result["compared_sections"])
    result["excluded_sections"] = [section for section in SECTIONS if section not in result["compared_sections"]]
    if not result["available"]:
        result["reason"] = "no_comparable_saved_source"
    else:
        identities = {value["observation_id"] for value in result["baselines"].values()}
        ticks = {value["tick"] for value in result["baselines"].values()}
        result["baseline_observation_id"] = next(iter(identities)) if len(identities) == 1 else None
        result["baseline_tick"] = next(iter(ticks)) if len(ticks) == 1 else None
        result["meaning"] = "New since each section's identified comparable saved read; excluded sources are not covered and missing items do not establish resolution."
    return result
