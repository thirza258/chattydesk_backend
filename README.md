# ChattyDesk backend

Django + DRF API for ChattyDesk. All inference goes through
[OpenRouter](https://openrouter.ai), a single OpenAI-compatible gateway in front of
400+ text models — so users pick any model instead of being limited to four hardcoded
providers, and the server needs one API key instead of four SDKs.

API contract for the frontend: **[FRONTEND_HANDOVER.md](./FRONTEND_HANDOVER.md)**.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/api/v1/auth/register/` | – | Create an account, get a token pair |
| `POST` | `/api/v1/auth/login/`    | – | Username + password, get a token pair |
| `POST` | `/api/v1/auth/refresh/`  | – | Exchange a refresh token for a new access token |
| `GET`  | `/api/v1/auth/me/`       | ✓ | The signed-in account |
| `GET`/`PATCH` | `/api/v1/auth/settings/` | ✓ | Preferences, including the user's own OpenRouter key |
| `GET`  | `/api/v1/openrouter/models/`  | – | Model catalogue for the picker (cached 1h) |
| `POST` | `/api/v1/openrouter/chat/`    | ✓ | Send a prompt to any model |
| `GET`  | `/api/v1/openrouter/history/` | ✓ | The caller's prompts/answers, paginated |

`/api/v1/{gpt,gemini,claude,mistral}_handler/` still respond, each pinned to one model,
so the existing frontend keeps working. They're deprecated — and they now need a token
too, being the same chat view with a preset model.

## Accounts and JWT

Authentication is [Simple JWT](https://django-rest-framework-simplejwt.readthedocs.io)
over `django.contrib.auth.User` — no custom user model, so `createsuperuser` and the
admin work as they always did. Clients send `Authorization: Bearer <access token>`;
`IsAuthenticated` is the project-wide default and the public endpoints (register, login,
refresh, the model catalogue) opt out explicitly.

Access tokens last an hour and refresh tokens a week (`JWT_ACCESS_MINUTES`,
`JWT_REFRESH_DAYS`). There is no token blacklist: signing out is the client dropping its
tokens. Sign-in, sign-up and refresh are throttled per IP (`AUTH_THROTTLE_RATE`,
20/min by default).

Chat history is scoped to the caller — `HistoryPrompt.user`, added in
`openrouter_handler.0003`. The column is nullable because rows written before accounts
existed cannot be attributed to anyone; they stay on disk and are no longer served.

## Bring-your-own OpenRouter key

`accounts.UserSettings` lets a user save their own key, so their chats survive the
server's key running out of credit. `PATCH /api/v1/auth/settings/` verifies a new key
against OpenRouter's `/key` endpoint before storing it, and only ever returns the last
four characters afterwards.

The key is encrypted at rest with Fernet (`accounts/crypto.py`), using a key derived
from `SECRET_KEY` unless `SETTINGS_ENCRYPTION_KEY` is set. That protects a leaked
database dump, not a leaked environment. Rotating either secret makes saved keys
undecryptable — the API then reports "no key saved" and chats fall back to the server's
key, so nothing breaks beyond users re-entering theirs.

By default the server's key is tried first and the user's is the fallback (on `401`,
`402`, `403` or `429`); `prefer_own_key` flips the order. A `400` is never retried with
the other key — a bad model id fails the same way twice. The chat response reports which
key paid via `used_own_key`.

## Local setup

```bash
python3 -m venv env && source env/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then set SECRET_KEY and OPENROUTER_API_KEY
python manage.py migrate
python manage.py runserver
```

Get an API key at <https://openrouter.ai/keys>. Every setting is documented in
`.env.example`.

## Deploy

`./deploy.sh` runs the whole release: loads `.env` (process env wins), validates the
config, installs dependencies, collects static files, migrates, runs
`manage.py check --deploy`, then execs gunicorn on `$PORT`.

```bash
./deploy.sh                # full release, then serve
./deploy.sh --no-serve     # release steps only
./deploy.sh --serve-only   # just boot gunicorn
./deploy.sh --help
```

The `Procfile` uses it for both process types:

```
release: ./deploy.sh --no-serve --skip-install
web: ./deploy.sh --skip-install
```

The `web` line deliberately re-runs `collectstatic` and `migrate` before booting:
migrations are idempotent, and not every platform actually fires the `release` phase.
If yours does (Heroku), use `web: ./deploy.sh --serve-only` to shave a few seconds off
boot.

Required in production: `SECRET_KEY`, `OPENROUTER_API_KEY`, `DATABASE_URL`
(or `DEVELOPMENT_MODE=True` for sqlite), `DJANGO_ALLOWED_HOSTS`.

`SECRET_KEY` now signs JWTs and (by default) encrypts saved OpenRouter keys, so changing
it signs everyone out and invalidates those keys. Set `JWT_SIGNING_KEY` and
`SETTINGS_ENCRYPTION_KEY` if you want to rotate the three independently.

## Chat history migration

History used to live in `gpt_handler_historyprompt`. Migration
`openrouter_handler.0002` copies those rows into `openrouter_handler_historyprompt` on
first `migrate`, timestamps intact, and skips cleanly on a fresh database. The old table
is left in place as a backup — drop it once you're satisfied:

```sql
DROP TABLE gpt_handler_historyprompt;
```

# Frontend Handover — ChattyDesk backend on OpenRouter

**What changed:** the backend no longer talks to OpenAI, Google, Anthropic and Mistral
through four separate SDKs and four separate endpoints. Everything now goes through
**OpenRouter**, a single OpenAI-compatible gateway that proxies 400+ text models.

**What this means for the frontend:** instead of one endpoint per provider, there is
one chat endpoint plus a model-catalogue endpoint. The user picks any model from the
catalogue and the same request shape works for all of them.

Your existing endpoints still work (see [Legacy endpoints](#legacy-endpoints)), so
nothing breaks the moment this deploys. Migrate when convenient.

---

## Base URL

```
<host>/api/v1/openrouter/
```

All responses use the same envelope you already handle:

```jsonc
{
  "status": 200,          // mirrors the HTTP status
  "message": "Success",   // error text when status >= 400
  "data": { ... }         // absent on errors
}
```

Chat, history and settings require `Authorization: Bearer <access token>` from
`/api/v1/auth/login/` — see [Accounts and JWT](#accounts-and-jwt) above. The model
catalogue stays public. No OpenRouter key is ever needed from the client; the server's
key pays unless the user saved one of their own.

---

## 1. List models — `GET /api/v1/openrouter/models/`

Feeds the model picker. Public and cached server-side for 1 hour.

| Query param | Default | Meaning |
|---|---|---|
| `search`    | –       | Case-insensitive substring match on model id and name |
| `free`      | `false` | Only models whose prompt and completion price are both 0 |
| `text_only` | `true`  | Only models that can output text (vision *inputs* are still included) |
| `refresh`   | `false` | Bypass the server cache |

**Response**

```jsonc
{
  "status": 200,
  "message": "Success",
  "data": {
    "count": 413,
    "default_model": "openai/gpt-4o-mini",
    "models": [
      {
        "id": "anthropic/claude-haiku-4.5",       // <- send this as `model`
        "name": "Anthropic: Claude Haiku 4.5",     // <- display this
        "provider": "anthropic",                   // <- good for grouping/icons
        "description": "…",
        "context_length": 200000,
        "max_completion_tokens": 64000,
        "input_modalities": ["text", "image", "file"],
        "output_modalities": ["text"],
        "supported_parameters": ["max_tokens", "temperature", "tools", "…"],
        "pricing": { "prompt": "0.000001", "completion": "0.000005" },  // USD per token, as strings
        "is_free": false
      }
    ]
  }
}
```

Notes for the picker UI:

- `models` comes back sorted by display name.
- Group by `provider` to get the old "ChatGPT / Gemini / Claude / Mistral" tabs back —
  they are just `openai`, `google`, `anthropic`, `mistralai` now.
- `is_free: true` is a nice "Free" badge; there are ~19 such models today.
- Pricing is USD **per token** as a string. Multiply by 1,000,000 for the usual
  "per 1M tokens" display.
- Ids can change over time. Don't hardcode a list — always read it from this endpoint,
  and fall back to `data.default_model` if a stored user preference is no longer present.

---

## 2. Send a chat message — `POST /api/v1/openrouter/chat/`

(`POST /api/v1/openrouter/` is an alias for the same view.)

**Request**

```jsonc
{
  "message": "Explain WebSockets",              // required
  "model": "anthropic/claude-haiku-4.5",        // optional, defaults to data.default_model
  "conversation_id": "5f0f…",                   // optional, omit on the first turn
  "system_prompt": "You are…",                  // optional, overrides the server default
  "use_history": true,                          // optional, default true
  "temperature": 0.7,                           // optional
  "max_tokens": 2048                            // optional
}
```

**Response** — `200`

```jsonc
{
  "status": 200,
  "message": "Success",
  "data": {
    "response": "## WebSockets\n\nA WebSocket is …",   // Markdown, render as before
    "model": "anthropic/claude-haiku-4.5",             // model that actually served it
    "conversation_id": "5f0f…",                        // store and send back on the next turn
    "usage": { "prompt_tokens": 412, "completion_tokens": 380, "total_tokens": 792 }
  }
}
```

### Three behaviour changes worth reading

1. **`data.response` is now plain Markdown text.**
   The old handlers forced every model to answer with a JSON object `{"response": "..."}`
   and parsed it server-side. That trick doesn't survive 400 models — many don't support
   structured outputs and would have leaked ` ```json ` fences into your chat bubbles.
   The field name is unchanged, so existing Markdown rendering keeps working.

2. **Conversations now have real memory.**
   `conversation_id` used to be stored and ignored. Now, when you send one back, the
   server replays the last 10 turns of that conversation to the model. Send the
   `conversation_id` you got from the previous response to continue a thread; omit it to
   start a fresh one (the server generates and returns a new UUID). Set
   `"use_history": false` for a genuinely one-off request.

3. **`usage` is new.** Use it if you want to show token counts or cost.

### Full manual control (optional)

If you'd rather own the conversation state client-side, send an OpenAI-style
`messages` array instead. When present it replaces the server-built context entirely:

```jsonc
{
  "message": "…",       // still required — this is what gets logged to history
  "model": "openai/gpt-4o-mini",
  "messages": [
    { "role": "system", "content": "You are a terse assistant." },
    { "role": "user", "content": "Hi" },
    { "role": "assistant", "content": "Hello." },
    { "role": "user", "content": "…" }
  ]
}
```

### Errors

| HTTP | When | `message` |
|---|---|---|
| 400 | `message` missing/empty, or `temperature`/`max_tokens` not numeric | `Bad Request: …` |
| 401 / 402 | Server key is invalid, or the OpenRouter account is out of credit | `OpenRouter error: …` |
| 404 | The `model` id doesn't exist on OpenRouter | `OpenRouter error: …` |
| 429 | Rate limited upstream | `OpenRouter error: …` |
| 502 / 504 | OpenRouter unreachable or timed out (>120s) | `OpenRouter error: …` |
| 500 | Server key not configured, or an unexpected failure | `Internal Server Error: …` |

Upstream status codes are passed through, so a 402 or 429 is genuinely OpenRouter's and
worth surfacing to the user ("out of credit", "too many requests"). Nothing is written
to history when a request fails.

---

## 3. History — `GET /api/v1/openrouter/history/`

| Query param | Default | Meaning |
|---|---|---|
| `conversation_id` | – | Only this thread |
| `model` | – | Only rows served by this exact model id |
| `limit` | `100` | Max 500 |
| `offset` | `0` | Pagination offset |

```jsonc
{
  "status": 200,
  "message": "Success",
  "data": {
    "count": 132,          // total matching rows, not the page size
    "limit": 100,
    "offset": 0,
    "results": [           // newest first
      {
        "id": 13,
        "prompt": "Hello",
        "response": "Hello! How can I help you today?",
        "conversation_id": "81146677-…",
        "model_name": "openai/gpt-4o-mini",
        "created_at": "2025-05-29T17:08:45.463267Z"
      }
    ]
  }
}
```

To rebuild a thread in the UI: `?conversation_id=<id>` and reverse the list.

**Your existing history data was migrated** into the new table, timestamps intact. Rows
created before this change carry the old display names in `model_name` (`ChatGPT`,
`claude`, `gemini`, `Mistral`); new rows carry real OpenRouter ids
(`openai/gpt-4o-mini`). Treat `model_name` as a label, not a key — if you filter by
`?model=`, expect old rows not to match.

---

## Legacy endpoints

These still work and are unchanged in shape. Each is pinned to one model:

| Old endpoint | Now calls |
|---|---|
| `POST /api/v1/gpt_handler/` | `openai/gpt-4o-mini` |
| `POST /api/v1/gemini_handler/` | `google/gemini-2.5-flash` |
| `POST /api/v1/claude_handler/` | `anthropic/claude-haiku-4.5` |
| `POST /api/v1/mistral_handler/` | `mistralai/mistral-large` |
| `GET /api/v1/gpt_handler/history/` | Same bare-array `data` as before |

They accept `message` + `conversation_id` and return `data.response` exactly as before.
Two of them are on newer models than the originals (`claude-3-5-haiku` and
`gemini-2.0-flash-exp` are retired); the backend can repoint them via env vars without a
frontend change.

**Please migrate to `/api/v1/openrouter/`** — these paths are deprecated and only exist
so the switchover isn't a hard cutover.

---

## Suggested migration path

1. Point the existing chat call at `POST /api/v1/openrouter/chat/`, sending
   `model` from wherever the user's provider choice lives today. Everything else in your
   code stays the same — `data.response` is still Markdown.
2. Swap the hardcoded 4-provider selector for a picker populated from
   `GET /api/v1/openrouter/models/`. Persist the chosen `model` id in local storage.
3. Start round-tripping `conversation_id` to get multi-turn memory.
4. Switch the history view to `/api/v1/openrouter/history/` and read `data.results`
   instead of `data`.

### Minimal TypeScript client

```ts
const API = `${import.meta.env.VITE_API_URL}/api/v1/openrouter`;

export type Model = {
  id: string; name: string; provider: string; description: string;
  context_length: number | null; is_free: boolean;
  pricing: { prompt: string | null; completion: string | null };
};

export async function listModels(search = ""): Promise<Model[]> {
  const res = await fetch(`${API}/models/?search=${encodeURIComponent(search)}`);
  const body = await res.json();
  if (!res.ok) throw new Error(body.message);
  return body.data.models;
}

export async function sendMessage(opts: {
  message: string; model?: string; conversationId?: string | null;
}) {
  const res = await fetch(`${API}/chat/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message: opts.message,
      model: opts.model,
      conversation_id: opts.conversationId ?? undefined,
    }),
  });
  const body = await res.json();
  if (!res.ok) throw new Error(body.message);          // surface OpenRouter errors verbatim
  return body.data as {
    response: string; model: string; conversation_id: string;
    usage: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number };
  };
}
```

---

## Things to know

- **Latency varies a lot by model.** A big reasoning model can take 60s+ where
  `gpt-4o-mini` takes 2s. Keep the send button disabled and the spinner up; the server
  gives up at 120s (gunicorn at 180s). Consider warning the user on models with high
  `pricing` or huge `context_length`.
- **No streaming yet.** Responses arrive in one piece. OpenRouter supports SSE and the
  backend can add a `/chat/stream/` endpoint if you want token-by-token rendering — it
  needs a gunicorn worker-class change on the server, so ask before building UI for it.
- **Model availability shifts.** A model in your cache today can disappear next month;
  handle a 404 from `/chat/` by re-fetching the catalogue and falling back to
  `default_model`.
- **`text_only=true` is the default** and keeps image-generation models out of the
  picker. Vision models (image *input*) are included — but this API only accepts text
  prompts today, so image upload would need a backend change.
- **CORS** is currently open (`CORS_ALLOW_ALL_ORIGINS = True`), unchanged from before.


 https://claude.ai/code/artifact/36e946bb-ad9e-474c-bc2e-94a5f045e368
