# Tech Stock Digest — Mistral Workflow

A one-shot Mistral Workflow that:

1. Reads Yahoo Finance (front page + [tech topic page](https://finance.yahoo.com/topic/tech/)) and extracts the latest headlines with their tickers.
2. Uses `mistral-medium-latest` to keep only **tech-industry stock news** (semiconductors, software, cloud, AI, hardware, consumer electronics, …) and drop personal-finance blogs, politics, sports, and other topics.
3. Writes a markdown digest and a podcast-style script (~1–2 minutes of speech).
4. Synthesizes the script as an MP3 with `voxtral-mini-tts-3-0`.
5. Sends the digest to your Gmail inbox with the MP3 attached, via the Gmail connector.

The workflow runs **on behalf of the triggering user** (`on_behalf_of=True`), so the email is sent from the triggering user's connected Gmail account. This requires a **hardened deployment**.

## Input

```json
{
  "recipient_email": "you@gmail.com",
  "max_headlines": 10,
  "podcast_voice": "gb_jane_neutral",
  "dry_run": false
}
```

| Field | Default | Description |
|---|---|---|
| `recipient_email` | `null` | Where to send the digest. Required unless `dry_run` is true. |
| `max_headlines` | `10` | Max headlines kept in the digest. |
| `podcast_voice` | `gb_jane_neutral` | Any Voxtral TTS preset voice. |
| `dry_run` | `false` | Generate digest + audio but skip the email. |

## Run locally

```bash
pip install "mistralai-workflows[mistralai]" pytest
export MISTRAL_API_KEY=...   # .env also works
PYTHONPATH=apps/worker/src python -m pytest -q
PYTHONPATH=apps/worker/src python -m worker.entrypoints.worker
```

Then trigger (or start it from Studio → Workflows → `tech-stock-digest`):

```bash
POST https://api.mistral.ai/v1/workflows/tech-stock-digest/execute
Authorization: Bearer $MISTRAL_API_KEY
{"input": {"recipient_email": "you@gmail.com", "dry_run": true}}
```

## Deploy (managed)

The image builds from `deploy/docker/Dockerfile.worker` (build context: repository root). No API key is stored in the repo: the platform injects a service-account token.

1. Push this repository to GitHub.
2. Create a managed deployment with Dockerfile path `deploy/docker/Dockerfile.worker`, hardened (required for `on_behalf_of`).
3. Trigger from Studio (Workflows → `tech-stock-digest` → Start workflow) or the execute API. The Gmail connector credential is resolved from the triggering user's connection.
