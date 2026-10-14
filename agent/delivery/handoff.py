"""Validate and retain the current text-only consultation delivery."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from ...game.state import redact_text
from ...tool_contracts import ToolExecutionError, ToolSpec
from ..model.tokenization import tokenizer as _tokenizer
from ..runtime.prompt import PLAIN_ANSWER_GUIDANCE, PLAIN_DETAIL_GUIDANCE, PLAIN_SUBMISSION_GUIDANCE

HANDOFF_TOKEN_BUDGET = 180
# The host parser truncates at 200 tokens; accepted answers stay below that bound.
HANDOFF_TRANSPORT_TOKEN_BUDGET = 200
MAX_FULL_TEXT_CHARS = 12_000
MAX_AGENT_TEXT_CHARS = 4_000
_MISSING_HANDOFF = (
    "The internal RimWorld agent completed without a callback-safe response. "
    "Review the full result in the plugin panel."
)


class HandoffValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SubmissionPatchError(HandoffValidationError):
    def __init__(self, code: str, message: str, details: dict[str, Any]):
        super().__init__(code, message)
        self.details = details


def count_handoff_tokens(text: str) -> tuple[int, str]:
    encoding = _tokenizer()
    if encoding is not None:
        return len(encoding.encode(text)), "o200k_base"
    return len(text.encode("utf-8", "replace")), "utf8_bytes_upper_bound"


def _validated_text(value: object, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HandoffValidationError("invalid_submission", f"{field} must be a non-empty string.")
    raw = value.replace("\x00", " ").strip()
    if len(raw) > max_chars:
        raise HandoffValidationError(
            "submission_too_large", f"{field} exceeded its {max_chars}-character transport safety limit."
        )
    return redact_text(raw, max(len(raw) + 1, 1))


def validate_full_text(value: object) -> str:
    return _validated_text(value, field="full_text", max_chars=MAX_FULL_TEXT_CHARS)


def validate_agent_text(value: object) -> str:
    text = _validated_text(value, field="agent_text", max_chars=MAX_AGENT_TEXT_CHARS)
    tokens, _ = count_handoff_tokens(text)
    if tokens > HANDOFF_TOKEN_BUDGET:
        raise HandoffValidationError(
            "handoff_over_budget",
            f"agent_text uses {tokens} tokens; the maximum is {HANDOFF_TOKEN_BUDGET}.",
        )
    return text


def rendered_authored_text(text: str, language: str) -> str:
    """Remove a trailing tool serialization fragment while retaining the saved input."""
    tail = r'(?:<parameter\s+name=[\"\x27]language[\"\x27]>\s*' + re.escape(language) + r'\s*(?:</parameter>)?\s*)?'
    return re.sub(r'\s*</answer>\s*' + tail + r'$', '', text).rstrip()


@dataclass(slots=True)
class SubmissionDraft:
    answer: str = ""
    detail: str | None = None
    language: str = "zh"
    full_text: str | None = None
    agent_text: str | None = None
    revision: int = 0
    errors: dict[str, dict[str, str]] = field(default_factory=dict)
    # Refused claims never enter committed delivery text or degraded fallbacks.
    claims_blocked: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "detail": self.detail,
            "language": self.language,
            "full_text": self.full_text,
            "agent_text": self.agent_text,
            "revision": self.revision,
            "saved_fields": [key for key in ("answer", "detail") if getattr(self, key)],
            "errors": copy.deepcopy(self.errors),
            "complete": bool(self.full_text and self.agent_text and not self.errors),
        }

    def apply(self, patch: Mapping[str, Any], *, observations=()) -> dict[str, Any]:
        if not isinstance(patch, Mapping) or not patch or set(patch) - {"answer", "detail", "language"}:
            raise SubmissionPatchError(
                "invalid_submission", "Submit answer as text, optional detail and language.",
                {"saved_fields": self.snapshot()["saved_fields"]},
            )
        changed = set()
        for key in ("language", "answer", "detail"):
            if key not in patch:
                continue
            try:
                value = patch[key]
                if key == "language":
                    if value not in {"zh", "en"}:
                        raise HandoffValidationError("invalid_submission", "language must be zh or en.")
                elif key == "answer" or value is not None:
                    value = _validated_text(value, field=key, max_chars=MAX_FULL_TEXT_CHARS)
                if value != getattr(self, key):
                    changed.add(key)
                setattr(self, key, value)
                self.errors.pop(key, None)
            except (HandoffValidationError, TypeError) as error:
                self.errors[key] = {"code": getattr(error, "code", "invalid_submission"), "message": str(error)}
        if changed:
            self.revision += 1
        self.errors.pop("delivery", None)
        if not self.errors:
            from .claims import check_claims, describe

            rejected = check_claims(self.answer, self.detail, observations=observations)
            if rejected:
                self.claims_blocked = True
                raise SubmissionPatchError(
                    rejected[0]["code"],
                    "Unsupported claim needs evidence from this run: " + describe(rejected),
                    {
                        "saved_fields": self.snapshot()["saved_fields"],
                        "rejected_claims": rejected,
                        "required_fields": list(dict.fromkeys(item.get("field", "answer") for item in rejected)),
                    },
                )
            self.claims_blocked = False
        self.full_text = self.agent_text = None
        short = ""
        try:
            short = rendered_authored_text(self.answer, self.language) if "answer" not in self.errors else ""
            detail = self.detail if "detail" not in self.errors and "answer" not in self.errors else None
            full = "\n\n".join(part for part in (short, detail if detail != short else None) if part)
            if full:
                self.full_text = validate_full_text(full)
            if not short:
                raise HandoffValidationError(
                    "invalid_submission", "Provide the short answer text; detail cannot replace it."
                )
            self.agent_text = validate_agent_text(short)
        except HandoffValidationError as error:
            self.errors["delivery"] = {"code": error.code, "message": str(error)}
        if self.errors:
            required = [key for key in self.errors if key != "delivery"]
            if "delivery" in self.errors:
                required.append("answer")
            details = {
                "saved_fields": self.snapshot()["saved_fields"],
                "required_fields": list(dict.fromkeys(required)),
                "field_errors": copy.deepcopy(self.errors),
                "draft_revision": self.revision,
                "handoff_tokens": count_handoff_tokens(short)[0] if short else None,
                "token_budget": HANDOFF_TOKEN_BUDGET,
            }
            first = next(iter(self.errors.values()))
            repair = " Valid inputs are saved. Correct the required fields."
            if self.errors.get("delivery", {}).get("code") == "handoff_over_budget":
                repair += (
                    " Shorten answer to the concise conclusion with room below the token limit;"
                    " keep requested facts in detail and preserve the conclusion's scope and unknowns."
                    " No new game query is needed for this length repair."
                )
            if "answer" in required and self.detail is not None:
                repair += (
                    " Omitted detail remains saved; update it only when the report changes;"
                    " do not clear detail that is needed to answer the question."
                )
            raise SubmissionPatchError(first["code"], " ".join(item["message"] for item in self.errors.values()) + repair, details)
        self.claims_blocked = False
        return validate_submission(self.full_text, self.agent_text)


def make_submission_tool(draft: SubmissionDraft, observations, on_update=None):
    async def submit(arguments):
        try:
            result = draft.apply(arguments, observations=observations)
        except SubmissionPatchError as error:
            if on_update is not None:
                on_update(draft.snapshot(), "submission_patch_rejected", error.details)
            raise ToolExecutionError(error.code, str(error), details=error.details) from None
        if on_update is not None:
            on_update(draft.snapshot(), "submission_patch_accepted",
                      {"saved_fields": draft.snapshot()["saved_fields"], "revision": draft.revision})
        return result

    return ToolSpec(
        name="submit_response", description=PLAIN_SUBMISSION_GUIDANCE,
        parameters={
            "type": "object",
            "properties": {
                "answer": {"type": "string", "minLength": 1, "description": PLAIN_ANSWER_GUIDANCE},
                "detail": {"type": ["string", "null"], "maxLength": MAX_FULL_TEXT_CHARS,
                           "description": PLAIN_DETAIL_GUIDANCE},
                "language": {"type": "string", "enum": ["zh", "en"], "default": "zh",
                             "description": "Language of the whole submission."},
            },
            "minProperties": 1, "additionalProperties": False,
        }, handler=submit, terminal=True,
    )


def validate_submission(full_text: object, agent_text: object) -> dict[str, Any]:
    full = validate_full_text(full_text)
    handoff = validate_agent_text(agent_text)
    handoff_tokens, tokenizer = count_handoff_tokens(handoff)
    full_tokens, _ = count_handoff_tokens(full)
    return {
        "full_text": full, "agent_text": handoff,
        "delivery": {
            "format": "natural_text", "token_budget": HANDOFF_TOKEN_BUDGET,
            "handoff_tokens": handoff_tokens, "full_answer_tokens": full_tokens,
            "tokenizer": tokenizer, "transport_token_budget": HANDOFF_TRANSPORT_TOKEN_BUDGET,
            "host_truncation_expected": False,
        },
    }


def attach_agent_handoff(result: object) -> dict[str, Any]:
    source = dict(result) if isinstance(result, Mapping) else {}
    status = str(source.get("status") or "degraded").lower()
    answer = source.get("answer")
    agent_text = source.get("agent_text")
    submission_mode = str(source.get("submission_mode") or "unknown")
    try:
        validated = validate_submission(answer, agent_text)
        text = validated["agent_text"]
        delivery = dict(validated["delivery"])
        delivery["submission_mode"] = submission_mode
    except HandoffValidationError as exception:
        if status == "ok":
            source["status"] = "degraded"
        text = _MISSING_HANDOFF
        handoff_tokens, tokenizer = count_handoff_tokens(text)
        delivery = {
            "format": "natural_text", "token_budget": HANDOFF_TOKEN_BUDGET,
            "handoff_tokens": handoff_tokens,
            "full_answer_tokens": count_handoff_tokens(answer)[0] if isinstance(answer, str) else None,
            "tokenizer": tokenizer, "submission_mode": "operational_fallback",
            "host_truncation_expected": False, "error_code": exception.code,
        }
    source["summary"] = source["reply"] = text
    source["delivery"] = delivery
    return source


__all__ = [
    "HANDOFF_TOKEN_BUDGET", "HANDOFF_TRANSPORT_TOKEN_BUDGET", "HandoffValidationError",
    "SubmissionDraft", "SubmissionPatchError", "validate_full_text", "validate_agent_text",
    "attach_agent_handoff", "count_handoff_tokens", "validate_submission",
]
