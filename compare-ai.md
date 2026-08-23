# Compare AI — backend plan

One endpoint: `POST /api/v1/openrouter/compare/`. One prompt, 2–5 models from
the OpenRouter catalogue, answered **in parallel** and returned in one
envelope. The frontend renders them side by side on the new `/compare` page
(frontend plan: `../chattydesk/compare-ai.md`).

Companion to the existing endpoints in `openrouter_handler`; reuses the same
helpers, the same error semantics, the same envelope. **No new models, no
migrations, no new env vars, no deploy.sh changes.**

---

## Decisions (the short version)

- **Parallel, not sequential.** gunicorn runs sync workers with
  `GUNICORN_TIMEOUT=180` (deploy.sh) and each upstream call may take up to
  `OPENROUTER_TIMEOUT=120` (client.py). In sequence, five models could total
  600s and the worker gets killed. In parallel the request is bounded by the
  slowest model (≤ ~120s), which fits the existing timeout budget. Threading
  is not an optimisation here; it is what makes the feature fit the
  deployment.
- **One request, one fan-out.** The frontend sends a single POST; the backend
  fans out to N models. A client-side loop would be N sequential round-trips,
  N auth refreshes, and N entries in chat history — and it would still hit
  the same worker-capacity wall, just slower.
- **Per-slot errors, mixed results.** A slot that fails (402 out of credit,
  429, moderation refusal, bogus model id) returns `error` in that slot and
  the other columns still render. Only when **all** slots fail does the whole
  request fail, with the first slot's mapped status.
- **No history writes.** A compare is ephemeral: nothing goes into
  `HistoryPrompt`, so the history sidebar stays clean and sqlite sees only
  reads (`settings_for` during key selection) — no write-lock contention
  across the worker threads.

---

## Endpoint spec

`POST /api/v1/openrouter/compare/` — authenticated (the `IsAuthenticated`
REST_FRAMEWORK default, same as `GenerateChat`; inference costs money).

### Request

```json
{
  "message": "Explain quicksort in two sentences.",
  "models": ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4.5"],
  "system_prompt": "…",
  "temperature": 0.7,
  "max_tokens": 1024
}
```

| Field | Rule |
| --- | --- |
| `message` | required, non-blank (same rule as `GenerateChat`) → else 400 |
| `models` | required list of **2–5** non-blank strings, in display order → else 400. **Duplicates allowed** — two columns of the same model are two independent samples (a 3-line check can forbid them later if unwanted) |
| `system_prompt` | optional string; absent → `client.DEFAULT_SYSTEM_PROMPT` (same as chat) |
| `temperature` | optional float, validated via the existing `_as_float` → 400 on garbage |
| `max_tokens` | optional int, validated via the existing `_as_int` → 400 on garbage |

Model ids are **not** checked against the catalogue up front: an unknown id
is rejected by OpenRouter per slot and surfaces as that slot's error. Zero
extra upstream calls per request. (A v2 could validate against the cached
`list_models` instead.)

### Response — 200

```json
{
  "status": "success",
  "message": "Success",
  "code": 200,
  "data": {
    "prompt": "Explain quicksort in two sentences.",
    "results": [
      {
        "model": "openai/gpt-4o-mini",
        "response": "Quicksort picks a pivot…",
        "usage": { "prompt_tokens": 41, "completion_tokens": 118, "total_tokens": 159 },
        "used_own_key": false,
        "duration_ms": 2140,
        "error": null
      },
      {
        "model": "anthropic/claude-sonnet-4.5",
        "response": "A divide-and-conquer sort…",
        "usage": { "prompt_tokens": 41, "completion_tokens": 97, "total_tokens": 138 },
        "used_own_key": true,
        "duration_ms": 4803,
        "error": null
      }
    ]
  }
}
```

- `results` is in **request order** (display order), not completion order —
  `ThreadPoolExecutor.map` preserves order even though slots finish
  out of order.
- A failed slot carries only `model`, `error`, `duration_ms`. The frontend
  checks `error` first, so the missing fields are intentional, not an
  accident of serialisation.
- `used_own_key` is per slot: the key fallback (`_complete`) runs
  independently for each slot, so one slot may bill the server key while its
  neighbour bills the user's own key.

### Response — all slots failed

No columns to show, so the whole request fails with the **first** slot's
error, status mapped exactly like `GenerateChat` via `_upstream_status`:

| Upstream | HTTP returned |
| --- | --- |
| 401 (key refused) | 502 — never a 401 to the client; that would make the frontend try to refresh a session token that was never the problem |
| 400–599 (402, 404, 429, 5xx…) | passed through unchanged |
| anything else | 502 |

Errors land in the usual envelope: `{ "status": "error", "message": "OpenRouter error: …" }`.

---

## Implementation

### 1. `openrouter_handler/views.py` — add `CompareModels(APIView)`

New imports: `import time`, `from concurrent.futures import ThreadPoolExecutor`.

```python
class CompareModels(APIView):
    """POST one prompt to 2–5 models at once; answers come back side by side.

    Requires a bearer token (the REST_FRAMEWORK default) for the same reason
    as GenerateChat: inference costs money. Unlike chat, nothing is written
    to history — a compare is ephemeral by design.
    """

    def post(self, request, *args, **kwargs):
        message = request.data.get("message")
        if not message or not str(message).strip():
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
```

Notes:

- `client.chat_completion` wraps **every** upstream failure mode (timeout →
  504, connection → 502, HTTP ≥ 400 → passthrough, non-JSON → 502, no
  choices → 502) in `OpenRouterError`, so catching that one type per slot is
  complete. Anything else propagating is a real bug and should 500 — the
  same stance `GenerateChat` takes.
- `_build_messages(message, None, system_prompt, use_history=False)` builds
  `[system?, user]` with no history replay. No `conversation_id`, no
  `use_history` on the wire — a compare is stateless.
- Threads are pure I/O (requests calls against OpenRouter) and the GIL is
  irrelevant; five short-lived threads inside one worker is nothing.
- **Capacity:** each active compare occupies one of the 3 sync gunicorn
  workers for up to ~2 minutes, so 3 compares can run at once before
  requests queue. Fine for v1. If it ever hurts, raise `WEB_CONCURRENCY` or
  switch the flag to gthread (`--threads`) — a deploy-time change, no code.

### 2. `openrouter_handler/urls.py`

```python
from openrouter_handler.views import CompareModels, GenerateChat, GetHistoryPrompt, ListModels

urlpatterns = [
    # …
    path("compare/", CompareModels.as_view(), name="openrouter-compare"),
]
```

Full URL: `/api/v1/openrouter/compare/` (mounted under the existing
`openrouter/` prefix like the other four paths).

### 3. No changes

- `client.py` — untouched; `chat_completion`, `extract_text`, `OpenRouterError`
  are reused as-is.
- `models.py` — no new models, no migration.
- `settings.py` — auth default already right; no throttle scope needed for
  v1. (Optional follow-up: a compare-specific `UserRateThrottle`, since one
  compare spends up to 5× a normal chat turn from the shared key.)
- `deploy.sh` / docker-compose — untouched; the timeout maths already works.

---

## Tests — new file `openrouter_handler/tests.py`

The backend currently has no test suite; this endpoint creates it. Django
`TestCase` + DRF `APIClient`, `force_authenticate` with a plain user,
`unittest.mock.patch` on `openrouter_handler.views.client.chat_completion`
(patching through `views` exercises `_complete` and the key fallback for
real). A fake payload helper returns

```python
{"model": m, "choices": [{"message": {"content": f"answer from {m}"}}],
 "usage": {"total_tokens": 42}}
```

Cases:

1. **auth** — anonymous POST → 401.
2. **message required** — missing/blank `message` → 400.
3. **models arity** — missing, one id, six ids, non-list, blank entry → 400
   each (parametrised).
4. **happy path** — 3 models → 200; `results` in request order; each has
   `response`, `usage`, `used_own_key`, `duration_ms`, `error: null`; `prompt`
   echoed.
5. **slot isolation** — mock `side_effect` raises `OpenRouterError(…, 402)`
   for one model id, succeeds for the rest → 200, failed slot carries
   `error`, the others carry `response`.
6. **all failed** — every id raises → status maps from the first failure
   (402 → 402; 401 → 502).
7. **no history** — after a successful compare,
   `HistoryPrompt.objects.count() == 0`.
8. **key fallback per slot** (optional but worth it) — user has their own key
   saved; mock raises 402 when `api_key` is the server key, succeeds on the
   user key → slot succeeds with `used_own_key: true`, proving `_complete`
   falls back correctly inside a worker thread.
9. **parallelism** (loose, anti-flake) — each mocked call sleeps 0.2s;
   3 models → assert `duration_ms` total is < ~0.5s (would be ≥ 0.6s if the
   code ran sequentially). Timing-based, so keep the margins generous.

Run: `python manage.py test openrouter_handler`.

---

## Manual verification (curl, before the frontend exists)

1. Start the dev server and sign in:
   `curl -s -X POST localhost:8000/api/v1/auth/login/ -H 'Content-Type: application/json' -d '{"username":"…","password":"…"}'` → `data.access`.
2. A real compare:
   `curl -s -X POST localhost:8000/api/v1/openrouter/compare/ -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"message":"Explain quicksort in two sentences.","models":["openai/gpt-4o-mini","meta-llama/llama-3.3-70b-instruct"]}'`
   → two results, wall clock ≈ slowest model.
3. Mixed success: swap one id for `"not/a-real-model"` → that slot has
   `error`, the other still answers.
4. Validation: 6 ids → 400; no token → 401; missing `message` → 400.

---

## Order of work

1. `views.py`: `CompareModels` (+ the two imports).
2. `urls.py`: the `compare/` path.
3. `tests.py`: the cases above; `python manage.py test openrouter_handler`.
4. curl verification against the dev server.
5. Then the frontend side (types → service → icon/navbar/route → page →
   tests), per the frontend plan.

## Notes / decisions worth confirming

- **No history writes, no streaming, duplicates allowed** — same v1
  positions as the frontend plan. Streaming would mean per-slot SSE through
  the envelope machinery; deliberately out of scope.
- **Wire order = slot order**, even though completions finish out of order —
  the frontend can render columns as they are, no re-sorting.
- **First-failure status on all-failed** — one status has to win when every
  column is dead; the first slot's is as good as any and matches "left to
  right" reading order.
