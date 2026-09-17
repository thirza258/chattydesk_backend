import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from accounts.models import settings_for
from chattydesk.envelope import envelope as _envelope, error as _error, first_error
from openrouter_handler import client
from openrouter_handler.memory import (
    MemoryPatchSerializer,
    build_messages,
    memory_data,
    memory_for,
)
from openrouter_handler.models import ConversationMemory, HistoryPrompt

# How many previous turns of a conversation are replayed back to the model.
HISTORY_TURNS = int(os.getenv("OPENROUTER_HISTORY_TURNS", "10"))

# Statuses that mean "this key cannot pay for this request" — revoked, out of
# credit, rate limited — as opposed to a bad request no key would fix. Note that
# OpenRouter's 403 is a moderation refusal, not a key problem.
KEY_FAILURE_STATUSES = {401, 402, 429}


def _upstream_status(exc):
    """The status to report for an OpenRouter failure.

    A 401 from OpenRouter means the *OpenRouter* key was rejected. Passing that
    through would tell the client its own session had expired, and send it off to
    refresh a token that was never the problem — so it becomes a 502: the
    upstream we depend on would not serve us.
    """
    if exc.status_code == 401:
        return status.HTTP_502_BAD_GATEWAY
    if 400 <= exc.status_code < 600:
        return exc.status_code
    return status.HTTP_502_BAD_GATEWAY


def _api_keys_to_try(user):
    """The keys to attempt, in order, as (owner, key) pairs.

    The server's key goes first so a user's own credit is only spent once ours
    has run out. `prefer_own_key` puts theirs in front instead.
    """
    server_key = os.getenv("OPENROUTER_API_KEY") or None
    own_settings = settings_for(user)
    own_key = own_settings.openrouter_api_key() if own_settings else None

    order = [("user", own_key), ("server", server_key)]
    if not (own_settings and own_settings.prefer_own_key):
        order.reverse()

    return [(owner, key) for owner, key in order if key]


def _complete(messages, keys, **kwargs):
    """Run the completion, moving on to the next key when one cannot pay.

    Returns (payload, owner) so the caller can tell whose key was spent.
    """
    if not keys:
        raise client.OpenRouterError(
            "No OpenRouter key available. Add your own key in Settings.",
            status_code=500,
        )

    last_error = None
    for owner, key in keys:
        try:
            return client.chat_completion(messages, api_key=key, **kwargs), owner
        except client.OpenRouterError as exc:
            if exc.status_code not in KEY_FAILURE_STATUSES:
                raise  # The request itself is wrong; another key changes nothing.
            last_error = exc

    raise last_error


def _build_messages(prompt, conversation_id, system_prompt, use_history=True, user=None):
    """Compose the OpenAI-style message list sent to OpenRouter."""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    if use_history and conversation_id:
        # Oldest first — replaying newest-first would hand the model the
        # conversation backwards. Scoped to the caller as well as the id: a
        # guessed conversation_id must not replay somebody else's thread.
        previous = (
            HistoryPrompt.objects.filter(conversation_id=conversation_id, user=user)
            .order_by("-created_at")[:HISTORY_TURNS]
        )
        for turn in reversed(list(previous)):
            messages.append({"role": "user", "content": turn.prompt})
            messages.append({"role": "assistant", "content": turn.response})

    messages.append({"role": "user", "content": prompt})
    return messages


def _as_float(value, field):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{field}' must be a number.")


def _as_bool(value, default=True):
    if value is None:
        return default
    if isinstance(value, str):
        # Form-encoded clients send "false", which is otherwise truthy.
        return value.strip().lower() not in ("false", "0", "no", "")
    return bool(value)


def _as_int(value, field):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{field}' must be an integer.")


class GenerateChat(APIView):
    """POST a prompt, get an answer back from any model OpenRouter proxies.

    Requires a bearer token (the REST_FRAMEWORK default): inference costs money,
    and the answer is filed under the caller's account.

    `default_model` is supplied by the URLconf so the legacy per-provider paths
    (/api/v1/gpt_handler/ and friends) keep working with a sensible model.
    """

    default_model = None

    def post(self, request, *args, **kwargs):
        message = request.data.get("message")
        if not isinstance(message, str) or not message.strip():
            return _error(
                "Bad Request: 'message' field is required.",
                status.HTTP_400_BAD_REQUEST,
            )

        model = request.data.get("model") or self.default_model or client.DEFAULT_MODEL
        conversation_id = request.data.get("conversation_id") or str(uuid.uuid4())
        system_prompt = request.data.get("system_prompt", client.DEFAULT_SYSTEM_PROMPT)
        memory = memory_for(request.user, conversation_id)
        options = MemoryPatchSerializer(data={
            **({"enabled": request.data["use_history"]} if "use_history" in request.data else {}),
            **({"history_turns": request.data["history_turns"]} if "history_turns" in request.data else {}),
        })
        if not options.is_valid():
            return _error(f"Bad Request: {first_error(options.errors)}", status.HTTP_400_BAD_REQUEST)
        memory.enabled = options.validated_data.get("enabled", memory.enabled)
        memory.history_turns = options.validated_data.get("history_turns", memory.history_turns)

        try:
            temperature = _as_float(request.data.get("temperature"), "temperature")
            max_tokens = _as_int(request.data.get("max_tokens"), "max_tokens")
        except ValueError as exc:
            return _error(f"Bad Request: {exc}", status.HTTP_400_BAD_REQUEST)

        messages = request.data.get("messages")
        memory_usage = None
        if not messages:
            messages, memory_usage = build_messages(message, system_prompt, memory)

        try:
            payload, key_owner = _complete(
                messages,
                _api_keys_to_try(request.user),
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except client.OpenRouterError as exc:
            return _error(f"OpenRouter error: {exc.message}", _upstream_status(exc))
        except Exception as exc:  # noqa: BLE001 - surface anything unexpected as 500
            return _error(
                f"Internal Server Error: {exc}", status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        answer = client.extract_text(payload)
        used_model = payload.get("model") or model

        HistoryPrompt.objects.create(
            user=request.user,
            prompt=message,
            response=answer,
            conversation_id=conversation_id,
            model_name=used_model[:100],
        )

        # First-turn preferences survive reloads and switching devices. Existing
        # threads are updated explicitly through the memory endpoint instead.
        ConversationMemory.objects.get_or_create(
            user=request.user,
            conversation_id=conversation_id,
            defaults={"enabled": memory.enabled, "history_turns": memory.history_turns},
        )

        return _envelope(
            {
                "response": answer,
                "model": used_model,
                "conversation_id": conversation_id,
                "usage": payload.get("usage") or {},
                # So the UI can say when a reply was paid for with the caller's
                # own key rather than the server's.
                "used_own_key": key_owner == "user",
                "memory": {**memory_data(memory), **memory_usage} if memory_usage is not None else None,
            }
        )


class ManageConversationMemory(APIView):
    """Preferences and a non-destructive context reset for one owned thread."""

    def _memory(self, user, conversation_id):
        if not HistoryPrompt.objects.filter(user=user, conversation_id=conversation_id).exists():
            return None
        return memory_for(user, conversation_id)

    def get(self, request, conversation_id):
        memory = self._memory(request.user, conversation_id)
        if memory is None:
            return _error("Conversation not found.", status.HTTP_404_NOT_FOUND)
        return _envelope(memory_data(memory))

    def patch(self, request, conversation_id):
        memory = self._memory(request.user, conversation_id)
        if memory is None:
            return _error("Conversation not found.", status.HTTP_404_NOT_FOUND)
        serializer = MemoryPatchSerializer(data=request.data)
        if not serializer.is_valid():
            return _error(f"Bad Request: {first_error(serializer.errors)}", status.HTTP_400_BAD_REQUEST)
        updates = dict(serializer.validated_data)
        if updates.pop("reset"):
            updates["reset_at"] = timezone.now()
        memory, _ = ConversationMemory.objects.update_or_create(
            user=request.user, conversation_id=conversation_id,
            defaults=updates, create_defaults={
                "enabled": memory.enabled, "history_turns": memory.history_turns, **updates,
            },
        )
        return _envelope(memory_data(memory))


class ListModels(APIView):
    """GET the OpenRouter catalogue so the UI can render a model picker.

    Public: it is a price list, it holds nothing about anybody, and the landing
    page should be able to quote from it without an account.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        search = (request.query_params.get("search") or "").strip().lower()
        only_free = _as_bool(request.query_params.get("free"), default=False)
        text_only = _as_bool(request.query_params.get("text_only"), default=True)
        refresh = _as_bool(request.query_params.get("refresh"), default=False)

        try:
            models = client.list_models(force_refresh=refresh)
        except client.OpenRouterError as exc:
            return _error(f"OpenRouter error: {exc.message}", _upstream_status(exc))

        if text_only:
            models = [m for m in models if client.is_text_model(m)]
        if only_free:
            models = [m for m in models if client.is_free_model(m)]
        if search:
            models = [
                m
                for m in models
                if search in (m.get("id") or "").lower()
                or search in (m.get("name") or "").lower()
            ]

        serialized = sorted(
            (client.serialize_model(m) for m in models), key=lambda m: m["name"].lower()
        )
        return _envelope(
            {
                "count": len(serialized),
                "default_model": client.DEFAULT_MODEL,
                "models": serialized,
            }
        )


class GetHistoryPrompt(APIView):
    """GET the caller's stored prompts/answers, newest first.

    The legacy path (/api/v1/gpt_handler/history/) sets `legacy_shape` so it keeps
    returning a bare list in `data`, which is what the current frontend reads.
    """

    legacy_shape = False

    def get(self, request):
        # Never `.all()`: history is the record of one person's conversations.
        queryset = HistoryPrompt.objects.filter(user=request.user)

        conversation_id = request.query_params.get("conversation_id")
        if conversation_id:
            queryset = queryset.filter(conversation_id=conversation_id)

        model_name = request.query_params.get("model")
        if model_name:
            queryset = queryset.filter(model_name=model_name)

        try:
            limit = min(int(request.query_params.get("limit", 100)), 500)
            offset = max(int(request.query_params.get("offset", 0)), 0)
        except (TypeError, ValueError):
            return _error(
                "Bad Request: 'limit' and 'offset' must be integers.",
                status.HTTP_400_BAD_REQUEST,
            )

        total = queryset.count()
        rows = queryset.order_by("-created_at")[offset : offset + limit]

        data = [
            {
                "id": row.id,
                "prompt": row.prompt,
                "response": row.response,
                "conversation_id": row.conversation_id,
                "model_name": row.model_name,
                "created_at": row.created_at,
            }
            for row in rows
        ]

        if self.legacy_shape:
            return _envelope(data)

        return _envelope({"count": total, "limit": limit, "offset": offset, "results": data})


class CompareModels(APIView):
    """POST one prompt to 2–5 models at once; answers come back side by side.

    Requires a bearer token (the REST_FRAMEWORK default) for the same reason
    as GenerateChat: inference costs money. Unlike chat, nothing is written
    to history — a compare is ephemeral by design.
    """

    def post(self, request, *args, **kwargs):
        message = request.data.get("message")
        if not isinstance(message, str) or not message.strip():
            return _error(
                "Bad Request: 'message' field is required.",
                status.HTTP_400_BAD_REQUEST,
            )

        models = request.data.get("models")
        if not isinstance(models, list) or not all(
            isinstance(m, str) and m.strip() for m in models
        ):
            return _error(
                "Bad Request: 'models' must be a list of 2-5 model ids.",
                status.HTTP_400_BAD_REQUEST,
            )
        models = [m.strip() for m in models]
        if not 2 <= len(models) <= 5:
            return _error(
                "Bad Request: 'models' must contain between 2 and 5 ids.",
                status.HTTP_400_BAD_REQUEST,
            )

        system_prompt = request.data.get("system_prompt", client.DEFAULT_SYSTEM_PROMPT)
        try:
            temperature = _as_float(request.data.get("temperature"), "temperature")
            max_tokens = _as_int(request.data.get("max_tokens"), "max_tokens")
        except ValueError as exc:
            return _error(f"Bad Request: {exc}", status.HTTP_400_BAD_REQUEST)

        messages = _build_messages(message, None, system_prompt, use_history=False)
        keys = _api_keys_to_try(request.user)

        def run_one(model):
            """One slot: its own completion, key fallback, error, and clock."""
            started = time.perf_counter()
            try:
                payload, key_owner = _complete(
                    messages, keys, model=model, temperature=temperature, max_tokens=max_tokens
                )
            except client.OpenRouterError as exc:
                # status_code is bookkeeping for the all-failed case; popped below.
                return {
                    "model": model,
                    "error": f"OpenRouter error: {exc.message}",
                    "status_code": exc.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                }
            return {
                "model": payload.get("model") or model,
                "response": client.extract_text(payload),
                "usage": payload.get("usage") or {},
                "used_own_key": key_owner == "user",
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "error": None,
            }

        # Sync gunicorn workers: this request must finish inside
        # GUNICORN_TIMEOUT (180s). In parallel it is bounded by the slowest
        # model (~120s upstream timeout); in sequence it would be the sum.
        with ThreadPoolExecutor(max_workers=len(models)) as pool:
            results = list(pool.map(run_one, models))  # map preserves slot order

        failures = [r for r in results if r["error"]]
        if len(failures) == len(results):
            first = failures[0]
            return _error(
                first["error"],
                _upstream_status(
                    client.OpenRouterError(first["error"], first["status_code"])
                ),
            )

        for result in results:
            result.pop("status_code", None)  # internal only; not part of the wire shape

        return _envelope({"prompt": message, "results": results})
