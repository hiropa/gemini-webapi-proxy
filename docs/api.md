# OpenAI-compatible API

Base URL for OpenAI clients: `http://localhost:4982/openai/v1`.

Auth: when `GOP_API_KEY` is set, every request needs `Authorization: Bearer <key>`
(missing key → 401, wrong key → 403). With an empty `GOP_API_KEY` auth is off.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness, never touches Gemini |
| GET | `/ready` | Ready once the Gemini client is initialised |
| GET | `/openai/v1/models` | Models the account can use right now |
| POST | `/openai/v1/chat/completions` | Chat; `stream`, `tools` (emulated), reference images |
| POST | `/openai/v1/images/generations` | Text-to-image, optional reference images |
| POST | `/openai/v1/images/edits` | Edit an uploaded image by instruction |
| POST | `/openai/v1/videos` | Start a video job (Veo) |
| GET | `/openai/v1/videos` | List video jobs |
| GET | `/openai/v1/videos/{id}` | Video job status |
| GET | `/openai/v1/videos/{id}/content` | Finished video, `video/mp4` |
| POST | `/admin/probe-models` | Re-probe every model and refresh the registry |
| GET | `/admin/quota` | Account usage limits (credits per model, 5-hour/weekly usage, reset times) |

## Models

The list comes from the Gemini Web account (`gemini-webapi` ≥ 2.1), so it follows
what Google currently offers to that account:

| id | What it is |
|----|------------|
| `gemini-flash` | Flash (3.8 at the time of writing), the default all-rounder |
| `gemini-pro` | Pro (3.1), slow |
| `gemini-argon` | Argon (Gemini 4) |
| `gemini-flash-lite` | Flash-Lite (3.5), fastest |
| `gemini-2.5-flash-image` | Stable image alias, routes to `gemini-flash` |
| `gemini-2.5-pro-image` | Stable image alias, routes to `gemini-pro` |

Old ids (`gemini-3-flash-preview`, `gemini-3-pro-preview`, `gemini-advanced`, …) still
resolve for compatibility but are not advertised.

If a requested model is missing from the list (for example, the proxy started while
the account was logged out or out of quota), the proxy re-reads the account's model
list, at most once a minute, before giving up.

```bash
curl -sS http://localhost:4982/openai/v1/models -H "Authorization: Bearer $KEY"
```

## Chat

```bash
curl -sS http://localhost:4982/openai/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "gemini-flash", "messages": [{"role": "user", "content": "Reply with exactly: pong"}]}'
```

- Each request is a fresh Gemini chat: the whole `messages` history is flattened into one
  prompt (`SYSTEM: …`, `USER: …`, `ASSISTANT: …`).
- `content` may be a string, `null`, or an array of parts; `image_url` parts are uploaded
  to Gemini as files.
- `usage` is always zeros: Gemini Web does not report tokens.

### Streaming

`"stream": true` is supported in the OpenAI SSE format. Gemini Web does not stream
through this proxy, so the full answer is generated first and then sent as chunks:
a content delta, a `finish_reason` chunk, an optional usage chunk
(`stream_options.include_usage`), and `data: [DONE]`.

### Tool calling (emulated)

Gemini Web has no function calling, so the proxy emulates it in the prompt:

1. The tool specs from `tools` are added to the prompt as a system block, with the rule
   to answer with `<tool_calls>[{"name": …, "arguments": {…}}]</tool_calls>` when a tool
   is needed. `tool_choice: "required"` or a named function adds a "you must call" rule;
   `tool_choice: "none"` skips the block.
2. The block in Gemini's answer is parsed into OpenAI `tool_calls` with
   `finish_reason: "tool_calls"`; text before the block becomes `content`.
3. Tool names Gemini Web renames after its built-ins (`image_gen` for `image_generate`)
   are matched back when the match is unique. Unknown names are dropped.
4. History `assistant.tool_calls` and `role: "tool"` messages are rendered in the same
   text format, so multi-step agent loops work.

```json
{"choices": [{"index": 0, "finish_reason": "tool_calls",
  "message": {"role": "assistant", "content": "Checking.",
    "tool_calls": [{"id": "call_…", "type": "function",
      "function": {"name": "get_weather", "arguments": "{\"city\": \"Moscow\"}"}}]}}]}
```

Limits: this is prompt engineering, not a native feature. For questions Gemini Web can
answer with its own widgets (weather, web search) it sometimes answers directly instead
of calling your tool. Agent-style tools (read files, run commands, generate images) are
called reliably. Agent prompts with many tools get long (tens of thousands of
characters), so give the client a generous timeout (≥ 300 s).

## Image generation

```bash
curl -sS -m 300 http://localhost:4982/openai/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "gemini-2.5-flash-image", "prompt": "a red apple on a white table", "n": 1}'
```

- The response always carries `data[].b64_json` (PNG/JPEG), never Google CDN URLs:
  most clients cannot fetch `lh3.googleusercontent.com` links.
- Reference images: the extension field `image` (string or array) with
  `data:image/png;base64,…` or `https://` URLs, top level or inside `extra_body`.
  The prompt is then wrapped as "use the attached image(s) as visual reference".
- One image takes about 30 s; Gemini returns 2048×2048 JPEGs.

## Image edits

OpenAI `images.edit` shape: multipart form with `image` (or `image[]`, several files)
and `prompt`; `model`, `n` and `size` are optional. A JSON body with `image` as data or
https URLs works too.

```bash
curl -sS -m 300 http://localhost:4982/openai/v1/images/edits \
  -H "Authorization: Bearer $KEY" \
  -F model=gemini-2.5-flash-image \
  -F "prompt=Add a modern kitchen along the right wall. Keep the window and the floor." \
  -F "image=@room.jpg;type=image/jpeg"
```

Gemini edits by instruction and is told to keep everything the instruction does not
change. `mask` is ignored. The response has the same format as for generations.

## Videos (Veo)

Same shape as the OpenAI Videos API. Generation runs in the background; one clip takes
1–3 minutes.

**Start a job.** JSON or multipart:

| Field | Required | Meaning |
|-------|----------|---------|
| `prompt` | yes | What happens in the video |
| `input_reference` | no | Image to animate: multipart file, or data/https URL in JSON (`image` is accepted too) |
| `seconds` | no | Duration hint (Veo decides; clips are about 8 s) |
| `size` | no | Aspect hint, e.g. `1280x720` or `720x1280` |
| `model` | no | Default `gemini-flash` |

```bash
curl -sS http://localhost:4982/openai/v1/videos \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt": "a ginger cat in a space helmet floating in a space station", "seconds": "8"}'
# {"id": "video_…", "object": "video", "status": "queued", "progress": 0, …}
```

**Poll** `GET /openai/v1/videos/{id}` until `status` is `completed` or `failed`
(`queued` → `in_progress` → `completed` / `failed`). Polling is local and cheap; poll
every 10–30 s.

**Download** `GET /openai/v1/videos/{id}/content` returns the mp4 (409 while the job is
not completed, 404 for an unknown id).

Failures come in `error.code`:

| `error.code` | Meaning | What to do |
|--------------|---------|------------|
| `video_limit_reached` | The account's video limit is used up. Gemini signals this by drawing an image instead of a video, or with limit/quota text | Wait for the daily reset |
| `refused` | Gemini refused the prompt (often "I'm having a hard time fulfilling…") | Rewrite it: no brand, cartoon, celebrity or real people's names |
| `upstream_glitch` | Transient Gemini error or timeout | Retry once, a few minutes later |

Rules the proxy enforces, learned from a browser bot that queued clips through the same
kind of account:

- Jobs run one at a time.
- A failed job is never retried automatically: re-sending a prompt burns the video limit.
- Jobs and files are kept for `GOP_VIDEO_KEEP_HOURS` (default: 90 days). The job list lives in
  memory: a restart forgets running and finished jobs.

## Quota

`GET /admin/quota` returns what gemini-webapi last read about the account's limits. It
sends nothing to Google: per-model credits are read on every client init, the 5-hour and
weekly usage after every generation.

```json
{
  "account_status": "AVAILABLE",
  "tier": "ULTRA",
  "usage": {"current_5h": {"remaining_credits": 900, "usage_percentage": 10, "reset_at": "…"}, "weekly": null},
  "models": [{"label": "Gemini Flash [None-11]", "remaining": 36685, "total": 48384, "usage_percentage": 24, "reset_at": "2026-10-19T19:39:22+00:00"}],
  "extra": {"is_blocked": false, "usage_percentage": 5.0, "reset_time": null},
  "abuse_status": {"status": "Clean"},
  "checked_at": 1792438000
}
```

Gemini does not report the video limit; it shows up only as a `video_limit_reached` job.

## Errors

OpenAI-style body: `{"error": {"message": "…", "type": "api_error"}}`.

| HTTP | Meaning | When |
|------|---------|------|
| 400 | Bad request | Missing `prompt` / image for edits and videos |
| 401 | Cookies expired | Re-export cookies, see [cookie-sources.md](cookie-sources.md) |
| 403 | Upstream refusal (images) | Gemini answered with a policy/capability refusal instead of an image |
| 404 | Not found | Unknown video id |
| 409 | Not ready | Video content requested before the job completed |
| 429 | Usage limit | Gemini says the account's limit is used up ("…as soon as your limit resets") |
| 500 | Internal error | Anything not classified above, e.g. "Unknown model name" |
| 502 | Upstream refusal (chat) / download failure | Chat refusals are 502, not 403, so agent clients don't treat them as broken auth; image CDN download failed |
| 504 | Timeout | `GOP_CHAT_TIMEOUT` / `GOP_IMAGE_TIMEOUT` exceeded |

## Image pipeline (internal)

```
POST /images/generations or /images/edits
  → gemini-webapi generate_content (cookies + optional files = reference images)
  → full-size RPC URL → CDN redirect chain
  → downloader chain (httpx → library save)
  → b64_json
```

Google CDN hosts are fetched directly, not through `GOP_HTTP_PROXY` (avoids MITM 403s).
