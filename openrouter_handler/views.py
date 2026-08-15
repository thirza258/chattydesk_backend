import os
import uuid

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from openrouter_handler import client
from openrouter_handler.models import HistoryPrompt

# How many previous turns of a conversation are replayed back to the model.
HISTORY_TURNS = int(os.getenv("OPENROUTER_HISTORY_TURNS", "10"))


def _envelope(data, message="Success", code=status.HTTP_200_OK):
    return Response(
        {"status": code, "message": message, "data": data}, status=code
    )


def _error(message, code):
    return Response({"status": code, "message": message}, status=code)


def _build_messages(prompt, conversation_id, system_prompt, use_history=True):
    """Compose the OpenAI-style message list sent to OpenRouter."""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    if use_history and conversation_id:
        # Oldest first — replaying newest-first would hand the model the
        # conversation backwards.
        previous = (
            HistoryPrompt.objects.filter(conversation_id=conversation_id)
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

    `default_model` is supplied by the URLconf so the legacy per-provider paths
    (/api/v1/gpt_handler/ and friends) keep working with a sensible model.
    """

    default_model = None

    def post(self, request, *args, **kwargs):
        message = request.data.get("message")
        if not message or not str(message).strip():
            return _error(
                "Bad Request: 'message' field is required.",
                status.HTTP_400_BAD_REQUEST,
            )

        model = request.data.get("model") or self.default_model or client.DEFAULT_MODEL
        conversation_id = request.data.get("conversation_id") or str(uuid.uuid4())
        system_prompt = request.data.get("system_prompt", client.DEFAULT_SYSTEM_PROMPT)
        use_history = _as_bool(request.data.get("use_history"))

        try:
            temperature = _as_float(request.data.get("temperature"), "temperature")
            max_tokens = _as_int(request.data.get("max_tokens"), "max_tokens")
        except ValueError as exc:
            return _error(f"Bad Request: {exc}", status.HTTP_400_BAD_REQUEST)

        messages = request.data.get("messages")
        if not messages:
            messages = _build_messages(
                message, conversation_id, system_prompt, use_history=use_history
            )

        try:
            payload = client.chat_completion(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except client.OpenRouterError as exc:
            code = exc.status_code if 400 <= exc.status_code < 600 else 502
            return _error(f"OpenRouter error: {exc.message}", code)
        except Exception as exc:  # noqa: BLE001 - surface anything unexpected as 500
            return _error(
                f"Internal Server Error: {exc}", status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        answer = client.extract_text(payload)
        used_model = payload.get("model") or model

        HistoryPrompt.objects.create(
            prompt=message,
            response=answer,
            conversation_id=conversation_id,
            model_name=used_model[:100],
        )

        return _envelope(
            {
                "response": answer,
                "model": used_model,
                "conversation_id": conversation_id,
                "usage": payload.get("usage") or {},
            }
        )


class ListModels(APIView):
    """GET the OpenRouter catalogue so the UI can render a model picker."""

    def get(self, request):
        search = (request.query_params.get("search") or "").strip().lower()
        only_free = _as_bool(request.query_params.get("free"), default=False)
        text_only = _as_bool(request.query_params.get("text_only"), default=True)
        refresh = _as_bool(request.query_params.get("refresh"), default=False)

        try:
            models = client.list_models(force_refresh=refresh)
        except client.OpenRouterError as exc:
            code = exc.status_code if 400 <= exc.status_code < 600 else 502
            return _error(f"OpenRouter error: {exc.message}", code)

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
    """GET stored prompts/answers, newest first.

    The legacy path (/api/v1/gpt_handler/history/) sets `legacy_shape` so it keeps
    returning a bare list in `data`, which is what the current frontend reads.
    """

    legacy_shape = False

    def get(self, request):
        queryset = HistoryPrompt.objects.all()

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
