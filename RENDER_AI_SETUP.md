# ShopNexa Work AI — Render setup

This build keeps the login/dashboard and adds a safer Work AI fallback.

## Required Render environment variables

Set these in the **ShopNexa web service** in Render:

- `SECRET_KEY` — a permanent random secret (Render can generate this).
- `DATABASE_URL` — the internal connection string for `shopnexa-db`.
- `OPENAI_API_KEY` — your own OpenAI API key. Never put this key in GitHub or in HTML/JavaScript.
- `WORK_AI_MODEL` — `gpt-5.6`
- `WORK_AI_IMAGE_MODEL` — `gpt-image-2`
- `WORK_AI_VIDEO_MODEL` — `sora-2`
- `TAVILY_API_KEY` — optional; DuckDuckGo is used as a no-key web-search fallback.

## What Work AI can do

With `OPENAI_API_KEY` configured, Work AI can use the OpenAI Responses API for natural-language commands, plus ShopNexa function tools for products, sales, expenses, staff, settings, reports and navigation. It can also use web search.

The Media Studio can:

- Search the public web for images.
- Search the public web for videos.
- Generate images with GPT Image 2.
- Generate 4/8/12-second Sora clips.
- Build 5–30 minute videos by generating multiple 12-second Sora clips and stitching them server-side with FFmpeg.

Long-video generation is a paid AI workload. The application does not make it free; the OpenAI account used by the server is charged according to the provider's current pricing.

## If the OpenAI key is missing

The app still supports public web search and a small set of explicit fallback commands such as:

- `list products`
- `add product Rice at GHS 100 with 50 in stock`
- `record 2 Rice`
- `remove product Rice`
- `open products`

Full natural-language command planning and AI media generation require the OpenAI key.

## Health check

Open `/health` after deployment. It reports only safe diagnostics such as database type, whether an OpenAI key is configured, whether the media directory is writable, and whether the database responds. It never returns the key itself.
