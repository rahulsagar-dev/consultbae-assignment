# Task 2 — Skill Category Auto-Tagger (n8n)

**Picked option:** "A flow with an LLM step that auto-tags each person's skill
category and writes results back."

## What it does

1. **Manual Trigger** — you click "Execute Workflow" (a real deployment
   would swap this for a Schedule Trigger, e.g. every night).
2. **Get Untagged People** — `GET /api/people/untagged` on the Task 3 Flask
   app, which returns everyone in `people` who has a `skills` value but no
   `skill_category` yet.
3. **Split Into Items** — turns the JSON array response into one n8n item
   per person, so the next two nodes run once per person.
4. **Classify Skill Category (LLM)** — sends the person's skill list to an
   LLM with a fixed prompt and five allowed labels (`automation-heavy`,
   `web dev`, `data`, `backend`, `qa-automation`). `temperature: 0` so runs
   are reproducible enough to defend on a call.
5. **Write Tag Back To DB** — `POST /api/people/{person_id}/tag` writes the
   label back into `people.skill_category`.

## Why HTTP Request nodes instead of a database node

n8n doesn't ship a first-class SQLite node. Rather than fight that, the
Flask app from Task 3 already has the DB connection open, so the workflow
just talks to it over HTTP (`/api/people/untagged`, `/api/people/<id>/tag`
— both defined in `audio-app/app.py`). This also means the same two
endpoints could later back a Slack bot, a cron job, or a proper cloud DB
without touching the workflow logic.

## How to actually run this yourself

This JSON is a template, not something that runs itself — you need to:

1. Start the Task 3 app: `cd audio-app && python3 app.py` (leave it running
   on port 5000, or update the URLs in the workflow if you deploy it
   elsewhere).
2. Open n8n (self-hosted `npx n8n` or the free cloud trial) and
   **Import from File** → `skill_tagger_workflow.json`.
3. Add your own OpenAI (or Anthropic) credential and select it on the
   "Classify Skill Category" node — the credential ID in this file is a
   placeholder, it won't work as-is.
4. If your n8n version names the OpenAI/Chat node differently, swap the
   node but keep the same prompt and the same output field
   (`$json.message.content`) feeding the write-back node.
5. Click **Execute Workflow**, then check `/submissions` or query
   `people.db` directly to confirm `skill_category` got filled in.
6. Re-export via **Download** so this JSON file reflects what you actually
   ran, and show the execution log in your video.

## What I'd change for 5,000 people instead of ~60

Manual Trigger → Schedule Trigger (nightly), add a rate-limit/batch node
before the LLM call so you're not firing 5,000 parallel API calls at once,
and add a retry/error branch so one failed classification doesn't stop the
whole run.
