"""Accumulate proved native windows for saved views, never for model autofetch.

Raw observations remain immutable. Every assembled fact retains the observation
IDs that supplied it; gaps, different snapshots and conflicting values fail
closed instead of establishing full coverage.
"""

from __future__ import annotations

import copy
import json

from .object_read_contract import canonical
from .tool_contracts import ToolExecutionError


def _error(message):
    return ToolExecutionError("conflicting_object_pages", message)


def _merge_sample(destination, source):
    """Retain the actual interval of accumulated evidence, not the last page."""
    for name, value in source.items():
        previous = destination.get(name)
        if name in ("colony_id", "load_epoch") and previous is not None and previous != value:
            raise _error("Sample identity changed inside one observed source")
        if name == "started_tick" and type(previous) is int and type(value) is int:
            destination[name] = min(previous, value)
        elif name in ("finished_tick", "verified_at_tick") and type(previous) is int and type(value) is int:
            destination[name] = max(previous, value)
        else:
            destination[name] = copy.deepcopy(value)
    # Joined intervals are observations over time, never an atomic snapshot.
    destination["atomic"] = False
    if "same_tick" in destination:
        destination["same_tick"] = destination.get("started_tick") == destination.get("finished_tick")


def _merge(destination, source):
    for name, value in source.items():
        if name in ("rows", "readings"):
            rows = destination.setdefault(name, [])
            for row in value:
                key = (lambda item: canonical(item.get("anchor", {}))) if name == "rows" else (
                    lambda item: canonical([item.get("field"), item.get("input_path"), item.get("snapshot_id")]))
                identity = key(row)
                old = next((item for item in rows if key(item) == identity), None)
                if old is None:
                    rows.append(copy.deepcopy(row))
                else:
                    _merge(old, row)
        elif name in ("values", "field_samples", "field_context", "field_semantics", "semantic_rules"):
            values = destination.setdefault(name, {})
            for column, observed in value.items():
                if column in values and values[column] != observed:
                    previous = values[column]
                    if name in ("values", "semantic_rules", "field_semantics") and _same_value_windows(previous, observed):
                        continue  # Independently validated windows of one sampled value.
                    raise _error(f"A sampled {name}.{column} changed inside one projection")
                values[column] = copy.deepcopy(observed)
        elif name in ("sample", "membership_sample") and isinstance(value, dict):
            _merge_sample(destination.setdefault(name, {}), value)
        elif name == "issues":
            issues = destination.setdefault(name, [])
            for issue in value:
                if issue not in issues:
                    issues.append(copy.deepcopy(issue))
        else:
            destination[name] = copy.deepcopy(value)


def _same_value_windows(left, right):
    if left == right:
        return True
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if left.get("value_snapshot_id"):
        return left["value_snapshot_id"] == right.get("value_snapshot_id")
    return left.keys() == right.keys() and all(_same_value_windows(left[key], right[key]) for key in left)


def _restore(value):
    if not isinstance(value, dict):
        return
    for row in value.get("rows", []):
        for reading in row.get("readings", []):
            _restore(reading)
    coverage = value.get("coverage", {})
    if value.get("page", {}).get("window_complete") is True:
        local = coverage.get("source_delivery_complete", coverage.get("source_display_complete"))
        if local is not None:
            coverage["display_complete"] = local is True and all(_row_display_complete(row) for row in value.get("rows", []))


def _row_display_complete(row):
    def complete(value):
        if isinstance(value, list):
            return all(complete(item) for item in value)
        if not isinstance(value, dict):
            return True
        if value.get("value_snapshot_id") and ("text" in value or "items" in value):
            if value.get("complete") is not True or value.get("offset", 0) != 0:
                return False
        return all(complete(item) for item in value.values())
    if not all(complete(value) for value in row.get("values", {}).values()):
        return False
    return all(reading.get("coverage", {}).get("display_complete") is True for reading in row.get("readings", []))


def _intervals_complete(intervals, end):
    at = 0
    for start, finish in sorted(intervals):
        if start != at or finish < start:
            return False
        at = finish
    return at == end


class ObjectReadResultAssembly:
    """Consultation-local materialization of already received evidence."""

    def __init__(self):
        self.projections = {}
        self.sources = {}
        self.values = {}
        self.value_projections = {}
        self.projection_views = {}

    def clear(self):
        self.projections.clear()
        self.sources.clear()
        self.values.clear()
        self.value_projections.clear()
        self.projection_views.clear()

    def observe(self, result):
        data = copy.deepcopy(result)
        source_ids = list(dict.fromkeys(data.get("source_observation_ids", [])))
        model_delivery = data.get("model_delivery", {})
        delivery = model_delivery if model_delivery.get("projection_id") else data.get("delivery", {})
        projection_id = delivery.get("projection_id")
        value_ids = self._observe_values(data)
        if projection_id:
            for value_id in value_ids:
                self.value_projections.setdefault(value_id, set()).add(projection_id)
        if projection_id:
            binding = canonical([data.get("scope"), data.get("sample", {}).get("load_epoch")])
            buffer = self.projections.setdefault(projection_id, {"binding": binding, "parts": {}, "ids": [], "total": None})
            if buffer["binding"] != binding:
                raise _error("A projection crossed its source scope or loaded world")
            start, end, total = (delivery.get(key) for key in ("leaf_start", "leaf_end", "total_leaves"))
            if type(start) is not int or type(end) is not int or start < 0 or end < start:
                raise _error("Invalid projection leaf interval")
            if total is not None:
                if type(total) is not int or total < end or buffer["total"] not in (None, total):
                    raise _error("Projection total changed")
                buffer["total"] = total
            semantic = copy.deepcopy(data)
            for key in ("observation_id", "source_observation_ids", "execution", "resume"):
                semantic.pop(key, None)
            old = buffer["parts"].get(start)
            if old is not None:
                if old[0] != end or not self._same_facts(old[1], semantic):
                    raise _error("Projection replay changed observed facts")
            else:
                if any(start < other_end and other_start < end for other_start, (other_end, _) in buffer["parts"].items()):
                    raise _error("Projection intervals overlap")
                buffer["parts"][start] = (end, semantic)
            buffer["ids"] = list(dict.fromkeys([*buffer["ids"], *source_ids]))
            data = {}
            at = 0
            for part_start, (part_end, part) in sorted(buffer["parts"].items()):
                if part_start != at:
                    break
                _merge(data, part)
                at = part_end
            if not data:
                return copy.deepcopy(result)
            _restore(data)
            data["assembly"] = {"projection_prefix_verified": True}
            data["source_observation_ids"] = buffer["ids"][:]
            data["observation_id"] = result.get("observation_id")
            data["delivery"] = {**data.get("delivery", {}), "complete": buffer["total"] == at,
                                "received_prefix": at, "projection_id": projection_id}
            self.projection_views[projection_id] = copy.deepcopy(data)
        elif value_ids:
            affected = set().union(*(self.value_projections.get(identity, set()) for identity in value_ids))
            if len(affected) == 1:
                view = self.projection_views.get(next(iter(affected)))
                if view is not None:
                    data = copy.deepcopy(view)
                    data["source_observation_ids"] = list(dict.fromkeys([*data.get("source_observation_ids", []), *source_ids]))
                    data["observation_id"] = result.get("observation_id")
        self._collect_sources(data)
        self._resolve_values(data, data.setdefault("source_observation_ids", []))
        _restore(data)
        return data

    def _observe_values(self, data):
        found = set()
        scope = canonical([data.get("scope"), data.get("sample", {}).get("load_epoch")])

        def visit(value, anchors=(), selection=(), column=None):
            if isinstance(value, list):
                for child in value:
                    visit(child, anchors, selection, column)
                return
            if not isinstance(value, dict):
                return
            if "anchor" in value:
                anchors = (*anchors, canonical(value["anchor"]))
            if "field" in value and "input_path" in value:
                selection = tuple(value["input_path"])
            identity = value.get("value_snapshot_id")
            if identity and ("text" in value or "items" in value):
                array = value.get("unit") == "items"
                if value.get("unit") not in {"UTF16_code_units", "items"}:
                    raise _error("Value window has no supported offset unit")
                encoding = value.get("encoding")
                if encoding not in (None, "json") or encoding == "json" and array:
                    raise _error("Value window has no supported encoding")
                json_type = value.get("json_type")
                if encoding == "json" and json_type not in ("array", "object") or encoding is None and json_type is not None:
                    raise _error("JSON value window has no supported bound type")
                start, width, total = (value.get(key) for key in ("offset", "returned_items" if array else "returned_chars", "total_items" if array else "total_chars"))
                raw = copy.deepcopy(value["items"]) if array else value["text"].encode("utf-16-le")
                if any(type(number) is not int for number in (start, width, total)) or not 0 <= start <= start + width <= total or len(raw) != width * (1 if array else 2):
                    raise _error("Invalid value window interval")
                binding = canonical([scope, anchors, selection, column, total, value["unit"], encoding, json_type])
                buffer = self.values.setdefault(identity, {"binding": binding, "parts": {}, "total": total, "ids": [], "array": array, "encoding": encoding, "json_type": json_type})
                if buffer["binding"] != binding:
                    raise _error("A value window changed its parent, column or source scope")
                old = buffer["parts"].get(start)
                if old is not None and old != raw:
                    raise _error("A value replay changed its sampled text")
                if old is None and any(start < offset + len(piece) // (1 if array else 2) and offset < start + width for offset, piece in buffer["parts"].items()):
                    raise _error("Value windows overlap")
                buffer["parts"][start] = raw
                buffer["ids"] = list(dict.fromkeys([*buffer["ids"], *data.get("source_observation_ids", [])]))
                found.add(identity)
            for name, child in value.items():
                if name == "values" and isinstance(child, dict):
                    for name, item in child.items():
                        visit(item, anchors, selection, name)
                elif name not in ("values", "anchor", "scope"):
                    visit(child, anchors, selection, column)

        visit(data)
        return found

    def _resolve_values(self, value, evidence):
        if isinstance(value, list):
            for index, child in enumerate(value):
                slot = {"item": child}
                self._resolve_values(slot, evidence)
                value[index] = slot["item"]
            return
        if not isinstance(value, dict):
            return
        for name, child in list(value.items()):
            identity = child.get("value_snapshot_id") if isinstance(child, dict) else None
            buffer = self.values.get(identity)
            if buffer is not None:
                intervals = [(offset, offset + len(piece) // (1 if buffer.get("array") else 2)) for offset, piece in buffer["parts"].items()]
                if _intervals_complete(intervals, buffer["total"]):
                    pieces = [piece for _, piece in sorted(buffer["parts"].items())]
                    value[name] = [item for piece in pieces for item in piece] if buffer.get("array") else b"".join(pieces).decode("utf-16-le")
                    if buffer.get("encoding") == "json":
                        try:
                            value[name] = json.loads(value[name], parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
                        except (ValueError, TypeError) as error:
                            raise _error("Sampled JSON value is invalid") from error
                        if type(value[name]) is not (list if buffer["json_type"] == "array" else dict):
                            raise _error("Sampled JSON value differs from its bound type")
                    evidence[:] = list(dict.fromkeys([*evidence, *buffer["ids"]]))
                    self._resolve_values(value[name], evidence)
                    continue
            self._resolve_values(child, evidence)

    @staticmethod
    def _same_facts(left, right):
        # Continuation handles and envelope observation IDs are not game facts.
        def facts(value):
            if isinstance(value, list):
                return [facts(item) for item in value]
            if not isinstance(value, dict):
                return value
            return {key: facts(item) for key, item in value.items() if key not in ("cursor", "resume")}
        return facts(left) == facts(right)

    def _collect_sources(self, data):
        if data.get("delivery", {}).get("cursor_scope") == "selection":
            return  # The proved projection prefix owns every source interval.
        epoch = data.get("sample", {}).get("load_epoch")
        scope = canonical(data.get("scope", {}))
        ids = data.get("source_observation_ids", [])

        def visit(row):
            for reading in row.get("readings", []):
                snapshot = reading.get("snapshot_id")
                page = reading.get("page", {})
                if snapshot and page.get("window_complete") is True:
                    key = canonical([epoch, scope, row.get("anchor"), reading.get("field"), reading.get("input_path"), snapshot])
                    buffer = self.sources.setdefault(key, {"parts": {}, "ids": []})
                    start, end = page.get("offset"), page.get("visited_through")
                    if type(start) is not int or type(end) is not int or not 0 <= start <= end:
                        raise _error("Invalid source membership interval")
                    piece = copy.deepcopy(reading)
                    previous = buffer["parts"].get(start)
                    if previous is not None and (previous[0] != end or not self._same_facts(previous[1], piece)):
                        raise _error("One source page changed its sampled values")
                    buffer["parts"][start] = (end, piece)
                    buffer["ids"] = list(dict.fromkeys([*buffer["ids"], *ids]))
                    captured = reading.get("coverage", {}).get("captured_members")
                    intervals = [(position, part[0]) for position, part in buffer["parts"].items()]
                    if type(captured) is int and _intervals_complete(intervals, captured):
                        merged = {}
                        for _, part in sorted(buffer["parts"].items()):
                            _merge(merged, part[1])
                        coverage = merged.setdefault("coverage", {})
                        pieces = [part[1].get("coverage", {}) for part in buffer["parts"].values()]
                        coverage["source_complete"] = all(piece.get("source_complete") is True for piece in pieces)
                        coverage["fields_complete"] = all(piece.get("fields_complete") is True for piece in pieces)
                        # A page's global predicate flag may be false solely
                        # because its later candidates have not been visited.
                        # Closed intervals prove only their own visited range.
                        coverage["predicate_complete"] = all(
                            piece.get("predicate_complete_for_visited", piece.get("predicate_complete")) is True
                            for piece in pieces)
                        coverage["predicate_complete_for_visited"] = coverage["predicate_complete"]
                        coverage["display_complete"] = True
                        coverage["source_delivery_complete"] = True
                        coverage["returned_count"] = len(merged.get("rows", []))
                        merged["page"] = {"offset": 0, "visited_through": captured, "window_complete": True,
                                          "has_more": False, "cursor": None, "returned_count": len(merged.get("rows", []))}
                        reading.clear()
                        reading.update(merged)
                        data["source_observation_ids"] = list(dict.fromkeys([*data.get("source_observation_ids", []), *buffer["ids"]]))
                for child in reading.get("rows", []):
                    visit(child)

        for row in data.get("rows", []):
            visit(row)
