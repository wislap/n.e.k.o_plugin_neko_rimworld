"""Lossless final look pages, including plugin-derived facts and tool wrapping."""

from __future__ import annotations

import copy
import json
import time
import uuid

from .object_read_results import _merge
from .tool_contracts import ToolExecutionError


def encoded_size(data):
    return len(json.dumps({"ok": True, "tool": "look", "data": data}, ensure_ascii=False,
                          allow_nan=False, separators=(",", ":")).encode("utf-8"))


_CONTEXT = {"scope", "context", "sample", "version", "status", "observation_id", "source_observation_ids",
            "source_granularity", "anchor", "field", "input_path", "snapshot_id", "membership_sample"}
_MAPPINGS = {"values", "field_samples", "field_context", "field_semantics", "semantic_rules"}


def _branch(root, path, replacement):
    def visit(value, depth):
        if depth == len(path):
            return copy.deepcopy(replacement)
        key = path[depth]
        if isinstance(value, list):
            return [visit(value[key], depth + 1)]
        result = {name: copy.deepcopy(item) for name, item in value.items() if name in _CONTEXT}
        result[key] = visit(value[key], depth + 1)
        if key == "values" and depth + 1 < len(path):
            column = path[depth + 1]
            for name in _MAPPINGS - {"values", "semantic_rules"}:
                if column in value.get(name, {}):
                    result[name] = {column: copy.deepcopy(value[name][column])}
        return result
    return visit(root, 0)


def _atoms(root, fits):
    def text_parts(text, path, *, identity=None, offset=0, total=None, unit="UTF16_code_units", metadata=None):
        identity = identity or uuid.uuid4().hex
        total = total if total is not None else len(text.encode("utf-16-le")) // 2
        at = 0
        while at < len(text):
            def atom(count):
                piece = text[at:at + count]
                width = len(piece.encode("utf-16-le")) // 2 if unit == "UTF16_code_units" else len(piece)
                return _branch(root, path, {**(metadata or {}), "value_snapshot_id": identity, "text": piece,
                    "offset": offset, "returned_chars": width, "total_chars": total, "unit": unit,
                    "complete": offset == 0 and width == total, "end_of_value": offset + width == total,
                    "has_more": offset + width < total}), width
            low, high = 1, min(80000, len(text) - at)
            best = None
            while low <= high:
                middle = (low + high) // 2
                candidate, width = atom(middle)
                if fits(candidate):
                    best = (candidate, middle, width)
                    low = middle + 1
                else:
                    high = middle - 1
            if best is None:
                raise ToolExecutionError("object_atom_too_large", "One text character and its required identity exceed the final output window")
            candidate, count, width = best
            yield candidate
            at += count
            offset += width

    def json_parts(value, path):
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        yield from text_parts(text, path, metadata={"encoding": "json", "json_type": "array" if isinstance(value, list) else "object"})

    def walk(value, path):
        if isinstance(value, dict):
            if path and path[-1] in {"values", "semantic_rules"}:
                for name, child in value.items():
                    yield from value_parts(child, (*path, name))
                return
            for name, child in value.items():
                if name in _CONTEXT or name in {"page", "resume", "delivery", "execution"}:
                    continue
                if name in {"rows", "readings"}:
                    for index, row in enumerate(child):
                        yield _branch(root, (*path, name, index), {key: copy.deepcopy(item) for key, item in row.items() if key in _CONTEXT})
                        yield from walk(row, (*path, name, index))
                elif name in _MAPPINGS:
                    if name in {"values", "semantic_rules"}:
                        yield from walk(child, (*path, name))
                    elif name == "field_semantics" and not path:
                        for column, meaning in child.items():
                            yield from value_parts(meaning, (name, column))
                else:
                    yield from value_parts(child, (*path, name))
        else:
            yield from value_parts(value, path)

    def value_parts(value, path):
        if isinstance(value, dict) and isinstance(value.get("text"), str) and "offset" in value and not fits(_branch(root, path, value)):
            yield from text_parts(value["text"], path, identity=value.get("value_snapshot_id"), offset=value["offset"],
                total=value.get("total_chars"), unit=value.get("unit", "UTF16_code_units"),
                metadata={key: value[key] for key in ("encoding", "json_type") if key in value})
        elif isinstance(value, str) and not fits(_branch(root, path, value)):
            yield from text_parts(value, path)
        elif isinstance(value, list) and len(value) > 20 and path[-1] not in {"issues"}:
            # Window a single selected JSON value, never the tool packet. An
            # oversized nested item cannot be made smaller by item count.
            if any(not fits(_branch(root, path, {"value_snapshot_id": "0" * 32, "items": [item],
                "offset": len(value) - 1, "returned_items": 1, "total_items": len(value), "unit": "items",
                "complete": False, "end_of_value": False, "has_more": False})) for item in value):
                yield from json_parts(value, path)
                return
            identity = uuid.uuid4().hex
            offset = 0
            while offset < len(value):
                count = min(1024, len(value) - offset)
                while True:
                    items = value[offset:offset + count]
                    atom = _branch(root, path, {"value_snapshot_id": identity, "items": copy.deepcopy(items),
                        "offset": offset, "returned_items": count, "total_items": len(value), "unit": "items",
                        "complete": offset == 0 and count == len(value), "end_of_value": offset + count == len(value),
                        "has_more": offset + count < len(value)})
                    if fits(atom) or count == 1:
                        break
                    count = max(1, count // 2)
                yield atom
                offset += count
        elif isinstance(value, (dict, list)) and not fits(_branch(root, path, value)):
            yield from json_parts(value, path)
        elif isinstance(value, list) and path[-1] == "issues":
            for item in value:
                yield _branch(root, path, [item])
        else:
            yield _branch(root, path, value)
    yield from walk(root, ())


class ObjectReadDelivery:
    def __init__(self, budget=80000):
        self.budget = budget
        self.pages = {}
        self.legacy_pages = {}

    def clear(self):
        self.pages.clear()
        self.legacy_pages.clear()

    def coordinate_legacy(self, result, callback, identity=None, pending=()):
        data = copy.deepcopy(result)
        tokens = list(pending)

        def remove(value):
            if isinstance(value, dict):
                token = value.pop("cursor", None)
                if isinstance(token, str) and token and token not in tokens:
                    tokens.append(token)
                value.pop("resume", None)
                for child in value.values():
                    remove(child)
            elif isinstance(value, list):
                for child in value:
                    remove(child)

        remove(data)
        token = uuid.uuid4().hex if tokens else None
        if token:
            self.legacy_pages[token] = {"tokens": tuple(tokens), "callback": callback,
                                        "identity": copy.deepcopy(identity), "expires": time.monotonic() + 300}
        data["page"] = {"has_more": bool(token), "cursor": token}
        if token:
            data["resume"] = {"page": {"cursor": token}}
        return data

    def resume_legacy(self, token, identity=None):
        state = self.legacy_pages[token]
        if state["identity"] != identity or state["expires"] < time.monotonic():
            raise ToolExecutionError("cursor_expired", "Compatibility cursor expired or its source identity changed")
        if "cached" not in state:
            first, *pending = state["tokens"]
            result = state["callback"]({"cursor": first})
            state["cached"] = self.coordinate_legacy(result, state["callback"], identity, pending)
        return copy.deepcopy(state["cached"])

    def _envelope(self, body, *, token, more, native, position=None):
        result = copy.deepcopy(body)
        result["model_delivery"] = {"complete": not more, "meaning": "Selected facts continued without another game read."}
        if position:
            result["model_delivery"].update(position)
        continuation = token if more else native.get("page", {}).get("cursor")
        result["page"] = {"has_more": bool(continuation), "cursor": continuation}
        if continuation:
            result["resume"] = {"page": {"cursor": continuation}}
        result["delivery"] = copy.deepcopy(native.get("delivery", {}))
        if "execution" in native:
            result["execution"] = copy.deepcopy(native["execution"])
        if more:
            result["delivery"].update(complete=False, end_of_projection=False)
        for node in _dicts(result):
            coverage = node.get("coverage")
            if isinstance(coverage, dict) and "display_complete" in coverage:
                coverage["native_display_complete"] = coverage["display_complete"]
                coverage["display_complete"] = False
        return result

    def deliver(self, result, identity=None):
        if encoded_size(result) <= self.budget:
            return result
        def fits(atom):
            return encoded_size(self._envelope(atom, token="0" * 32, more=True, native=result,
                position={"projection_id": "0" * 32, "leaf_start": 9223372036854775807,
                          "leaf_end": 9223372036854775807, "total_leaves": 9223372036854775807})) <= self.budget
        return self._page({"native": copy.deepcopy(result), "atoms": list(_atoms(result, fits)), "at": 0,
                           "projection_id": uuid.uuid4().hex,
                           "identity": copy.deepcopy(identity), "expires": time.monotonic() + 300})

    def resume(self, token, identity=None):
        state = self.pages.get(token)
        if state is None or state["expires"] < time.monotonic() or state["identity"] != identity:
            raise ToolExecutionError("cursor_expired", "Final output cursor expired or its source identity changed")
        if "cached" not in state:
            state["cached"] = self._page({**state, "atoms": state["atoms"]})
        return copy.deepcopy(state["cached"])

    def _page(self, state):
        native, atoms = state["native"], state["atoms"]
        body = {key: copy.deepcopy(value) for key, value in native.items() if key in _CONTEXT}
        token, at = uuid.uuid4().hex, state["at"]
        used = set()
        while at < len(atoms):
            atom = atoms[at]
            snapshots = {node["value_snapshot_id"] for node in _dicts(atom) if "value_snapshot_id" in node}
            if used & snapshots:
                break
            candidate = copy.deepcopy(body)
            _merge(candidate, atom)
            position = {"projection_id": state["projection_id"], "leaf_start": state["at"],
                        "leaf_end": at + 1, "total_leaves": len(atoms)}
            if encoded_size(self._envelope(candidate, token=token, more=True, native=native, position=position)) > self.budget:
                break
            body, at = candidate, at + 1
            used.update(snapshots)
        if at == state["at"]:
            raise ToolExecutionError("object_atom_too_large", "Required identity or one indivisible value exceeds the final output window")
        more = at < len(atoms)
        if more:
            self.pages[token] = {**state, "at": at}
        result = self._envelope(body, token=token, more=more, native=native,
            position={"projection_id": state["projection_id"], "leaf_start": state["at"],
                      "leaf_end": at, "total_leaves": len(atoms)})
        return result


def _dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _dicts(child)
