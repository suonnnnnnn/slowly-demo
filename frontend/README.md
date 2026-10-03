# slowly · Frontend

**English** | [中文](README.zh-CN.md)

A phone-app / mini-program style prototype for following along with a tutorial. The pages are static
files served by `server.cjs` in this directory.

> The UI copy is in Chinese.

## Run locally

Node.js 18 or newer; no dependencies to install. All commands below run in this directory
(`frontend/`).

1. At the repo root, copy `.env.example` to `.env` (shared by both ends).
2. Fill in `OPENROUTER_API_KEY` (used by the default free model); never commit the key. To point it
   at another OpenAI-compatible provider, see "Using another model provider" in the root README.
3. Start the FastAPI backend first, as described in the root README (defaults to
   `http://127.0.0.1:8000`).
4. Run `node server.cjs`.
5. Open http://127.0.0.1:8766/.

`server.cjs` locates `dist/` via `__dirname` and reads `.env` from the parent directory (the repo
root). It is also the API proxy: it handles `/api/chat` itself (calling the backend's `/api/search`
internally), and forwards `/api/videos*`, `/api/saved-tutorials` and `/api/library` to `VIDEO_BACKEND_URL`
unchanged. (`/api/search` is served by the backend only; the browser does not call it directly.)

In the root Docker deployment, this service listens on Railway's injected `PORT`, proxies FastAPI at
`http://127.0.0.1:8000`, and exposes `/health` only when the backend health check also succeeds.

## Install as an app (PWA)

The pages are a PWA: `dist/manifest.webmanifest` plus `dist/sw.js`, with icons under
`dist/icons/`. On a phone, open the site, then "Add to Home Screen" (iOS Safari) or "Install app"
(Android Chrome) — it opens full screen with its own icon, no store and no rewrite needed.

The service worker caches the app shell only. `/api/*` is **never** cached: step progress, download
state and the video stream are live data, and caching them would show last time's result as this
time's. Navigations are network-first with a cached fallback, so the pages still open when the WiFi
drops mid-demo. If you touch anything in `SHELL_ASSETS`, bump `VERSION` in `sw.js`.

To open it on a phone over your local network, set `HOST=0.0.0.0` in `.env` and restart; the
startup log prints the LAN URLs. This demo has no login, so anyone on the same Wi-Fi can use your
backend and your model key — prefer a personal hotspot over a shared network, and set it back when
done.

## The four pages

| Page | What it does |
| --- | --- |
| `dist/index.html` (`/`) | Chat home: ask, tap a suggested question to send it, preview YouTube results in a modal. The composer sits directly above the shared navigation, and "saved to do slowly" is always visible (also reachable from the ⋯ menu) |
| `dist/library.html` | Material library: successful real breakdowns in a hand-drawn grid, searchable and filterable by cooking, everyday tools or other tutorials, using the same 440 px app shell and warm yellow palette as Home |
| `dist/video.html?id=` | One video: plays the original YouTube video while downloading, then switches to the local MP4; `?start=&end=` plays only that segment |
| `dist/steps.html?id=` | Step by step: step strip, representative frame, checks, Q&A, save, re-break-down |

## What works today

- Chat home; tapping a suggested question sends it.
- Chinese/English mode persists across Home, video, steps, the material library and the shared navigation.
- A shared two-tab bottom navigation keeps Home and Material Library one tap away on every page. The library only lists tutorials whose latest real
  breakdown succeeded; failed generic skeletons are not presented as completed material. The default
  free model classifies from the title and screenshot-derived step titles, with title keywords as the
  fallback; neither is presented as understanding the whole video.
- When you ask something, the chat service calls FastAPI to search YouTube live. A failed search
  reports why it failed (unreachable / rate-limited / other) and offers "paste a video link" as an
  alternative — it is **never** reported as "nothing found".
- Tapping a result plays the original YouTube video in a modal on the same page; only "教程分解"
  creates a download job and opens `video.html`.
- The video page keeps playing YouTube during the download and switches to the local MP4 when it is
  done; it links to the steps page via "按步骤做 · 一步步来 →".
- The steps page (`dist/steps.html`):
  - Each step shows one representative frame plus "watch just this segment", which jumps to
    `video.html?start=&end=`.
  - An "I did it" checkbox; "let the model check this step" (the model judges pass / a bit more to
    do / unclear against the criterion, and a pass is recorded automatically); "ask when stuck".
  - "Save" / "Saved". Once saved, a "saved to do slowly" entry shows up on the home page, and the
    list shows progress such as "3 / 10 steps done".
  - "Re-break it down" is one tap: it runs in the background and returns immediately while the page
    polls for progress. The button turns red as a warning when there is progress, but there is no
    second confirmation.
  - The step strip is scrollable on desktop too (the wheel scrolls it horizontally, and you can drag
    it).
  - Steps and progress live in the backend's SQLite (`tutorial_steps` / `step_interactions`), so a
    refresh loses nothing.
  - The breakdown is **real**: `backend/app/video_analysis.py` uses ffmpeg to find scene cuts and
    grab representative frames, then `breakdown.py` sends each segment to a vision model to write
    the title / description / pass criterion. The model sees one screenshot per segment, hears no
    audio and gets no transcript; when a breakdown cannot be done it falls back to a generic
    skeleton (`mock=true` plus an honest note).
  - Checks and Q&A go through `backend/app/coach.py` with the same OpenAI-compatible config as the
    chat. Attached photos are sent to the model by default (a current vision-capable model was
    measured to describe images correctly); for a model that cannot see images, set
    `MODEL_VISION_ENABLED=false`, or point `MODEL_VISION_NAME` at a vision model. When the model
    cannot read a photo it falls back to judging on text alone and says so.
- Text chat uses the model configured by `MODEL_API_BASE` / `MODEL_API_KEY` / `MODEL_NAME`; the
  default is OpenRouter's free `inclusionai/ling-3.0-flash-sante:free`, switchable to any
  OpenAI-compatible provider.
- Local video preview; videos are never uploaded to a server.

## Prototype limits

YouTube results come from a live search and are downloaded to your machine when clicked. Downloading
and playing do not mean the AI has watched or parsed the video. The steps page's breakdown only sees
one screenshot per segment (scene cuts + frame grabs): no audio, no transcription. So what the model
writes is "what is in this frame", not "what this segment is about"; the checking model has not
watched the video either — it only has the pass criterion, your description and an optional photo.
Please only save public videos you are allowed to download. Free models may be rate-limited.

This is a local demo, not a production deployment — see "Scope: a local demo, not a production
deployment" in the root README for the follow-up work. The service binds to localhost only; add user
authentication, request limits and server-side secret management before deploying publicly.
