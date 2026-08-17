# Task 2 — Skill Category Auto-Tagger (n8n)

**Picked option:** "A flow with an LLM step that auto-tags each person's
skill category and writes results back."

## What it does

1. **Manual Trigger** — you click "Execute Workflow" (a real deployment
   would swap this for a Schedule Trigger, e.g. every night).
2. **Get Untagged People** — `GET /api/people/untagged` on the Task 3 Flask
   app, which returns everyone in `people` who has a `skills` value but no
   `skill_category` yet.
3. **Classify Skill Category (LLM)** — an **HTTP Request** node (not the
   built-in n8n OpenAI node — see "What changed from the original plan"
   below) calling Groq's chat completions API, which is OpenAI-compatible.
   Sends each person's skill list with a fixed system prompt and five
   allowed labels (`automation-heavy`, `web dev`, `data`, `backend`,
   `qa-automation`). `temperature: 0` for reproducibility. Batched at 1
   item per 2 seconds to stay under Groq's free-tier rate limit.
4. **Write Tag Back To DB** — `POST /api/people/{person_id}/tag` writes the
   label back into `people.skill_category`.

## What changed from the original plan (and why)

- **n8n's built-in OpenAI node wasn't installed** in this n8n instance
  ("Install this node to use it" error). Rather than pull in the
  LangChain community package for one HTTP call, swapped it for a plain
  **HTTP Request** node calling the API directly — more transparent about
  exactly what's being sent, zero extra dependencies.
- **No OpenAI API key available**, so switched providers to **Groq**
  (`https://api.groq.com/openai/v1/chat/completions`) — free tier, and
  exposes an OpenAI-compatible request/response shape, so nothing
  downstream needed to change.
- **Model name:** currently `openai/gpt-oss-120b`. Groq retired the model
  I started with (`llama-3.1-8b-instant`) partway through building this —
  a real example of what breaks in a production automation that depends
  on a third-party model catalog. If this stops working again, check
  Groq's current supported models and swap the `"model"` field in the
  node's JSON body.
- **Auth header needs the `Bearer ` prefix** — the credential's Value
  field must be `Bearer <your_groq_key>`, not just the raw key.
- **`127.0.0.1` instead of `localhost`** in the URLs — n8n on Windows
  failed to resolve `localhost` in testing; `127.0.0.1` worked.

## Why HTTP Request nodes instead of a database node

n8n doesn't ship a first-class SQLite node. Rather than fight that, the
Flask app from Task 3 already has the DB connection open, so the workflow
just talks to it over HTTP (`/api/people/untagged`, `/api/people/<id>/tag`
— both defined in `audio-app/app.py`). This also means the same two
endpoints could later back a Slack bot, a cron job, or a proper cloud DB
without touching the workflow logic.

## How to actually run this yourself

This JSON reflects what was actually run, but still needs your own
credentials to execute on a fresh machine:

1. Start the Task 3 app: `cd audio-app && python app.py` (leave it running
   on 127.0.0.1:5000, or update the URLs in the workflow if deployed
   elsewhere).
2. Open n8n and **Import from File** → `skill_tagger_workflow.json`.
3. Add your own Groq (or OpenAI) credential — Generic Credential Type →
   Header Auth, Name = `Authorization`, Value = `Bearer YOUR_KEY` — and
   select it on the "Classify Skill Category" node.
4. Confirm the `"model"` field in that node's JSON body is still a
   currently-supported model on your provider (see note above — provider
   model catalogs change).
5. Click **Execute Workflow**, then check `/submissions` or run
   `python merge/check_db.py` to confirm `skill_category` got filled in,
   without needing to re-run `merge.py` (which rebuilds the DB and would
   wipe this).
6. Re-export via **Download** if you change anything, so this JSON file
   stays in sync with what was actually run.

## What I'd change for 5,000 people instead of ~60

Manual Trigger → Schedule Trigger (nightly); the current 1-item/2-second
batching would take ~2.7 hours serially for 5,000 people, so I'd move to
small parallel batches (e.g. 5 at a time) with retry/backoff on 429s
instead of either "all at once" or strictly one at a time. I'd also add an
error branch so one failed classification doesn't stop the whole run.