"""Bind the current semantic consultation to caller-owned work capabilities."""

from __future__ import annotations

from ...tool_contracts import ToolSetup
from ..world.semantic_tools import semantic_interaction_factory
from .tools import ToolRegistry


def compose_tools(game_tools, capabilities):
    result = tuple(game_tools) + tuple(capabilities)
    names = [tool.name for tool in result]
    if len(names) != len(set(names)):
        raise ValueError("Game tools and work capabilities must have distinct names")
    return result


class ExecutionEnvironment:
    def __init__(self, setup: ToolSetup, session, update):
        self.setup = setup
        self.session = session
        session.interaction = semantic_interaction_factory(setup, session, update)
        self.set_prompt(session.interaction.prompt(session.preferred_cycles))

    def registry(self):
        session = self.session
        registry = ToolRegistry(context_pruner=session.context_pruner)
        work = () if session.interaction.finalizing else (*self.setup.capabilities, *session.capabilities)
        for spec in compose_tools(session.interaction.tools(), work):
            registry.register(spec)
        return registry

    def set_prompt(self, prompt):
        if self.session.workspace_id is not None:
            prompt = prompt.replace(
                "No long-term memory, game mutation or autonomous action.",
                "The caller retains a persistent colony workspace. No game mutation or autonomous action.",
            )
        self.session.messages[0]["content"] = prompt
