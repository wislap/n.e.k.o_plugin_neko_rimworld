"""Public package-local surface for the independent RimWorld agent runtime."""

from .runtime.audit import AgentAuditCapacityError, AgentAuditLedger, AgentAuditRegistry, AgentRunState
from .runtime.contracts import AssistantTurn, ToolCall, ToolResult
from .delivery.handoff import (
    HANDOFF_TOKEN_BUDGET,
    HandoffValidationError,
    attach_agent_handoff,
    count_handoff_tokens,
    validate_submission,
)
from .runtime.loop import AgentRuntime
from .model.model_client import (
    AgentConfigurationError,
    AgentModelSettings,
    ModelClientError,
    OpenAICompatibleModelClient,
    build_agent_model_settings,
    parse_agent_model_settings,
)
from .world.semantic_tools import SemanticInteraction, semantic_interaction_factory
from .runtime.session import AgentRunSession
from .runtime.tools import ToolExecutionError

__all__ = [
    "AgentAuditLedger",
    "AgentAuditCapacityError",
    "AgentAuditRegistry",
    "AgentConfigurationError",
    "AgentModelSettings",
    "AgentRunState",
    "AgentRunSession",
    "AgentRuntime",
    "AssistantTurn",
    "HANDOFF_TOKEN_BUDGET",
    "HandoffValidationError",
    "ModelClientError",
    "OpenAICompatibleModelClient",
    "SemanticInteraction",
    "ToolCall",
    "ToolExecutionError",
    "ToolResult",
    "attach_agent_handoff",
    "build_agent_model_settings",
    "count_handoff_tokens",
    "parse_agent_model_settings",
    "semantic_interaction_factory",
    "validate_submission",
]
