# ConsultBae AI Automation Assignment

## Structure

```
merge/           Task 1 — ETL pipeline, SQLite DB, auto-generated issues report
automation/       Task 2 — n8n workflow JSON + explanation
audio-app/       Task 3 — Flask app: submit + auto-analyze audio
data/            Raw source CSVs (unmodified)
README.md        This file — setup, Task 4 report, stuck log, Task 5
```

## Setup

Requires Python 3.10+, ffmpeg (`ffmpeg -version` to check), and Node (only
if you self-host n8n via `npx n8n`).

```bash
pip install -r audio-app/requirements.txt
pip install rapidfuzz  # used by merge.py for the fuzzy-duplicate check

# Task 1: build the merged database
cd merge
python3 merge.py
# -> writes people.db and issues_found.md
# NOTE: this drops and rebuilds people.db from the raw CSVs every run.
# Re-running it after Task 2/3 have written skill_category or audio
# submissions will wipe that data — re-run the n8n workflow (Task 2)
# afterward if you do this. To just inspect current DB state without
# rebuilding, use: python check_db.py

# Task 3: run the audio app (uses merge/people.db)
cd ../audio-app
python3 app.py
# -> http://localhost:5000 to submit, http://localhost:5000/submissions to view

# Task 2: see automation/README.md — needs your own n8n + LLM credentials
```

> **Important:** `merge.py` deletes and rebuilds `people.db` from scratch
> every time it runs, by design — the merge should always be reproducible
> from the raw CSVs. That means running it again **after** you've run the
> n8n automation (Task 2) or submitted audio (Task 3) will wipe those
> results, since everything lives in the same database file. Only re-run
> `merge.py` when you actually want a fresh rebuild; otherwise leave it
> alone once Tasks 2 and 3 have added their own data.
>
> To check the current state of the database without touching it, run:
> `python merge/check_db.py` — prints how many people are tagged with a
> skill category and how many audio submissions exist.

## Task 4 — Data Issues Report

Every issue below was actually caught while parsing (see `merge/merge.py`
and the full auto-generated log in `merge/issues_found.md` — this section
is a curated summary, not a rewrite).

| # | Issue | Where | How handled |
|---|-------|-------|-------------|
| 1 | Phone numbers in 4+ formats (`+919000000254`, `09000000287`, `919000000268`, plain 10-digit) | All 3 sources | Strip non-digits, keep last 10 digits as the canonical key |
| 2 | Same city under different names (Gurgaon/Gurugram, Delhi/New Delhi/Delhi NCR, Bangalore/Bengaluru), plus stray casing and trailing spaces | All 3 sources | Lowercased, trimmed, then mapped through an alias table to one canonical name |
| 3 | `Current CTC` mixes two units: plain annual rupees (417964) and lakhs-per-annum as a bare decimal (4.2 meaning ₹4.2L) | source1 | Any value < 1000 is treated as LPA and multiplied by 100,000 |
| 4 | `Applied Date` in 4 different formats (`24-07-2026`, `2026-08-08`, `7 Jul 2026`, `07/13/2026`) | source1 | Parsed against a list of known formats; anything that doesn't match is logged, not silently dropped |
| 5 | Skill names inconsistently cased/named across sources (`REST APIs` vs `rest apis`, `Web Scraping` vs `web scraping`) | source1 & source2 | Canonicalized to one spelling per known skill before merging skill sets |
| 6 | Gig `rate` mixes hourly (`1415/hr`) and monthly (`73k/month`) figures | source2 | Monthly converted to an hourly-equivalent (÷160 hrs), original raw value kept alongside for audit |
| 7 | A fully blank row | source2, row 10 | Detected and dropped (not inserted as a person) |
| 8 | A column-shifted row — values rotated one column left, so `skill_tags` landed in `email_id` | source2, row 18 | Detected by "email_id has no `@` but has commas", columns realigned before parsing. It's a duplicate of the Isha Chopra row above it once uncorrupted, so it merges rather than creating a ghost record |
| 9 | A second header row pasted mid-file (looks like two exports concatenated) | source3, row 15 | Detected (row content == header) and dropped, not ingested as a person named "Name" |
| 10 | Same person applied twice under slightly different names (`R. Verma` vs `Rohit Verma`, same email+phone) | source1 | Same phone key → merged, kept the fuller name |
| 11 | Same person, same everything, but two different email addresses (`alt.nikhil.chopra70@...` vs `nikhil.chopra70@...`) | source1 | Same phone key → merged, kept the first email seen |
| 12 | Email case inconsistency (`ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG` vs lowercase) | source2 | Lowercased before use as a match key |
| 13 | Two different real people share a name (`Arjun Mehta` appears twice in source3 with different phones; `Deepak Nair` appears twice in source2 with different emails/cities) | source1↔3, source2 | **Not** auto-merged — matching is phone/email only. Name-only collisions are logged as `possible_duplicate_needs_review` and kept as separate people, because guessing wrong here silently corrupts the dataset |
| 14 | No single ID field common to all 3 files | design constraint | Phone (normalized) is the primary match key since it's present in source1+source3; email is the fallback since it's present in source1+source2; a person present in only one file with only a name has no way to be cross-matched and is intentionally left standalone |

**Result:** 40 + 30 + 30 = 100 raw rows → **60 unified people**, 25 of whom
were matched across 2+ sources. Full row-by-row log: `merge/issues_found.md`.

## Task 5 — Stretch: launching to 5,000 gig workers in a weekend

**What breaks first:** the app as built runs on a single SQLite file and a
single Flask process with local disk storage — none of that survives real
concurrency. SQLite serializes writes, so simultaneous submissions from
hundreds of workers at once will start queueing or timing out well before
5,000 users. Local disk storage means audio files vanish on redeploy and
there's no CDN, so playback in `/submissions` gets slow fast.

**What I'd change before launch:**
- **Storage:** move audio files to S3/GCS (or equivalent) instead of local
  disk; store only the object URL in the DB.
- **Database:** move off SQLite to Postgres (or managed MySQL) so writes
  don't serialize, and add a unique constraint on normalized phone so
  duplicate people can't silently multiply under load.
- **Uploads:** cap file size (already do, 25MB) and add server-side
  transcoding to one format so `ffprobe`/`pydub` don't choke on a weird
  container from an old Android phone.
- **Failures:** client-side retry with exponential backoff on upload
  failure, and an idempotency key per submission so a retried request
  doesn't create two records for the same person.
- **Duplicates:** the phone-based `find_or_create_person` logic already
  guards against duplicate people, but at 5,000 concurrent users there's a
  race condition — two simultaneous first-time submissions from the same
  phone number could both hit "not found" and both insert. Needs a unique
  DB constraint + `INSERT ... ON CONFLICT`, not an app-level check.
- **Cost:** audio storage + LLM classification calls are the main variable
  costs; batch the LLM tagging (nightly, not per-submission) rather than
  calling it synchronously in the request path.

## Stuck Log

### 1. `UnicodeEncodeError` writing `issues_found.md` on Windows

- **What happened:** `merge.py` ran fine on the first Linux test but crashed
  on my Windows machine with `UnicodeEncodeError: 'charmap' codec can't
  encode character '\u2192'` — the `→` arrow character in the summary line
  of the issues report.
- **Why:** Python's `open(path, "w")` uses the OS default encoding when you
  don't specify one. On Linux that default is UTF-8, so it never showed up
  there. On Windows the default is `cp1252`, which can't represent an arrow
  character, so the exact same code fails only on Windows.
- **What I did:** Added `encoding="utf-8"` explicitly to the `open()` call
  that writes `issues_found.md`, instead of removing the arrow character —
  fixing the encoding is the correct fix since other non-ASCII content
  (e.g. skill names, city names) could hit the same wall later, not just
  this one arrow.
- **What I rejected:** Just deleting the `→` character to dodge the error
  would have papered over the actual bug (unsafe default encoding) rather
  than fixing it.

### 2. `ModuleNotFoundError: No module named 'audioop'` starting the Flask app

- **What happened:** `python app.py` crashed immediately on import, tracing
  back through `pydub` to `import audioop`.
- **Why:** `audioop` was a Python standard-library module that got removed
  starting in Python 3.13. I'm running 3.14, and `pydub` (last updated
  before this change) still expects it to exist.
- **What I did:** Installed `audioop-lts`, a backport package that restores
  the module, and added it to `requirements.txt` with a version marker
  (`audioop-lts; python_version >= "3.13"`) so it only installs on Python
  versions that actually need it — not a blanket dependency that could
  conflict on older Python.
- **What I rejected:** Downgrading to Python 3.12 would also have "fixed"
  it, but that's a much bigger, riskier change than adding one small
  package, and it doesn't fix the problem for anyone else who clones this
  repo on a current Python.

### 3. n8n's OpenAI node wouldn't load ("Install this node to use it")

- **What happened:** The `n8n-nodes-base.openAi` node from the workflow
  template showed a broken icon and refused to run — "This node is not
  currently installed... newer version of n8n, a custom node, or has an
  invalid structure."
- **What I tried first:** Looked for a way to install the missing node
  package inside the n8n UI, but that would mean pulling in the LangChain
  community node package just for one HTTP call.
- **What I did instead:** Replaced it with a plain **HTTP Request** node
  calling the API directly — a core node guaranteed to exist in any n8n
  install. I also had no OpenAI API key, so I switched providers to Groq,
  which exposes an OpenAI-compatible endpoint (`/openai/v1/chat/completions`)
  and has a genuinely free tier. The request/response shape is identical,
  so the rest of the workflow (parsing `choices[0].message.content`) didn't
  need to change.
- **Second snag inside this same fix:** the request kept failing with
  "Invalid API Key" even though the key was correct — the credential's
  header value was missing the required `Bearer ` prefix. Groq (like
  OpenAI) expects the Authorization header as `Bearer <key>`, not the raw
  key alone.
- **What I rejected:** I could have kept fighting to install the missing
  community node, but that adds a dependency and a version constraint for
  no real benefit over a plain HTTP call — the HTTP Request node is more
  transparent about exactly what's being sent anyway, which matters more
  for defending it live than using a prettier abstraction.

### 4. Groq returned "the service is receiving too many requests from you"

- **What happened:** Once the LLM node worked, running the full workflow
  against all 55 untagged people failed partway through with a 429 rate
  limit error from Groq.
- **Why:** n8n was firing all 55 HTTP requests to the LLM node back-to-back
  with no delay, which blew past Groq's free-tier requests-per-minute cap.
- **What I did:** Added batching to the node's Options (1 item per batch,
  2000ms interval), so requests go out one at a time with a 2-second gap
  instead of all at once. Slower, but reliable.
- **What I'd do differently at real scale (see Task 5):** for 5,000 people
  instead of 55, a fixed 2-second serial delay would take almost 3 hours.
  I'd batch in small parallel groups (e.g. 5 at a time) with backoff/retry
  on 429s, rather than either "all at once" or "strictly one at a time."

### 5. Groq retired the model mid-project ("resource could not be found")

- **What happened:** the workflow, which had been working, suddenly failed
  with a 404-style "resource you are requesting could not be found" on the
  LLM node.
- **Why:** the model name in my request body (`llama-3.1-8b-instant`) had
  been deprecated/retired on Groq's side between when I first configured
  the node and when I ran it again later.
- **What I did:** checked Groq's current model list and swapped in the
  currently-supported model name in the request body.
- **Why this is worth flagging, not just fixing quietly:** this is exactly
  the kind of failure that breaks real production automations — a
  third-party provider changes something out from under you with no
  warning. If this were scheduled and unmonitored (see Task 5), it would
  have silently stopped tagging people until someone noticed the gap.