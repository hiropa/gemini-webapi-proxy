---
name: gemini-video
description: Generate a short video (Veo, ~8 s, mp4) from a text prompt or animate an image, through our Gemini WebAPI proxy. Use when the user asks to make, generate or animate a video/clip/animation, including "animate this picture".
metadata: { "openclaw": { "requires": { "bins": ["python3"], "env": ["GEMINI_PROXY_URL", "GEMINI_PROXY_KEY"] } } }
---

# Gemini video

Video is made by Veo inside the user's Gemini Web account, through the proxy at
`$GEMINI_PROXY_URL`. One clip takes about 1–3 minutes.

## Run

Text to video:
```bash
python3 {baseDir}/scripts/gen_video.py --prompt "a ginger cat in a space helmet floating in a space station, soft light"
```

Animate an image (a file the user sent, or a URL):
```bash
python3 {baseDir}/scripts/gen_video.py --image /path/to/photo.jpg --prompt "the cat slowly turns its head and blinks"
```

Options: `--seconds 8` (a hint, Veo decides), `--size 720x1280` for vertical,
`--out-dir` (default `video-output` in the workspace).

Run it with a generous exec timeout (at least 15 minutes) and wait for it to finish.

## Deliver

On success the script prints `VIDEO_PATH=/abs/path.mp4`. Reply with a short caption
and the file on its own line, exactly like this (no backticks, no bold):

MEDIA:/abs/path.mp4

## When it fails

The script prints `FAILED code=... message=...` and exits with 2.

- `video_limit_reached` — the daily video limit of the Gemini account is used up.
  Tell the user; do not retry today.
- `refused` — Gemini refused the prompt. Rewrite it without brand, cartoon or
  celebrity names and without real people's names, then try once more if the user wants.
- `upstream_glitch` / `timeout` — a transient Gemini failure. Try again once,
  after a few minutes, not immediately.

## Rules

- One video at a time. Never start a second job while one is running.
- Never re-run automatically after a failure: every attempt spends the account's
  limited daily video quota, and too many requests can get the Google account flagged.
- Write prompts in English: describe the scene, motion and camera, in one or two sentences.
