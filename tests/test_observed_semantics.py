import asyncio
import copy

import pytest

from plugin.plugins.neko_rimworld.tool_contracts import ToolExecutionError
from plugin.plugins.neko_rimworld.agent.world.semantic_tools import _with_field_semantics, semantic_interaction_factory
from plugin.plugins.neko_rimworld.game.query_tools import make_game_tools

from .test_semantic_tools import Session, _update, public_source


def test_measure_units_and_trait_codes_stay_with_values_without_mutating_evidence():
    data = {
        "facts": [
            {
                "object": {"id": "p"},
                "facts": {
                    "health": {"summary": {"health_percent": 0.92, "bleed_rate": 0.36, "pain": None}},
                    "profile": {
                        "traits": {"rows": [{"degree": 2, "description": "Move speed +0.4 c/s", "suppressed": False}]}
                    },
                },
            }
        ]
    }
    before = copy.deepcopy(data)
    result = _with_field_semantics(data)
    summary = result["facts"][0]["facts"]["health"]["summary"]
    assert summary["bleed_rate"] == 0.36 and summary["pain"] is None
    assert summary["field_semantics"]["bleed_rate"]["unit"] == "blood_loss_fraction_per_game_day"
    assert "not blood" in summary["field_semantics"]["health_percent"]["meaning"]
    traits = result["facts"][0]["facts"]["profile"]["traits"]
    assert traits["rows"][0]["degree"] == 2 and "variant code" in traits["field_semantics"]["degree"]
    assert data == before
    unknown = {"facts": [{"facts": {"health": {"summary": {"state": "unavailable", "reason": "not captured"}}}}]}
    assert _with_field_semantics(unknown) == unknown
