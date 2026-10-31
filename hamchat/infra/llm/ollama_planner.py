"""Conservative, Ollama-only request planning before a chat stream is opened."""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Dict, List

from .base import ChatMessage, StreamError

# We do not have the active model's exact chat tokenizer.  Two characters per
# token is intentionally conservative for code and non-ASCII text.  Every
# message also receives wrapper overhead and every image a fixed allowance.
# This is a fallback estimate, not a tokenizer or a guaranteed upper bound.
# Ollama /api/chat reports prompt_eval_count after inference; /api/show metadata
# alone does not count a rendered chat with images. Do not calibrate a changed
# prompt from a previous request's measured count.
CHARS_PER_TOKEN = 2
PER_MESSAGE_OVERHEAD = 12
TEMPLATE_RESERVE = 64
IMAGE_TOKEN_ALLOWANCE = 1024
DEFAULT_NUM_PREDICT = 4096


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
    requested_num_predict: int
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

    # Reserve the user's entire shared generation allowance first. Oldest-first
    # removal prefers the latest two completed turns (and any additional history
    # that fits), without imposing a fixed history count or mutating saved history.
    removable = {index for group in groups for index in group}
    mandatory = [message for index, message in enumerate(original) if index not in removable]
    mandatory_space = _available_generation(
        context_length, estimate_input_tokens(mandatory), len(mandatory)
    )
    target_generation = _requested_num_predict(planned_options.get("num_predict"))
    if mandatory_space < target_generation:
        raise _too_large(context_length, target_generation)

    candidate = selected()
    while _available_generation(context_length, estimate_input_tokens(candidate), len(candidate)) < target_generation:
        if not groups:
            raise _too_large(context_length, target_generation)
        group = groups.pop(0)
        # Groups contain only eligible older, non-system messages.
        for index in group:
            retained.discard(index)
        omitted_turns += 1
        omitted_messages += len(group)
        candidate = selected()

    final_input = estimate_input_tokens(candidate)
    num_predict = target_generation
    planned_options["num_predict"] = num_predict
    return RequestPlan(
        messages=candidate,
        options=planned_options,
        original_message_count=len(original),
        original_input_tokens=original_input,
        final_input_tokens=final_input,
        template_reserve=TEMPLATE_RESERVE + len(candidate) * PER_MESSAGE_OVERHEAD,
        requested_num_predict=target_generation,
        num_predict=num_predict,
        omitted_turn_count=omitted_turns,
        omitted_message_count=omitted_messages,
        outcome="trimmed" if omitted_messages else "fit",
    )


def _available_generation(context_length: int, input_tokens: int, message_count: int) -> int:
    return context_length - input_tokens - TEMPLATE_RESERVE - message_count * PER_MESSAGE_OVERHEAD


def _requested_num_predict(requested: object) -> int:
    if isinstance(requested, int) and not isinstance(requested, bool) and requested > 0:
        return requested
    return DEFAULT_NUM_PREDICT


def _older_turn_groups(messages: List[ChatMessage]) -> List[List[int]]:
    newest_user = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].role == "user"), None)
    # A text-path attachment stub is a second user message belonging to its
    # preceding user text. Protect both as the newest turn.
    while newest_user is not None and messages[newest_user].metadata and messages[newest_user].metadata.get("attachment_stub_parent"):
        newest_user = next((i for i in range(newest_user - 1, -1, -1)
                            if messages[i].role == "user"), None)
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


def _too_large(context_length: int, generation: int) -> RequestTooLargeError:
    return RequestTooLargeError(
        f"Preflight context rejection: mandatory input plus the requested {generation}-token "
        f"generation allowance cannot fit within the {context_length}-token active context "
        "using the conservative input estimate. No answer generation was sent. "
        "Lower Maximum generation. Increase Context "
        "allocation in Model → Model Manager—higher allocations use more memory—or reduce "
        "the input by shortening the newest message or AI Profile, removing attachments, "
        "or temporarily disabling Use HamMem in the Chat Panel."
    )
