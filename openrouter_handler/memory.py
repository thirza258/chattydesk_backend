import os

from rest_framework import serializers

from openrouter_handler.models import ConversationMemory, HistoryPrompt

MAX_HISTORY_TURNS = 50
DEFAULT_HISTORY_TURNS = max(1, min(int(os.getenv("OPENROUTER_HISTORY_TURNS", "10")), MAX_HISTORY_TURNS))
# A character bound keeps large stored answers from growing requests without
# limit. It is not an exact token count and does not truncate the current prompt.
HISTORY_CHARACTER_LIMIT = max(1, int(os.getenv("OPENROUTER_HISTORY_CHARACTERS", "32000")))


class MemoryPatchSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False)
    history_turns = serializers.IntegerField(required=False, min_value=1, max_value=MAX_HISTORY_TURNS)
    reset = serializers.BooleanField(required=False, default=False)


def memory_for(user, conversation_id):
    return ConversationMemory.objects.filter(
        user=user, conversation_id=conversation_id
    ).first() or ConversationMemory(
        user=user, conversation_id=conversation_id, history_turns=DEFAULT_HISTORY_TURNS
    )


def remembered_turns(memory):
    rows = HistoryPrompt.objects.filter(user=memory.user, conversation_id=memory.conversation_id)
    if memory.reset_at:
        rows = rows.filter(created_at__gt=memory.reset_at)
    return rows


def memory_data(memory):
    return {
        "enabled": memory.enabled,
        "history_turns": memory.history_turns,
        "available_turns": remembered_turns(memory).count(),
        "reset_at": memory.reset_at,
    }


def build_messages(prompt, system_prompt, memory):
    messages = [{"role": "system", "content": system_prompt}] if system_prompt else []
    available = remembered_turns(memory)
    turns = []
    if memory.enabled:
        remaining = max(0, HISTORY_CHARACTER_LIMIT - len(prompt) - len(system_prompt or ""))
        recent = available.order_by("-created_at", "-id")[:memory.history_turns]
        for turn in recent:
            size = len(turn.prompt) + len(turn.response)
            if size > remaining:
                break
            turns.append(turn)
            remaining -= size
        for turn in reversed(turns):
            messages.extend([
                {"role": "user", "content": turn.prompt},
                {"role": "assistant", "content": turn.response},
            ])
    messages.append({"role": "user", "content": prompt})
    return messages, {
        "used_turns": len(turns),
        "trimmed": memory.enabled and available.count() > len(turns),
    }
