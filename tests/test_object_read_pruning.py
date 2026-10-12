"""Selected object facts must not pass through the retired aspect projector."""

import copy

from ..agent.context_pruning import ContextPruner
from ..agent.serialization import canonical


def test_selected_nested_anatomy_and_parent_coverage_are_preserved():
    arguments = {
        "target": {"kind": "pawns"},
        "select": [{"field": "health", "children": [{"field": "body_parts", "columns": ["health"]}]}],
    }
    value = {
        "tool": "look",
        "data": {
            "rows": [{"anchor": {"id": "Pawn_1"}, "readings": [{
                "field": "health", "input_path": [0], "rows": [{"anchor": {"owner_id": "Pawn_1"},
                    "body_parts": [{"key": "Pawn_1/body:Human/part:0", "health": 12, "max_health": 20}],
                    "readings": [{"field": "body_parts", "input_path": [0, 0], "page": {"next": "bound-parent-cursor"},
                        "coverage": {"source": "partial", "predicate": "complete", "fields": "complete"}}],
                }],
            }]}],
            "sample": {"load_epoch": "E1", "atomic": False},
        },
    }
    original = copy.deepcopy(value)
    compiled = ContextPruner().compile(value, arguments=arguments)
    assert compiled.content == canonical(value)
    assert value == original


def test_unavailable_selected_field_is_not_pruned_into_empty_observation():
    value = {"tool": "look", "data": {"rows": [], "issues": [{"input_path": [0], "code": "predicate_unavailable"}],
        "coverage": {"source": "complete", "predicate": "partial", "fields": "complete"}}}
    compiled = ContextPruner().compile(value, arguments={"target": {"kind": "colonists"}, "select": [{"field": "identity"}]})
    assert compiled.content == canonical(value)


def test_cursor_only_preserves_delivered_value_windows_and_parent_coverage():
    value = {"tool": "look", "data": {"rows": [{"anchor": {"id": "Pawn_1"}, "values": {"description": {
        "value_snapshot_id": "V1", "text": "尾部", "offset": 2048, "complete": False, "end_of_value": True,
    }}}], "coverage": {"display_complete": False}}}
    compiled = ContextPruner().compile(value, arguments={"page": {"cursor": "bound"}})
    assert compiled.content == canonical(value)
