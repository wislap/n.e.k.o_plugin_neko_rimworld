"""Small model-facing contracts; native/request validation stays unchanged."""

from __future__ import annotations

import copy

from ..runtime.prompt import finalization_sections, system_sections
from ...schema_serialization import compact_tools, remove_redundant_constraints

QUERY_GUIDE = (
    "Read only facts needed for this question. Specify look.aspects. state means physical/device state; "
    "Pawn health, skills, work, gear, mood and profile require their own aspects in the same call. "
    "focus selects named skills/work/capacities in that call. "
    "Resolve names directly; put IDs in id/ids. actor performs interaction/travel; user is the recipient. "
    "count records counts objects; units sums stack counts. Containment does not establish ownership or usability. "
    "evaluate_activity resolves actor/subject names and destination selectors in that same call; "
    "do not look up IDs, count destinations or survey the region first merely to prepare a path request. "
    "Use kind=state for contextual object predicates/occupancy/reservations without paths. "
    "Use it for all requested actors/destinations together; request additional approach, "
    "actor facts and destination state there only when relevant. Request environment in the original path call "
    "when hazards or safety affect the question; omitted environment means uninspected in the model view. survey_space reads regions. "
    "Path cost is not ETA, native Danger is not overall safety, and occupancy/reservation is not complete job eligibility. "
    "Preserve scope, unknowns and every explicitly requested comparison."
)


def query_guide():
    return QUERY_GUIDE


def task_prompt(preferred_cycles, *, finalizing=False):
    sections = finalization_sections if finalizing else system_sections
    parts = dict(sections(preferred_cycles))
    if not finalizing:
        parts["query_policy"] = (
            "Investigate the explicit question, not a larger hypothetical mission. "
            "Each read must supply a missing requested finding, distinguish the requested candidates, "
            "or resolve a contradiction in those findings. Do not repeat successful reads. "
            "Do not investigate unasked prerequisites merely to eliminate every possible uncertainty. "
            "State those prerequisites as conditions instead. Batch independent requested facts in the first call.\n"
        )
    parts["completion_policy"] = (
        "Cover every explicitly requested subject, destination and comparison. Bind values to their "
        "actor, recipient, origin, destination, metric and sample. Do not substitute another stage or actor. "
        "Measured path cost is not elapsed time; a skill/trait is not a computed final stat. "
        "Keep unknowns and incomplete coverage explicit. Where unasked prerequisites remain unobserved, "
        "make the recommendation conditional instead of expanding the investigation. "
        "Do not infer causes, eligibility or future success from unrelated observations.\n"
        "Medical-care policy belongs to the recipient. Assigned work is not a running job; "
        "priority 0 is unassigned for autonomous work. Among assigned priorities 1..4, lower wins; "
        "with higher natural priority breaking ties.\n"
    )
    parts["submission"] = (
        "Finish through submit_response. answer is a standalone conclusion with material conditions, "
        "aiming for <=100 tokens (hard maximum 180). detail contains only the remaining findings "
        "explicitly requested by the caller, including complete requested tables. "
        "Do not add adjacent investigations, speculative mechanisms or a catalogue of unasked unknowns. "
        "answer+detail <=12000 characters. Length-only repair sends answer only; saved detail remains.\n"
    )
    return "".join(parts.values())


def model_tools(tools):
    """Only equivalent schema syntax reduction; each business owner owns its semantics."""
    result = copy.deepcopy(tools)
    for tool in result:
        function = tool.get("function", {})
        if isinstance(function.get("parameters"), dict):
            function["parameters"] = remove_redundant_constraints(function["parameters"])
    return compact_tools(result)
