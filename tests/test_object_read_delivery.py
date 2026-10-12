"""Final byte budget includes plugin additions and the ToolRegistry envelope."""

import copy

import pytest

from ..agent.object_read_delivery import ObjectReadDelivery, encoded_size
from ..agent.object_read_results import ObjectReadResultAssembly
from ..agent.tool_contracts import ToolExecutionError


def result(values, *, next_cursor=None):
    return {"scope": {"map": "Map_0"}, "sample": {"load_epoch": "E1", "atomic": False}, "status": "ok",
            "rows": [{"anchor": {"id": "situation"}, "readings": [{"field": "comparison", "input_path": [0],
                "rows": [{"anchor": {"id": "comparison"}, "values": values,
                          "field_context": {"new_source_ids": {"recipient": "Pawn_Patient"}}}]}]}],
            "page": {"has_more": bool(next_cursor), "cursor": next_cursor},
            "delivery": {"projection_id": "P1", "leaf_start": 0, "leaf_end": 1,
                         "end_of_projection": not next_cursor, "complete": not next_cursor}}


def values(page):
    return page["rows"][0]["readings"][0]["rows"][0]["values"]


def test_large_derived_id_list_is_lossless_bounded_and_preserves_native_continuation():
    identities = ["events/Letter_" + str(index) for index in range(8000)]
    original = result({"available": True, "new_source_ids": identities}, next_cursor="native-next")
    frozen = copy.deepcopy(original)
    delivery = ObjectReadDelivery()
    page = delivery.deliver(original, "epoch")
    received = []
    count = 0
    while True:
        assert encoded_size(page) <= 80000
        current = values(page)
        if "new_source_ids" in current:
            window = current["new_source_ids"]
            assert window["offset"] == len(received)
            received.extend(window["items"])
            assert page["rows"][0]["readings"][0]["rows"][0]["field_context"]["new_source_ids"] == {"recipient": "Pawn_Patient"}
        count += 1
        if page["model_delivery"]["complete"]:
            break
        cursor = page["page"]["cursor"]
        page = delivery.resume(cursor, "epoch")
        assert delivery.resume(cursor, "epoch") == page
    assert received == identities
    assert count <= 10
    assert page["resume"] == {"page": {"cursor": "native-next"}}
    assert original == frozen


def test_large_plugin_description_keeps_unicode_and_original_sampling():
    text = "说明\"\n👾" * 15000
    original = result({"meaning": text})
    delivery = ObjectReadDelivery()
    page = delivery.deliver(original)
    pieces = []
    while True:
        assert encoded_size(page) <= 80000
        assert page["sample"] == original["sample"]
        pieces.append(values(page)["meaning"]["text"])
        if page["model_delivery"]["complete"]:
            break
        page = delivery.resume(page["page"]["cursor"])
    assert "".join(pieces) == text
    assert len(pieces) <= 6  # Same fixture formerly required 37 fixed-size windows.


def test_final_output_cursors_expire_on_identity_change_or_close():
    delivery = ObjectReadDelivery()
    page = delivery.deliver(result({"meaning": "文" * 50000}), "E1")
    cursor = page["page"]["cursor"]
    with pytest.raises(ToolExecutionError, match="identity changed"):
        delivery.resume(cursor, "E2")
    delivery.clear()
    with pytest.raises(ToolExecutionError, match="expired"):
        delivery.resume(cursor, "E1")


@pytest.mark.parametrize("payload", [
    ["文" * 40000 + '👾"\\\n'],
    [*range(4000), "文" * 40000, *range(4000)],
    {"a": "字" * 45000, "b": "文" * 45000,
     "typed": [None, True, False, 1, 1.25, '👾"\\\n', {}, []]},
])
def test_nested_selected_json_value_is_lossless_bounded_and_replayable(payload):
    original = result({"json_value": payload})
    frozen = copy.deepcopy(original)
    delivery = ObjectReadDelivery()
    assembly = ObjectReadResultAssembly()
    page = delivery.deliver(original, "E1")
    at = 0
    count = 0
    while True:
        count += 1
        assert encoded_size(page) <= 80000
        position = page["model_delivery"]
        assert position["leaf_start"] == at
        at = position["leaf_end"]
        assembled = assembly.observe(page)
        if position["complete"]:
            assert at == position["total_leaves"]
            break
        cursor = page["page"]["cursor"]
        assert page["resume"] == {"page": {"cursor": cursor}}
        page = delivery.resume(cursor, "E1")
        assert delivery.resume(cursor, "E1") == page
    assert values(assembled)["json_value"] == payload
    assert original == frozen
    assert count <= 6


def test_top_level_field_meanings_survive_final_pages():
    original = result({"meaning": "文" * 40000})
    original["field_semantics"] = {"LegacyThing.label": '说明👾"\\\n' * 15000}
    delivery, assembly = ObjectReadDelivery(), ObjectReadResultAssembly()
    page = delivery.deliver(original)
    while True:
        assert encoded_size(page) <= 80000
        assembled = assembly.observe(page)
        if page["model_delivery"]["complete"]:
            break
        page = delivery.resume(page["page"]["cursor"])
    assert assembled["field_semantics"] == original["field_semantics"]


def test_json_window_type_and_encoding_are_bound():
    page = ObjectReadDelivery().deliver(result({"json_value": ["文" * 40000]}))
    assembly = ObjectReadResultAssembly()
    assembly.observe(page)
    changed = copy.deepcopy(page)
    values(changed)["json_value"]["json_type"] = "object"
    with pytest.raises(ToolExecutionError, match="changed its parent"):
        assembly.observe(changed)


def test_items_reconstruction_recurses_into_nested_value_windows():
    outer = {"value_snapshot_id": "A1", "unit": "items", "items": [
        {"nested": {"value_snapshot_id": "T1", "unit": "UTF16_code_units", "text": "👾",
                    "offset": 0, "returned_chars": 2, "total_chars": 2, "complete": True}}, None],
        "offset": 0, "returned_items": 2, "total_items": 2, "complete": True}
    assert values(ObjectReadResultAssembly().observe(result({"json_value": outer})))["json_value"] == [{"nested": "👾"}, None]


def test_items_reconstruction_resolves_bare_child_windows():
    child = {"value_snapshot_id": "T2", "unit": "UTF16_code_units", "text": "👾",
             "offset": 0, "returned_chars": 2, "total_chars": 2, "complete": True}
    outer = {"value_snapshot_id": "A2", "unit": "items", "items": [child, None],
             "offset": 0, "returned_items": 2, "total_items": 2, "complete": True}
    assert values(ObjectReadResultAssembly().observe(result({"json_value": outer})))["json_value"] == ["👾", None]


def test_existing_native_text_window_uses_wrapped_budget_and_same_value_identity():
    text = '文👾"\\\n' * 15000
    total = len(text.encode("utf-16-le")) // 2 + 17
    original = result({"name": {"value_snapshot_id": "native-V1", "text": text, "offset": 17,
        "returned_chars": total - 17, "total_chars": total, "unit": "UTF16_code_units",
        "complete": False, "end_of_value": True, "has_more": False}})
    delivery = ObjectReadDelivery()
    page = delivery.deliver(original)
    pieces, offset = [], 17
    while True:
        assert encoded_size(page) <= 80000
        window = values(page)["name"]
        assert window["value_snapshot_id"] == "native-V1" and window["offset"] == offset
        pieces.append(window["text"])
        offset += window["returned_chars"]
        if page["model_delivery"]["complete"]:
            break
        page = delivery.resume(page["page"]["cursor"])
    assert "".join(pieces) == text and offset == total
    assert len(pieces) <= 6
