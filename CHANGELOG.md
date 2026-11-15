# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased] — fork Muxee4ka/gemini-webapi-proxy

### Changed
- Media of a slave is stored on the master only: images arrive as base64 and go to
  the master's gallery attributed to the slave (one tab per slave in the Media page);
  after the master downloads a video the slave deletes its copy (`DELETE /node/jobs/{id}`,
  one-hour fallback cleanup). Default retention is now 90 days for images and videos
  (`GOP_IMAGE_KEEP_HOURS` / `GOP_VIDEO_KEEP_HOURS`, was 24 h)
- Master/slave rebuilt around an explicit mode (`GOP_MODE=master|slave`, asked by the
  installers via `--mode` / `-Mode` or interactively). A **slave** can no longer add or
  change accounts itself (its admin API answers 403, the Telegram bot is read-only,
  `/openai/v1` is not served) — every account configuration and status change is made
  from the **master** over the link: new `/node/accounts*` API on the slave and
  `/admin/nodes/{id}/accounts/…` forwarding on the master (create, rename, enable,
  delete, cookies, check, reset limits, sign-in). Slave egress is enforced: proxies
  (global and per-account) are ignored in slave mode, so account traffic can only
  leave via the slave's own IP. Docs: docs/nodes.md

### Added
- Telegram bot (admin UI → Telegram; `docs/telegram.md`): admins linked by a one-time
  link, `/status`, `/accounts` with enable/disable/reset/check buttons, `/stats`, `/events`;
  alerts from the pool (`events.py`): logged out, abuse flag, quota, 429, all accounts
  down, recovered — throttled to once per 30 min per account and kind
- `scripts/install.ps1` (Windows, Docker Desktop) and `docker-compose.desktop.yml`: no host
  networking, the browser shares the proxy's network, only the proxy port is published;
  `install.sh --desktop` (automatic on macOS). Browser profile in a named volume.
  `.gitattributes` keeps `*.sh` LF on Windows checkouts
- `scripts/install.sh`: one-command server install. Root `docker-compose.yml` now runs
  the proxy and the sign-in browser together (cookie source `cdp`, no default HTTP proxy,
  browser ports via `GOP_CDP_PORT` / `GOP_VNC_PORT` / `GOP_RFB_PORT`); the macOS Safari
  flow (`scripts/docker-up.sh`) keeps `GOP_COOKIE_SOURCE=env` and starts the proxy only
- `data/` is ignored by git and left out of the Docker build context (it holds account
  cookies, client keys and the request journal)
- `stream=true` for chat completions (the answer is generated in full, then sent as SSE)
- Tool-calling emulation: OpenAI `tools` → prompt block, `<tool_calls>` answer → `tool_calls`
- `POST /openai/v1/images/edits` — instruction-based edits of uploaded images
- Video generation (Veo) in the OpenAI Videos API shape: `POST /openai/v1/videos`,
  `GET /openai/v1/videos/{id}`, `GET /openai/v1/videos/{id}/content`
- `GOP_VIDEO_TIMEOUT`, `GOP_VIDEO_KEEP_HOURS`; `GEMINI_COOKIE_PATH` in `docker-compose.yml`
- OpenClaw integration: `docs/openclaw.md`, `integrations/openclaw/gemini-video` skill
- Cookie source `cdp`: the session is signed in once in a Chrome on the server and
  taken over DevTools (`GOP_CDP_URL`); the browser then leaves Gemini so only the proxy
  uses the session. Container in `integrations/cdp-browser`
- `GET /admin/quota` — the account's limits as gemini-webapi last read them: credits
  per model, 5-hour and weekly usage, tier, reset times (the video limit is not reported)

- Several Google accounts (`<data_dir>/accounts.json`): requests go to the first account
  in priority order that has the model; on exhausted quota or a lost session the request
  moves to the next account, the first one cools down per model until its reset time
  (`GOP_QUOTA_COOLDOWN`, `GOP_VIDEO_COOLDOWN`). `docs/accounts.md`
- Admin UI `/admin/ui` (`GOP_ADMIN_PASSWORD`): accounts' state, models, limits and
  cooldowns; sign-in through the server's browser shown over noVNC (`GOP_VNC_URL`; the
  cdp-browser image serves it), after which the browser clears all its cookies; pasting a
  cookie string; enable/disable, reorder, check. API under `/admin/accounts`
- `UsageLimitExceededError` is 429 (was 500); a temporarily blocked IP is 503

### Fixed
- Data dir in the Docker image is `/app/data` (`GOP_DATA_DIR`): the model registry and
  videos used to land in `/src/data` inside the container, outside the volume
- Video jobs: the stream is allowed to stay silent for `GOP_VIDEO_TIMEOUT` and the prompt
  is never re-sent (Veo answers after minutes; the 120 s watchdog used to drop the turn
  and send the prompt again)

### Changed
- Docker image 2.86 GB → ~210 MB: `python:3.12-slim`, no Playwright/Chromium. The
  Playwright downloaders are removed (they always failed on a missing setting, images came
  through `httpx-rpc`); the `cdp` source speaks DevTools over a plain websocket
- `gemini-webapi` ≥ 2.1.1: the model list is the account's live list
  (`gemini-flash`, `gemini-pro`, `gemini-argon`, `gemini-flash-lite`); image aliases route there
- Chat refusals are 502 instead of 403; exhausted quota is 429 instead of 500
- A missing model triggers a re-read of the account's model list (at most once a minute)
- `docs/api.md` rewritten in English with the new endpoints
- A logged-out (guest) client is re-initialised at most once a minute: no longer on
  every failing request, and no longer kept until a restart

## [0.1.0] - 2026-06-23

### Added
- Pluggable cookie sources: `browser`, `env`, `file` (Netscape cookies.txt)
- Pluggable image downloader chain (5 strategies, configurable order)
- `BaseGeminiClient` interface with `WebAPIClient` reference adapter
- Optional `GOP_API_KEY` Bearer auth (off by default for local use)
- `/ready` health endpoint that probes the Gemini client
- Structured JSON logging (`GOP_LOG_FORMAT=json`)
- Image-generation refusals from upstream are now mapped to **HTTP 403**
  with the original Gemini message preserved, instead of a generic 500
- Plain-text upstream refusals ("I cannot fulfill this request.", "I'm
  unable to help with that request.", …) raised by chat-completion
  responses are detected and mapped to **HTTP 403** with an
  `UpstreamRefusalError`, so OpenAI clients no longer mistake them
  for successful answers
- `GOP_WEBAPI_MAX_RETRY` knob (default `1`) to cap the auto-retry
  behaviour of the underlying `gemini-webapi` RPC layer; surfaces real
  errors quickly instead of burning 5× the timeout
- English + Chinese README, full configuration & downloaders docs
- Project structure: `src/` layout, `pyproject.toml` (hatchling), `[project.scripts]`
- Test suite: 69 tests
- CI workflow: ruff + mypy + pytest matrix (Python 3.10/3.11/3.12, Ubuntu + macOS)
- Multi-platform Docker image published to `ghcr.io/Lutiancheng1/gemini-webapi-proxy`

### Changed
- All settings now use `GOP_` env prefix; old unprefixed names still work
  as legacy aliases
- HTTP errors now go through a single `map_api_error` mapper
- FastAPI app reorganised: routes split into `gemini_webapi_proxy.routes.*`
- `sync_runtime_env.py` now writes the **full** Safari cookie jar
  (35 cookies) to `GOP_GEMINI_COOKIES_RAW`, not just `__Secure-1PSID` /
  `__Secure-1PSIDTS`. Without the extras, the env-mode container
  was rejected by Gemini ("image creation isn't available").
- Dockerfile switched to `python:3.12-bookworm` (was `slim`); the slim
  image's OpenSSL 3.x is incompatible with the prebuilt `curl_cffi`
  aarch64 wheel, which throws `OPENSSL_internal: invalid library`.
- `docker compose` now uses `network_mode: host` (no more
  `host.docker.internal` magic, no port mapping needed)
- `scripts/docker-up.sh` now sets `GOP_BUILD_HTTP_PROXY` for the
  `docker build` step automatically

### Fixed
- `ModelRegistry.resolve_id()` no longer back-matches stable image
  aliases (e.g. `gemini-2.5-pro-image.aliases = ['gemini-3-pro']`).
  Without this guard, asking for `gemini-3-pro` could route to
  `gemini-2.5-pro-image` whenever the underlying model was missing
  from `_runtime` (e.g. transient `UNAUTHENTICATED` from Gemini),
  surfacing the misleading upstream error
  `Unknown model name: gemini-2.5-pro-image` against a request for
  the chat-only `gemini-3-pro` model.
- Dockerfile: stop building a pre-baked wheel in the builder stage and
  instead `pip install -e .` the live source tree in the runtime stage.
  The wheel path was copying `src` into a separate layer that could
  be served from cache even when the host source had changed, leading
  to stale code inside the image.
- Dockerfile: wrap the `apt-get` Chromium-deps layer in a 3-attempt
  retry loop. GitHub Actions runners occasionally hit
  `503 upstream connect error` from `deb.debian.org` mid-build,
  failing the job on a transient network blip that a single retry
  survives. Verified on the Docker workflow (CI run 28033739597).

### Removed
- Hard-coded path to `gemini-web-to-api/.env`
- Hard-coded Safari-first browser order on non-macOS platforms
- `ports:` and `extra_hosts:` from `docker-compose.yml` (replaced by
  `network_mode: host`)

### Security
- Cookie-file writes (`data/runtime.env`) are now `chmod 600`
- API key auth is opt-in; when unset, behaviour matches the previous
  local-only mode

### Verified end-to-end (2026-06-22)

Real traffic was driven through both the local venv and the Docker
container, using Safari cookies for the signed-in Gemini session:

| Path                                    | chat  | image      | notes |
|-----------------------------------------|-------|------------|-------|
| `venv` + `GOP_COOKIE_SOURCE=browser`    |  ✅   | ✅ 5.3 MB  | reference path |
| `docker compose` (bookworm) + `env`     |  ✅   | ✅ 4.8 MB  | what end users see |
| `docker compose` (slim, pre-fix)        |  ✅   | ❌ 500     | `curl_cffi` TLS error |

Sample prompts: "a simple red apple on white background" (venv), "a
tiny orange hexagon on white background" (Docker). Both returned real
PNG bytes (magic verified) within ~40s.

### Verified end-to-end (2026-06-23)

The curated 2-chat + 2-image model list (above) was re-verified through
both the Docker container and a local venv after the registry +
Dockerfile fixes:

| Path             | `gemini-3-flash` chat | `gemini-3-pro` chat | `gemini-2.5-flash-image` | `gemini-2.5-pro-image` |
|------------------|-----------------------|---------------------|--------------------------|-------------------------|
| Docker container | ✅                    | ✅                  | ✅ 5.5 MB apple          | ✅ 7.0 MB lemons         |
| venv (`-m`)      | ✅                    | ✅                  | ✅ 7.5 MB orange         | ✅ 8.2 MB succulent      |

All four images were real PNG bytes (magic verified, sample output
saved to `/tmp/gop-test/`). The image-prompts that previously
returned `Unknown model name: gemini-2.5-pro-image` now resolve
correctly to the underlying `gemini-3-pro` runtime entry.

### Project polish (0.1.0)

- README + README.zh: TOC, hero screenshot, "Why this project?" section,
  troubleshooting quick-reference inline.
- New `docs/architecture.md` with data-flow diagrams and registry
  internals.
- `examples/` rewritten with 4 runnable demos (chat, image, image-with-
  reference, Outsider Studio setup).
- GitHub metadata: 8 repository topics, expanded description, homepage
  URL pointing at `docs/`.
- Engineering: `CODEOWNERS`, `.editorconfig`, `.gitattributes`,
  `.pre-commit-config.yaml`, `.github/ISSUE_TEMPLATE/config.yml`,
  Dependabot for github-actions and pip.
- CONTRIBUTING.md now correctly references Python 3.11+ and
  pre-commit hook setup.
- `docs/zh/` empty directory removed.

## [Unreleased]

### Added
- (work in progress; this section is for unreleased changes)

## [0.0.1] - 2025-06-18

Internal preview, pre-release, not published to PyPI.
