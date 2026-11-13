<div align="center">

# 🌐 Gemini WebAPI Proxy

**OpenAI-compatible reverse-engineered gateway to Google Gemini Web (chat + image gen).**
Drop-in `base_url` replacement for any OpenAI client — **no API key, reuses your browser login**.

[![Release](https://img.shields.io/github/v/release/Lutiancheng1/gemini-webapi-proxy)](https://github.com/Lutiancheng1/gemini-webapi-proxy/releases)
[![Last Commit](https://img.shields.io/github/last-commit/Lutiancheng1/gemini-webapi-proxy)](https://github.com/Lutiancheng1/gemini-webapi-proxy/commits/main)
[![CI](https://img.shields.io/github/actions/workflow/status/Lutiancheng1/gemini-webapi-proxy/ci.yml?branch=main&label=lint%2Ftest)](https://github.com/Lutiancheng1/gemini-webapi-proxy/actions)
[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org)
[![Docker](https://img.shields.io/badge/docker-ghcr.io%2Flutiancheng1%2Fgemini--webapi--proxy-blue)](https://ghcr.io/Lutiancheng1/gemini-webapi-proxy)
[![Stars](https://img.shields.io/github/stars/Lutiancheng1/gemini-webapi-proxy)](https://github.com/Lutiancheng1/gemini-webapi-proxy/stargazers)
[![Forks](https://img.shields.io/github/forks/Lutiancheng1/gemini-webapi-proxy)](https://github.com/Lutiancheng1/gemini-webapi-proxy/network/members)
[![Issues](https://img.shields.io/github/issues/Lutiancheng1/gemini-webapi-proxy)](https://github.com/Lutiancheng1/gemini-webapi-proxy/issues)
[![macOS](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)](https://github.com/Lutiancheng1/gemini-webapi-proxy)
[![FastAPI](https://img.shields.io/badge/FastAPI-009485?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)](https://www.docker.com)

`pip install git+https://github.com/Lutiancheng1/gemini-webapi-proxy.git`

[English](README.md) · [中文](README.zh.md)

</div>

<p align="center">
  <img src="docs/assets/hero-lemon.png" alt="Lemon watercolor generated through the proxy" width="512">
  <br><em>Generated via <code>POST /openai/v1/images/generations</code> · <code>gemini-2.5-pro-image</code></em>
</p>

> **Fork of [Lutiancheng1/gemini-webapi-proxy](https://github.com/Lutiancheng1/gemini-webapi-proxy)** with:
> - `gemini-webapi` ≥ 2.1.1 — dynamic model list (`gemini-flash`, `gemini-pro`, `gemini-argon`, `gemini-flash-lite` instead of `gemini-3-*`);
> - `stream: true` support (the answer is generated in full, then sent as SSE);
> - tool-calling emulation for agent clients (OpenClaw etc.): tool specs go into the prompt,
>   a `<tool_calls>[...]</tool_calls>` block in the answer becomes OpenAI `tool_calls`;
> - upstream refusal mapped to 502 instead of 403 (clients treat 403 as broken auth).
> - `POST /openai/v1/images/edits` — instruction-based edits of an uploaded image;
> - video generation (Veo via Gemini Web) in the OpenAI Videos API shape: `POST /openai/v1/videos`
>   (`prompt`, optional `input_reference` image) → job; `GET /openai/v1/videos/{id}` → status;
>   `GET /openai/v1/videos/{id}/content` → mp4. Jobs run one at a time and are never retried
>   automatically (a re-sent prompt burns the daily video limit). An image instead of a video
>   means the video limit is used up (`error.code = video_limit_reached`);
> - exhausted Gemini quota → HTTP 429; the model list is re-read when a model is missing.
> - `GEMINI_COOKIE_PATH` in `docker-compose.yml` keeps rotated cookies in `data/` across restarts;
> - `GOP_COOKIE_SOURCE=cdp`: on a server, the session comes from a Chromium container you sign in to once
>   ([`integrations/cdp-browser`](integrations/cdp-browser)); copied desktop cookies die within hours,
>   see [cookie sources](docs/cookie-sources.md#cdp-recommended-for-servers);
> - [`integrations/openclaw/gemini-video`](integrations/openclaw/gemini-video) — an OpenClaw skill that
>   makes videos through `/openai/v1/videos` (env `GEMINI_PROXY_URL`, `GEMINI_PROXY_KEY` via `skills.entries`).

---

## 🤔 Why this project?

Google's official Gemini API requires a paid API key with separate quota. **This
proxy is for people who already have a Gemini Web session** (you log in via
browser, free daily quota) and want to use that session from any OpenAI-style
client — without copy-pasting prompts into the web UI.

- **No API key, no payment.** Uses your existing Gemini Web login (Safari
  cookies, env vars, or a cookies.txt).
- **One server, every OpenAI client.** Point OpenAI Python SDK, LobeChat,
  NextChat, ChatBox, or your own scripts at `http://localhost:4982/openai`.
- **Image gen, not just chat.** Returns base64 PNG/JPEG so downstream
  clients don't have to wrestle with Google's CDN redirects.
- **Docker one-liner.** A long-running container with auto-restart and
  cookie refresh via the macOS host's Safari.

> **Disclaimer:** This project is not affiliated with Google. It uses
> [`gemini-webapi`](https://github.com/HanaokaYuzu/Gemini-API), a
> reverse-engineered client of the Gemini Web frontend. Usage is subject
> to Google's Terms of Service. You are responsible for your own usage.

---

## 📑 Table of contents

- [✨ Features](#-features)
- [🚀 Quick Start](#-quick-start)
- [🔌 Wiring a client](#-wiring-a-client)
- [🖼 Image generation](#-image-generation)
- [✏️ Image edits](#️-image-edits)
- [🎬 Video generation](#-video-generation)
- [🦀 OpenClaw](#-openclaw)
- [⚙️ Configuration](#-configuration)
- [🏗 Architecture](#-architecture)
- [🐳 Deployment & operations](#-deployment--operations)
- [🛣 Roadmap](#-roadmap)
- [🧪 Development](#-development)
- [❓ Troubleshooting](#-troubleshooting)
- [🤝 Contributing](#-contributing)
- [⚖️ License](#-license)
- [🙏 Acknowledgements](#-acknowledgements)

---

## ✨ Features

- 🪄 **Drop-in OpenAI API** — point any OpenAI client (Python SDK, OpenAI CLI, ChatBox, NextChat, LobeChat, Outsider Studio, …) at `http://localhost:4982/openai`
- 💬 **Chat completions** — multi-turn, reference images, `stream=true` (SSE)
- 🧰 **Tool calling (emulated)** — OpenAI `tools` / `tool_calls` for agent clients, via a `<tool_calls>` block in the prompt
- 🖼 **Image generation** — base64 output only (no CDN URL leakage), text-to-image + reference-image-to-image
- ✏️ **Image edits** — `POST /openai/v1/images/edits`: upload an image, describe the change
- 🎬 **Video generation (Veo)** — OpenAI Videos API shape: `POST /openai/v1/videos` → job → mp4
- 🍪 **Pluggable cookie sources** — desktop browser (Safari/Chrome/…), env vars, Netscape cookie file
- 👥 **Several accounts + admin UI** — `/admin/ui`: state and limits per account, sign-in through the server's browser, automatic switch when an account runs out, a proxy per account, request journal, client keys, playground ([docs/accounts.md](docs/accounts.md))
- 🤖 **Telegram bot** — account status and controls, alerts on logged-out / flagged / exhausted accounts ([docs/telegram.md](docs/telegram.md))
- 🐳 **One-command install** — `scripts/install.sh` (Linux, macOS) / `scripts/install.ps1` (Windows): proxy + sign-in browser in Docker, keys generated, auto-restart
- 🔌 **Multi-fallback image download** — httpx (RPC / preview URL) → gemini-webapi library save; no browser in the image
- 🛡 **Upstream refusal detection** — Gemini "I cannot fulfill…" answers become errors (403 images / 502 chat), exhausted quota → 429
- 🛠 **Optional API key** — `GOP_API_KEY=...` enables Bearer auth; empty = off (local-only default)
- 📋 **Live model list** — the account's models (`gemini-flash`, `gemini-pro`, `gemini-argon`, `gemini-flash-lite`) plus stable `gemini-2.5-*-image` aliases via `/openai/v1/models`

## 🚀 Quick Start

### One command (Docker: several accounts, admin UI)

Installs the proxy and the browser used to sign in to Google accounts. The script
writes `.env` (a generated API key and admin password), builds and starts both
containers, waits for `/health` and prints the admin UI address and password.
Running it again keeps `.env` and the data and rebuilds/restarts.

**Linux** (Docker Engine + Compose v2):

```bash
git clone -b accounts-wip https://github.com/Muxee4ka/gemini-webapi-proxy
cd gemini-webapi-proxy
./scripts/install.sh                       # listens on 127.0.0.1:4982
./scripts/install.sh --listen 0.0.0.0      # or on all interfaces
./scripts/install.sh --port 4990           # another port
```

On a remote server, reach the UI through `ssh -N -L 4982:127.0.0.1:4982 <server>` or put
it behind nginx with TLS ([docs/accounts.md](docs/accounts.md#setup) has the WebSocket block
the sign-in screen needs).

**Windows** (Docker Desktop running, PowerShell 5.1 or 7):

```powershell
git clone -b accounts-wip https://github.com/Muxee4ka/gemini-webapi-proxy
cd gemini-webapi-proxy
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
# options: -Listen 0.0.0.0  -Port 4990
```

**macOS** (Docker Desktop running):

```bash
git clone -b accounts-wip https://github.com/Muxee4ka/gemini-webapi-proxy
cd gemini-webapi-proxy
./scripts/install.sh                       # Docker Desktop mode is picked automatically
```

On Docker Desktop the containers do not use host networking
([`docker-compose.desktop.yml`](docker-compose.desktop.yml)): the browser shares the proxy's
network and only the proxy port is published on `127.0.0.1`. A proxy running on the
computer itself is `http://host.docker.internal:<port>` for an account.

Then, on any system:

1. Open `http://127.0.0.1:4982/admin/ui` and sign in with the printed password
   (`GOP_ADMIN_PASSWORD` in `.env`). The UI is in English.
2. Press **Sign in** on the `default` account and sign in to Google in the browser shown on
   the page; add more accounts with **+ Account**, set a proxy per account in its card.
3. Point OpenAI clients at `http://127.0.0.1:4982/openai/v1` with `Bearer <GOP_API_KEY>`
   from `.env`, or create a named key per client in the **Keys** tab.

Manual setup with plain `docker compose`: create `.env` with `GOP_API_KEY=...` and
`GOP_ADMIN_PASSWORD=...` (`.env.example` is for running from source, not for Docker), then
`docker compose up -d --build` (Linux) or
`docker compose -f docker-compose.yml -f docker-compose.desktop.yml up -d --build`
(Docker Desktop).

### Single account on a desktop

You need either:

- **A logged-in Gemini Web session** in Safari/Chrome on macOS or Linux, **or**
- A `__Secure-1PSID` / `__Secure-1PSIDTS` pair from `gemini.google.com`, **or**
- A Netscape-format `cookies.txt` exported from a browser.

Pick one install path:

### Option A: `pip` (PyPI)

```bash
pip install "gemini-webapi-proxy[browser-cookie]"
gemini-webapi-proxy            # starts on :4982
```

### Option B: From source

```bash
git clone https://github.com/Lutiancheng1/gemini-webapi-proxy
cd gemini-webapi-proxy
python3 -m venv .venv
.venv/bin/pip install -e ".[browser-cookie,dev]"
./start.sh
```

### Option C: Docker on macOS (cookies from Safari)

```bash
git clone https://github.com/Lutiancheng1/gemini-webapi-proxy
cd gemini-webapi-proxy
bash scripts/docker-up.sh
```

`scripts/docker-up.sh` syncs your browser cookies into `data/runtime.env`
(only needed on macOS — Linux hosts can use env vars directly), then
launches the container.

In all three cases, verify the service:

```bash
curl -sS http://localhost:4982/health
# {"status":"ok","service":"gemini-webapi-proxy"}
```

See [docs/docker.md](docs/docker.md) for the full deployment guide.

## 🔌 Wiring a client

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:4982/openai/v1",
    api_key="not-verified",   # any non-empty string; or set GOP_API_KEY
)

resp = client.chat.completions.create(
    model="gemini-flash",
    messages=[{"role": "user", "content": "Reply with exactly: pong"}],
)
print(resp.choices[0].message.content)
```

Chat models come from the Gemini account: `gemini-flash`, `gemini-pro`,
`gemini-argon`, `gemini-flash-lite`. See what your deployment advertises with:

```bash
curl -sS http://localhost:4982/openai/v1/models | python3 -m json.tool
```

See [docs/studio.md](docs/studio.md) for connecting OpenAI-compatible
desktop apps, and [docs/api.md](docs/api.md) for the full API reference.

## 🖼 Image generation

```python
import base64, pathlib

img = client.images.generate(
    model="gemini-2.5-pro-image",       # stable alias — see /openai/v1/models
    prompt="a still life watercolor of lemons in a bowl, soft pastel colors",
    n=1,
    response_format="b64_json",
)
pathlib.Path("out.png").write_bytes(base64.b64decode(img.data[0].b64_json))
```

With a reference image:

```python
import base64, pathlib

data_url = (
    "data:image/png;base64,"
    + base64.b64encode(pathlib.Path("ref.png").read_bytes()).decode()
)
img = client.images.generate(
    model="gemini-2.5-pro-image",
    prompt="a portrait in the same style as the reference",
    n=1, size="1024x1024",
    extra_body={"image": [data_url]},
)
pathlib.Path("out.png").write_bytes(base64.b64decode(img.data[0].b64_json))
```

The proxy always returns `b64_json` (never Google CDN URLs) because most
downstream clients can't fetch `lh3.googleusercontent.com/...` directly.
See [docs/api.md](docs/api.md#reference-images) and
[docs/downloaders.md](docs/downloaders.md) for the download chain.

## ✏️ Image edits

```bash
curl -sS -m 300 http://localhost:4982/openai/v1/images/edits \
  -H "Authorization: Bearer $KEY" \
  -F model=gemini-2.5-flash-image \
  -F "prompt=Add a modern kitchen along the right wall. Keep the window and the floor." \
  -F "image=@room.jpg;type=image/jpeg"
```

Same as OpenAI `client.images.edit(...)`. Gemini edits by instruction; `mask` is ignored.

## 🎬 Video generation

```python
import time, httpx

api = httpx.Client(base_url="http://localhost:4982/openai/v1",
                   headers={"Authorization": f"Bearer {KEY}"}, timeout=60)
job = api.post("/videos", json={"prompt": "a ginger cat in a space helmet floating in a space station"}).json()
while job["status"] not in ("completed", "failed"):
    time.sleep(15)
    job = api.get(f"/videos/{job['id']}").json()
if job["status"] == "completed":
    open("cat.mp4", "wb").write(api.get(f"/videos/{job['id']}/content").content)
else:
    print(job["error"])  # video_limit_reached / refused / upstream_glitch
```

A clip (~8 s) takes 1–3 minutes. Pass `input_reference` (an image file or URL) to animate
a picture. Jobs run one at a time and are never retried automatically: every attempt
spends the account's daily video limit. Details: [docs/api.md](docs/api.md#videos-veo).

## 🦀 OpenClaw

Chat models, `image_generate` and a video skill — see [docs/openclaw.md](docs/openclaw.md).

## ⚙️ Configuration

All configuration is via environment variables. The `GOP_` prefix is
recommended; a few unprefixed names (`PORT`, `USE_BROWSER_COOKIES`, …)
are kept as legacy aliases for compatibility.

| Variable | Default | Description |
|----------|---------|-------------|
| `GOP_PORT` | `4982` | listen port |
| `GOP_HOST` | `0.0.0.0` | listen address |
| `GOP_API_KEY` | _(empty)_ | when set, require `Authorization: Bearer <key>` |
| `GOP_COOKIE_SOURCE` | `browser` | `browser` / `env` / `file` / `cdp` |
| `GOP_CDP_URL` | `http://127.0.0.1:9223` | `cdp`: DevTools endpoint of the browser you sign in with |
| `GOP_BROWSER` | `auto` | preferred browser: `auto` / `safari` / `chrome` / `edge` / `brave` / `chromium` |
| `GOP_COOKIE_FILE` | _(empty)_ | path to a Netscape-format cookies.txt |
| `GOP_GEMINI_1PSID` | _(empty)_ | `__Secure-1PSID` value (env / file modes) |
| `GOP_GEMINI_1PSIDTS` | _(empty)_ | `__Secure-1PSIDTS` value |
| `GOP_GEMINI_COOKIES_RAW` | _(empty)_ | `k1=v1; k2=v2` form |
| `GOP_HTTP_PROXY` | _(empty)_ | local HTTP proxy (`http://127.0.0.1:7897`) |
| `GOP_CHAT_TIMEOUT` | `180` | seconds |
| `GOP_IMAGE_TIMEOUT` | `300` | seconds |
| `GOP_INIT_TIMEOUT` | `120` | seconds |
| `GOP_PROBE_ON_START` | `false` | probe models at startup |
| `GOP_LOG_FORMAT` | `console` | `console` (dev) or `json` (container) |
| `GOP_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `GOP_DATA_DIR` | `./data` | model registry, generated videos |
| `GOP_VIDEO_TIMEOUT` | `900` | seconds for one background video job |
| `GOP_VIDEO_KEEP_HOURS` | `24` | how long finished videos are kept |
| `GEMINI_COOKIE_PATH` | _(temp dir)_ | where `gemini-webapi` caches rotated cookies; keep it in a volume |

See [docs/configuration.md](docs/configuration.md) for the full list and
detailed semantics.

## 🏗 Architecture

```
┌─────────────── FastAPI (uvicorn) ───────────────┐
│  /health    /ready                              │
│  /openai/v1/models  /openai/v1/chat/completions │
│  /openai/v1/images/generations  /images/edits   │
│  /openai/v1/videos[/{id}[/content]]             │
│  /admin/probe-models                            │
│                                                 │
│  ┌─────────────┐   ┌──────────────────┐         │
│  │ Cookie      │ → │ GeminiClient pool│ → gemini-webapi
│  │ Source      │   │ (lazy singleton) │         │
│  └─────────────┘   └──────────────────┘         │
│                                                 │
│  ┌────────────────────────── Image download ──┐ │
│  │ httpx (RPC → preview URL)                  │ │
│  │ →  gemini-webapi library save (last resort)│ │
│  └────────────────────────────────────────────┘ │
└────────────────────────────────────────────────┘
```

- **Pluggable cookie sources** (`gemini_webapi_proxy.cookies`) — add a new
  one by subclassing `BaseCookieSource` and registering it.
- **Pluggable image downloaders** (`gemini_webapi_proxy.downloaders`) —
  each strategy is a small class; reorder the chain in `GOP_DOWNLOADER_CHAIN`.
- **Pluggable Gemini client** (`gemini_webapi_proxy.client`) — current
  implementation wraps `gemini-webapi`; future backends can implement
  `BaseGeminiClient` without touching the rest of the code.
- **Curated model registry** (`gemini_webapi_proxy.client.registry`) —
  the account's chat models (`gemini-flash`, `gemini-pro`, …) and two stable
  image aliases (`gemini-2.5-flash-image`, `gemini-2.5-pro-image`).
  The reverse-alias index is guarded so that asking for `gemini-pro`
  can never route to an image alias by accident.

See [docs/architecture.md](docs/architecture.md) for the deeper data flow
(cookie bootstrap → registry sync → request → RPC → downloader chain →
b64 response).

## 🐳 Deployment & operations

- **[Docker on macOS](docs/docker.md)** — long-running container with
  `restart: unless-stopped`, syncs Safari cookies via `scripts/docker-up.sh`.
- **[Docker on Linux](docs/docker.md#linux-host)** — set `GOP_GEMINI_1PSID` /
  `GOP_GEMINI_1PSIDTS` in `.env`; no browser cookie source needed.
- **Bare-metal / venv** — `./start.sh` or `python -m gemini_webapi_proxy`.
- **Cookie refresh** — Safari re-login → `python scripts/sync_runtime_env.py`
  → `docker compose restart`. See
  [docs/troubleshooting.md](docs/troubleshooting.md#cookie-cache).

## 🛣 Roadmap

- [x] **Streaming chat completions** (SSE) — answer generated in full, then streamed
- [x] **Tool calling** — emulated through the prompt
- [x] **Image edits and video generation**
- [ ] **Pluggable Gemini backends** — official Gemini API, third-party proxies
- [ ] **More cookie sources** — Playwright storage state export, Firefox via `browser-cookie3`
- [ ] **Per-model rate limits** — token-bucket per model id
- [ ] **Hot-reload of model registry** — watch `data/model_registry.json`
- [ ] **Prometheus `/metrics` endpoint**

## 🧪 Development

```bash
git clone https://github.com/Lutiancheng1/gemini-webapi-proxy
cd gemini-webapi-proxy
python3 -m venv .venv
.venv/bin/pip install -e ".[browser-cookie,dev]"
.venv/bin/pytest tests/ -v
.venv/bin/ruff check
.venv/bin/ruff format
.venv/bin/mypy src/
bash scripts/e2e-image.sh   # requires a real Gemini session
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contribution workflow,
[docs/architecture.md](docs/architecture.md) for the design, and
[examples/](examples/) for runnable code samples.

## ❓ Troubleshooting

The short version:

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `Failed to connect to localhost:4982` | server not started | `bash scripts/docker-up.sh` or `./start.sh` |
| 401 / `AuthError` from upstream | cookie expired | re-login in Safari, re-run `scripts/sync_runtime_env.py`, `docker compose restart` |
| 403 `"I cannot fulfill..."` | upstream safety filter | try a different prompt, or check the model list |
| 502 with `Multiple cookies exist` | `.com` vs `.com.hk` duplicates | already auto-filtered; if not, `docker compose restart` |
| 502 (b64 download) | every downloader failed | check the session (`GET /admin/quota`); the error lists each strategy's reason |
| Upstream `Unknown model name: gemini-2.5-pro-image` against a `gemini-3-pro` request | fixed in 0.1.0 — re-pull the image and restart | `docker compose pull && docker compose up -d` |

Full troubleshooting matrix: [docs/troubleshooting.md](docs/troubleshooting.md).

## 🤝 Contributing

PRs welcome! Read [CONTRIBUTING.md](CONTRIBUTING.md) for the dev setup,
conventional-commit format, and the release process. Bug reports go to
[GitHub Issues](https://github.com/Lutiancheng1/gemini-webapi-proxy/issues);
security issues go to the address in [SECURITY.md](SECURITY.md), not to
public issues.

## ⚖️ License

This project is licensed under **GPL-3.0-or-later**. See [LICENSE](LICENSE).

It depends on [`gemini-webapi`](https://github.com/HanaokaYuzu/Gemini-API),
which is also GPL-3.0. By installing the runtime dependency, you agree to
its license.

The project is **not affiliated with Google** and provides no warranty.
Use it only in compliance with Google's Terms of Service.

## 🙏 Acknowledgements

- [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) —
  the upstream reverse-engineered client without which this project
  would not exist
- The OpenAI Python SDK team — for a clean, widely-implemented API
  contract that makes this kind of gateway easy to build
- All [contributors](https://github.com/Lutiancheng1/gemini-webapi-proxy/graphs/contributors)
  and early testers
