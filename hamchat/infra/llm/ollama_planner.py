"""Conservative, Ollama-only request planning before a chat stream is opened."""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Dict, List

from .base import ChatMessage, StreamError

# We do not have the active model's exact chat tokenizer.  Two characters per
# token is intentionally conservative for code and non-ASCII text.  Every
# message also receives wrapper overhead and every image a fixed allowance.
CHARS_PER_TOKEN = 2
PER_MESSAGE_OVERHEAD = 12
TEMPLATE_RESERVE = 64
IMAGE_TOKEN_ALLOWANCE = 1024
MIN_REASONING_RESERVE = 256
MIN_VISIBLE_RESERVE = 256
DEFAULT_REASONING_RESERVE = 512
DEFAULT_VISIBLE_RESERVE = 512
DEFAULT_NUM_PREDICT = DEFAULT_REASONING_RESERVE + DEFAULT_VISIBLE_RESERVE


class RequestTooLargeError(StreamError):
    """Mandatory system/newest-user content cannot fit the runtime context."""


@dataclass(frozen=True)
class RequestPlan:
    messages: List[ChatMessage]
    options: Dict
    original_message_count: int
    original_input_tokens: int
    final_input_tokens: int
    template_reserve: int
    reasoning_reserve: int
    visible_response_reserve: int
    num_predict: int
    omitted_turn_count: int
    omitted_message_count: int
    outcome: str  # "fit" | "trimmed"


def image_count(message: ChatMessage) -> int:
    parts = getattr(message, "parts", None) or []
    return sum(
        1 for part in parts
        if (isinstance(part, dict) and part.get("type") == "image")
        or (isinstance(part, str) and part.strip())
    )


def estimate_message_tokens(message: ChatMessage) -> int:
    content_tokens = ceil(len(message.content or "") / CHARS_PER_TOKEN)
    return content_tokens + image_count(message) * IMAGE_TOKEN_ALLOWANCE


def estimate_input_tokens(messages: List[ChatMessage]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def plan_ollama_request(
    *,
    messages: List[ChatMessage],
    options: Dict,
    context_length: int,
) -> RequestPlan:
    """Trim only complete old turns and return the exact list/options to send."""
    original = list(messages)
    planned_options = dict(options or {})
    original_input = estimate_input_tokens(original)
    groups = _older_turn_groups(original)
    retained = set(range(len(original)))
    omitted_turns = omitted_messages = 0

    def selected() -> List[ChatMessage]:
        return [message for index, message in enumerate(original) if index in retained]

    candidate = selected()
    while _available_generation(context_length, estimate_input_tokens(candidate), len(candidate)) < _minimum_output_reserve():
        if not groups:
            raise _too_large(context_length)
        group = groups.pop(0)
        # Groups contain only eligible older, non-system messages.
        for index in group:
            retained.discard(index)
        omitted_turns += 1
        omitted_messages += len(group)
        candidate = selected()

    final_input = estimate_input_tokens(candidate)
    available_generation = _available_generation(context_length, final_input, len(candidate))
    requested = planned_options.get("num_predict")
    num_predict = _effective_num_predict(requested, available_generation)
    planned_options["num_predict"] = num_predict
    reasoning = min(DEFAULT_REASONING_RESERVE, num_predict // 2)
    visible = num_predict - reasoning
    return RequestPlan(
        messages=candidate,
        options=planned_options,
        original_message_count=len(original),
        original_input_tokens=original_input,
        final_input_tokens=final_input,
        template_reserve=TEMPLATE_RESERVE + len(candidate) * PER_MESSAGE_OVERHEAD,
        reasoning_reserve=reasoning,
        visible_response_reserve=visible,
        num_predict=num_predict,
        omitted_turn_count=omitted_turns,
        omitted_message_count=omitted_messages,
        outcome="trimmed" if omitted_messages else "fit",
    )


def _available_generation(context_length: int, input_tokens: int, message_count: int) -> int:
    return context_length - input_tokens - TEMPLATE_RESERVE - message_count * PER_MESSAGE_OVERHEAD


def _minimum_output_reserve() -> int:
    return MIN_REASONING_RESERVE + MIN_VISIBLE_RESERVE


def _effective_num_predict(requested: object, available_generation: int) -> int:
    if isinstance(requested, int) and not isinstance(requested, bool) and requested > 0:
        return min(requested, available_generation)
    return min(DEFAULT_NUM_PREDICT, available_generation)


def _older_turn_groups(messages: List[ChatMessage]) -> List[List[int]]:
    newest_user = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].role == "user"), None)
    if newest_user is None:
        # Without a distinct newest user there is no safe history to remove.
        return []

    groups: List[List[int]] = []
    current: List[int] = []
    for index, message in enumerate(messages[:newest_user]):
        if message.role == "system":
            continue
        if message.role == "user" and message.metadata and message.metadata.get("attachment_stub_parent") and current:
            current.append(index)
        elif message.role == "user":
            if current:
                groups.append(current)
            current = [index]
        elif current:
            current.append(index)
        else:
            # An assistant/irregular leading entry is removable as its own unit;
            # it cannot become an orphan after removal.
            groups.append([index])
    if current:
        groups.append(current)
    return groups


def _too_large(context_length: int) -> RequestTooLargeError:
    return RequestTooLargeError(
        "This request cannot fit within the model’s active context. Increase Context "
        "allocation in Model → Model Manager—higher allocations use more memory—or reduce "
        "the input by shortening the newest message or AI Profile, removing attachments, "
        "or temporarily disabling Use HamMem in the Chat Panel."
    )
