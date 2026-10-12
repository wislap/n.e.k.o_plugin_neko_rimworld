"""Extract a saved native board without modifying its audit observation."""

import copy

SECTIONS = ("conditions", "threat_groups", "events", "alerts")


def _record(row):
    result = copy.deepcopy(row.get("values", {}))
    anchor = row.get("anchor", {})
    if "id" not in result and anchor.get("identity_scope") == "native":
        result["id"] = anchor.get("id")
    if "def_name" in result:
        result["def"] = result.pop("def_name")
    if "native_id" in anchor:
        result["id"] = anchor["native_id"]
    for reading in row.get("readings", []):
        field = reading.get("field")
        if field == "quest":
            children = reading.get("rows", [])
            if children:
                result["quest"] = _record(children[0])
        elif field in {"subjects", "members"}:
            result[field] = [_record(child) for child in reading.get("rows", [])]
    return result


def _complete(reading):
    coverage = reading.get("coverage", {})
    return (
        reading.get("status", "observed") not in {"unavailable", "unsupported", "selection_error"}
        and coverage.get("source_complete") is True
        and coverage.get("predicate_complete") is True
        and coverage.get("fields_complete") is True
        and coverage.get("display_complete") is True
        and reading.get("page", {}).get("offset", 0) == 0
        and reading.get("page", {}).get("has_more") is False
        and not any(_incomplete_row(row) for row in reading.get("rows", []))
    )


def _incomplete_row(row):
    if row.get("status") in {"unavailable", "selection_error"} or row.get("issues"):
        return True
    for value in row.get("values", {}).values():
        if isinstance(value, dict):
            if value.get("status") in {"unavailable", "unknown"} or value.get("complete") is False:
                return True
            if "total_chars" in value and (value.get("offset", 0) != 0 or value.get("returned_chars") != value["total_chars"]):
                return True
    return any(not _complete(reading) for reading in row.get("readings", []))


def _object_board(observation, payload):
    request = observation.get("request", {}).get("arguments", {})
    if request.get("target", {}).get("kind") != "situation":
        return None
    data = payload.get("data", {})
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        return None  # Transport fragments are original evidence, not a board.
    map_id = data.get("scope", {}).get("map")
    if not isinstance(map_id, str):
        return None
    board = {
        "map": {"id": map_id}, "sample": copy.deepcopy(data.get("sample", {})),
        "scope": {"source": "Selected native situation relations. Unselected sources were not read.",
                  "events": "Recent archived events plus directly linked pending/ongoing tasks while retained by Archive.",
                  "alerts": "Global cached UI alerts; not a fresh evaluation or selected-map complete scan."},
        "status": "ok", "read_only": True,
        "selection_scopes": copy.deepcopy(observation.get("selection_scopes", {})),
        "section_samples": {},
    }
    delivery_complete = data.get("delivery", {}).get("complete", True) is True
    selected = {}
    for row in data["rows"]:
        for reading in row.get("readings", []):
            field = reading.get("field")
            if field == "clock" and reading.get("rows"):
                board["clock"] = copy.deepcopy(reading["rows"][0].get("values", {}))
            elif field in SECTIONS:
                selected.setdefault(field, []).append(reading)
            elif field == "comparison" and reading.get("rows"):
                values = reading["rows"][0].get("values", {})
                board["comparison"] = copy.deepcopy(values.get("value", values))
    if isinstance(observation.get("comparison"), dict):
        board["comparison"] = copy.deepcopy(observation["comparison"])
    for section in SECTIONS:
        readings = selected.get(section, [])
        if not readings:
            board[section] = None
            board[section + "_unavailable"] = {"reason": "not_selected"}
            board["status"] = "partial"
            continue
        board[section] = [_record(row) for reading in readings for row in reading.get("rows", [])]
        samples = [reading.get("sample", {}) for reading in readings]
        if all(type(sample.get("started_tick")) is int and type(sample.get("finished_tick")) is int for sample in samples):
            board["section_samples"][section] = {
                "started_tick": min(sample["started_tick"] for sample in samples),
                "finished_tick": max(sample["finished_tick"] for sample in samples),
                "atomic": False, "granularity": "source_interval",
            }
        source_delivery_complete = delivery_complete or data.get("assembly", {}).get("projection_prefix_verified") is True and all(
            reading.get("page", {}).get("window_complete") is True for reading in readings)
        if not source_delivery_complete or len(readings) != 1 or not all(_complete(reading) for reading in readings):
            board[section + "_unavailable"] = {"reason": "incomplete_selected_source"}
            board["status"] = "partial"
    # This known aggregate warning permits comparison of the other complete
    # sections. Detailed issues stay inside their source-specific states.
    if board["status"] == "partial":
        board["issues"] = [{"code": "situation_partial", "path": [], "message": "Selected sources are incomplete or unselected."}]
    board["native_issues"] = copy.deepcopy(payload.get("issues", []))
    return board


def saved_board(observation):
    if not isinstance(observation, dict):
        return None
    response = observation.get("response", {})
    payload = response.get("payload", {})
    operation = observation.get("request", {}).get("operation")
    if operation == "read_objects":
        board = _object_board(observation, payload)
    elif operation == "read_situation":
        board = payload.get("data")
    else:
        return None
    if not isinstance(board, dict) or not isinstance(board.get("map"), dict):
        return None
    board = copy.deepcopy(board)
    board["sample"] = {
        **board.get("sample", {}),
        **{key: response[key] for key in ("colony_id", "load_epoch", "game_tick", "snapshot_sequence") if key in response},
    }
    for sample in board.get("section_samples", {}).values():
        sample.update({key: board["sample"][key] for key in ("colony_id", "load_epoch") if key in board["sample"]})
    if payload.get("status") != "ok" and operation == "read_situation":
        board["status"] = payload.get("status", "partial")
    if payload.get("issues") and operation == "read_situation":
        board["issues"] = copy.deepcopy(payload["issues"])
    return board
